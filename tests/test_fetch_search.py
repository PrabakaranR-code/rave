"""Fetcher (robots, headers, errors) and web_search backends/pipeline — all mocked."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from config import CommercialSearchConfig, PipelineConfig, SearchConfig
from llm.schemas import FetchPageArgs, WebSearchArgs
from tools.fetch_page import Fetcher, FetchError, FetchPageTool, RobotsDisallowed
from tools.web_search import (
    CommercialBackend,
    MetasearchBackend,
    SearchSetupError,
    WebSearchTool,
    make_backend,
)

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE = (FIXTURES / "article.html").read_text(encoding="utf-8")
PLAIN = (FIXTURES / "plain.html").read_text(encoding="utf-8")


def site_transport(robots="User-agent: *\nDisallow: /private/\n"):
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers[str(request.url.path)] = dict(request.headers)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if request.url.path.startswith("/private/"):
            return httpx.Response(200, html="<p>secret</p>")
        if request.url.path == "/article":
            return httpx.Response(200, html=ARTICLE)
        if request.url.path == "/plain":
            return httpx.Response(200, html=PLAIN)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"url": "https://site.test/article", "title": "Budget vote", "content": "council"},
                {"url": "https://site.test/plain", "title": "Frogs", "content": "frogs"},
            ]})
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler), seen_headers


def test_fetch_sends_polite_headers_and_returns_html():
    transport, seen = site_transport()
    fetcher = Fetcher(transport=transport)
    page = fetcher.fetch("https://site.test/article")
    assert page.status == 200 and "Transit Budget" in page.html
    ua = seen["/article"]["user-agent"]
    assert "rave-research-bot" in ua and "robots" in ua


def test_robots_disallow_blocks_fetch():
    transport, _ = site_transport()
    fetcher = Fetcher(transport=transport)
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://site.test/private/page")
    # allowed path still works, robots cached per host
    assert fetcher.fetch("https://site.test/plain").status == 200


def test_http_error_status_raises_fetch_error():
    transport, _ = site_transport()
    fetcher = Fetcher(transport=transport)
    with pytest.raises(FetchError):
        fetcher.fetch("https://site.test/missing")


def test_fetch_page_tool_returns_clean_text_and_date():
    transport, _ = site_transport()
    tool = FetchPageTool(Fetcher(transport=transport))
    out = tool(FetchPageArgs(url="https://site.test/article"))
    assert out["title"] == "City Council Approves New Transit Budget"
    assert out["date"] == "2026-05-14" and out["date_kind"] == "publication"
    assert "Subscribe now" not in out["text"]


def test_fetch_page_tool_reports_errors_instead_of_raising():
    transport, _ = site_transport()
    tool = FetchPageTool(Fetcher(transport=transport))
    out = tool(FetchPageArgs(url="https://site.test/missing"))
    assert "error" in out


def test_metasearch_backend_parses_results():
    transport, _ = site_transport()
    backend = MetasearchBackend("https://site.test", transport=transport)
    hits = backend.search("transit", top_k=2)
    assert [h.url for h in hits] == ["https://site.test/article", "https://site.test/plain"]


def test_commercial_backend_parses_alternate_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "k123"
        return httpx.Response(200, json={"web": {"results": [
            {"link": "https://a.test/x", "name": "A", "description": "d"}]}})

    backend = CommercialBackend("https://api.test/search", "k123",
                                transport=httpx.MockTransport(handler))
    hits = backend.search("q", 3)
    assert hits[0].url == "https://a.test/x" and hits[0].title == "A"


def test_backend_auto_selection_and_scout_default(monkeypatch):
    from tools.search_scout import ScoutBackend

    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    # metasearch_url takes precedence
    cfg = SearchConfig(backend="auto", metasearch_url="https://meta.test")
    assert isinstance(make_backend(cfg, fetcher), MetasearchBackend)

    # then a commercial key
    monkeypatch.setenv("RAVE_SEARCH_API_KEY", "k")
    cfg = SearchConfig(
        backend="auto",
        commercial=CommercialSearchConfig(endpoint="https://api.test/s"),
    )
    assert isinstance(make_backend(cfg, fetcher), CommercialBackend)

    # with nothing configured, auto now falls back to the keyless SCOUT backend
    monkeypatch.delenv("RAVE_SEARCH_API_KEY", raising=False)
    from config import ScoutConfig

    cfg = SearchConfig(backend="auto", scout=ScoutConfig(offline=True))
    assert isinstance(make_backend(cfg, fetcher), ScoutBackend)


def test_explicit_broken_backend_raises_setup_hint():
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    # backend named but its requirement missing → loud setup hint
    with pytest.raises(SearchSetupError):
        make_backend(SearchConfig(backend="metasearch"), fetcher)
    with pytest.raises(SearchSetupError):
        make_backend(SearchConfig(backend="commercial"), fetcher)


def test_web_search_tool_end_to_end_pipeline():
    transport, _ = site_transport()
    backend = MetasearchBackend("https://site.test", transport=transport)
    tool = WebSearchTool(backend, Fetcher(transport=transport), PipelineConfig())
    out = tool(WebSearchArgs(query="city transit budget light rail", top_k=2))
    assert out["results"], "pipeline should return ranked chunks"
    top = out["results"][0]
    assert top["url"] == "https://site.test/article"
    assert top["source_type"] and "score" in top
    assert top["date"] == "2026-05-14"
    # the frog page must rank below the transit article for this query
    urls = [r["url"] for r in out["results"]]
    assert urls.index("https://site.test/article") < len(urls)
