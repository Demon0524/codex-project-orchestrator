#!/usr/bin/env python3
"""Local MCP bridge for persistent Codex threads. Python 3.11+, POSIX."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

VERSION = "0.2.0"


class RpcError(RuntimeError):
    pass


def codex_binary():
    override = os.environ.get("CODEX_ORCHESTRATOR_CODEX")
    if override:
        return override
    desktop = Path("/usr/lib/chatgpt/resources/codex")
    found = str(desktop) if desktop.is_file() else shutil.which("codex")
    if not found:
        raise RpcError("Codex executable not found; set CODEX_ORCHESTRATOR_CODEX")
    return found


class Backend:
    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.attention = {}
        self.usage = {}
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        socket = home / "app-server-control/app-server-control.sock"
        mode = os.environ.get("CODEX_ORCHESTRATOR_BACKEND", "auto")
        if mode not in ("auto", "proxy", "stdio"):
            raise RpcError("Backend must be auto, proxy or stdio")
        self.mode = "proxy" if mode == "proxy" or (mode == "auto" and socket.exists()) else "stdio"
        cmd = [codex_binary(), "app-server"]
        cmd += ["proxy", "--sock", str(socket)] if self.mode == "proxy" else ["--stdio"]
        # stdout belongs exclusively to the backend protocol. Do not forward logs to MCP.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()
        self.info = self.call("initialize", {
            "clientInfo": {"name": "project_orchestrator", "version": VERSION},
            "capabilities": {"experimentalApi": True},
        })
        self._write({"method": "initialized"})

    def _write(self, value):
        with self.lock:
            if self.proc.poll() is not None:
                raise RpcError("Codex backend exited; running turns may need recovery")
            self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _read(self):
        try:
            for line in self.proc.stdout:
                value = json.loads(line)
                if "method" in value:
                    params = value.get("params", {})
                    if "id" in value:
                        # Never auto-grant approvals, secrets, user input, or unknown requests.
                        tid = params.get("threadId", "unknown")
                        self.attention[tid] = value["method"]
                        self._write({"id": value["id"], "error": {
                            "code": -32001, "message": "Interactive request requires human handling; bridge does not auto-approve"
                        }})
                    elif value["method"] == "thread/tokenUsage/updated":
                        self.usage[params.get("threadId")] = params.get("tokenUsage")
                else:
                    with self.lock:
                        target = self.pending.get(value.get("id"))
                    if target:
                        target.put(value)
        except Exception:
            pass
        finally:
            with self.lock:
                targets = list(self.pending.values())
            for target in targets:
                target.put({"error": {"message": "Codex connection closed; outcome may be uncertain"}})

    def call(self, method, params=None, timeout=30):
        with self.lock:
            self.sequence += 1
            rid = self.sequence
            response = queue.Queue()
            self.pending[rid] = response
        try:
            self._write({"id": rid, "method": method, "params": params or {}})
            try:
                value = response.get(timeout=timeout)
            except queue.Empty:
                raise RpcError(f"{method} timed out; do not repeat mutations with a new request_id")
            if "error" in value:
                raise RpcError(f"{method}: {value['error'].get('message', 'RPC failed')}")
            return value.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


class State:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get(
            "CODEX_ORCHESTRATOR_STATE", str(Path.home() / ".local/state/codex-project-orchestrator")))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def transaction(self):
        lockpath = self.directory / "state.lock"
        fd = os.open(lockpath, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / "state.json"
            data = json.loads(path.read_text()) if path.exists() else {"requests": {}, "handoffs": {}, "owners": {}}
            try:
                yield data
            finally:
                tmp = self.directory / "state.tmp"
                out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(out, "w") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                fcntl.flock(lock, fcntl.LOCK_UN)


def text_item(prompt):
    return [{"type": "text", "text": prompt}]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Toolkit:
    def __init__(self, backend, state=None):
        self.backend = backend
        self.state = state or State()

    def projects(self):
        data, cursor = [], None
        while True:
            page = self.backend.call("project/list", {"limit": 100, "cursor": cursor})
            data.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return {"projects": data}

    def health(self):
        projects = self.projects()
        return {"version": VERSION, "backend": self.backend.mode, "connected": True,
                "project_count": len(projects["projects"]),
                "scope": "local Codex home only; not cloud/durable hosts",
                "lifetime": "shared daemon" if self.backend.mode == "proxy" else "MCP process; active turns may stop on disconnect",
                "approval_handling": "interactive requests fail closed and are reported; never auto-approved",
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

    def create(self, request_id, cwd, prompt, title, project_id=None, sandbox="read-only"):
        cwd = self._project(project_id, cwd)
        if sandbox not in ("read-only", "workspace-write"):
            raise RpcError("Unsupported sandbox; broad permission bypass is not offered")
        key = "create:" + request_id
        digest = fingerprint([cwd, project_id, prompt, title, sandbox])
        # Persist 'creating' before any mutation. Uncertain requests never auto-repeat.
        with self.state.transaction() as data:
            old = data["requests"].get(key)
            if old:
                if old["fingerprint"] != digest:
                    raise RpcError("request_id reused with different arguments")
                return {**old, "replayed": True}
            data["requests"][key] = {"fingerprint": digest, "status": "creating"}
        result = self.backend.call("thread/start", {"cwd": cwd, "projectId": project_id,
                                   "sandbox": sandbox, "approvalPolicy": "on-request"})
        tid = result["thread"]["id"]
        with self.state.transaction() as data:
            data["requests"][key].update(thread_id=tid, status="created", cwd=cwd)
        self.backend.call("thread/name/set", {"threadId": tid, "name": title})
        # A newly created thread is already loaded; do not resume it again.
        turn = self.backend.call("turn/start", {"threadId": tid, "input": text_item(prompt)})
        with self.state.transaction() as data:
            entry = data["requests"][key]
            entry.update(status="started", turn_id=turn["turn"]["id"])
            return dict(entry)

    def _owned(self, thread_id):
        with self.state.transaction() as data:
            if not any(x.get("thread_id") == thread_id for x in data["requests"].values()):
                raise RpcError("Mutation restricted to threads created by this toolkit")

    def read(self, thread_id):
        meta = self.backend.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        turns = self.backend.call("thread/turns/list", {"threadId": thread_id, "limit": 3,
                                   "sortDirection": "desc", "itemsView": "full"})
        compact = []
        for original in turns.get("data", []):
            turn = {k: original.get(k) for k in ("id", "status", "error", "startedAt", "completedAt")}
            turn["items"] = []
            items = original.get("items", [])
            messages = [i for i in items if i.get("type") == "agentMessage"]
            for item in messages[-20:]:
                text = item.get("text", "")
                trimmed = len(text) > 12000
                if trimmed:
                    text = text[:6000] + "\n[output truncated]\n" + text[-6000:]
                turn["items"].append({"type": "agentMessage", "text": text,
                                      "phase": item.get("phase"), "truncated": trimmed})
            turn["omitted_item_count"] = len(items) - len(turn["items"])
            compact.append(turn)
        thread = meta.get("thread", {})
        return {"thread": {k: thread.get(k) for k in ("id", "cwd", "name", "projectId", "status")}, "turns": compact,
                "next_cursor": turns.get("nextCursor"),
                "needs_attention": self.backend.attention.get(thread_id),
                "observed_token_usage": self.backend.usage.get(thread_id)}

    def send(self, request_id, thread_id, prompt):
        self._owned(thread_id)
        key, digest = "send:" + request_id, fingerprint([thread_id, prompt])
        with self.state.transaction() as data:
            old = data["requests"].get(key)
            if old:
                if old["fingerprint"] != digest:
                    raise RpcError("request_id reused with different arguments")
                return {**old, "replayed": True}
            current = self.read(thread_id)
            if any(t.get("status") == "inProgress" for t in current["turns"]):
                raise RpcError("Thread is running; wait before sending a follow-up")
            data["requests"][key] = {"fingerprint": digest, "status": "sending", "thread_id": thread_id}
        self.backend.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        turn = self.backend.call("turn/start", {"threadId": thread_id, "input": text_item(prompt)})
        with self.state.transaction() as data:
            data["requests"][key].update(status="started", turn_id=turn["turn"]["id"])
            return dict(data["requests"][key])

    def wait(self, thread_id, timeout_seconds=30):
        deadline = time.monotonic() + max(0, min(timeout_seconds, 45))
        while True:
            result = self.read(thread_id)
            running = any(t.get("status") == "inProgress" for t in result["turns"])
            if result["needs_attention"] or not running or time.monotonic() >= deadline:
                result["timed_out"] = running and not result["needs_attention"]
                return result
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def handoff(self, request_id, source_thread_id, cwd, checkpoint, goal, project_id=None, sandbox="read-only"):
        cwd = self._project(project_id, cwd)
        path = Path(checkpoint).expanduser().resolve(strict=True)
        if Path(cwd) not in path.parents or not path.is_file():
            raise RpcError("Checkpoint must be a file inside the project directory")
        self.backend.call("thread/read", {"threadId": source_thread_id, "includeTurns": False})
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if sandbox not in ("read-only", "workspace-write"):
            raise RpcError("Unsupported sandbox")
        hid = fingerprint([request_id, source_thread_id, cwd, str(path), digest, goal, sandbox])[:24]
        with self.state.transaction() as data:
            old = data["handoffs"].get(request_id)
            if old:
                if old["id"] != hid:
                    raise RpcError("Handoff request_id reused with different checkpoint or goal")
                return dict(old)
            owner = data["owners"].get(cwd)
            if owner and owner != source_thread_id:
                raise RpcError("Another coordinator is already recorded for this project")
            if any(h["cwd"] == cwd and h["status"] == "pending" for h in data["handoffs"].values()):
                raise RpcError("Another handoff is pending; inspect it before starting another")
            data["owners"][cwd] = source_thread_id
            data["handoffs"][request_id] = {"id": hid, "source": source_thread_id, "cwd": cwd,
                "checkpoint": str(path), "sha256": digest, "goal": goal, "status": "pending"}
        prompt = (f"This is a coordinator takeover verification. Read applicable AGENTS.md and {path}. "
                  f"Verify working tree, constraints and next action for this goal: {goal}. "
                  "Do not modify files, launch workers, or execute the goal yet. "
                  f"If ready to take over, finish with exactly HANDOFF_ACCEPTED:{hid}; otherwise report the blocker. "
                  "A later user-authorized follow-up will activate execution.")
        created = self.create("handoff-" + request_id, cwd, prompt, "Coordinator takeover", project_id, sandbox)
        with self.state.transaction() as data:
            data["handoffs"][request_id]["successor"] = created["thread_id"]
            return dict(data["handoffs"][request_id])

    def handoff_status(self, request_id):
        with self.state.transaction() as data:
            record = dict(data["handoffs"][request_id])
        if "successor" not in record:
            return {**record, "needs_attention": "Creation outcome uncertain; reconcile before retrying"}
        result = self.read(record["successor"])
        marker = "HANDOFF_ACCEPTED:" + record["id"]
        accepted = any(t.get("status") == "completed" and any(
            i.get("type") == "agentMessage" and i.get("text", "").strip().endswith(marker)
            for i in t.get("items", [])) for t in result["turns"])
        with self.state.transaction() as data:
            live = data["handoffs"][request_id]
            unchanged = hashlib.sha256(Path(live["checkpoint"]).read_bytes()).hexdigest() == live["sha256"]
            if live["status"] == "pending" and accepted and unchanged:
                if data["owners"].get(live["cwd"]) != live["source"]:
                    raise RpcError("Coordinator changed concurrently; takeover not committed")
                data["owners"][live["cwd"]] = live["successor"]
                live["status"] = "accepted"
            record = dict(live)
        return {**record, "checkpoint_unchanged": unchanged,
                "next_action": "Send the authorized goal to successor; old coordinator stops dispatching"
                if record["status"] == "accepted" else "Wait or inspect blocker; do not activate successor"}


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
TOOLS = [
    ("health", "Check local backend, project discovery and runtime limitations.", schema()),
    ("projects", "List local projects and real App Server project IDs.", schema()),
    ("create", "Create and start a persistent independent chat. Requires human authorization. Stable request_id prevents duplicate creation.", schema(
        {"request_id": STRING, "cwd": STRING, "prompt": STRING, "title": STRING, "project_id": STRING,
         "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"], "default": "read-only"}},
        ["request_id", "cwd", "prompt", "title"])),
    ("read", "Read thread metadata, recent results and observed usage; no cross-host access.", schema({"thread_id": STRING}, ["thread_id"])),
    ("send", "Continue a toolkit-created chat with an authorized task. Does not change its permissions.", schema(
        {"request_id": STRING, "thread_id": STRING, "prompt": STRING}, ["request_id", "thread_id", "prompt"])),
    ("wait", "Wait at most 45 seconds for completion or attention. Repeat only when needed.", schema(
        {"thread_id": STRING, "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 45}}, ["thread_id"])),
    ("handoff", "Prepare a successor coordinator from an existing checkpoint. Verification turn performs no work; read-only sandbox is default. Requires authorization to create the successor.", schema(
        {"request_id": STRING, "source_thread_id": STRING, "cwd": STRING, "checkpoint": STRING, "goal": STRING, "project_id": STRING,
         "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"], "default": "read-only"}},
        ["request_id", "source_thread_id", "cwd", "checkpoint", "goal"])),
    ("handoff_status", "Verify acceptance marker and unchanged checkpoint before committing coordinator ownership. This does not mechanically stop the old agent.", schema({"request_id": STRING}, ["request_id"])),
]


def serve():
    backend, toolkit = None, None
    try:
        for line in sys.stdin:
            rid = None
            message = {}
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    message = {}
                    raise RpcError("MCP request must be an object")
                rid = message.get("id")
                method = message.get("method")
                if rid is None:
                    continue
                if method == "initialize":
                    result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "project-orchestrator", "version": VERSION}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": [{"name": "orchestrator_" + n, "description": d, "inputSchema": s,
                                         "annotations": {"readOnlyHint": n in {"health", "projects", "read", "wait"},
                                                         "destructiveHint": False,
                                                         "openWorldHint": False}}
                                        for n, d, s in TOOLS]}
                elif method == "tools/call":
                    params = message["params"]
                    name = params["name"].removeprefix("orchestrator_")
                    known = {n for n, _, _ in TOOLS}
                    if name not in known:
                        raise RpcError("Unknown tool")
                    args = params.get("arguments", {})
                    definition = next(s for n, _, s in TOOLS if n == name)
                    if not isinstance(args, dict) or set(args) - set(definition["properties"]):
                        raise RpcError("Unexpected arguments")
                    if any(k not in args for k in definition["required"]):
                        raise RpcError("Missing required arguments")
                    for key, value in args.items():
                        rule = definition["properties"][key]
                        if rule.get("type") == "string" and (not isinstance(value, str) or not value.strip()):
                            raise RpcError(f"{key} must be a nonblank string")
                        if rule.get("type") == "integer" and (type(value) is not int or not rule.get("minimum", 0) <= value <= rule.get("maximum", 45)):
                            raise RpcError(f"{key} must be an integer within the documented bounds")
                        if "enum" in rule and value not in rule["enum"]:
                            raise RpcError(f"Unsupported value for {key}")
                    if toolkit is None:
                        backend = Backend()
                        toolkit = Toolkit(backend)
                    value = getattr(toolkit, name)(**args)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                else:
                    raise RpcError("Unsupported MCP method")
                reply = {"jsonrpc": "2.0", "id": rid, "result": result}
            except Exception as exc:
                if message.get("method") == "tools/call":
                    reply = {"jsonrpc": "2.0", "id": rid, "result": {"isError": True,
                             "content": [{"type": "text", "text": str(exc)}]}}
                else:
                    reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(exc)}}
            print(json.dumps(reply, ensure_ascii=False), flush=True)
    finally:
        if backend:
            backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true", help="Run a harmless backend connection check")
    args = parser.parse_args()
    if args.health:
        backend = Backend()
        try:
            print(json.dumps(Toolkit(backend).health(), ensure_ascii=False, indent=2))
        finally:
            backend.close()
    else:
        serve()
