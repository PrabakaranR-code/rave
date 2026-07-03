"""Configuration loading and validation for RAVE.

All runtime knobs live in config.yaml; this module parses the file into typed
pydantic models so the rest of the code never touches raw dicts.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ModelRouting(BaseModel):
    planner: str = "local-model"
    researcher: str = "local-model"
    critic: str = "local-model"
    writer: str = "local-model"

    def for_role(self, role: str) -> str:
        try:
            return getattr(self, role)
        except AttributeError:
            raise KeyError(f"unknown agent role: {role!r}")


class LLMConfig(BaseModel):
    provider: str = "openai"  # openai | anthropic
    base_url: str = "http://localhost:11434/v1"
    api_key_env: str = "RAVE_LLM_API_KEY"
    timeout_seconds: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.2
    models: ModelRouting = Field(default_factory=ModelRouting)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


class CommercialSearchConfig(BaseModel):
    endpoint: str = ""
    api_key_env: str = "RAVE_SEARCH_API_KEY"

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


class CrawlerConfig(BaseModel):
    domains: list[str] = Field(default_factory=list)
    index_dir: str = ".rave_index"
    max_pages_per_domain: int = 50


class SearchConfig(BaseModel):
    backend: str = "auto"  # auto | metasearch | crawler | commercial
    metasearch_url: str = ""
    commercial: CommercialSearchConfig = Field(default_factory=CommercialSearchConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)


class EmbeddingsConfig(BaseModel):
    provider: str = "hash"  # hash | local
    base_url: str = ""
    model: str = ""


class PipelineConfig(BaseModel):
    chunk_tokens: int = 400
    chunk_overlap: int = 50
    stale_months: int = 6
    max_fetch_per_search: int = 5
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)


class ModeConfig(BaseModel):
    researcher_iters: int
    critic_cycles: int
    tool_call_ceiling: int


DEFAULT_MODES: dict[str, ModeConfig] = {
    "speed": ModeConfig(researcher_iters=2, critic_cycles=0, tool_call_ceiling=25),
    "balanced": ModeConfig(researcher_iters=4, critic_cycles=1, tool_call_ceiling=60),
    "quality": ModeConfig(researcher_iters=8, critic_cycles=3, tool_call_ceiling=150),
}


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    modes: dict[str, ModeConfig] = Field(default_factory=lambda: dict(DEFAULT_MODES))

    def mode(self, name: str) -> ModeConfig:
        if name not in self.modes:
            raise KeyError(
                f"unknown mode {name!r}; available: {', '.join(sorted(self.modes))}"
            )
        return self.modes[name]


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load config.yaml; a missing file yields all defaults."""
    p = Path(path)
    if not p.exists():
        return AppConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)
