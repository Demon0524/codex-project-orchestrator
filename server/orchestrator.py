#!/usr/bin/env python3
"""Local MCP adapter. Workflow and RPC implementations are independently tested."""
import argparse
import json
import sys

from rpc_client import Backend, RpcError, RpcTimeoutError, codex_binary
from workflow import State, Toolkit, VERSION, fingerprint, text_item


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
TOOLS = [
    ("health", "Check local backend, project discovery and runtime limitations.", schema()),
    ("projects", "List local projects and real App Server project IDs.", schema()),
    ("create", "Create and start a persistent independent chat. Requires human authorization. Stable request_id prevents duplicate creation.", schema(
        {"request_id": STRING, "cwd": STRING, "prompt": STRING, "title": STRING, "project_id": STRING,
         "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"], "default": "read-only"}, "coordinator_id": STRING},
        ["request_id", "cwd", "prompt", "title"])),
    ("read", "Read thread metadata, recent results and observed usage; no cross-host access.", schema({"thread_id": STRING}, ["thread_id"])),
    ("send", "Continue a toolkit-created chat with an authorized task. Does not change its permissions.", schema(
        {"request_id": STRING, "thread_id": STRING, "prompt": STRING, "coordinator_id": STRING}, ["request_id", "thread_id", "prompt"])),
    ("wait", "Wait at most 45 seconds for completion or attention. Repeat only when needed.", schema(
        {"thread_id": STRING, "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 45}}, ["thread_id"])),
    ("handoff", "Prepare a successor coordinator from an existing checkpoint. Verification turn performs no work; read-only sandbox is default. Requires authorization to create the successor.", schema(
        {"request_id": STRING, "source_thread_id": STRING, "cwd": STRING, "checkpoint": STRING, "checkpoint_ref": STRING, "goal": STRING, "project_id": STRING,
         "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"], "default": "read-only"}},
        ["request_id", "source_thread_id", "cwd", "goal"])),
    ("handoff_status", "Verify acceptance marker and unchanged checkpoint before committing coordinator ownership. This does not mechanically stop the old agent.", schema({"request_id": STRING}, ["request_id"])),
]



STATE_ARRAY = {"type": "array", "items": STRING, "maxItems": 256}
STATE_ARRAY_KEYS = ("constraints_preferences", "key_decisions", "rejected_attempts", "verification", "critical_context", "uncertainties", "next_steps")
STATE_SCHEMA = schema({"schema_version": {"type": "integer", "enum": [1]}, "goal": STRING,
    "progress": schema({k: STATE_ARRAY for k in ("done", "in_progress", "pending")}, ["done", "in_progress", "pending"]),
    **{k: STATE_ARRAY for k in STATE_ARRAY_KEYS}}, ["schema_version", "goal", "progress", *STATE_ARRAY_KEYS])
TOOLS.extend([
    ("resume_create", "Explicitly finish a reconciled, known-created chat with matching original inputs; never repeats a possibly-sent turn.", schema(
        {"request_id": STRING, "cwd": STRING, "prompt": STRING, "title": STRING, "project_id": STRING,
         "coordinator_id": STRING, "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"]}},
        ["request_id", "cwd", "prompt", "title"])),
    ("handoff_resume", "Explicitly resume a reconciled known-created successor using the persisted original verification task. No duplicate chat.", schema(
        {"request_id": STRING}, ["request_id"])),
    ("wait_many", "Collect up to 16 chats with a common deadline; wake on any/all completion or attention.", schema(
        {"thread_ids": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 16, "uniqueItems": True},
         "mode": {"type": "string", "enum": ["any", "all"]},
         "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 45}}, ["thread_ids"])),
    ("reconcile", "Reconcile late RPC replies for an uncertain create/send. Never retries the mutation. Late replies are connection-local.", schema(
        {"request_id": STRING, "operation": {"type": "string", "enum": ["create", "send"]}}, ["request_id"])),
    ("checkpoint_save", "Validate, best-effort redact and immutably save structured project state with an upstream-compatible schema.", schema(
        {"request_id": STRING, "state": STATE_SCHEMA}, ["request_id", "state"])),
    ("checkpoint_read", "Verify and read a saved checkpoint reference; return bounded Markdown.", schema(
        {"checkpoint_ref": STRING}, ["checkpoint_ref"])),
    ("handoff_reconcile", "Recover known successor/verification IDs after uncertain creation without duplicating a chat.", schema(
        {"request_id": STRING}, ["request_id"])),
    ("handoff_activate", "Activate an accepted successor exactly once after rechecking checkpoint and coordinator ownership. Requires existing human authorization.", schema(
        {"request_id": STRING}, ["request_id"])),
    ("handoff_activation_reconcile", "Record a known already-started activation turn after a late reply; never sends another activation.", schema(
        {"request_id": STRING}, ["request_id"])),
])


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
                                         "annotations": {"readOnlyHint": n in {"health", "projects", "read", "wait", "wait_many", "checkpoint_read"},
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
                        if rule.get("type") == "object" and not isinstance(value, dict):
                            raise RpcError(f"{key} must be an object")
                        if rule.get("type") == "array" and (not isinstance(value, list) or
                            not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 256) or
                            any(not isinstance(v, str) or not v.strip() for v in value)):
                            raise RpcError(f"{key} must contain nonblank strings within size bounds")
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
