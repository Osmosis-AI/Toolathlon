"""Pydantic request/response models for the v3 REST API.

v3 leases work at the *task-execution* level — no client-visible session.
One service endpoint can host up to ``max_active_executions`` concurrent
executions; admission is gated by endpoint-local locks on ``task:{task_id}``
and ``conflict:{group_id}`` and by a shared-infra readiness check.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ── Tool definitions ──────────────────────────────────────────────────

class ToolDef(BaseModel):
    """Single tool definition in OpenAI function-calling format.

    ``parameters`` contains the JSON Schema that describes the tool's input.
    """
    name: str
    description: str
    parameters: Dict[str, Any]


# ── Task catalog ─────────────────────────────────────────────────────

class TaskInfo(BaseModel):
    """Static metadata for a task (no dynamic tool schemas — those live on
    ``ExecutionStatusResponse`` once setup reaches ``ready``)."""
    task_id: str
    description: str
    system_prompt: str
    needed_mcp_servers: List[str]
    needed_local_tools: List[str] = []


class TaskListResponse(BaseModel):
    tasks: List[TaskInfo]


# ── Health ───────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Returned by ``GET /v3/health``.

    Reports endpoint-level capacity and shared-infra readiness so a client
    can decide whether to attempt ``/start`` (though ``/start`` is also the
    authoritative readiness trigger and does not require this preflight).

    ``deploy_status`` values:
      * ``checking``       — fast health probe in progress
      * ``repairing``      — shared-infra deploy/redeploy running
      * ``ready``          — task starts may be admitted
      * ``failed``         — last check/repair failed; ``last_infra_error``
                             explains why
      * ``disabled_debug`` — debug mode skipped shared-infra deploy
    """
    status: str                  # "ok"
    version: str                 # "3.0"
    deploy_status: str
    active_executions: int
    max_active_executions: int
    capacity_available: bool
    busy_locks: Dict[str, str] = {}
    last_infra_error: Optional[str] = None
    retry_after_s: Optional[float] = None
    # Wall-clock (epoch seconds) of last successful deploy_containers.sh.
    # ``None`` until the initial deploy completes.  Operators / dashboards
    # can compute (now - last_full_reset_at) to gauge how close the periodic
    # reset window is.
    last_full_reset_at: Optional[float] = None


# ── Start task ───────────────────────────────────────────────────────

class StartTaskRequest(BaseModel):
    model_name: Optional[str] = None      # informational label
    client_id: Optional[str] = None       # informational worker/job id
    debug: bool = False                    # skip shared-infra deploy preflight
    metadata: Dict[str, Any] = {}          # opaque client metadata


class StartTaskAccepted(BaseModel):
    """Returned with ``202`` from ``POST /v3/tasks/{task_id}/start``."""
    execution_id: str
    task_id: str
    status: str               # "starting"
    phase: str                # "container_start"
    lock_keys: List[str]      # ["task:ab-testing", "conflict:woocommerce", ...]
    task: TaskInfo
    tools: List[ToolDef] = []


# ── Execution status / lifecycle ─────────────────────────────────────

class ExecutionStatusResponse(BaseModel):
    """Returned by ``GET /v3/executions/{eid}/status``.

    ``tools`` is empty until ``status == "ready"``.  Returns 404 if the
    execution was never created, failed setup, was DELETE'd, was reaped,
    or its container exited.
    """
    execution_id: str
    task_id: str
    status: str                       # "starting" | "ready" | "grading" | "stopping"
    phase: str                        # informational setup phase or "ready"
    elapsed_s: float
    remaining_lifetime_s: float
    tools: List[ToolDef] = []


class CallToolRequest(BaseModel):
    tool_name: str
    arguments: Dict[str, Any] = {}


class CallToolResponse(BaseModel):
    result: str
    is_error: bool
    metadata: Dict[str, Any] = {}


class GradeResponse(BaseModel):
    """Terminal response from ``POST /v3/executions/{eid}/grade``.

    Even on infrastructure errors the container is stopped and locks are
    released before the response is returned.
    """
    status: str                       # "pass" | "fail" | "null"
    score: float                      # 1.0 / 0.0 / NaN
    details: Optional[str] = None
    failure: Optional[str] = None


class StopExecutionResponse(BaseModel):
    """Returned by ``DELETE /v3/executions/{eid}``.

    Idempotent: ``status="stopped"`` if the cleanup ran, ``status="not_found"``
    if the execution was already cleaned (so client ``finally`` blocks are
    safe after a terminal grade).
    """
    status: str
