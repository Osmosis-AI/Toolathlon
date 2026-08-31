#!/usr/bin/env bash
# Hermetic regression coverage for Kubernetes CLI bootstrap failures.

set -u

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$TEST_DIR/.." && pwd)
PREPARE_SCRIPT="$REPO_ROOT/deployment/k8s/scripts/prepare.sh"
MINIMAL_INSTALLER="$REPO_ROOT/global_preparation/install_env_minimal.sh"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/toolathlon-k8s-prepare.XXXXXX")
FAKE_BIN="$TEST_ROOT/fake-bin"
PINNED_KIND_BIN="$TEST_ROOT/pinned-kind-bin"
PINNED_KUBECTL_BIN="$TEST_ROOT/pinned-kubectl-bin"
PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "ok - $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "not ok - $1"; }

assert_eq() {
    local label=$1 expected=$2 actual=$3
    if [ "$actual" = "$expected" ]; then pass "$label"; else fail "$label (expected $expected, got $actual)"; fi
}

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

mkdir -p "$FAKE_BIN" "$PINNED_KIND_BIN" "$PINNED_KUBECTL_BIN"

cat > "$PINNED_KIND_BIN/kind" <<'EOF'
#!/bin/sh
echo 'kind v0.20.0 go1.20.4 linux/amd64'
EOF

cat > "$PINNED_KUBECTL_BIN/kubectl" <<'EOF'
#!/bin/sh
printf '%s\n' 'clientVersion:' '  gitVersion: v1.27.3'
EOF

cat > "$FAKE_BIN/curl" <<'EOF'
#!/bin/sh
state=${FAKE_PREPARE_STATE:?}
printf '%s\n' "$*" >> "$state/curl.args"
output=
url=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) output=$2; shift 2 ;;
        http*) url=$1; shift ;;
        *) shift ;;
    esac
done
[ "${FAKE_CURL_FAIL:-0}" = 1 ] && exit 22
case "$url" in
    */kind-linux-amd64)
        cat > "$output" <<'PAYLOAD'
#!/bin/sh
if [ "${1:-}" = version ]; then
    printf '%s\n' 'kind v0.20.0 go1.20.4 linux/amd64'
    exit 0
fi
exit 2
PAYLOAD
        ;;
    */kubectl)
        cat > "$output" <<'PAYLOAD'
#!/bin/sh
if [ "${1:-}" = version ]; then
    printf '%s\n' 'clientVersion:' '  gitVersion: v1.27.3'
    exit 0
fi
exit 2
PAYLOAD
        ;;
    *) exit 23 ;;
esac
EOF

cat > "$FAKE_BIN/sha256sum" <<'EOF'
#!/bin/sh
state=${FAKE_PREPARE_STATE:?}
input=$(cat)
printf '%s\n' "$input" >> "$state/checksum.input"
[ "${FAKE_CHECKSUM_FAIL:-0}" = 1 ] && exit 1
case "$input" in
    513a7213d6d3332dd9ef27c24dab35e5ef10a04fa27274fe1c14d8a246493ded\ \ *) exit 0 ;;
    fba6c062e754a120bc8105cde1344de200452fe014a8759e06e4eec7ed258a09\ \ *) exit 0 ;;
    *) exit 2 ;;
esac
EOF

cat > "$FAKE_BIN/timeout" <<'EOF'
#!/bin/sh
[ "${1:-}" = 10s ] || exit 98
shift
exec "$@"
EOF

chmod +x "$PINNED_KIND_BIN/kind" "$PINNED_KUBECTL_BIN/kubectl" \
    "$FAKE_BIN/curl" "$FAKE_BIN/sha256sum" "$FAKE_BIN/timeout"

for COMMAND_NAME in cat grep install mkdir mktemp rm; do
    ln -s "$(command -v "$COMMAND_NAME")" "$FAKE_BIN/$COMMAND_NAME"
done

run_prepare() {
    local name=$1 curl_fail=${2:-0} checksum_fail=${3:-0} existing_kubectl=${4:-0}
    local kind_mode=${5:-pinned} path_mode=${6:-normal}
    local kind_path= kubectl_path= scenario_path=
    SCENARIO="$TEST_ROOT/$name"
    mkdir -p "$SCENARIO/home/bin" "$SCENARIO/tmp"
    if [ "$existing_kubectl" = 1 ]; then
        cat > "$SCENARIO/home/bin/kubectl" <<'EOF'
#!/bin/sh
printf '%s\n' 'clientVersion:' '  gitVersion: v1.34.1'
EOF
        chmod +x "$SCENARIO/home/bin/kubectl"
    elif [ "$existing_kubectl" = 2 ]; then
        kubectl_path="$PINNED_KUBECTL_BIN:"
    fi
    case "$kind_mode" in
        pinned) kind_path="$PINNED_KIND_BIN:" ;;
        stale)
            cat > "$SCENARIO/home/bin/kind" <<'EOF'
#!/bin/sh
printf '%s\n' 'kind v0.19.0 go1.19 linux/amd64'
EOF
            chmod +x "$SCENARIO/home/bin/kind"
            ;;
        absent) ;;
        *) echo "unknown kind mode: $kind_mode" >&2; exit 98 ;;
    esac
    case "$path_mode" in
        normal) scenario_path="$SCENARIO/home/bin:${kubectl_path}${kind_path}$FAKE_BIN" ;;
        no_home) scenario_path="${kubectl_path}${kind_path}$FAKE_BIN" ;;
        shadow)
            mkdir -p "$SCENARIO/shadow-bin"
            cat > "$SCENARIO/shadow-bin/kind" <<'EOF'
#!/bin/sh
printf '%s\n' 'kind v0.19.0 go1.19 linux/amd64'
EOF
            chmod +x "$SCENARIO/shadow-bin/kind"
            scenario_path="$SCENARIO/shadow-bin:$SCENARIO/home/bin:${kubectl_path}${kind_path}$FAKE_BIN"
            ;;
        *) echo "unknown PATH mode: $path_mode" >&2; exit 97 ;;
    esac
    : > "$SCENARIO/curl.args"
    : > "$SCENARIO/checksum.input"
    (
        env PATH="$scenario_path" \
            HOME="$SCENARIO/home" \
            TMPDIR="$SCENARIO/tmp" \
            FAKE_PREPARE_STATE="$SCENARIO" \
            FAKE_CURL_FAIL="$curl_fail" \
            FAKE_CHECKSUM_FAIL="$checksum_fail" \
            /bin/bash "$PREPARE_SCRIPT" --no-sudo
    ) > "$SCENARIO/output" 2>&1
    SCENARIO_RC=$?
}

run_prepare success
assert_eq "kind-present bootstrap succeeds" 0 "$SCENARIO_RC"
assert_contains "kubectl download uses the Kubernetes 1.27.3 pin" "$SCENARIO/curl.args" \
    "https://dl.k8s.io/release/v1.27.3/bin/linux/amd64/kubectl"
assert_not_contains "pinned kind from another PATH entry is accepted" "$SCENARIO/curl.args" \
    "kind-linux-amd64"
assert_contains "kubectl download uses fail-follow redirects" "$SCENARIO/curl.args" "-fL"
assert_contains "kubectl download has bounded retries" "$SCENARIO/curl.args" "--retry 3"
assert_contains "kubectl checksum uses the fixed digest" "$SCENARIO/checksum.input" \
    "fba6c062e754a120bc8105cde1344de200452fe014a8759e06e4eec7ed258a09"
assert_contains "kubectl success is printed after validation" "$SCENARIO/output" \
    "kubectl has been installed to: $SCENARIO/home/bin/kubectl"
if [ -x "$SCENARIO/home/bin/kubectl" ]; then
    pass "kubectl is executable"
else
    fail "kubectl is executable"
fi
KUBECTL_MODE=$(stat -c '%a' "$SCENARIO/home/bin/kubectl" 2>/dev/null || stat -f '%Lp' "$SCENARIO/home/bin/kubectl")
assert_eq "kubectl is installed with mode 0755" 755 "$KUBECTL_MODE"

run_prepare pinned_tools_in_other_paths 0 0 2
assert_eq "pinned tools in other PATH entries are accepted" 0 "$SCENARIO_RC"
assert_contains "pinned kubectl path is reported" "$SCENARIO/output" \
    "kubectl is already installed at: $PINNED_KUBECTL_BIN/kubectl"
if [ ! -s "$SCENARIO/curl.args" ]; then
    pass "pinned tools in other PATH entries are not downloaded"
else
    fail "pinned tools in other PATH entries are not downloaded"
fi

run_prepare stale_kind 0 0 0 stale
assert_eq "stale kind is replaced successfully" 0 "$SCENARIO_RC"
assert_contains "stale kind is identified" "$SCENARIO/output" \
    "kind at $SCENARIO/home/bin/kind is not v0.20.0; replacing it."
assert_contains "stale kind replacement downloads the exact pin" "$SCENARIO/curl.args" \
    "https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64"
assert_contains "kind checksum uses the fixed digest" "$SCENARIO/checksum.input" \
    "513a7213d6d3332dd9ef27c24dab35e5ef10a04fa27274fe1c14d8a246493ded"
STALE_KIND_REPLACEMENT_PATH=$(PATH="$SCENARIO/home/bin:$FAKE_BIN" command -v kind)
assert_eq "replacement is the kind selected from PATH" "$SCENARIO/home/bin/kind" "$STALE_KIND_REPLACEMENT_PATH"
STALE_KIND_REPLACEMENT_VERSION=$(PATH="$SCENARIO/home/bin:$FAKE_BIN" kind version)
if printf '%s\n' "$STALE_KIND_REPLACEMENT_VERSION" | grep -Eq \
    '^kind[[:space:]]+v0\.20\.0([[:space:]]|$)'; then
    pass "stale kind replacement reports v0.20.0"
else
    fail "stale kind replacement reports v0.20.0"
fi

run_prepare shadowed_kind 0 0 0 absent shadow
if [ "$SCENARIO_RC" -ne 0 ]; then pass "shadowed kind replacement is nonzero"; else fail "shadowed kind replacement is nonzero"; fi
assert_contains "shadowed kind failure reports the selected command" "$SCENARIO/output" \
    "PATH selects $SCENARIO/shadow-bin/kind."
assert_not_contains "shadowed kind failure has no false success" "$SCENARIO/output" \
    "kind has been installed"

run_prepare home_bin_missing_from_path 0 0 0 pinned no_home
if [ "$SCENARIO_RC" -ne 0 ]; then pass "unreachable kubectl replacement is nonzero"; else fail "unreachable kubectl replacement is nonzero"; fi
assert_contains "unreachable kubectl reports no selected command" "$SCENARIO/output" \
    "PATH selects no kubectl command."
assert_contains "unreachable kubectl keeps a PATH hint" "$SCENARIO/output" \
    'export PATH="$HOME/bin:$PATH"'
assert_not_contains "unreachable kubectl has no false success" "$SCENARIO/output" \
    "kubectl has been installed"

run_prepare stale_version 0 0 1
assert_eq "stale kubectl is replaced successfully" 0 "$SCENARIO_RC"
assert_contains "stale kubectl is identified" "$SCENARIO/output" \
    "kubectl at $SCENARIO/home/bin/kubectl is not v1.27.3; replacing it."
assert_contains "stale kubectl replacement downloads the exact pin" "$SCENARIO/curl.args" \
    "https://dl.k8s.io/release/v1.27.3/bin/linux/amd64/kubectl"
STALE_REPLACEMENT_PATH=$(PATH="$SCENARIO/home/bin:$FAKE_BIN" command -v kubectl)
assert_eq "replacement is the kubectl selected from PATH" "$SCENARIO/home/bin/kubectl" "$STALE_REPLACEMENT_PATH"
STALE_REPLACEMENT_VERSION=$(PATH="$SCENARIO/home/bin:$FAKE_BIN" kubectl version --client --output=yaml)
if printf '%s\n' "$STALE_REPLACEMENT_VERSION" | grep -Eq \
    '^[[:space:]]*gitVersion:[[:space:]]*v1\.27\.3[[:space:]]*$'; then
    pass "stale kubectl replacement reports v1.27.3"
else
    fail "stale kubectl replacement reports v1.27.3"
fi

run_prepare download_failure 1
if [ "$SCENARIO_RC" -ne 0 ]; then pass "kubectl download failure is nonzero"; else fail "kubectl download failure is nonzero"; fi
assert_contains "kubectl download failure is diagnosed" "$SCENARIO/output" "Failed to download kubectl v1.27.3."
assert_not_contains "kubectl download failure has no false success" "$SCENARIO/output" "kubectl has been installed"

run_prepare checksum_failure 0 1
if [ "$SCENARIO_RC" -ne 0 ]; then pass "kubectl checksum failure is nonzero"; else fail "kubectl checksum failure is nonzero"; fi
assert_contains "kubectl checksum failure is diagnosed" "$SCENARIO/output" "Checksum verification failed for kubectl v1.27.3."
assert_not_contains "kubectl checksum failure has no false success" "$SCENARIO/output" "kubectl has been installed"
if [ ! -e "$SCENARIO/home/bin/kubectl" ]; then
    pass "checksum failure does not install kubectl"
else
    fail "checksum failure does not install kubectl"
fi

MINIMAL_ROOT="$TEST_ROOT/minimal-repo"
MINIMAL_BIN="$TEST_ROOT/minimal-bin"
MINIMAL_STATE="$TEST_ROOT/minimal-state"
mkdir -p "$MINIMAL_ROOT/global_preparation" "$MINIMAL_ROOT/configs" \
    "$MINIMAL_ROOT/deployment/k8s/scripts" "$MINIMAL_BIN" "$MINIMAL_STATE/home"
cp "$MINIMAL_INSTALLER" "$MINIMAL_ROOT/global_preparation/install_env_minimal.sh"
: > "$MINIMAL_ROOT/configs/global_configs_example.py"
: > "$MINIMAL_ROOT/configs/token_key_session_example.py"

cat > "$MINIMAL_ROOT/deployment/k8s/scripts/prepare.sh" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" > "${FAKE_MINIMAL_STATE:?}/prepare.args"
exit 37
EOF

cat > "$MINIMAL_BIN/uv" <<'EOF'
#!/bin/sh
exit 0
EOF

cat > "$MINIMAL_BIN/node" <<'EOF'
#!/bin/sh
echo 'v22.16.0'
EOF

cat > "$MINIMAL_BIN/npm" <<'EOF'
#!/bin/sh
if [ "${1:-}" = -v ]; then echo '11.4.1'; fi
exit 0
EOF

chmod +x "$MINIMAL_ROOT/deployment/k8s/scripts/prepare.sh" "$MINIMAL_BIN"/*
(
    cd "$MINIMAL_ROOT" || exit 99
    env PATH="$MINIMAL_BIN:/usr/bin:/bin" \
        HOME="$MINIMAL_STATE/home" \
        FAKE_MINIMAL_STATE="$MINIMAL_STATE" \
        /bin/bash global_preparation/install_env_minimal.sh true
) > "$MINIMAL_STATE/output" 2>&1
MINIMAL_RC=$?

assert_eq "minimal installer propagates prepare status" 37 "$MINIMAL_RC"
assert_contains "minimal installer invokes sudo preparation" "$MINIMAL_STATE/prepare.args" "--sudo"
assert_contains "minimal installer diagnoses prepare failure" "$MINIMAL_STATE/output" \
    "Kubernetes tooling preparation failed (exit 37)."
assert_not_contains "minimal installer stops before inotify configuration" "$MINIMAL_STATE/output" \
    "Configuring inotify directly via sudo"

echo "# passed: $PASS_COUNT"
echo "# failed: $FAIL_COUNT"
if [ "$FAIL_COUNT" -ne 0 ]; then
    exit 1
fi
