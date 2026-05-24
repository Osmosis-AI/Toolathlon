"""Run every test module in ``tests/v3_state_hygiene/`` and report a tally.

Each subordinate test module exits 0 on all-pass, non-zero otherwise.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = Path(__file__).resolve().parent

MODULES = [
    "tests.v3_state_hygiene.test_probe",
    "tests.v3_state_hygiene.test_periodic_reset",
    "tests.v3_state_hygiene.test_cleanup_helpers",
    "tests.v3_state_hygiene.test_v3_endpoints",
]


def main() -> int:
    print("#" * 72)
    print(f"#  Toolathlon v3 state-hygiene suite — {len(MODULES)} modules")
    print("#" * 72)
    total_fails = 0
    t_total = time.monotonic()
    for mod in MODULES:
        print()
        t0 = time.monotonic()
        proc = subprocess.run(
            ["uv", "run", "python", "-m", mod],
            cwd=str(PROJECT_ROOT),
        )
        dt = time.monotonic() - t0
        if proc.returncode != 0:
            total_fails += 1
            print(f"  >>> {mod} FAILED (exit={proc.returncode}, {dt:.2f}s)")
        else:
            print(f"  >>> {mod} PASSED ({dt:.2f}s)")
    print()
    print("#" * 72)
    print(f"#  total: {len(MODULES) - total_fails}/{len(MODULES)} modules passed "
          f"in {(time.monotonic() - t_total):.2f}s")
    print("#" * 72)
    return 0 if total_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
