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
# Reclaim disk that previous failed-deploy attempts may have leaked.
#
# Every time a shared-infra container crashes and setup.sh restarts it,
# docker creates a fresh anonymous volume for the new container.  Old
# volumes left behind ("dangling") accumulate to hundreds of GB across
# thousands of restarts and eventually fill the disk, at which point
# subsequent deploys fail with "No space left on device" from inside
# mysqld / canvas postgres.  Prune at deploy start so we begin with the
# largest possible free disk.
# ---------------------------------------------------------------------------
echo "============================================================================================="
echo "Disk-reclaim sweep before deploy ..."
echo "============================================================================================="
df -h / | awk 'NR==1 || /\//' | head -2
docker volume prune -f 2>&1 | grep -E "reclaimed|^[a-f0-9]{6,}" | tail -3 || true
docker container prune -f --filter "until=24h" 2>&1 | grep -E "reclaimed|^[a-f0-9]{6,}" | tail -3 || true
docker image prune -f 2>&1 | grep -E "reclaimed" | tail -1 || true
df -h / | awk '/\//' | head -1
echo "============================================================================================="
echo ""

# ---------------------------------------------------------------------------
# Refresh the host's docker image cache for images referenced by the
# k8s tasks.  Per-task preprocess scripts then `kind load docker-image`
# from this cache into their kind clusters offline — no Docker Hub
# round trip per preprocess → no rate-limit issues under repeated runs.
#
# Best-effort: anonymous Docker Hub allows ~100 pulls / 6h per IP, so
# a refresh sweep that exceeds the quota will start failing midway.
# Each pull is run with a short timeout, return-codes are ignored, and
# we continue on failure.  Even partial success is a win — anything
# already cached is enough for that image's pre-load step in
# preprocess.
# ---------------------------------------------------------------------------
echo "============================================================================================="
echo "Refreshing docker image cache for k8s task images (best-effort, never fails the deploy) ..."
echo "============================================================================================="
K8S_TASK_IMAGES=(
    # k8s-mysql
    mysql:8.4
    nginx:1.14
    # k8s-deployment-cleanup
    nginx:1.20-alpine
    # k8s-redis-helm-upgrade (distractor pods)
    oliver006/redis_exporter:v1.45.0
    nginx:1.21-alpine
    # k8s-safety-audit
    alpine:3.20
    busybox:1.36
    nginxinc/nginx-unprivileged:1.25-alpine
    prom/prometheus:v2.52.0
    python:3.12-alpine
    redis:7.2
    # k8s-pr-preview-testing (preview.yaml from feature/pr-123 branch)
    nginx:1.25-alpine
)
_pulled=0
_skipped=0
_failed=0
for _img in "${K8S_TASK_IMAGES[@]}"; do
    # 30s timeout per pull — if Docker Hub is rate-limiting we'll fail
    # fast and move on.  --quiet keeps output minimal in steady state.
    if timeout 30 docker pull --quiet "$_img" >/dev/null 2>&1; then
        _pulled=$(( _pulled + 1 ))
    else
        if docker image inspect "$_img" >/dev/null 2>&1; then
            _skipped=$(( _skipped + 1 ))   # have a stale copy, fine
        else
            _failed=$(( _failed + 1 ))     # don't have it; next preprocess will pull
        fi
    fi
done
echo "  pulled/refreshed: $_pulled    not refreshed but cached: $_skipped    missing: $_failed    (total: ${#K8S_TASK_IMAGES[@]})"
echo "============================================================================================="
echo ""

# Read `podman_or_docker` from global_configs.py
podman_or_docker=$(uv run python -c "import sys; sys.path.append('configs'); from global_configs import global_configs; print(global_configs.podman_or_docker)" 2>/dev/null || echo "docker")

# Read instance_suffix from ports_config.yaml
instance_suffix=$(uv run python -c "
import yaml
try:
    with open('configs/ports_config.yaml', 'r') as f:
        config = yaml.safe_load(f)
        print(config.get('instance_suffix', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

echo "============================================================================================="
echo "Stopping existing Docker/Podman services cleanly..."
echo "============================================================================================="

bash deployment/poste/scripts/setup.sh stop "$poste_configure_dovecot" || true
bash deployment/woocommerce/scripts/setup.sh stop || true
bash deployment/canvas/scripts/setup.sh stop || true

if [ "$podman_or_docker" = "docker" ]; then
    docker rm -f "poste${instance_suffix}" "woo-wp${instance_suffix}" "woo-db${instance_suffix}" "canvas-docker${instance_suffix}" 2>/dev/null || true
    docker network rm "woo-net${instance_suffix}" 2>/dev/null || true
elif [ "$podman_or_docker" = "podman" ]; then
    podman rm -f "poste${instance_suffix}" "woo-wp${instance_suffix}" "woo-db${instance_suffix}" "canvas-docker${instance_suffix}" 2>/dev/null || true
    podman network rm "woo-net${instance_suffix}" 2>/dev/null || true
    podman pod rm -f "woo-pod${instance_suffix}" 2>/dev/null || true
fi

sleep 2

# Kill processes occupying required ports
echo "============================================================================================="
echo "Checking and killing processes on required ports..."
echo "============================================================================================="

# Define all required ports
REQUIRED_PORTS=(10001 20001 10005 2525 1143 1587 10003 30123 30124 30137)

for port in "${REQUIRED_PORTS[@]}"; do
    if lsof -i :$port -t >/dev/null 2>&1; then
        echo "Port $port is in use. Checking process(es)..."
        pids=$(lsof -i :$port -t)

        for pid in $pids; do
            process_name=$(ps -p "$pid" -o comm= 2>/dev/null || true)
            process_args=$(ps -p "$pid" -o args= 2>/dev/null || true)

            case "$process_name:$process_args" in
                *docker-proxy*|*com.docker.backend*|*podman*)
                    echo "  Skipping Docker/Podman-managed process PID $pid ($process_name). Clean containers instead."
                    ;;
                *)
                    echo "  Killing non-Docker PID $pid on port $port ($process_name)"
                    kill -9 "$pid" 2>/dev/null || true
                    ;;
            esac
        done

        sleep 1

        if lsof -i :$port -t >/dev/null 2>&1; then
            echo "  Warning: port $port is still in use after cleanup"
            lsof -i :$port || true
        else
            echo "  Port $port cleared"
        fi
    else
        echo "Port $port is free"
    fi
done

echo "All required ports checked and cleared"
echo "============================================================================================="
echo ""

# this is just to launch a test cluster (also clear existing ones) to make sure the MCP servers are ready to use
bash deployment/k8s/scripts/setup.sh # this is to create one test cluster

bash deployment/canvas/scripts/setup.sh # port 10001 20001

bash deployment/poste/scripts/setup.sh start $poste_configure_dovecot # port 10005 2525 1143 1587

bash deployment/woocommerce/scripts/setup.sh start 81 20 # port 10003

# we also use 30123, 30124 ports in two of the k8s tasks

# we also use 30137 for a web task to deploy a web page locally

# record exit time
echo "Exit time: $(date)"

# record total time
echo "Total time: $(($(date +%s) - start_time)) seconds"
