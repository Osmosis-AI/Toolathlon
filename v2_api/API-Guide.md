# Toolathlon v2 API — Complete Integration Guide

This document is a self-contained reference for building a client that integrates with the Toolathlon v2 sandbox API. It is designed to be readable by both humans and AI assistants implementing eval clients.

## What is the v2 API?

Toolathlon is a benchmark for evaluating AI agents on real-world tool-use tasks (email, databases, Kubernetes, web APIs, etc.). The v2 API exposes Toolathlon as a **sandbox + tool provider**: the server manages task environments (Docker containers with pre-configured tools), and the client drives the agent loop (model inference, tool call extraction, result feeding).

The client's job is:
1. Pick a task
2. Get the task description and available tools from the server
3. Run an agent loop: send the task + tools to a model, extract tool calls, forward them to the server, feed results back to the model, repeat
4. When the agent is done, ask the server to grade the result

## Base URL

All v2 endpoints are prefixed with `/v2`. If the server runs on `http://example.com:8080`, the v2 base URL is `http://example.com:8080/v2`.

## Authentication

None. The v2 API does not require authentication.

## Concurrency Model

- The server supports **one session at a time** (shared with v1 — if a v1 job is running, v2 session creation will fail with 503).
- Within a session, you can run **multiple task executions**, but beware of [task conflicts](#task-conflicts).
- Sessions auto-expire after **20 minutes of inactivity** (no API calls). All containers are killed on expiry.

---

## Complete API Reference

### 1. Health Check

```
GET /v2/health
```

**Response** `200 OK`:
```json
{
  "status": "ok",
  "version": "2.0",
  "session": {
    "active": false,
    "session_id": null,
    "model_name": null,
    "started_at": null
  }
}
```

If a session is active, `session.active` is `true` and the other fields are populated.

Use this to check if the server is available before creating a session.

---

### 2. List Tasks

```
GET /v2/tasks
```

**Response** `200 OK`:
```json
{
  "tasks": [
    {
      "task_id": "ab-testing",
      "description": "You are a data analyst at a tech company...",
      "system_prompt": "You are an AI assistant helping with data analysis...",
      "needed_mcp_servers": ["postgres-mcp", "filesystem-mcp"],
      "needed_local_tools": ["claim_done", "python_execute"]
    },
    ...
  ]
}
```

**Fields**:
- `task_id`: Unique identifier, used in the `start` endpoint URL path.
- `description`: The task prompt to give to the agent model. This tells the model what to accomplish.
- `system_prompt`: The system prompt to use when calling the model.
- `needed_mcp_servers`: Informational — lists which MCP servers the task uses. Not needed for the client integration, but useful for understanding task requirements.
- `needed_local_tools`: Informational — lists in-container local tools the task exposes. Possible values: `claim_done`, `python_execute`, `web_search`, `sleep`. Like `needed_mcp_servers`, the actual tool schemas come back from `start`. (Per-task; varies between tasks.)

**Important**: This endpoint does NOT return tool schemas. Tools are task-specific and only available after calling `start` (which spins up the container and MCP servers).

---

### 3. Get Single Task

```
GET /v2/tasks/{task_id}
```

Same response shape as a single element of the `tasks` array above. Returns `404` if task not found.

---

### 4. Create Session

```
POST /v2/sessions
Content-Type: application/json

{
  "model_name": "gpt-5",
  "debug": false
}
```

- `model_name` (required, string): An informational label for logging. The server does not use this to route model calls — the client handles all model inference.
- `debug` (optional, boolean, default `false`): When `true`, the server skips `deploy_containers.sh` on the first task start. Useful for iterating on tasks that don't need shared infrastructure (Canvas, Poste, WooCommerce, K8s). Tool calls that require those containers will fail while debug mode is active.

**Response** `200 OK`:
```json
{
  "session_id": "sess_a1b2c3d4e5f6",
  "status": "created"
}
```

**Error** `503 Service Unavailable`: Server is busy (another v1 job or v2 session is active). Wait and retry.

**Save `session_id`** — it is required for all subsequent calls.

---

### 5. Start Task

```
POST /v2/sessions/{session_id}/tasks/{task_id}/start
```

No request body.

This is the most time-consuming call. On the first invocation in a session, it also deploys shared infrastructure (K8s, email server, WooCommerce, etc.), which can take 1-3 minutes. Subsequent task starts in the same session skip this step and take ~30-60 seconds.

**What happens server-side**:
1. (First call only) Runs `deploy_containers.sh` to set up shared infrastructure
2. Starts a Docker container for the task
3. Copies task files and configs into the container
4. Runs preprocessing (sets up MCP servers, databases, etc.)
5. Starts the tool gateway inside the container
6. Queries available tools from the gateway
7. Returns the tool list to the client

**Response** `200 OK`:
```json
{
  "execution_id": "exec_a1b2c3d4",
  "status": "ready",
  "tools": [
    {
      "name": "read-query",
      "description": "Execute a read-only SQL query on the PostgreSQL database",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "The SQL query to execute"
          }
        },
        "required": ["query"]
      }
    },
    {
      "name": "write-query",
      "description": "Execute a write SQL query on the PostgreSQL database",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "The SQL query to execute"
          }
        },
        "required": ["query"]
      }
    },
    ...
  ]
}
```

**Save `execution_id`** — it is required for `call-tool`, `grade`, and `stop`.

**Error** `404`: Task not found. **Error** `500`: Container or preprocess failure.

---

### 6. Call Tool

```
POST /v2/sessions/{session_id}/executions/{execution_id}/call-tool
Content-Type: application/json

{
  "tool_name": "read-query",
  "arguments": {
    "query": "SELECT COUNT(*) FROM users"
  }
}
```

- `tool_name` (required, string): Must exactly match a `name` from the `tools` array returned by `start`.
- `arguments` (required, object): The arguments for the tool call. Must conform to the JSON Schema in the tool's `parameters` field.

**Response** `200 OK`:
```json
{
  "result": "count\n-----\n42",
  "is_error": false,
  "metadata": {}
}
```

- `result` (string): The tool's output as plain text.
- `is_error` (boolean): `true` if the tool call failed. The `result` field contains the error message.

**Error** `400`: Execution is stopped. **Error** `404`: Session or execution not found.

**Timeout**: Tool calls have a 300-second (5-minute) timeout. Some tools (K8s operations, large file reads) can take significant time.

---

### 7. Grade

```
POST /v2/sessions/{session_id}/executions/{execution_id}/grade
```

No request body. Runs the evaluation script inside the task container.

**Response** `200 OK`:
```json
{
  "status": "pass",
  "score": 1.0,
  "details": "All assertions passed",
  "failure": null
}
```

- `status`: One of `"pass"`, `"fail"`, or `"null"` (evaluation could not determine a result).
- `score`: `1.0` for pass, `0.0` for fail, `NaN` for null.
- `details` (optional): Human-readable evaluation details.
- `failure` (optional): Reason for failure, if any.

Call this **after** the agent loop finishes (the model has stopped generating tool calls).

---

### 8. Stop Execution

```
DELETE /v2/sessions/{session_id}/executions/{execution_id}
```

Removes the task container. Call this after grading or if you want to abandon a task.

**Response** `200 OK`:
```json
{
  "status": "stopped"
}
```

---

### 9. Delete Session

```
DELETE /v2/sessions/{session_id}
```

Stops all running executions and frees the server for new sessions.

**Response** `200 OK`:
```json
{
  "status": "deleted"
}
```

**Always call this when done**, even if all executions have been individually stopped. This clears the session state and cancels the idle reaper.

---

## Tool Schema Contract (How to Wire Tools to a Model)

The tool integration is a pure pass-through — no schema translation is needed:

### Step 1: Convert tool definitions to model format

The `tools` array from `start` uses a format compatible with OpenAI function calling. Each tool has:
```json
{
  "name": "tool-name",
  "description": "What the tool does",
  "parameters": { /* JSON Schema */ }
}
```

For OpenAI-compatible APIs, wrap each tool as:
```json
{
  "type": "function",
  "function": {
    "name": "tool-name",
    "description": "What the tool does",
    "parameters": { /* JSON Schema, copied verbatim */ }
  }
}
```

### Step 2: Extract tool calls from model output

When the model generates a tool call, it produces something like:
```json
{
  "name": "read-query",
  "arguments": "{\"query\": \"SELECT * FROM users LIMIT 5\"}"
}
```

Note: `arguments` may be a JSON string (OpenAI format) that needs parsing, or already an object (depends on the API).

### Step 3: Forward to call-tool

```json
POST /v2/sessions/{sid}/executions/{eid}/call-tool
{
  "tool_name": "read-query",
  "arguments": {"query": "SELECT * FROM users LIMIT 5"}
}
```

`arguments` must be a JSON **object** (not a string).

### Step 4: Feed result back to model

Take the `result` string from the response and include it as a tool result message in the conversation. For OpenAI format:
```json
{
  "role": "tool",
  "tool_call_id": "<the tool_call_id from the model's response>",
  "content": "<the result string from call-tool>"
}
```

If `is_error` is `true`, you may want to include the error in the content so the model can react to it.

### Step 5: Repeat until done

Continue calling the model with the updated conversation. When the model responds with a regular text message (no tool calls), the agent loop is complete. Call `grade` to evaluate.

---

## Task Conflicts

Some tasks share infrastructure and must not run simultaneously. Conflict groups are defined in `tasks/finalpool/task_conflict.json`:

```json
{
  "conflict_groups": [
    ["set-conf-cr-ddl", "student-interview"],
    ["huggingface-upload", "dataset-license-issue"],
    ["woocommerce-customer-survey", "woocommerce-product-recall"]
  ]
}
```

**Rule**: Do not have two active executions for tasks in the same conflict group. Wait for one to finish (grade + stop) before starting the other.

The server does not currently enforce this — it is the client's responsibility.

---

## Complete Client Pseudocode

```python
import httpx

BASE = "http://server:8080/v2"

# 1. Create session
resp = httpx.post(f"{BASE}/sessions", json={"model_name": "my-model"})
session_id = resp.json()["session_id"]

# 2. Get task list
tasks = httpx.get(f"{BASE}/tasks").json()["tasks"]

# 3. For each task:
for task in tasks:
    task_id = task["task_id"]
    description = task["description"]
    system_prompt = task["system_prompt"]

    # 4. Start the task
    resp = httpx.post(f"{BASE}/sessions/{session_id}/tasks/{task_id}/start")
    start_data = resp.json()
    execution_id = start_data["execution_id"]
    tools = start_data["tools"]

    # 5. Build initial messages
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": description},
    ]

    # Convert tools to OpenAI format
    openai_tools = [
        {"type": "function", "function": t} for t in tools
    ]

    # 6. Agent loop
    while True:
        model_response = call_model(messages, tools=openai_tools)

        if not model_response.tool_calls:
            break  # agent is done

        # Process each tool call
        for tool_call in model_response.tool_calls:
            resp = httpx.post(
                f"{BASE}/sessions/{session_id}/executions/{execution_id}/call-tool",
                json={
                    "tool_name": tool_call.function.name,
                    "arguments": json.loads(tool_call.function.arguments),
                },
            )
            tool_result = resp.json()

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result["result"],
            })

    # 7. Grade
    grade = httpx.post(
        f"{BASE}/sessions/{session_id}/executions/{execution_id}/grade"
    ).json()
    print(f"{task_id}: {grade['status']} (score={grade['score']})")

    # 8. Stop execution
    httpx.delete(f"{BASE}/sessions/{session_id}/executions/{execution_id}")

# 9. Cleanup
httpx.delete(f"{BASE}/sessions/{session_id}")
```

---

## Error Handling Summary

| HTTP Status | Meaning | Action |
|-------------|---------|--------|
| `200` | Success | Process response |
| `400` | Bad request (e.g., calling tool on stopped execution) | Fix request |
| `404` | Resource not found (session, execution, or task) | Check IDs |
| `500` | Server error (container failure, preprocess failure) | Check server logs, retry or skip task |
| `503` | Server busy (another session/job active) | Wait and retry |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TOOLATHLON_V2_IMAGE` | `lockon0927/toolathlon-task-image:1016beta` | Docker image for task containers |

---

## Output Directory

Server-side logs and results are written to `dumps_v2/{session_id}/{task_id}/{execution_id}/`:

| File | Description |
|------|-------------|
| `task_bundle.json` | Preprocess output — task config, MCP server info |
| `preprocess.log` | Preprocess stdout/stderr |
| `gateway.log` | Tool gateway stdout/stderr |
| `eval.log` | Evaluation script stdout/stderr |
| `eval_res.json` | Evaluation result: `{"pass": true/false, "details": "...", "failure": "..."}` |
