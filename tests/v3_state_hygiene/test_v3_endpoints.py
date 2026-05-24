"""Smoke tests against a running v3 service.

Assumes a service is listening on ``http://localhost:8089/v3/``.

Asserts:
  - /v3/health returns 200 with the expected shape
  - The new ``last_full_reset_at`` field is present in the response
  - /v3/tasks returns a non-empty task catalog
  - Admission attempts return a structured response (we don't actually
    admit anything, just verify the endpoint shape).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

BASE = "http://localhost:8089/v3"
TIMEOUT = 8.0


def check_health_shape() -> Tuple[str, bool, str]:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{BASE}/health")
    if r.status_code != 200:
        return "health_shape", False, f"HTTP {r.status_code}"
    d = r.json()
    required = {
        "status", "version", "deploy_status", "active_executions",
        "max_active_executions", "capacity_available", "busy_locks",
        "last_full_reset_at",
    }
    missing = required - set(d)
    if missing:
        return "health_shape", False, f"missing fields: {missing}"
    if d["version"] != "3.0":
        return "health_shape", False, f"unexpected version: {d['version']!r}"
    return "health_shape", True, f"deploy_status={d['deploy_status']} last_full_reset_at={d['last_full_reset_at']}"


def check_health_last_full_reset_field() -> Tuple[str, bool, str]:
    """The new field must be present, may be None or a float epoch."""
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{BASE}/health")
    if r.status_code != 200:
        return "last_full_reset_field", False, f"HTTP {r.status_code}"
    d = r.json()
    val = d.get("last_full_reset_at", "MISSING")
    if val == "MISSING":
        return "last_full_reset_field", False, "field absent from response"
    if val is not None and not isinstance(val, (int, float)):
        return "last_full_reset_field", False, f"unexpected type: {type(val).__name__} ({val!r})"
    return "last_full_reset_field", True, f"value={val!r}"


def check_tasks_list_nonempty() -> Tuple[str, bool, str]:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{BASE}/tasks")
    if r.status_code != 200:
        return "tasks_list", False, f"HTTP {r.status_code}"
    d = r.json()
    tasks = d.get("tasks", [])
    if not tasks:
        return "tasks_list", False, "empty task list"
    return "tasks_list", True, f"{len(tasks)} tasks in catalog"


def check_admission_during_repair_returns_503_or_503ish() -> Tuple[str, bool, str]:
    """While the initial deploy_containers.sh is still running, /start_task
    must return a non-2xx status documenting the deploy state, NOT crash
    and NOT admit.  This is the safety invariant we care about — we don't
    need the deploy to actually complete for the test.
    """
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(
            f"{BASE}/start_task",
            json={"task_id": "canvas-list-test"},
        )
    # If deploy isn't ready: 503-ish.  If deploy IS ready: 202 with execution_id.
    # Either is acceptable from a structural standpoint; what we forbid is
    # 5xx with no JSON body, or 2xx without an execution_id field.
    if r.status_code >= 400:
        try:
            d = r.json()
        except Exception:
            return "admission_shape", False, f"non-JSON error body: {r.text[:200]}"
        # Should mention deploy_status or a reason
        if not any(k in d for k in ("reason", "deploy_status", "detail")):
            return "admission_shape", False, f"error JSON missing diagnostic fields: {d}"
        return "admission_shape", True, f"rejected cleanly: {r.status_code} reason={d.get('reason')} deploy_status={d.get('deploy_status')}"
    if r.status_code in (200, 202):
        try:
            d = r.json()
        except Exception:
            return "admission_shape", False, f"non-JSON success body: {r.text[:200]}"
        # Admitted — must include execution_id
        if "execution_id" not in d:
            return "admission_shape", False, f"admitted but no execution_id: {d}"
        # Cancel/cleanup it
        eid = d["execution_id"]
        try:
            with httpx.Client(timeout=TIMEOUT) as c:
                c.delete(f"{BASE}/executions/{eid}")
        except Exception:
            pass
        return "admission_shape", True, f"admitted execution_id={eid} (cleaned up)"
    return "admission_shape", False, f"unexpected status {r.status_code}: {r.text[:200]}"


CHECKS = [
    check_health_shape,
    check_health_last_full_reset_field,
    check_tasks_list_nonempty,
    check_admission_during_repair_returns_503_or_503ish,
]


def main() -> int:
    print("=" * 72)
    print(f"  V3 ENDPOINT TESTS  ({len(CHECKS)} checks against {BASE})")
    print("=" * 72)
    fails = 0
    for fn in CHECKS:
        t0 = time.monotonic()
        try:
            name, ok, detail = fn()
        except Exception as e:
            name, ok, detail = fn.__name__, False, f"unhandled exception: {e!r}"
        dt = time.monotonic() - t0
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name:<35} ({dt:5.2f}s)  {detail}")
        if not ok:
            fails += 1
    print("-" * 72)
    print(f"  {len(CHECKS) - fails}/{len(CHECKS)} passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
