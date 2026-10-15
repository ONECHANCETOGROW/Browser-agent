"""Model Context Protocol (stdio) adapter for the collaboration API, so any MCP-capable AI client can use the same capabilities as native tools.

It is a thin translation layer: every tool call becomes the same authenticated /api/collab request the CLI would make (same token, same idempotency,
same scope and approval rules). It adds no capability of its own. Run:  python -m octg_agent.collab.mcp_server
Environment: OCTG_COLLAB_BASE (default from config), OCTG_COLLAB_TOKEN_FILE (default <data dir>/collab_<caller>.token), OCTG_COLLAB_CALLER.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .client import CollabClient, CollabClientError
from .playbook import FULL as PLAYBOOK_FULL, SHORT as PLAYBOOK_SHORT

SERVER = {"name": "octg-collab", "version": "1.0.0"}
TOOLS = [
    {"name": "collab_playbook", "description": "READ THIS FIRST. The owner's guide to using the eye: which source answers which question, recipes, rules, pitfalls. Call it once at the start of any research or browsing task.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "collab_capabilities", "description": "List the local agent's capabilities with their input schemas, permissions, scope, side effects, approval rules and limitations.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "collab_task_new", "description": "Start a parent collaboration task (optionally bound to one client). Returns task_id.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "objective": {"type": "string"}, "client": {"type": "string"}}, "required": ["title"]}},
    {"name": "collab_task_show", "description": "Show a task with its requests, artifacts and approvals.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "collab_task_finish", "description": "Close a task: completed | failed | cancelled, with an outcome line and summary.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["completed", "failed", "cancelled"]}, "outcome": {"type": "string"},
                                                      "summary": {"type": "string"}}, "required": ["task_id", "status"]}},
    {"name": "collab_run", "description": "Submit a capability request and wait (bounded) for its structured result. Re-sending the same request_id never runs it twice.",
     "inputSchema": {"type": "object", "properties": {"capability": {"type": "string"}, "parameters": {"type": "object"}, "client": {"type": "string"}, "task_id": {"type": "string"},
                                                      "timeout_s": {"type": "number"}, "idempotency_key": {"type": "string"}, "request_id": {"type": "string"},
                                                      "max_wait_s": {"type": "number", "description": "How long to wait before returning a still-running job (default 120)."}},
                     "required": ["capability"]}},
    {"name": "collab_get", "description": "Get a request's status/result, optionally waiting up to wait_s seconds.",
     "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}, "wait_s": {"type": "number"}}, "required": ["request_id"]}},
    {"name": "collab_cancel", "description": "Cancel a queued or running request.", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "collab_artifact", "description": "Read an artifact (finding set, report, handoff, change, verification).",
     "inputSchema": {"type": "object", "properties": {"artifact_id": {"type": "string"}}, "required": ["artifact_id"]}},
    {"name": "collab_evidence", "description": "Read one piece of evidence (content + hash-checked metadata).",
     "inputSchema": {"type": "object", "properties": {"evidence_id": {"type": "string"}}, "required": ["evidence_id"]}},
]


def _client() -> CollabClient:
    tf = os.environ.get("OCTG_COLLAB_TOKEN_FILE")
    tok = Path(tf).read_text(encoding="utf-8").strip() if tf else None
    kw = {"caller": os.environ["OCTG_COLLAB_CALLER"]} if os.environ.get("OCTG_COLLAB_CALLER") else {}
    return CollabClient(base_url=os.environ.get("OCTG_COLLAB_BASE"), token=tok, **kw)


def call_tool(c: CollabClient, name: str, a: dict) -> dict:
    if name == "collab_playbook":
        return {"playbook": PLAYBOOK_FULL}
    if name == "collab_capabilities":
        return c.capabilities()
    if name == "collab_task_new":
        return c.new_task(a["title"], objective=a.get("objective", ""), client=a.get("client"))
    if name == "collab_task_show":
        return c.task(a["task_id"])
    if name == "collab_task_finish":
        return c.finish_task(a["task_id"], a["status"], outcome=a.get("outcome", ""), summary=a.get("summary", ""))
    if name == "collab_run":
        return c.run(a["capability"], a.get("parameters") or {}, max_wait_s=float(a.get("max_wait_s", 120)), client=a.get("client"), task_id=a.get("task_id"),
                     timeout_s=a.get("timeout_s"), idempotency_key=a.get("idempotency_key"), request_id=a.get("request_id"))
    if name == "collab_get":
        return c.get(a["request_id"], float(a.get("wait_s", 0)))
    if name == "collab_cancel":
        return c.cancel(a["request_id"])
    if name == "collab_artifact":
        return c.artifact(a["artifact_id"])
    if name == "collab_evidence":
        return c.evidence(a["evidence_id"])
    raise KeyError(name)


def handle(msg: dict, c: CollabClient | None) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                                                       "serverInfo": SERVER, "instructions": PLAYBOOK_SHORT}}
    if method in ("notifications/initialized", "initialized") or mid is None:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            out = call_tool(c if (c or p.get("name") == "collab_playbook") else _client(), p.get("name", ""), p.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}], "isError": False}}
        except CollabClientError as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(e.body, default=str)}], "isError": True}}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool or missing argument: {e}"}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def main() -> None:
    client = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") == "tools/call" and client is None:
            client = _client()
        out = handle(msg, client)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
