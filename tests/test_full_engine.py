"""Phase D: swarm parallelism, Critic→Verify loop, budget ceilings, HALT."""
from __future__ import annotations

import json
import threading

import httpx
import pytest

import main as main_mod
from agents.orchestrator import HALT_MESSAGE, HaltError, Orchestrator
from config import AppConfig, ModeConfig, SearchConfig
from llm.schemas import Confidence
from tests.mocks import ScriptedLLM, fake_site_transport, offline_transport
from tests.test_happy_path import make_config, scripted_speed_run

ARTICLE = "https://site.test/article"
MIRROR = "https://mirror.test/article"
CLAIM = "The council approved a transit budget of 48 million dollars"


# --- HALT behavior ---------------------------------------------------------

def test_offline_run_halts(tmp_path):
    orch = Orchestrator(
        make_config(), mode="speed",
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        llm_transport=ScriptedLLM().transport(),
        web_transport=offline_transport(),
    )
    with pytest.raises(HaltError) as ei:
        orch.run("anything")
    assert "HALT" in str(ei.value)
    entries = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    assert any(e["event"] == "halt" for e in entries)
    assert not (tmp_path / "report.md").exists()


def test_main_exits_nonzero_and_prints_halt(monkeypatch, capsys):
    class FakeOrch:
        def __init__(self, *a, **k):
            pass

        def run(self, q, context=""):
            raise HaltError(HALT_MESSAGE)

    monkeypatch.setattr(main_mod, "Orchestrator", FakeOrch)
    rc = main_mod.main(["any question"])
    assert rc == 2
    assert "HALT: no working web access" in capsys.readouterr().err


def test_main_exits_with_setup_hint_when_backend_broken(tmp_path, capsys):
    # An explicitly named backend whose requirement is missing exits 3 with the
    # setup hint. (A missing/empty config now defaults to the keyless SCOUT
    # backend, so "no backend" is no longer an error state.)
    cfg = tmp_path / "broken.yaml"
    cfg.write_text("search:\n  backend: metasearch\n  metasearch_url: \"\"\n",
                   encoding="utf-8")
    rc = main_mod.main([
        "q", "--config", str(cfg),
        "--out", str(tmp_path / "r.md"), "--runlog", str(tmp_path / "l.jsonl"),
    ])
    assert rc == 3
    assert "backend" in capsys.readouterr().err.lower()


# --- swarm parallelism -----------------------------------------------------

def test_researchers_run_in_parallel(tmp_path):
    llm = scripted_speed_run()
    barrier = threading.Barrier(3, timeout=15)
    lock = threading.Lock()
    seen = {"n": 0}
    inner = llm.handler

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        names = sorted(t["function"]["name"] for t in payload["tools"])
        if names == ["plan_preamble"]:
            with lock:
                idx = seen["n"]
                seen["n"] += 1
            if idx < 3:
                # the first three preamble calls come from three distinct
                # researcher threads; sequential execution would deadlock here
                barrier.wait()
        return inner(request)

    orch = Orchestrator(
        make_config(), mode="speed",
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        llm_transport=httpx.MockTransport(handler),
        web_transport=fake_site_transport(),
    )
    result = orch.run("transit budget?")
    assert len(result.outcomes) == 3
    # outcomes preserve plan order even with parallel execution
    assert [o.subpass.sub_question_id for o in result.outcomes] == ["sq1", "sq2", "sq3"]


# --- Critic → Verify loop --------------------------------------------------

def scripted_balanced_verify() -> ScriptedLLM:
    llm = ScriptedLLM()
    llm.enqueue("classify_query", ("classify_query", {
        "standalone_question": "What did the city council approve in the transit budget?",
        "topic_kind": "fast_moving",
        "needs_clarification": False,
    }))
    llm.enqueue("create_research_plan", ("create_research_plan", {
        "standalone_query": "What did the city council approve in the transit budget?",
        "sub_questions": [
            {"id": "sq1", "question": "Total budget figure?", "done_criteria": "a number"},
            {"id": "sq2", "question": "Funded items?", "done_criteria": "a list"},
            {"id": "sq3", "question": "Objections?", "done_criteria": "a source"},
        ],
    }))
    for _ in range(4):  # sq1 uses two turns; sq2/sq3 one turn each
        llm.enqueue("plan_preamble", ("plan_preamble", {
            "current_goal": "g", "next_action": "a", "expected_info": "i",
        }))
    llm.enqueue("action:sq1", ("web_search", {"query": "transit budget vote", "top_k": 2}))
    llm.enqueue("action:sq1",
                ("record_findings", {"findings": [{
                    "claim": CLAIM, "url": ARTICLE, "date": "2026-05-14",
                    "source_type": "news", "confidence": "M",
                    "quote": "approve a transit budget of 48 million dollars",
                }]}),
                ("done", {"coverage_summary": "figure found once", "gaps": []}))
    llm.enqueue("action:sq2", ("done", {
        "coverage_summary": "nothing solid found", "gaps": ["sq2: no evidence"]}))
    llm.enqueue("action:sq3", ("done", {
        "coverage_summary": "nothing solid found", "gaps": ["sq3: no evidence"]}))
    # cycle 1: one single-source issue → code verifies; cycle 2 audit: clean
    llm.enqueue("critique_findings", ("critique_findings", {"issues": [{
        "id": "i1", "type": "single_source", "claim_ref": CLAIM,
        "instruction": "search for an independent copy of the council transit budget approval figure",
    }]}))
    llm.enqueue("critique_findings", ("critique_findings", {"issues": []}))
    llm.enqueue("write_report", ("write_report", {
        "title": "Transit budget verification",
        "exec_summary": ["The council approved a 48 million dollar budget."],
        "findings": [{"claim": CLAIM, "citations": [ARTICLE, MIRROR],
                      "confidence": "H", "date": "2026-05-14"}],
        "verification_log": {"cycles_run": 0, "changes": [], "unverified": []},
    }))
    return llm


@pytest.fixture()
def verify_run(tmp_path):
    llm = scripted_balanced_verify()
    orch = Orchestrator(
        make_config(), mode="balanced",
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        llm_transport=llm.transport(), web_transport=fake_site_transport(),
    )
    return orch.run("transit budget?"), tmp_path


def test_verify_loop_finds_second_source_and_upgrades(verify_run):
    result, _ = verify_run
    assert result.verification.cycles_run == 1
    assert result.verification.unverified == []
    assert any("new source found" in c and "mirror.test" in c
               for c in result.verification.changes)
    assert any("still supports the claim" in c for c in result.verification.changes)
    group = next(g for g in result.checked.groups if "48 million" in g.rep_claim)
    assert group.corroborated and group.upgraded
    assert group.hosts == {"site.test", "mirror.test"}
    # original M finding upgraded one level by code, not by the LLM
    assert Confidence.high in {f.confidence for f in group.findings}


def test_verify_tool_calls_logged_with_verifier_role(verify_run):
    _, tmp_path = verify_run
    entries = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    verifier_calls = [e for e in entries
                      if e["event"] == "tool_call" and e["role"] == "verifier"]
    assert 1 <= len(verifier_calls) <= 2  # ≤2 tool calls per issue
    critiques = [e for e in entries
                 if e["event"] == "tool_call" and e["tool"] == "critique_findings"]
    assert len(critiques) == 2 and all(e["forced"] for e in critiques)


def test_speed_mode_critic_is_report_only(tmp_path):
    llm = scripted_speed_run()
    llm.queues["critique_findings"].clear()
    llm.enqueue("critique_findings", ("critique_findings", {"issues": [{
        "id": "i1", "type": "single_source",
        "claim_ref": "The council approved a transit budget of 48 million dollars",
        "instruction": "find a second independent source",
    }]}))
    orch = Orchestrator(
        make_config(), mode="speed",
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        llm_transport=llm.transport(), web_transport=fake_site_transport(),
    )
    result = orch.run("transit budget?")
    assert result.verification.cycles_run == 0
    assert result.verification.unverified == [
        "single_source: The council approved a transit budget of 48 million dollars"
    ]
    entries = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    assert not any(e.get("role") == "verifier" for e in entries)  # no re-search
    assert "[unverified]" in result.markdown


# --- budget ceilings -------------------------------------------------------

def test_tiny_ceiling_is_hard_stop_but_report_still_written(tmp_path):
    cfg = make_config()
    cfg.modes["speed"] = ModeConfig(researcher_iters=2, critic_cycles=0,
                                    tool_call_ceiling=3)
    llm = ScriptedLLM()
    llm.enqueue("classify_query", ("classify_query", {
        "standalone_question": "Q?", "topic_kind": "slow_moving",
        "needs_clarification": False,
    }))
    llm.enqueue("create_research_plan", ("create_research_plan", {
        "standalone_query": "Q?",
        "sub_questions": [
            {"id": f"sq{i}", "question": f"q{i}?", "done_criteria": "d"}
            for i in (1, 2, 3)
        ],
    }))
    for _ in range(3):
        llm.enqueue("plan_preamble", ("plan_preamble", {
            "current_goal": "g", "next_action": "a", "expected_info": "i",
        }))
    llm.enqueue("action", ("web_search", {"query": "q", "top_k": 2}))
    llm.enqueue("write_report", ("write_report", {
        "title": "Q", "exec_summary": ["no research completed within budget"],
        "findings": [],
        "verification_log": {"cycles_run": 0, "changes": [], "unverified": []},
    }))
    orch = Orchestrator(
        cfg, mode="speed",
        out_path=tmp_path / "report.md", runlog_path=tmp_path / "runlog.jsonl",
        llm_transport=llm.transport(), web_transport=fake_site_transport(),
    )
    result = orch.run("Q?")
    assert result.tool_calls_used == 3  # the ceiling held
    assert (tmp_path / "report.md").exists()
    assert any("ceiling" in c for c in result.verification.changes)
    entries = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    auto_dones = [e for e in entries if e["event"] == "tool_call"
                  and e["tool"] == "done" and e["auto"]]
    assert len(auto_dones) == 3  # every researcher exited via auto-fired done
    report_calls = [e for e in entries if e["event"] == "tool_call"
                    and e["tool"] == "write_report"]
    assert report_calls and report_calls[-1]["auto"] is True
