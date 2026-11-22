import concurrent.futures
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from handoff_journal import CheckpointError, CheckpointStore, transition
from vendor.session_handoff_state import HandoffStateError, render_state, validate_state


def sample_state():
    return {
        "schema_version": 1,
        "goal": "Continue the verified project work",
        "constraints_preferences": ["Read the project instructions before editing"],
        "progress": {"done": ["Core flow verified"], "in_progress": [], "pending": ["Review next module"]},
        "key_decisions": ["Retain Python standard-library runtime"],
        "rejected_attempts": ["Do not replay a request with unknown result"],
        "verification": ["Relevant tests passed"],
        "critical_context": ["Current branch: main"],
        "uncertainties": ["Desktop reload not yet observed"],
        "next_steps": ["Inspect the existing worktree"],
    }


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "checkpoints"
        self.store = CheckpointStore(self.directory)

    def test_upstream_validation_and_renderer_contract(self):
        state = sample_state()
        self.assertEqual(validate_state(state), state)
        rendered = render_state(state)
        self.assertIn("## Goal", rendered)
        self.assertIn("Rejected attempt: Do not replay", rendered)
        self.assertIn("Verification: Relevant tests passed", rendered)
        self.assertIn("1. Inspect the existing worktree", rendered)
        self.assertIsNot(validate_state(state), state)

    def test_save_read_and_idempotent_replay(self):
        first = self.store.create("stable-id", sample_state())
        second = self.store.create("stable-id", sample_state())
        self.assertEqual(first["checkpoint_ref"], second["checkpoint_ref"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(self.store.read(first["checkpoint_ref"])["state"], sample_state())
        self.assertEqual(stat.S_IMODE(Path(first["path"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(first["path"]).with_name("record.json").stat().st_mode), 0o600)

    def test_different_input_cannot_replace_same_request(self):
        first = self.store.create("stable-id", sample_state())
        updated = sample_state()
        updated["next_steps"] = ["A different step"]
        with self.assertRaisesRegex(CheckpointError, "different checkpoint"):
            self.store.create("stable-id", updated)
        self.assertEqual(self.store.read(first["checkpoint_ref"])["state"], sample_state())

    def test_redaction_runs_before_persistence(self):
        secret = "ghp_" + "A" * 32
        state = sample_state()
        state["critical_context"] = ["token " + secret, "password=not-for-publication"]
        result = self.store.create("redact", state)
        self.assertGreaterEqual(result["redactions"], 2)
        for path in Path(result["path"]).parent.iterdir():
            self.assertNotIn(secret, path.read_text())
            self.assertNotIn("not-for-publication", path.read_text())
        self.assertNotIn(secret, json.dumps(result))

    def test_same_redacted_output_is_not_same_raw_request(self):
        state = sample_state()
        state["critical_context"] = ["ghp_" + "A" * 32]
        self.store.create("redact-raw", state)
        state["critical_context"] = ["ghp_" + "B" * 32]
        with self.assertRaises(CheckpointError):
            self.store.create("redact-raw", state)

    def test_redactor_is_injectable(self):
        store = CheckpointStore(self.directory, lambda text: (text.replace("main", "branch"), int("main" in text)))
        result = store.create("custom", sample_state())
        self.assertEqual(result["state"]["critical_context"], ["Current branch: branch"])
        self.assertEqual(result["redactions"], 1)

    def test_modified_markdown_is_rejected(self):
        result = self.store.create("tampered", sample_state())
        Path(result["path"]).write_text("Tampered checkpoint")
        with self.assertRaisesRegex(CheckpointError, "changed"):
            self.store.read(result["checkpoint_ref"])

    def test_state_and_markdown_must_agree(self):
        result = self.store.create("tampered-json", sample_state())
        metadata = Path(result["path"]).with_name("record.json")
        record = json.loads(metadata.read_text())
        record["state"]["goal"] = "Different goal"
        metadata.write_text(json.dumps(record))
        with self.assertRaises(CheckpointError):
            self.store.read(result["checkpoint_ref"])

    def test_reference_and_symlink_escape_rejected(self):
        for ref in ["../state.json", "checkpoint://../state", "checkpoint://" + "a" * 63, "checkpoint://" + "a" * 64 + "/x"]:
            with self.assertRaises(CheckpointError):
                self.store.read(ref)
        result = self.store.create("symlink", sample_state())
        path = Path(result["path"])
        external = Path(self.temp.name) / "external"
        external.write_text(path.read_text())
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(CheckpointError):
            self.store.read(result["checkpoint_ref"])

    def test_schema_is_bounded_and_exact(self):
        for mutation in [lambda s: s.update(extra="unknown"), lambda s: s.update(schema_version=True),
                         lambda s: s.update(goal="x" * 8193), lambda s: s.update(next_steps=["x"] * 257),
                         lambda s: s.update(goal="\x00bad")]:
            state = sample_state()
            mutation(state)
            with self.assertRaises(HandoffStateError):
                self.store.create("invalid", state)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_concurrent_same_request_creates_one_snapshot(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.create("concurrent", sample_state()), range(6)))
        self.assertEqual(len({r["checkpoint_ref"] for r in results}), 1)
        self.assertEqual(sum(bool(r.get("replayed")) for r in results), 5)
        self.assertEqual(len([p for p in self.directory.iterdir() if p.is_dir()]), 1)

    def test_failed_write_never_exposes_partial_snapshot(self):
        import handoff_journal
        original = handoff_journal._private_write
        calls = 0

        def fail_second(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted write")
            original(path, content)

        with patch("handoff_journal._private_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.store.create("interrupted", sample_state())
        self.assertEqual([p for p in self.directory.iterdir() if p.is_dir()], [])
        recovered = self.store.create("interrupted", sample_state())
        self.assertEqual(self.store.read(recovered["checkpoint_ref"])["state"], sample_state())


class TransitionTests(unittest.TestCase):
    def test_normal_lifecycle_is_pure(self):
        original = {"request_id": "h1", "nested": {"value": 1}}
        snapshot = copy.deepcopy(original)
        record = original
        for phase in ["prepared", "creating", "verification_pending", "accepted", "activation_sending", "active"]:
            record = transition(record, phase)
        self.assertEqual(record["phase"], "active")
        self.assertEqual(len(record["history"]), 6)
        self.assertEqual(original, snapshot)
        record["nested"]["value"] = 9
        self.assertEqual(original["nested"]["value"], 1)

    def test_blocked_recovery_requires_reconciliation_phase(self):
        record = transition({}, "prepared")
        record = transition(record, "creating")
        record = transition(record, "blocked", reason="unknown outcome")
        with self.assertRaises(CheckpointError):
            transition(record, "active")
        recovered = transition(record, "verification_pending", successor="known-late-id")
        self.assertEqual(recovered["successor"], "known-late-id")

    def test_transition_history_is_bounded(self):
        record = transition({}, "prepared")
        for _ in range(40):
            record = transition(record, "blocked")
            record = transition(record, "prepared")
        self.assertEqual(len(record["history"]), 32)

    def test_unknown_and_reserved_fields_rejected(self):
        with self.assertRaises(CheckpointError):
            transition({}, "active")
        record = transition({}, "prepared")
        with self.assertRaises(CheckpointError):
            transition(record, "accepted")
        with self.assertRaises(CheckpointError):
            transition(record, "creating", history=[])

    def test_activation_recovery_requires_explicit_reconciled_turn(self):
        record = {}
        for phase in ["prepared", "creating", "verification_pending", "accepted"]:
            record = transition(record, phase)
        record = transition(record, "activation_sending", activation_request_id="original-activation")
        record = transition(record, "blocked", reason="RPC timed out")
        with self.assertRaises(CheckpointError):
            transition(record, "active")
        with self.assertRaises(CheckpointError):
            transition(record, "activation_sending")
        with self.assertRaises(CheckpointError):
            transition(record, "activation_sending", reconciled_activation_turn_id=" ")
        missing_request = dict(record)
        missing_request.pop("activation_request_id")
        with self.assertRaises(CheckpointError):
            transition(missing_request, "activation_sending", reconciled_activation_turn_id="real-turn")
        recovered = transition(record, "activation_sending", reconciled_activation_turn_id="real-turn")
        active = transition(recovered, "active")
        self.assertEqual(active["phase"], "active")
        self.assertEqual(active["activation_request_id"], "original-activation")
        self.assertEqual(active["reconciled_activation_turn_id"], "real-turn")


if __name__ == "__main__":
    unittest.main()
