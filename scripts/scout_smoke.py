#!/usr/bin/env python3
"""End-to-end mocked smoke run through the SCOUT fixture backend.

Fully in-process and network-free: a scripted LLM transport drives the agents,
the SCOUT backend runs in offline (fixture) mode, and a mock HTTP transport
serves a fixture article for the example.org URLs SCOUT's fixtures return. A
real speed-mode Orchestrator run produces report.md and runlog.jsonl.

Usage:  python scripts/scout_smoke.py [out_dir]
Exit 0 and prints "SCOUT SMOKE PASS" when both artifacts are produced and the
report carries the expected sections; nonzero otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agents.orchestrator import Orchestrator  # noqa: E402
from config import AppConfig, ScoutConfig, SearchConfig  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
CLAIM = "The council approved a transit budget of 48 million dollars"


# --- scripted LLM (OpenAI-style tool calls) --------------------------------

class ScriptedLLM:
    def __init__(self):
        self.queues: dict[str, deque] = defaultdict(deque)

    def enqueue(self, key: str, *calls):
        self.queues[key].append(list(calls))

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        names = sorted(t["function"]["name"] for t in payload["tools"])
        key = names[0] if len(names) == 1 else "action"
        if key == "action":
            convo = "\n".join(m["content"] for m in payload["messages"]
                              if isinstance(m.get("content"), str))
            m = re.search(r"YOUR sub-question \((\w+)\)", convo)
            if m and self.queues.get(f"action:{m.group(1)}"):
                key = f"action:{m.group(1)}"
        calls = self.queues[key].popleft()
        tool_calls = [
            {"id": f"c{i}", "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)
        ]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": None, "tool_calls": tool_calls}}]})

    def transport(self):
        return httpx.MockTransport(self.handler)


def build_script(article_url: str) -> ScriptedLLM:
    llm = ScriptedLLM()
    q = "What did the city council approve in the new transit budget?"
    llm.enqueue("classify_query", ("classify_query", {
        "standalone_question": q, "topic_kind": "fast_moving",
        "needs_clarification": False,
    }))
    llm.enqueue("create_research_plan", ("create_research_plan", {
        "standalone_query": q,
        "sub_questions": [
            {"id": f"sq{i}", "question": text, "done_criteria": "a sourced fact"}
            for i, text in enumerate(
                ["What total was approved?", "What does it fund?",
                 "What objections were raised?"], 1)
        ],
    }))
    for _ in range(6):
        llm.enqueue("plan_preamble", ("plan_preamble", {
            "current_goal": "gather evidence", "next_action": "search",
            "expected_info": "sourced facts"}))
    facts = {
        "sq1": (CLAIM, "approve a transit budget of 48 million dollars"),
        "sq2": ("The plan funds forty new electric buses and a light-rail extension",
                "funds forty new electric buses"),
        "sq3": ("Opponents argued the plan relies on optimistic ridership projections",
                "the plan relies on optimistic ridership projections"),
    }
    for sq, (claim, quote) in facts.items():
        llm.enqueue(f"action:{sq}",
                    ("web_search", {"query": "city transit budget", "top_k": 3}))
        llm.enqueue(f"action:{sq}",
                    ("record_findings", {"findings": [{
                        "claim": claim, "url": article_url, "date": "2026-05-14",
                        "source_type": "news", "confidence": "M", "quote": quote}]}),
                    ("done", {"coverage_summary": f"{sq} covered", "gaps": []}))
    llm.enqueue("critique_findings", ("critique_findings", {"issues": []}))
    llm.enqueue("write_report", ("write_report", {
        "title": "City transit budget: what was approved",
        "exec_summary": ["The council approved a 48 million dollar transit budget."],
        "findings": [{"claim": c, "citations": [article_url], "confidence": "M",
                      "date": "2026-05-14"} for c, _ in facts.values()],
        "verification_log": {"cycles_run": 0, "changes": [], "unverified": []},
    }))
    return llm


# --- mock website: answer the example.org URLs SCOUT fixtures return -------

def web_transport() -> httpx.MockTransport:
    article = (FIXTURES / "article.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path == "/" or not path.strip("/"):
            return httpx.Response(200, html="<p>ok</p>")
        # every fixture article slug resolves to the transit-budget page
        return httpx.Response(200, html=article)

    return httpx.MockTransport(handler)


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else REPO / ".rave" / "scout_smoke"
    out.mkdir(parents=True, exist_ok=True)
    # SCOUT fixtures return https://example.org/<slug>/<n>; the article there
    # is the URL the scripted researcher cites.
    article_url = "https://example.org/city-transit-budget/1"
    llm = build_script(article_url)

    cfg = AppConfig(search=SearchConfig(
        backend="scout",
        scout=ScoutConfig(offline=True, trusted_outlets=["example.org"]),
    ))
    orch = Orchestrator(
        cfg, mode="speed",
        out_path=out / "report.md", runlog_path=out / "runlog.jsonl",
        llm_transport=llm.transport(), web_transport=web_transport(),
    )
    result = orch.run("what happened with the transit budget vote?")

    report = out / "report.md"
    runlog = out / "runlog.jsonl"
    if not report.exists():
        print("SCOUT SMOKE FAIL: report.md not written")
        return 1
    if not runlog.exists():
        print("SCOUT SMOKE FAIL: runlog.jsonl not written")
        return 1
    text = report.read_text(encoding="utf-8")
    for needle in ("## Executive summary", "## Findings", "## Source ledger",
                   "## Verification log", "48 million"):
        if needle not in text:
            print(f"SCOUT SMOKE FAIL: report missing {needle!r}")
            return 1
    # the scout backend must actually have been exercised
    entries = [json.loads(l) for l in runlog.read_text().splitlines()]
    searches = [e for e in entries if e.get("event") == "tool_call"
                and e.get("tool") == "web_search"]
    if not searches:
        print("SCOUT SMOKE FAIL: no web_search tool calls recorded")
        return 1
    print(f"SCOUT SMOKE PASS: {report} ({len(text)} chars), "
          f"{result.tool_calls_used} tool calls, backend=scout(offline)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
