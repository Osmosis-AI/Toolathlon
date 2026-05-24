"""FastAPI router for the v3 REST API.

All endpoints are mounted under ``/v3`` (prefix added by the launcher).

Client workflow — every HTTP call is short (~ms in steady state):

  1. POST   /v3/tasks/{task_id}/start            → 202 + execution_id, status="starting"
  2. GET    /v3/executions/{eid}/status          → poll until status="ready" (then tools is populated)
  3. POST   /v3/executions/{eid}/call-tool       → call tools in a loop
  4. POST   /v3/executions/{eid}/grade           → terminal; releases locks + tears down container
  5. DELETE /v3/executions/{eid}                 → idempotent cancellation/cleanup

No sessions, no per-session lock, no admission queueing.  Admission is
gated by:
  * shared-infra readiness (auto-repaired under ``infra_lock`` if needed)
  * endpoint capacity (``max_active_executions``)
  * per-task and per-conflict-group locks

Failures auto-clean — a vanished status / 404 means the execution is gone
and the client should retry from a fresh ``/start``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

from .container_mgr import (
    allocate_execution_resources,
    run_eval,
    spawn_setup,
    teardown_container,
)
from .execution_manager import (
    AdmissionOutcome,
    ExecutionState,
    INFRA_RETRY_AFTER_SECONDS,
    manager,
)
from .models import (
    CallToolRequest,
    CallToolResponse,
    ExecutionStatusResponse,
    GradeResponse,
    HealthResponse,
    StartTaskAccepted,
    StartTaskRequest,
    StopExecutionResponse,
    TaskInfo,
    TaskListResponse,
)
from .task_catalog import get_task_info, load_task_catalog
from .tool_proxy import call_tool

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────

def _require_execution(execution_id: str) -> ExecutionState:
    """Look up an execution or raise 404.  Does NOT refresh activity — that
    is done by the middleware only on successful 2xx responses, so a
    misbehaving client retrying against a dead execution cannot keep stale
    state alive.
    """
    ex = manager.get(execution_id)
    if ex is None:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return ex


def install_v3_middleware(app: FastAPI) -> None:
    """Register the activity-refresh middleware.

    Each successful 2xx response on a ``/v3/executions/{eid}/...`` path
    refreshes that execution's ``last_activity_at``.  4xx / 5xx responses do
    not refresh the timer, so a client repeatedly retrying against a dead
    execution cannot extend its lease.
    """

    @app.middleware("http")
    async def _refresh_activity_on_success(request: Request, call_next):
        response = await call_next(request)
        if not (200 <= response.status_code < 300):
            return response
        path = request.url.path
        prefix = "/v3/executions/"
        if not path.startswith(prefix):
            return response
        rest = path[len(prefix):]
        eid = rest.split("/", 1)[0] if rest else ""
        if eid:
            ex = manager.get(eid)
            if ex is not None:
                manager.touch_activity(ex)
        return response


# ── Top-level: health, tasks ─────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health_endpoint():
    """Return endpoint-level health + capacity + shared-infra deploy status.

    Cheap in the steady state: returns cached deploy status if it is fresh.
    Will attempt a (locked) fast health probe + repair if not — but never
    spawns a parallel deploy if one is already in flight.
    """
    # Best-effort readiness refresh.  trigger_repair=True so a stale infra
    # state can be discovered (and repaired) even by a client that uses
    # ``/health`` as their preflight.  If another caller is in the middle
    # of checking/repairing, this returns False without blocking.
    await manager.ensure_shared_infra_ready(trigger_repair=True)

    retry_after = INFRA_RETRY_AFTER_SECONDS if manager.deploy_status not in (
        "ready", "disabled_debug"
    ) else None

    return HealthResponse(
        status="ok",
        version="3.0",
        deploy_status=manager.deploy_status,
        active_executions=manager.active_count(),
        max_active_executions=manager.max_active_executions,
        capacity_available=manager.capacity_available(),
        busy_locks=manager.busy_locks_snapshot(),
        last_infra_error=manager.last_infra_error,
        retry_after_s=retry_after,
        last_full_reset_at=manager.last_full_reset_at,
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks():
    return TaskListResponse(tasks=load_task_catalog())


@router.get("/tasks/{task_id}", response_model=TaskInfo)
async def get_task(task_id: str):
    info = get_task_info(task_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return info


# ── Start a task ─────────────────────────────────────────────────────

@router.post(
    "/tasks/{task_id}/start",
    response_model=StartTaskAccepted,
    status_code=202,
)
async def start_task_endpoint(task_id: str, req: StartTaskRequest):
    """Atomic admission decision: check shared infra readiness, then check
    capacity + locks under ``manager_lock``, then reserve and return 202.

    Returns:
      * 202 — admitted, setup runs in the background
      * 404 — task_id not in catalog
      * 409 — capacity full, task busy, or conflict group busy on this endpoint
      * 503 — shared infrastructure not ready (checking, repairing, failed,
              or unknown).  Retry after ``retry_after_s`` seconds.
    """
    info = get_task_info(task_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    # Shared-infra readiness gate.  Always called for every /start; uses a
    # short cached-fresh window so the common case is near-zero cost.
    # IMPORTANT: must not run while holding manager_lock.
    if not req.debug:
        ready = await manager.ensure_shared_infra_ready(trigger_repair=True)
        if not ready:
            reason_map = {
                "checking":  "infra_checking",
                "repairing": "infra_repairing",
                "failed":    "infra_failed",
                "unknown":   "infra_disabled_or_unknown",
            }
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": reason_map.get(manager.deploy_status, "infra_disabled_or_unknown"),
                    "deploy_status": manager.deploy_status,
                    "retry_after_s": INFRA_RETRY_AFTER_SECONDS,
                    "last_infra_error": manager.last_infra_error,
                },
            )

    # Allocate host resources up front (port, container name, output dir)
    # so the ExecutionState is born with stable identifiers.
    container_name, gateway_port, gateway_url, output_folder = allocate_execution_resources(task_id)

    admission = await manager.try_admit(
        task_id=task_id,
        container_name=container_name,
        gateway_port=gateway_port,
        gateway_url=gateway_url,
        output_folder=output_folder,
        model_name=req.model_name,
        client_id=req.client_id,
        metadata=dict(req.metadata or {}),
    )

    if admission.outcome != AdmissionOutcome.ADMITTED:
        # Free the output dir we pre-created — we're not running.
        try:
            import shutil
            shutil.rmtree(output_folder, ignore_errors=True)
        except Exception:
            pass

        if admission.outcome in (
            AdmissionOutcome.CAPACITY_FULL,
            AdmissionOutcome.TASK_BUSY,
            AdmissionOutcome.CONFLICT_GROUP_BUSY,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": admission.outcome.value,
                    "busy_keys": admission.busy_keys,
                    "retry_after_s": admission.retry_after_s,
                    "active_executions": manager.active_count(),
                    "max_active_executions": manager.max_active_executions,
                },
            )
        # else: infra outcome — race between ensure_shared_infra_ready and
        # try_admit's re-check.  Surface as 503.
        raise HTTPException(
            status_code=503,
            detail={
                "reason": admission.outcome.value,
                "deploy_status": admission.deploy_status,
                "retry_after_s": admission.retry_after_s,
                "last_infra_error": admission.last_infra_error,
            },
        )

    ex = admission.execution
    assert ex is not None
    spawn_setup(ex)

    return StartTaskAccepted(
        execution_id=ex.execution_id,
        task_id=ex.task_id,
        status=ex.setup_status,
        phase=ex.setup_phase,
        lock_keys=ex.lock_keys,
        task=info,
        tools=ex.tools,
    )


# ── Execution status ─────────────────────────────────────────────────

@router.get(
    "/executions/{execution_id}/status",
    response_model=ExecutionStatusResponse,
)
async def execution_status_endpoint(execution_id: str):
    """Poll for setup progress.  Returns 404 if the execution has been
    cleaned up (setup failed, watchdog reaped, idle/lifetime reaper fired,
    or explicit DELETE).
    """
    ex = _require_execution(execution_id)
    return ExecutionStatusResponse(
        execution_id=ex.execution_id,
        task_id=ex.task_id,
        status=ex.status,
        phase=ex.setup_phase,
        elapsed_s=time.time() - ex.created_at,
        remaining_lifetime_s=ex.remaining_lifetime(),
        tools=ex.tools,
    )


# ── Call tool ────────────────────────────────────────────────────────

@router.post(
    "/executions/{execution_id}/call-tool",
    response_model=CallToolResponse,
)
async def call_tool_endpoint(execution_id: str, req: CallToolRequest):
    """Forward a tool call to the per-task container gateway.

    Returns:
      * 200 — gateway responded (with ``is_error`` reflecting the tool result)
      * 404 — execution not found (already cleaned up)
      * 409 — execution exists but is not yet ``ready`` or is grading/stopping
      * 503 — gateway unreachable; execution is reconciled and removed so a
              future call sees a clean 404 instead of the same error again
    """
    ex = _require_execution(execution_id)
    if ex.setup_status != "ready" or ex.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Execution not ready, status={ex.status} setup_status={ex.setup_status}",
        )
    try:
        return await call_tool(ex, req.tool_name, req.arguments)
    except httpx.TransportError as e:
        # Gateway is unreachable — could be a race between an out-of-band
        # container death and the watchdog's reconcile, or a gateway process
        # that died inside a still-alive container (which the watchdog
        # can't see).  Reconcile now so retries see 404 instead of more 503s.
        for t in (ex.setup_task, ex.watchdog_task):
            if t is not None and not t.done():
                t.cancel()
        await manager.cleanup_execution(ex, reason=f"call_tool_transport_error:{e!r}")
        raise HTTPException(status_code=503, detail=f"Gateway unreachable: {e!r}")


# ── Grade (terminal) ─────────────────────────────────────────────────

@router.post(
    "/executions/{execution_id}/grade",
    response_model=GradeResponse,
)
async def grade_endpoint(execution_id: str):
    """Run grading and terminate the execution.

    Always tears down the container and releases locks before returning,
    even if grading raises.  After this returns, subsequent reads of the
    execution return 404.

    Race handling: the watchdog (``docker wait <container>``) and the
    lifetime reaper can both reconcile an execution between admission and
    grade.  To avoid returning a null/incomplete grade when this happens,
    we cancel the watchdog up front and then re-check that the execution
    is still in manager state.  If it vanished (container's sleep 5400
    fired, or out-of-band ``docker rm`` happened, or the reaper claimed
    it), we surface 404 so the client retries from ``/start`` instead of
    treating the empty trajectory as a model failure.
    """
    ex = _require_execution(execution_id)
    if ex.setup_status != "ready" or ex.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Execution not ready, status={ex.status} setup_status={ex.setup_status}",
        )

    # Cancel the watchdog up front so it can't reconcile state under us
    # while ``run_eval`` is executing inside the container.  Wait for it
    # to actually unwind (it kills its ``docker wait`` subprocess on
    # cancel) so we know it's done before we read manager state.
    wt = ex.watchdog_task
    if wt is not None and not wt.done():
        wt.cancel()
        try:
            await wt
        except (asyncio.CancelledError, Exception):
            pass

    # If the watchdog (or the reaper) had already fired in the window
    # between ``_require_execution`` and now, the execution is no longer
    # in manager state.  Return 404 — clients treat that the same as
    # "execution never existed" and retry the whole element.
    if manager.get(execution_id) is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Execution {execution_id} vanished before grade "
                f"(container exited or was reaped)"
            ),
        )

    ex.status = "grading"
    grade_error: Optional[BaseException] = None
    try:
        result = await run_eval(ex)
    except BaseException as e:
        grade_error = e
        result = GradeResponse(
            status="null",
            score=float("nan"),
            details=None,
            failure=f"grade raised: {e!r}",
        )

    # Setup task should be done long before we got here (we gate on
    # setup_status == "ready"), but cancel defensively so a stray pending
    # task doesn't linger past cleanup.
    st = ex.setup_task
    if st is not None and not st.done():
        st.cancel()

    await manager.cleanup_execution(ex, reason="grade")

    if grade_error is not None and isinstance(grade_error, RuntimeError):
        raise HTTPException(status_code=500, detail=str(grade_error))
    return result


# ── DELETE (idempotent cleanup) ──────────────────────────────────────

@router.delete("/executions/{execution_id}", response_model=StopExecutionResponse)
async def stop_execution_endpoint(execution_id: str):
    """Idempotent cancellation/cleanup.

    Returns ``status="stopped"`` if the execution existed and was cleaned,
    or ``status="not_found"`` if it had already been cleaned (so a client
    ``finally`` block after grade is safe).
    """
    ex = manager.get(execution_id)
    if ex is None:
        return StopExecutionResponse(status="not_found")

    for t in (ex.setup_task, ex.watchdog_task):
        if t is not None and not t.done():
            t.cancel()
    await manager.cleanup_execution(ex, reason="delete")
    return StopExecutionResponse(status="stopped")
