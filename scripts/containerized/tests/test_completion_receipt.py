import hashlib
import hmac
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


helper_module = types.ModuleType("utils.general.helper")
helper_module.read_json = read_json
with patch.dict(sys.modules, {"utils.general.helper": helper_module}):
    import run_parallel as run_parallel_module

AsyncTaskScheduler = run_parallel_module.AsyncTaskScheduler
completion_receipt_path = run_parallel_module.completion_receipt_path
prepare_completion_receipt_dir = run_parallel_module.prepare_completion_receipt_dir
write_completion_receipt = run_parallel_module.write_completion_receipt
run_command_async = run_parallel_module.run_command_async


class CompletionReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.dump_path = Path(self.temporary.name)
        self.task_dir = self.dump_path / "finalpool" / "demo-task"
        self.task_dir.mkdir(parents=True)
        self.key = b"k" * 32
        self.key_path = self.dump_path / "receipt-key"
        self.key_path.write_bytes(self.key)
        self.environment = patch.dict(
            os.environ,
            {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": str(self.key_path)},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_writes_only_allowlisted_regular_artifacts(self) -> None:
        eval_data = b'{"pass": false}\n'
        (self.task_dir / "eval_res.json").write_bytes(eval_data)
        (self.task_dir / "run.log").write_text("finished\n", encoding="utf-8")
        (self.task_dir / "secret.txt").write_text("not listed", encoding="utf-8")

        receipt_path = write_completion_receipt(
            str(self.dump_path), "demo-task", self.task_dir, 17
        )

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["task_name"], "demo-task")
        self.assertEqual(receipt["producer_returncode"], 17)
        self.assertEqual(set(receipt["artifacts"]), {"eval_res.json", "run.log"})
        self.assertEqual(
            receipt["artifacts"]["eval_res.json"],
            {"sha256": hashlib.sha256(eval_data).hexdigest(), "size": len(eval_data)},
        )
        payload = {key: value for key, value in receipt.items() if key != "hmac_sha256"}
        canonical_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(
            receipt["hmac_sha256"],
            hmac.new(self.key, canonical_payload, hashlib.sha256).hexdigest(),
        )
        self.assertEqual(list(receipt_path.parent.glob("*.tmp")), [])

    def test_rejects_symlink_artifact_without_receipt(self) -> None:
        target = self.task_dir / "agent-output.json"
        target.write_text("{}", encoding="utf-8")
        (self.task_dir / "status.json").symlink_to(target)

        with self.assertRaises((OSError, ValueError)):
            write_completion_receipt(str(self.dump_path), "demo-task", self.task_dir, 0)

        self.assertFalse(
            completion_receipt_path(str(self.dump_path), "demo-task").exists()
        )

    def test_prepares_empty_receipt_directory(self) -> None:
        receipt_dir = prepare_completion_receipt_dir(str(self.dump_path / "empty"))

        self.assertTrue(receipt_dir.is_dir())
        self.assertEqual(list(receipt_dir.iterdir()), [])

    def test_missing_key_does_not_write_receipt(self) -> None:
        (self.task_dir / "run.log").write_text("finished\n", encoding="utf-8")

        with patch.dict(os.environ, {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": ""}):
            with self.assertRaises(ValueError):
                write_completion_receipt(
                    str(self.dump_path), "demo-task", self.task_dir, 0
                )

        self.assertFalse(
            completion_receipt_path(str(self.dump_path), "demo-task").exists()
        )


class CompletionReceiptSchedulerTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(
        run_parallel_module,
        "prepare_completion_receipt_dir",
        side_effect=OSError("read-only"),
    )
    async def test_main_fails_closed_when_receipt_directory_cannot_be_prepared(
        self, prepare_receipt_dir
    ) -> None:
        argv = [
            "run_parallel.py",
            "--tasks_folder",
            "finalpool",
            "--model_short_name",
            "model",
            "--provider",
            "provider",
            "--maxstep",
            "10",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaisesRegex(
                SystemExit, "could not prepare completion receipt directory"
            ):
                await run_parallel_module.main()

        prepare_receipt_dir.assert_called_once_with("dumps")

    @patch.object(run_parallel_module.os, "listdir", side_effect=OSError("stop"))
    @patch.object(run_parallel_module, "prepare_completion_receipt_dir")
    async def test_decoupled_main_does_not_require_receipt_directory(
        self, prepare_receipt_dir, _listdir
    ) -> None:
        argv = [
            "run_parallel.py",
            "--tasks_folder",
            "finalpool",
            "--model_short_name",
            "model",
            "--provider",
            "provider",
            "--maxstep",
            "10",
            "--runner",
            "decoupled",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaisesRegex(OSError, "stop"):
                await run_parallel_module.main()

        prepare_receipt_dir.assert_not_called()

    @patch.object(
        run_parallel_module.asyncio,
        "create_subprocess_shell",
        new_callable=AsyncMock,
    )
    async def test_key_path_is_not_passed_to_the_task_process(
        self, create_subprocess: AsyncMock
    ) -> None:
        process = MagicMock()
        process.stdout.readline = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)
        process.returncode = 0
        create_subprocess.return_value = process
        with tempfile.TemporaryDirectory() as dump_path:
            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/tmp/private-key"},
            ):
                await run_command_async("true", str(Path(dump_path) / "run.log"))

        child_environment = create_subprocess.await_args.kwargs["env"]
        self.assertNotIn("TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE", child_environment)

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_containerized_normal_nonzero_return_writes_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.return_value = {"returncode": 17, "success": False}
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)

            result = await scheduler._execute_task(
                "finalpool/demo-task",
                "tag",
                "model",
                "provider",
                "10",
                30,
                False,
                dump_path=dump_path,
            )

            self.assertEqual(result["status"], "success")
            write_receipt.assert_called_once_with(
                dump_path,
                "demo-task",
                Path(dump_path) / "finalpool" / "demo-task",
                17,
            )

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_unsafe_cleanup_return_does_not_write_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.return_value = {
            "returncode": run_parallel_module.UNSAFE_CLEANUP_EXIT_CODE,
            "success": False,
        }
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)

            result = await scheduler._execute_task(
                "finalpool/demo-task",
                "tag",
                "model",
                "provider",
                "10",
                30,
                False,
                dump_path=dump_path,
            )

        write_receipt.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(scheduler.completed_tasks, 0)
        self.assertEqual(scheduler.failed_tasks, 1)

    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_stale_receipt_unlink_failure_marks_task_failed(
        self, run_command: AsyncMock
    ) -> None:
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)
            with patch.object(
                run_parallel_module,
                "completion_receipt_path",
            ) as receipt_path:
                receipt_path.return_value.unlink.side_effect = OSError("read-only")

                result = await scheduler._execute_task(
                    "finalpool/demo-task",
                    "tag",
                    "model",
                    "provider",
                    "10",
                    30,
                    False,
                    dump_path=dump_path,
                )

        run_command.assert_not_awaited()
        self.assertEqual(result["status"], "failed")
        self.assertIn("failed to clear stale completion receipt", result["error"])
        self.assertEqual(scheduler.completed_tasks, 0)
        self.assertEqual(scheduler.failed_tasks, 1)

    @patch.object(
        run_parallel_module,
        "write_completion_receipt",
        side_effect=OSError("disk full"),
    )
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_receipt_failure_does_not_fail_task(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.return_value = {"returncode": 0, "success": True}
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)

            result = await scheduler._execute_task(
                "finalpool/demo-task",
                "tag",
                "model",
                "provider",
                "10",
                30,
                False,
                dump_path=dump_path,
            )

            self.assertEqual(result["status"], "success")
            write_receipt.assert_called_once()

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_timeout_and_exception_do_not_write_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        for error, expected_status in (
            (TimeoutError("timed out"), "timeout"),
            (RuntimeError("failed"), "failed"),
        ):
            with self.subTest(expected_status=expected_status):
                run_command.side_effect = error
                write_receipt.reset_mock()
                with tempfile.TemporaryDirectory() as dump_path:
                    scheduler = AsyncTaskScheduler(None, 1)
                    stale_receipt = completion_receipt_path(dump_path, "demo-task")
                    stale_receipt.write_text("stale", encoding="utf-8")

                    result = await scheduler._execute_task(
                        "finalpool/demo-task",
                        "tag",
                        "model",
                        "provider",
                        "10",
                        30,
                        False,
                        dump_path=dump_path,
                    )

                    self.assertEqual(result["status"], expected_status)
                    write_receipt.assert_not_called()
                    self.assertFalse(stale_receipt.exists())


class ContainerCleanupContractTests(unittest.TestCase):
    def test_cleanup_fails_closed_when_container_absence_is_unverified(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[2] / "run_single_containerized.sh"
        )
        script = script_path.read_text(encoding="utf-8")
        cleanup = script[
            script.index("cleanup() {") : script.index("trap cleanup EXIT")
        ]

        remove = cleanup.index('$CONTAINER_RUNTIME rm "$CONTAINER_NAME"')
        verify = cleanup.index(
            "remaining_container_ids=$($CONTAINER_RUNTIME ps -aq "
            '--filter "name=$CONTAINER_NAME" 2>/dev/null)'
        )
        self.assertLess(remove, verify)
        self.assertIn('if [ "$container_query_exit_code" -ne 0 ]', cleanup)
        self.assertIn('elif [ -n "$remaining_container_ids" ]', cleanup)
        self.assertEqual(
            cleanup.count("cleanup_exit_code=$UNSAFE_CLEANUP_EXIT_CODE"), 2
        )
        self.assertIn('exit "$cleanup_exit_code"', cleanup)
        self.assertIn("UNSAFE_CLEANUP_EXIT_CODE=200", script)


if __name__ == "__main__":
    unittest.main()
