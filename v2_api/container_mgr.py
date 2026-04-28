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


# Serializes the first-call infrastructure deploy when many workers race into
# start_execution() concurrently.  Without this, all of them see
# session.infra_deployed == False before the first one finishes the (slow)
# deploy_containers.sh and the script is run N times in parallel.
_infra_deploy_lock = asyncio.Lock()


async def _deploy_infrastructure() -> None:
    """Run deploy_containers.sh to start shared infrastructure (K8s, Poste, WooCommerce, Canvas)."""
    deploy_script = PROJECT_ROOT / "global_preparation" / "deploy_containers.sh"
    if not deploy_script.exists():
        raise RuntimeError(f"Infrastructure deploy script not found: {deploy_script}")

    log("Deploying infrastructure containers (deploy_containers.sh)...")
    returncode, stdout = await _run_cmd_async(
        ["bash", str(deploy_script), "true"],
        timeout=600,
    )
    if returncode != 0:
        raise RuntimeError(f"Infrastructure deployment failed (exit {returncode}): {stdout[-2000:]}")
    log("Infrastructure containers deployed")


async def start_execution(
    task_id: str,
    session: SessionState,
) -> ExecutionState:
    """Spin up a container for ``task_id``, run preprocess, start the tool gateway,
    and return an ExecutionState with the available tools.

    On the first call in a session, also deploys shared infrastructure via
    ``deploy_containers.sh`` (K8s cluster, email server, WooCommerce, etc.).
    """
    if not session.infra_deployed:
        async with _infra_deploy_lock:
            # Re-check inside the lock: a peer may have finished deploying
            # while we were waiting for the lock.
            if not session.infra_deployed:
                await _deploy_infrastructure()
                session.infra_deployed = True

    runtime = _get_container_runtime()
    prefix = _get_instance_prefix()
    image = _get_image_name()

    task_source = TASKS_DIR / task_id
    if not task_source.is_dir():
        raise FileNotFoundError(f"Task not found: {task_id}")

    exec_id = f"exec_{uuid.uuid4().hex[:8]}"
    gateway_port = _allocate_port()
    container_name = f"{prefix}toolathlon-v2-{task_id}-{exec_id[-8:]}"

    output_folder = DUMPS_BASE / session.session_id / task_id / exec_id
    output_folder.mkdir(parents=True, exist_ok=True)
    log_dir = output_folder
    output_folder_str = str(output_folder.resolve())

    task_dir_arg = f"finalpool/{task_id}"

    log(f"Starting execution {exec_id} for task {task_id} (port={gateway_port}, container={container_name})")

    # Step 1: Start container
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

    log(f"Container {container_name} started")

    # v1 parity: run_single_*.sh registers ``trap cleanup EXIT`` so the
    # container is always removed on any exit path.  We emulate that by
    # wrapping all subsequent steps in try/except and force-removing the
    # container on any failure before re-raising.  Without this, a failed
    # preprocess (or gateway boot, or tool query) leaves an orphan container
    # running on the host with no entry in ``session.executions`` — the
    # session-level cleanup can't find it.
    try:
        # Step 2: Wait for container to be ready
        for _ in range(30):
            check = _run_cmd([runtime, "exec", container_name, "echo", "ready"], timeout=10)
            if check.returncode == 0:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"Container {container_name} not ready after 30s")

        # Step 3: Copy project files
        for item in FILES_TO_COPY:
            src = PROJECT_ROOT / item
            if not src.exists():
                continue
            if src.is_dir():
                parent = str(Path(item).parent)
                if parent != ".":
                    _run_cmd([runtime, "exec", container_name, "mkdir", "-p", f"/workspace/{parent}"])
            _run_cmd([runtime, "cp", str(src), f"{container_name}:/workspace/{item}"])

        # Copy task directory
        _run_cmd([runtime, "exec", container_name, "mkdir", "-p", "/workspace/tasks/finalpool"])
        _run_cmd([runtime, "cp", str(task_source), f"{container_name}:/workspace/tasks/finalpool/"])

        # Step 3.5: Copy config files (gmail/calendar MCP auth)
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
            # Surface the tail of preprocess stdout in the error so debugging
            # doesn't depend on a persisted log file.
            raise RuntimeError(f"Preprocess failed (exit {returncode}): {stdout[-2000:]}")

        log(f"Preprocess done for {task_id}")

        # Step 5: Start gateway.  Discard stdout/stderr; container's docker
        # logs still capture early-startup output if something explodes.
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
        gateway_url = f"http://127.0.0.1:{gateway_port}"

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

        log(f"Execution {exec_id} ready with {len(tools)} tools")

        execution = ExecutionState(
            execution_id=exec_id,
            task_id=task_id,
            container_name=container_name,
            gateway_port=gateway_port,
            gateway_url=gateway_url,
            output_folder=output_folder_str,
            status="ready",
            tools=tools,
        )
        session.executions[exec_id] = execution
        return execution
    except BaseException:
        # v1 ``trap cleanup EXIT`` equivalent: ensure the container never
        # outlives a failed start.  Use BaseException so CancelledError /
        # KeyboardInterrupt also trigger cleanup.
        try:
            _run_cmd([runtime, "rm", "-f", container_name], timeout=15)
            log(f"Cleaned up container {container_name} after failed start")
        except Exception as cleanup_exc:
            log(f"Warning: leaked container {container_name} after failed start: {cleanup_exc}")
        raise


async def stop_execution(execution: ExecutionState) -> None:
    """Force-remove the container, drop its host-side output dir, and mark
    the execution stopped.

    The host-side ``execution.output_folder`` (under ``dumps_v2/...``) only
    held transient artefacts (task_bundle.json, the synthesized traj_log,
    eval_res.json once read into the GradeResponse).  Nothing is needed
    after the container is gone, so wipe it to keep ``dumps_v2/`` from
    growing without bound.
    """
    runtime = _get_container_runtime()
    container = execution.container_name

    log(f"Stopping execution {execution.execution_id} (container={container})")

    try:
        _run_cmd([runtime, "rm", "-f", container], timeout=15)
    except Exception as e:
        log(f"Warning: failed to remove container {container}: {e}")

    if execution.output_folder:
        try:
            shutil.rmtree(execution.output_folder, ignore_errors=True)
        except Exception as e:
            log(f"Warning: failed to remove output dir {execution.output_folder}: {e}")

    execution.status = "stopped"


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
