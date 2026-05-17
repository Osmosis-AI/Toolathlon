"""V3-only launcher for the Toolathlon sandbox API.

Runs *just* the /v3/* REST routes — sessionless, lease-per-task model
with endpoint-local capacity and conflict-group locking.  See
``v3_service_implementation_plan.md`` for the design.

Usage:
    uv run eval_server_v3.py [port]

Default port is 8089.

Notes
-----
The v3 code is fully self-contained — no v1 ``current_job`` cross-modal
check.  If you need mutex with v1 on the same host, run them on different
ports.  Stub the ``eval_server`` module early so any transitive import
from shared code (e.g. via the catalog) doesn't accidentally pull in v1.
"""

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


@app.on_event("shutdown")
async def _shutdown() -> None:
    # On SIGTERM/SIGINT, stop every active container and cancel in-flight
    # setup/watchdog tasks so per-task resources don't leak.
    try:
        await manager.shutdown()
    except Exception as e:
        print(f"[eval_server_v3] shutdown error: {e!r}", flush=True)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    print(f"[eval_server_v3] starting v3-only server on http://0.0.0.0:{port}/v3/...", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=_LOG_CONFIG)


if __name__ == "__main__":
    main()
