import sys
import os
import time
from argparse import ArgumentParser
import requests

# Add utils to path
sys.path.append(os.path.dirname(__file__))

from configs.token_key_session import all_token_key_session
from utils.general.helper import print_color
from utils.app_specific.github.api import (
    DEFAULT_TIMEOUT, github_retry,
    _check_rate_limit, _throttle_mutation,
)


@github_retry
def wait_for_repo_deleted(repo_full_name: str, headers: dict, max_wait: int = 30, interval: int = 2) -> bool:
    """
    Wait for a repository to be deleted.
    Returns True if deleted successfully, False if timeout.
    """
    print_color(f"Waiting for repo {repo_full_name} to be deleted...", "yellow")
    start_time = time.time()

    while time.time() - start_time < max_wait:
        response = requests.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers, timeout=DEFAULT_TIMEOUT)
        _check_rate_limit(response)
        if response.status_code == 404:
            print_color(f"Repo {repo_full_name} has been deleted successfully", "green")
            return True

        print_color(f"Repo still exists, waiting {interval} seconds...", "yellow")
        time.sleep(interval)

    return False

@github_retry
def wait_for_fork_completed(repo_full_name: str, headers: dict, max_wait: int = 60, interval: int = 3) -> bool:
    """
    Wait for a fork operation to complete.
    Returns True if fork is ready, False if timeout.
    """
    print_color(f"Waiting for fork {repo_full_name} to be ready...", "yellow")
    start_time = time.time()

    while time.time() - start_time < max_wait:
        response = requests.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers, timeout=DEFAULT_TIMEOUT)
        _check_rate_limit(response)

        if response.status_code == 200:
            repo_data = response.json()
            # Check if the repo is ready (not being created)
            # Usually when fork is complete, the repo will have all metadata
            if repo_data.get("size") is not None and repo_data.get("default_branch"):
                print_color(f"Fork {repo_full_name} is ready", "green")
                return True
            else:
                print_color(f"Fork exists but still being set up, waiting {interval} seconds...", "yellow")
        else:
            print_color(f"Fork not yet available (status: {response.status_code}), waiting {interval} seconds...", "yellow")

        time.sleep(interval)

    return False


# ── Decorated helpers for the inline calls in main() ───────────────
# main() used to call requests.get / delete / post directly with no
# retry, so a single rate-limit response would kill the whole
# delete-and-refork flow.  These small wrappers go through @github_retry
# and call _check_rate_limit / _throttle_mutation appropriately.


@github_retry
def _get_user_login(headers: dict) -> str:
    r = requests.get("https://api.github.com/user", headers=headers, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch GitHub user: {r.status_code} {r.text}")
    return r.json()["login"]


@github_retry
def _repo_exists(repo_full_name: str, headers: dict) -> bool:
    r = requests.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"Unexpected status when checking repo {repo_full_name}: {r.status_code} {r.text}")


@github_retry
def _delete_repo(repo_full_name: str, headers: dict) -> None:
    _throttle_mutation()
    r = requests.delete(f"https://api.github.com/repos/{repo_full_name}", headers=headers, timeout=DEFAULT_TIMEOUT)
    _check_rate_limit(r)
    if r.status_code not in (204,):
        raise RuntimeError(f"Failed to delete repo {repo_full_name}: Status {r.status_code} {r.text}")


@github_retry
def _post_fork(source_repo_name: str, data: dict, headers: dict) -> requests.Response:
    _throttle_mutation()
    r = requests.post(
        f"https://api.github.com/repos/{source_repo_name}/forks",
        headers=headers, json=data, timeout=DEFAULT_TIMEOUT,
    )
    _check_rate_limit(r)
    if r.status_code != 202:
        error_msg = r.json().get('message', r.text) if r.text else f"Status {r.status_code}"
        raise RuntimeError(f"Failed to fork repo: {error_msg}")
    return r

def main():
    parser = ArgumentParser(description="Example code for notion tasks preprocess")
    parser.add_argument("--source_repo_name", required=True, help="Source repo name")  # org/repo
    parser.add_argument("--target_repo_name", required=True, help="Target repo name")  # org/name, if no org, just under the user
    parser.add_argument("--read_only", action="store_true", help="Read only mode")  # if the task is readonly, if so we only need to fork once
    parser.add_argument("--default_branch_only", action="store_true", help="Only delete the default branch")
    parser.add_argument("--max_wait_delete", type=int, default=30, help="Max seconds to wait for deletion")
    parser.add_argument("--max_wait_fork", type=int, default=180, help="Max seconds to wait for fork")
    args = parser.parse_args()

    github_token = all_token_key_session.github_token
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    source_repo_name = args.source_repo_name
    target_repo_name = args.target_repo_name
    target_repo_org_or_user = None

    username = _get_user_login(headers)

    if "/" in target_repo_name:
        target_repo_org_or_user = target_repo_name.split("/")[0]
        target_repo_name = target_repo_name.split("/")[1]
    else:
        target_repo_org_or_user = username

    target_repo_full_name = f"{target_repo_org_or_user}/{target_repo_name}"

    # First check if the target repo exists
    existed_flag = _repo_exists(target_repo_full_name, headers)
    if existed_flag:
        print_color(f"Target repo {target_repo_full_name} already exists", "green")
    else:
        print_color(f"Target repo {target_repo_full_name} does not exist", "yellow")

    if args.read_only and existed_flag:
        print_color(f"This is a read only task and target repo {target_repo_full_name} already exists, skipping...", "green")
        return

    # In all other cases: 1) write tasks, or, 2) read tasks but we have not forked yet
    # We need to delete the target repo first if it exists, then fork the repo

    if existed_flag:
        print_color(f"Deleting repo {target_repo_full_name} and reforking from {source_repo_name}", "cyan")

        # Delete the target repo first if it exists
        _delete_repo(target_repo_full_name, headers)
        print_color(f"Delete request sent for repo {target_repo_full_name}", "green")

        # Wait for deletion to complete
        if not wait_for_repo_deleted(target_repo_full_name, headers, args.max_wait_delete):
            print_color(f"Warning: Timeout waiting for repo deletion. The repo might still be deleting.", "red")
            raise Exception(f"Timeout waiting for repo {target_repo_full_name} to be deleted")

    print_color(f"Forking repo {source_repo_name} to {target_repo_full_name}", "cyan")

    # Fork the repo
    data = {
        "name": target_repo_name,
        "default_branch_only": args.default_branch_only
    }

    if target_repo_org_or_user is not None and target_repo_org_or_user != username:
        data["organization"] = target_repo_org_or_user

    _post_fork(source_repo_name, data, headers)
    print_color(f"Fork request sent for {source_repo_name} to {target_repo_full_name}", "green")

    # Wait for fork to complete
    if wait_for_fork_completed(target_repo_full_name, headers, args.max_wait_fork):
        print_color(f"Successfully forked {source_repo_name} to {target_repo_full_name}", "green")
    else:
        print_color(f"Warning: Timeout waiting for fork to complete. The fork might still be in progress.", "yellow")
        raise Exception(f"Timeout waiting for fork {target_repo_full_name} to be ready")

if __name__ == "__main__":
    main()