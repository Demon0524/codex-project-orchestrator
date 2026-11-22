"""Bounded, failure-aware local Codex App Server JSONL client.

Lifecycle and abandoned-request handling are ported/adapted from
j-pollack/codex-app-server-mcp (MIT); see THIRD_PARTY_NOTICES.md.
No upstream permission-granting behavior is imported.
"""
from collections import OrderedDict, deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


MAX_ABANDONED_REQUESTS = 64
MAX_STDERR_CHARS = 64_000


class RpcError(RuntimeError):
    pass


class RpcTimeoutError(RpcError):
    def __init__(self, method, rpc_id):
        self.method, self.rpc_id = method, rpc_id
        super().__init__(f"{method} timed out (RPC {rpc_id}); outcome is uncertain; do not automatically repeat mutations")


def codex_binary():
    override = os.environ.get("CODEX_ORCHESTRATOR_CODEX")
    if override:
        return override
    desktop = Path("/usr/lib/chatgpt/resources/codex")
    found = str(desktop) if desktop.is_file() else shutil.which("codex")
    if not found:
        raise RpcError("Codex executable not found; set CODEX_ORCHESTRATOR_CODEX")
    return found


@dataclass
class PendingRequest:
    method: str
    params: dict
    started: float
    response: queue.Queue


class Backend:
    def __init__(self, command=None, request_timeout=30, version="0.3.0"):
        self.pending = {}
        self.abandoned = OrderedDict()
        self.late_responses = deque(maxlen=MAX_ABANDONED_REQUESTS)
        self.lock = threading.RLock()
        self.events = threading.Condition(self.lock)
        self.sequence = 0
        self.event_sequence = 0
        self.attention = {}
        self.usage = {}
        self.request_timeout = request_timeout
        self._closed = False
        self._closing = False
        self._close_reason = None
        self._stderr_tail = ""
        self._stderr_chars_seen = 0
        self._reader = self._stderr_reader = None
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        socket = home / "app-server-control/app-server-control.sock"
        mode = os.environ.get("CODEX_ORCHESTRATOR_BACKEND", "auto")
        if mode not in ("auto", "proxy", "stdio"):
            raise RpcError("Backend must be auto, proxy or stdio")
        self.mode = "proxy" if mode == "proxy" or (mode == "auto" and socket.exists()) else "stdio"
        cmd = command or ([codex_binary(), "app-server"] +
                          (["proxy", "--sock", str(socket)] if self.mode == "proxy" else ["--stdio"]))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._stderr_reader = threading.Thread(target=self._capture_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        try:
            self.info = self.call("initialize", {
                "clientInfo": {"name": "project_orchestrator", "version": version},
                "capabilities": {"experimentalApi": True},
            })
            self._write({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def _notify_changed(self):
        # Caller holds the shared lock.
        self.event_sequence += 1
        self.events.notify_all()

    def _capture_stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.buffer.read1(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                with self.lock:
                    self._stderr_tail = (self._stderr_tail + text)[-MAX_STDERR_CHARS:]
                    self._stderr_chars_seen += len(text)
        except (OSError, ValueError):
            pass

    def diagnostics(self):
        """Return bounded metadata; stderr can contain credentials, so never expose it."""
        with self.lock:
            return {"closed": self._closed, "reason": self._close_reason,
                    "pending_count": len(self.pending), "abandoned_count": len(self.abandoned),
                    "late_response_count": len(self.late_responses),
                    "stderr_chars_seen": self._stderr_chars_seen,
                    "stderr_chars_retained": len(self._stderr_tail)}

    def _write(self, value):
        with self.lock:
            if self._closed or self._closing or self.proc.poll() is not None:
                raise RpcError(self._close_reason or "Codex backend exited; running turns may need recovery")
            try:
                self.proc.stdin.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._fail("Codex stdin closed; mutation outcome may be uncertain")
                raise RpcError(self._close_reason) from exc

    def _fail(self, reason):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._close_reason = reason
            requests = list(self.pending.values())
            self.pending.clear()
            self.abandoned.clear()
            for request in requests:
                request.response.put({"error": {"message": reason}})
            self._notify_changed()

    def _handle_response(self, value):
        rid = value["id"]
        with self.lock:
            request = self.pending.pop(rid, None)
            if request is not None:
                request.response.put(value)
            else:
                late = self.abandoned.pop(rid, None)
                if late is not None:
                    self.late_responses.append({"rpc_id": rid, "method": late.method,
                        "params": late.params, "result": value.get("result"),
                        "error": value.get("error"), "elapsed_seconds": time.monotonic() - late.started})
            self._notify_changed()

    def _handle_message(self, value):
        if not isinstance(value, dict):
            raise ValueError("JSONL message must be an object")
        if "method" in value:
            method = value["method"]
            params = value.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("Invalid method or params")
            if "id" in value and type(value["id"]) not in (int, str):
                raise ValueError("Invalid server request id")
            with self.lock:
                if "id" in value:
                    tid = params.get("threadId", "unknown")
                    if not isinstance(tid, str):
                        tid = "unknown"
                    self.attention[tid] = method
                    self._notify_changed()
                elif method == "thread/tokenUsage/updated":
                    tid = params.get("threadId")
                    if isinstance(tid, str):
                        self.usage[tid] = params.get("tokenUsage")
                    self._notify_changed()
                elif method == "turn/started":
                    tid = params.get("threadId")
                    if isinstance(tid, str):
                        self.attention.pop(tid, None)
                    self._notify_changed()
                else:
                    self._notify_changed()
            if "id" in value:
                # An unattended bridge must not grant permissions or synthesize user answers.
                self._write({"id": value["id"], "error": {"code": -32001,
                             "message": "Interactive request requires human handling; bridge does not auto-approve"}})
        else:
            if type(value.get("id")) not in (int, str) or ("result" in value) == ("error" in value):
                raise ValueError("Invalid JSON-RPC response")
            self._handle_response(value)

    def _read(self):
        reason = "Codex connection closed; mutation outcome may be uncertain"
        try:
            for line in self.proc.stdout:
                if not line.strip():
                    continue
                self._handle_message(json.loads(line))
        except (ValueError, TypeError, KeyError):
            # Never include raw protocol text in diagnostics: it can contain private prompts.
            reason = "Invalid JSONL from Codex backend; connection closed"
            if self.proc.poll() is None:
                self.proc.terminate()
        except (OSError, RpcError):
            reason = "Codex transport failed; mutation outcome may be uncertain"
        finally:
            self._fail(reason)

    def call(self, method, params=None, timeout=None):
        timeout = self.request_timeout if timeout is None else timeout
        with self.lock:
            if self._closed or self._closing or self.proc.poll() is not None:
                raise RpcError(self._close_reason or "Codex backend is closed")
            self.sequence += 1
            rid = self.sequence
            request = PendingRequest(method, params or {}, time.monotonic(), queue.Queue())
            self.pending[rid] = request
        try:
            self._write({"id": rid, "method": method, "params": request.params})
            try:
                value = request.response.get(timeout=timeout)
            except queue.Empty:
                with self.lock:
                    # A reply can arrive between queue timeout and acquiring this lock.
                    if rid not in self.pending:
                        try:
                            value = request.response.get_nowait()
                        except queue.Empty:
                            raise RpcError(self._close_reason or "Request outcome became uncertain")
                    else:
                        self.pending.pop(rid)
                        self.abandoned[rid] = request
                        while len(self.abandoned) > MAX_ABANDONED_REQUESTS:
                            self.abandoned.popitem(last=False)
                        raise RpcTimeoutError(method, rid)
            if "error" in value:
                error = value["error"]
                if not isinstance(error, dict):
                    raise RpcError(f"{method}: invalid RPC error")
                raise RpcError(f"{method}: {error.get('message', 'RPC failed')}")
            return value.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def drain_late_responses(self):
        """Collect outcomes of timed-out calls without issuing new backend mutations."""
        with self.lock:
            responses = list(self.late_responses)
            self.late_responses.clear()
            return responses

    def wait_for_update(self, after_sequence, timeout=1):
        with self.events:
            self.events.wait_for(lambda: self.event_sequence != after_sequence or self._closed,
                                 timeout=max(0, min(timeout, 45)))
            return self.event_sequence

    def close(self):
        with self.lock:
            if self._closing:
                return
            self._closing = True
        try:
            # EOF, then SIGTERM, then SIGKILL: each phase has a bounded deadline.
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except (OSError, ValueError):
                    pass
            for action in (None, self.proc.terminate, self.proc.kill):
                if self.proc.poll() is not None:
                    break
                if action:
                    try:
                        action()
                    except ProcessLookupError:
                        break
                try:
                    self.proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            self._fail("Codex backend closed")
        finally:
            current = threading.current_thread()
            for thread in (self._reader, self._stderr_reader):
                if thread and thread is not current:
                    thread.join(timeout=1)
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()
