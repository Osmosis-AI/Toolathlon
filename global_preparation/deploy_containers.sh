# record start time
start_time=$(date +%s)
echo "Start time: $(date)"

poste_configure_dovecot=${1:-true}
echo "============================================================================================="
echo "poste_configure_dovecot: $poste_configure_dovecot"
echo "For some Linux distributions, you need to configure Dovecot to allow plaintext auth."
echo "If you are not sure, please set to true."
echo "Our experience: Ubuntu 24.04 should set this as true, but AlmaLinux should set this as false."
echo "============================================================================================="

sleep 5

# ---------------------------------------------------------------------------
# Per-service ports.  apply_port_numbers.py rewrites these via its bounded
# regex (`(?<![0-9])<old>(?![0-9])`) on every run, so both this file's named
# variables and the REQUIRED_PORTS array stay in sync with ports_config.yaml.
# ---------------------------------------------------------------------------
CANVAS_HTTP_PORT=10001
CANVAS_HTTPS_PORT=20001
POSTE_WEB_PORT=10005
POSTE_SMTP_PORT=2525
POSTE_IMAP_PORT=1143
POSTE_SUB_PORT=1587
WOO_PORT=10003
K8S_PR_PORT=30123
K8S_MYSQL_PORT=30124
WEB_TASK_PORT=30137

REQUIRED_PORTS=($CANVAS_HTTP_PORT $CANVAS_HTTPS_PORT $POSTE_WEB_PORT $POSTE_SMTP_PORT $POSTE_IMAP_PORT $POSTE_SUB_PORT $WOO_PORT $K8S_PR_PORT $K8S_MYSQL_PORT $WEB_TASK_PORT)

# ---------------------------------------------------------------------------
# Retry / readiness tunables
# ---------------------------------------------------------------------------
MAX_DEPLOY_ATTEMPTS=2          # how many times to (re)run the full setup
READINESS_TIMEOUT_SECONDS=420  # 7 min: Canvas first-boot is the slow path
PROBE_INTERVAL_SECONDS=5

# ---------------------------------------------------------------------------
# Discover instance_suffix so we know what to call the kind cluster.
# Cluster name pattern is cluster${instance_suffix}1-control-plane.
# ---------------------------------------------------------------------------
instance_suffix=$(uv run python -c "
import yaml
with open('configs/ports_config.yaml', 'r') as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('instance_suffix', '') or '')
")
KIND_CLUSTER_NAME="cluster${instance_suffix}1-control-plane"

# ---------------------------------------------------------------------------
# Readiness probes — each returns 0 if the service is up and serving.
# Probe semantics chosen to match what real clients hit:
#   - Canvas Rails API actually responding (not just the TCP port being open).
#   - Poste IMAP/SMTP/SMTP-submission speaking their protocol banners.
#   - WooCommerce serving HTTP (the storefront 302s to login when ready).
#   - Kind cluster's control-plane node reporting Ready via kubectl.
# ---------------------------------------------------------------------------
canvas_ready() {
    # Canvas Rails API returns 401 with a JSON body when alive but unauthed.
    # Don't use curl -f (it would treat 401 as failure); just check the body.
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
poste_smtp_ready_at() {  # arg: port
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
    docker exec "$KIND_CLUSTER_NAME" \
        kubectl --kubeconfig=/etc/kubernetes/admin.conf get nodes --no-headers 2>/dev/null \
        | grep -q ' Ready '
}

probe_all() {
    canvas_ready          || { echo "  ✗ canvas not ready (:$CANVAS_HTTP_PORT)";        return 1; }
    poste_web_ready       || { echo "  ✗ poste web not ready (:$POSTE_WEB_PORT)";       return 1; }
    poste_imap_ready      || { echo "  ✗ poste imap not ready (:$POSTE_IMAP_PORT)";     return 1; }
    poste_smtp_ready_at $POSTE_SMTP_PORT || { echo "  ✗ poste smtp not ready (:$POSTE_SMTP_PORT)"; return 1; }
    poste_smtp_ready_at $POSTE_SUB_PORT  || { echo "  ✗ poste submission not ready (:$POSTE_SUB_PORT)"; return 1; }
    woo_ready             || { echo "  ✗ woocommerce not ready (:$WOO_PORT)";           return 1; }
    kind_ready            || { echo "  ✗ kind cluster not ready ($KIND_CLUSTER_NAME)";  return 1; }
    return 0
}

wait_for_all_ready() {
    local timeout=$1
    local deadline=$(( $(date +%s) + timeout ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if probe_all >/dev/null 2>&1; then
            echo "  ✓ all infra services ready"
            return 0
        fi
        local left=$(( deadline - $(date +%s) ))
        echo "  …waiting for services to be ready (${left}s left)"
        sleep $PROBE_INTERVAL_SECONDS
    done
    echo "  TIMEOUT — last failing probes:"
    probe_all || true
    return 1
}

# ---------------------------------------------------------------------------
# Run the actual setup steps.  Each component's setup.sh start is idempotent
# (it stops any existing instance first), so calling this in a retry loop
# is safe.
# ---------------------------------------------------------------------------
run_setup() {
    bash deployment/k8s/scripts/setup.sh
    bash deployment/canvas/scripts/setup.sh # port $CANVAS_HTTP_PORT $CANVAS_HTTPS_PORT
    bash deployment/poste/scripts/setup.sh start $poste_configure_dovecot # port $POSTE_WEB_PORT $POSTE_SMTP_PORT $POSTE_IMAP_PORT $POSTE_SUB_PORT
    bash deployment/woocommerce/scripts/setup.sh start 81 20 # port $WOO_PORT
}

# ---------------------------------------------------------------------------
# Kill processes occupying required ports (only on the very first attempt;
# subsequent attempts go through each setup.sh's own start→stop cycle).
# ---------------------------------------------------------------------------
echo "============================================================================================="
echo "Checking and killing processes on required ports..."
echo "============================================================================================="

for port in "${REQUIRED_PORTS[@]}"; do
    if lsof -i :$port -t >/dev/null 2>&1; then
        echo "Port $port is in use. Killing process(es)..."
        pids=$(lsof -i :$port -t)
        for pid in $pids; do
            echo "  Killing PID $pid on port $port"
            kill -9 $pid 2>/dev/null || true
        done
        sleep 1
        echo "  Port $port cleared"
    else
        echo "Port $port is free"
    fi
done
echo "All required ports checked and cleared"
echo "============================================================================================="
echo ""

# ---------------------------------------------------------------------------
# Retry loop: setup → wait for readiness → on timeout, redeploy from scratch.
# ---------------------------------------------------------------------------
attempt=1
while [ $attempt -le $MAX_DEPLOY_ATTEMPTS ]; do
    echo "============================================================================================="
    echo "Deploy attempt $attempt of $MAX_DEPLOY_ATTEMPTS (cluster=$KIND_CLUSTER_NAME)"
    echo "============================================================================================="
    run_setup

    echo ""
    echo "Waiting up to ${READINESS_TIMEOUT_SECONDS}s for all services to be ready..."
    if wait_for_all_ready $READINESS_TIMEOUT_SECONDS; then
        echo "Deploy attempt $attempt succeeded."
        break
    fi

    if [ $attempt -ge $MAX_DEPLOY_ATTEMPTS ]; then
        echo "ERROR: services not ready after $MAX_DEPLOY_ATTEMPTS attempts. Giving up."
        echo "Exit time: $(date)"
        echo "Total time: $(($(date +%s) - start_time)) seconds"
        exit 1
    fi

    echo ""
    echo "============================================================================================="
    echo "Services not ready in time on attempt $attempt — re-running deploy from the beginning."
    echo "============================================================================================="
    attempt=$((attempt + 1))
done

# we also use $K8S_PR_PORT, $K8S_MYSQL_PORT ports in two of the k8s tasks
# we also use $WEB_TASK_PORT for a web task to deploy a web page locally

# record exit time
echo "Exit time: $(date)"

# record total time
echo "Total time: $(($(date +%s) - start_time)) seconds"
