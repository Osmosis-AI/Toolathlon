#!/usr/bin/env python3
"""Behavioral readiness probe for Toolathlon shared infrastructure.

Replaces the older ``probe_shared_infra.sh`` (TCP-banner liveness) and
``probe_shared_infra_deep.py``.  Single probe, exercises real usage paths
against the same shared services tasks use:

  - **Poste**:  SMTP submission (STARTTLS + AUTH PLAIN) → IMAP fetch by
                subject → IMAP delete + EXPUNGE.  Round-trip uses the
                dedicated admin mailbox ``mcpposte_admin@mcp.com``, which
                no task owns, so concurrent tasks can't observe probe
                traffic in their own inboxes.
  - **Canvas**: ``GET /users/self`` + ``GET /accounts/1`` with an admin
                token.  Read-only by design — three admin-token tasks
                share Canvas's global course-listing view, so any write
                we did would briefly leak into their ``canvas_list_courses``
                results.  Confirms admin token validity and DB read.
  - **Woo**:    ``GET /wp-json/`` (WP REST root, unauth — no leak risk).
                Tasks each use a separate ``/storeNN/`` multi-site, so
                they're insulated from each other; the unauth root just
                confirms WordPress + REST are alive.
  - **kind**:   On the *base* shared cluster only (``cluster<suffix>1``,
                created by ``deploy_containers.sh``), which no task uses
                at runtime — each k8s task spins up its own dedicated
                cluster.  Apply a pause-image pod in namespace
                ``probe-system``, wait for Ready, delete.  Tests the
                scheduler, kubelet, image pull, and node lifecycle, all
                of which a node-Ready check alone misses.

Exits 0 if every check passes, 1 otherwise.  Per-check timing and (on
failure) detail are printed to stderr as ``✓ <name> (<dt>s)`` or
``✗ <name>: <detail> (<dt>s)``.

Usage::

    uv run python -m global_preparation.probe_shared_infra
    # or
    python3 global_preparation/probe_shared_infra.py
"""

from __future__ import annotations

import concurrent.futures as futures
import imaplib
import smtplib
import subprocess
import sys
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
import httpx  # noqa: E402

# NOTE: we intentionally do NOT load configs.token_key_session here.
# That file is a per-user placeholder reset per-task; using it for the
# probe (which validates the SHARED canvas DB, not any task's owned slice)
# leads to spurious 401s.  All probe credentials are hardcoded above
# (CANVAS_PROBE_TOKEN, POSTE_PROBE_USER/PASS).


# ── Probe-owned fixtures (do not change without coordinating with deploy_*) ──

POSTE_PROBE_USER = "mcpposte_admin@mcp.com"
POSTE_PROBE_PASS = "mcpposte"
# Canvas admin token seeded by deployment/canvas/scripts/create_admin_accounts.py.
# We use admin1 specifically because it's the most likely to remain valid across
# any in-place Canvas state work tasks might do.  Hardcoded (not read from
# ``configs.token_key_session``) because the global config file holds a
# per-user PLACEHOLDER token meant to be overridden per task — using it for the
# probe would mark Canvas as broken whenever the placeholder user happens not
# to exist in the Canvas DB.
CANVAS_PROBE_TOKEN = "mcpcanvasadmintoken1"
KIND_PROBE_NAMESPACE = "probe-system"
KIND_PROBE_IMAGE = "registry.k8s.io/pause:3.9"


# ── Port resolution ────────────────────────────────────────────────

def _load_ports_config() -> dict:
    with open(PROJECT_ROOT / "configs" / "ports_config.yaml", "r") as f:
        return yaml.safe_load(f) or {}


def _resolve_ports() -> dict:
    cfg = _load_ports_config()
    mappings = cfg.get("port_mappings", {}) or {}
    def m(default: int) -> int:
        return int(mappings.get(default, default))
    return {
        "canvas_http":   m(10001),
        "poste_smtp":    m(2525),
        "poste_imap":    m(1143),
        "poste_sub":     m(1587),
        "woo":           m(10003),
        "instance_suffix": cfg.get("instance_suffix", "") or "",
    }


# ── Individual checks ──────────────────────────────────────────────

def check_canvas(port: int) -> Tuple[bool, str]:
    """Admin-token self-lookup.  Confirms token + Canvas DB readable.

    Uses the seeded admin token directly rather than reading from
    ``configs.token_key_session`` because that file is a per-user
    placeholder that each task overrides — the placeholder user
    typically doesn't exist in the Canvas DB so the probe would mark
    Canvas as broken in fresh checkouts.
    """
    url = f"http://localhost:{port}/api/v1/users/self"
    token = CANVAS_PROBE_TOKEN
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        return False, f"GET /users/self failed: {e!r}"
    if r.status_code != 200:
        return False, f"GET /users/self → HTTP {r.status_code}: {r.text[:160]}"
    try:
        d = r.json()
    except Exception:
        return False, f"non-JSON body: {r.text[:120]}"
    if not isinstance(d.get("id"), int):
        return False, f"no numeric id in response: {d!r}"
    return True, ""


def check_woo(port: int) -> Tuple[bool, str]:
    """WP REST root reachable.  Unauth, no leak risk."""
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(f"http://localhost:{port}/wp-json/")
    except Exception as e:
        return False, f"GET /wp-json/ failed: {e!r}"
    if r.status_code != 200:
        return False, f"GET /wp-json/ → HTTP {r.status_code}: {r.text[:160]}"
    try:
        d = r.json()
    except Exception:
        return False, f"non-JSON body: {r.text[:120]}"
    if "namespaces" not in d:
        return False, f"unexpected /wp-json/ body: keys={list(d)[:5]}"
    return True, ""


def check_poste_behavioral(smtp_port: int, imap_port: int) -> Tuple[bool, str]:
    """Full send→receive→delete round-trip as the probe-owned admin user.

    1. IMAP LOGIN + SELECT INBOX  (auth + mailbox open)
    2. SMTP submit (plain AUTH PLAIN on port 2525) self→self with unique
       subject.  Port 2525 advertises AUTH directly; the 1587 submission
       port requires STARTTLS first (self-signed cert + Haraka quirk).
    3. IMAP SEARCH for the subject (poll up to 8s — LDA can take ~1s)
    4. IMAP STORE \\Deleted + EXPUNGE so the mailbox doesn't accumulate
    """
    subject = f"__probe_{uuid.uuid4().hex[:16]}"
    body = "Toolathlon shared-infra probe — safe to delete."

    # 1. IMAP login + SELECT
    try:
        imap = imaplib.IMAP4(host="localhost", port=imap_port)
    except Exception as e:
        return False, f"IMAP connect :{imap_port} failed: {e!r}"
    try:
        try:
            typ, _ = imap.login(POSTE_PROBE_USER, POSTE_PROBE_PASS)
            if typ != "OK":
                return False, f"IMAP LOGIN returned {typ}"
            typ, _ = imap.select("INBOX")
            if typ != "OK":
                return False, f"IMAP SELECT INBOX returned {typ}"
        except imaplib.IMAP4.error as e:
            return False, f"IMAP auth/select error: {e!r}"

        # 2. SMTP submit on port 2525 with plain AUTH PLAIN (no STARTTLS)
        try:
            smtp = smtplib.SMTP(host="localhost", port=smtp_port, timeout=10.0)
        except Exception as e:
            return False, f"SMTP connect :{smtp_port} failed: {e!r}"
        try:
            smtp.ehlo()
            try:
                smtp.login(POSTE_PROBE_USER, POSTE_PROBE_PASS)
            except smtplib.SMTPException as e:
                return False, f"SMTP AUTH on :{smtp_port} failed: {e!r}"

            msg = EmailMessage()
            msg["From"] = POSTE_PROBE_USER
            msg["To"] = POSTE_PROBE_USER
            msg["Subject"] = subject
            msg.set_content(body)
            try:
                smtp.send_message(msg)
            except smtplib.SMTPException as e:
                return False, f"SMTP send_message failed: {e!r}"
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

        # 3. IMAP poll until the message arrives (LDA can take ~1s)
        uid_list: List[bytes] = []
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                # New session each iteration would be cleaner; reusing
                # imap is fine but we need a fresh SELECT to see new mail.
                imap.noop()
                typ, data = imap.search(None, "SUBJECT", f'"{subject}"')
            except imaplib.IMAP4.error as e:
                return False, f"IMAP SEARCH error: {e!r}"
            if typ == "OK" and data and data[0]:
                uid_list = data[0].split()
                if uid_list:
                    break
            time.sleep(0.4)
        if not uid_list:
            return False, f"sent message subject={subject} not seen in INBOX within 8s"

        # 4. Cleanup: mark deleted + expunge
        try:
            for uid in uid_list:
                imap.store(uid, "+FLAGS", "\\Deleted")
            imap.expunge()
        except imaplib.IMAP4.error as e:
            return False, f"IMAP cleanup error: {e!r}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return True, ""


def check_kind_behavioral(instance_suffix: str) -> Tuple[bool, str]:
    """Apply + wait-ready + delete a pause pod on the base shared cluster.

    Tests scheduler + kubelet + node liveness, not just node-Ready.  Runs
    against the base cluster (which no task uses at runtime), in the
    probe-only ``probe-system`` namespace.
    """
    cluster = f"cluster{instance_suffix}1-control-plane"
    pod_name = f"probe-{uuid.uuid4().hex[:10]}"

    def run(args: List[str], stdin: str = "", timeout: float = 8.0) -> subprocess.CompletedProcess:
        # ``-i`` is required when piping yaml to ``kubectl apply -f -``;
        # benign for non-stdin commands.
        cmd = [
            "docker", "exec", "-i", cluster,
            "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf",
        ] + args
        return subprocess.run(
            cmd,
            input=stdin if stdin else None,
            capture_output=True, text=True, timeout=timeout,
        )

    # ensure probe namespace exists (idempotent)
    try:
        ns_check = run(["get", "ns", KIND_PROBE_NAMESPACE, "--no-headers"])
        if ns_check.returncode != 0:
            ns_create = run(["create", "ns", KIND_PROBE_NAMESPACE])
            if ns_create.returncode != 0 and "AlreadyExists" not in ns_create.stderr:
                return False, f"create ns failed: {ns_create.stderr.strip()[:160]}"
    except Exception as e:
        return False, f"namespace setup failed: {e!r}"

    pod_yaml = (
        f"apiVersion: v1\nkind: Pod\nmetadata:\n"
        f"  name: {pod_name}\n  namespace: {KIND_PROBE_NAMESPACE}\nspec:\n"
        f"  restartPolicy: Never\n  terminationGracePeriodSeconds: 1\n"
        f"  containers:\n"
        f"  - name: pause\n    image: {KIND_PROBE_IMAGE}\n"
        f"    imagePullPolicy: IfNotPresent\n"
    )

    # apply
    try:
        apply = run(["apply", "-n", KIND_PROBE_NAMESPACE, "-f", "-"], stdin=pod_yaml)
        if apply.returncode != 0:
            return False, f"kubectl apply failed: {apply.stderr.strip()[:160]}"
    except Exception as e:
        return False, f"apply error: {e!r}"

    # wait Ready (poll)
    ready = False
    deadline = time.monotonic() + 15.0
    last_phase = ""
    try:
        while time.monotonic() < deadline:
            ph = run([
                "get", "pod", pod_name, "-n", KIND_PROBE_NAMESPACE,
                "-o", "jsonpath={.status.phase}",
            ], timeout=5.0)
            if ph.returncode == 0:
                last_phase = ph.stdout.strip()
                if last_phase == "Running":
                    ready = True
                    break
                if last_phase == "Failed":
                    return False, f"pod entered Failed phase ({pod_name})"
            time.sleep(0.4)
    except Exception as e:
        # cleanup before reporting
        try: run(["delete", "pod", pod_name, "-n", KIND_PROBE_NAMESPACE, "--wait=false"])
        except Exception: pass
        return False, f"phase poll error: {e!r}"

    # cleanup pod regardless of ready
    try:
        run(["delete", "pod", pod_name, "-n", KIND_PROBE_NAMESPACE,
             "--grace-period=0", "--force", "--wait=false"], timeout=5.0)
    except Exception:
        pass  # best-effort

    if not ready:
        return False, f"pod {pod_name} never reached Running within 15s (last phase: {last_phase or 'unknown'})"
    return True, ""


# ── Retry wrapper ─────────────────────────────────────────────────
# Each individual check (canvas REST GET, woo REST GET, poste round-trip,
# kind pod apply) can transiently fail under normal load — DB momentarily
# locked, a packet drop, IMAP indexer pause, kubelet GC.  Reporting "infra
# broken" on a single such hiccup is too aggressive because the caller
# (ensure_shared_infra_ready) may then trigger a destructive repair
# (deploy_containers.sh tears down the shared containers).
#
# Real breakage SHOULD BE RARE, so we err on the side of more retries to
# minimise false positives.  Each check tries up to PROBE_CHECK_ATTEMPTS=5
# times with growing backoff (1s, 2s, 3s, 4s = 10s total wait worst case).
# A check is only declared failed when every attempt fails.
#
# Worst-case total probe time on a transient-then-recover: ~1s × attempts_taken
# + 1s..(2 + 3 + ... up to attempts_taken-1).  All checks run in parallel,
# so this is bounded by the slowest single check.  Happy-path latency is
# unchanged (single successful attempt).

PROBE_CHECK_ATTEMPTS = 5
PROBE_CHECK_BACKOFFS_S = [1.0, 2.0, 3.0, 4.0]  # delay BEFORE each retry


def _with_retry(fn: Callable[[], Tuple[bool, str]]) -> Tuple[bool, str]:
    """Run an individual check with retries on transient failure.

    Up to PROBE_CHECK_ATTEMPTS attempts; the i-th retry waits
    PROBE_CHECK_BACKOFFS_S[i-1] seconds before firing.
    """
    last_ok, last_detail = False, "no attempts"
    for i in range(PROBE_CHECK_ATTEMPTS):
        if i > 0:
            # PROBE_CHECK_BACKOFFS_S has PROBE_CHECK_ATTEMPTS-1 entries
            time.sleep(PROBE_CHECK_BACKOFFS_S[min(i - 1, len(PROBE_CHECK_BACKOFFS_S) - 1)])
        try:
            last_ok, last_detail = fn()
        except Exception as e:
            last_ok, last_detail = False, f"unhandled exception: {e!r}"
        if last_ok:
            if i > 0:
                last_detail = (last_detail or "") + f" (recovered after {i + 1} attempts)"
            return last_ok, last_detail
    # Every attempt failed → genuine breakage
    return last_ok, last_detail + f" (after {PROBE_CHECK_ATTEMPTS} attempts)"


# ── Runner ────────────────────────────────────────────────────────

def run() -> int:
    ports = _resolve_ports()
    checks: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
        ("canvas", lambda: _with_retry(lambda: check_canvas(ports["canvas_http"]))),
        ("woo",    lambda: _with_retry(lambda: check_woo(ports["woo"]))),
        ("poste",  lambda: _with_retry(lambda: check_poste_behavioral(ports["poste_smtp"], ports["poste_imap"]))),
        ("kind",   lambda: _with_retry(lambda: check_kind_behavioral(ports["instance_suffix"]))),
    ]

    results: List[Tuple[str, bool, str, float]] = []
    t_total = time.monotonic()

    with futures.ThreadPoolExecutor(max_workers=len(checks)) as pool:
        fut_to_name = {pool.submit(_timed, fn): name for name, fn in checks}
        for fut in futures.as_completed(fut_to_name):
            name = fut_to_name[fut]
            try:
                ok, detail, dt = fut.result()
            except Exception as e:
                ok, detail, dt = False, f"unhandled exception: {e!r}", 0.0
            results.append((name, ok, detail, dt))

    results.sort(key=lambda x: ("canvas", "woo", "poste", "kind").index(x[0]))
    any_fail = 0
    for name, ok, detail, dt in results:
        if ok:
            print(f"✓ {name} ({dt:.2f}s)", file=sys.stderr)
        else:
            print(f"✗ {name}: {detail} ({dt:.2f}s)", file=sys.stderr)
            any_fail = 1

    print(f"  total: {(time.monotonic() - t_total):.2f}s", file=sys.stderr)
    return any_fail


def _timed(fn: Callable[[], Tuple[bool, str]]) -> Tuple[bool, str, float]:
    t0 = time.monotonic()
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"unhandled exception: {e!r}"
    return ok, detail, time.monotonic() - t0


if __name__ == "__main__":
    sys.exit(run())
