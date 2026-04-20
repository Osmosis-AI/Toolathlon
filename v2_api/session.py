"""Session and execution state management for the v2 API.

The server supports exactly one active session at a time.  A session is
created by a client, holds zero or more task *executions* (each backed by a
Docker/Podman container), and is torn down either explicitly by the client
or automatically after 60 minutes of inactivity.

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
    """Tracks one running task: its container, gateway endpoint, and tools."""
    execution_id: str
    task_id: str
    container_name: str
    gateway_port: int
    gateway_url: str          # e.g. http://127.0.0.1:{port}
    output_folder: str        # host-side dump path
    status: str = "ready"     # ready → graded / stopped
    tools: List[ToolDef] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class SessionState:
    """One evaluation session — owns multiple executions (one per task)."""
    session_id: str
    model_name: str           # informational label from the client
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    executions: Dict[str, ExecutionState] = field(default_factory=dict)
    infra_deployed: bool = False  # set True after deploy_containers.sh runs


# If no v2 API request arrives for this long, the session is auto-reaped.
IDLE_TIMEOUT_SECONDS = 60 * 60  # 60 minutes
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


def create_session(model_name: str) -> SessionState:
    global current_session, _reaper_task

    if is_server_busy():
        raise RuntimeError("Server is busy")

    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    current_session = SessionState(
        session_id=session_id,
        model_name=model_name,
    )

    _reaper_task = asyncio.create_task(_idle_reaper())
    log(f"Session created: {session_id} (model: {model_name})")
    return current_session


def get_session(session_id: str) -> SessionState:
    if current_session is None or current_session.session_id != session_id:
        raise KeyError(f"Session not found: {session_id}")
    return current_session


async def delete_session(session_id: str) -> None:
    """Tear down a session: stop the idle reaper, kill all containers."""
    global current_session, _reaper_task

    if current_session is None or current_session.session_id != session_id:
        raise KeyError(f"Session not found: {session_id}")

    if _reaper_task is not None and not _reaper_task.done():
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None

    from .container_mgr import cleanup_all_executions
    await cleanup_all_executions(current_session)

    log(f"Session deleted: {session_id}")
    current_session = None


async def _idle_reaper() -> None:
    """Background task that auto-deletes the session after 60 min of inactivity.

    Every API request calls ``refresh_activity()`` which resets the timer.
    If no requests arrive for ``IDLE_TIMEOUT_SECONDS``, all containers are
    killed and the session is cleared, freeing the server for new work.
    """
    global current_session, _reaper_task
    try:
        while True:
            await asyncio.sleep(REAPER_CHECK_INTERVAL)

            if current_session is None:
                return

            idle_seconds = time.time() - current_session.last_activity_at
            if idle_seconds > IDLE_TIMEOUT_SECONDS:
                log(
                    f"Session {current_session.session_id} idle for "
                    f"{idle_seconds / 60:.1f} min, auto-reaping"
                )
                session_id = current_session.session_id
                from .container_mgr import cleanup_all_executions
                await cleanup_all_executions(current_session)

                _reaper_task = None
                current_session = None
                log(f"Session {session_id} reaped successfully")
                return

    except asyncio.CancelledError:
        pass
