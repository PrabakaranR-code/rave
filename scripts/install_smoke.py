#!/usr/bin/env python3
"""Install smoke test: a real CLI run against local scripted servers.

Starts one localhost HTTP server that plays both the LLM endpoint (scripted
OpenAI-style tool calls) and a small website + metasearch API (fixtures from
tests/), writes a config pointing at it, then runs the installed CLI in
speed mode. Passes when the run exits 0 and report.md contains the expected
sections. No external network, no API keys.

Usage:  python scripts/install_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
OUT = REPO / ".rave" / "smoke"

QUESTION = "What did the city council approve in the new transit budget?"
CLAIM = "The council approved a transit budget of 48 million dollars"

queues: dict[str, deque] = defaultdict(deque)
qlock = threading.Lock()


def enq(key: str, *calls):
    queues[key].append(list(calls))


def build_script(base: str) -> None:
    article = f"{base}/article"
    enq("classify_query", ("classify_query", {
        "standalone_question": QUESTION,
        "topic_kind": "fast_moving",
        "needs_clarification": False,
    }))
    enq("create_research_plan", ("create_research_plan", {
        "standalone_query": QUESTION,
        "sub_questions": [
            {"id": f"sq{i}", "question": q, "done_criteria": "a sourced fact"}
            for i, q in enumerate(
                ["What total was approved?", "What does it fund?",
                 "What objections were raised?"], 1)
        ],
    }))
    for _ in range(6):
        enq("plan_preamble", ("plan_preamble", {
            "current_goal": "gather sourced evidence", "next_action": "search",
            "expected_info": "facts with URLs",
        }))
    facts = {
        "sq1": (CLAIM, "approve a transit budget of 48 million dollars"),
        "sq2": ("The plan funds forty new electric buses and a light-rail extension",
                "funds forty new electric buses"),
        "sq3": ("Opponents argued the plan relies on optimistic ridership projections",
                "the plan relies on optimistic ridership projections"),
    }
    for sq, (claim, quote) in facts.items():
        enq(f"action:{sq}", ("web_search", {"query": "city transit budget", "top_k": 2}))
        enq(f"action:{sq}",
            ("record_findings", {"findings": [{
                "claim": claim, "url": article, "date": "2026-05-14",
                "source_type": "news", "confidence": "M", "quote": quote,
            }]}),
            ("done", {"coverage_summary": f"{sq} covered", "gaps": []}))
    enq("critique_findings", ("critique_findings", {"issues": []}))
    enq("write_report", ("write_report", {
        "title": "City transit budget: what was approved",
        "exec_summary": ["The council approved a 48 million dollar budget."],
        "findings": [
            {"claim": c, "citations": [article], "confidence": "M",
             "date": "2026-05-14"} for c, _q in facts.values()
        ],
        "verification_log": {"cycles_run": 0, "changes": [], "unverified": []},
    }))


def pick_response(payload: dict) -> list:
    names = sorted(t["function"]["name"] for t in payload["tools"])
    key = names[0] if len(names) == 1 else "action"
    if key == "action":
        convo = "\n".join(m["content"] for m in payload["messages"]
                          if isinstance(m.get("content"), str))
        m = re.search(r"YOUR sub-question \((\w+)\)", convo)
        if m and queues.get(f"action:{m.group(1)}"):
            key = f"action:{m.group(1)}"
    with qlock:
        if not queues[key]:
            raise RuntimeError(f"no scripted response left for {key!r}")
        return queues[key].popleft()


class Handler(BaseHTTPRequestHandler):
    base = ""  # set after the port is known

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            self._send(200, b"User-agent: *\nAllow: /\n", "text/plain")
        elif path == "/article":
            self._send(200, (FIXTURES / "article.html").read_bytes(), "text/html")
        elif path == "/plain":
            self._send(200, (FIXTURES / "plain.html").read_bytes(), "text/html")
        elif path == "/search":
            body = json.dumps({"results": [
                {"url": f"{self.base}/article", "title": "Budget vote",
                 "content": "council transit budget"},
                {"url": f"{self.base}/plain", "title": "Frogs",
                 "content": "glass frogs"},
            ]}).encode()
            self._send(200, body, "application/json")
        elif path == "/":
            self._send(200, b"<p>ok</p>", "text/html")
        else:
            self._send(404, b"nope", "text/plain")

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send(404, b"nope", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        calls = pick_response(json.loads(self.rfile.read(length)))
        tool_calls = [
            {"id": f"c{i}", "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)
        ]
        body = json.dumps({"choices": [{"message": {
            "content": None, "tool_calls": tool_calls}}]}).encode()
        self._send(200, body, "application/json")


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    Handler.base = base
    build_script(base)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    OUT.mkdir(parents=True, exist_ok=True)
    cfg = OUT / "config.yaml"
    cfg.write_text(
        f"llm:\n  mode: api\n  base_url: {base}/v1\n  model: scripted\n"
        f"search:\n  backend: metasearch\n  metasearch_url: {base}\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    report = OUT / "report.md"
    proc = subprocess.run(
        [sys.executable, str(REPO / "main.py"), QUESTION, "--mode", "speed",
         "--config", str(cfg), "--out", str(report),
         "--runlog", str(OUT / "runlog.jsonl")],
        cwd=REPO, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=300,
    )
    server.shutdown()
    print(proc.stderr[-1500:])
    if proc.returncode != 0:
        print(f"SMOKE FAIL: exit {proc.returncode}")
        return 1
    if not report.exists():
        print("SMOKE FAIL: report.md was not written")
        return 1
    text = report.read_text(encoding="utf-8")
    for needle in ("## Executive summary", "## Findings", "## Source ledger",
                   "## Verification log", "48 million"):
        if needle not in text:
            print(f"SMOKE FAIL: report missing {needle!r}")
            return 1
    print(f"SMOKE PASS: {report} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
