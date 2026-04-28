"""V2-only launcher for the Toolathlon sandbox API.

Runs *just* the /v2/* REST routes — no v1 evaluation pipeline, no v1
``submit_evaluation`` endpoint, no WebSocket proxy.  Use this when you only
want to serve sandbox-as-service workloads and don't need v1 backwards
compatibility on the same process/port.

Usage:
    uv run eval_server_v2.py [port]

Default port is 8088.

Implementation notes
--------------------
The v2 code (``v2_api.session.is_server_busy``) does ``import eval_server``
and reads ``eval_server.current_job`` for cross-modal mutual exclusion.  In
v2-only mode there is no v1, so we register a stub ``eval_server`` module
in ``sys.modules`` *before* importing anything from ``v2_api``.  The stub
exposes ``current_job = None`` permanently — v2 then treats v1 as always
idle, which is exactly what we want.
"""

import logging
import sys
import types
from datetime import datetime

# IMPORTANT: install the eval_server stub BEFORE importing anything from
# v2_api, otherwise v2_api.session would pull in the real eval_server.py
# (and its v1 route registrations) on first lookup.
_stub = types.ModuleType("eval_server")
_stub.current_job = None
sys.modules["eval_server"] = _stub

import uvicorn
from fastapi import FastAPI

from v2_api.container_mgr import reconcile_orphan_containers
from v2_api.router import router as v2_router
import v2_api.session as v2_session_mod


# Mirror eval_server.py's uvicorn log format: prefix every line with both
# local and UTC timestamps so logs from this server are interleavable with
# v1's logs without ambiguity.
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

app = FastAPI(title="Toolathlon v2 Sandbox API", version="2.0")
app.include_router(v2_router, prefix="/v2")


@app.on_event("startup")
async def _startup() -> None:
    # Reap any leftover ``{prefix}toolathlon-v2-*`` containers from a prior
    # crashed run.  Same behaviour as the v1+v2 launcher's startup hook.
    reconcile_orphan_containers()


@app.on_event("shutdown")
async def _shutdown() -> None:
    # On SIGTERM/SIGINT, tear down the active v2 session (if any) so
    # per-task containers don't leak.
    sess = v2_session_mod.current_session
    if sess is not None:
        try:
            await v2_session_mod.delete_session(sess.session_id)
        except Exception as e:
            print(f"[eval_server_v2] shutdown: error tearing down session {sess.session_id}: {e}", flush=True)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    print(f"[eval_server_v2] starting v2-only server on http://0.0.0.0:{port}/v2/...", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=_LOG_CONFIG)


if __name__ == "__main__":
    main()
