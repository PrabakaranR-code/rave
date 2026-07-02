"""Stub tool server exposing deep_research(query, mode) over JSON-RPC/stdio.

This is a forward-looking deployment shim for a future remote-connector
setup; it is NOT wired into the main CLI flow. It speaks the minimal subset
of the model-context tool protocol needed to list and call one tool:
  initialize → tools/list → tools/call {name: "deep_research"}.

Run:  python mcp_server.py   (reads JSON-RPC requests line-by-line on stdin)
"""
from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "rave", "version": "0.1.0"}

DEEP_RESEARCH_TOOL = {
    "name": "deep_research",
    "description": (
        "Run a full research-and-verify pipeline on a question: plan, "
        "parallel live-web research, deterministic cross-check, adversarial "
        "verification, and a fully cited markdown report."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "the question to research"},
            "mode": {
                "type": "string",
                "enum": ["speed", "balanced", "quality"],
                "default": "balanced",
            },
        },
        "required": ["query"],
    },
}


def deep_research(query: str, mode: str = "balanced", config_path: str = "config.yaml") -> str:
    """Run the engine and return the rendered markdown report."""
    from agents.orchestrator import Orchestrator
    from config import load_config

    orch = Orchestrator(
        load_config(config_path), mode=mode, out_path=None, runlog_path=None
    )
    return orch.run(query).markdown


def handle_request(req: dict, runner=deep_research) -> dict | None:
    """Dispatch one JSON-RPC request; notifications return None."""
    method = req.get("method", "")
    req_id = req.get("id")
    if req_id is None:  # notification (e.g. notifications/initialized)
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "tools/list":
        return ok({"tools": [DEEP_RESEARCH_TOOL]})
    if method == "tools/call":
        params = req.get("params", {})
        if params.get("name") != "deep_research":
            return err(-32602, f"unknown tool {params.get('name')!r}")
        args = params.get("arguments", {})
        if not args.get("query"):
            return err(-32602, "deep_research requires a 'query' argument")
        try:
            report_md = runner(args["query"], args.get("mode", "balanced"))
        except Exception as e:  # noqa: BLE001 — surface as tool error content
            return ok({"content": [{"type": "text", "text": f"run failed: {e}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": report_md}]})
    return err(-32601, f"method {method!r} not supported by this stub")


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
