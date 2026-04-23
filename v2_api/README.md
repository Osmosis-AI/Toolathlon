# Toolathlon v2 API

The v2 Toolathlon API enables you to onboard your own Toolathlon eval by hosting Toolathlon as a **sandbox + tool provider** service, so that you can manage your own model + harness as an agent, rather than only providing an model API endpoint for eval. You can follow the root README to configure accounts stuff, then you are good to go with this v2 API. For your convenience, you can pass the [API-Guide.md](API-Guide.md) to your AI assistent to help onboard.

## Architecture Overview

```
Client (agent loop)                          Toolathlon Server
─────────────────                          ──────────────────
                                           ┌─────────────────┐
  1. Create session ──────────────────────►│  POST /v2/sessions │
                                           └─────────────────┘
  2. Browse tasks ────────────────────────►│  GET  /v2/tasks    │
                                           └─────────────────┘
  3. Start task ──────────────────────────►│  POST .../start    │
     ◄── receive tool schemas              │  (spins up container, │
                                           │   returns tools[])    │
                                           └─────────────────┘
  4. Agent loop:                           ┌─────────────────┐
     model.generate(tools) ──►             │                 │
     extract tool_call     ──►             │                 │
     POST .../call-tool ─────────────────►│  Tool Gateway    │
     ◄── result                            │  (in container)  │
     feed result to model  ──►             │                 │
     ... repeat until done                 └─────────────────┘
                                           ┌─────────────────┐
  5. Grade ───────────────────────────────►│  POST .../grade  │
     ◄── pass/fail/score                   └─────────────────┘

  6. Cleanup ─────────────────────────────►│  DELETE session   │
```

## Quick Start

### 1. Start the server

The v2 API is co-hosted with the v1 server. Start `eval_server.py` as usual:

```bash
python eval_server.py [port] [ws_port] [max_submissions] [max_workers] [max_duration_minutes]
```

`ws_port`, `max_submissions`, `max_workers`, `max_duration_minutes` are ignored by the v2 API.

v2 endpoints are available at `http://<host>:<port>/v2/...`.

### 2. Client workflow (curl examples)

```bash
SERVER="http://localhost:8080"

# Check server health
curl $SERVER/v2/health

# Browse available tasks
curl $SERVER/v2/tasks
curl $SERVER/v2/tasks/ab-testing   # single task details

# Create a session
curl -X POST $SERVER/v2/sessions \
  -H "Content-Type: application/json" \
  -d '{"model_name": "my-model-v1"}'
# → {"session_id": "sess_abc123", "status": "created"}

# Create a debug session (skips deploy_containers.sh on first task start —
# useful for iterating on tasks that don't need Canvas/Poste/WooCommerce/K8s)
curl -X POST $SERVER/v2/sessions \
  -H "Content-Type: application/json" \
  -d '{"model_name": "my-model-v1", "debug": true}'

SID="sess_abc123"

# Start a task (deploys infrastructure on first call, then starts container)
curl -X POST $SERVER/v2/sessions/$SID/tasks/ab-testing/start
# → {"execution_id": "exec_def456", "status": "ready", "tools": [...]}

EID="exec_def456"

# Call a tool (repeat in your agent loop)
curl -X POST $SERVER/v2/sessions/$SID/executions/$EID/call-tool \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "read_file", "arguments": {"path": "/some/file"}}'
# → {"result": "file contents...", "is_error": false}

# Grade the task
curl -X POST $SERVER/v2/sessions/$SID/executions/$EID/grade
# → {"status": "pass", "score": 1.0, "details": "..."}

# Stop the execution (removes container)
curl -X DELETE $SERVER/v2/sessions/$SID/executions/$EID

# Delete the session (stops all remaining containers)
curl -X DELETE $SERVER/v2/sessions/$SID
```

## API Reference

All endpoints are under the `/v2` prefix.

### Health & Tasks (no session required)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v2/health` | Server health and active session info |
| `GET` | `/v2/tasks` | List all available tasks with descriptions and system prompts |
| `GET` | `/v2/tasks/{task_id}` | Get details for a single task |

### Session Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v2/sessions` | Create a new session. Body: `{"model_name": "...", "debug": false}`. Set `debug: true` to skip `deploy_containers.sh` on the first task start (tasks that don't need shared infra will still work). Returns 503 if server is busy (v1 job or another v2 session active). |
| `DELETE` | `/v2/sessions/{session_id}` | Delete session and stop all containers |

### Task Execution

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v2/sessions/{sid}/tasks/{task_id}/start` | Start a task: deploys container, runs preprocess, starts tool gateway, returns available tools |
| `POST` | `/v2/sessions/{sid}/executions/{eid}/call-tool` | Call a tool. Body: `{"tool_name": "...", "arguments": {...}}` |
| `POST` | `/v2/sessions/{sid}/executions/{eid}/grade` | Run evaluation and return pass/fail result |
| `DELETE` | `/v2/sessions/{sid}/executions/{eid}` | Stop a single execution (removes its container) |

## Tool Schema Contract

The tool interaction is a pure pass-through — no schema translation needed on the client side:

1. **`start`** returns `tools[]` where each tool has `{name, description, parameters}`. The `parameters` field is a JSON Schema (same as MCP's `inputSchema`, just renamed).

2. **Client feeds these tools verbatim** to the model as function/tool definitions (OpenAI function calling format).

3. **Model generates** `{name, arguments}` — the client extracts these and sends to `call-tool` as `{"tool_name": name, "arguments": arguments}`.

4. **`call-tool` returns** `{"result": "...", "is_error": false}` — the client wraps this as a tool result message for the model.

5. **Repeat** until the model stops calling tools, then call `grade`.

## Task Conflicts

Some tasks share infrastructure and cannot run simultaneously. Conflict groups are defined in `tasks/finalpool/task_conflict.json`:

```json
{
    "conflict_groups": [
        ["set-conf-cr-ddl", "student-interview"],
        ["huggingface-upload", "dataset-license-issue"],
        ["woocommerce-customer-survey", "woocommerce-product-recall"]
    ]
}
```

Tasks within the same group should not have concurrent active executions. The client is responsible for scheduling around these conflicts.

## Session Protection

- **Mutual exclusion**: Only one workload (v1 job OR v2 session) can run at a time. Both v1 and v2 check each other's state before accepting work.
- **Idle timeout**: If no v2 API request arrives for 60 minutes, the session is automatically reaped — all containers are killed and the server becomes available again.
- **Every request refreshes the timer**: Any API call that references a session resets the 60-minute countdown.

## Infrastructure Deployment

On the first `start` call in a session, the server runs `global_preparation/deploy_containers.sh` to set up shared infrastructure (K8s cluster, Poste email server, WooCommerce, Canvas). This is a one-time cost per session, matching v1's behavior of deploying infrastructure once before running tasks.

To skip this step — e.g. when iterating on tasks that don't touch shared infra — create the session with `"debug": true`. Tool calls that do require those containers will fail while debug mode is active.

Override the container image via the `TOOLATHLON_V2_IMAGE` environment variable (default: `lockon0927/toolathlon-task-image:1016beta`).

## File Structure

```
v2_api/
├── __init__.py         # Package init
├── models.py           # Pydantic request/response models
├── session.py          # Session state, idle timeout reaper, v1/v2 mutual exclusion
├── task_catalog.py     # Reads task metadata from tasks/finalpool/*/
├── container_mgr.py    # Container lifecycle: start, preprocess, gateway, eval, stop
├── tool_proxy.py       # Forwards tool calls to container gateway
├── router.py           # FastAPI router with all /v2 endpoints
└── README.md           # This file
```

### Modified files outside this package

- **`eval_server.py`** — Mounts the v2 router at `/v2`, adds v2 session check to the v1 busy guard and status endpoint.
- **`scripts/decoupled/container_tool_gateway.py`** — Added `GET /tools` and `POST /call-tool` REST endpoints alongside existing MCP/SSE endpoints.

## Output Directory

v2 dumps are written to `dumps_v2/{session_id}/{task_id}/{execution_id}/` and include:
- `task_bundle.json` — preprocess output (task config, MCP server info)
- `preprocess.log` — preprocess stdout
- `gateway.log` — tool gateway stdout
- `eval.log` — evaluation stdout
- `eval_res.json` — evaluation result (`{"pass": true/false, ...}`)
