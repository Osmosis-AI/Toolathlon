"""Session and execution state management for the v2 API.

The server supports exactly one active session at a time.  A session is
created by a client, holds zero or more task *executions* (each backed by a
Docker/Podman container), and is torn down either explicitly by the client
or automatically after 30 minutes of inactivity.

Mutual exclusion with v1:  ``is_server_busy()`` checks both v1's
``current_job`` and v2's ``current_session``, so only one workload type
can run at a time.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .models import ToolDef


def log(msg: str) -> None:
    local_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    utc_time = datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{local_time}][UTC {utc_time}] [v2] {msg}", flush=True)


@dataclass
class ExecutionState:
    """Tracks one running task: its container, gateway endpoint, and tools.

    Setup runs as a background asyncio task (see ``container_mgr.start_execution``);
    ``setup_status``/``setup_phase`` track its progress.  Clients poll the status
    endpoint until ``setup_status == "ready"`` before calling tools or grade.
    On setup failure the entry is auto-removed from ``session.executions``, so a
    subsequent status poll returns 404.
    """
    execution_id: str
    task_id: str
    container_name: str
    gateway_port: int
    gateway_url: str          # e.g. http://127.0.0.1:{port}
    output_folder: str        # host-side dump path
    status: str = "ready"     # ready → graded / stopped
    tools: List[ToolDef] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Async setup state — see _run_setup in container_mgr
    setup_status: str = "starting"          # "starting" or "ready"
    setup_phase: str = "container_start"    # informational sub-state
    setup_started_at: float = field(default_factory=time.time)
    setup_task: Optional[asyncio.Task] = None


@dataclass
class SessionState:
    """One evaluation session — owns multiple executions (one per task).

    Session creation also runs the shared-infrastructure deploy as a background
    task; ``deploy_status`` tracks its progress.  While ``deploy_status ==
    "deploying"``, the session counts as busy (so other clients see 503 on
    POST /v2/sessions), but the create endpoint has already returned 202 to
    its own client — no long-lived HTTP request is held.
    """
    session_id: str
    model_name: str           # informational label from the client
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    executions: Dict[str, ExecutionState] = field(default_factory=dict)
    infra_deployed: bool = False  # set True after deploy_containers.sh runs
    # Async deploy state — see _run_deploy in router
    deploy_status: str = "deploying"        # "deploying" or "ready"
    deploy_started_at: float = field(default_factory=time.time)
    deploy_task: Optional[asyncio.Task] = None


# If no v2 API request arrives for this long, the session is auto-reaped.
IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
# Hard upper bound on session lifetime from creation.  Mirrors the v1 service's
# outer job cap (``eval_server.TIMEOUT_SECONDS``) so a client that keeps the
# session artificially busy with activity refreshes can't hold the server
# forever.  An active client is still expected to finish well inside this.
MAX_SESSION_DURATION_SECONDS = 24 * 60 * 60  # 24 hours
REAPER_CHECK_INTERVAL = 60      # poll every 60 seconds

# Module-level singleton — at most one session at a time.
current_session: Optional[SessionState] = None
_reaper_task: Optional[asyncio.Task] = None


def is_server_busy() -> bool:
    """True if either a v1 job or a v2 session is active."""
    import eval_server
    return eval_server.current_job is not None or current_session is not None


def refresh_activity() -> None:
    """Update last_activity_at to prevent idle reaping."""
    global current_session
    if current_session is not None:
        current_session.last_activity_at = time.time()


def create_session(model_name: str, debug: bool = False) -> SessionState:
    global current_session, _reaper_task

    if is_server_busy():
        raise RuntimeError("Server is busy")

    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    current_session = SessionState(
        session_id=session_id,
        model_name=model_name,
        infra_deployed=debug,
        # debug=True sessions skip deploy entirely; mark them ready up front so
        # the status endpoint reports the correct state immediately.
        deploy_status="ready" if debug else "deploying",
    )

    _reaper_task = asyncio.create_task(_idle_reaper())
    log(f"Session created: {session_id} (model: {model_name}{', debug' if debug else ''})")
    return current_session


def get_session(session_id: str) -> SessionState:
    if current_session is None or current_session.session_id != session_id:
        raise KeyError(f"Session not found: {session_id}")
    return current_session


async def delete_session(session_id: str) -> None:
    """Tear down a session: cancel any in-flight deploy / setup tasks, stop
    the idle reaper, kill all containers.
    """
    global current_session, _reaper_task

    if current_session is None or current_session.session_id != session_id:
        raise KeyError(f"Session not found: {session_id}")

    session = current_session

    # Cancel an in-flight session-level deploy task if any.  Skip self-cancel
    # if ``delete_session`` was called from inside the deploy task itself
    # (auto-clean path on deploy failure), which would otherwise deadlock on
    # ``await deploy_task``.
    deploy_task = session.deploy_task
    if deploy_task is not None and not deploy_task.done():
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if current is not deploy_task:
            deploy_task.cancel()
            try:
                await deploy_task
            except (asyncio.CancelledError, Exception):
                pass

    # Cancel in-flight per-execution setup tasks.  We don't await them here —
    # cleanup_all_executions below will docker-rm their containers, which is
    # what actually frees host resources; the cancelled tasks will unwind on
    # their own shortly and skip their own auto-clean (the entry will already
    # be gone from session.executions).
    for execution in list(session.executions.values()):
        st = execution.setup_task
        if st is not None and not st.done():
            st.cancel()

    if _reaper_task is not None and not _reaper_task.done():
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None

    from .container_mgr import cleanup_all_executions
    await cleanup_all_executions(session)

    log(f"Session deleted: {session_id}")
    current_session = None


async def autoclean_session_after_deploy_failure(session: SessionState) -> None:
    """Called from ``_run_deploy`` itself when its deploy raises.

    Cannot just call ``delete_session`` because that would try to cancel +
    await the very task we're running in.  Performs the same cleanup minus
    the self-cancel.
    """
    global current_session, _reaper_task

    if current_session is not session:
        # Already cleaned up (e.g. concurrent DELETE or reaper).
        return

    if _reaper_task is not None and not _reaper_task.done():
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None

    from .container_mgr import cleanup_all_executions
    await cleanup_all_executions(session)

    log(f"Session {session.session_id} auto-cleaned after deploy failure")
    current_session = None


async def _idle_reaper() -> None:
    """Background task that auto-deletes the session on two conditions:

    1. **Idle timeout** — no v2 API request for ``IDLE_TIMEOUT_SECONDS``
       (30 min).  Every API request calls ``refresh_activity()`` which
       resets this timer; catches abandoned / crashed clients.
    2. **Max session duration** — total wall time since creation exceeds
       ``MAX_SESSION_DURATION_SECONDS`` (24 h).  Activity refreshes cannot
       extend this; catches runaway or stuck-in-loop clients.  Mirrors
       v1's ``eval_server.TIMEOUT_SECONDS`` outer job cap.
    """
    global current_session, _reaper_task
    try:
        while True:
            await asyncio.sleep(REAPER_CHECK_INTERVAL)

            if current_session is None:
                return

            now = time.time()
            idle_seconds = now - current_session.last_activity_at
            age_seconds = now - current_session.created_at

            reap_reason = None
            if age_seconds > MAX_SESSION_DURATION_SECONDS:
                reap_reason = (
                    f"exceeded max session duration "
                    f"({age_seconds / 3600:.1f}h > "
                    f"{MAX_SESSION_DURATION_SECONDS / 3600:.1f}h)"
                )
            elif idle_seconds > IDLE_TIMEOUT_SECONDS:
                reap_reason = f"idle for {idle_seconds / 60:.1f} min"

            if reap_reason is not None:
                session_id = current_session.session_id
                log(f"Session {session_id} {reap_reason}, auto-reaping")

                # Cancel an in-flight deploy task if reaping during "deploying".
                # Don't await it — the bash subprocess will be killed by
                # _run_cmd_async's TimeoutError handling, and the deploy
                # task will unwind soon and short-circuit its own auto-clean
                # because current_session will no longer be itself.
                if current_session.deploy_task is not None and not current_session.deploy_task.done():
                    current_session.deploy_task.cancel()

                # Same for any per-execution setup tasks.
                for execution in current_session.executions.values():
                    if execution.setup_task is not None and not execution.setup_task.done():
                        execution.setup_task.cancel()

                from .container_mgr import cleanup_all_executions
                await cleanup_all_executions(current_session)

                _reaper_task = None
                current_session = None
                log(f"Session {session_id} reaped successfully")
                return

    except asyncio.CancelledError:
        pass
