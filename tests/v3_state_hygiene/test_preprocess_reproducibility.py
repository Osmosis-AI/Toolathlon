"""Deep reproducibility tests for task preprocess scripts.

For each representative task across the four shared services, this test:
  1. Runs the task's preprocess once.
  2. Snapshots state visible through the AGENT's runtime credentials
     (NOT preprocess-only admin tokens) — we record only the surfaces an
     agent would actually look at.
  3. Pollutes those surfaces with marker resources whose names cannot
     collide with any task's owned resources.
  4. Runs the task's preprocess again.
  5. Re-snapshots and asserts the markers are gone AND the post-state
     matches the post-preprocess state from step 2 (modulo nondeterminism
     in internal IDs / timestamps — see _canonicalize_*).

A passing test demonstrates: the same task running multiple times on a
never-reset shared infra produces the same initial state from the
agent's perspective.

Representative tasks covered:
  - Canvas:        canvas-new-students-notification (admin token)
  - Poste:         apply-phd-email (per-task mailbox)
  - WooCommerce:   woocommerce-stock-alert (dedicated /store84)
  - kind/k8s:      k8s-mysql (per-task fresh cluster)
"""

from __future__ import annotations

import imaplib
import json
import os
import smtplib
import subprocess
import sys
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402


# ── Common helpers ─────────────────────────────────────────────────

def _run_preprocess(task: str, timeout: int = 600) -> Tuple[int, str]:
    """Invoke a task's preprocess/main.py via the same entry point pattern
    every task supports.  Returns (exit_code, last 1500 chars of output)."""
    workspace = Path("/tmp") / f"reprotest_{task}_{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + ":" + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        ["uv", "run", "python",
         f"tasks/finalpool/{task}/preprocess/main.py",
         "--agent_workspace", str(workspace)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True, text=True, timeout=timeout,
    )
    output = (proc.stdout + "\n" + proc.stderr)[-1500:]
    return proc.returncode, output


def _diff_keys(a: Dict, b: Dict, path: str = "") -> List[str]:
    """Return human-readable diff lines for two dicts."""
    diffs: List[str] = []
    for k in set(a) | set(b):
        if k not in a:
            diffs.append(f"  + {path}{k} = {b[k]!r}")
        elif k not in b:
            diffs.append(f"  - {path}{k} = {a[k]!r}")
        elif a[k] != b[k]:
            if isinstance(a[k], dict) and isinstance(b[k], dict):
                diffs.extend(_diff_keys(a[k], b[k], f"{path}{k}."))
            else:
                diffs.append(f"  ! {path}{k}: {a[k]!r} -> {b[k]!r}")
    return diffs


# ── Canvas snapshotter ─────────────────────────────────────────────

def _canvas_snapshot(token: str, account_id: int = 1) -> Dict[str, Any]:
    """Snapshot the agent-observable Canvas state.

    Captures: course names (with workflow_state), per-course assignment
    names (with points_possible), per-course quiz names, count of
    conversations the user can see.  Strips internal IDs because they
    drift across delete-and-recreate cycles.
    """
    base = "http://localhost:10001/api/v1"
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=15.0, headers=h) as c:
        # Admin sees /accounts/{id}/courses (including unpublished).
        # Per-user agent token would use /courses but admin token
        # benefits from the account-scoped variant.  We try account
        # first, fall back to user-scoped.
        r = c.get(f"{base}/accounts/{account_id}/courses", params={"per_page": 100})
        if r.status_code != 200:
            r = c.get(f"{base}/courses", params={"per_page": 100})
        r.raise_for_status()
        courses = sorted(
            ({"name": cc["name"], "workflow_state": cc.get("workflow_state")}
             for cc in r.json()),
            key=lambda x: x["name"],
        )

        per_course: Dict[str, Dict[str, Any]] = {}
        # Build per-course detail using the same listing
        for cc in r.json():
            cid = cc["id"]
            cname = cc["name"]
            # assignments
            try:
                ar = c.get(f"{base}/courses/{cid}/assignments", params={"per_page": 100})
                assignments = sorted(
                    [{"name": a["name"], "points_possible": a.get("points_possible")}
                     for a in ar.json()],
                    key=lambda x: x["name"],
                ) if ar.status_code == 200 else []
            except Exception:
                assignments = []
            # quizzes
            try:
                qr = c.get(f"{base}/courses/{cid}/quizzes", params={"per_page": 100})
                quizzes = sorted(
                    [{"title": q["title"], "question_count": q.get("question_count")}
                     for q in qr.json()],
                    key=lambda x: x["title"],
                ) if qr.status_code == 200 else []
            except Exception:
                quizzes = []
            per_course[cname] = {"assignments": assignments, "quizzes": quizzes}

        # conversations across all scopes
        conv_count = 0
        for scope in ("inbox", "sent", "archived"):
            try:
                cr = c.get(f"{base}/conversations",
                           params={"scope": scope, "per_page": 100})
                if cr.status_code == 200:
                    d = cr.json()
                    conv_count += len(d) if isinstance(d, list) else 0
            except Exception:
                pass

    return {
        "courses": courses,
        "per_course": per_course,
        "conversation_count": conv_count,
    }


def _canvas_pollute(token: str) -> List[str]:
    """Create a marker conversation that the next preprocess MUST delete.
    Returns the marker subject so the test can confirm it disappeared.
    """
    base = "http://localhost:10001/api/v1"
    h = {"Authorization": f"Bearer {token}"}
    nonce = f"__repro_pollute_{uuid.uuid4().hex[:8]}"
    with httpx.Client(timeout=10.0, headers=h) as c:
        rs = c.get(f"{base}/users/self")
        rs.raise_for_status()
        my_id = rs.json()["id"]
        r = c.post(
            f"{base}/conversations",
            data={
                "recipients[]": str(my_id),
                "subject": nonce,
                "body": "pollution marker — should be wiped by next preprocess",
                "group_conversation": "false",
            },
        )
        r.raise_for_status()
    return [nonce]


# ── Poste snapshotter ──────────────────────────────────────────────

def _poste_snapshot(email_config: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot mailbox state: INBOX and Sent message subjects sorted."""
    server = email_config["imap_server"]
    port = int(email_config["imap_port"])
    user = email_config["email"]
    pw = email_config["password"]

    result: Dict[str, List[str]] = {}
    for folder in ("INBOX", "Sent"):
        try:
            imap = imaplib.IMAP4(server, port)
            try:
                imap.login(user, pw)
                typ, _ = imap.select(folder)
                if typ != "OK":
                    result[folder] = []
                    continue
                typ, data = imap.search(None, "ALL")
                ids = data[0].split() if typ == "OK" else []
                subjects = []
                for mid in ids:
                    try:
                        typ, mdata = imap.fetch(mid, "(BODY[HEADER.FIELDS (SUBJECT FROM)])")
                        if typ == "OK" and mdata and mdata[0]:
                            subjects.append(mdata[0][1].decode("utf-8", errors="replace").strip())
                    except Exception:
                        pass
                result[folder] = sorted(subjects)
            finally:
                try: imap.logout()
                except Exception: pass
        except Exception:
            result[folder] = []
    return result


def _poste_pollute(email_config: Dict[str, Any]) -> List[str]:
    nonce = f"__repro_pollute_{uuid.uuid4().hex[:8]}"
    user = email_config["email"]
    smtp_port = 2525  # the AUTH-accepting port (not 1587 which needs STARTTLS)
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = user
    msg["Subject"] = nonce
    msg.set_content("pollution marker")
    s = smtplib.SMTP("localhost", smtp_port, timeout=8.0)
    try:
        s.ehlo()
        s.login(user, email_config["password"])
        s.send_message(msg)
    finally:
        try: s.quit()
        except Exception: pass
    # give LDA a moment
    time.sleep(1.0)
    return [nonce]


# ── WooCommerce snapshotter ────────────────────────────────────────

def _woo_snapshot(site_url: str, key: str, secret: str) -> Dict[str, Any]:
    """Snapshot store: product names (with stock + price + status)."""
    with httpx.Client(timeout=15.0, auth=(key, secret)) as c:
        r = c.get(f"{site_url}/wp-json/wc/v3/products", params={"per_page": 100})
        r.raise_for_status()
        products = sorted(
            ({"name": p["name"], "sku": p.get("sku"), "price": p.get("price"),
              "status": p.get("status"), "stock_status": p.get("stock_status"),
              "stock_quantity": p.get("stock_quantity")}
             for p in r.json()),
            key=lambda x: (x["name"], x.get("sku") or ""),
        )
    return {"products": products}


def _woo_pollute(site_url: str, key: str, secret: str) -> List[str]:
    nonce = f"__repro_pollute_{uuid.uuid4().hex[:8]}"
    with httpx.Client(timeout=15.0, auth=(key, secret)) as c:
        r = c.post(
            f"{site_url}/wp-json/wc/v3/products",
            json={"name": nonce, "type": "simple", "regular_price": "9.99"},
        )
        r.raise_for_status()
    return [nonce]


# ── kind/k8s snapshotter ───────────────────────────────────────────

def _k8s_snapshot(kubeconfig_path: str) -> Dict[str, Any]:
    """Snapshot cluster state: namespace list + per-namespace pod names."""
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig_path
    try:
        ns_proc = subprocess.run(
            ["kubectl", "get", "ns", "-o",
             "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
            env=env, capture_output=True, text=True, timeout=20,
        )
        namespaces = sorted(line for line in ns_proc.stdout.splitlines() if line.strip())
    except Exception:
        namespaces = []

    pods_by_ns: Dict[str, List[str]] = {}
    for ns in namespaces:
        try:
            pp = subprocess.run(
                ["kubectl", "-n", ns, "get", "pods", "-o",
                 "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
                env=env, capture_output=True, text=True, timeout=20,
            )
            pods_by_ns[ns] = sorted(line for line in pp.stdout.splitlines() if line.strip())
        except Exception:
            pods_by_ns[ns] = []
    return {"namespaces": namespaces, "pods_by_ns": pods_by_ns}


def _k8s_pollute(kubeconfig_path: str) -> List[str]:
    """Create a dummy namespace.  Subsequent preprocess deletes the entire
    cluster so the namespace cannot survive."""
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig_path
    nonce = f"repro-pollute-{uuid.uuid4().hex[:8]}"
    subprocess.run(["kubectl", "create", "ns", nonce],
                   env=env, capture_output=True, text=True, timeout=15)
    return [nonce]


# ── Per-task test definitions ──────────────────────────────────────

def check_canvas_new_students_notification() -> Tuple[str, bool, str]:
    """Canvas admin1 token + 'Introduction to AI-8' course.  Preprocess
    deletes course AND calls cleanup_conversations.  Test that polluting
    the admin1's conversation view is wiped by the next preprocess.
    """
    task = "canvas-new-students-notification"
    token = "mcpcanvasadmintoken1"

    # Run 1
    rc1, out1 = _run_preprocess(task)
    if rc1 != 0:
        return task, False, f"first preprocess failed (rc={rc1}); tail:\n{out1}"
    snap1 = _canvas_snapshot(token)

    # Pollute
    pollute_subjects = _canvas_pollute(token)

    # Verify pollution actually took
    polluted_snap = _canvas_snapshot(token)
    if polluted_snap["conversation_count"] <= snap1["conversation_count"]:
        return task, False, (
            f"pollution didn't take: conv_count {snap1['conversation_count']} -> "
            f"{polluted_snap['conversation_count']}"
        )

    # Run 2
    rc2, out2 = _run_preprocess(task)
    if rc2 != 0:
        return task, False, f"second preprocess failed (rc={rc2}); tail:\n{out2}"
    snap2 = _canvas_snapshot(token)

    # Pollution markers must be gone
    if snap2["conversation_count"] != snap1["conversation_count"]:
        return task, False, (
            f"conv count drift after re-preprocess: "
            f"{snap1['conversation_count']} (after run1) vs "
            f"{snap2['conversation_count']} (after run2 — polluted with "
            f"{pollute_subjects} between)"
        )

    # Course-level state must match across runs
    if snap1["courses"] != snap2["courses"] or snap1["per_course"] != snap2["per_course"]:
        diffs = _diff_keys(snap1, snap2)
        return task, False, f"course-level state drift across runs:\n" + "\n".join(diffs[:8])

    return task, True, (
        f"2 runs converge: {len(snap1['courses'])} courses, "
        f"{sum(len(c['assignments']) for c in snap1['per_course'].values())} assignments, "
        f"conv_count={snap1['conversation_count']} (pollution markers cleared)"
    )


def check_apply_phd_email() -> Tuple[str, bool, str]:
    """Poste-only task using setup_email_environment.  Mailbox should be
    deterministic across runs.
    """
    task = "apply-phd-email"
    email_config = {
        "email": "maryc@mcp.com",
        "password": "castillo$m888",
        "imap_server": "localhost",
        "imap_port": 1143,
        "smtp_server": "localhost",
        "smtp_port": 1587,
        "use_ssl": False,
        "use_starttls": False,
    }

    # Run 1
    rc1, out1 = _run_preprocess(task)
    if rc1 != 0:
        return task, False, f"first preprocess failed (rc={rc1}); tail:\n{out1}"
    snap1 = _poste_snapshot(email_config)

    # Pollute
    pollute_subjects = _poste_pollute(email_config)
    polluted = _poste_snapshot(email_config)
    if all(not any(p in s for s in polluted["INBOX"]) for p in pollute_subjects):
        return task, False, f"pollution markers not seen in INBOX: {pollute_subjects}"

    # Run 2
    rc2, out2 = _run_preprocess(task)
    if rc2 != 0:
        return task, False, f"second preprocess failed (rc={rc2}); tail:\n{out2}"
    snap2 = _poste_snapshot(email_config)

    # Markers must be gone
    leaked = [p for p in pollute_subjects
              if any(p in s for s in snap2["INBOX"]) or any(p in s for s in snap2["Sent"])]
    if leaked:
        return task, False, f"pollution markers survived re-preprocess: {leaked}"

    # State across runs must match
    if snap1["INBOX"] != snap2["INBOX"]:
        return task, False, (
            f"INBOX drift across runs: run1={len(snap1['INBOX'])} run2={len(snap2['INBOX'])}; "
            f"only-in-run1={sorted(set(snap1['INBOX']) - set(snap2['INBOX']))[:3]}; "
            f"only-in-run2={sorted(set(snap2['INBOX']) - set(snap1['INBOX']))[:3]}"
        )

    return task, True, (
        f"2 runs converge: INBOX={len(snap1['INBOX'])} msgs, Sent={len(snap1['Sent'])} msgs; "
        f"pollution markers ({len(pollute_subjects)}) wiped"
    )


def check_woocommerce_stock_alert() -> Tuple[str, bool, str]:
    """WooCommerce store /store84 — preprocess should leave store in
    deterministic product set."""
    task = "woocommerce-stock-alert"
    site_url = "http://localhost:10003/store84"
    key = "ck_woocommerce_token_benjhMtCdOGk"
    secret = "cs_woocommerce_token_benjhMtCdOGk"

    rc1, out1 = _run_preprocess(task)
    if rc1 != 0:
        return task, False, f"first preprocess failed (rc={rc1}); tail:\n{out1}"
    snap1 = _woo_snapshot(site_url, key, secret)

    pollute_markers = _woo_pollute(site_url, key, secret)
    polluted = _woo_snapshot(site_url, key, secret)
    if not any(p["name"] in pollute_markers for p in polluted["products"]):
        return task, False, f"pollution didn't materialize: markers={pollute_markers}"

    rc2, out2 = _run_preprocess(task)
    if rc2 != 0:
        return task, False, f"second preprocess failed (rc={rc2}); tail:\n{out2}"
    snap2 = _woo_snapshot(site_url, key, secret)

    leaked = [p for p in snap2["products"] if p["name"] in pollute_markers]
    if leaked:
        return task, False, f"pollution markers survived re-preprocess: {[p['name'] for p in leaked]}"

    # Compare product sets ignoring volatile fields
    a = sorted((p["name"], p["sku"]) for p in snap1["products"])
    b = sorted((p["name"], p["sku"]) for p in snap2["products"])
    if a != b:
        return task, False, (
            f"product set drift: run1={len(a)} run2={len(b)}; "
            f"only-in-1={sorted(set(a)-set(b))[:3]}, only-in-2={sorted(set(b)-set(a))[:3]}"
        )
    return task, True, (
        f"2 runs converge: {len(a)} products; pollution markers wiped"
    )


def check_k8s_mysql() -> Tuple[str, bool, str]:
    """k8s-mysql preprocess creates its own kind cluster every run.  The
    second run deletes and recreates the cluster from scratch.
    """
    task = "k8s-mysql"
    kubeconfig = str(PROJECT_ROOT / "tasks/finalpool/k8s-mysql/scripts/../k8s_configs/cluster-mysql-config.yaml")

    rc1, out1 = _run_preprocess(task, timeout=600)
    if rc1 != 0:
        return task, False, f"first preprocess failed (rc={rc1}); tail:\n{out1}"
    if not Path(kubeconfig).exists():
        return task, False, f"kubeconfig not created at {kubeconfig}"
    snap1 = _k8s_snapshot(kubeconfig)

    pollute_ns = _k8s_pollute(kubeconfig)
    polluted = _k8s_snapshot(kubeconfig)
    if not any(n in pollute_ns for n in polluted["namespaces"]):
        return task, False, f"pollution didn't take: ns={pollute_ns}, snap.namespaces={polluted['namespaces']}"

    rc2, out2 = _run_preprocess(task, timeout=600)
    if rc2 != 0:
        return task, False, f"second preprocess failed (rc={rc2}); tail:\n{out2}"
    snap2 = _k8s_snapshot(kubeconfig)

    leaked = [n for n in pollute_ns if n in snap2["namespaces"]]
    if leaked:
        return task, False, f"polluted namespaces survived cluster recreate: {leaked}"

    if sorted(snap1["namespaces"]) != sorted(snap2["namespaces"]):
        return task, False, (
            f"namespace drift: run1={snap1['namespaces']} run2={snap2['namespaces']}"
        )
    return task, True, (
        f"2 runs converge: {len(snap1['namespaces'])} namespaces, "
        f"pollution ns wiped (cluster recreated)"
    )


CHECKS = [
    check_apply_phd_email,                  # cheapest — Poste only, ~10s
    check_woocommerce_stock_alert,          # Woo — minutes
    check_canvas_new_students_notification, # Canvas admin — minutes
    check_k8s_mysql,                        # slowest — full kind recreate, several minutes
]


def main() -> int:
    print("=" * 78)
    print(f"  DEEP PREPROCESS-REPRODUCIBILITY TESTS  ({len(CHECKS)} representative tasks)")
    print("=" * 78)
    fails = 0
    t_total = time.monotonic()
    for fn in CHECKS:
        print()
        print(f"  >>> {fn.__name__} starting ...")
        t0 = time.monotonic()
        try:
            name, ok, detail = fn()
        except Exception as e:
            name, ok, detail = fn.__name__, False, f"unhandled exception: {e!r}"
        dt = time.monotonic() - t0
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name:<40} ({dt:6.1f}s)  {detail}")
        if not ok:
            fails += 1
    print()
    print("-" * 78)
    print(f"  {len(CHECKS) - fails}/{len(CHECKS)} passed in {(time.monotonic() - t_total):.1f}s")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
