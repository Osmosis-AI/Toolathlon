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
