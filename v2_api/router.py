"""FastAPI router for the v2 REST API.

All endpoints are mounted under ``/v2`` (prefix added in ``eval_server.py``).

Typical client workflow (async resource pattern — each HTTP call is short):
  1. POST   /v2/sessions                                              → create session, returns 202 + status="deploying"
  2. GET    /v2/sessions/{sid}/status                                  → poll until status="ready"
  3. GET    /v2/tasks                                                  → browse available tasks
  4. POST   /v2/sessions/{sid}/tasks/{tid}/start                       → start a task, returns 202 + status="starting"
  5. GET    /v2/sessions/{sid}/executions/{eid}/status                 → poll until status="ready", get tools
  6. POST   /v2/sessions/{sid}/executions/{eid}/call-tool              → call tools in a loop
  7. POST   /v2/sessions/{sid}/executions/{eid}/grade                  → evaluate results
  8. DELETE /v2/sessions/{sid}/executions/{eid}                        → stop the task container
  9. DELETE /v2/sessions/{sid}                                         → tear down session

Failure semantics: when a deploy or per-task setup fails, the server logs
loudly and auto-cleans the resource.  The status endpoint then returns 404
on subsequent polls — the client treats 404 the same as "this resource never
existed" and retries from scratch.

Every request that references a session refreshes its idle timer, preventing
the 30-minute auto-reaper from cleaning it up.
"""

import asyncio
import time
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request

from .container_mgr import _deploy_infrastructure, run_eval, start_execution, stop_execution
from .models import (
    CallToolRequest,
    CallToolResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ExecutionStatusResponse,
    GradeResponse,
    HealthResponse,
    SessionInfo,
    SessionStatusResponse,
    StartTaskResponse,
    TaskInfo,
    TaskListResponse,
)
from . import session as _session
from .session import (
    autoclean_session_after_deploy_failure,
    create_session,
    delete_session,
    get_session,
    refresh_activity,
)
from .task_catalog import get_task_info, load_task_catalog
from .tool_proxy import call_tool

router = APIRouter()


def _require_session(session_id: str):
    """Look up the session or raise 404.

    Does NOT refresh the idle timer — that is now done by
    ``install_v2_middleware`` only on successful 2xx responses, so failing
    requests (404, 409, 503) cannot keep a dead session alive forever by
    repeatedly retrying.
    """
    try:
        return get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")


def _require_execution(session, execution_id: str):
    """Look up an execution within the session or raise 404."""
    execution = session.executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return execution


def _reconcile_dead_execution(session, execution, reason: str) -> None:
    """Lazy-invalidation path: discovered the execution's container is gone.

    Cancels the watchdog (if it hasn't fired yet), wipes the host output dir,
    and pops the entry from ``session.executions`` so subsequent reads return
    a clean 404.  Symmetric with the watchdog's reconciliation, just triggered
    by a failed call_tool / run_eval rather than by docker_wait returning.
    """
    import shutil
    wt = execution.watchdog_task
    if wt is not None and not wt.done():
        wt.cancel()
    if execution.output_folder:
        try:
            shutil.rmtree(execution.output_folder, ignore_errors=True)
        except Exception:
            pass
    if session.executions.pop(execution.execution_id, None) is not None:
        _session.log(
            f"Execution {execution.execution_id} reconciled lazily ({reason})"
        )


def install_v2_middleware(app: FastAPI) -> None:
    """Register the activity-refresh middleware on a FastAPI app.

    The session's idle reaper countdown is reset only when a session-scoped
    endpoint returns a 2xx response.  4xx and 5xx responses (including ones
    against a dead/missing session or a lazily-invalidated execution) do not
    refresh the timer — so a misbehaving client retrying against state that
    no longer exists cannot keep a session artificially alive.  The 30-min
    reaper then claims it on schedule.

    Must be called by every launcher that mounts the v2 router (eval_server.py
    for v1+v2 mode, eval_server_v2.py for v2-only mode).
    """

    @app.middleware("http")
    async def _refresh_session_on_success(request: Request, call_next):
        response = await call_next(request)
        # Only refresh activity for *session-scoped* successful responses.
        # Path filter uses the trailing slash so POST /v2/sessions (create)
        # does not refresh — create_session already stamps last_activity_at.
        # Top-level routes (/v2/health, /v2/tasks, /v2/tasks/{id}) are also
        # excluded, so polling them cannot keep a session alive.
        if (
            200 <= response.status_code < 300
            and request.url.path.startswith("/v2/sessions/")
        ):
            refresh_activity()
        return response


@router.get("/health", response_model=HealthResponse)
async def health_check():
    # ``active`` reflects cross-modal busy state — true if either a v2 session
    # or a v1 job is occupying the server, since both block POST /v2/sessions.
    # The session_id/model_name/started_at fields are populated only when v2
    # specifically is the cause; clients can hit /check_server_status for v1
    # job details.
    import eval_server
    v1_busy = eval_server.current_job is not None
    v2_session = _session.current_session

    if v2_session is not None:
        session_info = SessionInfo(
            active=True,
            session_id=v2_session.session_id,
            model_name=v2_session.model_name,
            started_at=datetime.fromtimestamp(v2_session.created_at).isoformat(),
        )
    else:
        session_info = SessionInfo(active=v1_busy)
    return HealthResponse(status="ok", version="2.0", session=session_info)


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks():
    return TaskListResponse(tasks=load_task_catalog())


@router.get("/tasks/{task_id}", response_model=TaskInfo)
async def get_task(task_id: str):
    info = get_task_info(task_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return info


async def _run_deploy(session) -> None:
    """Background task that runs ``deploy_containers.sh`` for a freshly-created
    session.  On success, flips ``session.deploy_status`` to ``ready``.  On
    failure or cancellation, logs loudly and auto-cleans the session so the
    client's next status poll returns 404 (per the no-failed-state design).
    """
    try:
        await _deploy_infrastructure()
        # Only mutate state if our session is still the active one — could
        # have been DELETE'd or reaped while we were running.
        if _session.current_session is session:
            session.infra_deployed = True
            session.deploy_status = "ready"
            _session.log(f"Session {session.session_id} deploy complete (status=ready)")
    except asyncio.CancelledError:
        _session.log(f"Session {session.session_id} deploy cancelled")
        raise
    except BaseException as e:
        _session.log(f"Session {session.session_id} deploy FAILED: {e!r}")
        await autoclean_session_after_deploy_failure(session)


@router.post("/sessions", response_model=CreateSessionResponse, status_code=202)
async def create_session_endpoint(req: CreateSessionRequest):
    """Create a session and start the infrastructure deploy in the background.

    Returns immediately with ``status="deploying"`` (or ``status="ready"`` for
    debug-mode sessions that skip deploy).  Clients poll
    ``GET /v2/sessions/{sid}/status`` until status flips to ``ready``.  If the
    deploy fails, the session is auto-cleaned and the next status poll returns
    404 — the client retries from scratch.

    The single-session invariant means concurrent ``POST /v2/sessions`` callers
    immediately get 503 while a deploy is in flight, so we never run two
    deploys concurrently on the same v2 host.
    """
    from .session import is_server_busy
    if is_server_busy():
        raise HTTPException(status_code=503, detail="Server is busy with an existing session or v1 job")

    session = create_session(req.model_name, debug=req.debug)
    if not session.infra_deployed:
        # Spawn the deploy as a background task and return immediately.
        session.deploy_task = asyncio.create_task(_run_deploy(session))
    # Else: debug=True path — create_session already set deploy_status="ready".
    return CreateSessionResponse(
        session_id=session.session_id,
        status=session.deploy_status,
    )


@router.get("/sessions/{session_id}/status", response_model=SessionStatusResponse)
async def session_status_endpoint(session_id: str):
    """Poll endpoint for session deploy progress.

    Returns 404 if the session was never created, or was auto-cleaned (deploy
    failure / idle reap) or DELETE'd.  Refreshes the idle timer on each call.
    """
    session = _require_session(session_id)
    return SessionStatusResponse(
        session_id=session.session_id,
        status=session.deploy_status,
        elapsed_s=time.time() - session.deploy_started_at,
    )


@router.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    try:
        await delete_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return {"status": "deleted"}


@router.post(
    "/sessions/{session_id}/tasks/{task_id}/start",
    response_model=StartTaskResponse,
    status_code=202,
)
async def start_task(session_id: str, task_id: str):
    """Start a task execution.

    Returns immediately with ``status="starting"`` and an empty ``tools`` list
    while container/preprocess/gateway boot run in the background.  Clients
    poll ``GET .../executions/{eid}/status`` until status flips to ``ready``,
    at which point ``tools`` is populated.

    Refuses with 409 if the session is still deploying — deploy must complete
    before tasks can start.
    """
    session = _require_session(session_id)
    if session.deploy_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Session not ready, status={session.deploy_status}",
        )
    try:
        execution = start_execution(task_id, session)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return StartTaskResponse(
        execution_id=execution.execution_id,
        status=execution.setup_status,
        tools=execution.tools,
    )


@router.get(
    "/sessions/{session_id}/executions/{execution_id}/status",
    response_model=ExecutionStatusResponse,
)
async def execution_status_endpoint(session_id: str, execution_id: str):
    """Poll endpoint for per-execution setup progress.

    Returns 404 if the execution was never created, or was auto-cleaned
    (setup failure) or DELETE'd.  ``tools`` is populated only when status is
    ``ready``.  Refreshes the session's idle timer.
    """
    session = _require_session(session_id)
    execution = _require_execution(session, execution_id)
    return ExecutionStatusResponse(
        execution_id=execution.execution_id,
        status=execution.setup_status,
        phase=execution.setup_phase,
        elapsed_s=time.time() - execution.setup_started_at,
        tools=execution.tools,
    )


@router.post(
    "/sessions/{session_id}/executions/{execution_id}/call-tool",
    response_model=CallToolResponse,
)
async def call_tool_endpoint(
    session_id: str,
    execution_id: str,
    req: CallToolRequest,
):
    session = _require_session(session_id)
    execution = _require_execution(session, execution_id)
    if execution.setup_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Execution not ready, status={execution.setup_status}",
        )
    try:
        return await call_tool(execution, req.tool_name, req.arguments)
    except httpx.TransportError as e:
        # Container's gateway is unreachable — race window before the
        # watchdog finishes its reconcile, or a gateway process that died
        # while the container is still alive (which the watchdog can't see).
        _reconcile_dead_execution(session, execution, reason=f"call-tool transport error: {e!r}")
        raise HTTPException(status_code=503, detail=f"Gateway unreachable: {e!r}")


@router.post(
    "/sessions/{session_id}/executions/{execution_id}/grade",
    response_model=GradeResponse,
)
async def grade_endpoint(session_id: str, execution_id: str):
    session = _require_session(session_id)
    execution = _require_execution(session, execution_id)
    if execution.setup_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Execution not ready, status={execution.setup_status}",
        )
    try:
        return await run_eval(execution)
    except httpx.TransportError as e:
        _reconcile_dead_execution(session, execution, reason=f"grade transport error: {e!r}")
        raise HTTPException(status_code=503, detail=f"Gateway unreachable: {e!r}")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_id}/executions/{execution_id}")
async def stop_execution_endpoint(session_id: str, execution_id: str):
    session = _require_session(session_id)
    execution = _require_execution(session, execution_id)
    await stop_execution(execution, session)
    return {"status": "stopped"}
