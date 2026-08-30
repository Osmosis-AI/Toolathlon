import hashlib
import hmac
import json
import os
import subprocess
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


def completed_command(returncode: int = 0):
    async def _run(*_args, **kwargs):
        marker_name = (kwargs.get("extra_env") or {}).get(
            run_parallel_module.COMPLETION_CLEANUP_MARKER_ENV
        )
        if marker_name:
            Path(marker_name).write_text(
                run_parallel_module.COMPLETION_CLEANUP_MARKER_CONTENT,
                encoding="utf-8",
            )
        return {"returncode": returncode, "success": returncode == 0}

    return _run


async def timed_out_after_cleanup(*_args, **kwargs):
    marker_name = (kwargs.get("extra_env") or {}).get(
        run_parallel_module.COMPLETION_CLEANUP_MARKER_ENV
    )
    if marker_name:
        Path(marker_name).write_text(
            run_parallel_module.COMPLETION_CLEANUP_MARKER_CONTENT,
            encoding="utf-8",
        )
    raise TimeoutError("timed out")


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
        (self.task_dir / "status.json").write_text(
            json.dumps({"evaluation": False}), encoding="utf-8"
        )
        (self.task_dir / "run.log").write_text("finished\n", encoding="utf-8")
        (self.task_dir / "secret.txt").write_text("not listed", encoding="utf-8")

        receipt_path = write_completion_receipt(
            str(self.dump_path),
            "demo-task",
            self.task_dir,
            1,
            completion_kind=run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
        )

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["task_name"], "demo-task")
        self.assertEqual(
            receipt["completion_kind"],
            run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
        )
        self.assertEqual(receipt["producer_returncode"], 1)
        self.assertEqual(
            set(receipt["artifacts"]),
            {"eval_res.json", "status.json", "run.log"},
        )
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
            write_completion_receipt(
                str(self.dump_path),
                "demo-task",
                self.task_dir,
                0,
                completion_kind=run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
            )

        self.assertFalse(
            completion_receipt_path(str(self.dump_path), "demo-task").exists()
        )

    def test_refuses_to_sign_inconsistent_evaluator_evidence(self) -> None:
        (self.task_dir / "eval_res.json").write_text(
            json.dumps({"pass": True}), encoding="utf-8"
        )
        (self.task_dir / "status.json").write_text(
            json.dumps({"evaluation": False}), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "producer lifecycle"):
            write_completion_receipt(
                str(self.dump_path),
                "demo-task",
                self.task_dir,
                1,
                completion_kind=run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
            )

        self.assertFalse(
            completion_receipt_path(str(self.dump_path), "demo-task").exists()
        )

    def test_timeout_receipt_requires_the_producer_timeout_code(self) -> None:
        (self.task_dir / "status.json").write_text(
            json.dumps({"running": "timeout", "evaluation": None}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "producer lifecycle"):
            write_completion_receipt(
                str(self.dump_path),
                "demo-task",
                self.task_dir,
                17,
                completion_kind=(
                    run_parallel_module.COMPLETION_KIND_SCHEDULER_TIMEOUT
                ),
            )
        with self.assertRaisesRegex(ValueError, "producer lifecycle"):
            write_completion_receipt(
                str(self.dump_path),
                "demo-task",
                self.task_dir,
                run_parallel_module.TIMEOUT_EXIT_CODE,
                completion_kind=run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
            )

        receipt = write_completion_receipt(
            str(self.dump_path),
            "demo-task",
            self.task_dir,
            run_parallel_module.TIMEOUT_EXIT_CODE,
            completion_kind=run_parallel_module.COMPLETION_KIND_SCHEDULER_TIMEOUT,
        )
        self.assertTrue(receipt.is_file())

    def test_prepares_empty_receipt_directory(self) -> None:
        receipt_dir = prepare_completion_receipt_dir(str(self.dump_path / "empty"))

        self.assertTrue(receipt_dir.is_dir())
        self.assertEqual(list(receipt_dir.iterdir()), [])

    def test_missing_key_does_not_write_receipt(self) -> None:
        (self.task_dir / "run.log").write_text("finished\n", encoding="utf-8")

        with patch.dict(os.environ, {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": ""}):
            with self.assertRaises(ValueError):
                write_completion_receipt(
                    str(self.dump_path),
                    "demo-task",
                    self.task_dir,
                    0,
                    completion_kind=run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
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
        with patch.dict(
            os.environ,
            {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
            clear=True,
        ):
            with patch.object(sys, "argv", argv):
                with self.assertRaisesRegex(
                    SystemExit, "could not prepare signed completion receipts"
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

    @patch.object(run_parallel_module.os, "listdir", side_effect=OSError("stop"))
    @patch.object(run_parallel_module, "prepare_completion_receipt_dir")
    async def test_unsigned_containerized_main_does_not_require_receipt_directory(
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
        ]
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, "argv", argv):
                with self.assertRaisesRegex(OSError, "stop"):
                    await run_parallel_module.main()

        prepare_receipt_dir.assert_not_called()

    @patch.object(
        run_parallel_module.asyncio,
        "create_subprocess_exec",
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
                await run_command_async(["true"], str(Path(dump_path) / "run.log"))

        child_environment = create_subprocess.await_args.kwargs["env"]
        self.assertNotIn("TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE", child_environment)

    @patch.object(
        run_parallel_module.asyncio,
        "create_subprocess_exec",
        new_callable=AsyncMock,
    )
    async def test_command_arguments_are_not_reparsed_by_a_shell(
        self, create_subprocess: AsyncMock
    ) -> None:
        process = MagicMock()
        process.stdout.readline = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)
        process.returncode = 0
        create_subprocess.return_value = process
        metacharacters = "model; $(should-stay-data)"

        with tempfile.TemporaryDirectory() as dump_path:
            await run_command_async(
                ["printf", "%s", metacharacters],
                str(Path(dump_path) / "run.log"),
            )

        self.assertEqual(
            create_subprocess.await_args.args[:3],
            ("printf", "%s", metacharacters),
        )

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_containerized_normal_nonzero_return_writes_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.side_effect = completed_command(17)
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)

            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
                clear=True,
            ):
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
                completion_kind=run_parallel_module.COMPLETION_KIND_PROCESS_EXIT,
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

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_missing_positive_cleanup_marker_does_not_write_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.return_value = {"returncode": 0, "success": True}
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)
            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
                clear=True,
            ):
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
        self.assertIn("not positively verified", result["error"])

    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_stale_receipt_unlink_failure_marks_task_failed(
        self, run_command: AsyncMock
    ) -> None:
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)
            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
                clear=True,
            ):
                with patch.object(
                    run_parallel_module,
                    "_completion_receipt_path",
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
        self.assertIn("failed to revoke prior completion receipt", result["error"])
        self.assertEqual(scheduler.completed_tasks, 0)
        self.assertEqual(scheduler.failed_tasks, 1)

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_stale_artifact_after_archive_failure_stops_before_command(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        with tempfile.TemporaryDirectory() as dump_path:
            task_dir = Path(dump_path) / "finalpool" / "demo-task"
            task_dir.mkdir(parents=True)
            (task_dir / "eval_res.json").write_text('{"pass": true}', encoding="utf-8")
            scheduler = AsyncTaskScheduler(None, 1)

            with patch.object(
                run_parallel_module.shutil,
                "move",
                side_effect=OSError("read-only"),
            ):
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

            self.assertFalse(completion_receipt_path(dump_path, "demo-task").exists())

        run_command.assert_not_awaited()
        write_receipt.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertIn("eval_res.json", result["error"])

    @patch.object(
        run_parallel_module,
        "write_completion_receipt",
        side_effect=OSError("disk full"),
    )
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_unsigned_and_decoupled_runs_revoke_without_writing_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.return_value = {"returncode": 0, "success": True}
        for runner, environment in (
            ("containerized", {}),
            ("decoupled", {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/key"}),
        ):
            with self.subTest(runner=runner), tempfile.TemporaryDirectory() as dump_path:
                scheduler = AsyncTaskScheduler(None, 1)
                stale_receipt = completion_receipt_path(dump_path, "demo-task")
                stale_receipt.write_text("stale", encoding="utf-8")

                with patch.dict(os.environ, environment, clear=True):
                    result = await scheduler._execute_task(
                        "finalpool/demo-task",
                        "tag",
                        "model",
                        "provider",
                        "10",
                        30,
                        False,
                        dump_path=dump_path,
                        runner=runner,
                    )

                self.assertEqual(result["status"], "success")
                self.assertFalse(stale_receipt.exists())
        write_receipt.assert_not_called()

    @patch.object(
        run_parallel_module,
        "write_completion_receipt",
        side_effect=OSError("disk full"),
    )
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_signed_receipt_failure_marks_task_failed(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.side_effect = completed_command(0)
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)

            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
                clear=True,
            ):
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

        write_receipt.assert_called_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("failed to write signed completion receipt", result["error"])
        self.assertEqual(scheduler.completed_tasks, 0)
        self.assertEqual(scheduler.failed_tasks, 1)

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

                    with patch.dict(
                        os.environ,
                        {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
                        clear=True,
                    ):
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

    @patch.object(run_parallel_module, "write_completion_receipt")
    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_verified_timeout_writes_terminal_receipt(
        self, run_command: AsyncMock, write_receipt
    ) -> None:
        run_command.side_effect = timed_out_after_cleanup
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)
            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": "/receipt-key"},
                clear=True,
            ):
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

        self.assertEqual(result["status"], "timeout")
        write_receipt.assert_called_once_with(
            dump_path,
            "demo-task",
            Path(dump_path) / "finalpool" / "demo-task",
            run_parallel_module.TIMEOUT_EXIT_CODE,
            completion_kind=(
                run_parallel_module.COMPLETION_KIND_SCHEDULER_TIMEOUT
            ),
        )

    @patch.object(run_parallel_module, "run_command_async", new_callable=AsyncMock)
    async def test_malformed_eval_is_not_double_counted(
        self, run_command: AsyncMock
    ) -> None:
        async def write_malformed_eval(_command, log_file, **_kwargs):
            eval_path = Path(log_file).with_name("eval_res.json")
            eval_path.parent.mkdir(parents=True, exist_ok=True)
            eval_path.write_text(
                "not json", encoding="utf-8"
            )
            return {"returncode": 0, "success": True}

        run_command.side_effect = write_malformed_eval
        with tempfile.TemporaryDirectory() as dump_path:
            scheduler = AsyncTaskScheduler(None, 1)
            scheduler.total_tasks = 1

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

        self.assertEqual(result["status"], "failed")
        self.assertEqual(scheduler.completed_tasks, 0)
        self.assertEqual(scheduler.failed_tasks, 1)


class ExistingResultFilterTests(unittest.TestCase):
    def test_signed_mode_reruns_a_result_without_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as dump_path:
            key_path = Path(dump_path) / "receipt.key"
            key_path.write_bytes(b"k" * 32)
            task_dir = Path(dump_path) / "finalpool" / "demo-task"
            task_dir.mkdir(parents=True)
            (task_dir / "status.json").write_text(
                json.dumps(
                    {
                        "preprocess": "done",
                        "running": "done",
                        "evaluation": True,
                    }
                ),
                encoding="utf-8",
            )
            (task_dir / "eval_res.json").write_text(
                json.dumps({"pass": True}), encoding="utf-8"
            )

            with patch.dict(
                os.environ,
                {"TOOLATHLON_COMPLETION_RECEIPT_KEY_FILE": str(key_path)},
                clear=True,
            ):
                pending, completed = (
                    run_parallel_module.filter_tasks_with_existing_results(
                        ["finalpool/demo-task"],
                        dump_path,
                        require_completion_receipts=True,
                    )
                )
                self.assertEqual(pending, ["finalpool/demo-task"])
                self.assertEqual(completed, [])

                write_completion_receipt(
                    dump_path,
                    "demo-task",
                    task_dir,
                    producer_returncode=0,
                    completion_kind=(
                        run_parallel_module.COMPLETION_KIND_PROCESS_EXIT
                    ),
                )
                pending, completed = (
                    run_parallel_module.filter_tasks_with_existing_results(
                        ["finalpool/demo-task"],
                        dump_path,
                        require_completion_receipts=True,
                    )
                )
                self.assertEqual(pending, [])
                self.assertEqual(completed, ["finalpool/demo-task"])

                (task_dir / "eval_res.json").write_text(
                    json.dumps({"pass": False}), encoding="utf-8"
                )
                pending, completed = (
                    run_parallel_module.filter_tasks_with_existing_results(
                        ["finalpool/demo-task"],
                        dump_path,
                        require_completion_receipts=True,
                    )
                )
                self.assertEqual(pending, ["finalpool/demo-task"])
                self.assertEqual(completed, [])

                (task_dir / "status.json").write_text(
                    json.dumps(
                        {
                            "preprocess": "done",
                            "running": "done",
                            "evaluation": False,
                        }
                    ),
                    encoding="utf-8",
                )
                (task_dir / "eval_res.json").write_text(
                    json.dumps({"pass": True}), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    write_completion_receipt(
                        dump_path,
                        "demo-task",
                        task_dir,
                        producer_returncode=1,
                        completion_kind=(
                            run_parallel_module.COMPLETION_KIND_PROCESS_EXIT
                        ),
                    )
                pending, completed = (
                    run_parallel_module.filter_tasks_with_existing_results(
                        ["finalpool/demo-task"],
                        dump_path,
                        require_completion_receipts=True,
                    )
                )
                self.assertEqual(pending, ["finalpool/demo-task"])
                self.assertEqual(completed, [])

                (task_dir / "status.json").unlink()
                with self.assertRaises(ValueError):
                    write_completion_receipt(
                        dump_path,
                        "demo-task",
                        task_dir,
                        producer_returncode=0,
                        completion_kind=(
                            run_parallel_module.COMPLETION_KIND_PROCESS_EXIT
                        ),
                    )
                pending, completed = (
                    run_parallel_module.filter_tasks_with_existing_results(
                        ["finalpool/demo-task"],
                        dump_path,
                        require_completion_receipts=True,
                    )
                )
                self.assertEqual(pending, ["finalpool/demo-task"])
                self.assertEqual(completed, [])


class ContainerCleanupContractTests(unittest.TestCase):
    def test_cleanup_fails_closed_when_container_absence_is_unverified(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[2] / "run_single_containerized.sh"
        )
        script = script_path.read_text(encoding="utf-8")
        cleanup = script[
            script.index("cleanup() {") : script.index("trap cleanup EXIT")
        ]

        remove = cleanup.index('$CONTAINER_RUNTIME rm -f "$CONTAINER_ID"')
        verify = cleanup.index(
            "remaining_container_ids=$($CONTAINER_RUNTIME ps -aq --no-trunc "
            '--filter "id=$CONTAINER_ID" 2>/dev/null)'
        )
        self.assertLess(remove, verify)
        self.assertIn('$CONTAINER_RUNTIME stop -t 0 "$CONTAINER_ID"', cleanup)
        self.assertIn('if [ "$container_query_exit_code" -ne 0 ]', cleanup)
        self.assertIn('elif [ -n "$remaining_container_ids" ]', cleanup)
        self.assertIn("printf 'container-absent\\n'", cleanup)
        self.assertEqual(cleanup.count("cleanup_exit_code=$UNSAFE_CLEANUP_EXIT_CODE"), 3)
        self.assertIn('exit "$cleanup_exit_code"', cleanup)
        self.assertIn("UNSAFE_CLEANUP_EXIT_CODE=200", script)
        self.assertIn('CONTAINER_NAME="$CONTAINER_ID"', script)
        self.assertNotIn('--filter "name=$CONTAINER_NAME"', script)

    def test_name_collision_never_deletes_an_unowned_container(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[2] / "run_single_containerized.sh"
        )
        project_root = script_path.parents[1]
        task_name = next(
            path.name
            for path in (project_root / "tasks" / "finalpool").iterdir()
            if path.is_dir()
        )

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            docker_log = temporary_path / "docker.log"
            marker = temporary_path / "cleanup.marker"

            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *podman_or_docker*) printf 'docker\\n' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            fake_readlink = fake_bin / "readlink"
            fake_readlink.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -f ]; then printf '%s\\n' \"$2\"; exit 0; fi\n"
                "exec /usr/bin/readlink \"$@\"\n",
                encoding="utf-8",
            )
            fake_readlink.chmod(0o755)
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_LOG\"\n"
                "if [ \"$1\" = run ]; then\n"
                "  shift\n"
                "  while [ \"$#\" -gt 0 ]; do\n"
                "    if [ \"$1\" = --cidfile ]; then\n"
                "      shift\n"
                "      if [ -e \"$1\" ]; then\n"
                "        printf 'cidfile-existed\\n' >> \"$FAKE_DOCKER_LOG\"\n"
                "      fi\n"
                "      break\n"
                "    fi\n"
                "    shift\n"
                "  done\n"
                "  exit 125\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "FAKE_DOCKER_LOG": str(docker_log),
                    run_parallel_module.COMPLETION_CLEANUP_MARKER_ENV: str(marker),
                }
            )
            completed = subprocess.run(
                [
                    "bash",
                    str(script_path),
                    f"finalpool/{task_name}",
                    "tag",
                    str(temporary_path / "dumps"),
                    "model",
                    "provider",
                    "10",
                    "scripts/formal_run_v0.json",
                    "image",
                ],
                cwd=project_root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertTrue(docker_log.exists(), completed.stdout)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                completed.returncode, run_parallel_module.UNSAFE_CLEANUP_EXIT_CODE
            )
            self.assertTrue(any(call.startswith("run ") for call in calls))
            self.assertNotIn("cidfile-existed", calls)
            self.assertFalse(any(call.startswith(("stop ", "rm ")) for call in calls))
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
