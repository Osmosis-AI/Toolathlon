"""Task catalog — reads static task metadata from disk.

Scans ``tasks/finalpool/*/`` and returns ``TaskInfo`` objects containing
the task prompt, agent system prompt, and list of needed MCP servers.

Tool schemas are NOT included here because they are dynamic: each task
spins up different MCP servers inside a container, and the actual tool
list is only known at runtime.  Use the ``start`` endpoint to get tools.
"""

import json
from pathlib import Path
from typing import List, Optional

from .models import TaskInfo

TASKS_DIR = Path(__file__).parent.parent / "tasks" / "finalpool"

# Names that appear in task_config.json#needed_local_tools but are NOT exposed
# as callable tools by the container gateway — they're harness-internal
# concepts (context management / history / output truncation).  Mirrors
# IGNORED_LOCAL_TOOLS in scripts/decoupled/container_tool_gateway.py so the
# catalog and the actual /tools list returned by `start` agree.
_HARNESS_INTERNAL_LOCAL_TOOLS = {"manage_context", "history", "handle_overlong_tool_outputs"}


def _read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _read_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_task_catalog() -> List[TaskInfo]:
    """Return metadata for all available tasks."""
    tasks = []

    if not TASKS_DIR.is_dir():
        return tasks

    for task_dir in sorted(TASKS_DIR.iterdir()):
        if not task_dir.is_dir():
            continue

        config_path = task_dir / "task_config.json"
        if not config_path.exists():
            continue

        try:
            config = _read_json_file(config_path)
        except Exception:
            continue

        task_id = task_dir.name
        description = _read_text_file(task_dir / "docs" / "task.md")
        system_prompt = _read_text_file(task_dir / "docs" / "agent_system_prompt.md")
        needed_mcp_servers = config.get("needed_mcp_servers", [])
        needed_local_tools = [
            t for t in config.get("needed_local_tools", [])
            if t not in _HARNESS_INTERNAL_LOCAL_TOOLS
        ]

        tasks.append(TaskInfo(
            task_id=task_id,
            description=description,
            system_prompt=system_prompt,
            needed_mcp_servers=needed_mcp_servers,
            needed_local_tools=needed_local_tools,
        ))

    return tasks


def get_task_info(task_id: str) -> Optional[TaskInfo]:
    """Return metadata for a single task, or None if not found."""
    task_dir = TASKS_DIR / task_id
    if not task_dir.is_dir():
        return None

    config_path = task_dir / "task_config.json"
    if not config_path.exists():
        return None

    try:
        config = _read_json_file(config_path)
    except Exception:
        return None

    description = _read_text_file(task_dir / "docs" / "task.md")
    system_prompt = _read_text_file(task_dir / "docs" / "agent_system_prompt.md")
    needed_mcp_servers = config.get("needed_mcp_servers", [])
    needed_local_tools = [
        t for t in config.get("needed_local_tools", [])
        if t not in _HARNESS_INTERNAL_LOCAL_TOOLS
    ]

    return TaskInfo(
        task_id=task_id,
        description=description,
        system_prompt=system_prompt,
        needed_mcp_servers=needed_mcp_servers,
        needed_local_tools=needed_local_tools,
    )
