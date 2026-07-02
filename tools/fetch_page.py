"""Polite HTTP fetcher plus the `fetch_page` tool executor.

Respects robots.txt (cached per host, fetched with the same client so tests
can mock everything), sends an honest user agent, and enforces timeouts and a
response-size cap. The tool executor runs the fetch → extract pipeline and
returns cleaned text + title + best-effort publication date.
"""
from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit, urlunsplit

import httpx

from tools.extractor import extract

USER_AGENT = "rave-research-bot/0.1 (self-hosted research engine; respects robots.txt)"
MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT = 15.0


class FetchError(Exception):
    """A page could not be fetched (network, status, robots, or size)."""


class RobotsDisallowed(FetchError):
    """robots.txt forbids fetching this URL."""


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    html: str


class Fetcher:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        respect_robots: bool = True,
    ):
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en",
            },
        )
        self.respect_robots = respect_robots
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._lock = threading.Lock()

    def fetch(self, url: str) -> FetchedPage:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError(f"unsupported URL scheme: {url}")
        if self.respect_robots and not self._allowed(parts):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise FetchError(f"fetch failed for {url}: {e}") from e
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        body = resp.text
        if len(body.encode("utf-8", errors="ignore")) > MAX_BYTES:
            body = body[: MAX_BYTES // 2]
        return FetchedPage(
            url=url, final_url=str(resp.url), status=resp.status_code, html=body
        )

    def _allowed(self, parts) -> bool:
        host_key = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            rp = self._robots.get(host_key)
        if rp is None:
            rp = robotparser.RobotFileParser()
            robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
            try:
                resp = self._http.get(robots_url)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.allow_all = True  # no readable robots.txt → allowed
            except httpx.HTTPError:
                rp.allow_all = True
            with self._lock:
                self._robots[host_key] = rp
        return rp.can_fetch(USER_AGENT, urlunsplit(parts))


class FetchPageTool:
    """Executor for the `fetch_page` LLM tool: fetch → extract → cleaned text."""

    def __init__(self, fetcher: Fetcher, max_chars: int = 8000):
        self.fetcher = fetcher
        self.max_chars = max_chars

    def __call__(self, args) -> dict:
        try:
            page = self.fetcher.fetch(args.url)
        except FetchError as e:
            return {"url": args.url, "error": str(e)}
        ex = extract(page.html)
        retrieved = _dt.date.today().isoformat()
        return {
            "url": page.final_url,
            "title": ex.title,
            "date": ex.date or retrieved,
            "date_kind": "publication" if ex.date else "retrieval",
            "text": ex.text[: self.max_chars],
        }
