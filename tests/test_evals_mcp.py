"""Phase F: eval harness scoring/results.tsv and the tool-server stub."""
from __future__ import annotations

from pathlib import Path

import mcp_server
from evals.harness import (
    RESULTS_HEADER,
    EvalOutcome,
    fact_hit_rate,
    load_questions,
    run_evals,
    score_run,
)
from llm.schemas import (
    Confidence,
    ReportFinding,
    VerificationLog,
    WriteReportArgs,
)

QUESTIONS = Path(__file__).resolve().parent.parent / "evals" / "questions.yaml"


class FakeRunResult:
    def __init__(self, findings, unverified=(), markdown="report text"):
        self.report = WriteReportArgs(
            title="t", exec_summary=["s"], findings=findings,
        )
        self.verification = VerificationLog(unverified=list(unverified))
        self.markdown = markdown


def two_finding_result(markdown="42.195 fixed in 1921"):
    return FakeRunResult(
        findings=[
            ReportFinding(claim="a", citations=["https://x.test/1", "https://y.test/2"],
                          confidence=Confidence.high),
            ReportFinding(claim="b", citations=["https://x.test/1"]),
        ],
        markdown=markdown,
    )


def test_questions_yaml_loads_ten_questions():
    qs = load_questions(QUESTIONS)
    assert len(qs) == 10
    assert all(q.id and q.question and q.mode in ("speed", "balanced", "quality")
               for q in qs)


def test_fact_hit_rate_with_alternatives():
    assert fact_hit_rate(["42.195", "1921"], "distance 42.195 km since 1921") == 1.0
    assert fact_hit_rate(["100 OR 212"], "boils at 212 F") == 1.0
    assert fact_hit_rate(["100 OR 212"], "no numbers") == 0.0
    assert fact_hit_rate([], "anything") == 1.0


def test_score_run_metrics():
    qs = load_questions(QUESTIONS)
    q2 = next(q for q in qs if q.id == "q02")
    outcome = score_run(q2, two_finding_result())
    assert outcome.citation_coverage == 1.0
    assert outcome.corroboration == 0.5
    assert outcome.fact_hit_rate == 1.0
    assert outcome.unverified == 0
    assert outcome.score == 85.0  # 100*(0.5*1 + 0.3*0.5 + 0.2*1)


def test_unverified_items_cost_points():
    o = EvalOutcome("q", ok=True, citation_coverage=1.0, corroboration=1.0,
                    fact_hit_rate=1.0, unverified=3)
    assert o.score == 94.0


def test_run_evals_appends_keep_then_discard(tmp_path):
    results = tmp_path / "results.tsv"

    def good_runner(question, mode):
        return two_finding_result(markdown="42.195 1921 100 212 250 employees "
                                           "million shrink per hour docked percent "
                                           "828 tambora 1816 299792458")

    avg1, status1, outcomes = run_evals(
        QUESTIONS, results, description="baseline", runner=good_runner,
        log=lambda *a: None,
    )
    assert status1 == "keep" and len(outcomes) == 10

    def bad_runner(question, mode):
        raise RuntimeError("backend down")

    avg2, status2, _ = run_evals(
        QUESTIONS, results, description="broken change", runner=bad_runner,
        log=lambda *a: None,
    )
    assert avg2 == 0.0 and status2 == "discard"

    lines = results.read_text(encoding="utf-8").splitlines()
    assert lines[0] == RESULTS_HEADER
    assert len(lines) == 3
    assert lines[1].split("\t")[3] == "keep"
    assert lines[2].split("\t")[3] == "discard"
    assert lines[2].split("\t")[4] == "broken change"


# --- tool server stub -------------------------------------------------------

def test_stub_initialize_and_tools_list():
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["serverInfo"]["name"] == "rave"
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == ["deep_research"]
    assert tools[0]["inputSchema"]["required"] == ["query"]


def test_stub_tools_call_routes_to_runner():
    calls = {}

    def fake_runner(query, mode="balanced"):
        calls["query"], calls["mode"] = query, mode
        return "# fake report"

    resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "deep_research",
                    "arguments": {"query": "q?", "mode": "speed"}}},
        runner=fake_runner,
    )
    assert calls == {"query": "q?", "mode": "speed"}
    assert resp["result"]["content"][0]["text"] == "# fake report"


def test_stub_errors_and_notifications():
    assert mcp_server.handle_request({"jsonrpc": "2.0",
                                      "method": "notifications/initialized"}) is None
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert "error" in resp
    resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "other", "arguments": {}}})
    assert "error" in resp
    resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "deep_research", "arguments": {}}})
    assert "error" in resp


def test_stub_run_failure_reported_as_tool_error():
    def boom(query, mode="balanced"):
        raise RuntimeError("HALT: no working web access")

    resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "deep_research", "arguments": {"query": "q"}}},
        runner=boom,
    )
    assert resp["result"]["isError"] is True
    assert "HALT" in resp["result"]["content"][0]["text"]
