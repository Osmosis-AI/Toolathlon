#!/usr/bin/env bash
# Regression tests for the ownership-restoration flow in
# scripts/run_single_decoupled.sh.
#
# Preprocess runs as container root and writes into the bind-mounted output
# tree (/workspace/dumps).  The runner must hand ownership back to the
# pre-preprocess owner:
#   1. even when preprocess fails,
#   2. without clobbering the preprocess exit code,
#   3. through a best-effort retry in cleanup() before the container is
#      stopped, which also covers SIGINT/SIGTERM interruptions.
#
# The container runtime (docker/podman) and the host-side `uv` probes are
# replaced with fakes from tests/decoupled/fake_bin; no real containers are
# involved.  The fakes record the commands the runner issues, and the tests
# assert on that recorded sequence (preprocess -> chown -> stop) plus the
# runner's exit code.
#
# Requirements: Linux or any GNU userland (the runner itself depends on GNU
# `readlink -f` semantics).  Run from anywhere:
#   bash tests/decoupled/test_ownership_restore.sh

set -u

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TESTS_DIR/../.." && pwd)"
RUNNER="$REPO_ROOT/scripts/run_single_decoupled.sh"
FAKE_BIN="$TESTS_DIR/fake_bin"

PASS_COUNT=0
FAIL_COUNT=0
KEEP_DIRS=()

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "ok - $1"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "not ok - $1"
}

note() {
    echo "# $*"
}

on_exit() {
    if [ "$FAIL_COUNT" -eq 0 ] && [ "${KEEP_TEST_ARTIFACTS:-0}" != "1" ]; then
        for dir in ${KEEP_DIRS[@]+"${KEEP_DIRS[@]}"}; do
            rm -rf -- "$dir"
        done
    else
        for dir in ${KEEP_DIRS[@]+"${KEEP_DIRS[@]}"}; do
            note "artifacts kept in $dir"
        done
    fi
}
trap on_exit EXIT

if [ ! -f "$RUNNER" ]; then
    echo "runner not found: $RUNNER" >&2
    exit 1
fi

# Any real task directory satisfies the runner's existence check; the fake
# runtime never copies it anywhere.
TASK_DIR_REL=$(
    cd "$REPO_ROOT/tasks" 2>/dev/null &&
    find . -mindepth 2 -maxdepth 2 -type d |
    sed 's|^\./||' |
    grep -E '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' |
    LC_ALL=C sort |
    head -n 1
)
if [ -z "$TASK_DIR_REL" ]; then
    echo "no task directory found under $REPO_ROOT/tasks" >&2
    exit 1
fi
note "using task directory: $TASK_DIR_REL"

new_workdir() {
    SCENARIO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/toolathlon-ownership-test.XXXXXX")
    KEEP_DIRS+=("$SCENARIO_DIR")
    STATE="$SCENARIO_DIR/state"
    DUMPS="$SCENARIO_DIR/dumps"
    mkdir -p "$STATE" "$DUMPS"
}

build_runner_cmd() {
    # Positional gateway_port avoids the runner's live-socket probe.
    RUNNER_CMD=(
        bash "$RUNNER" "$TASK_DIR_REL" normal "$DUMPS" fake-model unified 5
        scripts/formal_run_v0.json fake-image toolathlon_default 18999
    )
}

run_runner() {
    # $1 preprocess behavior, $2 chown behavior, $3 reported dumps owner
    build_runner_cmd
    env PATH="$FAKE_BIN:$PATH" \
        FAKE_RUNTIME_STATE="$STATE" \
        FAKE_PREPROCESS_BEHAVIOR="$1" \
        FAKE_CHOWN_BEHAVIOR="$2" \
        FAKE_DUMPS_OWNER="$3" \
        "${RUNNER_CMD[@]}" > "$STATE/runner.log" 2>&1
    RUNNER_EXIT=$?
}

start_runner_own_pgroup() {
    # Launch the runner as its own process-group leader so a signal can be
    # delivered to the whole group, mirroring Ctrl-C / an orchestrator kill.
    # A non-job-control shell starts async children with SIGINT/SIGQUIT
    # ignored, the ignore survives both exec and setsid, and an ignored
    # signal would make kill -INT a silent no-op (the scenario would then
    # pass vacuously once the fake's hang expires).  The perl wrapper
    # restores the default dispositions before entering the new group.
    build_runner_cmd
    env PATH="$FAKE_BIN:$PATH" \
        FAKE_RUNTIME_STATE="$STATE" \
        FAKE_PREPROCESS_BEHAVIOR="hang" \
        FAKE_CHOWN_BEHAVIOR="succeed" \
        FAKE_DUMPS_OWNER="1000:1000" \
        perl -e '$SIG{INT} = "DEFAULT"; $SIG{QUIT} = "DEFAULT"; setpgrp(0, 0); exec @ARGV; die "exec failed: $!"' \
        "${RUNNER_CMD[@]}" > "$STATE/runner.log" 2>&1 &
    RUNNER_PID=$!
}

wait_for_file() {
    # $1 path, $2 max iterations of 0.2s
    local i=0
    while [ "$i" -lt "$2" ]; do
        [ -e "$1" ] && return 0
        sleep 0.2
        i=$((i + 1))
    done
    return 1
}

event_lineno() {
    # First line number in the events file matching prefix $1; empty if none.
    grep -n "^$1" "$STATE/events" 2>/dev/null | head -n 1 | cut -d: -f1
}

event_last_lineno() {
    # Last line number in the events file matching prefix $1; empty if none.
    grep -n "^$1" "$STATE/events" 2>/dev/null | tail -n 1 | cut -d: -f1
}

count_events() {
    grep -c "^$1" "$STATE/events" 2>/dev/null || true
}

assert_eq() {
    # $1 label, $2 expected, $3 actual
    if [ "$2" = "$3" ]; then
        pass "$1"
    else
        fail "$1 (expected '$2', got '$3'; see $SCENARIO_DIR)"
    fi
}

assert_log_contains() {
    # $1 label, $2 fixed string expected in runner.log
    if grep -qF "$2" "$STATE/runner.log"; then
        pass "$1"
    else
        fail "$1 (missing '$2' in $STATE/runner.log)"
    fi
}

assert_event_order() {
    # $1 label, $2 earlier event prefix, $3 later event prefix
    local earlier later
    earlier=$(event_lineno "$2")
    later=$(event_lineno "$3")
    if [ -n "$earlier" ] && [ -n "$later" ] && [ "$earlier" -lt "$later" ]; then
        pass "$1"
    else
        fail "$1 (lines: '$2'=${earlier:-absent}, '$3'=${later:-absent}; see $SCENARIO_DIR)"
    fi
}

assert_last_event_order() {
    # Like assert_event_order, but pins the LAST occurrence of $2 before $3.
    local earlier later
    earlier=$(event_last_lineno "$2")
    later=$(event_lineno "$3")
    if [ -n "$earlier" ] && [ -n "$later" ] && [ "$earlier" -lt "$later" ]; then
        pass "$1"
    else
        fail "$1 (lines: last '$2'=${earlier:-absent}, '$3'=${later:-absent}; see $SCENARIO_DIR)"
    fi
}

# ---------------------------------------------------------------------------
note "scenario 1: preprocess failure still restores ownership"
new_workdir
run_runner "fail:7" "succeed" "4242:4242"
assert_eq "s1: preprocess exit code is preserved" "7" "$RUNNER_EXIT"
assert_log_contains "s1: failure is reported" "Preprocess failed, exit code: 7"
assert_event_order "s1: ownership restored after preprocess" \
    "preprocess-start" "chown -R -- "
assert_event_order "s1: ownership restored before container stop" \
    "chown -R -- " "stop"
assert_eq "s1: restoration uses the captured owner" \
    "1" "$(count_events "chown -R -- 4242:4242 /workspace/dumps")"
assert_eq "s1: restoration is not repeated by cleanup" \
    "1" "$(count_events "chown -R -- ")"

# ---------------------------------------------------------------------------
note "scenario 2: failed restoration cannot mask the preprocess exit code"
new_workdir
run_runner "fail:7" "fail" "1000:1000"
assert_eq "s2: preprocess exit code survives a failed chown" "7" "$RUNNER_EXIT"
assert_log_contains "s2: restoration failure is only a warning" \
    "Warning: could not restore output ownership after failed preprocess"
assert_eq "s2: cleanup retries the pending restoration" \
    "2" "$(count_events "chown -R -- ")"
assert_last_event_order "s2: the cleanup retry happens before container stop" \
    "chown -R -- " "stop"

# ---------------------------------------------------------------------------
note "scenario 3: successful preprocess still fails hard on a failed chown"
new_workdir
run_runner "succeed" "fail" "1000:1000"
assert_eq "s3: failed hand-off after successful preprocess is fatal" \
    "1" "$RUNNER_EXIT"
assert_log_contains "s3: hand-off failure is reported" \
    "Failed to hand output ownership to the host agent"
assert_eq "s3: cleanup still retries the pending restoration" \
    "2" "$(count_events "chown -R -- ")"

# ---------------------------------------------------------------------------
# Interruption coverage.  A group-delivered fatal signal kills the hung
# preprocess exec and makes the runner abort through its EXIT trap, so the
# restoration must come from cleanup()'s pending path — for SIGTERM and
# SIGINT alike.  Each scenario asserts the signal's conventional 128+N exit
# code (a 137 here means the watchdog had to SIGKILL a runner that ignored
# the signal), and that ownership is restored exactly once, before the
# container is stopped.
signal_scenario() {
    local sig=$1 expected_exit=$2
    note "scenario SIG$sig: interruption still restores ownership"
    new_workdir
    start_runner_own_pgroup
    if ! wait_for_file "$STATE/preprocess_started" 300; then
        fail "SIG$sig: runner never reached preprocess (see $SCENARIO_DIR)"
        kill -KILL -- "-$RUNNER_PID" 2>/dev/null
        wait "$RUNNER_PID" 2>/dev/null
        return
    fi
    sleep 0.5  # let the runner block inside the hung preprocess exec
    kill "-$sig" -- "-$RUNNER_PID" 2>/dev/null
    # Bound the wait: if the signal were ever ignored again, the watchdog
    # SIGKILLs the group, wait returns 137, and the exit-code assertion
    # fails fast instead of riding out the fake's hang.
    ( sleep 15 && kill -KILL -- "-$RUNNER_PID" ) >/dev/null 2>&1 &
    local watchdog=$!
    wait "$RUNNER_PID" 2>/dev/null
    local observed_exit=$?
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
    assert_eq "SIG$sig: runner exits with the signal's status" \
        "$expected_exit" "$observed_exit"
    assert_event_order "SIG$sig: ownership restored after interruption" \
        "preprocess-start" "chown -R -- "
    assert_event_order "SIG$sig: restoration happens before container stop" \
        "chown -R -- " "stop"
    assert_eq "SIG$sig: exactly one restoration attempt" \
        "1" "$(count_events "chown -R -- ")"
    if [ ! -e "$STATE/running" ]; then
        pass "SIG$sig: container was stopped by cleanup"
    else
        fail "SIG$sig: container still running after cleanup (see $SCENARIO_DIR)"
    fi
}

signal_scenario TERM 143
signal_scenario INT 130

# ---------------------------------------------------------------------------
echo ""
echo "passed: $PASS_COUNT, failed: $FAIL_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
