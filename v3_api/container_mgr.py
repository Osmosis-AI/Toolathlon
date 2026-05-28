"""Container lifecycle manager for v3 task executions.

Sessionless variant of v2's container_mgr.  Key differences from v2:

  * No ``SessionState`` parameter — every execution stands alone.  All
    state mutations go through ``manager`` (see ``execution_manager.py``).
  * Shared-infrastructure handling is split:
      - ``fast_shared_infra_health_check`` runs the extracted probe script
        and reports (healthy?, error message).
      - ``run_shared_infra_deploy`` runs the full deploy_containers.sh
        pipeline.  Both are called from the manager under ``infra_lock``.
  * Container teardown is centralised in ``teardown_container`` so the
    manager's converging cleanup path can call one function regardless of
    which terminal condition fired (setup fail, watchdog, grade, DELETE,
    reaper, shutdown).
  * Per-task containers use the ``toolathlon-v3-`` infix and ``dumps_v3/``
    output dir so v2 and v3 can coexist on the same host.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from .execution_manager import ExecutionState, log, manager
from .models import GradeResponse, ToolDef

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TASKS_DIR = PROJECT_ROOT / "tasks" / "finalpool"
DUMPS_BASE = PROJECT_ROOT / "dumps_v3"

DEFAULT_IMAGE = "lockon0927/toolathlon-task-image:1016beta"
DEFAULT_EVAL_CONFIG = "scripts/formal_run_v0.json"
DEFAULT_MAX_STEP = 100
DEFAULT_MODEL_SHORT_NAME = "v3-sandbox-model"
DEFAULT_PROVIDER = "unified"

GATEWAY_STARTUP_TIMEOUT = int(os.environ.get("TOOLATHLON_V3_GATEWAY_STARTUP_TIMEOUT", "900"))
TOOL_QUERY_TIMEOUT = 10
LONG_STEP_TIMEOUT = 30 * 60
DEPLOY_TIMEOUT = 30 * 60          # outer cap on deploy_containers.sh
PROBE_TIMEOUT = 60                 # outer cap on fast shared-infra probe
                                   # — with 5-attempt retries (up to ~20s
                                   # under transient flake), bump from 30 to
                                   # 60 to leave headroom.

FILES_TO_COPY = [
    "configs",
    "deployment/k8s",
    "scripts",
    "deployment/canvas/logs",
    "global_preparation/check_installation.py",
    "local_binary/github-mcp-server",
    "utils",
    "main.py",
]


# ── Runtime / config helpers ────────────────────────────────────────

def _get_container_runtime() -> str:
    try:
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "configs"))
        from global_configs import global_configs
        return global_configs.get("podman_or_docker", "docker")
    except Exception:
        return "docker"


def _get_instance_prefix() -> str:
    try:
        import yaml
        config_path = PROJECT_ROOT / "configs" / "ports_config.yaml"
        if config_path.exists():
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
                return config.get("instance_prefix", "") or ""
    except Exception:
        pass
    return ""


def _get_image_name() -> str:
    return os.environ.get("TOOLATHLON_V3_IMAGE", os.environ.get("TOOLATHLON_V2_IMAGE", DEFAULT_IMAGE))


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_cmd(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


async def _run_cmd_async(cmd: List[str], timeout: int = 300) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout.decode("utf-8", errors="replace")


# ── Shared-infrastructure: fast probe + full deploy ─────────────────

async def fast_shared_infra_health_check() -> Tuple[bool, Optional[str]]:
    """Run the behavioral shared-infra probe (``probe_shared_infra.py``).

    Returns ``(True, None)`` if every required shared service round-trips
    successfully, else ``(False, "<details>")`` where details is the
    probe's stderr (one ``✗`` line per failing service plus per-check
    timings).  The probe runs all four checks (Canvas, Woo, Poste,
    kind) in parallel so happy-path wall time is ~1.5-2s.
    """
    probe = PROJECT_ROOT / "global_preparation" / "probe_shared_infra.py"
    if not probe.exists():
        return False, f"probe script missing: {probe}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "python", "-m", "global_preparation.probe_shared_infra",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"probe timed out after {PROBE_TIMEOUT}s"
    except Exception as e:
        return False, f"probe exec failed: {e!r}"

    err_text = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return True, None
    return False, err_text or f"probe exit={proc.returncode}"


async def run_shared_infra_deploy() -> None:
    """Run ``deploy_containers.sh``.  Raises on non-zero exit or timeout.

    Called by the manager under ``infra_lock`` after a failed health check.
    """
    deploy_script = PROJECT_ROOT / "global_preparation" / "deploy_containers.sh"
    if not deploy_script.exists():
        raise RuntimeError(f"deploy script missing: {deploy_script}")

    log("Running deploy_containers.sh ...")
    returncode, stdout = await _run_cmd_async(
        ["bash", str(deploy_script), "true"],
        timeout=DEPLOY_TIMEOUT,
    )
    if returncode != 0:
        raise RuntimeError(f"deploy_containers.sh failed (exit {returncode}): {stdout[-2000:]}")
    log("deploy_containers.sh done")


# ── Execution start ─────────────────────────────────────────────────

def _container_name_for(task_id: str, execution_id: str) -> str:
    prefix = _get_instance_prefix()
    return f"{prefix}toolathlon-v3-{task_id}-{execution_id[-8:]}"


def allocate_execution_resources(task_id: str) -> Tuple[str, int, str, str]:
    """Reserve the host-side artifacts that an admission needs before
    creating the ExecutionState: a unique container name, an OS-assigned
    gateway port, the gateway URL, and the host output folder path.

    The caller (router) feeds these into ``manager.try_admit`` so the
    ExecutionState is born with stable identifiers.  Output folder is
    created here so failure to mkdir surfaces before admission.
    """
    execution_id_seed = uuid.uuid4().hex[:8]
    container_name = _container_name_for(task_id, f"exec_{execution_id_seed}")
    gateway_port = _allocate_port()
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    output_folder = DUMPS_BASE / task_id / execution_id_seed
    output_folder.mkdir(parents=True, exist_ok=True)
    return container_name, gateway_port, gateway_url, str(output_folder.resolve())


def spawn_setup(execution: ExecutionState) -> None:
    """Spawn the async setup task on a newly-admitted execution."""
    execution.setup_task = asyncio.create_task(_run_setup(execution))


async def _run_setup(execution: ExecutionState) -> None:
    """Background container spin / file copy / preprocess / gateway boot.

    Updates ``execution.setup_phase`` as it progresses.  On any exception
    (incl. ``CancelledError``), calls ``manager.cleanup_execution`` which
    releases locks and tears down the container — so a subsequent
    ``/status`` poll returns 404.
    """
    runtime = _get_container_runtime()
    image = _get_image_name()
    container_name = execution.container_name
    gateway_port = execution.gateway_port
    gateway_url = execution.gateway_url
    output_folder_str = execution.output_folder
    task_id = execution.task_id
    exec_id = execution.execution_id
    task_source = TASKS_DIR / task_id
    task_dir_arg = f"finalpool/{task_id}"

    container_started = False
    try:
        if not task_source.is_dir():
            raise FileNotFoundError(f"Task source missing: {task_source}")

        # Step 1: Start container
        execution.setup_phase = "container_start"
        start_cmd = [runtime, "run", "-d", "--name", container_name, "--network", "host"]
        if runtime == "podman":
            for sock_path in ["/run/podman/podman.sock", f"/run/user/{os.getuid()}/podman/podman.sock"]:
                if os.path.exists(sock_path):
                    start_cmd += ["-v", f"{sock_path}:/run/podman/podman.sock"]
                    break
            start_cmd += ["-e", "KIND_EXPERIMENTAL_PROVIDER=podman"]
        elif runtime == "docker":
            if os.path.exists("/var/run/docker.sock"):
                start_cmd += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
        # Bind-mount the shared .mcp-auth dir so OAuth refresh writes from
        # mcp-remote (e.g. the notion_official server used by the page
        # duplicator) persist back to the host filesystem.  Without this,
        # rotated refresh_tokens die with the container and the next
        # container reads stale tokens, breaking the OAuth grant.  The
        # ``configs/.mcp-auth`` path matches MCP_REMOTE_CONFIG_DIR set in
        # configs/mcp_servers/notion_official.yaml.
        mcp_auth_host = (PROJECT_ROOT / "configs" / ".mcp-auth").resolve()
        mcp_auth_host.mkdir(parents=True, exist_ok=True)
        start_cmd += ["-v", f"{mcp_auth_host}:/workspace/configs/.mcp-auth"]
        start_cmd += [
            "-v", f"{output_folder_str}:/workspace/dumps",
            "-v", f"{output_folder_str}:/workspace/logs",
            "-w", "/workspace",
            image,
            "sleep", "5400",
        ]
        result = _run_cmd(start_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Container start failed: {result.stderr}")
        container_started = True
        log(f"Container {container_name} started")

        # Step 2: Wait for container exec readiness
        execution.setup_phase = "container_ready"
        for _ in range(30):
            check = _run_cmd([runtime, "exec", container_name, "echo", "ready"], timeout=10)
            if check.returncode == 0:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"Container {container_name} not ready after 30s")

        # Step 3: Copy project files
        execution.setup_phase = "files_copied"
        for item in FILES_TO_COPY:
            src = PROJECT_ROOT / item
            if not src.exists():
                continue
            if src.is_dir():
                # Always mkdir the destination AND use the ``src/.`` "contents
                # only" cp pattern.  Plain ``docker cp src dest`` puts src
                # inside dest when dest exists — which happens whenever a
                # bind-mount earlier in this docker run causes Docker to
                # auto-create the destination's parent (e.g. configs/ is
                # auto-created here because configs/.mcp-auth is bind-mounted).
                # Without this pattern, configs/ contents would land at
                # /workspace/configs/configs/* instead of /workspace/configs/*.
                _run_cmd([runtime, "exec", container_name, "mkdir", "-p", f"/workspace/{item}"])
                _run_cmd([runtime, "cp", f"{src}/.", f"{container_name}:/workspace/{item}/"])
            else:
                parent = str(Path(item).parent)
                if parent != ".":
                    _run_cmd([runtime, "exec", container_name, "mkdir", "-p", f"/workspace/{parent}"])
                _run_cmd([runtime, "cp", str(src), f"{container_name}:/workspace/{item}"])

        _run_cmd([runtime, "exec", container_name, "mkdir", "-p", "/workspace/tasks/finalpool"])
        _run_cmd([runtime, "cp", str(task_source), f"{container_name}:/workspace/tasks/finalpool/"])

        copy_config_cmd = (
            "for dir in ~/.gmail-mcp ~/.calendar-mcp; do "
            "mkdir -p $dir && "
            "cp ./configs/gcp-oauth.keys.json $dir/ 2>/dev/null; "
            "cp ./configs/google_credentials.json $dir/credentials.json 2>/dev/null; "
            "done"
        )
        _run_cmd([runtime, "exec", container_name, "bash", "-c", copy_config_cmd])

        mcp_auth_src = PROJECT_ROOT / "configs" / ".mcp-auth"
        if mcp_auth_src.is_dir():
            _run_cmd([runtime, "exec", container_name, "mkdir", "-p", "/root/.mcp-auth"])
            _run_cmd([runtime, "cp", f"{mcp_auth_src}/.", f"{container_name}:/root/.mcp-auth/"])
        log(f"Files copied to container {container_name}")

        # Step 4: Run preprocess
        execution.setup_phase = "preprocess"
        preprocess_cmd = (
            f"uv run python -m scripts.decoupled.container_preprocess "
            f"--eval_config {DEFAULT_EVAL_CONFIG} "
            f"--task_dir {task_dir_arg} "
            f"--max_steps_under_single_turn_mode {DEFAULT_MAX_STEP} "
            f"--model_short_name {DEFAULT_MODEL_SHORT_NAME} "
            f"--provider {DEFAULT_PROVIDER} "
            f"--bundle_file /workspace/dumps/task_bundle.json "
            f"--host_output_folder {output_folder_str} "
            f"--debug"
        )
        exec_env = ["--env", "DOCKER_API_VERSION=1.44"]
        returncode, stdout = await _run_cmd_async(
            [runtime, "exec"] + exec_env + [container_name, "bash", "-c", preprocess_cmd],
            timeout=LONG_STEP_TIMEOUT,
        )
        if returncode != 0:
            raise RuntimeError(f"Preprocess failed (exit {returncode}): {stdout[-2000:]}")
        log(f"Preprocess done for {task_id}")

        # Step 4.5: Withhold grader-side artifacts from the agent
        # ----------------------------------------------------------
        # The agent runs in the container as root, so file-permission
        # tricks don't help — only physical absence does.  After
        # preprocess has finished, we stash these per-task dirs to a
        # host-side location and ``rm -rf`` them from the container so
        # the agent's tool calls have no way to read groundtruth /
        # grading scripts / preprocess code that could short-circuit
        # the task.  ``run_eval`` later restores ``evaluation/`` and
        # ``groundtruth_workspace/`` from the stash before invoking the
        # grader.  ``preprocess/`` is not restored — not needed for
        # grading.
        task_in_container = f"/workspace/tasks/finalpool/{task_id}"
        # Host-side stash, namespaced by instance prefix so co-resident
        # v3 services (different repo checkouts, different ports) never
        # touch each other's stash on startup-reconcile or teardown.
        # exec_id is a uuid so per-exec collisions are statistically zero;
        # the prefix is purely for cross-service operational isolation.
        instance_prefix = _get_instance_prefix() or "default"
        stash_root = Path(f"/tmp/v3-task-stash/{instance_prefix.rstrip('-')}")
        stash_dir = stash_root / execution.execution_id
        stash_dir.mkdir(parents=True, exist_ok=True)
        execution.task_stash_dir = str(stash_dir)
        for sub in ("preprocess", "evaluation", "groundtruth_workspace"):
            if not (task_source / sub).is_dir():
                continue  # some tasks legitimately lack one of these
            dest = stash_dir / sub
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            _run_cmd(
                [runtime, "cp", f"{container_name}:{task_in_container}/{sub}", str(dest)],
                timeout=120,
            )
            _run_cmd(
                [runtime, "exec", container_name, "rm", "-rf", f"{task_in_container}/{sub}"],
                timeout=30,
            )
        log(f"Withheld preprocess/evaluation/groundtruth for {task_id} (stash={stash_dir})")

        # Step 5: Start gateway (as root — same user the container's
        # default exec runs as).  MCP servers + python_execute inherit
        # this user; physical absence of the grader-side dirs (Step
        # 4.5) is what keeps the agent away from groundtruth.
        execution.setup_phase = "gateway_boot"
        gateway_cmd = (
            f"nohup uv run python -m scripts.decoupled.container_tool_gateway "
            f"--bundle_file /workspace/dumps/task_bundle.json "
            f"--host 0.0.0.0 --port {gateway_port} --debug "
            f"> /dev/null 2>&1 & echo $!"
        )
        exec_env = ["--env", "DOCKER_API_VERSION=1.44"]
        result = _run_cmd(
            [runtime, "exec"] + exec_env + [container_name, "bash", "-c", gateway_cmd]
        )
        gateway_pid = result.stdout.strip()
        log(f"Gateway started (PID={gateway_pid}) on port {gateway_port}")

        # Step 6: Wait for gateway /health
        for _ in range(GATEWAY_STARTUP_TIMEOUT):
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{gateway_url}/health")
                    if resp.status_code == 200:
                        break
            except Exception:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"Gateway not ready on port {gateway_port} after {GATEWAY_STARTUP_TIMEOUT}s")

        # Step 7: Query tool schemas
        execution.setup_phase = "tool_query"
        tools: List[ToolDef] = []
        try:
            async with httpx.AsyncClient(timeout=TOOL_QUERY_TIMEOUT) as client:
                resp = await client.get(f"{gateway_url}/tools")
                resp.raise_for_status()
                data = resp.json()
                for t in data.get("tools", []):
                    tools.append(ToolDef(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters=t.get("parameters", {}),
                    ))
        except Exception as e:
            raise RuntimeError(f"Failed to query tools from gateway: {e}")

        # Done — flip to ready and start the container watchdog so any
        # out-of-band exit gets reconciled into manager state automatically.
        import time as _time
        execution.tools = tools
        execution.setup_phase = "ready"
        execution.setup_status = "ready"
        execution.status = "ready"
        execution.ready_at = _time.time()
        execution.last_activity_at = _time.time()
        execution.watchdog_task = asyncio.create_task(_container_watchdog(execution))
        log(f"Execution {exec_id} ready with {len(tools)} tools")

    except asyncio.CancelledError:
        log(f"Execution {exec_id} setup cancelled at phase={execution.setup_phase}")
        await manager.cleanup_execution(execution, reason="setup_cancelled")
        raise
    except BaseException as e:
        log(f"Execution {exec_id} setup FAILED at phase={execution.setup_phase}: {e!r}")
        await manager.cleanup_execution(execution, reason=f"setup_failed:{type(e).__name__}")


# ── Watchdog ─────────────────────────────────────────────────────────

async def _container_watchdog(execution: ExecutionState) -> None:
    """Block on ``docker wait <container>`` and reconcile state on exit.

    Spawned when an execution flips to ``ready``.  When the container exits
    for any reason (clean exit at sleep 5400, crash, OOM, external rm,
    daemon restart), pop the execution from manager state and release locks.
    """
    runtime = _get_container_runtime()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime, "wait", execution.container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await proc.wait()
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        raise
    except Exception as e:
        log(f"Watchdog for {execution.execution_id} crashed: {e!r}")
        return

    # Container is gone.  Reconcile (cleanup_execution will be a no-op for
    # the manager state if an explicit DELETE already removed us, but the
    # output dir wipe is still useful).
    log(
        f"Watchdog: container {execution.container_name} exited "
        f"(docker wait rc={returncode}); reconciling {execution.execution_id}"
    )
    await manager.cleanup_execution(execution, reason=f"watchdog:container_exit_rc={returncode}")


# ── Teardown / grade ─────────────────────────────────────────────────

async def teardown_container(execution: ExecutionState) -> None:
    """Force-remove the container and wipe the host output dir.

    Idempotent — safe to call multiple times.  This is the only function
    that touches the host filesystem and the docker daemon for cleanup; the
    manager calls it from its converging cleanup path so every terminal
    outcome (grade, DELETE, watchdog, reaper, shutdown, setup failure)
    converges here.
    """
    runtime = _get_container_runtime()
    try:
        _run_cmd([runtime, "rm", "-f", execution.container_name], timeout=15)
    except Exception as e:
        log(f"teardown: docker rm {execution.container_name} failed: {e!r}")
    if execution.output_folder:
        try:
            shutil.rmtree(execution.output_folder, ignore_errors=True)
        except Exception:
            pass
    # Wipe the host-side stash of withheld grader-side dirs.  May not
    # exist if setup failed before Step 4.5.
    if execution.task_stash_dir:
        try:
            shutil.rmtree(execution.task_stash_dir, ignore_errors=True)
        except Exception:
            pass


async def run_eval(execution: ExecutionState) -> GradeResponse:
    """Run ``container_eval.py`` inside the task container and parse results.

    Restores ``preprocess/``, ``evaluation/`` and ``groundtruth_workspace/``
    from the host-side stash (created in ``start_execution`` Step 4.5)
    so the grader has access to artifacts the agent could not see during
    its tool-call phase.  ``preprocess/`` has to come back too — several
    tasks' graders import helper modules from there
    (woocommerce-update-cover/preprocess/woocommerce_client.py is the
    canonical case), and they fail with ``ImportError: No module named
    'woocommerce_client'`` if the directory is still absent at grade time.

    Caller (router) is responsible for calling ``manager.cleanup_execution``
    after grading returns (success or failure) — that's the contract that
    makes ``/grade`` terminal.
    """
    runtime = _get_container_runtime()
    container = execution.container_name
    task_id = execution.task_id
    log(f"Running evaluation for execution {execution.execution_id}")

    # Re-materialise grader-side dirs from the host stash.  Tolerant
    # of missing subdirs — many tasks have no groundtruth_workspace/
    # or preprocess/.
    stash_dir = Path(execution.task_stash_dir) if execution.task_stash_dir else None
    if stash_dir and stash_dir.is_dir():
        target_task_dir = f"/workspace/tasks/finalpool/{task_id}"
        restored = []
        for sub in ("preprocess", "evaluation", "groundtruth_workspace"):
            src = stash_dir / sub
            if not src.is_dir():
                continue
            _run_cmd(
                [runtime, "exec", container, "mkdir", "-p", f"{target_task_dir}/{sub}"],
                timeout=15,
            )
            _run_cmd(
                [runtime, "cp", f"{str(src)}/.", f"{container}:{target_task_dir}/{sub}/"],
                timeout=120,
            )
            restored.append(sub)
        log(f"Restored {'+'.join(restored)} for {execution.execution_id} from {stash_dir}")

    eval_cmd = (
        "uv run python -m scripts.decoupled.container_eval "
        "--bundle_file /workspace/dumps/task_bundle.json"
    )
    exec_env = ["--env", "DOCKER_API_VERSION=1.44"]
    returncode, stdout = await _run_cmd_async(
        [runtime, "exec"] + exec_env + [container, "bash", "-c", eval_cmd],
        timeout=LONG_STEP_TIMEOUT,
    )

    eval_res_path = Path(execution.output_folder) / "eval_res.json"
    if not eval_res_path.exists():
        return GradeResponse(
            status="null",
            score=float("nan"),
            details="Evaluation did not produce results",
            failure=f"eval exit code: {returncode}; stdout tail: {stdout[-2000:]}",
        )

    with open(eval_res_path, "r") as f:
        eval_res = json.load(f)

    pass_value = eval_res.get("pass")
    if pass_value is True:
        status, score = "pass", 1.0
    elif pass_value is False:
        status, score = "fail", 0.0
    else:
        status, score = "null", float("nan")

    return GradeResponse(
        status=status,
        score=score,
        details=eval_res.get("details"),
        failure=eval_res.get("failure"),
    )


# ── Startup reconciliation ───────────────────────────────────────────

def reconcile_orphan_containers() -> int:
    """Reap any v3 per-task containers left over from a previous server run.

    Called at server startup.  v3-only — matches containers named
    ``{prefix}toolathlon-v3-*``.  v1 and v2 containers (different naming
    pattern) are left untouched so v3 can coexist with them on the same host.
    """
    runtime = _get_container_runtime()
    prefix = _get_instance_prefix()
    name_filter = f"{prefix}toolathlon-v3-"
    try:
        result = _run_cmd(
            [runtime, "ps", "-a", "--filter", f"name={name_filter}", "--format", "{{.Names}}"],
            timeout=15,
        )
    except Exception as e:
        log(f"[startup] reconcile skipped: could not list containers ({e})")
        return 0
    if result.returncode != 0:
        log(f"[startup] reconcile skipped: {runtime} ps failed: {result.stderr.strip()}")
        return 0
    names = [n for n in result.stdout.splitlines() if n.strip().startswith(name_filter)]
    if not names:
        log(f"[startup] reconcile: no v3 orphan containers matching '{name_filter}*'")
        return 0
    log(f"[startup] reconcile: removing {len(names)} v3 orphan container(s): {names}")
    removed = 0
    for name in names:
        try:
            _run_cmd([runtime, "rm", "-f", name], timeout=15)
            removed += 1
        except Exception as e:
            log(f"[startup] reconcile: failed to remove {name}: {e}")
    log(f"[startup] reconcile: removed {removed}/{len(names)}")
    return removed
