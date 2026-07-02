"""Forced-tool mechanics: tool_choice payloads, validation, retries, failure."""
from __future__ import annotations

import json

import httpx
import pytest

from config import LLMConfig, ModelRouting
from llm.client import ForcedToolError, LLMClient, LLMError, ToolDecl
from llm.schemas import DoneArgs, PlanPreambleArgs, WebSearchArgs

PREAMBLE = ToolDecl("plan_preamble", "state your plan", PlanPreambleArgs)
SEARCH = ToolDecl("web_search", "search the web", WebSearchArgs)
DONE = ToolDecl("done", "finish research", DoneArgs)

VALID_PREAMBLE = {
    "current_goal": "find pricing",
    "next_action": "search",
    "expected_info": "a price",
}


def openai_tool_response(name: str, args: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ]
    }


def make_client(responses: list[dict], requests_out: list[dict], provider="openai") -> LLMClient:
    """LLMClient over a mock transport that replays canned JSON responses."""
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests_out.append(json.loads(request.content))
        return httpx.Response(200, json=next(it))

    cfg = LLMConfig(
        provider=provider,
        base_url="http://mock" + ("/v1" if provider == "openai" else ""),
        models=ModelRouting(),
    )
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


def test_forced_tool_happy_path_openai():
    reqs: list[dict] = []
    client = make_client([openai_tool_response("plan_preamble", VALID_PREAMBLE)], reqs)
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert isinstance(out, PlanPreambleArgs)
    assert out.current_goal == "find pricing"
    # OpenAI forcing = "required" + the single tool offered
    assert reqs[0]["tool_choice"] == "required"
    assert [t["function"]["name"] for t in reqs[0]["tools"]] == ["plan_preamble"]


def test_invalid_args_retried_with_error_appended():
    reqs: list[dict] = []
    client = make_client(
        [
            openai_tool_response("plan_preamble", {"current_goal": "x"}),  # missing fields
            openai_tool_response("plan_preamble", VALID_PREAMBLE),
        ],
        reqs,
    )
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert out.next_action == "search"
    assert len(reqs) == 2
    # the retry conversation must carry the validation error back to the model
    retry_msgs = reqs[1]["messages"]
    assert any("VALIDATION ERROR" in m["content"] for m in retry_msgs)


def test_persistent_invalid_args_fail_loudly_after_two_retries():
    reqs: list[dict] = []
    bad = openai_tool_response("plan_preamble", {"nope": 1})
    client = make_client([bad, bad, bad], reqs)
    with pytest.raises(ForcedToolError):
        client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert len(reqs) == 3  # initial + exactly 2 retries


def test_direct_answer_without_tool_call_is_rejected():
    reqs: list[dict] = []
    direct = {"choices": [{"message": {"content": "The answer is 42.", "tool_calls": []}}]}
    client = make_client(
        [direct, openai_tool_response("plan_preamble", VALID_PREAMBLE)], reqs
    )
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert isinstance(out, PlanPreambleArgs)
    assert any("Direct answers are rejected" in m["content"] for m in reqs[1]["messages"])


def test_wrong_tool_name_is_rejected():
    reqs: list[dict] = []
    client = make_client(
        [
            openai_tool_response("web_search", {"query": "hi"}),
            openai_tool_response("plan_preamble", VALID_PREAMBLE),
        ],
        reqs,
    )
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert isinstance(out, PlanPreambleArgs)


def test_choose_tool_allows_any_of_the_offered_tools():
    reqs: list[dict] = []
    client = make_client([openai_tool_response("done", {"coverage_summary": "ok"})], reqs)
    calls = client.choose_tool(
        "researcher", [SEARCH, DONE], [{"role": "user", "content": "go"}]
    )
    assert calls[0].name == "done"
    assert isinstance(calls[0].arguments, DoneArgs)
    assert {t["function"]["name"] for t in reqs[0]["tools"]} == {"web_search", "done"}


def test_anthropic_forcing_and_parsing():
    reqs: list[dict] = []
    resp = {
        "content": [
            {"type": "text", "text": "calling now"},
            {"type": "tool_use", "id": "tu_1", "name": "plan_preamble", "input": VALID_PREAMBLE},
        ]
    }
    client = make_client([resp], reqs, provider="anthropic")
    out = client.forced_tool(
        "planner",
        PREAMBLE,
        [{"role": "system", "content": "you are a planner"}, {"role": "user", "content": "go"}],
    )
    assert isinstance(out, PlanPreambleArgs)
    # Anthropic forcing = {"type": "tool", "name": ...}; system message lifted out
    assert reqs[0]["tool_choice"] == {"type": "tool", "name": "plan_preamble"}
    assert reqs[0]["system"] == "you are a planner"
    assert all(m["role"] != "system" for m in reqs[0]["messages"])
    assert reqs[0]["tools"][0]["input_schema"]["type"] == "object"


def test_http_error_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    cfg = LLMConfig(base_url="http://mock/v1")
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
