#!/usr/bin/env bash
# Fast shared-infrastructure readiness probe for the v3 service.
#
# Extracted from deploy_containers.sh so /v3/start admission can call the
# same checks without paying for the full deploy pipeline (which sleeps,
# clears ports, and starts deployment).  Each probe is independently bounded
# so the overall probe latency stays low on the happy path; on failure the
# script lists the failing services on stderr and exits non-zero.
#
# Exit code:
#   0  all required shared services are healthy
#   1  at least one probe failed
#
# Stderr:
#   per-failure lines like `✗ canvas not ready (:10001)` so the v3 service
#   can surface what went wrong via /v3/health and structured logs.

set -u

# ---------------------------------------------------------------------------
# Per-service ports.  apply_port_numbers.py rewrites these via its bounded
# regex on every run, so this file's named variables stay in sync with
# ports_config.yaml — identical to deploy_containers.sh.
# ---------------------------------------------------------------------------
CANVAS_HTTP_PORT=10001
POSTE_WEB_PORT=10005
POSTE_SMTP_PORT=2525
POSTE_IMAP_PORT=1143
POSTE_SUB_PORT=1587
WOO_PORT=10003

# ---------------------------------------------------------------------------
# Discover instance_suffix so we know what to call the kind cluster.
# Cluster name pattern is cluster${instance_suffix}1-control-plane.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

instance_suffix=$(cd "$PROJECT_ROOT" && uv run python -c "
import yaml
with open('configs/ports_config.yaml', 'r') as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('instance_suffix', '') or '')
" 2>/dev/null)
KIND_CLUSTER_NAME="cluster${instance_suffix}1-control-plane"

# ---------------------------------------------------------------------------
# Readiness probes — verbatim semantics from deploy_containers.sh, but the
# kind_ready probe now has an explicit `timeout` wrap so a wedged dockerd
# can't stall the whole admission decision.
# ---------------------------------------------------------------------------
canvas_ready() {
    curl -s --max-time 5 "http://localhost:$CANVAS_HTTP_PORT/api/v1/accounts" 2>/dev/null \
        | grep -q '"status"\|"errors"'
}
poste_web_ready() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:$POSTE_WEB_PORT/")
    [ "$code" = "302" ] || [ "$code" = "200" ]
}
poste_imap_ready() {
    (echo "a1 CAPABILITY"; sleep 0.2; echo "a2 LOGOUT") \
        | timeout 5 nc -w 3 localhost $POSTE_IMAP_PORT 2>/dev/null \
        | grep -q 'IMAP4rev1'
}
poste_smtp_ready_at() {
    (echo "EHLO healthcheck"; sleep 0.2; echo "QUIT") \
        | timeout 5 nc -w 3 localhost $1 2>/dev/null \
        | grep -q 'ESMTP'
}
woo_ready() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:$WOO_PORT/")
    [ "$code" = "302" ] || [ "$code" = "200" ]
}
kind_ready() {
    timeout 5 docker exec "$KIND_CLUSTER_NAME" \
        kubectl --kubeconfig=/etc/kubernetes/admin.conf get nodes --no-headers 2>/dev/null \
        | grep -q ' Ready '
}

any_fail=0
canvas_ready                          || { echo "✗ canvas not ready (:$CANVAS_HTTP_PORT)"        >&2; any_fail=1; }
poste_web_ready                       || { echo "✗ poste web not ready (:$POSTE_WEB_PORT)"       >&2; any_fail=1; }
poste_imap_ready                      || { echo "✗ poste imap not ready (:$POSTE_IMAP_PORT)"     >&2; any_fail=1; }
poste_smtp_ready_at $POSTE_SMTP_PORT  || { echo "✗ poste smtp not ready (:$POSTE_SMTP_PORT)"     >&2; any_fail=1; }
poste_smtp_ready_at $POSTE_SUB_PORT   || { echo "✗ poste submission not ready (:$POSTE_SUB_PORT)" >&2; any_fail=1; }
woo_ready                             || { echo "✗ woocommerce not ready (:$WOO_PORT)"           >&2; any_fail=1; }
kind_ready                            || { echo "✗ kind cluster not ready ($KIND_CLUSTER_NAME)"  >&2; any_fail=1; }

exit $any_fail
