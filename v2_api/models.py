"""Pydantic request/response models for the v2 REST API.

All tool schemas use the OpenAI function calling format (JSON Schema in the
``parameters`` field).  The client can feed ``ToolDef`` objects directly to a
model as tool definitions, and map model-generated tool calls straight to
``CallToolRequest`` with no schema translation.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel


# ── Session management ────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    model_name: str  # informational label, not used server-side for routing
    debug: bool = False  # skip deploy_containers.sh on first task start

class CreateSessionResponse(BaseModel):
    session_id: str
    status: str  # "deploying" (deploy still running) or "ready" (debug=True bypass)


class SessionStatusResponse(BaseModel):
    """Returned by ``GET /v2/sessions/{sid}/status``.

    Client-visible states are ``deploying`` and ``ready``.  If the session has
    been auto-cleaned (deploy failed, idle reaper, DELETE), the status endpoint
    returns 404 instead.
    """
    session_id: str
    status: str  # "deploying" or "ready"
    elapsed_s: float


# ── Tool definitions ──────────────────────────────────────────────────

class ToolDef(BaseModel):
    """Single tool definition in OpenAI function-calling format.

    ``parameters`` contains the JSON Schema that describes the tool's input.
    Clients should pass this verbatim as the tool/function schema to the model.
    """
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (== MCP inputSchema, renamed)


# ── Task catalog ─────────────────────────────────────────────────────

class TaskInfo(BaseModel):
    """Static metadata for a task.  Does NOT include tool schemas (those are
    dynamic and only available after ``start``)."""
    task_id: str
    description: str        # task prompt from docs/task.md
    system_prompt: str      # agent system prompt from docs/agent_system_prompt.md
    needed_mcp_servers: List[str]
    needed_local_tools: List[str] = []  # in-container local tools (claim_done, python_execute, ...)

class TaskListResponse(BaseModel):
    tasks: List[TaskInfo]


# ── Task execution ───────────────────────────────────────────────────

class StartTaskResponse(BaseModel):
    """Returned by ``POST /start``.

    The endpoint is async: it returns immediately with ``status="starting"``
    and an empty ``tools`` list while container/preprocess/gateway run in the
    background.  Clients should poll ``GET .../status`` until status flips to
    ``ready``, at which point ``tools`` is populated with the task-specific
    tool schemas.
    """
    execution_id: str
    status: str                              # "starting" or "ready"
    tools: List[ToolDef] = []                # populated only when status == "ready"


class ExecutionStatusResponse(BaseModel):
    """Returned by ``GET /v2/sessions/{sid}/executions/{eid}/status``.

    Client-visible states are ``starting`` and ``ready``.  If setup failed or
    the execution was DELETE'd, the status endpoint returns 404 instead.
    """
    execution_id: str
    status: str                              # "starting" or "ready"
    phase: str                               # informational sub-state during "starting"
    elapsed_s: float
    tools: List[ToolDef] = []                # populated only when status == "ready"

class CallToolRequest(BaseModel):
    tool_name: str                   # must match a name from StartTaskResponse.tools
    arguments: Dict[str, Any] = {}   # model-generated arguments (JSON object)

class CallToolResponse(BaseModel):
    result: str                      # flattened text result from MCP
    is_error: bool
    metadata: Dict[str, Any] = {}


# ── Evaluation ───────────────────────────────────────────────────────

class GradeResponse(BaseModel):
    status: str                    # "pass", "fail", or "null"
    score: float                   # 1.0 / 0.0 / NaN
    details: Optional[str] = None
    failure: Optional[str] = None  # reason for failure, if any


# ── Health check ─────────────────────────────────────────────────────

class SessionInfo(BaseModel):
    """Reported by ``GET /v2/health``.

    ``active`` is true whenever the server is occupied — either by a v2
    session or by a v1 job — since both block ``POST /v2/sessions``.  The
    remaining fields are populated only when a v2 session is the cause; if
    only a v1 job is running they stay ``None``.  Clients that need v1 job
    details should call ``GET /check_server_status``.
    """
    active: bool
    session_id: Optional[str] = None
    model_name: Optional[str] = None
    started_at: Optional[str] = None

class HealthResponse(BaseModel):
    status: str       # "ok"
    version: str      # "2.0"
    session: SessionInfo
