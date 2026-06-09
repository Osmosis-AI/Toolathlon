"""
Core GitHub API operations for common tasks.
"""
import os
import threading
import time
import requests
from typing import Dict, Any, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
    retry_if_exception_type,
    AsyncRetrying,
)
from requests.exceptions import RequestException, Timeout, ConnectionError


GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 500  # Default timeout for all HTTP requests in seconds


# ── Mutation throttle ───────────────────────────────────────────────
#
# GitHub's documented best practices for staying under secondary rate
# limits (https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api):
#
#   "If you are making a large number of POST, PATCH, PUT, or DELETE
#    requests, wait at least one second between each request."
#
# Single tasks routinely chain 5-7 mutating helper calls in their
# preprocess (delete_repo → create_repo → enable_issues → create_issue →
# update_file → ...).  Without spacing, that's a 100ms burst — well
# above GitHub's "rapid succession" abuse detection threshold even when
# the per-minute average is fine.  Enforcing a 1s minimum gap between
# mutations within a single python process spreads those bursts out and
# matches GitHub's explicit guidance.
#
# Per-process (not cross-container) — same Python interpreter shares
# the timestamp, separate preprocess/eval invocations reset cleanly.
# Read-only operations (GET/HEAD/OPTIONS) are NOT throttled — they
# don't trigger the abuse heuristics.
MUTATION_MIN_GAP_SECONDS = 1.0

_mutation_lock = threading.Lock()
_last_mutation_ts: float = 0.0


def _throttle_mutation() -> None:
    """Sleep just enough to keep ≥``MUTATION_MIN_GAP_SECONDS`` since the
    previous mutation in this process.  Called inside each mutating
    helper, BEFORE the actual HTTP request.

    Cheap and safe to call when there's been no recent mutation (no
    sleep when the gap is already exceeded).
    """
    global _last_mutation_ts
    with _mutation_lock:
        now = time.time()
        elapsed = now - _last_mutation_ts
        gap = MUTATION_MIN_GAP_SECONDS - elapsed
        if gap > 0:
            time.sleep(gap)
        _last_mutation_ts = time.time()


# ── Rate-limit handling ──────────────────────────────────────────────
#
# GitHub returns rate-limit exhaustion as an HTTP response, not a
# transport-level exception:
#   - Primary rate limit  → 403 with ``X-RateLimit-Remaining: 0`` and an
#     ``X-RateLimit-Reset`` epoch second header.  Resets on the hour;
#     worst-case wait is ~60 min for the next bucket.
#   - Secondary (abuse) rate limit → 429 with a ``Retry-After`` header
#     in seconds.  Resets on the order of minutes.
#
# Without explicit handling these were getting wrapped in
# ``RuntimeError`` which the retry filter ignored, so graders /
# preprocesses that hit the limit failed immediately on the first 403.
# Now we raise a typed exception that the retry decorator catches and
# waits according to GitHub's headers, bounded to a sensible upper
# bound so we don't deadlock the per-task watchdog.

# Cap on total wait across all rate-limit retries for a single helper
# call.  15 min is well under the 30 min preprocess timeout used by v3
# and is enough to ride out most abuse-detection windows.  If a primary
# rate-limit reset takes longer than this, we'd rather fail fast and
# let the operator notice than silently block the whole eval.
RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS = 15 * 60


class GitHubRateLimitError(RequestException):
    """Raised when GitHub returns a rate-limit response.

    Inherits from ``RequestException`` so the existing retry filter
    catches it as a transient transport-class error.  Carries the
    parsed reset window so the wait callable can compute the right
    sleep instead of blind exponential backoff.
    """

    def __init__(
        self,
        wait_seconds: float,
        scope: str,
        status_code: int,
        message: str,
    ):
        self.wait_seconds = wait_seconds
        self.scope = scope        # "primary" | "secondary"
        self.status_code = status_code
        super().__init__(message)


class GitHubQuotaExhaustedError(RuntimeError):
    """Rate-limit reset window exceeds our retry budget.

    When GitHub's ``X-RateLimit-Reset`` is N minutes away and N >
    ``RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS / 60``, the retry decorator
    cannot recover within its delay budget — it would either waste
    15 min of wall time blocking on a sleep that's still going to
    fail, or give up immediately when ``stop_after_delay`` checks
    after the wait.  Either way the call is doomed.

    This exception is RAISED INSTEAD of ``GitHubRateLimitError`` in
    that doomed case, and is NOT a ``RequestException`` — so the
    retry filter ignores it and it propagates immediately to the
    caller.  Preprocess fails fast with a clear "GitHub quota is
    exhausted for the next N minutes" message, freeing the slot
    instead of holding it during a futile wait.

    Note: the message is intentionally non-cryptic so operators can
    distinguish "GitHub-side quota issue, retry the task later" from
    "real bug, look at the trace".
    """

    def __init__(self, wait_seconds: float, status_code: int):
        self.wait_seconds = wait_seconds
        self.status_code = status_code
        mins = wait_seconds / 60.0
        super().__init__(
            f"GitHub primary rate limit exhausted; reset is {wait_seconds:.0f}s "
            f"({mins:.1f} min) away, beyond our {RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS//60} min "
            f"retry cap.  Failing fast — retry this task after the reset."
        )


def _body_indicates_rate_limit(response: requests.Response) -> bool:
    """Fallback heuristic — some rate-limit responses arrive without
    the standard headers (e.g. GitHub Enterprise, certain search
    endpoints, or org-specific custom policies).  In those cases the
    body usually contains a recognisable phrase.

    Safe enough to use as a second-line check: anything matching here
    is almost certainly a rate limit, and the consequence of a false
    positive is just a 60s retry — far better than failing outright.
    """
    try:
        body = (response.text or "").lower()
    except Exception:
        return False
    return any(s in body for s in (
        "rate limit",
        "abuse detection",
        "secondary rate limit",
        "too many requests",
    ))


def _parse_rate_limit_wait(response: requests.Response) -> Optional[GitHubRateLimitError]:
    """If ``response`` is a rate-limit failure, return a
    ``GitHubRateLimitError`` describing how long to wait.  Returns
    ``None`` for any other response (including normal 4xx / 5xx that
    aren't rate-limit related).

    Detection order matches GitHub's documented header precedence:
      1. ``retry-after`` header (highest priority — present on either
         403 or 429 for secondary limits)
      2. ``x-ratelimit-remaining: 0`` + ``x-ratelimit-reset`` (primary)
      3. Body-text fallback when headers are missing entirely
    """
    sc = response.status_code

    # Only 403 and 429 carry rate-limit semantics (per GitHub docs).
    # Plain 4xx/5xx other than these go through the normal RuntimeError
    # path so the caller sees the real error.
    if sc not in (403, 429):
        return None

    # 1. ``Retry-After`` header — highest priority per docs.
    #    Can appear on either 429 (secondary) OR 403 (secondary on the
    #    abuse-detection path), so check unconditionally first.
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            wait_s = float(retry_after)
        except ValueError:
            wait_s = 60.0
        return GitHubRateLimitError(
            wait_seconds=wait_s,
            scope="secondary",
            status_code=sc,
            message=f"GitHub secondary rate limit ({sc}), Retry-After={retry_after}s",
        )

    # 2. ``X-RateLimit-Remaining: 0`` — primary rate limit.
    if response.headers.get("X-RateLimit-Remaining") == "0":
        reset = response.headers.get("X-RateLimit-Reset")
        try:
            reset_at = float(reset) if reset is not None else (time.time() + 60.0)
        except ValueError:
            reset_at = time.time() + 60.0
        # +5s grace so we're past the reset boundary, not exactly on it.
        wait_s = max(reset_at - time.time() + 5.0, 5.0)
        resource = response.headers.get("X-RateLimit-Resource", "core")
        return GitHubRateLimitError(
            wait_seconds=wait_s,
            scope="primary",
            status_code=sc,
            message=(
                f"GitHub primary rate limit ({sc}, resource={resource}); "
                f"reset in {wait_s:.0f}s"
            ),
        )

    # 3. Body-text fallback.  Per docs, "Otherwise, wait for at least
    #    one minute before retrying."
    if _body_indicates_rate_limit(response):
        return GitHubRateLimitError(
            wait_seconds=60.0,
            scope="secondary",
            status_code=sc,
            message=f"GitHub rate limit ({sc}) detected via body text (no headers)",
        )

    # 4. Status 429 with NO indicators whatsoever still means rate
    #    limit (per docs: "Otherwise, wait for at least one minute
    #    before retrying").  Status 403 without ANY indicator we
    #    classify as a real permission error — too ambiguous to retry.
    if sc == 429:
        return GitHubRateLimitError(
            wait_seconds=60.0,
            scope="secondary",
            status_code=sc,
            message="GitHub rate limit (429) without indicator headers — default 60s wait",
        )

    return None


def _check_rate_limit(response: requests.Response) -> None:
    """Inspect ``response`` and raise the right typed exception when
    it's a GitHub rate-limit failure.  Three cases:

      1. Not a rate limit → return, caller proceeds with normal flow.
      2. Rate limited, reset window ≤ our retry cap → raise
         ``GitHubRateLimitError`` (a ``RequestException``) so the
         retry decorator catches it and waits per ``_wait_for_github``.
      3. Rate limited, reset window > our retry cap → raise
         ``GitHubQuotaExhaustedError`` (a ``RuntimeError``) which is
         NOT a ``RequestException`` and therefore PROPAGATES
         IMMEDIATELY past the retry filter.  Saves 15 min of pointless
         waiting on a doomed call when the bucket isn't refilling
         until well outside our budget.

    Call this BEFORE any generic ``raise RuntimeError(...)`` so we
    keep the typed header context the retry decorator needs.
    """
    err = _parse_rate_limit_wait(response)
    if err is None:
        return
    # Primary rate limit with a reset window we can't ride out — fail
    # fast.  ``scope == "secondary"`` (typically Retry-After ≤ a few
    # minutes) is excluded from this short-circuit because secondary
    # waits are short enough to fit our budget after the multiplier.
    if err.scope == "primary" and err.wait_seconds > RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS:
        raise GitHubQuotaExhaustedError(
            wait_seconds=err.wait_seconds,
            status_code=err.status_code,
        )
    raise err


def _wait_for_github(retry_state) -> float:
    """tenacity wait callable.

    If the last exception is a ``GitHubRateLimitError``:
      * Primary limits: use the parsed reset window as-is (waiting
        longer than the reset doesn't help — the bucket refills at a
        fixed time).
      * Secondary limits: per GitHub docs, "wait for an exponentially
        increasing amount of time between retries."  Apply
        ``2^(attempt-1)`` multiplier to the response's Retry-After,
        capped at 4× (so a 60s Retry-After grows 60 → 120 → 240).
        This is what the docs explicitly recommend and prevents a
        client that's already over the limit from immediately
        re-triggering after the first short wait.

    Either way, the final wait is bounded by
    ``RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS`` so a watchdog can't deadlock.

    For non-rate-limit transient errors, fall back to short exponential.
    """
    out = retry_state.outcome
    if out is not None and out.failed:
        exc = out.exception()
        if isinstance(exc, GitHubRateLimitError):
            base = exc.wait_seconds
            if exc.scope == "secondary":
                # ``attempt_number`` is the attempt that just failed
                # (1-indexed).  First failure → multiplier=1, second →
                # 2, third → 4 ... capped at 4× to stay sane.
                multiplier = min(2 ** (max(retry_state.attempt_number - 1, 0)), 4)
                base = base * multiplier
            return min(base, RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS)
    # Fall back to 2s/4s/8s capped at 10s for transient transport errors
    return wait_exponential(multiplier=2, min=2, max=10)(retry_state)


# Retry decorator for synchronous functions.
#
# Catches:
#   - Transport errors (RequestException, Timeout, ConnectionError) —
#     short exponential backoff (2s, 4s, 8s, capped at 10s).
#   - GitHubRateLimitError (also a RequestException) — wait per
#     GitHub's reset headers, capped at 15 min total.
#
# Stop condition is whichever fires first:
#   - 6 attempts (was 3; needed more to absorb a single full reset
#     window plus a few short transport retries on either side)
#   - 15 min total elapsed (matches the per-call wait cap so we
#     never run more than one full primary-limit reset wait)
github_retry = retry(
    stop=(stop_after_attempt(6) | stop_after_delay(RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS)),
    wait=_wait_for_github,
    retry=retry_if_exception_type((RequestException, Timeout, ConnectionError)),
    reraise=True,
)

# Async retry configuration for async functions.  Same shape.
github_retry_async = AsyncRetrying(
    stop=(stop_after_attempt(6) | stop_after_delay(RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS)),
    wait=_wait_for_github,
    retry=retry_if_exception_type((RequestException, Timeout, ConnectionError)),
    reraise=True,
)


def github_headers(token: str) -> Dict[str, str]:
    """Generate standard GitHub API headers."""
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


@github_retry
def github_get_repo(token: str, owner: str, repo_name: str) -> Dict[str, Any]:
    """Get a repository."""
    url = f"{GITHUB_API}/repos/{owner}/{repo_name}"
    r = requests.get(url, headers=github_headers(token), timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch repo {owner}/{repo_name}: {r.status_code} {r.text}")
    return r.json()

@github_retry
def github_get_login(token: str) -> str:
    """Get the authenticated user's login name."""
    url = f"{GITHUB_API}/user"
    r = requests.get(url, headers=github_headers(token), timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch GitHub user: {r.status_code} {r.text}")
    return r.json().get("login")


@github_retry
def github_delete_repo(token: str, owner: str, repo_name: str,enable_not_found:bool=True) -> None:
    """Delete a GitHub repository."""
    _throttle_mutation()
    url = f"{GITHUB_API}/repos/{owner}/{repo_name}"
    r = requests.delete(url, headers=github_headers(token), timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code not in (204,):
        if enable_not_found and r.status_code == 404:
            return
        raise RuntimeError(f"Failed to delete repo {owner}/{repo_name}: {r.status_code} {r.text}")


@github_retry
def github_create_user_repo(token: str, name: str, private: bool = False) -> Dict[str, Any]:
    """Create a new repository under the authenticated user's account."""
    _throttle_mutation()
    url = f"{GITHUB_API}/user/repos"
    payload = {
        "name": name,
        "private": private,
        "has_issues": True,
        "auto_init": False,
    }
    r = requests.post(url, headers=github_headers(token), json=payload, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code not in (201,):
        raise RuntimeError(f"Failed to create repo {name}: {r.status_code} {r.text}")
    return r.json()


@github_retry
def github_enable_issues(token: str, full_name: str) -> None:
    """Enable issues for a repository."""
    _throttle_mutation()
    url = f"{GITHUB_API}/repos/{full_name}"
    payload = {"has_issues": True}
    r = requests.patch(url, headers=github_headers(token), json=payload, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code not in (200,):
        raise RuntimeError(f"Failed to enable issues: {r.status_code} {r.text}")


@github_retry
def github_create_issue(token: str, full_name: str, title: str, body: str) -> Dict[str, Any]:
    """Create an issue in a repository."""
    # First ensure issues are enabled
    github_enable_issues(token, full_name)

    _throttle_mutation()
    url = f"{GITHUB_API}/repos/{full_name}/issues"
    payload = {"title": title, "body": body}
    r = requests.post(url, headers=github_headers(token), json=payload, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code not in (201,):
        raise RuntimeError(f"Failed to create issue: {r.status_code} {r.text}")
    return r.json()


@github_retry
def github_get_repo_info(token: str, full_name: str) -> Dict[str, Any]:
    """Get repository information."""
    url = f"{GITHUB_API}/repos/{full_name}"
    r = requests.get(url, headers=github_headers(token), timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch repo info {full_name}: {r.status_code} {r.text}")
    return r.json()


@github_retry
def github_get_latest_commit(token: str, full_name: str) -> str:
    """Get the latest commit SHA for a repository."""
    url = f"{GITHUB_API}/repos/{full_name}/commits"
    r = requests.get(url, headers=github_headers(token), params={"per_page": 1}, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch commits: {r.status_code} {r.text}")
    commits = r.json()
    if not commits:
        raise RuntimeError(f"No commits found in {full_name}")
    return commits[0]["sha"]


@github_retry
def github_get_issue(token: str, full_name: str, issue_number: int) -> Dict[str, Any]:
    """Get issue information."""
    url = f"{GITHUB_API}/repos/{full_name}/issues/{issue_number}"
    r = requests.get(url, headers=github_headers(token), timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch issue: {r.status_code} {r.text}")
    return r.json()


@github_retry
def github_get_issue_comments(token: str, full_name: str, issue_number: int) -> list:
    """Get all comments for an issue."""
    url = f"{GITHUB_API}/repos/{full_name}/issues/{issue_number}/comments"
    r = requests.get(url, headers=github_headers(token), params={"per_page": 100}, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch comments: {r.status_code} {r.text}")
    return r.json()
