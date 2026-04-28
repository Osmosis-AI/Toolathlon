"""FastAPI router for the v2 REST API.

All endpoints are mounted under ``/v2`` (prefix added in ``eval_server.py``).

Typical client workflow:
  1. POST   /v2/sessions                                   → create session
  2. GET    /v2/tasks                                      → browse available tasks
  3. POST   /v2/sessions/{sid}/tasks/{tid}/start            → start a task, get tools
  4. POST   /v2/sessions/{sid}/executions/{eid}/call-tool   → call tools in a loop
  5. POST   /v2/sessions/{sid}/executions/{eid}/grade       → evaluate results
  6. DELETE /v2/sessions/{sid}/executions/{eid}             → stop the task container
  7. DELETE /v2/sessions/{sid}                              → tear down session

Every request that references a session refreshes its idle timer, preventing
the 30-minute auto-reaper from cleaning it up.
"""

import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

from .container_mgr import run_eval, start_execution, stop_execution
from .models import (
    CallToolRequest,
    CallToolResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    GradeResponse,
    HealthResponse,
    SessionInfo,
    StartTaskResponse,
    TaskInfo,
    TaskListResponse,
)
from . import session as _session
from .session import (
    create_session,
    delete_session,
    get_session,
    refresh_activity,
)
from .task_catalog import get_task_info, load_task_catalog
from .tool_proxy import call_tool

router = APIRouter()


def _require_session(session_id: str):
    """Look up the session or raise 404.  Also refreshes the idle timer."""
    try:
        session = get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    refresh_activity()
    return session


def _require_execution(session, execution_id: str):
    """Look up an execution within the session or raise 404."""
    execution = session.executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return execution


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


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session_endpoint(req: CreateSessionRequest):
    from .session import is_server_busy
    if is_server_busy():
        raise HTTPException(status_code=503, detail="Server is busy with an existing session or v1 job")
    session = create_session(req.model_name, debug=req.debug)
    return CreateSessionResponse(session_id=session.session_id, status="created")


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
)
async def start_task(session_id: str, task_id: str):
    session = _require_session(session_id)
    try:
        execution = await start_execution(task_id, session)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Task start timed out (preprocess or gateway boot exceeded internal limit)",
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return StartTaskResponse(
        execution_id=execution.execution_id,
        status=execution.status,
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
    if execution.status == "stopped":
        raise HTTPException(status_code=400, detail="Execution is stopped")
    return await call_tool(execution, req.tool_name, req.arguments)


@router.post(
    "/sessions/{session_id}/executions/{execution_id}/grade",
    response_model=GradeResponse,
)
async def grade_endpoint(session_id: str, execution_id: str):
    session = _require_session(session_id)
    execution = _require_execution(session, execution_id)
    if execution.status == "stopped":
        raise HTTPException(status_code=400, detail="Execution is stopped")
    try:
        return await run_eval(execution)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_id}/executions/{execution_id}")
async def stop_execution_endpoint(session_id: str, execution_id: str):
    session = _require_session(session_id)
    execution = _require_execution(session, execution_id)
    await stop_execution(execution)
    return {"status": "stopped"}
