"""End-to-end tests for the preprocess-hygiene helpers we rely on.

Each test:
  1. Records baseline state in a dedicated test-only fixture (mailbox /
     Canvas user) — separate from any task's runtime credentials, so
     this test can never disturb a running task.
  2. Pollutes that state with a known fingerprint.
  3. Calls the cleanup helper.
  4. Asserts the polluted state was removed.

Helpers exercised:
  - utils/app_specific/poste/ops.py:clear_folder  → must drop all
    messages in the named mailbox folder of a user we own.
  - utils/app_specific/canvas/preprocess_pipeline.py:cleanup_conversations
    → must drop all Canvas conversations visible to the calling user.

Fixtures:
  - Poste:  uses the dedicated probe admin account ``mcpposte_admin@mcp.com``
    (no task owns it).
  - Canvas: uses the dedicated probe admin account ``mcpcanvasadmin2@mcp.com``
    — admin2's token is shared between four canvas tasks for PREPROCESS-only
    admin work, NOT used by any agent at runtime, so polluting its
    conversation view here cannot affect any in-flight task's agent.
"""

from __future__ import annotations

import imaplib
import smtplib
import sys
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


POSTE_USER = "mcpposte_admin@mcp.com"
POSTE_PASS = "mcpposte"
POSTE_SMTP_PORT = 2525
POSTE_IMAP_PORT = 1143

CANVAS_HOST = "http://localhost:10001"
CANVAS_ADMIN_TOKEN = "mcpcanvasadmintoken2"  # preprocess-only, never an agent's runtime token


# ── Poste clear_folder ─────────────────────────────────────────────

def _imap_login() -> imaplib.IMAP4:
    imap = imaplib.IMAP4(host="localhost", port=POSTE_IMAP_PORT)
    typ, _ = imap.login(POSTE_USER, POSTE_PASS)
    if typ != "OK":
        raise RuntimeError(f"IMAP LOGIN returned {typ}")
    return imap


def _send_self_mail(subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = POSTE_USER
    msg["To"] = POSTE_USER
    msg["Subject"] = subject
    msg.set_content("preprocess hygiene test")
    s = smtplib.SMTP(host="localhost", port=POSTE_SMTP_PORT, timeout=8.0)
    try:
        s.ehlo()
        s.login(POSTE_USER, POSTE_PASS)
        s.send_message(msg)
    finally:
        try: s.quit()
        except Exception: pass


def _count_inbox() -> int:
    imap = _imap_login()
    try:
        typ, data = imap.select("INBOX")
        if typ != "OK":
            raise RuntimeError(f"IMAP SELECT returned {typ}")
        return int(data[0])
    finally:
        try: imap.logout()
        except Exception: pass


def check_clear_folder_drops_messages() -> Tuple[str, bool, str]:
    """Pollute inbox with N marker emails, run clear_folder, verify 0 remain."""
    try:
        from utils.app_specific.poste.ops import clear_folder  # type: ignore
    except Exception as e:
        return "clear_folder_drops", False, f"import failed: {e!r}"

    baseline = _count_inbox()
    nonce = uuid.uuid4().hex[:8]
    n_pollute = 3
    for i in range(n_pollute):
        _send_self_mail(f"hygiene_test_{nonce}_{i}")

    # Give LDA a moment
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _count_inbox() >= baseline + n_pollute:
            break
        time.sleep(0.4)

    polluted = _count_inbox()
    if polluted < baseline + n_pollute:
        return "clear_folder_drops", False, (
            f"polluted count {polluted} less than baseline {baseline} + {n_pollute} "
            "— LDA did not deliver in 10s"
        )

    config = {
        "email": POSTE_USER,
        "password": POSTE_PASS,
        "imap_server": "localhost",
        "imap_port": POSTE_IMAP_PORT,
        "use_ssl": False,
        "use_starttls": False,
    }
    try:
        clear_folder("INBOX", config)
    except Exception as e:
        return "clear_folder_drops", False, f"clear_folder raised: {e!r}"

    after = _count_inbox()
    if after != 0:
        return "clear_folder_drops", False, (
            f"inbox still has {after} messages after clear_folder "
            f"(baseline before pollute={baseline}, after pollute={polluted})"
        )
    return "clear_folder_drops", True, (
        f"polluted {baseline}→{polluted} ({n_pollute} added), cleared to 0"
    )


# ── Canvas cleanup_conversations ───────────────────────────────────

def _canvas_self_id() -> int:
    import httpx
    with httpx.Client(timeout=8.0) as c:
        r = c.get(
            f"{CANVAS_HOST}/api/v1/users/self",
            headers={"Authorization": f"Bearer {CANVAS_ADMIN_TOKEN}"},
        )
    r.raise_for_status()
    return int(r.json()["id"])


def _create_self_conversation(subject: str) -> int:
    """Send a Canvas conversation from admin2 to admin2 (self).  Returns
    the conversation id Canvas assigned."""
    import httpx
    self_id = _canvas_self_id()
    with httpx.Client(timeout=8.0) as c:
        r = c.post(
            f"{CANVAS_HOST}/api/v1/conversations",
            headers={"Authorization": f"Bearer {CANVAS_ADMIN_TOKEN}"},
            data={
                "recipients[]": str(self_id),
                "subject": subject,
                "body": "preprocess hygiene test conversation",
                "group_conversation": "false",
            },
        )
    r.raise_for_status()
    out = r.json()
    # Canvas returns an array of conversations
    if isinstance(out, list) and out:
        return int(out[0]["id"])
    if isinstance(out, dict):
        return int(out.get("id", -1))
    return -1


def _count_conversations() -> int:
    import httpx
    with httpx.Client(timeout=8.0) as c:
        total = 0
        for scope in ("inbox", "sent", "archived"):
            r = c.get(
                f"{CANVAS_HOST}/api/v1/conversations",
                headers={"Authorization": f"Bearer {CANVAS_ADMIN_TOKEN}"},
                params={"scope": scope, "per_page": 100},
            )
            r.raise_for_status()
            d = r.json()
            if isinstance(d, list):
                total += len(d)
    return total


def check_cleanup_conversations_drops_all() -> Tuple[str, bool, str]:
    """Send a marker conversation to ourselves, call cleanup_conversations,
    verify it was deleted from the inbox/sent/archived views."""
    try:
        from utils.app_specific.canvas.preprocess_pipeline import CanvasPreprocessUtils  # type: ignore
        from utils.app_specific.canvas.api_client import CanvasAPI  # type: ignore
    except Exception as e:
        return "cleanup_conversations_drops", False, f"import failed: {e!r}"

    baseline = _count_conversations()
    nonce = uuid.uuid4().hex[:8]
    try:
        _create_self_conversation(f"hygiene_test_{nonce}")
    except Exception as e:
        return "cleanup_conversations_drops", False, f"could not create conversation: {e!r}"

    # Give Canvas a moment for the conversation to materialize
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _count_conversations() > baseline:
            break
        time.sleep(0.3)
    polluted = _count_conversations()
    if polluted <= baseline:
        return "cleanup_conversations_drops", False, (
            f"conversation never materialized (baseline={baseline}, polluted={polluted})"
        )

    api = CanvasAPI(base_url=CANVAS_HOST, access_token=CANVAS_ADMIN_TOKEN)
    utils = CanvasPreprocessUtils(api)
    try:
        deleted = utils.cleanup_conversations()
    except Exception as e:
        return "cleanup_conversations_drops", False, f"cleanup_conversations raised: {e!r}"

    after = _count_conversations()
    if after != 0:
        return "cleanup_conversations_drops", False, (
            f"after cleanup_conversations, view still has {after} conversations "
            f"(baseline={baseline}, polluted={polluted}, deleted_count={deleted})"
        )
    return "cleanup_conversations_drops", True, (
        f"polluted {baseline}→{polluted} (+1 marker), cleared to 0 (deleted={deleted})"
    )


CHECKS = [
    check_clear_folder_drops_messages,
    check_cleanup_conversations_drops_all,
]


def main() -> int:
    print("=" * 72)
    print(f"  CLEANUP-HELPER E2E TESTS  ({len(CHECKS)} checks)")
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
        print(f"  {marker} {name:<32} ({dt:5.2f}s)  {detail}")
        if not ok:
            fails += 1
    print("-" * 72)
    print(f"  {len(CHECKS) - fails}/{len(CHECKS)} passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
