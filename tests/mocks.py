"""Shared test doubles: a scripted LLM endpoint and a small fake website.

No network anywhere — both are httpx.MockTransport handlers.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


class ScriptedLLM:
    """Replays canned OpenAI-style tool-call responses.

    Responses are queued under a key: the tool's name for forced (single-tool)
    calls, or "action" for multi-tool researcher turns. Researcher swarms run
    in parallel, so action queues can also be scoped per sub-question with the
    key "action:<sq_id>" — the handler routes on the "YOUR sub-question (sqN)"
    marker in the conversation. One queued item may contain several tool calls
    (parallel calls in one assistant message).
    """

    def __init__(self):
        self.queues: dict[str, deque] = defaultdict(deque)
        self.requests: list[dict] = []

    def enqueue(self, key: str, *calls: tuple[str, dict]):
        self.queues[key].append(list(calls))

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        names = sorted(t["function"]["name"] for t in payload["tools"])
        key = names[0] if len(names) == 1 else "action"
        if key == "action":
            convo = "\n".join(
                m["content"] for m in payload["messages"]
                if isinstance(m.get("content"), str)
            )
            m = re.search(r"YOUR sub-question \((\w+)\)", convo)
            if m and self.queues.get(f"action:{m.group(1)}"):
                key = f"action:{m.group(1)}"
        if not self.queues[key]:
            raise AssertionError(f"no scripted LLM response left for key {key!r}")
        calls = self.queues[key].popleft()
        tool_calls = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls)
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None, "tool_calls": tool_calls}}]},
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def fake_site_transport() -> httpx.MockTransport:
    """Serves fixtures at https://site.test plus a metasearch /search endpoint."""
    article = (FIXTURES / "article.html").read_text(encoding="utf-8")
    plain = (FIXTURES / "plain.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path == "/article":
            return httpx.Response(200, html=article)
        if path == "/plain":
            return httpx.Response(200, html=plain)
        if path == "/search":
            return httpx.Response(200, json={"results": [
                {"url": "https://site.test/article", "title": "Budget vote",
                 "content": "council transit budget"},
                {"url": "https://site.test/plain", "title": "Frogs",
                 "content": "glass frogs"},
                {"url": "https://mirror.test/article", "title": "Budget vote (wire copy)",
                 "content": "council transit budget"},
            ]})
        if path == "/":  # connectivity probes
            return httpx.Response(200, html="<p>ok</p>")
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def offline_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is down", request=request)

    return httpx.MockTransport(handler)
