"""
Programmatic Tool Calling (PTC) wrapper for Toolathlon.

Adapted from mcpmark's PTCWrapper. The model is given one extra tool,
``programmatic_tool_call``, that runs Python code in a persistent subprocess
sandbox. From inside the sandbox, the code calls the task's underlying MCP
tools through a ``tools`` proxy, e.g.::

    tools["canvas-list_courses"]()
    tools["arxiv-search"](query="LLM agents", max_results=5)

The proxy forwards tool calls back to the parent over a JSON-line protocol on
stdin/stdout; the parent looks the name up in an aggregated index built from
all connected ``MCPServerManager`` servers and dispatches to the right one.

Key differences from the mcpmark reference implementation:
  * Aggregates over MULTIPLE inner MCP servers — Toolathlon tasks expose many.
    Tool index keys are ``f"{server.name}-{tool.name}"`` so they line up with
    the prefixed names the model already sees through
    ``custom_mcp_util.my_to_function_tool``.
  * Ships as a synthetic ``MCPServer`` (``PTCSyntheticServer``) so the existing
    OpenAI-Agents-SDK plumbing picks it up with no further changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agents.mcp import MCPServer
from mcp.types import CallToolResult, TextContent, Tool as MCPTool

logger = logging.getLogger(__name__)


# Persistent worker source. Stays alive across calls; talks JSON-line on
# stdin/stdout. Kept as a string so we can drop it onto disk lazily.
_PERSISTENT_WORKER = r'''
import os, sys, json, traceback, uuid
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

_proto_out = sys.stdout
_proto_in = sys.stdin


def _read_msg():
    line = _proto_in.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def _write_msg(msg):
    _proto_out.write(json.dumps(msg) + "\n")
    _proto_out.flush()


def _rpc_tool_call(tool_name, args, kwargs):
    req_id = uuid.uuid4().hex
    _write_msg({"type": "tool_call", "id": req_id,
                "tool_name": tool_name,
                "args": list(args), "kwargs": kwargs})
    while True:
        msg = _read_msg()
        if msg.get("type") == "tool_result" and msg.get("id") == req_id:
            if msg.get("ok"):
                return msg.get("value")
            return f"[Tool error] {msg.get('error', 'unknown error')}"


class _ToolProxy:
    __slots__ = ("_name",)

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __call__(self, *args, **kwargs):
        return _rpc_tool_call(self._name, args, kwargs)


class ToolCaller:
    def __getitem__(self, key):
        return _ToolProxy(str(key))

    def __getattr__(self, name):
        return _ToolProxy(str(name))


def main():
    init = _read_msg()
    workspace = init.get("workspace") or os.getcwd()
    try:
        os.chdir(workspace)
    except Exception:
        pass

    g = {
        "__name__": "__main__",
        "tools": ToolCaller(),
        "WORKSPACE": workspace,
        "workspace_path": workspace,
    }

    _write_msg({"type": "ready"})

    while True:
        try:
            msg = _read_msg()
        except Exception as exc:
            _write_msg({"type": "done", "stdout": None, "stderr": None,
                        "error": f"Protocol error: {exc}"})
            continue

        if msg.get("type") != "exec":
            continue

        code = msg.get("code", "")
        file_path = msg.get("file_path", "<code>")
        g["__file__"] = file_path

        out_buf, err_buf, tb = StringIO(), StringIO(), None
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                exec(compile(code, file_path, "exec"), g)
        except Exception:
            tb = traceback.format_exc()

        _write_msg({"type": "done",
                    "stdout": out_buf.getvalue() or None,
                    "stderr": err_buf.getvalue() or None,
                    "error": tb})


if __name__ == "__main__":
    main()
'''


_CODE_EXECUTION_DESCRIPTION = (
    "Execute Python code that calls underlying MCP tools via "
    "`tools[\"<server>-<tool>\"](*args, **kwargs)`. The interpreter is "
    "PERSISTENT: variables, imports, and function definitions from earlier "
    "calls remain in scope.\n\n"
    "Use this when you need: loops over many tool calls, conditional "
    "branching, intermediate computation between calls, or aggregation of "
    "results — all in one turn instead of N turns of single tool calls.\n\n"
    "Tool names contain hyphens, so attribute access does NOT work — use "
    "bracket access only:\n"
    "  - correct:   tools[\"canvas-list_courses\"]()\n"
    "  - incorrect: tools.canvas-list_courses()  # SyntaxError\n\n"
    "Use print() for output. Stdout, stderr, and any traceback are returned "
    "as the tool result.\n\n"
    "Example — batch fetch and aggregate:\n"
    "```python\n"
    "totals = []\n"
    "for cid in course_ids:\n"
    "    info = tools[\"canvas-get_course\"](course_id=cid)\n"
    "    totals.append((cid, info[\"total_students\"]))\n"
    "print(sorted(totals, key=lambda x: -x[1])[:5])\n"
    "```\n\n"
    "Example — conditional workflow:\n"
    "```python\n"
    "info = tools[\"canvas-get_course\"](course_id='123')\n"
    "if info['workflow_state'] == 'available':\n"
    "    students = tools[\"canvas-list_students\"](course_id='123')\n"
    "    print(len(students))\n"
    "else:\n"
    "    print('course not available')\n"
    "```"
)


def _stringify_result(result: Any) -> Any:
    """Flatten an MCP CallToolResult to JSON / text for the worker channel."""
    try:
        content = getattr(result, "content", None)
        if content is None and isinstance(result, dict):
            content = result.get("content")

        if content is not None:
            texts: List[str] = []
            for item in content:
                text = getattr(item, "text", None)
                if text is None and isinstance(item, dict):
                    text = item.get("text")
                if text is not None:
                    texts.append(text)
            if texts:
                joined = "\n".join(texts)
                try:
                    return json.loads(joined)
                except (json.JSONDecodeError, TypeError, ValueError):
                    return joined

        if hasattr(result, "model_dump"):
            try:
                return result.model_dump(mode="json")
            except Exception:
                pass
        if isinstance(result, (str, int, float, bool, list, dict)) or result is None:
            return result
        return str(result)
    except Exception:
        return str(result)


def _ptc_text_result(text: str, is_error: bool = True) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=is_error,
    )


def _format_exec_result(msg: Dict[str, Any]) -> CallToolResult:
    parts: List[str] = []
    if msg.get("stdout"):
        parts.append(str(msg["stdout"]))
    if msg.get("stderr"):
        parts.append("STDERR:\n" + str(msg["stderr"]))
    if msg.get("error"):
        parts.append("ERROR:\n" + str(msg["error"]))
    text = "\n".join(parts) if parts else ""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=bool(msg.get("error")),
    )


class PTCWrapper:
    """Aggregates underlying MCP servers behind a single ``programmatic_tool_call`` tool."""

    CODE_EXECUTION_TOOL = "programmatic_tool_call"

    def __init__(
        self,
        servers: List[MCPServer],
        workspace: Optional[str] = None,
        default_code_timeout: int = 60,
    ):
        self._servers: List[MCPServer] = list(servers)
        self._workspace = os.path.abspath(workspace) if workspace else os.getcwd()
        self._default_code_timeout = int(default_code_timeout)

        # exposed_name -> (server, original_tool_name)
        self._tool_index: Dict[str, Tuple[MCPServer, str]] = {}
        # exposed_name -> ordered parameter list (for positional-arg binding)
        self._tool_param_order: Dict[str, List[str]] = {}
        self._index_built = False
        self._index_lock = asyncio.Lock()

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._proc_lock = asyncio.Lock()
        self._tmp_dir: Optional[str] = None
        self._script_path: Optional[str] = None

    async def setup(self) -> None:
        await self._ensure_index()

    async def aclose(self) -> None:
        await self._kill_worker()
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

    def code_execution_tool(self) -> MCPTool:
        return MCPTool(
            name=self.CODE_EXECUTION_TOOL,
            description=_CODE_EXECUTION_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python code. Use tools[\"<server>-<tool>\"]("
                            "*args, **kwargs) to call underlying MCP tools."
                        ),
                    },
                },
                "required": ["code"],
            },
        )

    @property
    def known_tools(self) -> List[str]:
        return sorted(self._tool_index.keys())

    async def _ensure_index(self) -> None:
        if self._index_built:
            return
        async with self._index_lock:
            if self._index_built:
                return
            for server in self._servers:
                server_name = getattr(server, "name", "") or ""
                try:
                    tools = await server.list_tools()
                except Exception as exc:
                    logger.warning(
                        "PTC: failed to list tools from server %r: %s",
                        server_name, exc,
                    )
                    continue
                for tool in tools:
                    tname = getattr(tool, "name", None)
                    if not tname:
                        continue
                    exposed = f"{server_name}-{tname}"
                    self._tool_index[exposed] = (server, tname)
                    schema = getattr(tool, "inputSchema", None) or {}
                    if not isinstance(schema, dict):
                        schema = {}
                    props = schema.get("properties") or {}
                    if not isinstance(props, dict):
                        props = {}
                    self._tool_param_order[exposed] = list(props.keys())
            self._index_built = True
            logger.info(
                "PTC index built: %d tools across %d server(s)",
                len(self._tool_index), len(self._servers),
            )

    async def call_programmatic(self, code: str) -> CallToolResult:
        await self._ensure_index()
        return await self._handle_code_execution({"code": code})

    async def _handle_code_execution(self, arguments: Dict[str, Any]) -> CallToolResult:
        code = arguments.get("code") or ""
        timeout = self._default_code_timeout

        filename = f"ptc_{uuid.uuid4().hex[:8]}.py"
        tmp_dir = self._ensure_tmp_dir()
        file_path = os.path.join(tmp_dir, filename)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as exc:
            return _ptc_text_result(f"[ptc] failed to write code file: {exc}")

        async with self._proc_lock:
            try:
                await self._ensure_worker()
            except Exception as exc:
                return _ptc_text_result(f"[ptc] worker failed to start: {exc}")

            try:
                await self._send({"type": "exec", "code": code, "file_path": file_path})
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                await self._kill_worker()
                return _ptc_text_result(f"[ptc] worker crashed before exec: {exc}")

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._kill_worker()
                    return _ptc_text_result(
                        f"[ptc] execution timed out after {timeout}s"
                    )
                try:
                    msg = await self._readline(remaining)
                except asyncio.TimeoutError:
                    await self._kill_worker()
                    return _ptc_text_result(
                        f"[ptc] execution timed out after {timeout}s"
                    )
                if msg is None:
                    await self._kill_worker()
                    return _ptc_text_result("[ptc] worker exited without output")

                mtype = msg.get("type")
                if mtype == "done":
                    return _format_exec_result(msg)
                if mtype == "tool_call":
                    await self._handle_tool_call(msg)
                    continue
                logger.warning("PTC worker sent unknown message: %s", mtype)

    async def _handle_tool_call(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        tool_name = msg.get("tool_name") or ""
        args = msg.get("args") or []
        kwargs = msg.get("kwargs") or {}

        # The agent sees the prefixed name ("ptc-programmatic_tool_call");
        # accept the bare name too in case a caller strips the prefix.
        prefixed = f"{PTCSyntheticServer.SERVER_NAME}-{self.CODE_EXECUTION_TOOL}"
        if tool_name == self.CODE_EXECUTION_TOOL or tool_name == prefixed:
            await self._send({
                "type": "tool_result", "id": req_id, "ok": False,
                "error": "programmatic_tool_call cannot call itself recursively",
            })
            return

        target = self._tool_index.get(tool_name)
        if target is None:
            sample = ", ".join(self.known_tools[:10]) or "(none)"
            await self._send({
                "type": "tool_result", "id": req_id, "ok": False,
                "error": (
                    f"unknown tool '{tool_name}' — first 10 known: {sample}. "
                    f"Use exact prefixed names, e.g. 'serverName-toolName'."
                ),
            })
            return

        server, original_name = target
        try:
            bound_kwargs = self._bind_positional(tool_name, args, kwargs)
        except Exception as exc:
            await self._send({
                "type": "tool_result", "id": req_id, "ok": False,
                "error": f"argument binding failed: {exc}",
            })
            return

        try:
            raw = await server.call_tool(original_name, bound_kwargs)
            value = _stringify_result(raw)
            reply = {"type": "tool_result", "id": req_id, "ok": True, "value": value}
        except Exception as exc:
            reply = {
                "type": "tool_result", "id": req_id, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        try:
            await self._send(reply)
        except (BrokenPipeError, ConnectionResetError, OSError):
            await self._kill_worker()

    def _bind_positional(
        self, tool_name: str, args: List[Any], kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not args:
            return dict(kwargs)
        order = self._tool_param_order.get(tool_name)
        if order is None:
            raise ValueError(f"unknown tool '{tool_name}' for positional binding")
        out = dict(kwargs)
        idx = 0
        for pname in order:
            if idx >= len(args):
                break
            if pname in out:
                continue
            out[pname] = args[idx]
            idx += 1
        if idx < len(args):
            raise ValueError(
                f"too many positional arguments for '{tool_name}': "
                f"got {len(args)}, schema declares {len(order)} parameter(s)"
            )
        return out

    def _ensure_tmp_dir(self) -> str:
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            return self._tmp_dir
        self._tmp_dir = tempfile.mkdtemp(prefix="toolathlon_ptc_")
        self._script_path = os.path.join(self._tmp_dir, "_ptc_worker.py")
        with open(self._script_path, "w", encoding="utf-8") as f:
            f.write(_PERSISTENT_WORKER)
        return self._tmp_dir

    async def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._proc is not None:
            logger.warning(
                "PTC worker exited (code %s) — restarting (state reset)",
                self._proc.returncode,
            )
            self._proc = None

        self._ensure_tmp_dir()
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, self._script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace,
        )

        init = json.dumps({"workspace": self._workspace}) + "\n"
        self._proc.stdin.write(init.encode())
        await self._proc.stdin.drain()

        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=15)
            ready = json.loads(line) if line else {}
            if ready.get("type") != "ready":
                raise RuntimeError(f"unexpected init response: {ready}")
        except Exception as exc:
            await self._kill_worker()
            raise RuntimeError(f"worker failed to start: {exc}") from exc

        logger.info(
            "PTC worker started (pid %s, cwd=%s)",
            self._proc.pid, self._workspace,
        )

    async def _send(self, msg: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BrokenPipeError("PTC worker is not running")
        data = (json.dumps(msg) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _readline(self, timeout: float) -> Optional[Dict[str, Any]]:
        if self._proc is None or self._proc.stdout is None:
            return None
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        if not line:
            return None
        return json.loads(line)

    async def _kill_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass


class PTCSyntheticServer(MCPServer):
    """Exposes a ``PTCWrapper`` as a single-tool MCP server.

    The Toolathlon agent loop iterates ``MCPServerManager.connected_servers``
    and converts each server's tools through ``my_to_function_tool``, which
    prefixes the tool name with the server name. Plugging this synthetic
    server in there means the model sees a regular tool named
    ``ptc-programmatic_tool_call`` with no other code changes.
    """

    SERVER_NAME = "ptc"

    def __init__(self, wrapper: PTCWrapper):
        self._wrapper = wrapper
        # Mirror the MCPServerStdio API so the agents framework can read it.
        self.cache_tools_list = True

    @property
    def name(self) -> str:
        return self.SERVER_NAME

    async def connect(self):
        # Real servers were already connected before this wrapper was built;
        # the worker is started lazily on the first programmatic_tool_call.
        return None

    async def cleanup(self):
        await self._wrapper.aclose()

    def invalidate_tools_cache(self):
        return None

    async def list_tools(self) -> List[MCPTool]:
        return [self._wrapper.code_execution_tool()]

    async def call_tool(
        self, tool_name: str, arguments: Optional[Dict[str, Any]]
    ) -> CallToolResult:
        if tool_name != PTCWrapper.CODE_EXECUTION_TOOL:
            return _ptc_text_result(
                f"[ptc] unknown tool '{tool_name}' — only "
                f"'{PTCWrapper.CODE_EXECUTION_TOOL}' is exposed."
            )
        return await self._wrapper.call_programmatic(
            (arguments or {}).get("code") or ""
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.cleanup()
        return False
