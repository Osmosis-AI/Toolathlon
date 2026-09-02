#!/usr/bin/env bash

set -u

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/toolathlon-run-parallel-exit.XXXXXX")
FAKE_BIN="$TEST_ROOT/bin"
DUMP_PATH="$TEST_ROOT/dump"
CONFIG_FILE="$TEST_ROOT/config.json"

trap 'rm -rf -- "$TEST_ROOT"' EXIT
mkdir -p "$FAKE_BIN"
printf '{}\n' > "$CONFIG_FILE"

cat > "$FAKE_BIN/uv" <<'EOF'
#!/bin/sh
if [ "${1:-}" = run ] && [ "${2:-}" = run_parallel.py ]; then
    exit 37
fi
exit 0
EOF

cat > "$FAKE_BIN/shuf" <<'EOF'
#!/bin/sh
echo 1234
EOF

chmod +x "$FAKE_BIN/uv" "$FAKE_BIN/shuf"

PATH="$FAKE_BIN:$PATH" bash "$REPO_ROOT/scripts/run_parallel.sh" \
    test-model "$DUMP_PATH" unified 1 test-image "$CONFIG_FILE" \
    > /dev/null 2>&1
status=$?

if [ "$status" -ne 37 ]; then
    echo "expected run_parallel.sh to exit 37, got $status" >&2
    exit 1
fi

echo "ok - run_parallel.sh propagates the runner exit code"
