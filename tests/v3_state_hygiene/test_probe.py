"""Behavioral tests for ``global_preparation/probe_shared_infra.py``.

Verifies:
  - Exit code 0 on a healthy shared infra
  - Wall-clock latency within the budget
  - Idempotence: two sequential runs both succeed and don't accumulate state
  - Cleanup: number of pods in ``probe-system`` namespace and number of
    messages in the probe mailbox is identical before and after a run
  - Concurrent-probe safety: two probes running at the same time both
    succeed (the unique-nonce design means they shouldn't race)

Run via ``uv run python -m tests.v3_state_hygiene.test_probe``.
Each ``check_*`` function returns ``(name, ok, detail)`` and the runner
prints a single line per check + a tally at the end.
"""

from __future__ import annotations

import concurrent.futures as futures
import imaplib
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

POSTE_PROBE_USER = "mcpposte_admin@mcp.com"
POSTE_PROBE_PASS = "mcpposte"
KIND_PROBE_NAMESPACE = "probe-system"
PROBE_BUDGET_SECONDS = 10.0


def _run_probe() -> Tuple[int, str, float]:
    t0 = time.monotonic()
    proc = subprocess.run(
        ["uv", "run", "python", "-m", "global_preparation.probe_shared_infra"],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, proc.stderr, time.monotonic() - t0


def check_probe_exit_zero() -> Tuple[str, bool, str]:
    rc, stderr, dt = _run_probe()
    if rc != 0:
        return "exit_zero", False, f"exit={rc}; stderr:\n{stderr}"
    if "✓ canvas" not in stderr or "✓ woo" not in stderr \
       or "✓ poste" not in stderr or "✓ kind" not in stderr:
        return "exit_zero", False, f"missing ✓ markers; stderr:\n{stderr}"
    return "exit_zero", True, f"all four checks ✓ ({dt:.2f}s)"


def check_probe_within_budget() -> Tuple[str, bool, str]:
    rc, stderr, dt = _run_probe()
    if rc != 0:
        return "within_budget", False, f"probe failed before timing test: {stderr[:200]}"
    if dt > PROBE_BUDGET_SECONDS:
        return "within_budget", False, f"took {dt:.2f}s (>{PROBE_BUDGET_SECONDS}s budget)"
    return "within_budget", True, f"{dt:.2f}s ≤ {PROBE_BUDGET_SECONDS}s budget"


def _count_probe_pods(instance_suffix: str = "") -> int:
    cluster = f"cluster{instance_suffix}1-control-plane"
    try:
        r = subprocess.run(
            ["docker", "exec", cluster,
             "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf",
             "get", "pods", "-n", KIND_PROBE_NAMESPACE, "--no-headers"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return -1
    if r.returncode != 0:
        # namespace may not exist yet — that's fine, treat as 0
        return 0
    # Count non-empty lines
    return sum(1 for line in r.stdout.splitlines() if line.strip())


def _count_inbox_messages() -> int:
    try:
        imap = imaplib.IMAP4(host="localhost", port=1143)
    except Exception:
        return -1
    try:
        typ, _ = imap.login(POSTE_PROBE_USER, POSTE_PROBE_PASS)
        if typ != "OK":
            return -1
        typ, data = imap.select("INBOX")
        if typ != "OK":
            return -1
        # message count = total messages in mailbox (EXISTS response)
        return int(data[0])
    except Exception:
        return -1
    finally:
        try: imap.logout()
        except Exception: pass


def check_probe_no_leak() -> Tuple[str, bool, str]:
    """Pod count + inbox count must be identical before and after."""
    pods_before = _count_probe_pods()
    inbox_before = _count_inbox_messages()
    if pods_before < 0 or inbox_before < 0:
        return "no_leak", False, f"could not read baseline (pods={pods_before}, inbox={inbox_before})"
    rc, stderr, _ = _run_probe()
    if rc != 0:
        return "no_leak", False, f"probe failed: {stderr[:200]}"
    # Give the cluster a moment to settle deleted pod cleanup
    time.sleep(1.0)
    pods_after = _count_probe_pods()
    inbox_after = _count_inbox_messages()
    if pods_after != pods_before:
        return "no_leak", False, f"pod count drift: before={pods_before} after={pods_after}"
    if inbox_after != inbox_before:
        return "no_leak", False, f"inbox count drift: before={inbox_before} after={inbox_after}"
    return "no_leak", True, f"pods={pods_before}→{pods_after}, inbox={inbox_before}→{inbox_after}"


def check_probe_idempotent() -> Tuple[str, bool, str]:
    """Two sequential probes both succeed; state stays clean across the pair."""
    pods_before = _count_probe_pods()
    inbox_before = _count_inbox_messages()
    rc1, stderr1, dt1 = _run_probe()
    if rc1 != 0:
        return "idempotent", False, f"first run failed: {stderr1[:200]}"
    rc2, stderr2, dt2 = _run_probe()
    if rc2 != 0:
        return "idempotent", False, f"second run failed: {stderr2[:200]}"
    time.sleep(1.0)
    pods_after = _count_probe_pods()
    inbox_after = _count_inbox_messages()
    if pods_after != pods_before or inbox_after != inbox_before:
        return "idempotent", False, (
            f"state drift across two runs: pods={pods_before}→{pods_after}, "
            f"inbox={inbox_before}→{inbox_after}"
        )
    return "idempotent", True, f"two runs in {dt1:.2f}s + {dt2:.2f}s, state unchanged"


def check_probe_concurrent_safe() -> Tuple[str, bool, str]:
    """Two probes running at the same time must both succeed."""
    pods_before = _count_probe_pods()
    inbox_before = _count_inbox_messages()
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_probe)
        f2 = pool.submit(_run_probe)
        rc1, stderr1, dt1 = f1.result()
        rc2, stderr2, dt2 = f2.result()
    if rc1 != 0 or rc2 != 0:
        return "concurrent_safe", False, (
            f"rc1={rc1} rc2={rc2}; stderr1:\n{stderr1[:300]}\nstderr2:\n{stderr2[:300]}"
        )
    time.sleep(2.0)  # give cleanup a beat after concurrent run
    pods_after = _count_probe_pods()
    inbox_after = _count_inbox_messages()
    if pods_after != pods_before or inbox_after != inbox_before:
        return "concurrent_safe", False, (
            f"state drift after concurrent run: pods={pods_before}→{pods_after}, "
            f"inbox={inbox_before}→{inbox_after}"
        )
    return "concurrent_safe", True, (
        f"both succeeded ({dt1:.2f}s, {dt2:.2f}s); state stayed clean"
    )


CHECKS = [
    check_probe_exit_zero,
    check_probe_within_budget,
    check_probe_no_leak,
    check_probe_idempotent,
    check_probe_concurrent_safe,
]


def main() -> int:
    print("=" * 72)
    print(f"  PROBE BEHAVIORAL TESTS  ({len(CHECKS)} checks)")
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
        print(f"  {marker} {name:<18} ({dt:5.2f}s)  {detail}")
        if not ok:
            fails += 1
    print("-" * 72)
    print(f"  {len(CHECKS) - fails}/{len(CHECKS)} passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
