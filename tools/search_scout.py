"""SCOUT search backend.

Wraps the keyless, in-process `scout.Scout` engine — which queries independent
search engines, science databases, Wikipedia, and news feeds in parallel and
returns one merged, deduplicated list — behind RAVE's search-backend interface
(`search(query, top_k) -> list[SearchHit]`).

Design notes:
  * SCOUT's `SearchResult` carries {title, url, snippet, source, category,
    published, trusted}; we map all of it onto `SearchHit` so the embedded
    pipeline (and the source vetter's trusted-outlet boost) can use it.
  * SCOUT never raises for a failing source — it records per-source health.
    We surface any non-ok source as a logger warning so operators can see a
    degraded fan-out without the run failing.
  * The engine is injectable so tests and smoke checks can pass a fixture-only
    `Scout(offline=True)` and never touch the network.
"""
from __future__ import annotations

import logging

from config import ScoutConfig
from tools.web_search import SearchHit

logger = logging.getLogger("rave.scout")


def build_engine(cfg: ScoutConfig):
    """Construct a `scout.Scout` from RAVE's scout settings.

    Imported lazily so the rest of RAVE (and its tests) do not require the
    scout package unless the scout backend is actually used.
    """
    from scout import Scout

    sources: dict[str, dict] = {
        name: {"enabled": bool(enabled)} for name, enabled in cfg.sources.items()
    }
    if cfg.searxng_public:
        # off by default in SCOUT; turned on only when the operator asks
        sources["searxng_public"] = {"enabled": True}
    kwargs: dict = {
        "trusted_outlets": list(cfg.trusted_outlets),
        "offline": cfg.offline,
    }
    if sources:
        kwargs["sources"] = sources
    return Scout(**kwargs)


class ScoutBackend:
    """RAVE search backend over `scout.Scout`."""

    def __init__(self, engine):
        self.engine = engine

    @classmethod
    def from_config(cls, cfg: ScoutConfig) -> "ScoutBackend":
        return cls(build_engine(cfg))

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        # ask SCOUT for a few extra candidates; the pipeline fetches a capped
        # number and ranks the survivors.
        response = self.engine.search(query, limit=max(top_k * 2, top_k))
        self._warn_unhealthy(response.health)
        hits: list[SearchHit] = []
        for r in response.results:
            if not r.url:
                continue
            hits.append(
                SearchHit(
                    url=r.url,
                    title=r.title or "",
                    snippet=r.snippet or "",
                    trusted=bool(r.trusted),
                    source=r.source or "",
                    category=r.category or "",
                    published=r.published.isoformat() if r.published else "",
                )
            )
        return hits[:top_k]

    @staticmethod
    def _warn_unhealthy(health: dict) -> None:
        for name, status in health.items():
            value = getattr(status, "value", status)
            if value not in ("ok", "disabled"):
                logger.warning("scout source %s: %s", name, value)
