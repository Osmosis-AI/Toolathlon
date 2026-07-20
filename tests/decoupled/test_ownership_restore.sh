#!/usr/bin/env bash
# Regression tests for the ownership-restoration flow in
# scripts/run_single_decoupled.sh.
#
# Preprocess runs as container root and writes into the bind-mounted output
# tree (/workspace/dumps).  The runner must hand ownership back to the
# pre-preprocess owner:
#   1. even when preprocess fails,
#   2. without clobbering the preprocess exit code,
#   3. through a best-effort pass in cleanup() that first QUIESCES any
#      container-side writer (killing the local `docker exec` client does
#      not kill the exec'd process inside the container), then restarts the
#      inert container and restores ownership -- covering SIGINT/SIGTERM.
#
# The container runtime (docker/podman) and the host-side `uv` probes are
# replaced with fakes from tests/decoupled/fake_bin; no real containers are
# involved.  The fakes record the commands the runner issues, and the tests
# assert on that recorded event sequence plus the runner's exit code.  See
# fake_bin/docker for the client/container signal-semantics model.
#
# Corner-case -> coverage map:
#   C1 preprocess fails, exec client returns normally
#        -> s1 (restored via the main flow; cleanup's pending path must NOT
#           re-enter: no `start` event)
#   C2 restoration fails after a failed preprocess
#        -> s2 (warning only; preprocess exit code preserved; cleanup
#           retries through the quiesced stop -> start -> chown path)
#   C3 restoration fails after a successful preprocess
#        -> s3 (fatal: the host agent may not run on root-owned output)
#   C4 group-delivered SIGTERM/SIGINT kills the exec client while the
#      container-side process keeps writing (the reviewer's race; produced
#      in production by run_parallel.py's killpg on task timeout)
#        -> s4/s5: `stop-writer-alive` proves the writer survived the
#           group signal; first `stop` precedes the cleanup chown
#           (quiesce-before-restore); `start` sits between them; the last
#           `inner-write` precedes the cleanup chown (the race is closed)
#   C5 run_parallel.py escalates SIGTERM to SIGKILL after only 3 seconds
#        -> s4/s5: the pending path's FIRST container action is `stop`
#           with `-t 0` (no 10s grace for a `sleep` PID 1 that cannot
#           receive it; an accepted stop completes daemon-side even if the
#           client is later SIGKILLed).  The SIGKILL truncation itself is
#           not directly testable (a SIGKILLed bash leaves no state to
#           assert), so the mitigation is structural: ordering + timeout
#           are asserted here, and the next-run self-heal is C8.
#   C6 container restart fails after the quiesce stop
#        -> s6 (warning only; container still stopped and removed; the
#           interruption exit code is preserved)
#   C7 successful hand-off (the healthy path)
#        -> s7 (exactly one chown -- the main-flow hand-off -- and the
#           pending path is never entered: no `start` event)
#   C8 SIGKILL / double-Ctrl-C strands root-owned files with no cleanup
#        -> not directly testable (no process left to observe); bounded by
#           design: the restoration is always a full-tree `chown -R
#           <owner> /workspace/dumps` (argument shape asserted in s1), so
#           the next successful run of the same output folder self-heals
#           anything a killed cleanup left behind
#   Out of scope (documented follow-up, per review agreement): root-owned
#   files written after the restoration point by task `process_command`
#   background services or by the eval phase; both are the same family
#   ("container-side root writers that outlive the hand-off") and need a
#   separate end-of-run restoration pass.
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
    # A failed scenario may leave a detached inner writer running (it is
    # normally killed by the fake `stop`); reap it so nothing outlives the
    # test run.
    for dir in ${KEEP_DIRS[@]+"${KEEP_DIRS[@]}"}; do
        if [ -f "$dir/state/inner_writer_pid" ]; then
            kill -9 "$(cat "$dir/state/inner_writer_pid")" 2>/dev/null
        fi
    done
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
    # $1 (optional) container-start behavior for the fake runtime.
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
        FAKE_START_BEHAVIOR="${1:-succeed}" \
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

assert_event_subsequence() {
    # $1 label; remaining args are event prefixes that must appear in the
    # events file in the given order (as a subsequence: other events may be
    # interleaved).  Prefix matching is literal, no regex.
    local label=$1; shift
    local lineno=0 prefix next
    for prefix in "$@"; do
        next=$(awk -v start="$lineno" -v pat="$prefix" \
            'NR > start && index($0, pat) == 1 {print NR; exit}' \
            "$STATE/events" 2>/dev/null)
        if [ -z "$next" ]; then
            fail "$label (no '$prefix' after line $lineno; see $SCENARIO_DIR)"
            return
        fi
        lineno=$next
    done
    pass "$label"
}

assert_no_event() {
    # $1 label, $2 event prefix that must not appear
    local n
    n=$(count_events "$2")
    if [ "${n:-0}" -eq 0 ]; then
        pass "$1"
    else
        fail "$1 ('$2' appeared $n time(s); see $SCENARIO_DIR)"
    fi
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
# C1: preprocess fails but its exec client RETURNS, so the container-side
# process is already gone -- the main flow restores ownership directly and
# cleanup's pending path (quiesce + restart) must not re-enter.  The
# full-tree `chown -R <owner> /workspace/dumps` argument shape asserted here
# is also the C8 self-heal: it is what collects files stranded by an
# unkillable-cleanup (SIGKILL) on the next successful run.
note "scenario 1: preprocess failure still restores ownership"
new_workdir
run_runner "fail:7" "succeed" "4242:4242"
assert_eq "s1: preprocess exit code is preserved" "7" "$RUNNER_EXIT"
assert_log_contains "s1: failure is reported" "Preprocess failed, exit code: 7"
assert_event_order "s1: ownership restored after preprocess" \
    "preprocess-start" "chown -R -- "
assert_event_order "s1: ownership restored before container stop" \
    "chown -R -- " "stop"
assert_eq "s1: restoration is a full-tree pass with the captured owner" \
    "1" "$(count_events "chown -R -- 4242:4242 /workspace/dumps")"
assert_eq "s1: restoration is not repeated by cleanup" \
    "1" "$(count_events "chown -R -- ")"
assert_no_event "s1: cleanup's pending path is not entered" "start"

# ---------------------------------------------------------------------------
# C2: the main-flow restoration itself fails after a failed preprocess.  The
# preprocess exit code stays authoritative and cleanup retries -- through the
# quiesced path (stop first, then restart, then chown), since after any
# abnormal flow a container-side writer cannot be ruled out.
note "scenario 2: failed restoration cannot mask the preprocess exit code"
new_workdir
run_runner "fail:7" "fail" "1000:1000"
assert_eq "s2: preprocess exit code survives a failed chown" "7" "$RUNNER_EXIT"
assert_log_contains "s2: restoration failure is only a warning" \
    "Warning: could not restore output ownership after failed preprocess"
assert_eq "s2: cleanup retries the pending restoration" \
    "2" "$(count_events "chown -R -- ")"
assert_event_subsequence "s2: the cleanup retry goes through the quiesced path" \
    "preprocess-start" "chown -R -- " "stop -t 0" "start" "chown -R -- " "stop"

# ---------------------------------------------------------------------------
# C3: restoration fails after a SUCCESSFUL preprocess -- fatal, because the
# host agent must never run against root-owned output.  Cleanup still makes
# its best-effort quiesced retry.
note "scenario 3: successful preprocess still fails hard on a failed chown"
new_workdir
run_runner "succeed" "fail" "1000:1000"
assert_eq "s3: failed hand-off after successful preprocess is fatal" \
    "1" "$RUNNER_EXIT"
assert_log_contains "s3: hand-off failure is reported" \
    "Failed to hand output ownership to the host agent"
assert_eq "s3: cleanup still retries the pending restoration" \
    "2" "$(count_events "chown -R -- ")"
assert_event_subsequence "s3: the cleanup retry is quiesced too" \
    "chown -R -- " "stop -t 0" "start" "chown -R -- "

# ---------------------------------------------------------------------------
# C4 + C5 (s4 SIGTERM / s5 SIGINT).  A group-delivered fatal signal kills
# the local exec CLIENT, but the container-side process survives it and
# keeps writing (`inner-write` events) until the container is torn down —
# the exact race observed in review on rootful Docker, and what
# run_parallel.py's killpg produces on task timeout.  Cleanup must
# therefore quiesce first: its pending path's FIRST container action is
# `stop -t 0` (C5: only a ~3s window exists before the orchestrator's
# SIGKILL; a `sleep` PID 1 never honors a graceful stop, so any longer
# timeout is pure loss), then restart the inert container, then chown.
# `stop-writer-alive` proves the writer outlived the group signal, and the
# last `inner-write` preceding the restoration proves the race is closed.
# Each scenario also asserts the signal's conventional 128+N exit code (a
# 137 here means the watchdog had to SIGKILL a runner that ignored the
# signal).
signal_scenario() {
    local sig=$1 expected_exit=$2
    note "scenario SIG$sig: interruption quiesces the writer, then restores"
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
    assert_event_subsequence \
        "SIG$sig: writer survives the signal; quiesce precedes restoration" \
        "preprocess-start" "inner-write" "stop-writer-alive" "stop -t 0" \
        "start" "chown -R -- 1000:1000 /workspace/dumps" "stop"
    assert_last_event_order \
        "SIG$sig: nothing is written after the restoration" \
        "inner-write" "chown -R -- "
    assert_eq "SIG$sig: exactly one restoration attempt" \
        "1" "$(count_events "chown -R -- ")"
    assert_eq "SIG$sig: the writer is dead by the second (final) stop" \
        "1" "$(count_events "stop-writer-alive")"
    assert_eq "SIG$sig: the container is restarted exactly once" \
        "1" "$(count_events "start")"
    if [ ! -e "$STATE/running" ]; then
        pass "SIG$sig: container was stopped by cleanup"
    else
        fail "SIG$sig: container still running after cleanup (see $SCENARIO_DIR)"
    fi
    if [ ! -e "$STATE/created" ]; then
        pass "SIG$sig: container was removed by cleanup"
    else
        fail "SIG$sig: container still present after cleanup (see $SCENARIO_DIR)"
    fi
}

signal_scenario TERM 143
signal_scenario INT 130

# ---------------------------------------------------------------------------
# C6: the restart after the quiesce stop fails.  Restoration is best-effort
# on this path — the runner must warn, keep the interruption exit code, and
# still stop/remove the container.  No chown can happen (the container
# never came back), so the stranded files are left for the next successful
# run's full-tree hand-off (C8) — but the writer is still dead, which is
# the part that must never fail.
note "scenario 6: failed restart degrades to a warning, writer still dead"
new_workdir
start_runner_own_pgroup fail
if ! wait_for_file "$STATE/preprocess_started" 300; then
    fail "s6: runner never reached preprocess (see $SCENARIO_DIR)"
    kill -KILL -- "-$RUNNER_PID" 2>/dev/null
    wait "$RUNNER_PID" 2>/dev/null
else
    sleep 0.5
    kill -TERM -- "-$RUNNER_PID" 2>/dev/null
    ( sleep 15 && kill -KILL -- "-$RUNNER_PID" ) >/dev/null 2>&1 &
    s6_watchdog=$!
    wait "$RUNNER_PID" 2>/dev/null
    s6_exit=$?
    kill "$s6_watchdog" 2>/dev/null
    wait "$s6_watchdog" 2>/dev/null
    assert_eq "s6: interruption exit code survives the failed restart" \
        "143" "$s6_exit"
    assert_event_subsequence "s6: quiesce still happens before the failed restart" \
        "stop-writer-alive" "stop -t 0" "start"
    assert_no_event "s6: no restoration without a running container" \
        "chown -R -- "
    assert_log_contains "s6: the failure is reported as a warning" \
        "Warning: could not restore output ownership"
    if [ ! -e "$STATE/created" ]; then
        pass "s6: container was still removed"
    else
        fail "s6: container still present after cleanup (see $SCENARIO_DIR)"
    fi
fi

# ---------------------------------------------------------------------------
# C7: successful hand-off.  The main flow restores ownership exactly once
# and clears the pending flag, so cleanup's quiesce/restart path must never
# run — the fix may not tax the healthy path.  The fake `docker cp` does
# not materialize the bundle file, so the runner exits at the `chmod 600`
# guard right AFTER the hand-off — the exact moment cleanup's non-pending
# behavior is observable, without mocking the whole agent/eval pipeline.
note "scenario 7: successful hand-off never re-enters the pending path"
new_workdir
run_runner "succeed" "succeed" "1000:1000"
assert_log_contains "s7: the hand-off path completed" "Preprocess completed"
assert_eq "s7: exactly one restoration (the main-flow hand-off)" \
    "1" "$(count_events "chown -R -- ")"
assert_no_event "s7: cleanup's pending path is not entered" "start"
assert_event_order "s7: hand-off precedes the final stop" \
    "chown -R -- " "stop"

# ---------------------------------------------------------------------------
echo ""
echo "passed: $PASS_COUNT, failed: $FAIL_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
