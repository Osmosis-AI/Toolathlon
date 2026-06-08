"""
Git operations for GitHub repositories.
"""
import os
import re
import shutil
import asyncio
from tenacity import (
    retry,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
    retry_if_exception_type,
)
from requests.exceptions import RequestException, Timeout, ConnectionError
from utils.general.helper import run_command

from .api import (
    GitHubRateLimitError,
    _wait_for_github,
    RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS,
)


# ── Rate-limit detection from git subprocess output ─────────────────
#
# Git-over-HTTPS to github.com has its own rate-limit / soft-throttle
# behaviour separate from the REST API.  Per GitHub's community
# discussion (https://github.com/orgs/community/discussions/44515) and
# observed behaviour, exhaustion typically manifests as one of:
#
#   - "remote: error: 429 Too Many Requests"
#   - "fatal: unable to access '...': The requested URL returned error: 429"
#   - "remote: API rate limit exceeded"
#   - "fatal: unable to access '...': The requested URL returned error: 503"
#     (treat 503 as transient + rate-limit-shaped)
#   - "secondary rate limit" / "abuse detection"
#
# Unlike REST, subprocess output has no Retry-After header.  We use a
# conservative 60s default wait — the @git_retry_async decorator will
# escalate per ``_wait_for_github``'s secondary-limit logic on repeat.
_GIT_RATE_LIMIT_PATTERNS = [
    r"\b429\b\s*(?:too\s*many|rate)",
    r"too\s*many\s*requests",
    r"\brate\s*limit\b",
    r"\bsecondary\s*rate\s*limit\b",
    r"abuse\s*detection",
    r"requested\s*url\s*returned\s*error:\s*429",
    r"requested\s*url\s*returned\s*error:\s*503",
]
_GIT_RATE_LIMIT_RE = re.compile("|".join(_GIT_RATE_LIMIT_PATTERNS), re.IGNORECASE)


def _stderr_indicates_rate_limit(stderr: str) -> bool:
    """Heuristic: does this git subprocess stderr look like a GitHub
    rate-limit response?  Conservative — false positives just cause
    one extra 60s retry which is harmless; false negatives mean we
    propagate the original error (acceptable, same as today).
    """
    return bool(stderr and _GIT_RATE_LIMIT_RE.search(stderr))


async def _run_git_capturing(cmd: str) -> None:
    """Run a git command via ``run_command`` and convert non-zero exits
    into typed exceptions:

      * Rate-limit shaped stderr → ``GitHubRateLimitError`` (caught and
        retried-with-wait by ``git_retry_async``)
      * Anything else → ``RuntimeError`` (not retried)

    This replaces the pre-existing pattern where ``run_command``'s rc
    was discarded entirely, making the surrounding retry decorator a
    no-op even when the git command failed.
    """
    stdout, stderr, rc = await run_command(cmd, debug=False, show_output=False)
    if rc == 0:
        return

    stderr = stderr or ""
    stdout = stdout or ""
    combined = f"{stderr}\n{stdout}".strip()

    # Always log the FULL output to the server log so failures are
    # diagnosable later — the truncated exception message in v3's
    # cleanup_execution log otherwise hides the actual error line,
    # which for ``git push --mirror`` usually comes AFTER a long list
    # of successful ``[new branch]`` updates and gets clipped.
    print(
        f"[git_ops] command FAILED rc={rc}: {cmd}\n"
        f"--- stderr ({len(stderr)} chars) ---\n{stderr}\n"
        f"--- stdout ({len(stdout)} chars) ---\n{stdout}\n"
        f"--- end ---",
        flush=True,
    )

    if _stderr_indicates_rate_limit(combined):
        # No header to parse → conservative 60s base.  ``_wait_for_github``
        # will escalate on repeated hits.  Include the TAIL of stderr in
        # the exception message — for git push that's where the actual
        # failure line is (after the list of successful refs).
        raise GitHubRateLimitError(
            wait_seconds=60.0,
            scope="secondary",
            status_code=429,
            message=f"git command rate-limited (rc={rc}): ...{combined[-400:]}",
        )

    # Non-rate-limit failure.  Preserve the TAIL of the output (head is
    # usually success noise like ``* [new branch] foo -> foo`` lines;
    # the real error is at the end).  Keep a small head too so we can
    # see what command produced this.
    head = combined[:200]
    tail = combined[-800:] if len(combined) > 200 else ""
    sep = "\n  ...truncated...\n" if tail else ""
    raise RuntimeError(
        f"git command failed (rc={rc}): {cmd}\n{head}{sep}{tail}"
    )


# Async retry decorator for git subprocess operations.
#
# Catches:
#   - GitHubRateLimitError (from ``_run_git_capturing`` when stderr
#     looks rate-limited) — wait per ``_wait_for_github`` (60s, then
#     escalating per secondary-limit rule, capped at 15 min)
#   - Other RequestException / Timeout / ConnectionError — fall back
#     exponential
#
# Bounded by attempt count AND total delay so a watchdog can't deadlock.
git_retry_async = retry(
    stop=(stop_after_attempt(6) | stop_after_delay(RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS)),
    wait=_wait_for_github,
    retry=retry_if_exception_type((GitHubRateLimitError, RequestException, Timeout, ConnectionError)),
    reraise=True,
)


def git_auth_url(token: str, full_name: str) -> str:
    """Generate authenticated Git URL."""
    return f"https://x-access-token:{token}@github.com/{full_name}.git"


async def _prune_hidden_refs(local_dir: str) -> None:
    """Delete server-managed refs from a mirror clone that GitHub
    refuses to accept on push.

    ``git clone --mirror`` fetches EVERY ref including those that
    GitHub considers hidden / read-only on its side:
      * ``refs/pull/*``  — open and merged pull-request refs (head/merge)
      * ``refs/changes/*`` — Gerrit-style review refs (rare but seen)

    When ``git push --mirror`` then tries to write these back to a
    DIFFERENT repository, GitHub rejects each with:
        ! [remote rejected] refs/pull/1/head -> refs/pull/1/head
                                  (deny updating a hidden ref)
    which makes the overall push exit non-zero even when every branch
    and tag pushed cleanly.  Stripping these refs from the local
    mirror BEFORE the push avoids the rejection entirely.
    """
    for prefix in ("refs/pull/", "refs/changes/"):
        # for-each-ref lists matching refs; update-ref -d deletes each
        cmd = (
            f"bash -c \"git -C {local_dir} for-each-ref --format='%(refname)' "
            f"{prefix} | xargs -r -I {{}} git -C {local_dir} update-ref -d {{}}\""
        )
        stdout, stderr, rc = await run_command(cmd, debug=False, show_output=False)
        # Best-effort: a clone without any matching refs returns rc=0
        # with no output; a real failure here would be unusual, log
        # and continue so the push still gets a chance.
        if rc != 0:
            print(
                f"[git_ops] _prune_hidden_refs: non-fatal warning for prefix "
                f"{prefix} in {local_dir} (rc={rc}): {(stderr or '')[:200]}",
                flush=True,
            )


@git_retry_async
async def git_mirror_clone(token: str, full_name: str, local_dir: str) -> None:
    """Clone a repository as a mirror, then strip GitHub-managed
    hidden refs (``refs/pull/*``, ``refs/changes/*``) so a subsequent
    ``git push --mirror`` doesn't get rejected by GitHub for trying
    to update refs it owns.  See ``_prune_hidden_refs`` for details.
    """
    src_url = git_auth_url(token, full_name)
    if os.path.exists(local_dir):
        shutil.rmtree(local_dir)
    cmd = f"git clone --mirror {src_url} {local_dir}"
    await _run_git_capturing(cmd)
    await _prune_hidden_refs(local_dir)


@git_retry_async
async def git_mirror_push(token: str, local_dir: str, dst_full_name: str) -> None:
    """Push a mirror to a destination repository."""
    dst_url = git_auth_url(token, dst_full_name)
    cmd = f"git -C {local_dir} push --mirror {dst_url}"
    await _run_git_capturing(cmd)
