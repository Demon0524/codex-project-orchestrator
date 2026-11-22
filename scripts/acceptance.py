#!/usr/bin/env python3
"""Opt-in live local acceptance: creates test chats, never edits project files."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from rpc_client import Backend
from workflow import State, Toolkit


def completed(toolkit, thread_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = toolkit.wait(thread_id, 20)
        if result.get("needs_attention") or result.get("snapshot_unavailable"):
            raise RuntimeError(json.dumps(result))
        if not result.get("timed_out"):
            if not result.get("turns") or result["turns"][0]["status"] != "completed":
                raise RuntimeError("Acceptance turn did not complete: " + json.dumps(result))
            return result
    raise RuntimeError("Live acceptance deadline reached; inspect recorded thread IDs")


def has_answer(result, text):
    return any(i.get("phase") == "final_answer" and text in i.get("text", "")
               for t in result["turns"] for i in t.get("items", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize actual test chats and model calls")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run actual model calls and create labeled acceptance chats")
    root = Path(__file__).resolve().parents[1]
    prefix = "v030-" + uuid.uuid4().hex[:12]
    directory = root / "work" / prefix
    directory.mkdir(parents=True)
    b = Backend()
    # Isolate acceptance coordinator ownership from real projects and normal toolkit state.
    t = Toolkit(b, State(directory / "toolkit-state"))
    report = {"request_prefix": prefix, "backend": b.mode, "checks": []}

    def checked(name):
        report["checks"].append(name)
        (directory / "report.json").write_text(json.dumps(report, indent=2))
        print(name, flush=True)

    try:
        first = t.create(prefix + "-source", str(directory),
                         "Acceptance check only. No tools, file changes, other agents or external actions. Reply exactly LIVE_CREATE_OK.",
                         "Toolkit 0.3 acceptance source")
        report["source_thread_id"] = first["thread_id"]
        assert has_answer(completed(t, first["thread_id"]), "LIVE_CREATE_OK")
        checked("independent_create_read")
        t.send(prefix + "-followup", first["thread_id"],
               "Acceptance follow-up only. No tools or changes. Reply exactly LIVE_SEND_OK.")
        assert has_answer(completed(t, first["thread_id"]), "LIVE_SEND_OK")
        checked("followup")
        state = {"schema_version": 1, "goal": "Validate coordinator takeover only",
                 "constraints_preferences": ["No file changes, external actions, deployments or new workers.",
                                             "The activation goal is only to return the requested marker."],
                 "progress": {"done": ["Independent chat creation and follow-up verified"], "in_progress": [],
                              "pending": ["Verify checkpoint and await activation"]},
                 "key_decisions": ["Use the local read-only backend"], "rejected_attempts": [],
                 "verification": ["LIVE_CREATE_OK and LIVE_SEND_OK observed"],
                 "critical_context": ["Working directory is an isolated acceptance directory inside the toolkit checkout.",
                                      "Existing checkout changes belong to the main implementation task and must be preserved."],
                 "uncertainties": [], "next_steps": ["After activation, reply exactly LIVE_ACTIVATE_OK without doing project work."]}
        checkpoint = t.checkpoint_save(prefix + "-checkpoint", state)
        report["checkpoint_ref"] = checkpoint["checkpoint_ref"]
        checked("immutable_structured_checkpoint")
        handoff_id = prefix + "-handoff"
        record = t.handoff(handoff_id, first["thread_id"], str(directory),
                           "Acceptance continuation only: after activation reply exactly LIVE_ACTIVATE_OK. No file changes, tools or workers are needed after activation.",
                           checkpoint_ref=checkpoint["checkpoint_ref"])
        report["successor_thread_id"] = record["successor"]
        completed(t, record["successor"])
        status = t.handoff_status(handoff_id)
        assert status["phase"] == "accepted", status
        checked("bound_final_takeover_acceptance")
        activated = t.handoff_activate(handoff_id)
        assert activated["phase"] == "active", activated
        assert has_answer(completed(t, record["successor"]), "LIVE_ACTIVATE_OK")
        checked("explicit_activation_and_continuation")
        replay = t.handoff_activate(handoff_id)
        assert replay["activation_turn_id"] == activated["activation_turn_id"]
        checked("idempotent_activation")
        fleet = t.wait_many([first["thread_id"], record["successor"]], "all", 0)
        assert len(fleet["ready"]) == 2 and not fleet["errors"], fleet
        checked("fleet_collection")
        report["result"] = "passed"
        (directory / "report.json").write_text(json.dumps(report, indent=2))
        print("REPORT", directory / "report.json", flush=True)
    finally:
        b.close()


if __name__ == "__main__":
    main()
