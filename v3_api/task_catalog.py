"""Task catalog — reads static task metadata from disk.

Behavior identical to v2's catalog; duplicated rather than imported so v2
and v3 can evolve independently.  Tool schemas are NOT included here
because they are only known after the container's gateway boots and is
queried at ``/tools`` — see ``ExecutionStatusResponse.tools``.
"""

import json
from pathlib import Path
from typing import List, Optional

from .models import TaskInfo

TASKS_DIR = Path(__file__).parent.parent / "tasks" / "finalpool"

# Harness-internal local-tool names that should NOT appear as callable tools.
_HARNESS_INTERNAL_LOCAL_TOOLS = {"manage_context", "history", "handle_overlong_tool_outputs"}


def _read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _read_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_task_info(task_dir: Path) -> Optional[TaskInfo]:
    config_path = task_dir / "task_config.json"
    if not config_path.exists():
        return None
    try:
        config = _read_json_file(config_path)
    except Exception:
        return None
    return TaskInfo(
        task_id=task_dir.name,
        description=_read_text_file(task_dir / "docs" / "task.md"),
        system_prompt=_read_text_file(task_dir / "docs" / "agent_system_prompt.md"),
        needed_mcp_servers=config.get("needed_mcp_servers", []),
        needed_local_tools=[
            t for t in config.get("needed_local_tools", [])
            if t not in _HARNESS_INTERNAL_LOCAL_TOOLS
        ],
    )


def load_task_catalog() -> List[TaskInfo]:
    if not TASKS_DIR.is_dir():
        return []
    out: List[TaskInfo] = []
    for task_dir in sorted(TASKS_DIR.iterdir()):
        if not task_dir.is_dir():
            continue
        info = _build_task_info(task_dir)
        if info is not None:
            out.append(info)
    return out


def get_task_info(task_id: str) -> Optional[TaskInfo]:
    task_dir = TASKS_DIR / task_id
    if not task_dir.is_dir():
        return None
    return _build_task_info(task_dir)
