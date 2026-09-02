#!/bin/bash

# Read `podman_or_docker` from global_configs.py
podman_or_docker=$(uv run python -c "import sys; sys.path.append('configs'); from global_configs import global_configs; print(global_configs.podman_or_docker)")

# Read instance_suffix from ports_config.yaml
instance_suffix=$(uv run python -c "
import yaml
try:
    with open('configs/ports_config.yaml', 'r') as f:
        config = yaml.safe_load(f)
        print(config.get('instance_suffix', ''))
except:
    print('')
" 2>/dev/null || echo "")

# Exposed ports - use unprivileged ports
WEB_PORT=10005        # Web interface port
SMTP_PORT=2525        # SMTP port
IMAP_PORT=1143        # IMAP port
SUBMISSION_PORT=1587  # SMTP submission port
NUM_USERS=503

# Container name with suffix
CONTAINER_NAME="poste${instance_suffix}"

# Data storage directories - convert to absolute path
DATA_DIR="$(pwd)/deployment/poste/data"
CONFIG_DIR="$(pwd)/deployment/poste/configs"

# Get command arguments
COMMAND=${1:-start}            # Default is start
CONFIGURE_DOVECOT=${2:-true}   # Default is true

# Function to stop and remove container
stop_container() {
  echo "🛑 Stopping Poste.io container..."
  $podman_or_docker stop $CONTAINER_NAME 2>/dev/null
  $podman_or_docker rm $CONTAINER_NAME 2>/dev/null
  echo "✅ Container stopped and removed"
}

# Function to start container
start_container() {
  # Create data directory and set permissions
  mkdir -p "$DATA_DIR"
  
  # Set directory permissions - Poste.io uses UID 1001
  chmod -R 777 "$DATA_DIR"
  
  echo "📁 Data directory: $DATA_DIR"
  
  # Start Poste.io
  echo "🚀 Starting Poste.io..."
  $podman_or_docker run -d \
    --name $CONTAINER_NAME \
    --cap-add NET_ADMIN \
    --cap-add NET_RAW \
    --cap-add NET_BIND_SERVICE \
    --cap-add SYS_PTRACE \
    -p ${WEB_PORT}:80 \
    -p ${SMTP_PORT}:25 \
    -p ${IMAP_PORT}:143 \
    -p ${SUBMISSION_PORT}:587 \
    -e "DISABLE_CLAMAV=TRUE" \
    -e "DISABLE_RSPAMD=TRUE" \
    -e "DISABLE_P0F=TRUE" \
    -e "HTTPS_FORCE=0" \
    -e "HTTPS=OFF" \
    -v ${DATA_DIR}:/data:Z \
    --hostname mcp.com \
    analogic/poste.io:2.5.5

  # Check start status
  if [ $? -eq 0 ]; then
    echo "✅ Poste.io started successfully!"
    echo "📧 Web interface: http://localhost:${WEB_PORT}"
    echo "📁 Data directory: ${DATA_DIR}"
    echo ""
    echo "⚠️  Note: Non-standard ports are used"
    echo "   SMTP: localhost:${SMTP_PORT}"
    echo "   IMAP: localhost:${IMAP_PORT}"
    echo "   Submission: localhost:${SUBMISSION_PORT}"
    echo ""
    echo "First visit please go to: http://localhost:${WEB_PORT}/admin/install"
    echo "View logs please run: $podman_or_docker logs -f $CONTAINER_NAME"
  else
    echo "❌ Start failed!"
    exit 1
  fi
}

wait_for_dovecot() {
  local attempt=0

  while [ "$attempt" -lt 120 ]; do
    if printf 'a1 CAPABILITY\r\na2 LOGOUT\r\n' \
        | timeout 3 nc -w 2 localhost "$IMAP_PORT" 2>/dev/null \
        | grep -q 'IMAP4'; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done

  echo "❌ Dovecot did not become ready"
  return 1
}

# Function to modify mail service config in container to allow plaintext auth
configure_dovecot() {
  local dovecot_pid

  echo "🔧 Configuring mail services to allow plaintext authentication..."

  # Poste's startup config pass can overwrite this setting, so wait for live IMAP first.
  wait_for_dovecot || return 1

  # Modify Dovecot SSL config: change ssl = required to ssl = yes
  $podman_or_docker exec $CONTAINER_NAME sed -i 's/ssl = required/ssl = yes/' /etc/dovecot/conf.d/10-ssl.conf

  # Replace the effective setting when present and append it when absent.
  $podman_or_docker exec $CONTAINER_NAME sh -c '
    config=/etc/dovecot/conf.d/10-auth.conf
    if grep -Eq "^[[:space:]]*auth_allow_cleartext[[:space:]]*=" "$config"; then
      sed -i "s/^[[:space:]]*auth_allow_cleartext[[:space:]]*=.*/auth_allow_cleartext = yes/" "$config"
    else
      printf "\nauth_allow_cleartext = yes\n" >> "$config"
    fi
  '

  # Clean up previously added wrong config
  $podman_or_docker exec $CONTAINER_NAME sed -i '/disable_plaintext_auth/d' /etc/dovecot/conf.d/10-auth.conf

  # Configure Haraka SMTP to allow plaintext auth
  echo "🔧 Configuring Haraka SMTP..."
  $podman_or_docker exec $CONTAINER_NAME sed -i 's/tls_required = true/tls_required = false/' /opt/haraka-smtp/config/auth.ini

  # Configure Haraka Submission (port 587) to allow plaintext auth
  echo "🔧 Configuring Haraka Submission (port 587)..."
  $podman_or_docker exec $CONTAINER_NAME sed -i 's/tls_required = true/tls_required = false/' /opt/haraka-submission/config/auth.ini

  # Temporarily disable auth plugin for submission for testing
  echo "🔧 Temporarily disabling auth plugin for submission..."
  $podman_or_docker exec $CONTAINER_NAME sed -i 's/^auth\/poste/#auth\/poste/' /opt/haraka-submission/config/plugins

  # Configure relay ACL to allow local connections
  echo "🔧 Configuring relay ACL..."
  $podman_or_docker exec $CONTAINER_NAME sh -c 'echo "127.0.0.1/8" > /opt/haraka-submission/config/relay_acl_allow'
  $podman_or_docker exec $CONTAINER_NAME sh -c 'echo "192.168.0.0/16" >> /opt/haraka-submission/config/relay_acl_allow'
  $podman_or_docker exec $CONTAINER_NAME sh -c 'echo "172.16.0.0/12" >> /opt/haraka-submission/config/relay_acl_allow'
  $podman_or_docker exec $CONTAINER_NAME sh -c 'echo "10.0.0.0/8" >> /opt/haraka-submission/config/relay_acl_allow'

  # Verify Dovecot config
  echo "🔍 Verifying Dovecot configuration..."
  if ! $podman_or_docker exec $CONTAINER_NAME doveconf -n > /dev/null 2>&1; then
    echo "❌ Dovecot configuration error, checking..."
    $podman_or_docker exec $CONTAINER_NAME doveconf -n
    return 1
  fi
  if ! $podman_or_docker exec $CONTAINER_NAME doveconf -n 2>/dev/null \
      | grep -Eq '^auth_allow_cleartext = yes$'; then
    echo "❌ Dovecot plaintext authentication setting is not effective"
    return 1
  fi
  echo "✅ Dovecot configuration is valid"

  # Reload service config
  echo "🔄 Reloading mail service configurations..."
  if ! $podman_or_docker exec $CONTAINER_NAME doveadm reload 2>/dev/null; then
    dovecot_pid=$($podman_or_docker exec $CONTAINER_NAME pgrep dovecot 2>/dev/null | head -1)
    if [ -z "$dovecot_pid" ] \
        || ! $podman_or_docker exec $CONTAINER_NAME kill -HUP "$dovecot_pid" 2>/dev/null; then
      echo "❌ Failed to reload Dovecot"
      return 1
    fi
  fi

  # Restart Haraka SMTP services
  echo "🔄 Restarting Haraka services..."
  $podman_or_docker exec $CONTAINER_NAME kill $($podman_or_docker exec $CONTAINER_NAME pgrep -f "haraka.*smtp") 2>/dev/null || true
  $podman_or_docker exec $CONTAINER_NAME kill $($podman_or_docker exec $CONTAINER_NAME pgrep -f "haraka.*submission") 2>/dev/null || true
  sleep 3

  echo "✅ Mail services configured to allow plaintext authentication"
}

# Function to create accounts
create_accounts() {
  bash deployment/poste/scripts/create_users.sh $NUM_USERS
}

# Verify the mapped IMAP endpoint accepts the same plaintext login used by tasks.
verify_imap_login() {
  local email password quoted_email quoted_password response

  IFS=$'\t' read -r email password < <(
    jq -r '.users[0] | [.email, .password] | @tsv' configs/users_data.json
  )
  if [ -z "$email" ] || [ -z "$password" ]; then
    echo "❌ IMAP fixture credential is missing"
    return 1
  fi

  quoted_email=${email//\\/\\\\}
  quoted_email=${quoted_email//\"/\\\"}
  quoted_password=${password//\\/\\\\}
  quoted_password=${quoted_password//\"/\\\"}

  if ! response=$(printf 'a1 LOGIN "%s" "%s"\r\na2 LOGOUT\r\n' \
      "$quoted_email" "$quoted_password" \
      | timeout 10 nc -w 5 localhost "$IMAP_PORT" 2>/dev/null); then
    echo "❌ IMAP plaintext login probe could not connect"
    return 1
  fi
  if ! printf '%s\n' "$response" | grep -Eq '^a1 OK([[:space:]]|$)' \
      || ! printf '%s\n' "$response" | grep -Eq '^a2 OK([[:space:]]|$)'; then
    echo "❌ IMAP plaintext login probe failed"
    return 1
  fi

  echo "✅ IMAP plaintext login verified"
}

# Function to perform cleanup
perform_cleanup() {
  echo "🧹 Starting cleanup process..."
  
  # Clean data directory
  if [ -d "$DATA_DIR" ]; then
    if [ "$podman_or_docker" = "podman" ] && command -v podman >/dev/null 2>&1; then
      # Podman environment
      # Try direct removal first, if fails then use unshare
      if rm -rf "$DATA_DIR" >/dev/null 2>&1; then
        echo "🗑️  Cleaned data directory..."
      else
        echo "🗑️  Cleaned data directory (podman unshare)..."
        podman unshare rm -rf "$DATA_DIR"
      fi
    elif [ "$EUID" -eq 0 ]; then
      # Root user
      echo "🗑️  Cleaned data directory (as root)..."
      rm -rf "$DATA_DIR"
    else
      # Use sudo
      echo "🗑️  Cleaned data directory (sudo)..."
      sudo rm -rf "$DATA_DIR"
    fi
  fi
  
  # Clean config directory (usually doesn't need special permissions)
  if [ -d "$CONFIG_DIR" ]; then
    echo "🗑️  Cleaned configs directory..."
    rm -rf "$CONFIG_DIR"
  fi
  
  echo "✅ Cleanup completed"
}

# Main logic
case "$COMMAND" in
  start)
    stop_container
    perform_cleanup
    start_container
    sleep 10
    if [ "$CONFIGURE_DOVECOT" = "true" ]; then
      configure_dovecot || exit 1
    fi
    create_accounts || exit 1
    verify_imap_login || exit 1
    ;;
  stop)
    stop_container
    perform_cleanup
    ;;
  restart)
    stop_container
    perform_cleanup
    start_container
    sleep 10
    if [ "$CONFIGURE_DOVECOT" = "true" ]; then
      configure_dovecot || exit 1
    fi
    create_accounts || exit 1
    verify_imap_login || exit 1
    ;;
  clean)
    stop_container
    perform_cleanup
    ;;
  config)
    configure_dovecot || exit 1
    ;;
  *)
    echo "How to use: $0 {start|stop|restart|clean|config}"
    echo "  start   - Stop old container and start new container"
    echo "  stop    - Just stop and delete container"
    echo "  restart - Restart container"
    echo "  config  - Configure Dovecot to allow plaintext auth"
    echo "  All above operations will clear old data and configs"
    exit 1
    ;;
esac
