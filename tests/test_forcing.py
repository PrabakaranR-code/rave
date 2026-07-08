"""Forced-tool mechanics: tool_choice payloads, validation, retries, failure."""
from __future__ import annotations

import json

import httpx
import pytest

from config import LLMConfig
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


def make_client(responses: list[dict], requests_out: list[dict], protocol="openai") -> LLMClient:
    """LLMClient over a mock transport that replays canned JSON responses.

    The wire protocol is chosen by base_url shape: an api.anthropic.com URL
    speaks Anthropic, anything else OpenAI-compatible.
    """
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests_out.append(json.loads(request.content))
        return httpx.Response(200, json=next(it))

    base = "https://api.anthropic.com" if protocol == "anthropic" else "http://mock/v1"
    cfg = LLMConfig(mode="api", base_url=base, model="test-model")
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
    client = make_client([resp], reqs, protocol="anthropic")
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


def raw_args_response(name: str, raw_arguments: str) -> dict:
    """A tool call whose arguments string is NOT valid JSON."""
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": raw_arguments},
                        }
                    ],
                }
            }
        ]
    }


def test_malformed_json_arguments_feed_the_retry_loop():
    reqs: list[dict] = []
    client = make_client(
        [
            raw_args_response("plan_preamble", '{"current_goal": "x", oops'),
            raw_args_response("plan_preamble", "not json at all"),
            openai_tool_response("plan_preamble", VALID_PREAMBLE),
        ],
        reqs,
    )
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert isinstance(out, PlanPreambleArgs)
    assert len(reqs) == 3  # two malformed rounds retried, then success
    assert any("VALIDATION ERROR" in m["content"] for m in reqs[1]["messages"])
    assert any("VALIDATION ERROR" in m["content"] for m in reqs[2]["messages"])


def test_persistently_malformed_json_fails_loudly_not_crash():
    bad = raw_args_response("plan_preamble", "{{{")
    client = make_client([bad, bad, bad], [])
    with pytest.raises(ForcedToolError):
        client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])


def text_only_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": []}}]}


def test_fallback_extracts_named_tool_call_from_prose():
    reqs: list[dict] = []
    content = (
        "Sure — calling the tool now:\n"
        + json.dumps({"name": "plan_preamble", "arguments": VALID_PREAMBLE})
    )
    client = make_client([text_only_response(content)], reqs)
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert isinstance(out, PlanPreambleArgs)
    assert len(reqs) == 1  # salvaged without another round trip


def test_fallback_accepts_bare_args_in_fenced_json_for_forced_tool():
    content = "```json\n" + json.dumps(VALID_PREAMBLE) + "\n```"
    client = make_client([text_only_response(content)], [])
    out = client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert out.current_goal == VALID_PREAMBLE["current_goal"]


def test_fallback_works_for_choose_tool_with_named_call():
    content = 'Using search: {"tool": "web_search", "parameters": {"query": "q1"}}'
    client = make_client([text_only_response(content)], [])
    calls = client.choose_tool(
        "researcher", [SEARCH, DONE], [{"role": "user", "content": "go"}]
    )
    assert calls[0].name == "web_search"
    assert calls[0].arguments.query == "q1"


def test_fallback_ignores_irrelevant_json_and_still_retries():
    # a named call to a tool that is not offered must not be salvaged
    content = '{"name": "rm_rf", "arguments": {"path": "/"}}'
    client = make_client(
        [text_only_response(content),
         openai_tool_response("done", {"coverage_summary": "ok"})],
        [],
    )
    calls = client.choose_tool(
        "researcher", [SEARCH, DONE], [{"role": "user", "content": "go"}]
    )
    assert calls[0].name == "done"


def test_anthropic_base_url_with_v1_suffix_still_posts_to_v1_messages():
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "id": "t", "name": "plan_preamble", "input": VALID_PREAMBLE}
        ]})

    cfg = LLMConfig(mode="api", base_url="https://api.anthropic.com/v1", model="m")
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
    assert urls == ["https://api.anthropic.com/v1/messages"]


def test_http_error_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    cfg = LLMConfig(base_url="http://mock/v1")
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        client.forced_tool("planner", PREAMBLE, [{"role": "user", "content": "go"}])
