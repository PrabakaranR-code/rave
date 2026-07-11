"""SCOUT backend: translation via the fixture adapter (no live network),
health-warning surfacing, trusted-outlet boost, and config back-compat."""
from __future__ import annotations

import datetime as dt
import logging

import httpx
import pytest

from config import PipelineConfig, ScoutConfig, SearchConfig
from llm.schemas import TopicKind, WebSearchArgs
from tools.fetch_page import Fetcher
from tools.search_scout import ScoutBackend, build_engine
from tools.source_vetter import vet
from tools.web_search import SearchHit, WebSearchTool, make_backend

FIXTURES_URL = "https://example.org"


# --- translation via SCOUT's real offline fixture adapter (no network) -------

def test_offline_fixture_translation():
    be = ScoutBackend.from_config(
        ScoutConfig(offline=True, trusted_outlets=["example.org"])
    )
    hits = be.search("solar sail propulsion", top_k=3)
    assert hits, "offline fixture should return candidates"
    assert all(isinstance(h, SearchHit) for h in hits)
    for h in hits:
        assert h.url.startswith(FIXTURES_URL)
        assert h.source == "fixture"
        assert h.category == "general"
        assert h.trusted is True  # matched the trusted-outlet list


def test_top_k_slicing_offline():
    be = ScoutBackend.from_config(ScoutConfig(offline=True))
    assert len(be.search("q", top_k=1)) == 1
    hits = be.search("q", top_k=2)
    assert len(hits) <= 2


# --- full field mapping + health warnings via a hand-built response ----------

class _FakeEngine:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def search(self, query, limit=20, **kw):
        self.calls.append((query, limit))
        return self._response


def _scout_response(results, health):
    from scout.schema import SearchResponse
    return SearchResponse(query="q", results=results, health=health)


def test_full_field_mapping_and_source_ordering():
    from scout.schema import HealthStatus, SearchResult

    published = dt.datetime(2026, 5, 14, 9, 30)
    results = [
        SearchResult(title="Budget vote", url="https://a.test/1",
                     snippet="forty buses", source="mojeek",
                     category="news", published=published, trusted=True,
                     raw_rank=1),
        SearchResult(title="No URL", url="", source="x"),  # dropped: no url
        SearchResult(title="Frogs", url="https://b.test/2", snippet="",
                     source="wikipedia", category="knowledge"),
    ]
    engine = _FakeEngine(_scout_response(results, {"mojeek": HealthStatus.OK}))
    hits = ScoutBackend(engine).search("transit budget", top_k=5)
    assert [h.url for h in hits] == ["https://a.test/1", "https://b.test/2"]
    first = hits[0]
    assert first.title == "Budget vote"
    assert first.snippet == "forty buses"
    assert first.source == "mojeek" and first.category == "news"
    assert first.trusted is True
    assert first.published == published.isoformat()
    assert hits[1].trusted is False
    # asked SCOUT for extra candidates (top_k*2)
    assert engine.calls[0][1] == 10


def test_unhealthy_sources_logged_as_warnings(caplog):
    from scout.schema import HealthStatus, SearchResult

    results = [SearchResult(title="ok", url="https://a.test/1", source="mojeek")]
    health = {
        "mojeek": HealthStatus.OK,
        "startpage": HealthStatus.TIMEOUT,
        "brave": HealthStatus.ERROR,
        "guardian": HealthStatus.DISABLED,
    }
    engine = _FakeEngine(_scout_response(results, health))
    with caplog.at_level(logging.WARNING, logger="rave.scout"):
        ScoutBackend(engine).search("q", top_k=3)
    text = caplog.text
    assert "startpage" in text and "timeout" in text
    assert "brave" in text and "error" in text
    assert "guardian" not in text  # disabled is not a warning
    assert "mojeek" not in text    # ok is not a warning


# --- build_engine maps RAVE scout config onto scout.Scout --------------------

def test_build_engine_passes_settings():
    engine = build_engine(ScoutConfig(
        offline=True, trusted_outlets=["reuters.com"],
        sources={"startpage": False}, searxng_public=True,
    ))
    assert engine.config.trusted_outlets == ["reuters.com"]
    assert engine.config.offline is True
    assert engine.config.sources["startpage"].enabled is False
    assert engine.config.sources["searxng_public"].enabled is True


# --- trusted-outlet vetting boost -------------------------------------------

def test_trusted_boost_raises_weight():
    kw = dict(url="https://news.example.com/story", title="t",
              text="some ordinary news text here about a topic", date_str="",
              topic_kind=TopicKind.slow_moving)
    plain = vet(**kw, trusted=False)
    boosted = vet(**kw, trusted=True)
    assert boosted.weight > plain.weight
    assert "boost:trusted_outlet" in boosted.reasons


def test_trusted_flows_through_web_search_tool():
    from pathlib import Path

    article = (Path(__file__).parent / "fixtures" / "article.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=article,
                              headers={"content-type": "text/html"})

    class OneHitBackend:
        def search(self, query, top_k):
            return [SearchHit(url="https://trusted.test/a", title="Budget",
                              trusted=True)]

    tool = WebSearchTool(OneHitBackend(),
                         Fetcher(transport=httpx.MockTransport(handler)),
                         PipelineConfig())
    out = tool(WebSearchArgs(query="transit budget light rail", top_k=3))
    assert out["results"], "pipeline should return chunks"
    assert out["results"][0]["trusted"] is True


# --- config back-compat: old configs still load and run ----------------------

def test_old_metasearch_config_still_loads_and_runs(tmp_path):
    from config import load_config
    from tools.web_search import MetasearchBackend

    p = tmp_path / "config.yaml"
    p.write_text(
        "llm:\n  mode: local\n  model: m\n"
        "search:\n  backend: metasearch\n  metasearch_url: http://localhost:8080\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.search.backend == "metasearch"
    # scout block defaults exist even though the old file never mentioned it
    assert cfg.search.scout.trusted_outlets == []
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert isinstance(make_backend(cfg.search, fetcher), MetasearchBackend)


def test_old_crawler_config_still_loads_and_runs(tmp_path):
    from config import load_config
    from tools.web_search import CrawlerBackend

    p = tmp_path / "config.yaml"
    p.write_text(
        "llm:\n  mode: local\n  model: m\n"
        "search:\n  backend: crawler\n  crawler:\n    domains: [docs.example.com]\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.search.backend == "crawler"
    assert cfg.search.crawler.domains == ["docs.example.com"]
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert isinstance(make_backend(cfg.search, fetcher), CrawlerBackend)
