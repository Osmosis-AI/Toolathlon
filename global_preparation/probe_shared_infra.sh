#!/usr/bin/env bash
# Compatibility shim: the canonical probe is now probe_shared_infra.py.
# This thin wrapper exists so running services that were started before
# the cutover (and hardcoded the .sh path) keep working without restart.
#
# After every v3 process has been restarted, this file can be removed
# and ``v3_api/container_mgr.py``'s subprocess call already points at
# the Python module directly.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

exec uv run --project "$PROJECT_ROOT" python -m global_preparation.probe_shared_infra
