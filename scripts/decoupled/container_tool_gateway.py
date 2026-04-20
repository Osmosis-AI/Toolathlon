import argparse
import asyncio
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiohttp import web
from aiohttp_sse import sse_response

from utils.mcp.tool_servers import MCPServerManager, call_tool_with_retry

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

IGNORED_LOCAL_TOOLS = {"manage_context", "history", "handle_overlong_tool_outputs"}

LOCAL_TOOL_DEFS = {
    "sleep": {
        "name": "local-sleep",
        "description": "use this tool to sleep for a while",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "the number of seconds to sleep",
                },
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    },
    "python_execute": {
        "name": "local-python-execute",
        "description": "Execute Python code directly under the agent workspace, and returns stdout, stderr, return code, and execution time in a structured format.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute (can be directly pasted into a .py file)",
                },
                "filename": {
                    "type": "string",
                    "description": "Filename for the Python file (including .py extension). If not provided, a random UUID will be used.",
                },
                "timeout": {
                    "type": "number",
                    "maximum": 120,
                    "default": 30,
                    "description": "Maximum execution time in seconds. Cannot exceed 120 seconds. Default is 30 seconds.",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    "web_search": {
        "name": "local-web_search",
        "description": "Search the web using Google Serper API with concurrency control and retry mechanisms. Supports various Google search operators.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query with optional Google search operators.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return, default 10, max 50",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


async def execute_local_tool(backend_name: str, arguments: Dict[str, Any], agent_workspace: str) -> str:
    if backend_name == "local-claim_done":
        return "you have claimed the task is done!"

    if backend_name == "local-sleep":
        seconds = arguments.get("seconds", 1)
        time.sleep(seconds)
        return f"has slept {seconds} seconds, wake up!"

    if backend_name == "local-python-execute":
        code = arguments.get("code", "")
        filename = arguments.get("filename", f"{uuid.uuid4()}.py")
        timeout = arguments.get("timeout", 30)
        if timeout > 120:
            timeout = 120
        if not filename.endswith(".py"):
            filename += ".py"

        agent_workspace = os.path.abspath(agent_workspace)
        tmp_dir = os.path.join(agent_workspace, ".python_tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        file_path = os.path.join(tmp_dir, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        start_time = time.time()
        cmd = f"uv run --directory {agent_workspace} ./.python_tmp/{filename}"
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                encoding="utf-8", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            execution_time = time.time() - start_time
            return f"=== EXECUTION TIMEOUT ===\nExecution timed out after {timeout} seconds\nExecution time: {execution_time:.3f} seconds"

        execution_time = time.time() - start_time
        output_parts = []
        if result.stdout:
            output_parts.append("=== STDOUT ===")
            output_parts.append(result.stdout.rstrip())
        if result.stderr:
            output_parts.append("=== STDERR ===")
            output_parts.append(result.stderr.rstrip())
        output_parts.append("=== EXECUTION INFO ===")
        output_parts.append(f"Return code: {result.returncode}")
        output_parts.append(f"Execution time: {execution_time:.3f} seconds")
        output_parts.append(f"Timeout limit: {timeout} seconds")
        if not result.stdout and not result.stderr:
            output_parts.insert(0, "No console output produced.")
        return "\n".join(output_parts)

    if backend_name == "local-web_search":
        query = arguments.get("query", "").strip()
        num_results = min(max(arguments.get("num_results", 10), 1), 50)
        if not query:
            return "Error: Query parameter is required and cannot be empty"
        from utils.aux_tools.web_search import search_google
        results = search_google([query], num_results=num_results)
        if not results:
            return "No search results found."
        formatted = []
        for result in results:
            if "error" in result:
                formatted.append(f"Error: {result['error']}")
            else:
                formatted.append(
                    f"Title: {result.get('title', 'No title')}\n"
                    f"Link: {result.get('link', 'No link')}\n"
                    f"Snippet: {result.get('snippet', 'No description')}\n"
                    f"Sitelinks: {result.get('sitelinks', 'No sitelinks')}\n"
                )
        return "\n".join(formatted)

    return f"Unknown local tool: {backend_name}"


def read_json_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _tool_to_dict(tool: Any) -> Dict[str, Any]:
    if isinstance(tool, dict):
        raw = dict(tool)
    elif hasattr(tool, "model_dump"):
        raw = tool.model_dump(mode="json", exclude_none=True)
    else:
        raw = {}

    name = raw.get("name", getattr(tool, "name", None))
    description = raw.get("description", getattr(tool, "description", "")) or ""
    input_schema = (
        raw.get("inputSchema")
        or raw.get("input_schema")
        or raw.get("parameters")
        or getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {"type": "object", "properties": {}, "additionalProperties": True}
    )
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "additionalProperties": True}
    if "type" not in input_schema:
        input_schema = dict(input_schema)
        input_schema["type"] = "object"
    if input_schema.get("type") == "object" and "properties" not in input_schema:
        input_schema = dict(input_schema)
        input_schema["properties"] = {}

    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid MCP tool name: {name}")

    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
    }


def _content_item_to_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        result = dict(item)
    elif hasattr(item, "model_dump"):
        result = item.model_dump(mode="json", exclude_none=True)
    else:
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        result = {"type": "text", "text": text}
    if "type" not in result:
        result["type"] = "text"
    return result


def _call_result_to_dict(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        out = dict(result)
    elif hasattr(result, "model_dump"):
        out = result.model_dump(mode="json", exclude_none=True)
    else:
        out = {}

    if isinstance(out, dict) and "content" in out and isinstance(out["content"], list):
        out["content"] = [_content_item_to_dict(item) for item in out["content"]]
        if "isError" not in out:
            out["isError"] = bool(out.get("is_error", False))
        return out

    content = getattr(result, "content", None)
    if isinstance(content, list):
        is_error = bool(getattr(result, "isError", getattr(result, "is_error", False)))
        return {
            "content": [_content_item_to_dict(item) for item in content],
            "isError": is_error,
        }

    return {
        "content": [_content_item_to_dict(result)],
        "isError": False,
    }


@dataclass
class ToolRecord:
    exposed_name: str
    backend_type: str  # remote | local
    backend_name: str
    description: str
    input_schema: Dict[str, Any]
    server_name: Optional[str] = None


class ToolRegistry:
    def __init__(self) -> None:
        self._records: Dict[str, ToolRecord] = {}

    def _allocate_name(
        self,
        base_name: str,
        server_name: Optional[str],
        always_prefix: bool = True,
    ) -> str:
        if not always_prefix and base_name not in self._records:
            return base_name

        prefix = server_name or "tool"
        candidate = f"{prefix}-{base_name}"
        suffix = 2
        while candidate in self._records:
            candidate = f"{prefix}-{base_name}-{suffix}"
            suffix += 1
        return candidate

    def add_remote_tools(self, server_name: str, tools: List[Any]) -> None:
        for tool in tools:
            normalized = _tool_to_dict(tool)
            exposed_name = self._allocate_name(normalized["name"], server_name)
            self._records[exposed_name] = ToolRecord(
                exposed_name=exposed_name,
                backend_type="remote",
                backend_name=normalized["name"],
                description=normalized["description"],
                input_schema=normalized["inputSchema"],
                server_name=server_name,
            )

    def add_local_tool(self, tool_def: Dict[str, Any]) -> None:
        backend_name = tool_def["name"]
        name = self._allocate_name(backend_name, "local", always_prefix=False)
        self._records[name] = ToolRecord(
            exposed_name=name,
            backend_type="local",
            backend_name=backend_name,
            description=tool_def.get("description", ""),
            input_schema=tool_def.get("inputSchema", {
                "type": "object", "properties": {},
            }),
            server_name=None,
        )

    def add_claim_done(self) -> None:
        self.add_local_tool({
            "name": "local-claim_done",
            "description": "claim the task is done",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        })

    def list_tools(self) -> List[Dict[str, Any]]:
        tools = []
        for name in sorted(self._records.keys()):
            record = self._records[name]
            tools.append(
                {
                    "name": record.exposed_name,
                    "description": record.description,
                    "inputSchema": record.input_schema,
                }
            )
        return tools

    def get(self, tool_name: str) -> Optional[ToolRecord]:
        return self._records.get(tool_name)

    def __len__(self) -> int:
        return len(self._records)


class GatewayCore:
    def __init__(
        self,
        registry: ToolRegistry,
        remote_caller: Callable[[ToolRecord, Dict[str, Any]], Awaitable[Dict[str, Any]]],
        local_caller: Callable[[ToolRecord, Dict[str, Any]], Awaitable[Dict[str, Any]]],
    ) -> None:
        self.registry = registry
        self.remote_caller = remote_caller
        self.local_caller = local_caller

    @staticmethod
    def _success(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": result,
        }

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    async def handle_json_rpc(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return self._error(None, -32600, "Invalid Request")

        request_id = payload.get("id")
        method = payload.get("method")

        if not isinstance(method, str):
            if request_id is None:
                return None
            return self._error(request_id, -32600, "Invalid Request")

        if method == "notifications/initialized":
            return None

        if method == "initialize":
            if request_id is None:
                return None
            return self._success(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "toolathlon-container-gateway",
                        "version": "0.1.0",
                    },
                },
            )

        if method == "ping":
            if request_id is None:
                return None
            return self._success(request_id, {})

        if method == "tools/list":
            if request_id is None:
                return None
            return self._success(request_id, {"tools": self.registry.list_tools()})

        if method == "tools/call":
            params = payload.get("params", {})
            if not isinstance(params, dict):
                if request_id is None:
                    return None
                return self._error(request_id, -32602, "Invalid params")

            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(tool_name, str) or not tool_name:
                if request_id is None:
                    return None
                return self._error(request_id, -32602, "Tool name is required")
            if not isinstance(arguments, dict):
                if request_id is None:
                    return None
                return self._error(request_id, -32602, "Tool arguments must be an object")

            tool_record = self.registry.get(tool_name)
            if tool_record is None:
                if request_id is None:
                    return None
                return self._error(request_id, -32602, f"Tool not found: {tool_name}")

            try:
                if tool_record.backend_type == "local":
                    result = await self.local_caller(tool_record, arguments)
                else:
                    result = await self.remote_caller(tool_record, arguments)
            except Exception as e:
                if request_id is None:
                    return None
                return self._error(request_id, -32603, f"Tool call failed: {e}")

            if request_id is None:
                return None
            return self._success(request_id, result)

        if request_id is None:
            return None
        return self._error(request_id, -32601, f"Method not found: {method}")


class ContainerToolGateway:
    def __init__(self, bundle_file: str, debug: bool = False) -> None:
        self.bundle_file = bundle_file
        self.debug = debug

        self.bundle: Dict[str, Any] = {}
        self.registry = ToolRegistry()
        self.agent_workspace: str = "."
        self.core = GatewayCore(self.registry, self._remote_call, self._local_call)

        self.mcp_manager: Optional[MCPServerManager] = None

        self._sse_connections: Dict[str, Any] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._request_tasks: set[asyncio.Task] = set()

    async def startup(self, app: web.Application) -> None:
        self.bundle = read_json_file(self.bundle_file)

        needed_servers = self.bundle.get("needed_mcp_servers", []) or []
        mcp_config = self.bundle["eval_config"]["mcp"]
        self.agent_workspace = self.bundle["container_paths"]["agent_workspace"]
        local_token_key_session = self.bundle.get("local_token_key_session")

        self.mcp_manager = MCPServerManager(
            agent_workspace=self.agent_workspace,
            config_dir=mcp_config["server_config_path"],
            debug=self.debug,
            local_token_key_session=local_token_key_session,
        )
        await self.mcp_manager.connect_servers(needed_servers)

        for server_name in self.mcp_manager.get_connected_server_names():
            server = self.mcp_manager.connected_servers[server_name]
            tools = await server.list_tools()
            self.registry.add_remote_tools(server_name, tools)

        self.registry.add_claim_done()

        needed_local_tools = self.bundle.get("needed_local_tools") or []
        for tool_name in needed_local_tools:
            if tool_name in IGNORED_LOCAL_TOOLS:
                continue
            if tool_name == "claim_done":
                continue
            if tool_name in LOCAL_TOOL_DEFS:
                self.registry.add_local_tool(LOCAL_TOOL_DEFS[tool_name])

        if self.debug:
            print(f"[gateway] connected servers: {self.mcp_manager.get_connected_server_names()}")
            print(f"[gateway] exposed tools: {[tool['name'] for tool in self.registry.list_tools()]}")

    async def cleanup(self, app: web.Application) -> None:
        for task in list(self._request_tasks):
            task.cancel()
        if self._request_tasks:
            await asyncio.gather(*self._request_tasks, return_exceptions=True)
        self._request_tasks.clear()

        if self.mcp_manager is not None:
            await self.mcp_manager.ensure_all_disconnected()

    async def _remote_call(self, tool_record: ToolRecord, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if self.mcp_manager is None:
            raise RuntimeError("MCP manager is not initialized")
        if tool_record.server_name not in self.mcp_manager.connected_servers:
            raise RuntimeError(f"MCP server is not connected: {tool_record.server_name}")

        server = self.mcp_manager.connected_servers[tool_record.server_name]
        result = await call_tool_with_retry(
            server=server,
            tool_name=tool_record.backend_name,
            arguments=arguments,
            retry_time=1,
            delay=0.5,
        )
        return _call_result_to_dict(result)

    async def _local_call(self, tool_record: ToolRecord, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result_text = await execute_local_tool(
            tool_record.backend_name, arguments, self.agent_workspace,
        )
        return {
            "content": [{"type": "text", "text": result_text}],
            "isError": False,
        }

    async def handle_health(self, request: web.Request) -> web.Response:
        connected = []
        if self.mcp_manager is not None:
            connected = self.mcp_manager.get_connected_server_names()
        return web.json_response(
            {
                "ok": True,
                "connected_servers": connected,
                "tool_count": len(self.registry),
            }
        )

    async def handle_list_tools_rest(self, request: web.Request) -> web.Response:
        tools = []
        for tool_dict in self.registry.list_tools():
            tools.append({
                "name": tool_dict["name"],
                "description": tool_dict.get("description", ""),
                "parameters": tool_dict["inputSchema"],
            })
        return web.json_response({"tools": tools})

    async def handle_call_tool_rest(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response(
                {"result": f"Invalid JSON: {e}", "is_error": True}, status=400
            )

        tool_name = body.get("tool_name")
        arguments = body.get("arguments", {})

        if not tool_name or not isinstance(tool_name, str):
            return web.json_response(
                {"result": "tool_name is required", "is_error": True}, status=400
            )
        if not isinstance(arguments, dict):
            return web.json_response(
                {"result": "arguments must be an object", "is_error": True}, status=400
            )

        record = self.registry.get(tool_name)
        if record is None:
            return web.json_response(
                {"result": f"Tool not found: {tool_name}", "is_error": True}, status=404
            )

        try:
            if record.backend_type == "local":
                raw_result = await self._local_call(record, arguments)
            else:
                raw_result = await self._remote_call(record, arguments)
            content = raw_result.get("content", [])
            result_text = "\n".join(
                item.get("text", str(item))
                for item in content
                if isinstance(item, dict)
            )
            is_error = raw_result.get("isError", False)
        except Exception as e:
            return web.json_response(
                {"result": f"Tool call failed: {e}", "is_error": True}, status=500
            )

        return web.json_response({"result": result_text, "is_error": is_error})

    async def handle_sse_connection(self, request: web.Request) -> web.StreamResponse:
        session_id = str(uuid.uuid4())
        async with sse_response(request) as resp:
            self._sse_connections[session_id] = resp
            self._session_locks[session_id] = asyncio.Lock()
            await resp.send(f"/messages/?session_id={session_id}", event="endpoint")

            try:
                while True:
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            finally:
                self._sse_connections.pop(session_id, None)
                self._session_locks.pop(session_id, None)

        return resp

    async def _send_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        sse_conn = self._sse_connections.get(session_id)
        session_lock = self._session_locks.get(session_id)
        if sse_conn is None or session_lock is None:
            return

        async with session_lock:
            await sse_conn.send(json.dumps(payload, ensure_ascii=False), event="message")

    async def _process_rpc_request(self, session_id: str, payload: Dict[str, Any]) -> None:
        response = await self.core.handle_json_rpc(payload)
        if response is None:
            return
        await self._send_to_session(session_id, response)

    async def handle_json_rpc(self, request: web.Request) -> web.Response:
        session_id = request.query.get("session_id")
        if not session_id or session_id not in self._sse_connections:
            return web.json_response({"error": "Invalid or missing session_id"}, status=400)

        try:
            payload = await request.json()
        except Exception as e:
            parse_error = {
                "jsonrpc": JSONRPC_VERSION,
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"},
            }
            await self._send_to_session(session_id, parse_error)
            return web.Response(status=202)

        task = asyncio.create_task(self._process_rpc_request(session_id, payload))
        self._request_tasks.add(task)
        task.add_done_callback(self._request_tasks.discard)
        return web.Response(status=202)

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/sse", self.handle_sse_connection)
        app.router.add_post("/messages", self.handle_json_rpc)
        app.router.add_post("/messages/", self.handle_json_rpc)
        app.router.add_get("/tools", self.handle_list_tools_rest)
        app.router.add_post("/call-tool", self.handle_call_tool_rest)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Container-side aggregated MCP SSE gateway")
    parser.add_argument("--bundle_file", default="/workspace/dumps/task_bundle.json")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10086)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gateway = ContainerToolGateway(bundle_file=args.bundle_file, debug=args.debug)
    app = gateway.create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
