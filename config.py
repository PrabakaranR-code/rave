"""Configuration loading and validation for RAVE.

All runtime knobs live in config.yaml; this module parses the file into typed
pydantic models so the rest of the code never touches raw dicts.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

LOCAL_DEFAULT_BASE_URL = "http://localhost:11434/v1"


class LLMConfig(BaseModel):
    """Single-model LLM configuration.

    mode "local" is keyless and defaults to a local server's OpenAI-compatible
    /v1 (works out of the box with a local model server hosting any model);
    mode "api" requires base_url and reads the key from the env var named by
    api_key_env. The wire protocol is auto-detected from base_url: an
    api.anthropic.com URL speaks the Anthropic messages protocol, everything
    else speaks OpenAI-compatible chat completions.
    """

    mode: str = "local"  # local | api
    base_url: str = ""
    model: str = "llama3.1:8b"
    api_key_env: str = "RAVE_LLM_API_KEY"  # api mode only
    timeout_seconds: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.2

    @model_validator(mode="after")
    def _fill_and_check(self) -> "LLMConfig":
        if self.mode not in ("local", "api"):
            raise ValueError(f"llm.mode must be 'local' or 'api', got {self.mode!r}")
        if not self.base_url:
            if self.mode == "api":
                raise ValueError("llm.mode 'api' requires llm.base_url")
            self.base_url = LOCAL_DEFAULT_BASE_URL
        return self

    @property
    def protocol(self) -> str:
        return "anthropic" if "api.anthropic.com" in self.base_url else "openai"

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
