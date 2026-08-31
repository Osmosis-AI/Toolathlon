#!/usr/bin/env bash
# Hermetic regression coverage for deploy_containers.sh failure diagnostics.

set -u

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$TEST_DIR/.." && pwd)
DEPLOY_SCRIPT="$REPO_ROOT/global_preparation/deploy_containers.sh"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/toolathlon-deploy-diagnostics.XXXXXX")
FAKE_BIN="$TEST_ROOT/fake-bin"
PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "ok - $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "not ok - $1"; }

assert_contains() {
    local label=$1 file=$2 text=$3
    if grep -Fq -- "$text" "$file"; then pass "$label"; else fail "$label"; fi
}

assert_not_contains() {
    local label=$1 file=$2 text=$3
    if grep -Fq -- "$text" "$file"; then fail "$label"; else pass "$label"; fi
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
    *) echo -diag ;;
esac
EOF

cat > "$FAKE_BIN/bash" <<'EOF'
#!/bin/sh
state=${FAKE_DEPLOY_STATE:?}
script=${1:-}
case "$script" in
    deployment/k8s/scripts/setup.sh) component=k8s ;;
    deployment/canvas/scripts/setup.sh) component=canvas ;;
    deployment/poste/scripts/setup.sh) component=poste ;;
    deployment/woocommerce/scripts/setup.sh) component=woo ;;
    *) exec /bin/bash "$@" ;;
esac

if [ "${2:-start}" = stop ]; then
    echo "pre-stop:$component" >> "$state/events"
    exit 0
fi

count_file="$state/$component.count"
count=$(cat "$count_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$count_file"
[ "$count" -gt 1 ] && echo "retry-cleanup:$component:$count" >> "$state/events"
echo "setup-start:$component:$count" >> "$state/events"

case "$component" in
    k8s) rcs=$FAKE_K8S_RCS ;;
    canvas) rcs=$FAKE_CANVAS_RCS ;;
    poste) rcs=$FAKE_POSTE_RCS ;;
    woo) rcs=$FAKE_WOO_RCS ;;
esac
rc=$(printf '%s\n' "$rcs" | cut -d, -f"$count")
[ -n "$rc" ] || rc=0

echo "$component-HEAD-attempt-$count"
if [ "$rc" -ne 0 ]; then
    echo "OAuth callback: https://oauth.example/callback?code=oauth-value-730"
    echo "Authorization: Bearer auth-value-731"
    echo "TOKEN = token-value-732"
    echo "CLIENT_SECRET: client-value-733"
    echo 'export SAFE_MESSAGE="quoted-value-734 with spaces"'
    echo "PLAIN_ENV = assignment-value-735"
    echo '{"config":{"value":"json-value-736"}}'
    echo "api-key: api-value-737"
    echo "Password: password-value-738"
    echo "Cookie: cookie-value-739"
    echo "credential: credential-value-740"
    echo "public URL https://public.example/path?value=url-value-741"
    echo "private key: private-value-742"
    echo "access_key = access-value-743"
fi
i=1
while [ "$i" -le 100 ]; do
    printf '%s-MIDDLE-%03d-abcdefghijklmnopqrstuvwxyz0123456789\n' "$component" "$i"
    i=$((i + 1))
done
echo "$component-TAIL-attempt-$count"
exit "$rc"
EOF

cat > "$FAKE_BIN/docker" <<'EOF'
#!/bin/sh
state=${FAKE_DEPLOY_STATE:?}
echo "docker:$*" >> "$state/events"
case "${1:-}" in
    inspect)
        if [ "${2:-}" = --format ]; then echo "exited exit=17"; fi
        ;;
    exec) echo "fake-node Ready control-plane" ;;
esac
exit 0
EOF

cat > "$FAKE_BIN/kind" <<'EOF'
#!/bin/sh
state=${FAKE_DEPLOY_STATE:?}
echo "kind:$*" >> "$state/events"
if [ "${1:-} ${2:-}" = "get clusters" ]; then
    echo cluster-diag1
    echo unrelated-cluster
fi
EOF

cat > "$FAKE_BIN/sysctl" <<'EOF'
#!/bin/sh
state=${FAKE_DEPLOY_STATE:?}
key=${2:-}
echo "sysctl:$key" >> "$state/events"
case "$key" in
    fs.inotify.max_user_watches) echo 1048576 ;;
    fs.inotify.max_user_instances) echo 16384 ;;
    fs.inotify.max_queued_events) echo 32768 ;;
    user.max_user_namespaces) echo 10000 ;;
    *) echo unexpected >&2; exit 9 ;;
esac
EOF

cat > "$FAKE_BIN/timeout" <<'EOF'
#!/bin/sh
shift
exec "$@"
EOF

cat > "$FAKE_BIN/curl" <<'EOF'
#!/bin/sh
[ "${FAKE_PROBES_FAIL:-0}" = 1 ] && exit 1
case "$*" in
    *api/v1/accounts*) echo '{"status":"ready"}' ;;
    *) printf '200' ;;
esac
EOF

cat > "$FAKE_BIN/date" <<'EOF'
#!/bin/sh
if [ "${1:-}" = +%s ] && [ "${FAKE_DATE_STEP:-0}" -gt 0 ]; then
    state=${FAKE_DEPLOY_STATE:?}
    value=$(cat "$state/date.value" 2>/dev/null || echo 0)
    value=$((value + FAKE_DATE_STEP))
    echo "$value" > "$state/date.value"
    echo "$value"
    exit 0
fi
exec /bin/date "$@"
EOF

cat > "$FAKE_BIN/nc" <<'EOF'
#!/bin/sh
echo '* OK IMAP4rev1 ESMTP'
EOF

cat > "$FAKE_BIN/sleep" <<'EOF'
#!/bin/sh
exit 0
EOF

cat > "$FAKE_BIN/lsof" <<'EOF'
#!/bin/sh
exit 1
EOF

chmod +x "$FAKE_BIN"/*

run_scenario() {
    local name=$1 k8s_rcs=$2 canvas_rcs=$3 poste_rcs=$4 woo_rcs=$5
    local probes_fail=${6:-0} date_step=${7:-0}
    SCENARIO="$TEST_ROOT/$name"
    mkdir -p "$SCENARIO"
    : > "$SCENARIO/events"
    (
        cd "$REPO_ROOT" || exit 99
        env PATH="$FAKE_BIN:$PATH" \
            FAKE_DEPLOY_STATE="$SCENARIO" \
            FAKE_K8S_RCS="$k8s_rcs" \
            FAKE_CANVAS_RCS="$canvas_rcs" \
            FAKE_POSTE_RCS="$poste_rcs" \
            FAKE_WOO_RCS="$woo_rcs" \
            FAKE_PROBES_FAIL="$probes_fail" \
            FAKE_DATE_STEP="$date_step" \
            /bin/bash "$DEPLOY_SCRIPT" true
    ) > "$SCENARIO/output" 2>&1
    SCENARIO_RC=$?
}

run_scenario sparse 11,11 0,0 33,33 0,0
if [ "$SCENARIO_RC" -eq 1 ]; then pass "failed deploy preserves exit 1"; else fail "failed deploy preserves exit 1"; fi
assert_contains "all component rc values are independent" "$SCENARIO/output" \
    "components: k8s=11 canvas=0 poste=33 woo=0"
assert_contains "failed k8s has a head excerpt" "$SCENARIO/output" "[k8s] log head: k8s-HEAD-attempt-1"
assert_contains "failed k8s has a tail excerpt" "$SCENARIO/output" "k8s-TAIL-attempt-1"
assert_contains "failed poste marks omitted middle" "$SCENARIO/output" "[poste] log middle: omitted"
assert_not_contains "zero canvas has no excerpt" "$SCENARIO/output" "[canvas] log head:"
assert_not_contains "zero woo has no excerpt" "$SCENARIO/output" "[woo] log head:"
assert_not_contains "unbounded log middle is omitted" "$SCENARIO/output" "k8s-MIDDLE-050"
assert_contains "sensitive lines become whole-line markers" "$SCENARIO/output" \
    "<sensitive-line-redacted>"
for leaked_value in \
    oauth-value-730 \
    auth-value-731 \
    token-value-732 \
    client-value-733 \
    "quoted-value-734 with spaces" \
    assignment-value-735 \
    json-value-736 \
    api-value-737 \
    password-value-738 \
    cookie-value-739 \
    credential-value-740 \
    url-value-741 \
    private-value-742 \
    access-value-743; do
    assert_not_contains "redacts $leaked_value" "$SCENARIO/output" "$leaked_value"
done
assert_not_contains "redacts OAuth URL host" "$SCENARIO/output" "oauth.example"
assert_not_contains "redacts remaining URL host" "$SCENARIO/output" "public.example"

diagnostic_done_line=$(grep -n '^sysctl:user.max_user_namespaces$' "$SCENARIO/events" | head -n 1 | cut -d: -f1)
retry_cleanup_line=$(grep -n '^retry-cleanup:' "$SCENARIO/events" | head -n 1 | cut -d: -f1)
if [ -n "$diagnostic_done_line" ] && [ -n "$retry_cleanup_line" ] \
    && [ "$diagnostic_done_line" -lt "$retry_cleanup_line" ]; then
    pass "diagnostics finish before retry cleanup"
else
    fail "diagnostics finish before retry cleanup"
fi

assert_contains "exact Kind logical/container status" "$SCENARIO/output" \
    "kind: logical_cluster=cluster-diag1 status=present container=cluster-diag1-control-plane status=exited exit=17"
assert_contains "formatted inspect targets only the exact Kind container" "$SCENARIO/events" \
    "docker:inspect --format {{.State.Status}} exit={{.State.ExitCode}} cluster-diag1-control-plane"

actual_sysctls=$(sed -n 's/^sysctl://p' "$SCENARIO/events" | LC_ALL=C sort -u)
expected_sysctls=$(printf '%s\n' \
    fs.inotify.max_queued_events \
    fs.inotify.max_user_instances \
    fs.inotify.max_user_watches \
    user.max_user_namespaces)
if [ "$actual_sysctls" = "$expected_sysctls" ]; then
    pass "only the four requested sysctls are captured"
else
    fail "only the four requested sysctls are captured"
fi

run_scenario all-fail 11,11 22,22 33,33 44,44
final_block=$(awk '
    /^=== setup diagnostics attempt=2 ===$/ {capture=1}
    capture {print}
    /^=== setup diagnostics end ===$/ && capture {exit}
' "$SCENARIO/output")
final_bytes=$(printf '%s\n' "$final_block" | wc -c | tr -d ' ')
if [ "$final_bytes" -le 1600 ]; then pass "all-failure block is at most 1600 bytes"; else fail "all-failure block is at most 1600 bytes"; fi
for component in k8s canvas poste woo; do
    if printf '%s\n' "$final_block" | grep -Fq "[$component] log head:" \
        && printf '%s\n' "$final_block" | grep -Fq "[$component] log tail:"; then
        pass "$component survives the shared excerpt budget"
    else
        fail "$component survives the shared excerpt budget"
    fi
done
tail -c 2000 "$SCENARIO/output" > "$SCENARIO/stdout-tail"
assert_contains "final block header survives the monolith tail" "$SCENARIO/stdout-tail" \
    "=== setup diagnostics attempt=2 ==="
assert_contains "final block end survives the monolith tail" "$SCENARIO/stdout-tail" \
    "=== setup diagnostics end ==="
assert_contains "terminal error follows final diagnostics" "$SCENARIO/stdout-tail" \
    "ERROR: one or more setup.sh scripts reported failure after 2 attempts. Giving up."

run_scenario readiness-timeout 0,0 0,0 0,0 0,0 1 1000
if [ "$SCENARIO_RC" -eq 1 ]; then pass "readiness timeout preserves exit 1"; else fail "readiness timeout preserves exit 1"; fi
assert_contains "readiness timeout captures all zero setup rc" "$SCENARIO/output" \
    "components: k8s=0 canvas=0 poste=0 woo=0"
assert_contains "readiness timeout captures both attempts" "$SCENARIO/output" \
    "=== setup diagnostics attempt=2 ==="
assert_not_contains "readiness timeout has no component excerpts" "$SCENARIO/output" \
    "log head:"
assert_contains "readiness timeout reaches final failure" "$SCENARIO/output" \
    "ERROR: services not ready in time after 2 attempts. Giving up."
diagnostic_done_line=$(grep -n '^sysctl:user.max_user_namespaces$' "$SCENARIO/events" | head -n 1 | cut -d: -f1)
retry_cleanup_line=$(grep -n '^retry-cleanup:' "$SCENARIO/events" | head -n 1 | cut -d: -f1)
if [ -n "$diagnostic_done_line" ] && [ -n "$retry_cleanup_line" ] \
    && [ "$diagnostic_done_line" -lt "$retry_cleanup_line" ]; then
    pass "readiness diagnostics finish before retry cleanup"
else
    fail "readiness diagnostics finish before retry cleanup"
fi
final_diagnostic_line=$(grep -n '^=== setup diagnostics end ===$' "$SCENARIO/output" | tail -n 1 | cut -d: -f1)
final_error_line=$(grep -n '^ERROR: services not ready in time' "$SCENARIO/output" | tail -n 1 | cut -d: -f1)
if [ -n "$final_diagnostic_line" ] && [ -n "$final_error_line" ] \
    && [ "$final_diagnostic_line" -lt "$final_error_line" ]; then
    pass "final readiness diagnostics precede exit"
else
    fail "final readiness diagnostics precede exit"
fi

run_scenario success 0 0 0 0
if [ "$SCENARIO_RC" -eq 0 ]; then pass "successful deploy still exits 0"; else fail "successful deploy still exits 0"; fi
assert_contains "success still replays complete component logs" "$SCENARIO/output" "woo-MIDDLE-050"
assert_contains "success still reaches readiness" "$SCENARIO/output" "Deploy attempt 1 succeeded."
assert_not_contains "success emits no failure diagnostics" "$SCENARIO/output" "=== setup diagnostics"
assert_not_contains "success performs no diagnostic sysctl capture" "$SCENARIO/events" "sysctl:"
assert_not_contains "success does not retry" "$SCENARIO/output" "Deploy attempt 2"

echo "1..$((PASS_COUNT + FAIL_COUNT))"
echo "# $PASS_COUNT passed; $FAIL_COUNT failed"
[ "$FAIL_COUNT" -eq 0 ]
