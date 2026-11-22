import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from orchestrator import Backend, RpcError, State, Toolkit


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.attention = {}
        self.usage = {}
        self.turns = []
        self.fail = None
        self.cwd = None

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == self.fail:
            raise RpcError("Uncertain transport failure")
        if method == "thread/start":
            return {"thread": {"id": "real-thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "real-turn-1"}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "cwd": self.cwd, "projectId": None}}
        if method == "thread/turns/list":
            return {"data": self.turns}
        return {}


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.b = FakeBackend()
        self.b.cwd = str(self.root)
        self.t = Toolkit(self.b, State(self.root / "state"))

    def create(self, **overrides):
        params = dict(request_id="create-1", cwd=str(self.root), prompt="Do the task", title="Task")
        params.update(overrides)
        return self.t.create(**params)

    def test_create_replay_does_not_duplicate(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(sum(m == "thread/start" for m, _ in self.b.calls), 1)

    def test_changed_request_arguments_rejected(self):
        self.create()
        with self.assertRaises(RpcError):
            self.create(prompt="Different task")

    def test_uncertain_create_does_not_retry(self):
        self.b.fail = "thread/start"
        with self.assertRaises(RpcError):
            self.create()
        self.assertEqual(self.create()["status"], "creating")
        self.assertEqual(sum(m == "thread/start" for m, _ in self.b.calls), 1)

    def test_uncertain_turn_keeps_created_thread(self):
        self.b.fail = "turn/start"
        with self.assertRaises(RpcError):
            self.create()
        self.assertEqual(self.create()["status"], "created")
        self.assertEqual(self.create()["thread_id"], "real-thread-1")

    def test_defaults_do_not_bypass_permissions(self):
        self.create()
        params = next(p for m, p in self.b.calls if m == "thread/start")
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "on-request")

    def test_cannot_message_unmanaged_thread(self):
        with self.assertRaises(RpcError):
            self.t.send("s1", "unrelated-thread", "continue")

    def test_running_thread_is_not_steered(self):
        self.create()
        self.b.turns = [{"status": "inProgress"}]
        with self.assertRaises(RpcError):
            self.t.send("s1", "real-thread-1", "continue")

    def test_handoff_requires_marker_and_unchanged_checkpoint(self):
        checkpoint = self.root / "HANDOFF.md"
        checkpoint.write_text("Verified checkpoint")
        record = self.t.handoff("h1", "source", str(self.root), "Continue", checkpoint=str(checkpoint))
        self.b.turns = [{"id": record["verification_turn_id"], "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": "Ready"}]}]
        self.assertEqual(self.t.handoff_status("h1")["status"], "pending")
        self.b.turns[0]["items"][0]["text"] = "HANDOFF_ACCEPTED:" + record["id"]
        checkpoint.write_text("Changed underneath successor")
        self.assertEqual(self.t.handoff_status("h1")["status"], "pending")
        checkpoint.write_text("Verified checkpoint")
        self.assertEqual(self.t.handoff_status("h1")["status"], "accepted")

    def test_mcp_discovery_and_bad_input_need_no_backend(self):
        server = Path(__file__).resolve().parents[1] / "server/orchestrator.py"
        requests = ["invalid JSON", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "orchestrator_wait", "arguments": {"thread_id": "t1", "timeout_seconds": 1000}}})]
        result = subprocess.run([sys.executable, str(server)], input="\n".join(requests) + "\n",
                                capture_output=True, text=True, check=True, timeout=5)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn("error", messages[0])
        self.assertEqual(len(messages[1]["result"]["tools"]), 17)
        hints = {t["name"]: t["annotations"]["readOnlyHint"] for t in messages[1]["result"]["tools"]}
        self.assertTrue(hints["orchestrator_health"])
        self.assertFalse(hints["orchestrator_create"])
        self.assertTrue(messages[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
