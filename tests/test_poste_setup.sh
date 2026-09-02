#!/usr/bin/env bash
# Hermetic regression coverage for Poste plaintext authentication setup.

set -u

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$TEST_DIR/.." && pwd)
SETUP_SCRIPT="$REPO_ROOT/deployment/poste/scripts/setup.sh"
CREATE_USERS_SCRIPT="$REPO_ROOT/deployment/poste/scripts/create_users.sh"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/toolathlon-poste-setup.XXXXXX")
FAKE_BIN="$TEST_ROOT/fake-bin"
PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "ok - $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "not ok - $1"; }

assert_success() {
    local label=$1 rc=$2
    if [ "$rc" -eq 0 ]; then pass "$label"; else fail "$label"; fi
}

assert_failure() {
    local label=$1 rc=$2
    if [ "$rc" -ne 0 ]; then pass "$label"; else fail "$label"; fi
}

on_exit() {
    if [ "$FAIL_COUNT" -eq 0 ] && [ "${KEEP_TEST_ARTIFACTS:-0}" != "1" ]; then
        rm -rf -- "$TEST_ROOT"
    else
        echo "# artifacts kept in $TEST_ROOT"
    fi
}
trap on_exit EXIT

mkdir -p "$FAKE_BIN"

cat > "$FAKE_BIN/uv" <<'EOF'
#!/bin/sh
case "$*" in
    *global_configs*) echo docker ;;
    *) echo "" ;;
esac
EOF

cat > "$FAKE_BIN/docker" <<'EOF'
#!/usr/bin/env bash
state=${FAKE_POSTE_STATE:?}

if [ "${1:-}" = ps ]; then
    echo poste
    exit 0
fi

if [ "${1:-}" != exec ]; then
    exit 0
fi
shift
if [ "${1:-}" = --user=8 ]; then
    shift 2
else
    shift
fi

case "${1:-}" in
    php)
        case "$*" in
            *'domain:list'*) echo mcp.com ;;
            *'email:create'*"${FAKE_CREATE_FAIL_EMAIL:-__never__}"*) exit 1 ;;
            *'email:create'*|*'email:admin'*) exit 0 ;;
            *'email:list'*) printf 'mcpposte_admin@mcp.com\nfirst@mcp.com\n' ;;
        esac
        ;;
    sh)
        script=${3:-}
        case "$script" in
            *'/etc/dovecot/conf.d/10-auth.conf'*)
                echo config-write >> "$state/events"
                if grep -Eq '^[[:space:]]*auth_allow_cleartext[[:space:]]*=' "$state/10-auth.conf"; then
                    awk '
                        /^[[:space:]]*auth_allow_cleartext[[:space:]]*=/ {
                            print "auth_allow_cleartext = yes"
                            next
                        }
                        {print}
                    ' "$state/10-auth.conf" > "$state/10-auth.conf.tmp" \
                        && mv "$state/10-auth.conf.tmp" "$state/10-auth.conf"
                else
                    printf '\nauth_allow_cleartext = yes\n' >> "$state/10-auth.conf"
                fi
                ;;
            *) exit 0 ;;
        esac
        ;;
    doveconf)
        if [ "${FAKE_DOVECONF_OMIT:-0}" != 1 ]; then
            grep -E '^[[:space:]]*auth_allow_cleartext[[:space:]]*=' "$state/10-auth.conf" || true
        fi
        exit 0
        ;;
    doveadm)
        [ "${FAKE_RELOAD_FAIL:-0}" != 1 ]
        ;;
    pgrep)
        echo 101
        ;;
    kill)
        if [ "${2:-}" = -HUP ] && [ "${FAKE_RELOAD_FAIL:-0}" = 1 ]; then
            exit 1
        fi
        exit 0
        ;;
    *) exit 0 ;;
esac
EOF

cat > "$FAKE_BIN/bash" <<'EOF'
#!/bin/sh
if [ "${1:-}" = deployment/poste/scripts/create_users.sh ]; then
    state=${FAKE_POSTE_STATE:?}
    echo created > "$state/accounts-created"
    echo accounts-created >> "$state/events"
    exit 0
fi
exec /bin/bash "$@"
EOF

cat > "$FAKE_BIN/sleep" <<'EOF'
#!/bin/sh
exit 0
EOF

cat > "$FAKE_BIN/timeout" <<'EOF'
#!/bin/sh
shift
exec "$@"
EOF

cat > "$FAKE_BIN/nc" <<'EOF'
#!/bin/sh
request=$(cat)
state=${FAKE_POSTE_STATE:?}

if printf '%s\n' "$request" | grep -q '^a1 CAPABILITY'; then
    count=$(cat "$state/ready-attempts" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$state/ready-attempts"
    if [ "$count" -lt "${FAKE_DOVECOT_READY_AFTER:-1}" ]; then
        echo 'auth_allow_cleartext = no' > "$state/10-auth.conf"
        exit 1
    fi
    echo imap-ready >> "$state/events"
    printf '* OK [CAPABILITY IMAP4rev1] ready\r\na1 OK Capability completed\r\na2 OK Logout completed\r\n'
    exit 0
fi

printf '%s\n' "$request" | grep -q '^a1 LOGIN "' || exit 9
printf '%s\n' "$request" | grep -q '^a2 LOGOUT' || exit 9
echo probed > "$state/imap-probed"
echo imap-login >> "$state/events"
if [ "${FAKE_IMAP_AUTH:-success}" = success ]; then
    printf '* OK ready\r\na1 OK Logged in\r\n* BYE Logging out\r\na2 OK Logout completed\r\n'
else
    printf '* OK ready\r\na1 NO Authentication failed\r\na2 BAD Not authenticated\r\n'
fi
EOF

chmod +x "$FAKE_BIN"/*

run_config() {
    local name=$1
    shift
    STATE="$TEST_ROOT/$name"
    mkdir -p "$STATE"
    [ -f "$STATE/10-auth.conf" ] || echo '# no explicit auth setting' > "$STATE/10-auth.conf"
    : > "$STATE/events"
    (
        cd "$REPO_ROOT" || exit 99
        env PATH="$FAKE_BIN:$PATH" FAKE_POSTE_STATE="$STATE" "$@" \
            /bin/bash "$SETUP_SCRIPT" config
    ) > "$STATE/output" 2>&1
    CONFIG_RC=$?
}

STATE="$TEST_ROOT/startup-delay"
mkdir -p "$STATE"
echo 'auth_allow_cleartext = yes' > "$STATE/10-auth.conf"
run_config startup-delay env FAKE_DOVECOT_READY_AFTER=2
assert_success "configuration waits through Poste startup" "$CONFIG_RC"
if [ "$(cat "$STATE/ready-attempts")" -eq 2 ] \
    && [ "$(tail -n 1 "$STATE/10-auth.conf")" = 'auth_allow_cleartext = yes' ]; then
    pass "late startup rewrite is corrected after IMAP is ready"
else
    fail "late startup rewrite is corrected after IMAP is ready"
fi
ready_line=$(grep -n '^imap-ready$' "$STATE/events" | cut -d: -f1)
config_line=$(grep -n '^config-write$' "$STATE/events" | cut -d: -f1)
if [ -n "$ready_line" ] && [ -n "$config_line" ] && [ "$ready_line" -lt "$config_line" ]; then
    pass "Dovecot is ready before auth config is written"
else
    fail "Dovecot is ready before auth config is written"
fi

run_config missing-line env
assert_success "absent auth setting is configured" "$CONFIG_RC"
if [ "$(grep -Fxc 'auth_allow_cleartext = yes' "$STATE/10-auth.conf")" -eq 1 ]; then
    pass "absent auth setting is appended exactly once"
else
    fail "absent auth setting is appended exactly once"
fi

run_config missing-line env
assert_success "repeated configuration succeeds" "$CONFIG_RC"
if [ "$(grep -Fxc 'auth_allow_cleartext = yes' "$STATE/10-auth.conf")" -eq 1 ]; then
    pass "repeated configuration remains idempotent"
else
    fail "repeated configuration remains idempotent"
fi

run_config ineffective env FAKE_DOVECONF_OMIT=1
assert_failure "missing effective doveconf setting fails closed" "$CONFIG_RC"

run_config reload-failure env FAKE_RELOAD_FAIL=1
assert_failure "failed Dovecot reload fails setup" "$CONFIG_RC"

run_start() {
    local name=$1 auth=$2
    local root="$TEST_ROOT/$name/repo"
    STATE="$TEST_ROOT/$name/state"
    mkdir -p "$root/deployment/poste/scripts" "$root/configs" "$STATE"
    cp "$SETUP_SCRIPT" "$root/deployment/poste/scripts/setup.sh"
    echo 'auth_allow_cleartext = no' > "$STATE/10-auth.conf"
    cat > "$root/configs/users_data.json" <<'EOF'
{"users":[{"email":"fixture@mcp.com","password":"fixture-secret"}]}
EOF
    (
        cd "$root" || exit 99
        env PATH="$FAKE_BIN:$PATH" FAKE_POSTE_STATE="$STATE" FAKE_IMAP_AUTH="$auth" \
            /bin/bash deployment/poste/scripts/setup.sh start
    ) > "$TEST_ROOT/$name/output" 2>&1
    START_RC=$?
}

run_start auth-success success
assert_success "authenticated IMAP readiness passes" "$START_RC"
if [ -f "$STATE/accounts-created" ] && [ -f "$STATE/imap-probed" ]; then
    account_line=$(grep -n '^accounts-created$' "$STATE/events" | cut -d: -f1)
    login_line=$(grep -n '^imap-login$' "$STATE/events" | cut -d: -f1)
    if [ -n "$account_line" ] && [ -n "$login_line" ] && [ "$account_line" -lt "$login_line" ]; then
        pass "IMAP login is probed after account creation"
    else
        fail "IMAP login is probed after account creation"
    fi
else
    fail "IMAP login is probed after account creation"
fi
if grep -Fq 'fixture-secret' "$TEST_ROOT/auth-success/output"; then
    fail "IMAP probe does not print fixture password"
else
    pass "IMAP probe does not print fixture password"
fi

run_start auth-failure failure
assert_failure "rejected IMAP login fails readiness" "$START_RC"

ROOT="$TEST_ROOT/partial-account/repo"
STATE="$TEST_ROOT/partial-account/state"
mkdir -p "$ROOT/deployment/poste/scripts" "$ROOT/configs" "$STATE"
cp "$CREATE_USERS_SCRIPT" "$ROOT/deployment/poste/scripts/create_users.sh"
cat > "$ROOT/configs/users_data.json" <<'EOF'
{"users":[
  {"id":1,"first_name":"First","last_name":"User","full_name":"First User","email":"first@mcp.com","password":"first-password"},
  {"id":2,"first_name":"Second","last_name":"User","full_name":"Second User","email":"second@mcp.com","password":"second-password"}
]}
EOF
(
    cd "$ROOT" || exit 99
    env PATH="$FAKE_BIN:$PATH" FAKE_POSTE_STATE="$STATE" \
        FAKE_CREATE_FAIL_EMAIL=second@mcp.com \
        /bin/bash deployment/poste/scripts/create_users.sh 2
) > "$TEST_ROOT/partial-account/output" 2>&1
CREATE_USERS_RC=$?
assert_failure "a later mailbox creation failure fails the batch" "$CREATE_USERS_RC"

echo "1..$((PASS_COUNT + FAIL_COUNT))"
echo "# $PASS_COUNT passed; $FAIL_COUNT failed"
[ "$FAIL_COUNT" -eq 0 ]
