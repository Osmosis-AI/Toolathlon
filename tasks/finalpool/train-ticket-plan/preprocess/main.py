"""Fast-fail preprocess: train-ticket-plan is disabled.

Why disabled
------------
The ``rail_12306`` MCP server (npm package ``12306-mcp``) calls
``getLCQueryPath`` at startup to fetch the *station-name JS file* from
12306.cn, which is the lookup table mapping Chinese station names to
12306 telecodes (see
``/workspace/node_modules/12306-mcp/build/index.js:1103``).  That
network call cannot reach 12306.cn from the container, so the MCP
server throws::

    Error: get station name js file failed.

The gateway in ``scripts/decoupled/container_tool_gateway.py`` is
all-or-nothing — it expects every server in ``needed_mcp_servers`` to
come up, and exits with::

    ValueError: Only 3 servers connected, expected 4

Because the gateway is spawned via ``nohup ... > /dev/null 2>&1 &`` and
the container's PID 1 is just ``sleep 5400`` (no init reaper), the
gateway python process becomes a defunct zombie immediately, and v3's
gateway_boot phase hangs for the full 900s timeout window before
declaring the execution failed.

Why fail-fast here
------------------
Without this script, every train-ticket-plan launch costs the host:
~15 minutes per attempt before the slot is freed.  Returning a non-zero
exit from preprocess gives v3 a clean ``setup_failed`` in seconds.
Clients see a normal task-failure result and the slot frees fast.

To re-enable
------------
Either pin 12306-mcp to a version that bundles the station data, or
make the gateway tolerant of partial MCP server availability, then
delete this file so v3 stops invoking it as the preprocess command.
"""

import sys

print(
    "train-ticket-plan: disabled — 12306-mcp cannot fetch the station-name "
    "lookup table from 12306.cn at startup, so the task gateway hangs "
    "until the 900s timeout fires.  Fast-failing here so the client "
    "receives a setup_failed in seconds.  See this file's module "
    "docstring for re-enable instructions.",
    file=sys.stderr,
)
sys.exit(1)
