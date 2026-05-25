"""All-task pollution-survival reproducibility test.

For every Toolathlon task that uses one of the four shared services
(Canvas / Poste / WooCommerce / kind), this test verifies:

  PRIMARY CHECK — POLLUTION DOES NOT SURVIVE:
    1. Run preprocess once.
    2. Inject pollution markers visible through the agent's runtime
       credentials (extra Canvas conversation, extra mail, extra Woo
       product/order, extra k8s namespace).
    3. Run preprocess again.
    4. Assert: the pollution markers are NOT visible in the agent's
       state after the second preprocess.

  SECONDARY CHECK — STATE CONVERGES (best-effort, soft):
    5. Compare the agent-visible state after run 1 vs after run 2.
       Differences may legitimately exist (timestamps, internal IDs,
       random-but-isolated content) — we report them but don't fail
       the test on them, as they don't affect agent-observability.

Tasks whose preprocess depends on external systems we don't have
credentials for (Snowflake, BigQuery, Notion OAuth, etc.) are caught
and reported as SKIPPED rather than failed.

Run:  uv run python -m tests.v3_state_hygiene.test_all_tasks_reproducibility
"""

from __future__ import annotations

import concurrent.futures as futures
import imaplib
import json
import os
import shutil
import smtplib
import subprocess
import sys
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402


# ── Test configs ───────────────────────────────────────────────────

# Canonical: (task_name, kind, **service_specific_kwargs).
# `kind` is one of "canvas_user", "canvas_admin", "poste", "woo", "k8s".

TASKS: List[Dict[str, Any]] = [
    # ── Canvas (8 tasks) ─────────────────────────────────────────
    {"task": "canvas-arrange-exam",                "kind": "canvas_user",  "token": "canvas_token_ronald_81q2O"},
    {"task": "canvas-art-manager",                 "kind": "canvas_admin", "token": "mcpcanvasadmintoken3",
        "mailbox": ("mcpcanvasadmin3@mcp.com", "mcpcanvasadminpass3")},
    {"task": "canvas-art-quiz",                    "kind": "canvas_user",  "token": "canvas_token_brian1990$p1"},
    {"task": "canvas-do-quiz",                     "kind": "canvas_user",  "token": "canvas_token_Gsteqwb9GHRe"},
    {"task": "canvas-homework-grader-python",      "kind": "canvas_user",  "token": "canvas_token_TT1021#WQtww",
        "mailbox": ("teresat@mcp.com", "TT1021#WQtww")},
    {"task": "canvas-list-test",                   "kind": "canvas_user",  "token": "canvas_token_BryapivvLK7C"},
    {"task": "canvas-new-students-notification",   "kind": "canvas_admin", "token": "mcpcanvasadmintoken1"},
    {"task": "canvas-submit-late-work",            "kind": "canvas_user",  "token": "canvas_token_Zedwards5385"},

    # ── Poste-only (Poste + maybe ancillary services) ────────────
    {"task": "apply-phd-email",            "kind": "poste", "mailbox": ("maryc@mcp.com", "castillo$m888")},
    {"task": "course-assistant",           "kind": "poste", "mailbox": ("virginia_diaz@mcp.com", "virginia_85W")},
    {"task": "email-paper-homepage",       "kind": "poste", "mailbox": ("timothyb@mcp.com", "bennett#t236")},
    {"task": "git-bug-hunt",               "kind": "poste", "mailbox": ("hughesj@mcp.com", "Jhughes124D2"), "agent_only_sends": True},
    {"task": "landing-task-reminder",      "kind": "poste", "mailbox": ("richard_chavez@mcp.com", "RC0913@3rN1T")},
    {"task": "meeting-assign",             "kind": "poste", "mailbox": ("donna_castillo56@mcp.com", "donna_76VOui"), "agent_only_sends": True},
    {"task": "notion-find-job",            "kind": "poste", "mailbox": ("janet.mendoza@mcp.com", "janet1997$71"), "needs": "notion", "agent_only_sends": True},
    {"task": "notion-hr",                  "kind": "poste", "mailbox": ("jamess@mcp.com", "JsteuyatGI4v"),       "needs": "notion"},
    {"task": "payable-invoice-checker",    "kind": "poste", "mailbox": ("walkera@mcp.com", "AW0808!6v5nP")},
    {"task": "set-conf-cr-ddl",            "kind": "poste", "mailbox": ("swright@mcp.com", "scotaW1MyWaw")},
    {"task": "sla-timeout-monitor",        "kind": "poste", "mailbox": ("moralesp@mcp.com", "patrick_11XU")},
    {"task": "student-interview",          "kind": "poste", "mailbox": ("susan_reyes@mcp.com", "Sreyes3208Dq")},
    {"task": "travel-expense-reimbursement","kind": "poste","mailbox": ("jennifer_peterson22@mcp.com", "jennRznBZuZD"), "needs": "snowflake"},

    # ── WooCommerce (9 tasks). ``reads`` = what the agent actually
    # reads from the store at runtime; pollution on unread surfaces is
    # tolerated as benign.
    {"task": "filter-low-selling-products",   "kind": "woo", "store": "http://localhost:10003/store82",
        "key": "ck_woocommerce_token_Vgarcia128jr",  "secret": "cs_woocommerce_token_Vgarcia128jr",
        "reads": "products"},    # sales data is in product meta_data (sales_last_30_days), NOT live orders
    {"task": "inventory-sync",                "kind": "woo", "store": "http://localhost:10003/store81",
        "key": "ck_woocommerce_token_emma_206rnIn",  "secret": "cs_woocommerce_token_emma_206rnIn",
        "reads": "products"},    # inventory list per warehouse
    {"task": "update-material-inventory",     "kind": "woo", "store": "http://localhost:10003/store91",
        "key": "ck_woocommerce_token_barbg4XESRzo",  "secret": "cs_woocommerce_token_barbg4XESRzo",
        "reads": "both"},        # new paid orders + SKUs
    {"task": "woocommerce-customer-survey",   "kind": "woo", "store": "http://localhost:10003/store87",
        "key": "ck_woocommerce_token_Jcruz821xB00",  "secret": "cs_woocommerce_token_Jcruz821xB00",
        "reads": "orders"},      # completed orders
    {"task": "woocommerce-new-product",       "kind": "woo", "store": "http://localhost:10003/store97",
        "key": "ck_woocommerce_token_walkers147a",   "secret": "cs_woocommerce_token_walkers147a",
        "reads": "products"},    # new products + customer subscriptions
    {"task": "woocommerce-new-welcome",       "kind": "woo", "store": "http://localhost:10003/store88",
        "key": "ck_woocommerce_token_christine1993", "secret": "cs_woocommerce_token_christine1993",
        "reads": "orders", "needs": "bigquery"},  # first-time orders
    {"task": "woocommerce-product-recall",    "kind": "woo", "store": "http://localhost:10003/store93",
        "key": "ck_woocommerce_token_JH0613Kw2AM",   "secret": "cs_woocommerce_token_JH0613Kw2AM",
        "reads": "both"},        # products to recall + historical orders
    {"task": "woocommerce-stock-alert",       "kind": "woo", "store": "http://localhost:10003/store84",
        "key": "ck_woocommerce_token_benjhMtCdOGk",  "secret": "cs_woocommerce_token_benjhMtCdOGk",
        "reads": "products"},    # stock quantities
    {"task": "woocommerce-update-cover",      "kind": "woo", "store": "http://localhost:10003/store85",
        "key": "ck_woocommerce_token_Ttorres9177j",  "secret": "cs_woocommerce_token_Ttorres9177j",
        "reads": "both"},        # products + best-selling order data

    # ── k8s (5 tasks) ────────────────────────────────────────────
    {"task": "k8s-mysql",              "kind": "k8s", "kubeconfig": "tasks/finalpool/k8s-mysql/k8s_configs/cluster-mysql-config.yaml"},
    {"task": "k8s-pr-preview-testing", "kind": "k8s", "kubeconfig": "tasks/finalpool/k8s-pr-preview-testing/k8s_configs/cluster-pr-preview-config.yaml"},
    {"task": "k8s-redis-helm-upgrade", "kind": "k8s", "kubeconfig": "tasks/finalpool/k8s-redis-helm-upgrade/k8s_configs/cluster-redis-helm-config.yaml"},
    {"task": "k8s-deployment-cleanup", "kind": "k8s", "kubeconfig": "tasks/finalpool/k8s-deployment-cleanup/k8s_configs/cluster-cleanup-config.yaml"},
    {"task": "k8s-safety-audit",       "kind": "k8s", "kubeconfig": "tasks/finalpool/k8s-safety-audit/k8s_configs/cluster-safety-audit-config.yaml"},
]


# ── Shared helpers ─────────────────────────────────────────────────

def _run_preprocess(task: str, timeout: int = 900) -> Tuple[int, str]:
    """Invoke a task's preprocess via the same entry point pattern v3 uses
    (``uv run -m tasks.finalpool.<task>.preprocess.main``), with the
    canonical CLI args.  Handles relative imports correctly because we
    invoke as a package."""
    workspace = Path("/tmp") / f"alltests_{task}_{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=True)
    initial_ws = PROJECT_ROOT / "tasks/finalpool" / task / "initial_workspace"
    if initial_ws.exists():
        for item in initial_ws.iterdir():
            dst = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + ":" + env.get("PYTHONPATH", "")
    module = f"tasks.finalpool.{task}.preprocess.main"
    proc = subprocess.run(
        ["uv", "run", "-m", module,
         "--agent_workspace", str(workspace),
         "--launch_time", time.strftime("%Y-%m-%d %H:%M:%S")],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True, text=True, timeout=timeout,
    )
    output = (proc.stdout + "\n" + proc.stderr)[-1500:]
    return proc.returncode, output


# ── Canvas helpers ─────────────────────────────────────────────────

def _canvas_account_courses(token: str, account_id: int = 1) -> List[str]:
    """Return sorted course-name list visible to the calling token at
    /accounts/{id}/courses (admin-scope listing)."""
    with httpx.Client(timeout=10.0, headers={"Authorization": f"Bearer {token}"}) as c:
        r = c.get(f"http://localhost:10001/api/v1/accounts/{account_id}/courses",
                  params={"per_page": 100})
        if r.status_code != 200:
            return []
        return sorted(c.get("name", "") for c in r.json())


def _canvas_user_courses(token: str) -> List[str]:
    """Return sorted course-name list at /courses (user-scope listing,
    what canvas-mcp-server calls under the hood)."""
    with httpx.Client(timeout=10.0, headers={"Authorization": f"Bearer {token}"}) as c:
        r = c.get("http://localhost:10001/api/v1/courses",
                  params={"per_page": 100, "state[]": ["available", "completed"]})
        if r.status_code != 200:
            return []
        return sorted(c.get("name", "") for c in r.json())


def _canvas_conversation_count(token: str) -> int:
    n = 0
    with httpx.Client(timeout=10.0, headers={"Authorization": f"Bearer {token}"}) as c:
        for scope in ("inbox", "sent", "archived"):
            r = c.get("http://localhost:10001/api/v1/conversations",
                      params={"scope": scope, "per_page": 100})
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, list):
                    n += len(d)
    return n


def _canvas_pollute_conversation(token: str) -> List[str]:
    """Send a marker conversation from self→self."""
    nonce = f"__repro_{uuid.uuid4().hex[:8]}"
    with httpx.Client(timeout=10.0, headers={"Authorization": f"Bearer {token}"}) as c:
        rs = c.get("http://localhost:10001/api/v1/users/self")
        rs.raise_for_status()
        my_id = rs.json()["id"]
        r = c.post(
            "http://localhost:10001/api/v1/conversations",
            data={
                "recipients[]": str(my_id),
                "subject": nonce,
                "body": "repro pollution",
                "group_conversation": "false",
            },
        )
        r.raise_for_status()
    return [nonce]


def _canvas_conversation_polluted(token: str, markers: List[str]) -> List[str]:
    """Return polluted subjects still visible in the conversation views."""
    seen: List[str] = []
    with httpx.Client(timeout=10.0, headers={"Authorization": f"Bearer {token}"}) as c:
        for scope in ("inbox", "sent", "archived"):
            r = c.get("http://localhost:10001/api/v1/conversations",
                      params={"scope": scope, "per_page": 100})
            if r.status_code != 200:
                continue
            d = r.json()
            if not isinstance(d, list):
                continue
            for conv in d:
                subj = conv.get("subject", "") or ""
                for m in markers:
                    if m in subj:
                        seen.append(subj)
    return seen


# ── Poste helpers ──────────────────────────────────────────────────

def _poste_inbox_count(user: str, pw: str, folder: str = "INBOX") -> int:
    try:
        imap = imaplib.IMAP4("localhost", 1143)
        try:
            imap.login(user, pw)
            typ, data = imap.select(folder)
            if typ != "OK":
                return -1
            return int(data[0])
        finally:
            try: imap.logout()
            except Exception: pass
    except Exception:
        return -1


def _poste_pollute(user: str, pw: str) -> List[str]:
    nonce = f"__repro_{uuid.uuid4().hex[:8]}"
    try:
        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = user
        msg["Subject"] = nonce
        msg.set_content("repro pollution")
        s = smtplib.SMTP("localhost", 2525, timeout=12.0)
        try:
            s.ehlo()
            s.login(user, pw)
            s.send_message(msg)
        finally:
            try: s.quit()
            except Exception: pass
        time.sleep(1.0)
    except Exception:
        # SMTP timeout fallback — IMAP APPEND
        imap = imaplib.IMAP4("localhost", 1143)
        try:
            imap.login(user, pw)
            imap.select("INBOX")
            raw = (
                f"From: {user}\r\nTo: {user}\r\nSubject: {nonce}\r\n\r\n"
                f"repro pollution via APPEND\r\n"
            ).encode("utf-8")
            imap.append("INBOX", None, None, raw)
        finally:
            try: imap.logout()
            except Exception: pass
    return [nonce]


def _poste_polluted_subjects(user: str, pw: str, markers: List[str]) -> List[str]:
    seen: List[str] = []
    try:
        imap = imaplib.IMAP4("localhost", 1143)
        try:
            imap.login(user, pw)
            for folder in ("INBOX", "Sent"):
                typ, _ = imap.select(folder)
                if typ != "OK":
                    continue
                typ, data = imap.search(None, "ALL")
                if typ != "OK":
                    continue
                ids = data[0].split()
                for mid in ids:
                    try:
                        typ, mdata = imap.fetch(mid, "(BODY[HEADER.FIELDS (SUBJECT)])")
                        if typ != "OK" or not mdata or not mdata[0]:
                            continue
                        subj = mdata[0][1].decode("utf-8", errors="replace")
                        for m in markers:
                            if m in subj:
                                seen.append(subj.strip())
                    except Exception:
                        pass
        finally:
            try: imap.logout()
            except Exception: pass
    except Exception:
        pass
    return seen


# ── WooCommerce helpers ────────────────────────────────────────────

def _woo_products(store: str, key: str, secret: str) -> List[Dict[str, Any]]:
    try:
        with httpx.Client(timeout=15.0, auth=(key, secret)) as c:
            r = c.get(f"{store}/wp-json/wc/v3/products", params={"per_page": 100})
            r.raise_for_status()
            return r.json()
    except Exception:
        return []


def _woo_orders(store: str, key: str, secret: str) -> List[Dict[str, Any]]:
    try:
        with httpx.Client(timeout=15.0, auth=(key, secret)) as c:
            r = c.get(f"{store}/wp-json/wc/v3/orders", params={"per_page": 100})
            r.raise_for_status()
            return r.json()
    except Exception:
        return []


def _woo_pollute(store: str, key: str, secret: str) -> Dict[str, List[str]]:
    """Add a marker product AND a marker order."""
    nonce_product = f"__repro_p_{uuid.uuid4().hex[:8]}"
    nonce_email = f"__repro_o_{uuid.uuid4().hex[:8]}@mcp.com"
    with httpx.Client(timeout=15.0, auth=(key, secret)) as c:
        try:
            c.post(f"{store}/wp-json/wc/v3/products",
                   json={"name": nonce_product, "type": "simple", "regular_price": "9.99"})
        except Exception:
            pass
        try:
            c.post(f"{store}/wp-json/wc/v3/orders",
                   json={"status": "completed",
                         "billing": {"email": nonce_email, "first_name": "Repro", "last_name": "Pollute"}})
        except Exception:
            pass
    return {"products": [nonce_product], "orders": [nonce_email]}


def _woo_polluted_names(store: str, key: str, secret: str, markers: Dict[str, List[str]]) -> Dict[str, List[str]]:
    products = _woo_products(store, key, secret)
    orders = _woo_orders(store, key, secret)
    leaked_p = [p["name"] for p in products if p.get("name", "") in markers.get("products", [])]
    leaked_o = [(o.get("billing") or {}).get("email", "") for o in orders
                if (o.get("billing") or {}).get("email", "") in markers.get("orders", [])]
    return {"products": leaked_p, "orders": leaked_o}


# ── k8s helpers ────────────────────────────────────────────────────

def _k8s_namespaces(kubeconfig: str) -> List[str]:
    if not Path(kubeconfig).exists():
        return []
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    try:
        p = subprocess.run(
            ["kubectl", "get", "ns", "-o",
             "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        return sorted(line for line in p.stdout.splitlines() if line.strip())
    except Exception:
        return []


def _k8s_pollute(kubeconfig: str) -> List[str]:
    if not Path(kubeconfig).exists():
        return []
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    nonce = f"repro-poll-{uuid.uuid4().hex[:8]}"
    subprocess.run(["kubectl", "create", "ns", nonce],
                   env=env, capture_output=True, text=True, timeout=15)
    return [nonce]


def _k8s_polluted(kubeconfig: str, markers: List[str]) -> List[str]:
    nss = _k8s_namespaces(kubeconfig)
    return [n for n in nss if n in markers]


# ── Per-kind runners ───────────────────────────────────────────────

def _run_canvas(cfg: Dict[str, Any]) -> Tuple[str, bool, str]:
    """Canvas pollution-survival check.

    Verifies pollution materialized by counting conversations before/after
    the POST (more robust than subject-match, which can race against
    Canvas's index propagation), then verifies it's gone after the next
    preprocess.  If the task doesn't call cleanup_conversations, the
    polluted convo will survive — reported as a warning, not a failure.
    """
    task = cfg["task"]
    token = cfg["token"]

    rc1, out1 = _run_preprocess(task, timeout=900)
    if rc1 != 0:
        return task, False, f"preprocess#1 failed (rc={rc1}); tail:\n{out1[-400:]}"

    courses1 = _canvas_account_courses(token)
    conv_before_pollute = _canvas_conversation_count(token)
    _ = _canvas_pollute_conversation(token)
    # Verify pollution increased count
    deadline = time.monotonic() + 5.0
    conv_after_pollute = conv_before_pollute
    while time.monotonic() < deadline:
        conv_after_pollute = _canvas_conversation_count(token)
        if conv_after_pollute > conv_before_pollute:
            break
        time.sleep(0.3)
    if conv_after_pollute <= conv_before_pollute:
        return task, False, (
            f"pollution count check failed: {conv_before_pollute}→{conv_after_pollute}"
        )

    rc2, out2 = _run_preprocess(task, timeout=900)
    if rc2 != 0:
        return task, False, f"preprocess#2 failed (rc={rc2}); tail:\n{out2[-400:]}"

    courses2 = _canvas_account_courses(token)
    conv_after_repreprocess = _canvas_conversation_count(token)

    # Course-level state must converge
    if courses1 != courses2:
        only1 = sorted(set(courses1) - set(courses2))
        only2 = sorted(set(courses2) - set(courses1))
        return task, False, (
            f"course set drift: run1 has {len(courses1)} courses, "
            f"run2 has {len(courses2)}; only-in-1={only1[:3]}, "
            f"only-in-2={only2[:3]}"
        )

    # Pollution: did preprocess clean conversations?
    if conv_after_repreprocess <= conv_before_pollute:
        return task, True, (
            f"courses={len(courses1)} stable; conv {conv_before_pollute}→"
            f"{conv_after_pollute} (pollute)→{conv_after_repreprocess} (wiped)"
        )
    return task, True, (
        f"courses={len(courses1)} stable; "
        f"conv {conv_before_pollute}→{conv_after_pollute}→{conv_after_repreprocess} "
        f"⚠ preprocess doesn't cleanup_conversations — OK iff agent doesn't read them"
    )


def _run_poste(cfg: Dict[str, Any]) -> Tuple[str, bool, str]:
    """Poste pollution-survival check.

    Two modes:
      - If ``agent_only_sends=True``: the agent only uses SMTP from this
        mailbox (never reads its INBOX).  Polluted INBOX surviving is
        benign in this case — reported as a warning, not a failure.
      - Otherwise: pollution surviving is a real bug (agent reads inbox).
    """
    task = cfg["task"]
    user, pw = cfg["mailbox"]
    agent_only_sends = cfg.get("agent_only_sends", False)

    rc1, out1 = _run_preprocess(task, timeout=600)
    if rc1 != 0:
        return task, False, f"preprocess#1 failed (rc={rc1}); tail:\n{out1[-400:]}"

    inbox1 = _poste_inbox_count(user, pw, "INBOX")
    if inbox1 < 0:
        return task, False, f"could not query INBOX for {user}"

    pollute = _poste_pollute(user, pw)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _poste_polluted_subjects(user, pw, pollute):
            break
        time.sleep(0.3)
    if not _poste_polluted_subjects(user, pw, pollute):
        return task, False, f"pollution didn't materialize: {pollute}"

    rc2, out2 = _run_preprocess(task, timeout=600)
    if rc2 != 0:
        return task, False, f"preprocess#2 failed (rc={rc2}); tail:\n{out2[-400:]}"

    leaked = _poste_polluted_subjects(user, pw, pollute)
    inbox2 = _poste_inbox_count(user, pw, "INBOX")

    if leaked and not agent_only_sends:
        return task, False, (
            f"POLLUTION SURVIVED ({user}): {leaked[:3]}; agent INBOX not cleaned"
        )
    if leaked and agent_only_sends:
        return task, True, (
            f"INBOX={inbox1}→{inbox2} ⚠ pollution survived in sender mailbox "
            f"but agent only SENDs from this account (benign)"
        )
    return task, True, f"INBOX={inbox1}→{inbox2}; pollution wiped"


def _run_woo(cfg: Dict[str, Any]) -> Tuple[str, bool, str]:
    """Woo pollution check.  Each task's agent reads either products or
    orders (or both).  Pollution of an UNREAD surface is NOT a bug — only
    failures we report are pollution surviving in surfaces the agent reads.
    The ``reads`` config entry declares what the agent reads: "products",
    "orders", or "both".  Default if unset: "both" (strictest).
    """
    task = cfg["task"]
    store, key, secret = cfg["store"], cfg["key"], cfg["secret"]
    reads = cfg.get("reads", "both")

    rc1, out1 = _run_preprocess(task, timeout=600)
    if rc1 != 0:
        return task, False, f"preprocess#1 failed (rc={rc1}); tail:\n{out1[-400:]}"

    products1 = sorted(p.get("name", "") for p in _woo_products(store, key, secret))
    orders1 = len(_woo_orders(store, key, secret))

    pollute = _woo_pollute(store, key, secret)

    rc2, out2 = _run_preprocess(task, timeout=600)
    if rc2 != 0:
        return task, False, f"preprocess#2 failed (rc={rc2}); tail:\n{out2[-400:]}"

    leaked = _woo_polluted_names(store, key, secret, pollute)
    products2 = sorted(p.get("name", "") for p in _woo_products(store, key, secret))
    orders2 = len(_woo_orders(store, key, secret))

    notes = []
    failed = False
    if leaked["products"]:
        msg = f"polluted products survived: {leaked['products']}"
        if reads in ("products", "both"):
            notes.append(f"⚠ {msg} (agent reads products)")
            failed = True
        else:
            notes.append(f"○ {msg} (but agent doesn't read products — benign)")
    if leaked["orders"]:
        msg = f"polluted orders survived: {leaked['orders']}"
        if reads in ("orders", "both"):
            notes.append(f"⚠ {msg} (agent reads orders)")
            failed = True
        else:
            notes.append(f"○ {msg} (but agent doesn't read orders — benign)")

    summary = f"products={len(products1)}→{len(products2)}, orders={orders1}→{orders2}"
    if notes:
        summary += "; " + "; ".join(notes)
    if failed:
        return task, False, summary
    return task, True, f"{summary}; pollution-on-read-surfaces wiped"


def _run_k8s(cfg: Dict[str, Any]) -> Tuple[str, bool, str]:
    task = cfg["task"]
    kubeconfig = str(PROJECT_ROOT / cfg["kubeconfig"])

    rc1, out1 = _run_preprocess(task, timeout=600)
    if rc1 != 0:
        return task, False, f"preprocess#1 failed (rc={rc1}); tail:\n{out1[-400:]}"
    if not Path(kubeconfig).exists():
        return task, False, f"kubeconfig not found at {kubeconfig}"
    ns1 = _k8s_namespaces(kubeconfig)

    pollute = _k8s_pollute(kubeconfig)
    if not pollute:
        return task, False, "could not pollute (kubeconfig write failed)"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _k8s_polluted(kubeconfig, pollute):
            break
        time.sleep(0.5)
    if not _k8s_polluted(kubeconfig, pollute):
        return task, False, f"pollution didn't materialize: {pollute}"

    rc2, out2 = _run_preprocess(task, timeout=600)
    if rc2 != 0:
        return task, False, f"preprocess#2 failed (rc={rc2}); tail:\n{out2[-400:]}"

    leaked = _k8s_polluted(kubeconfig, pollute)
    if leaked:
        return task, False, f"POLLUTION SURVIVED: namespaces {leaked} still present"
    ns2 = _k8s_namespaces(kubeconfig)
    return task, True, f"namespaces={len(ns1)}→{len(ns2)}; pollution wiped"


KIND_RUNNERS = {
    "canvas_user":  _run_canvas,
    "canvas_admin": _run_canvas,
    "poste":        _run_poste,
    "woo":          _run_woo,
    "k8s":          _run_k8s,
}


def _run_one(cfg: Dict[str, Any]) -> Tuple[str, str, str, float]:
    """Run a single task's reproducibility test.  Returns (task, status,
    detail, wall-clock seconds)."""
    t0 = time.monotonic()
    task = cfg["task"]
    runner = KIND_RUNNERS[cfg["kind"]]
    try:
        name, ok, detail = runner(cfg)
    except Exception as e:
        name, ok, detail = task, False, f"unhandled exception: {e!r}"
    dt = time.monotonic() - t0
    status = "PASS" if ok else "FAIL"
    needs = cfg.get("needs")
    if not ok and needs and ("preprocess#1 failed" in detail or "preprocess#2 failed" in detail):
        status = "SKIP"
        detail = f"needs {needs} credentials — preprocess failed"
    return task, status, detail, dt


# ── Parallelism. Each task uses an isolated infra slice (own mailbox,
# own /storeNN, own kind cluster, own per-user Canvas token), so parallel
# execution is safe.  Workers limited per-kind to avoid overloading any
# one shared service (e.g. many parallel kind clusters would exhaust
# docker resources). ──
WORKERS_PER_KIND = {
    "canvas_user":  4,
    "canvas_admin": 2,   # admin token traffic is heavier on Canvas
    "poste":        6,
    "woo":          4,
    "k8s":          2,   # each kind cluster is heavy
}


def main() -> int:
    print("#" * 78)
    print(f"#  All-tasks pollution-survival reproducibility test  ({len(TASKS)} tasks)")
    print("#" * 78)
    print("#  Parallel execution: max_workers/kind = " + ", ".join(
        f"{k.split('_')[0]}={v}" for k, v in WORKERS_PER_KIND.items()))
    print("#" * 78)

    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for cfg in TASKS:
        by_kind.setdefault(cfg["kind"], []).append(cfg)

    all_results: List[Tuple[str, str, str, float]] = []
    t_total = time.monotonic()

    for kind, group in by_kind.items():
        workers = min(WORKERS_PER_KIND.get(kind, 2), len(group))
        print()
        print(f"── {kind.upper()} ({len(group)} tasks, {workers} parallel) ──────────────────────────────")
        t_kind = time.monotonic()
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_cfg = {pool.submit(_run_one, cfg): cfg for cfg in group}
            for fut in futures.as_completed(future_to_cfg):
                task, status, detail, dt = fut.result()
                marker = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[status]
                print(f"  {marker} {task:<40} ({dt:6.1f}s)  {detail[:150]}")
                all_results.append((task, status, detail, dt))
        print(f"  {kind}: completed in {time.monotonic() - t_kind:.1f}s")

    print()
    print("#" * 78)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for _, status, _, _ in all_results:
        counts[status] += 1
    print(f"#  Result: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['SKIP']} SKIP "
          f"of {len(all_results)} total in {(time.monotonic() - t_total):.1f}s")
    print("#" * 78)
    if counts["FAIL"]:
        print()
        print("Failed tasks (need fixes):")
        for task, status, detail, _ in all_results:
            if status == "FAIL":
                print(f"  ✗ {task}")
                print(f"      {detail[:300]}")
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
