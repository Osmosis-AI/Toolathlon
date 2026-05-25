"""Drain test that holds an execution alive while drain triggers.

  1. Admit a real task via /v3/start_task — its setup takes ~30-60s
     (preprocess + container start), during which it sits in
     manager.executions.
  2. Wait for the periodic reset interval to elapse.
  3. Watch /v3/health: status must flip ready → draining (and STAY
     there because executions is non-empty).
  4. Attempt another /v3/start_task → must be rejected with HTTP 503
     and reason=infra_draining.
  5. Cancel the first execution (via DELETE) so the dict drains.
  6. Watch: draining → repairing → ready (new last_full_reset_at).
"""

from __future__ import annotations
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8089/v3"
POLL_INTERVAL = 2.0
DEADLINE = 600.0


def get_health() -> dict:
    with urllib.request.urlopen(f"{BASE}/health", timeout=5.0) as r:
        return json.loads(r.read())


def post_start_task(task_id: str = "apply-phd-email") -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}/tasks/{task_id}/start",
        data=json.dumps({"client_id": "drain-test"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300]}


def delete_execution(execution_id: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{BASE}/executions/{execution_id}", method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:200]


def main() -> int:
    print("=" * 72)
    print("DRAIN INTEGRATION TEST 2 — holding an execution to make drain stick")
    print("=" * 72)

    t_start = time.monotonic()
    # Step 1: capture current state
    h0 = get_health()
    if h0["deploy_status"] != "ready":
        print(f"  pre-condition fail: deploy_status={h0['deploy_status']}; need 'ready'")
        return 1
    first_reset_at = h0["last_full_reset_at"]
    print(f"  [{0.0:6.1f}s] starting state: ready, last_full_reset_at={first_reset_at}")

    # Step 2: admit a task to occupy executions[]
    code, body = post_start_task()
    print(f"  [{time.monotonic()-t_start:6.1f}s] /start_task → {code}: {json.dumps(body)[:200]}")
    if code not in (200, 202):
        print(f"  ✗ couldn't admit task to hold execution alive")
        return 1
    execution_id = body.get("execution_id")
    if not execution_id:
        print(f"  ✗ no execution_id in response")
        return 1
    print(f"  [{time.monotonic()-t_start:6.1f}s] admitted execution_id={execution_id}; holds active=1")

    # Step 3: poll until drain
    saw_drain_with_active_task = False
    admission_during_drain_outcome = None
    drain_entered_at = None
    last_status = "ready"
    second_attempt_done = False

    while time.monotonic() - t_start < DEADLINE:
        elapsed = time.monotonic() - t_start
        try:
            h = get_health()
        except Exception as e:
            print(f"  [{elapsed:6.1f}s] health err: {e!r}")
            time.sleep(POLL_INTERVAL)
            continue
        s = h["deploy_status"]
        active = h["active_executions"]

        if s != last_status:
            print(f"  [{elapsed:6.1f}s] status: {last_status} → {s}  active={active}  last_full_reset_at={h['last_full_reset_at']}")
            last_status = s
            if s == "draining" and not saw_drain_with_active_task:
                drain_entered_at = elapsed
                if active >= 1:
                    saw_drain_with_active_task = True
                    print(f"           >>> ✓ DRAIN entered while {active} task(s) still active")
                else:
                    print(f"           >>> drain entered but active={active}")

        # While in drain, attempt second admission (one-shot)
        if s == "draining" and not second_attempt_done:
            second_attempt_done = True
            code2, body2 = post_start_task()
            admission_during_drain_outcome = {"code": code2, "body": body2}
            print(f"  [{time.monotonic()-t_start:6.1f}s] 2nd /start_task → {code2}: {json.dumps(body2)[:200]}")
            # Now delete the original execution to allow drain to complete
            print(f"  [{time.monotonic()-t_start:6.1f}s] deleting execution {execution_id} to let drain finish")
            dc, dbody = delete_execution(execution_id)
            print(f"           >>> DELETE → {dc}")

        if s == "ready" and last_status == "ready" and drain_entered_at is not None:
            # Already past drain → second reset stamp
            second_reset_at = h["last_full_reset_at"]
            print(f"  [{elapsed:6.1f}s] final ready; last_full_reset_at={second_reset_at}")
            break
        if s == "ready" and drain_entered_at is not None:
            # First time we're back to ready after drain
            second_reset_at = h["last_full_reset_at"]
            print(f"  [{elapsed:6.1f}s] reset complete; last_full_reset_at={second_reset_at}")
            break

        time.sleep(POLL_INTERVAL)

    second_reset_at = h.get("last_full_reset_at") if 'h' in dir() else None

    print("=" * 72)
    print("VERDICTS:")
    checks = [
        ("admitted held execution", True),  # we got this far
        ("drain entered while 1+ active tasks", saw_drain_with_active_task),
        ("admission during DRAIN returned 503", (
            admission_during_drain_outcome is not None
            and admission_during_drain_outcome["code"] == 503
        )),
        ("admission reason=infra_draining", (
            admission_during_drain_outcome is not None
            and admission_during_drain_outcome["body"].get("detail", {}).get("reason") == "infra_draining"
        )),
        ("after dict drained → status returned to 'ready'", last_status == "ready" and drain_entered_at is not None),
        ("4h clock restarted (new last_full_reset_at > first)", (
            second_reset_at is not None
            and first_reset_at is not None
            and second_reset_at > first_reset_at
        )),
    ]
    all_ok = True
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok:
            all_ok = False
    print("=" * 72)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
