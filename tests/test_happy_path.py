"""Phase C: end-to-end happy path — Planner → Researchers → Writer, speed mode.

Fully offline: scripted LLM transport + fake website transport.
"""
from __future__ import annotations

import json

import pytest

from agents.orchestrator import Orchestrator
from config import AppConfig, SearchConfig
from tests.mocks import ScriptedLLM, fake_site_transport

ARTICLE = "https://site.test/article"


def make_config() -> AppConfig:
    return AppConfig(search=SearchConfig(backend="metasearch",
                                         metasearch_url="https://site.test"))


def scripted_speed_run() -> ScriptedLLM:
    llm = ScriptedLLM()
    llm.enqueue("classify_query", ("classify_query", {
        "standalone_question": "What did the city council approve in the new transit budget?",
        "topic_kind": "fast_moving",
        "needs_clarification": False,
    }))
    llm.enqueue("create_research_plan", ("create_research_plan", {
        "standalone_query": "What did the city council approve in the new transit budget?",
        "sub_questions": [
            {"id": "sq1", "question": "What is the total size of the approved budget?",
             "done_criteria": "a figure with a date and source"},
            {"id": "sq2", "question": "What does the budget fund?",
             "done_criteria": "specific funded items"},
            {"id": "sq3", "question": "What criticisms were raised?",
             "done_criteria": "at least one sourced objection"},
        ],
    }))
    claims = {
        "sq1": ("The council approved a transit budget of 48 million dollars",
                "approve a transit budget of 48 million dollars"),
        "sq2": ("The plan funds forty new electric buses and a light-rail extension",
                "funds forty new electric buses"),
        "sq3": ("Opponents argued the plan relies on optimistic ridership projections",
                "the plan relies on optimistic ridership projections"),
    }
    for sq_id, (claim, quote) in claims.items():
        llm.enqueue("plan_preamble", ("plan_preamble", {
            "current_goal": f"answer {sq_id}",
            "next_action": "search the web",
            "expected_info": "sourced facts",
        }))
        llm.enqueue("action", ("web_search", {"query": "city transit budget vote", "top_k": 2}))
        llm.enqueue("plan_preamble", ("plan_preamble", {
            "current_goal": f"bank evidence for {sq_id}",
            "next_action": "record findings and finish",
            "expected_info": "confirmation",
        }))
        llm.enqueue(
            "action",
            ("record_findings", {"findings": [{
                "claim": claim, "url": ARTICLE, "date": "2026-05-14",
                "source_type": "news", "confidence": "M", "quote": quote,
            }]}),
            ("done", {"coverage_summary": f"{sq_id} answered from the article", "gaps": []}),
        )
    llm.enqueue("write_report", ("write_report", {
        "title": "City transit budget: what was approved",
        "exec_summary": ["The council approved a 48 million dollar transit budget.",
                         "It funds forty electric buses and a light-rail extension.",
                         "Critics question the ridership projections."],
        "findings": [
            {"claim": c, "citations": [ARTICLE], "confidence": "M", "date": "2026-05-14"}
            for c, _q in claims.values()
        ],
        "disagreements": [],
        "gaps": [],
        "recommendations": ["Re-check after the independent audit early next year."],
        "source_ledger": [{"url": ARTICLE, "title": "Budget vote", "source_type": "news",
                           "date": "2026-05-14"}],
        "verification_log": {"cycles_run": 0, "changes": [], "unverified": []},
    }))
    return llm


@pytest.fixture()
def run_result(tmp_path):
    llm = scripted_speed_run()
    orch = Orchestrator(
        make_config(),
        mode="speed",
        out_path=tmp_path / "report.md",
        runlog_path=tmp_path / "runlog.jsonl",
        llm_transport=llm.transport(),
        web_transport=fake_site_transport(),
    )
    result = orch.run("what happened with the transit budget vote?")
    return result, tmp_path, llm


def test_run_completes_and_writes_report(run_result):
    result, tmp_path, _ = run_result
    assert result.report.title.startswith("City transit budget")
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert md == result.markdown
    for section in ("## Executive summary", "## Findings", "## Source ledger",
                    "## Verification log"):
        assert section in md
    assert ARTICLE in md


def test_every_reported_claim_has_a_citation(run_result):
    result, _, _ = run_result
    assert result.report.findings
    for f in result.report.findings:
        assert f.citations, f"finding without citation: {f.claim}"


def test_runlog_shows_every_phase_transition_as_forced_calls(run_result):
    result, tmp_path, _ = run_result
    entries = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    tool_events = [e for e in entries if e["event"] == "tool_call"]
    forced = [e["tool"] for e in tool_events if e["forced"]]
    assert forced.count("classify_query") == 1
    assert forced.count("create_research_plan") == 1
    assert forced.count("plan_preamble") == 6  # first call of every turn, 3 sq x 2 turns
    assert forced.count("done") == 3
    assert forced.count("write_report") == 1
    phases = {e["phase"] for e in entries if e["event"] == "phase"}
    assert {"P0", "P1", "P2", "P3", "P4", "P5"} <= phases


def test_budget_accounting_in_runlog(run_result):
    result, _, _ = run_result
    # classify + plan + 3x(2 preambles + search + record + done) + report = 18
    assert result.tool_calls_used == 18
    assert result.tool_calls_used <= 25  # speed ceiling


def test_findings_survived_crosscheck_with_sources(run_result):
    result, _, _ = run_result
    assert len(result.checked.groups) == 3
    assert all(result.checked.fact_source_map[g.rep_claim] == [ARTICLE]
               for g in result.checked.groups)
