"""V3-only launcher for the Toolathlon sandbox API.

Runs *just* the /v3/* REST routes — sessionless, lease-per-task model
with endpoint-local capacity and conflict-group locking.  See
``v3_service_implementation_plan.md`` for the design.

Usage:
    uv run eval_server_v3.py [port] [--max-actors N] [--idle-timeout-seconds S]

Examples:
    uv run eval_server_v3.py                        # port 8089, defaults
    uv run eval_server_v3.py 9000                   # port 9000, defaults
    uv run eval_server_v3.py 9000 --max-actors 10   # cap at 10 concurrent tasks
    uv run eval_server_v3.py --idle-timeout-seconds 600   # 10-min idle reap

CLI flags override the corresponding env-var defaults
(TOOLATHLON_V3_MAX_ACTIVE_EXECUTIONS, TOOLATHLON_V3_IDLE_TIMEOUT_SECONDS).

Notes
-----
The v3 code is fully self-contained — no v1 ``current_job`` cross-modal
check.  If you need mutex with v1 on the same host, run them on different
ports.  Stub the ``eval_server`` module early so any transitive import
from shared code (e.g. via the catalog) doesn't accidentally pull in v1.
"""

import argparse
import asyncio
import logging
import sys
import types
from datetime import datetime

# Stub eval_server early in case anything transitively imports it.
_stub = types.ModuleType("eval_server")
_stub.current_job = None
sys.modules["eval_server"] = _stub

import uvicorn
from fastapi import FastAPI

from v3_api.container_mgr import reconcile_orphan_containers
from v3_api.execution_manager import manager
from v3_api.router import install_v3_middleware, router as v3_router


class _TimestampFormatter(logging.Formatter):
    def format(self, record):
        local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        utc_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        record.timestamp = f"[{local_time}][UTC {utc_time}]"
        return super().format(record)


_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": _TimestampFormatter,
            "fmt": "%(timestamp)s %(levelname)s:     %(message)s",
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn":        {"handlers": ["default"], "level": "INFO"},
        "uvicorn.error":  {"level": "INFO"},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
    },
}


app = FastAPI(title="Toolathlon v3 Sandbox API", version="3.0")
app.include_router(v3_router, prefix="/v3")
install_v3_middleware(app)


@app.on_event("startup")
async def _startup() -> None:
    # Reap any leftover {prefix}toolathlon-v3-* containers from a crashed run.
    reconcile_orphan_containers()
    # Start the per-execution idle/lifetime reaper.
    manager.start_reaper()
    # Force a fresh deploy_containers.sh on every service start.  No probe
    # shortcut — operators have asked that a v3 service restart always
    # means a freshly-deployed shared infrastructure, not "verify the
    # previous instance's containers are still alive".  Fire-and-forget:
    # uvicorn must not be blocked from binding the port while the
    # multi-minute deploy runs; clients arriving in the meantime see
    # deploy_status="repairing" and bounce with a fast 503 + retry_after_s.
    # The TOOLATHLON_V3_SKIP_DEPLOY env var still bypasses this for debug.
    manager.trigger_initial_deploy()


@app.on_event("shutdown")
async def _shutdown() -> None:
    # On SIGTERM/SIGINT, stop every active container and cancel in-flight
    # setup/watchdog tasks so per-task resources don't leak.
    try:
        await manager.shutdown()
    except Exception as e:
        print(f"[eval_server_v3] shutdown error: {e!r}", flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval_server_v3.py",
        description="Toolathlon v3 sandbox API launcher.",
    )
    parser.add_argument(
        "port",
        nargs="?",
        type=int,
        default=8089,
        help="Port to bind (default: 8089).",
    )
    # Defaults come from the manager, which already incorporates env vars
    # (TOOLATHLON_V3_MAX_ACTIVE_EXECUTIONS / TOOLATHLON_V3_IDLE_TIMEOUT_SECONDS).
    # CLI flags override env-var defaults.
    parser.add_argument(
        "--max-actors",
        type=int,
        default=manager.max_active_executions,
        metavar="N",
        help=(
            "Maximum number of concurrent task executions admitted on this "
            "endpoint (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--idle-timeout-seconds",
        type=int,
        default=manager.idle_timeout_seconds,
        metavar="S",
        help=(
            "Seconds an execution may go without client activity before the "
            "reaper releases it (default: %(default)s)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Apply CLI overrides before the reaper / setup tasks are spawned.
    manager.max_active_executions = args.max_actors
    manager.idle_timeout_seconds = args.idle_timeout_seconds

    print(
        f"[eval_server_v3] starting on http://0.0.0.0:{args.port}/v3/ "
        f"(max_actors={args.max_actors}, idle_timeout_seconds={args.idle_timeout_seconds})",
        flush=True,
    )
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_config=_LOG_CONFIG)


if __name__ == "__main__":
    main()
