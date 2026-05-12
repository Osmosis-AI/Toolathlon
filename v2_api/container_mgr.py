"""Container lifecycle manager for v2 task executions.

Translates the decoupled-mode shell workflow (``run_single_decoupled.sh``)
into Python, managing the full lifecycle of per-task containers:

  1. Deploy shared infrastructure (K8s, email, WooCommerce, etc.) — once per session
  2. Start a container for the task
  3. Copy project files and task data into the container
  4. Run preprocess (``container_preprocess.py``) to set up MCP servers
  5. Start the tool gateway (``container_tool_gateway.py``) inside the container
  6. Wait for gateway health and query available tool schemas
  7. Forward tool calls from the client to the gateway (via ``tool_proxy``)
  8. Run evaluation (``container_eval.py``) when the client requests grading
  9. Tear down the container

Supports both Docker and Podman (auto-detected from ``global_configs.py``).
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from typing import List

import httpx

from .models import GradeResponse, ToolDef
from .session import ExecutionState, SessionState, log

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TASKS_DIR = PROJECT_ROOT / "tasks" / "finalpool"
DUMPS_BASE = PROJECT_ROOT / "dumps_v2"

DEFAULT_IMAGE = "lockon0927/toolathlon-task-image:1016beta"
DEFAULT_EVAL_CONFIG = "scripts/formal_run_v0.json"
DEFAULT_MAX_STEP = 100
DEFAULT_MODEL_SHORT_NAME = "v2-sandbox-model"
DEFAULT_PROVIDER = "unified"

GATEWAY_STARTUP_TIMEOUT = 40  # seconds to wait for gateway /health
TOOL_QUERY_TIMEOUT = 10       # seconds to wait for GET /tools
# Per-task step cap for preprocess and eval.  v1 gives each task 40 min total
# (``TIMEOUT=2400`` in ``scripts/run_parallel.sh``) covering preprocess + agent
# loop + eval combined; the 4h ``TIMEOUT_SECONDS`` in eval_server.py is the
# outer batch watchdog for the whole parallel run, not per-task.  In v2 the
# agent loop lives on the client, so 30 min per step is comfortably inside
# v1's per-task envelope and still a real guard against hung subprocesses.
LONG_STEP_TIMEOUT = 30 * 60  # 30 minutes

# Project files copied into every task container (mirrors run_single_decoupled.sh)
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
    return os.environ.get("TOOLATHLON_V2_IMAGE", DEFAULT_IMAGE)


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_cmd(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def _run_cmd_async(cmd: List[str], timeout: int = 300) -> tuple:
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


async def _deploy_infrastructure() -> None:
    """Run deploy_containers.sh to start shared infrastructure (K8s, Poste, WooCommerce, Canvas)."""
    deploy_script = PROJECT_ROOT / "global_preparation" / "deploy_containers.sh"
    if not deploy_script.exists():
        raise RuntimeError(f"Infrastructure deploy script not found: {deploy_script}")

    log("Deploying infrastructure containers (deploy_containers.sh)...")
    returncode, stdout = await _run_cmd_async(
        ["bash", str(deploy_script), "true"],
        timeout=1800,
    )
    if returncode != 0:
        raise RuntimeError(f"Infrastructure deployment failed (exit {returncode}): {stdout[-2000:]}")
    log("Infrastructure containers deployed")


def start_execution(
    task_id: str,
    session: SessionState,
) -> ExecutionState:
    """Allocate an execution slot and kick off setup as a background task.

    Returns immediately with a "starting" ExecutionState; the actual container
    spin / file copy / preprocess / gateway boot runs in ``_run_setup`` as an
    asyncio task.  Clients should poll the status endpoint until
    ``setup_status == "ready"`` before calling tools or grade.

    On setup failure (raised from ``_run_setup``), the entry is auto-removed
    from ``session.executions`` and the container is force-removed; a
    subsequent status poll returns 404.

    Shared infrastructure (``deploy_containers.sh``) is deployed once during
    session creation, not here — see ``router.create_session_endpoint``.
    """
    prefix = _get_instance_prefix()

    task_source = TASKS_DIR / task_id
    if not task_source.is_dir():
        # Synchronous 404 — the client gets a clean error before we burn any
        # resources.  Everything from here on can fail asynchronously.
        raise FileNotFoundError(f"Task not found: {task_id}")

    exec_id = f"exec_{uuid.uuid4().hex[:8]}"
    gateway_port = _allocate_port()
    container_name = f"{prefix}toolathlon-v2-{task_id}-{exec_id[-8:]}"

    output_folder = DUMPS_BASE / session.session_id / task_id / exec_id
    output_folder.mkdir(parents=True, exist_ok=True)
    output_folder_str = str(output_folder.resolve())

    log(f"Starting execution {exec_id} for task {task_id} (port={gateway_port}, container={container_name})")

    execution = ExecutionState(
        execution_id=exec_id,
        task_id=task_id,
        container_name=container_name,
        gateway_port=gateway_port,
        gateway_url=f"http://127.0.0.1:{gateway_port}",
        output_folder=output_folder_str,
        status="starting",
        tools=[],
        setup_status="starting",
        setup_phase="container_start",
    )
    session.executions[exec_id] = execution
    execution.setup_task = asyncio.create_task(_run_setup(execution, session, task_source))
    return execution


async def _run_setup(
    execution: ExecutionState,
    session: SessionState,
    task_source: Path,
) -> None:
    """Background work for ``start_execution``: container spin, file copy,
    preprocess, gateway boot, tool query.

    Updates ``execution.setup_phase`` as it progresses.  On any exception
    (including ``CancelledError``), force-removes the container, wipes the
    output folder, and drops the entry from ``session.executions`` so the
    client sees a clean 404 on its next status poll.
    """
    runtime = _get_container_runtime()
    image = _get_image_name()
    container_name = execution.container_name
    gateway_port = execution.gateway_port
    gateway_url = execution.gateway_url
    output_folder_str = execution.output_folder
    task_id = execution.task_id
    exec_id = execution.execution_id
    task_dir_arg = f"finalpool/{task_id}"

    container_started = False
    try:
        # Step 1: Start container
        execution.setup_phase = "container_start"
        start_cmd = [
            runtime, "run", "-d",
            "--name", container_name,
            "--network", "host",
        ]
        if runtime == "podman":
            for sock_path in ["/run/podman/podman.sock", f"/run/user/{os.getuid()}/podman/podman.sock"]:
                if os.path.exists(sock_path):
                    start_cmd += ["-v", f"{sock_path}:/run/podman/podman.sock"]
                    break
            start_cmd += ["-e", "KIND_EXPERIMENTAL_PROVIDER=podman"]
        elif runtime == "docker":
            if os.path.exists("/var/run/docker.sock"):
                start_cmd += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
        start_cmd += [
            "-v", f"{output_folder_str}:/workspace/dumps",
            "-v", f"{output_folder_str}:/workspace/logs",
            "-w", "/workspace",
            image,
            "sleep", "3600",
        ]
        result = _run_cmd(start_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Container start failed: {result.stderr}")
        container_started = True
        log(f"Container {container_name} started")

        # Step 2: Wait for container to be ready
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

        # Step 5: Start gateway
        execution.setup_phase = "gateway_boot"
        gateway_cmd = (
            f"nohup uv run python -m scripts.decoupled.container_tool_gateway "
            f"--bundle_file /workspace/dumps/task_bundle.json "
            f"--host 0.0.0.0 --port {gateway_port} --debug "
            f"> /dev/null 2>&1 & echo $!"
        )
        result = _run_cmd(
            [runtime, "exec"] + exec_env + [container_name, "bash", "-c", gateway_cmd]
        )
        gateway_pid = result.stdout.strip()
        log(f"Gateway started (PID={gateway_pid}) on port {gateway_port}")

        # Step 6: Wait for gateway health
        for i in range(GATEWAY_STARTUP_TIMEOUT):
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

        # Done — flip to ready and start the container liveness watchdog so
        # any out-of-band container death (crash / OOM / external rm) is
        # reconciled into session.executions automatically.
        execution.tools = tools
        execution.setup_phase = "ready"
        execution.setup_status = "ready"
        execution.status = "ready"
        execution.watchdog_task = asyncio.create_task(
            _container_watchdog(execution, session)
        )
        log(f"Execution {exec_id} ready with {len(tools)} tools")
    except asyncio.CancelledError:
        log(f"Execution {exec_id} setup cancelled at phase={execution.setup_phase}")
        await _autoclean_execution(execution, session, container_started)
        raise
    except BaseException as e:
        # Loud log so post-mortem is possible even though the API hides the
        # failure (client just sees 404 on next status poll).
        log(f"Execution {exec_id} setup FAILED at phase={execution.setup_phase}: {e!r}")
        await _autoclean_execution(execution, session, container_started)


async def _autoclean_execution(
    execution: ExecutionState,
    session: SessionState,
    container_started: bool,
) -> None:
    """Auto-clean a failed/cancelled execution.

    - Force-removes the container (if it was actually started)
    - Wipes the host output folder
    - Drops the entry from session.executions, so the next status poll 404s
    """
    runtime = _get_container_runtime()
    if container_started:
        try:
            _run_cmd([runtime, "rm", "-f", execution.container_name], timeout=15)
            log(f"Cleaned up container {execution.container_name} after failed setup")
        except Exception as cleanup_exc:
            log(f"Warning: leaked container {execution.container_name}: {cleanup_exc}")
    if execution.output_folder:
        try:
            shutil.rmtree(execution.output_folder, ignore_errors=True)
        except Exception:
            pass
    session.executions.pop(execution.execution_id, None)


async def _container_watchdog(
    execution: ExecutionState,
    session: SessionState,
) -> None:
    """Block on ``docker wait <container>`` and reconcile state when it exits.

    Spawned right after an execution flips to ``ready``.  ``docker wait``
    blocks until the named container exits for *any* reason — clean exit,
    crash, OOM kill, manual ``docker rm``, host docker daemon restart — at
    which point we pop the entry from ``session.executions`` so subsequent
    status / call-tool / grade requests return a clean 404 instead of trying
    to reach a dead gateway.

    Cancellation:  the orderly DELETE path (``stop_execution``) and the
    session-wide cleanup paths cancel this task before they run their own
    ``docker rm -f``, so the watchdog never races with the explicit teardown.
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

    # docker wait returned — container is gone.  Reconcile.
    if session.executions.pop(execution.execution_id, None) is not None:
        log(
            f"Execution {execution.execution_id} container exited "
            f"unexpectedly (docker wait rc={returncode}), reconciled"
        )
        if execution.output_folder:
            try:
                shutil.rmtree(execution.output_folder, ignore_errors=True)
            except Exception:
                pass


async def stop_execution(execution: ExecutionState, session: SessionState) -> None:
    """Force-remove the container, drop its host-side output dir, and remove
    the execution entry from ``session.executions``.

    If the execution is still in setup (``setup_status == "starting"``), the
    background setup task is cancelled first; its own auto-clean handler then
    runs to completion before we proceed with the explicit teardown.  The
    container watchdog is also cancelled up front so it doesn't race with us
    on the docker rm.

    The host-side ``execution.output_folder`` (under ``dumps_v2/...``) only
    held transient artefacts (task_bundle.json, the synthesized traj_log,
    eval_res.json once read into the GradeResponse).  Nothing is needed
    after the container is gone, so wipe it to keep ``dumps_v2/`` from
    growing without bound.

    After this returns, subsequent reads of the execution return 404, exactly
    as if the execution had never existed — symmetric with what the watchdog
    does on out-of-band container death.
    """
    runtime = _get_container_runtime()
    container = execution.container_name

    log(f"Stopping execution {execution.execution_id} (container={container})")

    # Cancel the watchdog before docker rm -f so it doesn't observe our own
    # teardown as an "unexpected" exit and double-reconcile.
    watchdog_task = execution.watchdog_task
    if watchdog_task is not None and not watchdog_task.done():
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass

    # Cancel in-flight setup if any.  Skip self-cancel guard isn't needed
    # here — stop_execution is never called from inside its own setup task.
    setup_task = execution.setup_task
    if setup_task is not None and not setup_task.done():
        setup_task.cancel()
        try:
            await setup_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        _run_cmd([runtime, "rm", "-f", container], timeout=15)
    except Exception as e:
        log(f"Warning: failed to remove container {container}: {e}")

    if execution.output_folder:
        try:
            shutil.rmtree(execution.output_folder, ignore_errors=True)
        except Exception as e:
            log(f"Warning: failed to remove output dir {execution.output_folder}: {e}")

    session.executions.pop(execution.execution_id, None)


async def run_eval(execution: ExecutionState) -> GradeResponse:
    """Run ``container_eval.py`` inside the task container and parse results."""
    runtime = _get_container_runtime()
    container = execution.container_name

    log(f"Running evaluation for execution {execution.execution_id}")

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
        # Include the tail of eval stdout so a missing eval_res.json is
        # diagnosable without a persisted log file.
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
        status = "pass"
        score = 1.0
    elif pass_value is False:
        status = "fail"
        score = 0.0
    else:
        status = "null"
        score = float("nan")

    execution.status = "graded"

    return GradeResponse(
        status=status,
        score=score,
        details=eval_res.get("details"),
        failure=eval_res.get("failure"),
    )


async def cleanup_all_executions(session: SessionState) -> None:
    """Stop all containers owned by this session and wipe the session's
    output dir tree (called on delete or idle reap)."""
    runtime = _get_container_runtime()

    for exec_id, execution in list(session.executions.items()):
        if execution.status != "stopped":
            try:
                _run_cmd([runtime, "rm", "-f", execution.container_name], timeout=15)
            except Exception as e:
                log(f"Warning: cleanup failed for {execution.container_name}: {e}")
            execution.status = "stopped"

    # Remove the whole dumps_v2/{session_id}/ subtree in one shot.  Any
    # per-execution dirs that stop_execution already wiped are no-ops; this
    # also catches dirs from executions that never got a clean stop.
    session_dir = DUMPS_BASE / session.session_id
    if session_dir.exists():
        try:
            shutil.rmtree(session_dir, ignore_errors=True)
        except Exception as e:
            log(f"Warning: failed to remove session dump dir {session_dir}: {e}")

    log(f"Cleaned up {len(session.executions)} execution(s)")


def reconcile_orphan_containers() -> int:
    """Reap any v2 per-task containers left over from a previous server run.

    Called at server startup.  Because ``current_session`` is empty at boot,
    any container matching the v2 naming pattern ``{prefix}toolathlon-v2-*``
    is by definition an orphan (from a prior process that crashed or was
    killed before it could tear its session down).  v1 per-task containers
    use ``{prefix}toolathlon-{task}-{timestamp}`` (no ``v2-`` infix) and are
    not matched by this filter — safe to run alongside a live v1 service.

    Shared infrastructure (Canvas / Poste / WooCommerce / Kind clusters)
    is intentionally left alone: it is expensive to redeploy and shared
    across sessions by design.

    Returns the number of containers removed.
    """
    runtime = _get_container_runtime()
    prefix = _get_instance_prefix()
    name_filter = f"{prefix}toolathlon-v2-"

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
        log(f"[startup] reconcile: no v2 orphan containers matching '{name_filter}*'")
        return 0

    log(f"[startup] reconcile: removing {len(names)} v2 orphan container(s): {names}")
    removed = 0
    for name in names:
        try:
            _run_cmd([runtime, "rm", "-f", name], timeout=15)
            removed += 1
        except Exception as e:
            log(f"[startup] reconcile: failed to remove {name}: {e}")
    log(f"[startup] reconcile: removed {removed}/{len(names)}")
    return removed
