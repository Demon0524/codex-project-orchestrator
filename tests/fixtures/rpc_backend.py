"""Deterministic fake JSONL server; no Codex invocation or model requests."""
import json
import signal
import sys
import time

scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"
approval_call = None


def emit(value):
    print(json.dumps(value), flush=True)


if scenario == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    rid = request.get("id")
    if method == "initialize":
        if scenario == "bad-init":
            print("private-token-do-not-display", flush=True)
        else:
            emit({"id": rid, "result": {"fake": True}})
    elif method == "never":
        pass
    elif method == "late":
        time.sleep(0.08)
        emit({"id": rid, "result": {"thread": {"id": "late-created-thread"}}})
    elif method == "invalid":
        print("this-is-not-json-and-is-private", flush=True)
    elif method == "array":
        emit(["not", "an", "object"])
    elif method == "die":
        sys.exit(3)
    elif method == "stderr":
        sys.stderr.write("secret_token=never-echo-this\n" + "x" * 100_000 + "\n")
        sys.stderr.flush()
        emit({"id": rid, "result": {"ok": True}})
    elif method == "approval":
        approval_call = rid
        emit({"id": 900, "method": "item/commandExecution/requestApproval",
              "params": {"threadId": "test-thread", "command": "do not execute"}})
    elif method == "usage":
        emit({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "test-thread", "tokenUsage": {"total": {"totalTokens": 123}}}})
        emit({"id": rid, "result": {"ok": True}})
    elif method == "new-turn":
        emit({"method": "turn/started", "params": {"threadId": "test-thread", "turn": {"id": "new-turn"}}})
        emit({"id": rid, "result": {"ok": True}})
    elif method == "initialized":
        pass
    elif method is not None:
        emit({"id": rid, "result": {"echo": request.get("params", {})}})
    elif rid == 900:
        emit({"id": approval_call, "result": {"reply": request}})

if scenario == "stubborn":
    time.sleep(60)
