"""Tool server exposing deep_research(query, mode) to MCP clients.

Two transports:
  * stdio (default) — what local AI apps launch; the setup wizard writes
    their configs to point here and verifies with a handshake.
  * --http — the remote-connector mode the wizard runs as a background
    service behind an HTTPS tunnel or reverse proxy. POST a JSON-RPC body to
    /, authenticated with a bearer token (env RAVE_MCP_TOKEN, or ?token=…
    in the URL for connector UIs that only take an address). Content-Type/
    Accept are validated and Mcp-Session-Id is passed through (or minted on
    initialize) for streamable-http-style clients; server-initiated SSE
    streams are not supported.

Both speak the minimal protocol subset needed to list and call one tool:
  initialize → tools/list → tools/call {name: "deep_research"}.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from urllib.parse import parse_qs, urlsplit

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


def serve_http(port: int, host: str = "0.0.0.0", runner=deep_research,
               token: str | None = None):
    """HTTP front for remote connectors: POST a JSON-RPC body to /.

    Authentication is required on every POST: `Authorization: Bearer <token>`
    or `?token=<token>` in the URL (for connector UIs that only accept an
    address). The token comes from the argument or the RAVE_MCP_TOKEN env
    var; with no token configured, all POSTs are rejected with a hint.
    Returns the server; call serve_forever().
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    expected = os.environ.get("RAVE_MCP_TOKEN", "") if token is None else token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _reply(self, code: int, payload: dict,
                   extra_headers: dict | None = None) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not expected:
                return False
            if self.headers.get("Authorization", "") == f"Bearer {expected}":
                return True
            qs = parse_qs(urlsplit(self.path).query)
            return expected in qs.get("token", [])

        def do_GET(self):
            if "text/event-stream" in (self.headers.get("Accept") or ""):
                # streamable-http server-push streams are not supported
                self._reply(405, {"error": "SSE not supported; POST JSON-RPC"},
                            {"Allow": "POST"})
                return
            self._reply(200, {"ok": True, "server": SERVER_INFO["name"]})

        def do_POST(self):
            if not self._authorized():
                hint = (
                    "send Authorization: Bearer <token> or ?token=<token>"
                    if expected else
                    "server has no token configured; set RAVE_MCP_TOKEN"
                )
                self._reply(401, {"error": "unauthorized", "hint": hint})
                return
            ctype = self.headers.get("Content-Type", "") or ""
            if "application/json" not in ctype:
                self._reply(415, {"error": "Content-Type must be application/json"})
                return
            accept = self.headers.get("Accept") or "*/*"
            if not any(t in accept for t in
                       ("application/json", "*/*", "text/event-stream")):
                self._reply(406, {"error": "responses are application/json"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._reply(400, {"error": "invalid JSON"})
                return
            session_id = self.headers.get("Mcp-Session-Id", "")
            if not session_id and req.get("method") == "initialize":
                session_id = uuid.uuid4().hex
            resp = handle_request(req, runner=runner)
            headers = {"Mcp-Session-Id": session_id} if session_id else None
            self._reply(200, resp if resp is not None else {"ok": True}, headers)

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    import argparse
    import secrets
    from pathlib import Path

    from wizard.envfile import load_env

    load_env(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true",
                        help="serve JSON-RPC over HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.http:
        if not os.environ.get("RAVE_MCP_TOKEN"):
            ephemeral = secrets.token_urlsafe(24)
            os.environ["RAVE_MCP_TOKEN"] = ephemeral
            print(f"RAVE_MCP_TOKEN not set; using one-off token: {ephemeral}",
                  file=sys.stderr)
        serve_http(args.port).serve_forever()
    else:
        serve()
