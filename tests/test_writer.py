"""Writer post-checks: no source → no claim, ledger rebuild, rendering."""
from __future__ import annotations

from agents.writer import Writer, render_markdown
from llm.schemas import (
    ComparisonTable,
    Confidence,
    Disagreement,
    Finding,
    ReportFinding,
    VerificationLog,
    WriteReportArgs,
)
from tools.crosscheck import SubPassFindings, crosscheck


def checked_fixture():
    f = Finding(claim="The budget totals 48 million dollars",
                url="https://a.test/article", date="2026-05-14",
                confidence=Confidence.medium, quote="48 million dollars")
    return crosscheck([SubPassFindings("sq1", [f])])


def report_args(findings):
    return WriteReportArgs(
        title="T", exec_summary=["s"], findings=findings,
        verification_log=VerificationLog(),
    )


def test_uncited_claim_is_deleted_and_moved_to_gaps():
    checked = checked_fixture()
    args = report_args([
        ReportFinding(claim="The budget totals 48 million dollars",
                      citations=["https://a.test/article"]),
        ReportFinding(claim="A claim the model made up from memory", citations=[]),
    ])
    out = Writer._enforce_citations(args, checked)
    assert len(out.findings) == 1
    assert out.gaps == ["[no source found] A claim the model made up from memory"]


def test_unknown_citation_urls_are_stripped_and_recovered_from_fact_map():
    checked = checked_fixture()
    args = report_args([
        ReportFinding(claim="The budget totals 48 million dollars",
                      citations=["https://invented.example/nope"]),
    ])
    out = Writer._enforce_citations(args, checked)
    # invented URL dropped; the fact→source map supplies the real citation
    assert out.findings[0].citations == ["https://a.test/article"]


def test_source_ledger_rebuilt_from_used_citations():
    checked = checked_fixture()
    args = report_args([
        ReportFinding(claim="The budget totals 48 million dollars",
                      citations=["https://a.test/article"]),
    ])
    out = Writer._enforce_citations(args, checked)
    assert [s.url for s in out.source_ledger] == ["https://a.test/article"]
    assert out.source_ledger[0].date == "2026-05-14"


def test_render_markdown_all_sections():
    args = WriteReportArgs(
        title="Sample",
        exec_summary=["one", "two"],
        findings=[ReportFinding(claim="c1", citations=["https://a.test/1", "https://b.test/2"],
                                confidence=Confidence.high, date="2026-01-01")],
        comparison_table=ComparisonTable(headers=["thing", "value"], rows=[["a", "1"]]),
        disagreements=[Disagreement(claim="x", position_a="p1", position_b="p2",
                                    likely_reason="different dates")],
        gaps=["[no source found] y"],
        recommendations=["r"],
        verification_log=VerificationLog(cycles_run=2, changes=["upgraded c1"],
                                         unverified=["z"]),
    )
    md = render_markdown(args)
    assert "# Sample" in md
    assert "*(corroborated)*" in md          # two citations
    assert "| thing | value |" in md
    assert "Position A: p1" in md
    assert "[no source found] y" in md
    assert "[unverified] z" in md
    assert "Verify cycles run: 2" in md
