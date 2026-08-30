import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


_FIXTURE_PATH = Path(__file__).resolve()
_EXPECTED_ROLES = {"parent", "child", "grandchild"}
_FIXTURE_MODES = {
    "--fixture-parent",
    "--fixture-child",
    "--fixture-grandchild",
}


def _record_pid(state_path: Path, role: str) -> None:
    with state_path.open("a", encoding="utf-8") as state_file:
        state_file.write(f"{role}:{os.getpid()}\n")
        state_file.flush()
        os.fsync(state_file.fileno())


def _wait_forever() -> None:
    while True:
        signal.pause()


def _ignore_soft_shutdown_signals() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def _run_fixture_grandchild(state_path: Path) -> None:
    _ignore_soft_shutdown_signals()
    _record_pid(state_path, "grandchild")
    _wait_forever()


def _run_fixture_child(state_path: Path) -> None:
    _ignore_soft_shutdown_signals()
    _record_pid(state_path, "child")
    subprocess.Popen(
        [sys.executable, str(_FIXTURE_PATH), "--fixture-grandchild", str(state_path)]
    )
    _wait_forever()


def _run_fixture_parent(state_path: Path) -> None:
    _record_pid(state_path, "parent")
    subprocess.Popen(
        [sys.executable, str(_FIXTURE_PATH), "--fixture-child", str(state_path)]
    )
    _wait_forever()


def _read_fixture_pids(state_path: Path) -> dict[str, int]:
    if not state_path.exists():
        return {}

    pids: dict[str, int] = {}
    for line in state_path.read_text(encoding="utf-8").splitlines():
        role, raw_pid = line.split(":", 1)
        pids[role] = int(raw_pid)
    return pids


async def _wait_for_fixture_tree(state_path: Path, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if set(_read_fixture_pids(state_path)) == _EXPECTED_ROLES:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("fixture process tree did not start")


async def _cancel_stdio_server(state_path: Path) -> float:
    from agents.mcp import MCPServerStdio

    server = MCPServerStdio(
        name="stdio-process-tree-fixture",
        params={
            "command": sys.executable,
            "args": [str(_FIXTURE_PATH), "--fixture-parent", str(state_path)],
        },
    )

    tree_ready = asyncio.Event()

    async def run_server() -> None:
        async with server.create_streams():
            await _wait_for_fixture_tree(state_path)
            tree_ready.set()
            await asyncio.Future()

    server_task = asyncio.create_task(run_server())
    await asyncio.wait_for(tree_ready.wait(), timeout=6.0)

    started = time.monotonic()
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    return time.monotonic() - started


def _run_driver(state_path: Path) -> None:
    elapsed = asyncio.run(_cancel_stdio_server(state_path))
    print(json.dumps({"cleanup_elapsed_seconds": elapsed}), flush=True)


def _matching_fixture_processes(state_path: Path):
    import psutil

    fixture_path = str(_FIXTURE_PATH)
    unique_state_path = str(state_path)
    matches = {}

    def add_if_fixture(process, *, require_inspection: bool = False) -> None:
        try:
            command = process.cmdline()
        except (psutil.AccessDenied, PermissionError) as exc:
            if require_inspection:
                raise RuntimeError(
                    f"cannot safely inspect recorded fixture PID {process.pid}"
                ) from exc
            return
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return
        if (
            fixture_path in command
            and unique_state_path in command
            and _FIXTURE_MODES.intersection(command)
        ):
            matches[process.pid] = process

    try:
        for process in psutil.process_iter():
            add_if_fixture(process)
    except (psutil.Error, PermissionError):
        # The recorded PID fallback below is enough once the full fixture tree
        # has started, and every fixture records before spawning its child.
        pass

    for pid in _read_fixture_pids(state_path).values():
        if pid in matches:
            continue
        try:
            add_if_fixture(psutil.Process(pid), require_inspection=True)
        except psutil.NoSuchProcess:
            continue

    return list(matches.values())


def _wait_for_fixture_processes_to_exit(
    state_path: Path, timeout_seconds: float
) -> list[int]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _matching_fixture_processes(state_path):
            return []
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return [process.pid for process in _matching_fixture_processes(state_path)]


def _kill_fixture_processes(state_path: Path, timeout_seconds: float = 2.0) -> list[int]:
    import psutil

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        processes = _matching_fixture_processes(state_path)
        if not processes:
            return []
        for process in processes:
            try:
                process.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        psutil.wait_procs(
            processes,
            timeout=min(0.2, max(0.0, deadline - time.monotonic())),
        )
    return [process.pid for process in _matching_fixture_processes(state_path)]


def _kill_owned_process(process) -> None:
    import psutil

    try:
        process.kill()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        pass


def _stop_driver(driver: subprocess.Popen, driver_process, timeout_seconds: float = 2.0) -> bool:
    _kill_owned_process(driver_process)
    try:
        driver.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


@unittest.skipUnless(os.name == "posix", "process-group regression is POSIX-specific")
class MCPStdioTeardownTests(unittest.TestCase):
    def test_cancellation_terminates_stubborn_descendant_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "fixture-pids.txt"
            driver = subprocess.Popen(
                [sys.executable, str(_FIXTURE_PATH), "--driver", str(state_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            import psutil

            driver_process = psutil.Process(driver.pid)

            try:
                try:
                    stdout, stderr = driver.communicate(timeout=12.0)
                except subprocess.TimeoutExpired:
                    driver_stopped = _stop_driver(driver, driver_process)
                    cleanup_remaining = _kill_fixture_processes(state_path)
                    try:
                        stdout, stderr = driver.communicate(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        stdout, stderr = "", "driver pipes did not close after cleanup"
                    self.fail(
                        "MCP stdio cancellation did not finish within 12 seconds; "
                        f"fixture_pids={_read_fixture_pids(state_path)}, "
                        f"driver_stopped={driver_stopped}, "
                        f"cleanup_remaining={cleanup_remaining}, stderr={stderr!r}"
                    )

                self.assertEqual(driver.returncode, 0, stderr)
                result = json.loads(stdout)
                self.assertLess(result["cleanup_elapsed_seconds"], 10.0)

                fixture_pids = _read_fixture_pids(state_path)
                self.assertEqual(set(fixture_pids), _EXPECTED_ROLES)
                remaining = _wait_for_fixture_processes_to_exit(state_path, 2.0)
                self.assertEqual(remaining, [], f"fixture processes still running: {remaining}")
            finally:
                driver_stopped = _stop_driver(driver, driver_process)
                cleanup_remaining = _kill_fixture_processes(state_path)
                remaining = _wait_for_fixture_processes_to_exit(state_path, 2.0)
                self.assertTrue(driver_stopped, "fixture driver did not stop during cleanup")
                self.assertEqual(
                    remaining,
                    [],
                    "fixture cleanup left running processes: "
                    f"{remaining}; first cleanup snapshot: {cleanup_remaining}",
                )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-parent":
        _run_fixture_parent(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--fixture-child":
        _run_fixture_child(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--fixture-grandchild":
        _run_fixture_grandchild(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--driver":
        _run_driver(Path(sys.argv[2]))
    else:
        unittest.main()
