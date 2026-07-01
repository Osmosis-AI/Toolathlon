"""
GitHub admission gate — per-token cap on github-heavy task starts.

Why
───
GitHub abuse-detection has been silently throttling our PAT(s) on this
host: `tooldev22` was downgraded from the documented 5000/h to the
unauthenticated 60/h, and three sibling accounts were outright
suspended.  The pattern that triggers this is bursty content-mutation
load (create_repo / create_issue / update_file_content) across many
concurrent task starts.

This module enforces three caps PER TOKEN-LOGIN, at the v3 ``/start``
admission boundary, so we never push GitHub harder than the policy
allows — protecting the remaining healthy token from the same fate.

Caps (single policy applied to every token-login for v0):
  - ``MAX_CONCURRENT_HEAVY_TASKS = 5``
        At most 5 github-heavy execs live (admitted, not yet
        cleaned-up) at any moment per token.
  - ``MAX_HEAVY_TASKS_PER_HOUR = 40``
        At most 40 github-heavy admissions in any rolling 60-min
        window per token.
  - ``REQUIRE_QUOTA_REMAINING = 50``
        Refuse if ``/rate_limit.core.remaining < 50`` for this token.
        Catches the case where some out-of-band caller has burned the
        quota independent of our admissions.

Cross-instance coordination
───────────────────────────
All v3 instances on the same host share state via a single JSON file
guarded by ``fcntl.flock``.  This is the same pattern used for the
Notion OAuth refresh window (commit d2ab39d5), so the pattern is
proven on this host.

State file layout
─────────────────
::

    {
      "tokens": {
        "<login>": {
          "concurrent": {"exec_abc": 1780949000.0, ...},   # exec_id -> start_ts
          "recent_starts": [1780949000.0, ...]              # all admits ≤1h old
        }
      },
      "quota_cache": {
        "<sha256(token)[:16]>": {
          "remaining": 4500, "reset_at": 1780952600, "checked_at": 1780949000
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests


# ── Config ─────────────────────────────────────────────────────────

# Path where the per-host shared admission state lives.  /tmp survives
# v3 process restarts but not host reboots — fine because the state is
# strictly bounded by the 1h window anyway.
STATE_FILE = Path(os.environ.get(
    "TOOLATHLON_GITHUB_ADMISSION_STATE",
    "/tmp/toolathlon-github-admissions.json",
))

# Policy caps — v0 single tier for all tokens.  Per the per-token
# rationale at the top, callers should treat these as TOKEN-scoped.
MAX_CONCURRENT_HEAVY_TASKS = 5
MAX_HEAVY_TASKS_PER_HOUR = 40
# Lower than the typical-task call count (~10) so a token at 60/h
# still gets a few admissions per reset window before refusing.
REQUIRE_QUOTA_REMAINING = 15

# Rolling window for "recent_starts" pruning + counting.
WINDOW_SECONDS = 3600.0

# How long an entry can sit in ``concurrent`` before we assume it's a
# stale registration the deregister hook missed.  An execution's hard
# lifetime cap in v3 is ~90 min; we trust the dedicated deregister
# path for normal cleanup and use this only as a safety net.
STALE_CONCURRENT_SECONDS = 90.0 * 60.0

# Quota probe caching — /rate_limit doesn't cost a core call, but we
# don't want to hammer it on every admission either.
QUOTA_CACHE_TTL_SECONDS = 60.0

# Master kill-switch — set this env var to disable the gate entirely
# (useful for debugging or temporarily bypassing).
DISABLE_ENV_VAR = "TOOLATHLON_GITHUB_ADMISSION_DISABLED"


# ── Static list of github-heavy tasks ──────────────────────────────
#
# Tasks whose preprocess+runtime hit GitHub's REST API heavily enough
# to be worth gating.  Identified from a grep over
# ``tasks/finalpool/*/preprocess/main.py`` for github-helper imports
# plus a manual review of agent task descriptions for github-MCP use.
#
# Note: ``k8s-pr-preview-testing`` is NOT on the heavy list because
# its github calls happen only in the eval phase, not preprocess, and
# eval runs after the agent finishes (admission already past).
GITHUB_HEAVY_TASKS = frozenset({
    "sync-todo-to-readme",
    "personal-website-construct",
    "email-paper-homepage",
    "git-milestone",
    "task-tracker",
    "dataset-license-issue",
})


# ── Refusal type ───────────────────────────────────────────────────


@dataclass
class AdmissionRefusal:
    """Returned from ``check_and_register_admission`` when the gate
    refuses a task start.  Caller (router) converts this into the
    standard v3 ``503 + retry_after_s`` HTTP response."""
    reason: str
    retry_after_s: int
    detail: Dict[str, object]


# ── State persistence ─────────────────────────────────────────────


def _empty_state() -> dict:
    return {"tokens": {}, "quota_cache": {}}


@contextmanager
def _locked_state_file():
    """Open the shared state file under an exclusive ``flock``.  Yields
    a tuple ``(state_dict, file_handle)``; caller must mutate state
    and call ``_save_state(state, fh)`` while still inside the
    context.  File creation if absent.

    ``flock`` is per-process, so this handles multi-instance v3
    coordination on the same host.  Within a single v3 process
    multiple concurrent tasks queue on the kernel-level lock —
    serialised cleanly.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # ``a+`` creates if absent and lets us read+write without truncating.
    with open(STATE_FILE, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            if not raw.strip():
                state = _empty_state()
            else:
                try:
                    state = json.loads(raw)
                    # Normalise old/missing fields
                    state.setdefault("tokens", {})
                    state.setdefault("quota_cache", {})
                except json.JSONDecodeError:
                    logging.warning(
                        "github_admission: state file corrupt, resetting"
                    )
                    state = _empty_state()
            yield state, fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _save_state(state: dict, fh) -> None:
    """Overwrite the state file with the current dict.  Must be called
    inside ``_locked_state_file``'s context (caller holds the flock)."""
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(state, indent=2))
    fh.flush()


# ── Token → login cache ────────────────────────────────────────────


_login_cache: Dict[str, Tuple[str, float]] = {}
_LOGIN_CACHE_TTL = 600.0  # 10 min — login is stable for the life of a PAT


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _resolve_login(token: str) -> Optional[str]:
    """Get the GitHub login for ``token``, cached per-process.  Returns
    None if the token isn't a valid PAT (401/suspended) — caller
    should treat that as "no admission needed" and let downstream
    code surface the auth failure.
    """
    fp = _token_fingerprint(token)
    now = time.time()
    cached = _login_cache.get(fp)
    if cached and now - cached[1] < _LOGIN_CACHE_TTL:
        return cached[0]
    try:
        r = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=8,
        )
    except Exception as e:
        logging.warning(f"github_admission: _resolve_login HTTP failed: {e!r}")
        return None
    if r.status_code != 200:
        return None
    login = r.json().get("login")
    if not login:
        return None
    _login_cache[fp] = (login, now)
    return login


# ── Quota probe with cache ─────────────────────────────────────────


def _probe_quota(token: str, state: dict) -> Optional[Tuple[int, float]]:
    """Return ``(remaining, reset_at)`` for the token's ``core``
    bucket, freshly probed if cache is stale.  Mutates
    ``state['quota_cache']`` in place.  Returns None on HTTP failure
    — caller treats that as "skip the quota check".
    """
    fp = _token_fingerprint(token)
    now = time.time()
    cache = state["quota_cache"].get(fp)
    if cache and now - cache.get("checked_at", 0) < QUOTA_CACHE_TTL_SECONDS:
        return (int(cache["remaining"]), float(cache["reset_at"]))

    try:
        r = requests.get(
            "https://api.github.com/rate_limit",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=8,
        )
    except Exception as e:
        logging.warning(f"github_admission: rate_limit HTTP failed: {e!r}")
        return None
    if r.status_code != 200:
        return None
    try:
        core = r.json()["resources"]["core"]
    except (KeyError, ValueError):
        return None
    state["quota_cache"][fp] = {
        "remaining": int(core["remaining"]),
        "reset_at": float(core["reset"]),
        "checked_at": now,
    }
    return (int(core["remaining"]), float(core["reset"]))


# ── Pruning helpers ────────────────────────────────────────────────


def _prune_token_entry(entry: dict, now: float) -> None:
    """Strip stale state from a per-login entry IN PLACE."""
    entry.setdefault("concurrent", {})
    entry.setdefault("recent_starts", [])
    # Drop recent_starts older than the rolling window
    entry["recent_starts"] = [
        ts for ts in entry["recent_starts"] if now - ts < WINDOW_SECONDS
    ]
    # Drop concurrent entries that look stale (caller-side deregister
    # hook missed, or instance crashed mid-task).  90 min == v3's hard
    # lifetime cap; anything still in concurrent past that is real
    # leakage we have to guess at.
    entry["concurrent"] = {
        eid: ts for eid, ts in entry["concurrent"].items()
        if now - ts < STALE_CONCURRENT_SECONDS
    }


# ── Public API ─────────────────────────────────────────────────────


def is_disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").lower() in ("1", "true", "yes")


def check_and_register_admission(
    task_id: str,
    exec_id: str,
    token: str,
) -> Optional[AdmissionRefusal]:
    """Top-level gate for v3 ``/start``.  Returns None to admit (and
    registers the exec for later deregistration), or an
    ``AdmissionRefusal`` to refuse.

    Safe to call unconditionally on every ``/start``: non-github-heavy
    tasks are a fast no-op (no file lock, no HTTP).
    """
    if task_id not in GITHUB_HEAVY_TASKS:
        return None
    if is_disabled():
        return None

    login = _resolve_login(token)
    if login is None:
        # Invalid token — let downstream code surface the failure
        # instead of silently refusing.  This keeps the gate honest:
        # we don't refuse based on assumed-bad tokens.
        return None

    now = time.time()
    with _locked_state_file() as (state, fh):
        entry = state["tokens"].setdefault(login, {})
        _prune_token_entry(entry, now)

        # Cap 1: concurrent
        if len(entry["concurrent"]) >= MAX_CONCURRENT_HEAVY_TASKS:
            return AdmissionRefusal(
                reason="github_concurrent_cap",
                retry_after_s=30,
                detail={
                    "login": login,
                    "current_concurrent": len(entry["concurrent"]),
                    "max_concurrent": MAX_CONCURRENT_HEAVY_TASKS,
                },
            )

        # Cap 2: per-hour
        if len(entry["recent_starts"]) >= MAX_HEAVY_TASKS_PER_HOUR:
            oldest = min(entry["recent_starts"])
            retry_after = max(int(WINDOW_SECONDS - (now - oldest)) + 1, 1)
            return AdmissionRefusal(
                reason="github_hourly_cap",
                retry_after_s=retry_after,
                detail={
                    "login": login,
                    "starts_in_last_hour": len(entry["recent_starts"]),
                    "max_per_hour": MAX_HEAVY_TASKS_PER_HOUR,
                },
            )

        # Cap 3: quota probe
        probe = _probe_quota(token, state)
        if probe is not None:
            remaining, reset_at = probe
            if remaining < REQUIRE_QUOTA_REMAINING:
                retry_after = max(int(reset_at - now) + 1, 1)
                return AdmissionRefusal(
                    reason="github_quota_low",
                    retry_after_s=retry_after,
                    detail={
                        "login": login,
                        "quota_remaining": remaining,
                        "quota_required": REQUIRE_QUOTA_REMAINING,
                        "reset_in_s": retry_after,
                    },
                )

        # Admit + register
        entry["concurrent"][exec_id] = now
        entry["recent_starts"].append(now)
        _save_state(state, fh)
    return None


def deregister_admission(exec_id: str, token: Optional[str] = None) -> None:
    """Remove ``exec_id`` from any token's concurrent set.  Called from
    every v3 cleanup path; idempotent.  ``token`` parameter is
    optional — if absent, we scan all tokens.  Quick (the file is
    small) and removes the need for callers to remember which token
    admitted which exec.
    """
    if is_disabled():
        return
    with _locked_state_file() as (state, fh):
        changed = False
        for login, entry in state["tokens"].items():
            if exec_id in entry.get("concurrent", {}):
                entry["concurrent"].pop(exec_id)
                changed = True
        if changed:
            _save_state(state, fh)


def sweep_own_login_concurrent(token: str) -> Tuple[Optional[str], int]:
    """Clear this instance's stale ``concurrent`` entries at startup.

    At process boot the in-memory execution registry is empty, so any
    entry present in the shared state under this instance's github
    login is a leak from a prior ungraceful termination (SIGKILL, OOM,
    systemd stop-timeout, kernel panic) — the normal cleanup path
    (cleanup_execution → deregister_admission) never fired.

    Safety invariant: each github PAT is used by exactly one v3
    instance host-wide (per-instance token isolation in
    configs/token_key_session).  If that invariant is ever violated,
    a startup here would clear live entries owned by another instance
    and could over-admit until the next tick.

    Returns ``(login, count_cleared)``.  ``login`` is None (and count
    is 0) if the token is missing / unresolvable / gate disabled.
    """
    if not token or is_disabled():
        return None, 0
    login = _resolve_login(token)
    if login is None:
        logging.warning(
            "github_admission.sweep_own_login_concurrent: token did not "
            "resolve to a login (auth failure?), skipping sweep"
        )
        return None, 0
    with _locked_state_file() as (state, fh):
        entry = state.get("tokens", {}).get(login)
        if not entry:
            return login, 0
        concurrent = entry.get("concurrent", {})
        n = len(concurrent)
        if n:
            entry["concurrent"] = {}
            _save_state(state, fh)
            logging.warning(
                f"github_admission: startup sweep cleared {n} stale "
                f"concurrent entries for login={login}"
            )
        return login, n
