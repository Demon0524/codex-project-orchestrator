import concurrent.futures
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from workflow import State, Toolkit
from rpc_client import RpcError, RpcTimeoutError
from test_handoff_journal import sample_state


class BackendDouble:
    mode = "stdio"

    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.attention, self.usage = {}, {}
        self.calls, self.late = [], []
        self.threads = {"source": {"id": "source", "cwd": self.cwd, "projectId": None, "status": {"type": "idle"}}}
        self.turns = {"source": []}
        self.next_thread, self.next_turn = 0, 0
        self.timeout_method = None
        self.entered = self.release = None

    def call(self, method, params=None, timeout=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "project/list":
            return {"data": [{"id": "project", "roots": [{"path": self.cwd}]}]}
        if method == "thread/start":
            self.next_thread += 1
            tid = "worker-" + str(self.next_thread)
            self.threads[tid] = {"id": tid, "cwd": params["cwd"], "projectId": params.get("projectId"), "status": {"type": "idle"}}
            self.turns[tid] = []
            result = {"thread": self.threads[tid]}
        elif method == "thread/read":
            return {"thread": copy.deepcopy(self.threads[params["threadId"]])}
        elif method == "thread/turns/list":
            return {"data": copy.deepcopy(self.turns[params["threadId"]])}
        elif method == "turn/start":
            if self.entered:
                self.entered.set()
                self.release.wait(timeout=3)
            self.next_turn += 1
            turn = {"id": "turn-" + str(self.next_turn), "status": "completed", "items": []}
            self.turns[params["threadId"]].insert(0, turn)
            result = {"turn": turn}
        else:
            result = {}
        if method == self.timeout_method:
            self.timeout_method = None
            self.late.append({"rpc_id": 77, "method": method, "result": copy.deepcopy(result), "error": None})
            raise RpcTimeoutError(method, 77)
        return result

    def drain_late_responses(self):
        result, self.late = self.late, []
        return result


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.b = BackendDouble(self.root)
        self.t = Toolkit(self.b, State(self.root / "state"))
        self.file = self.root / "HANDOFF.md"
        self.file.write_text("Verified progress. Next: continue within scope.")

    def prepare(self):
        return self.t.handoff("h1", "source", str(self.root), "Continue verification", checkpoint=str(self.file))

    def accept(self, record):
        self.b.turns[record["successor"]][0]["items"] = [{"type": "agentMessage", "phase": "final_answer",
                                                          "text": "HANDOFF_ACCEPTED:" + record["id"]}]
        return self.t.handoff_status("h1")

    def test_exception_rolls_back_state(self):
        with self.assertRaises(RuntimeError):
            with self.t.state.transaction() as data:
                data["owners"][str(self.root)] = "wrong"
                raise RuntimeError("interrupted transaction")
        with self.t.state.transaction() as data:
            self.assertEqual(data["owners"], {})

    def test_source_cwd_must_match(self):
        self.b.threads["source"]["cwd"] = "/elsewhere"
        with self.assertRaisesRegex(RpcError, "cwd"):
            self.prepare()
        self.assertEqual(self.b.next_thread, 0)

    def test_source_project_must_match(self):
        with self.assertRaisesRegex(RpcError, "project"):
            self.t.handoff("h1", "source", str(self.root), "Continue", checkpoint=str(self.file), project_id="project")

    def test_only_bound_completed_final_answer_accepts(self):
        record = self.prepare()
        turn = self.b.turns[record["successor"]][0]
        marker = "HANDOFF_ACCEPTED:" + record["id"]
        turn["items"] = [{"type": "agentMessage", "phase": "commentary", "text": marker}]
        self.assertEqual(self.t.handoff_status("h1")["phase"], "verification_pending")
        turn["items"][0]["phase"] = "final_answer"
        turn["status"] = "failed"
        self.assertEqual(self.t.handoff_status("h1")["phase"], "verification_pending")
        turn["status"], turn["id"] = "completed", "unrelated-turn"
        self.assertEqual(self.t.handoff_status("h1")["phase"], "verification_pending")
        turn["id"] = record["verification_turn_id"]
        self.assertEqual(self.t.handoff_status("h1")["phase"], "accepted")

    def test_owner_race_blocks_takeover(self):
        record = self.prepare()
        self.b.turns[record["successor"]][0]["items"] = [{"type": "agentMessage", "phase": "final_answer",
                                                        "text": "HANDOFF_ACCEPTED:" + record["id"]}]
        with self.t.state.transaction() as data:
            data["owners"][str(self.root)] = "other"
        with self.assertRaisesRegex(RpcError, "concurrently"):
            self.t.handoff_status("h1")

    def test_activate_rechecks_changed_checkpoint(self):
        record = self.prepare()
        self.accept(record)
        self.file.write_text("Changed after acceptance")
        with self.assertRaisesRegex(RpcError, "unchanged"):
            self.t.handoff_activate("h1")
        self.assertEqual(self.b.next_turn, 1)

    def test_activate_rechecks_missing_checkpoint(self):
        record = self.prepare()
        self.accept(record)
        self.file.unlink()
        with self.assertRaises(RpcError):
            self.t.handoff_activate("h1")

    def test_activation_once_and_retired_dispatch_rejected(self):
        record = self.prepare()
        self.accept(record)
        active = self.t.handoff_activate("h1")
        self.assertEqual(active["phase"], "active")
        self.assertEqual(self.t.handoff_activate("h1")["activation_turn_id"], active["activation_turn_id"])
        self.assertEqual(self.b.next_turn, 2)
        for actor in (None, "source"):
            with self.assertRaisesRegex(RpcError, "Coordinator"):
                self.t.create("retired-" + str(actor), str(self.root), "Work", "Work", coordinator_id=actor)
        self.t.create("current", str(self.root), "Work", "Work", coordinator_id=record["successor"])

    def test_late_created_thread_is_reconciled_not_recreated(self):
        self.b.timeout_method = "thread/start"
        with self.assertRaises(RpcTimeoutError):
            self.t.create("c1", str(self.root), "Task", "Task")
        result = self.t.reconcile("c1")
        self.assertEqual(result["thread_id"], "worker-1")
        self.assertEqual(result["status"], "created")
        self.t.create("c1", str(self.root), "Task", "Task")
        self.assertEqual(self.b.next_thread, 1)
        resumed = self.t.resume_create("c1", str(self.root), "Task", "Task")
        self.assertEqual(resumed["status"], "started")
        self.assertEqual(self.b.next_thread, 1)
        self.assertEqual(self.b.next_turn, 1)

    def test_resume_requires_original_inputs_and_never_replays_uncertain_turn(self):
        self.b.timeout_method = "turn/start"
        with self.assertRaises(RpcTimeoutError):
            self.t.create("c1", str(self.root), "Task", "Task")
        with self.assertRaisesRegex(RpcError, "different arguments"):
            self.t.resume_create("c1", str(self.root), "Changed", "Task")
        with self.assertRaisesRegex(RpcError, "safe to resume"):
            self.t.resume_create("c1", str(self.root), "Task", "Task")
        self.assertEqual(self.b.next_turn, 1)

    def test_late_response_cannot_match_another_connection(self):
        self.b.timeout_method = "thread/start"
        with self.assertRaises(RpcTimeoutError):
            self.t.create("c1", str(self.root), "Task", "Task")
        other = Toolkit(self.b, self.t.state)
        self.assertNotIn("thread_id", other.reconcile("c1"))

    def test_handoff_creation_late_turn_recovers_bound_ids(self):
        self.b.timeout_method = "turn/start"
        with self.assertRaises(RpcTimeoutError):
            self.prepare()
        record = self.t.handoff_reconcile("h1")
        self.assertEqual(record["phase"], "verification_pending")
        self.assertEqual(record["verification_turn_id"], "turn-1")
        self.assertEqual(self.b.next_thread, 1)

    def test_handoff_late_thread_creation_resumes_original_verification(self):
        self.b.timeout_method = "thread/start"
        with self.assertRaises(RpcTimeoutError):
            self.prepare()
        self.assertEqual(self.t.handoff_reconcile("h1")["phase"], "blocked")
        record = self.t.handoff_resume("h1")
        self.assertEqual(record["phase"], "verification_pending")
        self.assertEqual(self.b.next_thread, 1)
        self.assertEqual(self.b.next_turn, 1)
        self.assertEqual(self.accept(record)["phase"], "accepted")

    def test_journal_before_dispatch_crash_is_explicitly_recoverable(self):
        original = self.t.create
        self.t.create = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("crash before reservation"))
        try:
            with self.assertRaises(SystemExit):
                self.prepare()
        finally:
            self.t.create = original
        record = self.t.handoff_reconcile("h1")
        self.assertTrue(record["creation_not_dispatched"])
        self.assertTrue(record["verification_prompt"])
        recovered = self.t.handoff_resume("h1")
        self.assertEqual(recovered["phase"], "verification_pending")
        self.assertEqual(self.b.next_thread, 1)
        self.assertEqual(self.b.next_turn, 1)

    def test_activation_preflight_refusal_remains_retryable_without_fake_uncertainty(self):
        record = self.prepare()
        self.accept(record)
        self.b.threads[record["successor"]]["status"] = {"type": "active"}
        with self.assertRaisesRegex(RpcError, "running"):
            self.t.handoff_activate("h1")
        with self.t.state.transaction() as data:
            self.assertEqual(data["handoffs"]["h1"]["phase"], "accepted")
            self.assertNotIn("send:activate-h1", data["requests"])
        self.b.threads[record["successor"]]["status"] = {"type": "idle"}
        self.assertEqual(self.t.handoff_activate("h1")["phase"], "active")

    def test_handoff_cannot_bypass_parent_coordinator(self):
        child = self.root / "child"
        child.mkdir()
        self.file = child / "HANDOFF.md"
        self.file.write_text("Checkpoint")
        self.b.threads["source"]["cwd"] = str(child)
        with self.t.state.transaction() as data:
            data["owners"][str(self.root)] = "parent-coordinator"
        with self.assertRaisesRegex(RpcError, "Coordinator"):
            self.t.handoff("h1", "source", str(child), "Continue", checkpoint=str(self.file))

    def test_activation_late_turn_is_recorded_without_resend(self):
        record = self.prepare()
        self.accept(record)
        self.b.timeout_method = "turn/start"
        with self.assertRaises(RpcTimeoutError):
            self.t.handoff_activate("h1")
        blocked = self.t.handoff_reconcile("h1")
        self.assertEqual(blocked["phase"], "blocked")
        self.assertIn("handoff_activation_reconcile", blocked["needs_attention"])
        self.assertEqual(self.t.handoff_resume("h1")["phase"], "blocked")
        recovered = self.t.handoff_activation_reconcile("h1")
        self.assertEqual(recovered["phase"], "active")
        self.assertTrue(recovered["reconciled"])
        self.assertEqual(self.b.next_turn, 2)
        self.assertEqual(self.t.handoff_activate("h1")["phase"], "active")
        self.assertEqual(self.b.next_turn, 2)

    def test_concurrent_send_reserves_thread_before_rpc(self):
        created = self.t.create("c1", str(self.root), "Task", "Task")
        self.b.entered, self.b.release = threading.Event(), threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.t.send, "s1", created["thread_id"], "Next")
            self.assertTrue(self.b.entered.wait(timeout=2))
            try:
                with self.assertRaisesRegex(RpcError, "uncertain"):
                    self.t.send("s2", created["thread_id"], "Concurrent")
            finally:
                self.b.release.set()
            first.result(timeout=3)
        self.assertEqual(self.b.next_turn, 2)

    def test_wait_many_uses_metadata_activity(self):
        self.b.threads["source"]["status"] = {"type": "active"}
        created = self.t.create("c1", str(self.root), "Task", "Task")
        result = self.t.wait_many(["source", created["thread_id"]], mode="any", timeout_seconds=0)
        self.assertEqual(result["ready"], [created["thread_id"]])

    def test_structured_checkpoint_can_drive_handoff(self):
        saved = self.t.checkpoint_save("cp1", sample_state())
        self.assertIn("checkpoint_ref", saved)
        record = self.t.handoff("h1", "source", str(self.root), "Continue", checkpoint_ref=saved["checkpoint_ref"])
        self.assertEqual(record["checkpoint_ref"], saved["checkpoint_ref"])
        self.assertIn("Continue the verified", self.t.checkpoint_read(saved["checkpoint_ref"])["markdown"])

    def test_total_result_text_is_bounded(self):
        self.b.turns["source"] = [{"id": str(t), "status": "completed", "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": "x" * 30000} for _ in range(20)]} for t in range(3)]
        result = self.t.read("source")
        self.assertLessEqual(sum(len(i["text"]) for t in result["turns"] for i in t["items"]), 24000)


if __name__ == "__main__":
    unittest.main()
