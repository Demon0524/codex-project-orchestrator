"""Persistent independent-chat operations and verified coordinator transitions."""
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from rpc_client import RpcError, RpcTimeoutError
from handoff_journal import CheckpointStore, transition

VERSION = "0.3.0"
TERMINAL = {"completed", "failed", "interrupted"}


class State:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get(
            "CODEX_ORCHESTRATOR_STATE", str(Path.home() / ".local/state/codex-project-orchestrator")))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def transaction(self):
        fd = os.open(self.directory / "state.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / "state.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            for key in ("requests", "handoffs", "owners", "dispatches"):
                data.setdefault(key, {})
            # Commit only on normal exit. Exceptions cannot save partial transitions.
            yield data
            tmp = self.directory / "state.tmp"
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, "w") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def text_item(prompt):
    return [{"type": "text", "text": prompt}]


def running(snapshot):
    return (snapshot.get("thread", {}).get("status") or {}).get("type") == "active" or any(
        t.get("status") == "inProgress" for t in snapshot.get("turns", []))


class Toolkit:
    def __init__(self, backend, state=None):
        self.backend = backend
        self.state = state or State()
        self.connection_tag = uuid.uuid4().hex
        self.checkpoints = CheckpointStore(self.state.directory / "checkpoints")

    def projects(self):
        data, cursor = [], None
        while True:
            page = self.backend.call("project/list", {"limit": 100, "cursor": cursor})
            data.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return {"projects": data}

    def health(self):
        return {"version": VERSION, "backend": self.backend.mode, "connected": True,
                "project_count": len(self.projects()["projects"]),
                "scope": "local Codex home only; not cloud/durable hosts",
                "lifetime": "shared daemon" if self.backend.mode == "proxy" else "MCP process; active turns may stop on disconnect",
                "approval_handling": "interactive requests fail closed; never auto-approved",
                "structured_checkpoints": True, "late_response_reconciliation": True,
                "handoff_activation": "explicit, idempotent, verified",
                "automatic_token_rotation": False}

    def _project(self, project_id, cwd):
        path = Path(cwd).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise RpcError("cwd must be an existing directory")
        if project_id:
            project = next((p for p in self.projects()["projects"] if p["id"] == project_id), None)
            if not project:
                raise RpcError("Project ID is not available on this local backend")
            roots = [Path(r["path"]).resolve() for r in project.get("roots", [])]
            if not any(path == r or r in path.parents for r in roots):
                raise RpcError("cwd does not belong to the selected project")
        return str(path)

    @staticmethod
    def _coordinator(data, cwd, coordinator_id):
        path = Path(cwd)
        matches = [(root, owner) for root, owner in data["owners"].items()
                   if path == Path(root) or Path(root) in path.parents]
        if matches:
            root, owner = max(matches, key=lambda item: len(item[0]))
            if coordinator_id != owner:
                raise RpcError("Coordinator changed: provide the current coordinator_id; retired or missing coordinator cannot dispatch")

    @staticmethod
    def _same_request(old, digest):
        if old["fingerprint"] != digest:
            raise RpcError("request_id reused with different arguments")
        return {**old, "replayed": True}

    def _rpc(self, key, method, params):
        with self.state.transaction() as data:
            data["requests"][key]["inflight_method"] = method
        try:
            result = self.backend.call(method, params)
        except RpcError as exc:
            with self.state.transaction() as data:
                entry = data["requests"][key]
                entry.update(needs_attention="RPC outcome requires reconciliation", last_method=method)
                if isinstance(exc, RpcTimeoutError):
                    entry["uncertain_rpc_id"] = exc.rpc_id
                    entry["uncertain_connection_tag"] = self.connection_tag
            raise
        self._result(key, method, result)
        return result

    def _result(self, key, method, result):
        with self.state.transaction() as data:
            entry = data["requests"][key]
            entry.pop("uncertain_rpc_id", None)
            entry.pop("uncertain_connection_tag", None)
            entry.pop("needs_attention", None)
            entry["last_method"] = method
            entry.pop("inflight_method", None)
            if method == "thread/start":
                entry.update(thread_id=result["thread"]["id"], status="created")
                data["dispatches"][entry["thread_id"]] = {"request_key": key}
            elif method == "turn/start":
                entry.update(turn_id=result["turn"]["id"], status="started")
                data["dispatches"][entry["thread_id"]] = {"request_key": key, "turn_id": entry["turn_id"]}

    def create(self, request_id, cwd, prompt, title, project_id=None, sandbox="read-only", coordinator_id=None):
        cwd = self._project(project_id, cwd)
        if sandbox not in ("read-only", "workspace-write"):
            raise RpcError("Unsupported sandbox; broad permission bypass is not offered")
        key = "create:" + request_id
        digest = fingerprint([cwd, project_id, prompt, title, sandbox])
        with self.state.transaction() as data:
            old = data["requests"].get(key)
            if old:
                return self._same_request(old, digest)
            self._coordinator(data, cwd, coordinator_id)
            data["requests"][key] = {"fingerprint": digest, "status": "creating", "cwd": cwd,
                                     "project_id": project_id, "title": title}
        result = self._rpc(key, "thread/start", {"cwd": cwd, "projectId": project_id,
                           "sandbox": sandbox, "approvalPolicy": "on-request"})
        tid = result["thread"]["id"]
        self._rpc(key, "thread/name/set", {"threadId": tid, "name": title})
        self._rpc(key, "turn/start", {"threadId": tid, "input": text_item(prompt)})
        with self.state.transaction() as data:
            return copy.deepcopy(data["requests"][key])

    def resume_create(self, request_id, cwd, prompt, title, project_id=None,
                      sandbox="read-only", coordinator_id=None):
        """Explicitly finish a known creation; never repeat a possibly sent turn."""
        cwd = self._project(project_id, cwd)
        key, digest = "create:" + request_id, fingerprint([cwd, project_id, prompt, title, sandbox])
        with self.state.transaction() as data:
            entry = data["requests"][key]
            self._same_request(entry, digest)
            if entry.get("status") == "started":
                return {**copy.deepcopy(entry), "replayed": True}
            self._coordinator(data, cwd, coordinator_id)
            if (not entry.get("thread_id") or entry.get("status") != "created" or
                entry.get("uncertain_rpc_id") is not None or
                entry.get("inflight_method") not in (None, "thread/name/set", "thread/resume") or
                entry.get("last_method") not in ("thread/start", "thread/name/set", "thread/resume")):
                raise RpcError("Creation is not known to be safe to resume; reconcile first")
            entry["status"] = "recovering_created"
            tid = entry["thread_id"]
        try:
            self._rpc(key, "thread/resume", {"threadId": tid, "excludeTurns": True})
            self._rpc(key, "thread/name/set", {"threadId": tid, "name": title})
            self._rpc(key, "turn/start", {"threadId": tid, "input": text_item(prompt)})
        except RpcError:
            with self.state.transaction() as data:
                entry = data["requests"][key]
                if entry["status"] == "recovering_created":
                    entry["status"] = "created"
            raise
        with self.state.transaction() as data:
            return copy.deepcopy(data["requests"][key])

    def read(self, thread_id, _deadline=None):
        def fetch(method, params):
            if _deadline is None:
                return self.backend.call(method, params)
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                raise RpcError("Snapshot deadline reached")
            return self.backend.call(method, params, timeout=remaining)
        meta = fetch("thread/read", {"threadId": thread_id, "includeTurns": False})
        turns = fetch("thread/turns/list", {"threadId": thread_id, "limit": 3,
                                   "sortDirection": "desc", "itemsView": "full"})
        compact, budget = [], 24000
        for original in turns.get("data", []):
            turn = {k: original.get(k) for k in ("id", "status", "error", "startedAt", "completedAt")}
            turn["items"] = []
            items = original.get("items", [])
            for item in [i for i in items if i.get("type") == "agentMessage"][-20:]:
                text = item.get("text", "")
                allowance = min(6000, budget)
                if allowance < 128:
                    break
                trimmed = len(text) > allowance
                if trimmed:
                    half = (allowance - 30) // 2
                    text = text[:half] + "\n[output truncated]\n" + text[-half:]
                budget -= len(text)
                turn["items"].append({"type": "agentMessage", "text": text,
                                      "phase": item.get("phase"), "truncated": trimmed})
            turn["omitted_item_count"] = len(items) - len(turn["items"])
            compact.append(turn)
        thread = meta.get("thread", {})
        return {"thread": {k: thread.get(k) for k in ("id", "cwd", "name", "projectId", "status")},
                "turns": compact, "next_cursor": turns.get("nextCursor"),
                "needs_attention": self.backend.attention.get(thread_id),
                "observed_token_usage": self.backend.usage.get(thread_id)}

    def send(self, request_id, thread_id, prompt, coordinator_id=None, _handoff_request=None):
        key, digest = "send:" + request_id, fingerprint([thread_id, prompt])
        current = self.read(thread_id)
        with self.state.transaction() as data:
            old = data["requests"].get(key)
            if old:
                return self._same_request(old, digest)
            own = next((x for k, x in data["requests"].items()
                        if k.startswith("create:") and x.get("thread_id") == thread_id), None)
            if not own:
                raise RpcError("Mutation restricted to threads created by this toolkit")
            cwd = own.get("cwd") or current["thread"].get("cwd")
            self._coordinator(data, cwd, coordinator_id)
            if running(current):
                raise RpcError("Thread is running; wait before sending a follow-up")
            gate = data["dispatches"].get(thread_id)
            if gate:
                previous = next((t for t in current["turns"] if t["id"] == gate.get("turn_id")), None)
                if previous is None or previous.get("status") not in TERMINAL:
                    raise RpcError("A prior dispatch has an uncertain outcome; reconcile it before continuing")
            if _handoff_request:
                live = data["handoffs"][_handoff_request]
                if live.get("phase") != "accepted" or data["owners"].get(live["cwd"]) != live["successor"]:
                    raise RpcError("Handoff changed concurrently; activation not sent")
                if not self._unchanged(live):
                    raise RpcError("Checkpoint changed before activation reservation")
                data["handoffs"][_handoff_request] = transition(live, "activation_sending",
                                                               activation_request_id=request_id)
            data["requests"][key] = {"fingerprint": digest, "status": "sending", "thread_id": thread_id, "cwd": cwd}
            data["dispatches"][thread_id] = {"request_key": key}
        self._rpc(key, "thread/resume", {"threadId": thread_id, "excludeTurns": True})
        self._rpc(key, "turn/start", {"threadId": thread_id, "input": text_item(prompt)})
        self.backend.attention.pop(thread_id, None)
        with self.state.transaction() as data:
            return copy.deepcopy(data["requests"][key])

    def _pause(self, seconds):
        if hasattr(self.backend, "wait_for_update"):
            self.backend.wait_for_update(self.backend.event_sequence, seconds)
        else:
            time.sleep(seconds)

    def wait(self, thread_id, timeout_seconds=30):
        deadline = time.monotonic() + max(0, min(timeout_seconds, 45))
        while True:
            try:
                result = self.read(thread_id, _deadline=deadline if timeout_seconds else time.monotonic() + 2)
            except RpcError as exc:
                return {"thread_id": thread_id, "timed_out": time.monotonic() >= deadline,
                        "snapshot_unavailable": True, "error": str(exc)}
            active = running(result)
            if result["needs_attention"] or not active or time.monotonic() >= deadline:
                result["timed_out"] = active and not bool(result["needs_attention"])
                return result
            self._pause(min(1, max(0, deadline - time.monotonic())))

    def wait_many(self, thread_ids, mode="any", timeout_seconds=30):
        if not 1 <= len(thread_ids) <= 16 or len(set(thread_ids)) != len(thread_ids) or mode not in ("any", "all"):
            raise RpcError("Use 1-16 unique thread IDs and mode any or all")
        deadline = time.monotonic() + max(0, min(timeout_seconds, 45))
        while True:
            results, errors = {}, {}
            for tid in thread_ids:
                try:
                    results[tid] = self.read(tid, _deadline=deadline if timeout_seconds else time.monotonic() + 2)
                except RpcError as exc:
                    errors[tid] = str(exc)
            ready = [tid for tid, value in results.items() if not running(value) or value["needs_attention"]]
            done = bool(ready) if mode == "any" else len(ready) == len(thread_ids)
            if done or errors or time.monotonic() >= deadline:
                return {"threads": results, "ready": ready, "errors": errors,
                        "timed_out": not done and not bool(errors)}
            self._pause(min(1, max(0, deadline - time.monotonic())))

    def reconcile(self, request_id, operation="create"):
        if operation not in ("create", "send"):
            raise RpcError("operation must be create or send")
        if hasattr(self.backend, "drain_late_responses"):
            for late in self.backend.drain_late_responses():
                with self.state.transaction() as data:
                    key = next((k for k, value in data["requests"].items()
                                if value.get("uncertain_rpc_id") == late["rpc_id"]
                                and value.get("uncertain_connection_tag") == self.connection_tag
                                and value.get("last_method") == late["method"]), None)
                if key and not late.get("error"):
                    self._result(key, late["method"], late.get("result", {}))
        with self.state.transaction() as data:
            value = data["requests"][operation + ":" + request_id]
            return {**copy.deepcopy(value), "automatic_retry": False,
                    "recovery": "Inspect known thread; late replies are available only on the originating connection"}

    def checkpoint_save(self, request_id, state):
        record = self.checkpoints.create(request_id, state)
        return {k: record[k] for k in ("checkpoint_ref", "path", "sha256", "created_at", "redactions")}

    def checkpoint_read(self, checkpoint_ref):
        record = self.checkpoints.read(checkpoint_ref)
        text = Path(record["path"]).read_text()
        return {k: record[k] for k in ("checkpoint_ref", "path", "sha256", "created_at", "redactions")} | {
            "markdown": text[:24000], "truncated": len(text) > 24000}

    def _snapshot_file(self, request_id, path, goal):
        content = path.read_bytes()
        if len(content) > 128 * 1024:
            raise RpcError("Plain handoff file exceeds 128 KiB; prepare a focused structured checkpoint")
        text = content.decode("utf-8")
        state = {"schema_version": 1, "goal": goal, "constraints_preferences": [],
                 "progress": {"done": [], "in_progress": [], "pending": []}, "key_decisions": [],
                 "rejected_attempts": [], "verification": [],
                 "critical_context": [text[i:i + 1000] for i in range(0, len(text), 1000)] or ["Empty handoff file"],
                 "uncertainties": ["Plain-file content supplied by the previous coordinator; verify claims"],
                 "next_steps": [goal]}
        return self.checkpoints.create("handoff:" + request_id, state), hashlib.sha256(content).hexdigest()

    def handoff(self, request_id, source_thread_id, cwd, goal, checkpoint=None,
                project_id=None, sandbox="read-only", checkpoint_ref=None):
        if bool(checkpoint) == bool(checkpoint_ref):
            raise RpcError("Provide exactly one of checkpoint or checkpoint_ref")
        source = self.backend.call("thread/read", {"threadId": source_thread_id, "includeTurns": False})["thread"]
        cwd = self._project(project_id or source.get("projectId"), cwd)
        if not source.get("cwd") or str(Path(source["cwd"]).resolve()) != cwd:
            raise RpcError("Source thread cwd does not match the handoff project directory")
        if project_id and source.get("projectId") != project_id:
            raise RpcError("Source thread project does not match the selected project")
        project_id = project_id or source.get("projectId")
        if sandbox not in ("read-only", "workspace-write"):
            raise RpcError("Unsupported sandbox")
        path, original_sha = None, None
        if checkpoint:
            path = Path(checkpoint).expanduser().resolve(strict=True)
            if Path(cwd) not in path.parents or not path.is_file():
                raise RpcError("Checkpoint must be a file inside the project directory")
            snapshot, original_sha = self._snapshot_file(request_id, path, goal)
        else:
            snapshot = self.checkpoints.read(checkpoint_ref)
        hid = fingerprint([request_id, source_thread_id, cwd, snapshot["checkpoint_ref"], goal, sandbox])[:24]
        prompt = ("Coordinator takeover verification only. Read applicable AGENTS.md and the immutable checkpoint at "
                  f"{json.dumps(snapshot['path'])}. Work in {json.dumps(cwd)}. Verify constraints, working tree and next action "
                  f"for this goal: {goal}. Do not modify files, launch workers or execute the goal yet. "
                  f"If ready, finish the final answer with exactly HANDOFF_ACCEPTED:{hid}. Otherwise report the blocker. "
                  "Wait for an explicit activation follow-up before doing project work.")
        with self.state.transaction() as data:
            old = data["handoffs"].get(request_id)
            if old:
                if old["id"] != hid:
                    raise RpcError("Handoff request_id reused with different arguments")
                return copy.deepcopy(old)
            self._coordinator(data, cwd, source_thread_id)
            if any(h["cwd"] == cwd and h.get("phase", h.get("status")) not in ("active",)
                   for h in data["handoffs"].values()):
                raise RpcError("Another handoff exists; inspect and reconcile it before starting another")
            data["owners"][cwd] = source_thread_id
            record = {"id": hid, "source": source_thread_id, "cwd": cwd, "goal": goal,
                      "checkpoint_ref": snapshot["checkpoint_ref"], "snapshot_sha256": snapshot["sha256"],
                      "checkpoint": str(path) if path else None, "sha256": original_sha,
                      "status": "pending", "project_id": project_id, "sandbox": sandbox,
                      "verification_prompt": prompt}
            data["handoffs"][request_id] = transition(transition(record, "prepared"), "creating")
        try:
            created = self.create("handoff-" + request_id, cwd, prompt, "Coordinator takeover", project_id,
                                  sandbox, coordinator_id=source_thread_id)
            with self.state.transaction() as data:
                data["handoffs"][request_id] = transition(data["handoffs"][request_id], "verification_pending",
                    successor=created["thread_id"], verification_turn_id=created["turn_id"])
                return copy.deepcopy(data["handoffs"][request_id])
        except (RpcError, KeyError):
            with self.state.transaction() as data:
                data["handoffs"][request_id] = transition(data["handoffs"][request_id], "blocked",
                    needs_attention="Creation outcome requires handoff_reconcile; do not repeat with a new ID")
            raise

    def _unchanged(self, record):
        if record.get("checkpoint_ref"):
            snapshot = self.checkpoints.read(record["checkpoint_ref"])
            if snapshot["sha256"] != record.get("snapshot_sha256"):
                return False
        if record.get("checkpoint"):
            try:
                return hashlib.sha256(Path(record["checkpoint"]).read_bytes()).hexdigest() == record["sha256"]
            except OSError:
                return False
        return bool(record.get("checkpoint_ref"))

    def handoff_reconcile(self, request_id):
        with self.state.transaction() as data:
            record = data["handoffs"][request_id]
            if record.get("activation_request_id"):
                return {**copy.deepcopy(record), "needs_attention":
                        "Activation has been reserved; use handoff_activation_reconcile instead of creation recovery"}
            if "create:handoff-" + request_id not in data["requests"]:
                return {**copy.deepcopy(record), "creation_not_dispatched": True,
                        "needs_attention": "Creation was not reserved; explicit handoff_resume can start the saved task"}
        created = self.reconcile("handoff-" + request_id)
        with self.state.transaction() as data:
            record = data["handoffs"][request_id]
            if record.get("phase") in ("creating", "blocked") and created.get("thread_id") and created.get("turn_id"):
                record = transition(record, "verification_pending", successor=created["thread_id"],
                                    verification_turn_id=created["turn_id"], needs_attention=None)
                data["handoffs"][request_id] = record
            return copy.deepcopy(record)

    def handoff_resume(self, request_id):
        record = self.handoff_reconcile(request_id)
        if record.get("verification_turn_id"):
            return record
        if not record.get("verification_prompt") or not self._unchanged(record):
            raise RpcError("No original verification prompt or checkpoint changed; resume refused")
        operation = self.create if record.get("creation_not_dispatched") else self.resume_create
        created = operation("handoff-" + request_id, record["cwd"], record["verification_prompt"],
                            "Coordinator takeover", record.get("project_id"), record.get("sandbox", "read-only"),
                            coordinator_id=record["source"])
        if not created.get("thread_id") or not created.get("turn_id"):
            return {**record, "needs_attention": "Creation is in progress or uncertain; reconcile the same request"}
        with self.state.transaction() as data:
            live = data["handoffs"][request_id]
            data["handoffs"][request_id] = transition(live, "verification_pending", successor=created["thread_id"],
                                                    verification_turn_id=created["turn_id"], needs_attention=None)
            return copy.deepcopy(data["handoffs"][request_id])

    def handoff_status(self, request_id):
        with self.state.transaction() as data:
            record = copy.deepcopy(data["handoffs"][request_id])
        if not record.get("verification_turn_id"):
            return {**record, "needs_attention": "No bound verification turn; reconcile or reverify this legacy handoff manually"}
        result = self.read(record["successor"])
        marker = "HANDOFF_ACCEPTED:" + record["id"]
        accepted = any(t.get("id") == record["verification_turn_id"] and t.get("status") == "completed" and any(
            i.get("phase") == "final_answer" and i.get("text", "").strip().endswith(marker)
            for i in t.get("items", [])) for t in result["turns"])
        try:
            unchanged = self._unchanged(record)
        except ValueError:
            unchanged = False
        with self.state.transaction() as data:
            live = data["handoffs"][request_id]
            if live.get("phase") == "verification_pending" and accepted and unchanged:
                if data["owners"].get(live["cwd"]) != live["source"]:
                    raise RpcError("Coordinator changed concurrently; takeover not committed")
                data["owners"][live["cwd"]] = live["successor"]
                data["handoffs"][request_id] = transition(live, "accepted", status="accepted")
            record = copy.deepcopy(data["handoffs"][request_id])
        return {**record, "checkpoint_unchanged": unchanged,
                "next_action": "Call handoff_activate with this request_id; old coordinator must stop dispatching"
                if record.get("phase") == "accepted" else "Wait or inspect blocker; do not dispatch project work"}

    def handoff_activate(self, request_id):
        record = self.handoff_status(request_id)
        if record.get("phase") in ("active", "activation_sending"):
            return record
        if record.get("phase") != "accepted" or not record.get("checkpoint_unchanged"):
            raise RpcError("Handoff is not accepted with an unchanged checkpoint")
        prompt = (f"Coordinator takeover accepted. You are the active coordinator for {record['cwd']}; "
                  f"use coordinator_id={record['successor']} for further toolkit dispatch. "
                  "The old coordinator stops dispatching. Continue only within the user's existing authorization. "
                  f"Goal: {record['goal']}. Read the checkpoint again if needed, verify progress and proceed.")
        try:
            sent = self.send("activate-" + request_id, record["successor"], prompt,
                             coordinator_id=record["successor"], _handoff_request=request_id)
            with self.state.transaction() as data:
                if not sent.get("turn_id"):
                    return copy.deepcopy(data["handoffs"][request_id])
                data["handoffs"][request_id] = transition(data["handoffs"][request_id], "active",
                                                         status="active", activation_turn_id=sent["turn_id"])
                return copy.deepcopy(data["handoffs"][request_id])
        except (RpcError, KeyError):
            with self.state.transaction() as data:
                if "send:activate-" + request_id in data["requests"]:
                    data["handoffs"][request_id] = transition(data["handoffs"][request_id], "blocked",
                        needs_attention="Activation outcome uncertain; use handoff_activation_reconcile, never re-send blindly")
            raise

    def handoff_activation_reconcile(self, request_id):
        with self.state.transaction() as data:
            record = data["handoffs"][request_id]
            if not record.get("activation_request_id"):
                raise RpcError("No activation was reserved; inspect handoff status before activating")
        sent = self.reconcile("activate-" + request_id, "send")
        with self.state.transaction() as data:
            record = data["handoffs"][request_id]
            if sent.get("turn_id") and record.get("phase") in ("activation_sending", "blocked"):
                # Reconciliation records a known already-started turn; it does not start one.
                if data["owners"].get(record["cwd"]) != record["successor"]:
                    raise RpcError("Coordinator changed; activation cannot be reconciled")
                if record["phase"] == "blocked":
                    record = transition(record, "activation_sending",
                                        reconciled_activation_turn_id=sent["turn_id"])
                data["handoffs"][request_id] = transition(record, "active", status="active",
                    activation_turn_id=sent["turn_id"], reconciled=True, needs_attention=None)
            return copy.deepcopy(data["handoffs"][request_id])
