"""Unit-level tests for ``_maybe_periodic_reset``.

Asserts the gating conditions on the periodic-reset trigger.  Uses a
fresh ``ExecutionManager`` instance and direct state manipulation — no
network, no subprocess, no shared infra.  The test deliberately
substitutes ``_run_background_repair`` with a dummy that just sets
``deploy_status = "ready"`` so we can observe whether the trigger fires
without paying for a real deploy.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from v3_api import execution_manager as em  # noqa: E402
from v3_api.execution_manager import ExecutionManager, ExecutionState  # noqa: E402


def _fresh_manager() -> ExecutionManager:
    """Construct a clean ExecutionManager with a no-op repair coroutine."""
    m = ExecutionManager()
    # Patch the repair to a fast no-op so triggers can be observed without
    # actually running deploy_containers.sh.
    async def _fake_repair():
        await asyncio.sleep(0.01)
        m.deploy_status = "ready"
        now = time.time()
        m.last_infra_check_at = now
        m.last_full_reset_at = now
    m._run_background_repair = _fake_repair  # type: ignore[assignment]
    return m


def _fake_execution(execution_id: str = "test-exec") -> ExecutionState:
    now = time.time()
    return ExecutionState(
        execution_id=execution_id,
        task_id="dummy-task",
        container_name="dummy-container",
        gateway_port=0,
        gateway_url="",
        output_folder=Path("/tmp"),
        lock_keys=[],
        created_at=now,
        last_activity_at=now,
    )


async def _check_does_not_fire_when_status_not_ready() -> Tuple[str, bool, str]:
    m = _fresh_manager()
    m.deploy_status = "checking"
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS - 100
    await m._maybe_periodic_reset(time.time())
    if m.deploy_task is not None:
        return "no_fire_status_not_ready", False, "deploy_task was scheduled despite status != ready"
    return "no_fire_status_not_ready", True, "trigger correctly suppressed"


async def _check_does_not_fire_when_deploy_in_flight() -> Tuple[str, bool, str]:
    m = _fresh_manager()
    m.deploy_status = "ready"
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS - 100
    # Fake an in-flight deploy task
    async def _never_done():
        await asyncio.sleep(60)
    inflight = asyncio.create_task(_never_done())
    m.deploy_task = inflight
    try:
        await m._maybe_periodic_reset(time.time())
        # Same handle should still be there
        if m.deploy_task is not inflight:
            return "no_fire_inflight", False, "deploy_task was replaced even though one was in flight"
    finally:
        inflight.cancel()
        try:
            await inflight
        except (asyncio.CancelledError, Exception):
            pass
    return "no_fire_inflight", True, "trigger correctly suppressed"


async def _check_does_not_fire_when_tasks_active() -> Tuple[str, bool, str]:
    m = _fresh_manager()
    m.deploy_status = "ready"
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS - 100
    # Inject a fake active execution
    ex = _fake_execution()
    m.executions[ex.execution_id] = ex
    await m._maybe_periodic_reset(time.time())
    if m.deploy_task is not None:
        return "no_fire_tasks_active", False, "deploy_task scheduled despite 1 active execution"
    return "no_fire_tasks_active", True, "trigger correctly suppressed (1 active task)"


async def _check_does_not_fire_before_interval() -> Tuple[str, bool, str]:
    m = _fresh_manager()
    m.deploy_status = "ready"
    # Just barely too recent
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS + 60
    await m._maybe_periodic_reset(time.time())
    if m.deploy_task is not None:
        return "no_fire_too_recent", False, "deploy_task scheduled before interval elapsed"
    return "no_fire_too_recent", True, "trigger correctly suppressed (too recent)"


async def _check_does_not_fire_when_no_baseline() -> Tuple[str, bool, str]:
    """Without a prior reset timestamp, the trigger must wait for the
    initial deploy to set the baseline."""
    m = _fresh_manager()
    m.deploy_status = "ready"
    m.last_full_reset_at = None
    await m._maybe_periodic_reset(time.time())
    if m.deploy_task is not None:
        return "no_fire_no_baseline", False, "deploy_task scheduled despite last_full_reset_at=None"
    return "no_fire_no_baseline", True, "trigger correctly suppressed (no baseline)"


async def _check_does_fire_when_all_conditions_met() -> Tuple[str, bool, str]:
    m = _fresh_manager()
    m.deploy_status = "ready"
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS - 1
    await m._maybe_periodic_reset(time.time())
    if m.deploy_task is None:
        return "does_fire", False, "deploy_task NOT scheduled when all conditions met"
    # Status should have flipped to "repairing" already
    if m.deploy_status != "repairing":
        return "does_fire", False, f"deploy_status should be 'repairing' but is {m.deploy_status!r}"
    # Wait for fake repair to finish
    try:
        await asyncio.wait_for(m.deploy_task, timeout=5.0)
    except asyncio.TimeoutError:
        return "does_fire", False, "fake repair did not complete in 5s"
    if m.deploy_status != "ready":
        return "does_fire", False, f"after repair, status should be 'ready' but is {m.deploy_status!r}"
    return "does_fire", True, "trigger fired and repair re-ran, status restored to ready"


async def _check_cache_invalidated_on_fire() -> Tuple[str, bool, str]:
    m = _fresh_manager()
    m.deploy_status = "ready"
    m.last_infra_check_at = time.time()  # fresh cache
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS - 1
    await m._maybe_periodic_reset(time.time())
    # Right after the trigger fires and the status flips, but BEFORE the
    # fake repair completes, the cache must be wiped.  Capture immediately:
    if m.last_infra_check_at is not None and m.deploy_status == "repairing":
        return "cache_invalidated", False, "cache not invalidated when reset trigger fired"
    # Let the fake repair finish
    try:
        await asyncio.wait_for(m.deploy_task, timeout=5.0)
    except asyncio.TimeoutError:
        return "cache_invalidated", False, "fake repair did not complete"
    return "cache_invalidated", True, "cache wiped at fire-time, re-stamped after repair"


async def _check_admission_blocked_during_reset() -> Tuple[str, bool, str]:
    """During the brief 'repairing' window after the trigger fires,
    deploy_status != 'ready' so admission would reject."""
    m = _fresh_manager()
    m.deploy_status = "ready"
    m.last_full_reset_at = time.time() - em.PERIODIC_RESET_INTERVAL_SECONDS - 1

    # Slow down the fake repair so we can observe the intermediate state
    async def _slow_repair():
        await asyncio.sleep(0.3)
        m.deploy_status = "ready"
        now = time.time()
        m.last_infra_check_at = now
        m.last_full_reset_at = now
    m._run_background_repair = _slow_repair  # type: ignore[assignment]

    await m._maybe_periodic_reset(time.time())
    # Status flipped synchronously inside _maybe_periodic_reset
    blocked = m.deploy_status != "ready"
    # Let the repair finish
    try:
        await asyncio.wait_for(m.deploy_task, timeout=5.0)
    except asyncio.TimeoutError:
        return "admission_blocked", False, "repair did not complete"
    if not blocked:
        return "admission_blocked", False, "deploy_status stayed 'ready' through the reset"
    return "admission_blocked", True, "deploy_status flipped to 'repairing' synchronously with trigger"


CHECKS = [
    _check_does_not_fire_when_status_not_ready,
    _check_does_not_fire_when_deploy_in_flight,
    _check_does_not_fire_when_tasks_active,
    _check_does_not_fire_before_interval,
    _check_does_not_fire_when_no_baseline,
    _check_does_fire_when_all_conditions_met,
    _check_cache_invalidated_on_fire,
    _check_admission_blocked_during_reset,
]


def main() -> int:
    print("=" * 72)
    print(f"  PERIODIC RESET LOGIC TESTS  ({len(CHECKS)} checks)")
    print("=" * 72)
    fails = 0
    for fn in CHECKS:
        t0 = time.monotonic()
        try:
            name, ok, detail = asyncio.run(fn())
        except Exception as e:
            name, ok, detail = fn.__name__, False, f"unhandled exception: {e!r}"
        dt = time.monotonic() - t0
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name:<28} ({dt:5.2f}s)  {detail}")
        if not ok:
            fails += 1
    print("-" * 72)
    print(f"  {len(CHECKS) - fails}/{len(CHECKS)} passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
