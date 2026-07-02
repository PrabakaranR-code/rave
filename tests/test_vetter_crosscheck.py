"""Source vetting heuristics and deterministic cross-check rules."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from llm.schemas import Confidence, Finding, SourceType, TopicKind
from tools.crosscheck import SubPassFindings, crosscheck
from tools.source_vetter import classify_source, seo_farm_score, vet

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 7, 1)


def f(claim, url, conf=Confidence.low, quote="q"):
    return Finding(claim=claim, url=url, date="2026-06-01", confidence=conf, quote=quote)


# --- vetter ---------------------------------------------------------------

def test_classify_source_heuristics():
    assert classify_source("https://transport.gov/budget") == SourceType.official
    assert classify_source("https://phys.example.edu/study") == SourceType.peer_reviewed
    assert classify_source("https://docs.vendor.com/api/limits") == SourceType.primary
    assert classify_source("https://blog.someone.net/thoughts") == SourceType.blog
    assert classify_source("https://forum.trains.net/thread/123") == SourceType.forum
    assert classify_source("https://daily.example.com/news/2026/vote") == SourceType.news


def test_stale_flag_only_for_fast_topics():
    old = "2025-09-01"  # 10 months before TODAY
    fast = vet("https://x.com/a", date_str=old, topic_kind=TopicKind.fast_moving,
               stale_months=6, today=TODAY)
    slow = vet("https://x.com/a", date_str=old, topic_kind=TopicKind.slow_moving,
               stale_months=6, today=TODAY)
    assert fast.stale and not slow.stale
    assert fast.weight < slow.weight


def test_undated_pages_downranked():
    dated = vet("https://x.com/a", date_str="2026-06-20", today=TODAY)
    undated = vet("https://x.com/a", date_str="", today=TODAY)
    assert undated.weight < dated.weight


def test_seo_farm_detected_and_dropped():
    spam = (FIXTURES / "spam.html").read_text(encoding="utf-8")
    from tools.extractor import extract

    ex = extract(spam)
    assert seo_farm_score(ex.title, ex.text) > 0.4
    clean = (FIXTURES / "article.html").read_text(encoding="utf-8")
    exc = extract(clean)
    assert seo_farm_score(exc.title, exc.text) < 0.2
    spam_verdict = vet("https://widgets.biz/best", title=ex.title, text=ex.text,
                       date_str="", today=TODAY)
    clean_verdict = vet("https://daily.example.com/news/2026/vote", title=exc.title,
                        text=exc.text, date_str="2026-05-14", today=TODAY)
    assert spam_verdict.weight < clean_verdict.weight


# --- crosscheck -----------------------------------------------------------

def test_confidence_upgraded_when_two_subpasses_agree():
    shared = "The transit budget is 48 million dollars for next year"
    res = crosscheck([
        SubPassFindings("sq1", [f(shared, "https://a.com/1")]),
        SubPassFindings("sq2", [f(shared, "https://b.org/2")]),
    ])
    g = res.groups[0]
    assert g.upgraded and g.corroborated
    assert all(x.confidence == Confidence.medium for x in g.findings)


def test_single_subpass_not_upgraded():
    res = crosscheck([
        SubPassFindings("sq1", [f("Only one pass saw this claim", "https://a.com/1")]),
    ])
    assert not res.groups[0].upgraded
    assert res.findings[0].confidence == Confidence.low


def test_dedupe_by_url():
    claim = "Forty new electric buses are funded by the plan"
    res = crosscheck([
        SubPassFindings("sq1", [f(claim, "https://a.com/x"), f(claim, "https://a.com/x/")]),
    ])
    assert len(res.findings) == 1


def test_numeric_contradiction_flagged_not_smoothed():
    res = crosscheck([
        SubPassFindings("sq1", [f("The transit budget totals 48 million dollars", "https://a.com/1")]),
        SubPassFindings("sq2", [f("The transit budget totals 52 million dollars", "https://b.com/2")]),
    ])
    assert res.contradictions
    assert res.contradictions[0].reason == "numeric mismatch"
    # both positions preserved
    assert len(res.groups[0].findings) == 2


def test_polarity_contradiction_flagged():
    res = crosscheck([
        SubPassFindings("sq1", [f("The northern line extension was approved by the council", "https://a.com/1")]),
        SubPassFindings("sq2", [f("The northern line extension was not approved by the council", "https://b.com/2")]),
    ])
    assert any(c.reason == "polarity mismatch" for c in res.contradictions)


def test_thin_subpass_flagged():
    res = crosscheck([
        SubPassFindings("sq1", [f("claim one about buses", "https://a.com/1"),
                                f("claim two about rail lines", "https://a.com/2")]),
        SubPassFindings("sq2", [f("lonely claim about audits", "https://b.com/1")]),
        SubPassFindings("sq3", []),
    ])
    assert res.thin_subpasses == ["sq2", "sq3"]


def test_fact_source_map_and_lookup():
    shared = "The audit is scheduled for early next year"
    res = crosscheck([
        SubPassFindings("sq1", [f(shared, "https://a.com/1")]),
        SubPassFindings("sq2", [f(shared, "https://b.com/2")]),
    ])
    urls = res.fact_source_map[shared]
    assert urls == ["https://a.com/1", "https://b.com/2"]
    assert res.sources_for("An audit is scheduled early next year") == urls
    assert res.sources_for("completely unrelated topic") == []
