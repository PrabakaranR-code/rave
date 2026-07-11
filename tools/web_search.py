"""`web_search` tool: pluggable search backends + the embedded pipeline.

Backends (one interface, chosen in config.yaml):
  (a) metasearch — a self-hosted SearXNG/metasearch instance (recommended:
                   community-maintained engine adapters, Google + Bing reach);
  (b) scout      — built-in keyless multi-source search (zero-install
                   fallback; see tools/search_scout.py);
  (c) crawler    — own mini-crawler + local BM25 index over a user domain list;
  (d) commercial — a generic JSON search API adapter (key in an env var).
Selection when backend is "auto": (a) when a SearXNG answers — at the
configured metasearch_url, or at localhost:8080 when none is configured —
else (d) if a commercial key is present, else (b) scout, which needs no
configuration at all. Explicitly named backends are never probed.

Every search runs the embedded pipeline on the hits: fetch → extract → chunk
→ hybrid rank (BM25 + embedding cosine) → vet → top chunks with metadata.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from config import SearchConfig
from llm.schemas import TopicKind
from tools.chunker import chunk_text
from tools.extractor import extract
from tools.fetch_page import Fetcher, FetchError
from tools.ranker import BM25, hybrid_rank, tokenize
from tools.source_vetter import vet


class SearchSetupError(Exception):
    """No usable search backend is configured."""


SETUP_HINT = (
    "Search backend misconfigured. Set one of:\n"
    "  * search.backend: scout — built-in, keyless, no setup (recommended), or\n"
    "  * search.metasearch_url in config.yaml (self-hosted metasearch, keyless), or\n"
    "  * search.commercial.endpoint + an API key in the env var named by\n"
    "    search.commercial.api_key_env, or\n"
    "  * search.backend: crawler with search.crawler.domains for a local index."
)


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    # SCOUT-provided metadata; other backends leave these at their defaults.
    trusted: bool = False
    source: str = ""
    category: str = ""
    published: str = ""


class MetasearchBackend:
    """Self-hosted metasearch instance exposing /search?q=...&format=json."""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None,
                 timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  follow_redirects=True)

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        resp = self._http.get(
            f"{self.base_url}/search", params={"q": query, "format": "json"}
        )
        resp.raise_for_status()
        data = resp.json()
        hits = []
        for r in data.get("results", [])[: top_k * 2]:
            url = r.get("url") or r.get("link") or ""
            if url:
                hits.append(SearchHit(url, r.get("title", ""), r.get("content", "")))
        return hits[:top_k]


class CommercialBackend:
    """Generic adapter for keyed JSON search APIs (shape-tolerant parsing)."""

    def __init__(self, endpoint: str, api_key: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  follow_redirects=True)

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        resp = self._http.get(
            self.endpoint,
            params={"q": query, "count": top_k},
            headers={"X-API-Key": self.api_key,
                     "Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        rows = (
            data.get("results")
            or data.get("items")
            or data.get("web", {}).get("results")
            or []
        )
        hits = []
        for r in rows[:top_k]:
            url = r.get("url") or r.get("link") or ""
            if url:
                hits.append(
                    SearchHit(url, r.get("title") or r.get("name", ""),
                              r.get("snippet") or r.get("description", ""))
                )
        return hits


class CrawlerBackend:
    """Mini-crawler + local BM25 index over a user-supplied domain list."""

    def __init__(self, domains: list[str], index_dir: str, fetcher: Fetcher,
                 max_pages_per_domain: int = 50):
        if not domains:
            raise SearchSetupError("crawler backend needs search.crawler.domains")
        self.domains = domains
        self.index_path = Path(index_dir) / "index.jsonl"
        self.fetcher = fetcher
        self.max_pages = max_pages_per_domain
        self._docs: list[dict] | None = None
        self._bm25: BM25 | None = None

    def _load_or_build(self) -> None:
        if self._docs is not None:
            return
        if self.index_path.exists():
            self._docs = [
                json.loads(line)
                for line in self.index_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            self._docs = self._crawl()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("w", encoding="utf-8") as f:
                for d in self._docs:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
        self._bm25 = BM25([tokenize(d["title"] + " " + d["text"]) for d in self._docs])

    def _crawl(self) -> list[dict]:
        docs: list[dict] = []
        for domain in self.domains:
            start = domain if domain.startswith("http") else f"https://{domain}"
            host = urlsplit(start).netloc
            queue: deque[str] = deque([start])
            seen: set[str] = set()
            while queue and len(seen) < self.max_pages:
                url = queue.popleft()
                if url in seen:
                    continue
                seen.add(url)
                try:
                    page = self.fetcher.fetch(url)
                except FetchError:
                    continue
                ex = extract(page.html)
                if ex.text:
                    docs.append({"url": page.final_url, "title": ex.title,
                                 "text": ex.text[:20000], "date": ex.date})
                for link in _links(page.html, page.final_url):
                    if urlsplit(link).netloc == host and link not in seen:
                        queue.append(link)
        return docs

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        self._load_or_build()
        assert self._docs is not None and self._bm25 is not None
        scored = sorted(
            enumerate(self._bm25.scores(query)), key=lambda p: p[1], reverse=True
        )
        return [
            SearchHit(self._docs[i]["url"], self._docs[i]["title"],
                      self._docs[i]["text"][:200])
            for i, s in scored[:top_k]
            if s > 0
        ]


def _links(html: str, base_url: str) -> list[str]:
    import re

    out = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\'#]+)["\']', html, re.I):
        href = m.group(1)
        if href.startswith(("mailto:", "javascript:")):
            continue
        out.append(urljoin(base_url, href))
    return out


DEFAULT_SEARXNG_URL = "http://localhost:8080"


def searxng_reachable(
    base_url: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 1.5,
) -> bool:
    """True when a SearXNG-style JSON search API answers at base_url."""
    try:
        with httpx.Client(transport=transport, timeout=timeout,
                          follow_redirects=True) as client:
            resp = client.get(
                f"{base_url.rstrip('/')}/search",
                params={"q": "ping", "format": "json"},
            )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 — unreachable means "not there"
        return False


def make_backend(
    cfg: SearchConfig,
    fetcher: Fetcher,
    transport: httpx.BaseTransport | None = None,
):
    kind = cfg.backend
    if kind == "auto":
        # A reachable SearXNG wins (recommended: community-maintained breadth);
        # probe the configured URL, or localhost:8080 when none is configured.
        probe_url = cfg.metasearch_url or DEFAULT_SEARXNG_URL
        if searxng_reachable(probe_url, transport):
            return MetasearchBackend(probe_url, transport=transport)
        if cfg.commercial.endpoint and cfg.commercial.api_key:
            kind = "commercial"
        else:
            kind = "scout"  # built-in, keyless — the zero-install fallback
    if kind == "scout":
        from tools.search_scout import ScoutBackend  # lazy: avoids import cycle

        return ScoutBackend.from_config(cfg.scout)
    if kind == "metasearch":
        if not cfg.metasearch_url:
            raise SearchSetupError(SETUP_HINT)
        return MetasearchBackend(cfg.metasearch_url, transport=transport)
    if kind == "commercial":
        if not (cfg.commercial.endpoint and cfg.commercial.api_key):
            raise SearchSetupError(SETUP_HINT)
        return CommercialBackend(cfg.commercial.endpoint, cfg.commercial.api_key,
                                 transport=transport)
    if kind == "crawler":
        return CrawlerBackend(cfg.crawler.domains, cfg.crawler.index_dir, fetcher,
                              cfg.crawler.max_pages_per_domain)
    raise SearchSetupError(f"unknown search backend {kind!r}\n{SETUP_HINT}")


class WebSearchTool:
    """Executor for the `web_search` LLM tool: backend + embedded pipeline."""

    def __init__(self, backend, fetcher: Fetcher, pipeline_cfg, embedder=None):
        self.backend = backend
        self.fetcher = fetcher
        self.cfg = pipeline_cfg
        self.embedder = embedder
        # set by the orchestrator after query classification (P0)
        self.topic_kind: TopicKind = TopicKind.fast_moving

    def __call__(self, args) -> dict:
        hits = self.backend.search(args.query, args.top_k)
        chunks: list[dict] = []
        fetched = 0
        for hit in hits:
            if fetched >= self.cfg.max_fetch_per_search:
                break
            try:
                page = self.fetcher.fetch(hit.url)
            except FetchError:
                continue
            fetched += 1
            ex = extract(page.html)
            verdict = vet(
                url=page.final_url, title=ex.title or hit.title, text=ex.text,
                date_str=ex.date, topic_kind=self.topic_kind,
                stale_months=self.cfg.stale_months,
                trusted=hit.trusted,
            )
            if verdict.drop:
                continue
            for ch in chunk_text(ex.text, self.cfg.chunk_tokens, self.cfg.chunk_overlap):
                chunks.append({
                    "url": page.final_url,
                    "title": ex.title or hit.title,
                    "date": ex.date,
                    "source_type": verdict.source_type.value,
                    "stale": verdict.stale,
                    "trusted": hit.trusted,
                    "vet_weight": verdict.weight,
                    "text": ch.text,
                })
        if not chunks:
            return {"query": args.query, "results": [],
                    "note": "no fetchable results; try a different query"}
        ranked = hybrid_rank(args.query, [c["text"] for c in chunks], self.embedder)
        results = []
        for item in ranked:
            c = chunks[item.index]
            results.append({**c, "score": round(item.score * c["vet_weight"], 4)})
        results.sort(key=lambda c: c["score"], reverse=True)
        return {"query": args.query, "results": results[: max(args.top_k, 8)]}
