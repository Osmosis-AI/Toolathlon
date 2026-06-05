"""
Core GitHub API operations for common tasks.
"""
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


def _parse_rate_limit_wait(response: requests.Response) -> Optional[GitHubRateLimitError]:
    """If ``response`` is a rate-limit failure, return a
    ``GitHubRateLimitError`` describing how long to wait.  Returns
    ``None`` for any other response (including normal 4xx / 5xx that
    aren't rate-limit related).
    """
    sc = response.status_code

    # Secondary rate limit (abuse detection) — 429 with ``Retry-After``.
    if sc == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait_s = float(retry_after) if retry_after is not None else 60.0
        except ValueError:
            wait_s = 60.0
        return GitHubRateLimitError(
            wait_seconds=wait_s,
            scope="secondary",
            status_code=sc,
            message=f"GitHub secondary rate limit (429), Retry-After={retry_after}s",
        )

    # Primary rate limit — 403 with ``X-RateLimit-Remaining: 0``.
    if sc == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
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
                f"GitHub primary rate limit (403, resource={resource}); "
                f"reset in {wait_s:.0f}s"
            ),
        )

    return None


def _check_rate_limit(response: requests.Response) -> None:
    """Raise ``GitHubRateLimitError`` if the response is a rate-limit
    failure; do nothing otherwise.  Call this BEFORE any
    ``raise RuntimeError(...)`` so we keep the typed header context
    that the retry decorator needs to schedule the right wait.
    """
    err = _parse_rate_limit_wait(response)
    if err is not None:
        raise err


def _wait_for_github(retry_state) -> float:
    """tenacity wait callable.

    If the last exception is a ``GitHubRateLimitError``, sleep for the
    parsed reset window (capped at ``RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS``).
    Otherwise fall back to short exponential backoff for transient
    transport errors.
    """
    out = retry_state.outcome
    if out is not None and out.failed:
        exc = out.exception()
        if isinstance(exc, GitHubRateLimitError):
            return min(exc.wait_seconds, RATE_LIMIT_TOTAL_WAIT_CAP_SECONDS)
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
