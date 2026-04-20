"""Live HTTP tests for the v2 REST API.

These tests run against a real ``eval_server.py`` that the developer has
started in a separate terminal — they do not spin up a server or mock
anything internal.  Containers, MCP servers, and the task sandbox are all
treated as black boxes behind the HTTP contract.

Env vars:
    V2_API_URL    base URL of the running server (default http://127.0.0.1:8080)
    V2_TASK_ID    task id for the end-to-end flow (default "cooking-guidance")
    V2_RUN_SLOW   "1" to run the container-touching e2e test, "0" to skip

Run:
    uv run python -m unittest v2_api.tests.test_v2_api_live -v
    V2_RUN_SLOW=0 uv run python -m unittest v2_api.tests.test_v2_api_live -v
"""

import math
import os
import re
import unittest

import httpx


BASE_URL = os.environ.get("V2_API_URL", "http://127.0.0.1:8080")
TASK_ID = os.environ.get("V2_TASK_ID", "cooking-guidance")
RUN_SLOW = os.environ.get("V2_RUN_SLOW", "1") == "1"

FAST_TIMEOUT = 10.0
SLOW_TIMEOUT = 900.0  # cold-start can take 3-5 min: infra deploy + container boot + preprocess

SESSION_ID_RE = re.compile(r"^sess_[0-9a-f]{12}$")
EXEC_ID_RE = re.compile(r"^exec_[0-9a-f]{8}$")


def _client(timeout: float = FAST_TIMEOUT) -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=timeout)


def _best_effort_delete_session(session_id: str) -> None:
    try:
        with _client() as c:
            c.delete(f"/v2/sessions/{session_id}")
    except Exception:
        pass


class TestV2HealthAndCatalog(unittest.TestCase):
    """Read-only endpoints — do not create any session."""

    def test_health_shape(self) -> None:
        with _client() as c:
            r = c.get("/v2/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("version", body)
        self.assertIn("session", body)
        self.assertIsInstance(body["session"]["active"], bool)
        # When a v2 session is the cause, all v2 fields must be populated.
        # When v1 is the cause, ``active`` can be true with v2 fields None.
        if body["session"]["session_id"] is not None:
            self.assertTrue(body["session"]["active"])
            self.assertIsNotNone(body["session"]["model_name"])
            self.assertIsNotNone(body["session"]["started_at"])

    def test_list_tasks_nonempty(self) -> None:
        with _client() as c:
            r = c.get("/v2/tasks")
        self.assertEqual(r.status_code, 200)
        tasks = r.json()["tasks"]
        self.assertGreaterEqual(len(tasks), 50, "expected many tasks in finalpool")
        for t in tasks:
            self.assertTrue(t["task_id"], f"empty task_id in {t}")
            self.assertIsInstance(t["description"], str)
            self.assertIsInstance(t["system_prompt"], str)
            self.assertIsInstance(t["needed_mcp_servers"], list)

    def test_get_known_task(self) -> None:
        with _client() as c:
            r = c.get("/v2/tasks/cooking-guidance")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["task_id"], "cooking-guidance")
        self.assertIn("filesystem", body["needed_mcp_servers"])
        self.assertIn("howtocook", body["needed_mcp_servers"])

    def test_get_unknown_task_404(self) -> None:
        with _client() as c:
            r = c.get("/v2/tasks/no-such-task-xyz")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Task not found", r.json().get("detail", ""))


class TestV2SessionLifecycle(unittest.TestCase):
    """Session create/delete and 4xx paths — does not trigger task start."""

    def setUp(self) -> None:
        with _client() as c:
            r = c.get("/v2/health")
        self.assertEqual(r.status_code, 200)
        if r.json()["session"]["active"]:
            self.skipTest(
                "Server already has an active session — cannot run session "
                "lifecycle tests safely. Delete it and re-run."
            )
        self._owned_session_id = None

    def tearDown(self) -> None:
        if self._owned_session_id:
            _best_effort_delete_session(self._owned_session_id)

    def _create_session(self, model_name: str = "test-v2") -> str:
        with _client() as c:
            r = c.post("/v2/sessions", json={"model_name": model_name})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["session_id"]
        self._owned_session_id = sid
        return sid

    def test_create_and_delete_session(self) -> None:
        with _client() as c:
            r = c.post("/v2/sessions", json={"model_name": "test-v2"})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertRegex(body["session_id"], SESSION_ID_RE)
            self.assertEqual(body["status"], "created")
            sid = body["session_id"]
            self._owned_session_id = sid

            h = c.get("/v2/health").json()
            self.assertTrue(h["session"]["active"])
            self.assertEqual(h["session"]["session_id"], sid)
            self.assertEqual(h["session"]["model_name"], "test-v2")

            d = c.delete(f"/v2/sessions/{sid}")
            self.assertEqual(d.status_code, 200, d.text)
            self.assertEqual(d.json(), {"status": "deleted"})
            self._owned_session_id = None

            h2 = c.get("/v2/health").json()
            self.assertFalse(h2["session"]["active"])

    def test_create_while_active_returns_503(self) -> None:
        self._create_session()
        with _client() as c:
            r = c.post("/v2/sessions", json={"model_name": "test-second"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("busy", r.json().get("detail", "").lower())

    def test_delete_unknown_session_404(self) -> None:
        with _client() as c:
            r = c.delete("/v2/sessions/sess_bogus1234a")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Session not found", r.json().get("detail", ""))

    def test_call_tool_unknown_session_404(self) -> None:
        with _client() as c:
            r = c.post(
                "/v2/sessions/sess_bogus1234a/executions/exec_bogusxx/call-tool",
                json={"tool_name": "local-claim_done", "arguments": {}},
            )
        self.assertEqual(r.status_code, 404)
        self.assertIn("Session not found", r.json().get("detail", ""))

    def test_call_tool_unknown_execution_404(self) -> None:
        sid = self._create_session()
        with _client() as c:
            r = c.post(
                f"/v2/sessions/{sid}/executions/exec_bogusxx/call-tool",
                json={"tool_name": "local-claim_done", "arguments": {}},
            )
        self.assertEqual(r.status_code, 404)
        self.assertIn("Execution not found", r.json().get("detail", ""))

    def test_grade_unknown_execution_404(self) -> None:
        sid = self._create_session()
        with _client() as c:
            r = c.post(f"/v2/sessions/{sid}/executions/exec_bogusxx/grade")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Execution not found", r.json().get("detail", ""))

    def test_stop_unknown_execution_404(self) -> None:
        sid = self._create_session()
        with _client() as c:
            r = c.delete(f"/v2/sessions/{sid}/executions/exec_bogusxx")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Execution not found", r.json().get("detail", ""))


@unittest.skipUnless(RUN_SLOW, "V2_RUN_SLOW=0 — skipping container e2e")
class TestV2FullFlow(unittest.TestCase):
    """End-to-end: create session → start task → call tool → grade → stop → delete."""

    def setUp(self) -> None:
        with _client() as c:
            r = c.get("/v2/health")
        self.assertEqual(r.status_code, 200)
        if r.json()["session"]["active"]:
            self.skipTest(
                "Server already has an active session — cannot run e2e test."
            )

    def test_full_flow(self) -> None:
        sid = None
        try:
            with _client() as c:
                r = c.post("/v2/sessions", json={"model_name": "test-e2e"})
                self.assertEqual(r.status_code, 200, r.text)
                sid = r.json()["session_id"]
                self.assertRegex(sid, SESSION_ID_RE)

            with _client(timeout=SLOW_TIMEOUT) as c:
                r = c.post(f"/v2/sessions/{sid}/tasks/{TASK_ID}/start")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["status"], "ready")
            self.assertRegex(body["execution_id"], EXEC_ID_RE)
            tools = body["tools"]
            self.assertIsInstance(tools, list)
            self.assertGreater(len(tools), 0, "expected at least one tool")
            for t in tools:
                self.assertIn("name", t)
                self.assertIn("description", t)
                self.assertIn("parameters", t)
                self.assertIsInstance(t["parameters"], dict)
                self.assertIn("type", t["parameters"])

            tool_names = {t["name"] for t in tools}
            self.assertIn(
                "local-claim_done", tool_names,
                "gateway should always expose local-claim_done",
            )

            eid = body["execution_id"]

            # Pick a safe-ish tool to exercise the call-tool surface.  Prefer a
            # 'list'-shaped tool; fall back to claim_done.  We only assert on
            # the response shape — both is_error=True and False are acceptable,
            # since some tools require arguments.
            probe_tool = next(
                (t["name"] for t in tools if "list" in t["name"].lower()),
                "local-claim_done",
            )
            with _client(timeout=SLOW_TIMEOUT) as c:
                r = c.post(
                    f"/v2/sessions/{sid}/executions/{eid}/call-tool",
                    json={"tool_name": probe_tool, "arguments": {}},
                )
            self.assertEqual(r.status_code, 200, r.text)
            ct = r.json()
            self.assertIsInstance(ct["result"], str)
            self.assertIsInstance(ct["is_error"], bool)

            # Unknown tool name → gateway returns 404, proxy maps to
            # is_error=True within a 200 response (see tool_proxy.py:30).
            with _client(timeout=FAST_TIMEOUT) as c:
                r = c.post(
                    f"/v2/sessions/{sid}/executions/{eid}/call-tool",
                    json={"tool_name": "definitely_not_a_tool", "arguments": {}},
                )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["is_error"])

            # Infra is now deployed, so the unknown-task check is fast.
            with _client(timeout=FAST_TIMEOUT) as c:
                r = c.post(f"/v2/sessions/{sid}/tasks/no-such-task-xyz/start")
            self.assertEqual(r.status_code, 404, r.text)

            # Grade.  Known limitation: container_eval.py expects a
            # traj_log.json that v2's client-driven loop does not produce, so
            # 500 (TimeoutError leaking through router.grade_endpoint) is a
            # currently-expected outcome.  We assert on shape if 200, and
            # tolerate 500 with a logged message.
            with _client(timeout=SLOW_TIMEOUT) as c:
                r = c.post(f"/v2/sessions/{sid}/executions/{eid}/grade")
            self.assertIn(r.status_code, (200, 500), r.text)
            if r.status_code == 200:
                grade = r.json()
                self.assertIn(grade["status"], {"pass", "fail", "null"})
                self.assertIsInstance(grade["score"], float)
                if grade["status"] == "null":
                    self.assertTrue(math.isnan(grade["score"]))
            else:
                print(
                    "  [known-limitation] grade returned 500 — likely missing "
                    "traj_log.json (v2 client-driven loop doesn't emit one)"
                )

            # Stop the execution, then verify call-tool against a stopped
            # execution returns 400 (router.py:141-142).
            with _client(timeout=SLOW_TIMEOUT) as c:
                r = c.delete(f"/v2/sessions/{sid}/executions/{eid}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"status": "stopped"})

            with _client() as c:
                r = c.post(
                    f"/v2/sessions/{sid}/executions/{eid}/call-tool",
                    json={"tool_name": "local-claim_done", "arguments": {}},
                )
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("stopped", r.json().get("detail", "").lower())

            # Delete the session and verify idle.
            with _client() as c:
                r = c.delete(f"/v2/sessions/{sid}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"status": "deleted"})
            sid = None

            with _client() as c:
                h = c.get("/v2/health").json()
            self.assertFalse(h["session"]["active"])
        finally:
            if sid is not None:
                _best_effort_delete_session(sid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
