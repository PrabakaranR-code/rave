"""Phase E: ask_user renderer and the --confirm Proceed/Edit plan gate."""
from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from agents.orchestrator import Orchestrator
from llm.schemas import AskUserArgs
from tests.mocks import ScriptedLLM, fake_site_transport
from tests.test_happy_path import make_config, scripted_speed_run
from ui.prompt import QuestionSpec, render_question


def quiet_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


def scripted_inputs(*answers):
    it = iter(answers)

    def input_fn(prompt: str) -> str:
        return next(it)

    return input_fn


SPEC = QuestionSpec(
    question="Which market?",
    options=["Domestic", "International"],
    allow_free_text=True,
)


def test_number_picks_option():
    assert render_question(SPEC, quiet_console(), scripted_inputs("2")) == "International"


def test_free_text_escape_hatch():
    out = render_question(SPEC, quiet_console(), scripted_inputs("both, compared"))
    assert out == "both, compared"


def test_out_of_range_number_reasks():
    out = render_question(SPEC, quiet_console(), scripted_inputs("9", "1"))
    assert out == "Domestic"


def test_free_text_rejected_when_not_allowed():
    spec = QuestionSpec(question="Proceed?", options=["Proceed", "Edit"],
                        allow_free_text=False)
    out = render_question(spec, quiet_console(), scripted_inputs("maybe", "1"))
    assert out == "Proceed"


def test_eof_defaults_to_first_option():
    def eof(prompt):
        raise EOFError

    assert render_question(SPEC, quiet_console(), eof) == "Domestic"


def test_no_options_pure_free_text():
    spec = QuestionSpec(question="Describe changes:", options=[])
    out = render_question(spec, quiet_console(), scripted_inputs("add costs"))
    assert out == "add costs"


def test_chip_ready_payload():
    payload = SPEC.to_payload()
    assert payload == {
        "type": "question",
        "question": "Which market?",
        "choices": [{"id": 1, "label": "Domestic"}, {"id": 2, "label": "International"}],
        "free_text": True,
    }


# --- --confirm gate ---------------------------------------------------------

def second_plan():
    return {
        "standalone_query": "What did the city council approve in the new transit budget?",
        "sub_questions": [
            {"id": "sq1", "question": "What is the total size of the approved budget?",
             "done_criteria": "a figure with a date and source"},
            {"id": "sq2", "question": "What does the budget fund?",
             "done_criteria": "specific funded items"},
            {"id": "sq3", "question": "What criticisms were raised?",
             "done_criteria": "at least one sourced objection"},
        ],
    }


def test_confirm_gate_edit_then_proceed(tmp_path):
    llm = scripted_speed_run()
    llm.enqueue("create_research_plan", ("create_research_plan", second_plan()))

    answers = ["Edit", "also cover the audit timeline", "Proceed"]
    asked: list[AskUserArgs] = []

    def fake_user(args: AskUserArgs) -> str:
        asked.append(args)
        return answers[len(asked) - 1]

    orch = Orchestrator(
        make_config(), mode="speed", confirm=True,
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        ask_user_fn=fake_user,
        llm_transport=llm.transport(), web_transport=fake_site_transport(),
    )
    result = orch.run("transit budget?")

    # gate asked: plan review (Edit) → feedback prompt → plan review (Proceed)
    assert len(asked) == 3
    assert asked[0].options == ["Proceed", "Edit"]
    assert "Research plan for:" in asked[0].question
    assert asked[1].allow_free_text and asked[1].options == []

    # the regenerated plan request carried the user's feedback verbatim
    plan_requests = [r for r in llm.requests
                     if [t["function"]["name"] for t in r["tools"]] == ["create_research_plan"]]
    assert len(plan_requests) == 2
    assert any("also cover the audit timeline" in m["content"]
               for m in plan_requests[1]["messages"])

    # gate calls are code-fired and budget-free; only the regenerated plan
    # (one extra forced create_research_plan) costs budget: 19 + 1
    assert result.tool_calls_used == 20
    entries = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    gate_calls = [e for e in entries if e["event"] == "tool_call"
                  and e["tool"] == "ask_user"]
    assert len(gate_calls) == 3 and all(e["auto"] for e in gate_calls)


def test_confirm_gate_immediate_proceed_keeps_first_plan(tmp_path):
    llm = scripted_speed_run()
    orch = Orchestrator(
        make_config(), mode="speed", confirm=True,
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        ask_user_fn=lambda args: "Proceed",
        llm_transport=llm.transport(), web_transport=fake_site_transport(),
    )
    result = orch.run("transit budget?")
    assert [sq.id for sq in result.plan.sub_questions] == ["sq1", "sq2", "sq3"]


def test_clarification_flows_into_plan(tmp_path):
    llm = scripted_speed_run()
    llm.queues["classify_query"].clear()
    llm.enqueue("classify_query", ("classify_query", {
        "standalone_question": "What did the city council approve in the new transit budget?",
        "topic_kind": "fast_moving",
        "needs_clarification": True,
        "clarifying_question": {
            "question": "Which council?",
            "options": ["Riverton", "Lakewood"],
        },
    }))
    seen = []

    def fake_user(args: AskUserArgs) -> str:
        seen.append(args)
        return "Riverton"

    orch = Orchestrator(
        make_config(), mode="speed",
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        ask_user_fn=fake_user,
        llm_transport=llm.transport(), web_transport=fake_site_transport(),
    )
    orch.run("transit budget?")
    assert seen and seen[0].question == "Which council?"
    plan_requests = [r for r in llm.requests
                     if [t["function"]["name"] for t in r["tools"]] == ["create_research_plan"]]
    assert any("Riverton" in m["content"] for m in plan_requests[0]["messages"])
