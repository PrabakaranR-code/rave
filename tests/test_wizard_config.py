"""Providers table, config writing, .env secrets, dual-language lint."""
from __future__ import annotations

import os
import stat

import pytest

from config import LLMConfig, load_config
from wizard.configwrite import ConfigChoices, write_config
from wizard.envfile import load_env, write_env
from wizard.providers import BUNDLES, PROVIDERS, by_key
from wizard.texts import JARGON, SCREENS


# --- providers table ---------------------------------------------------------

def test_providers_table_integrity():
    keys = [p.key for p in PROVIDERS]
    assert keys == ["anthropic", "openai", "google", "deepseek", "kimi",
                    "qwen", "mistral", "groq", "perplexity"]
    for p in PROVIDERS:
        assert p.base_url.startswith("https://")
        assert p.key_page.startswith("https://")
        assert p.models, f"{p.key} has no model menu"
        assert all(m.id and m.note for m in p.models)


def test_anthropic_provider_base_url_triggers_anthropic_protocol():
    p = by_key("anthropic")
    assert LLMConfig(mode="api", base_url=p.base_url).protocol == "anthropic"
    for other in PROVIDERS:
        if other.key != "anthropic":
            assert LLMConfig(mode="api", base_url=other.base_url).protocol == "openai"


def test_bundles_reference_known_providers():
    assert set(BUNDLES) == {"anthropic", "openai", "google", "qwen", "kimi"}
    assert BUNDLES["anthropic"][-1] == "claude-ai"  # the online-chat bundle


# --- config writing ----------------------------------------------------------

def test_write_config_local_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path, ConfigChoices(model="qwen2.5:7b",
                                     search_backend="crawler",
                                     crawler_domains=["en.wikipedia.org"]))
    cfg = load_config(path)
    assert cfg.llm.mode == "local" and cfg.llm.model == "qwen2.5:7b"
    assert cfg.llm.base_url == "http://localhost:11434/v1"  # blank → default
    assert cfg.search.backend == "crawler"
    assert cfg.search.crawler.domains == ["en.wikipedia.org"]


def test_write_config_api_metasearch(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path, ConfigChoices(
        llm_mode="api", base_url="https://api.anthropic.com",
        model="claude-sonnet-4-6", search_backend="metasearch",
        metasearch_url="http://localhost:8080",
    ))
    cfg = load_config(path)
    assert cfg.llm.protocol == "anthropic"
    assert cfg.search.metasearch_url == "http://localhost:8080"
    # inline docs survive generation (the expert layer reads these)
    text = path.read_text()
    assert "mode local" in text and "api_key_env" in text


def test_write_config_never_contains_a_secret(tmp_path):
    os.environ["RAVE_LLM_API_KEY"] = "sk-super-secret-value"
    try:
        path = tmp_path / "config.yaml"
        write_config(path, ConfigChoices(llm_mode="api",
                                         base_url="https://api.openai.com/v1",
                                         model="gpt-4o-mini"))
        assert "sk-super-secret-value" not in path.read_text()
    finally:
        del os.environ["RAVE_LLM_API_KEY"]


def test_write_config_rejects_invalid_and_keeps_old_file(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path, ConfigChoices())
    before = path.read_text()
    with pytest.raises(Exception):
        write_config(path, ConfigChoices(llm_mode="warp"))
    assert path.read_text() == before


# --- .env secrets ------------------------------------------------------------

def test_write_env_creates_owner_only_and_replaces(tmp_path):
    env = tmp_path / ".env"
    write_env(env, "RAVE_LLM_API_KEY", "first")
    write_env(env, "OTHER", "x")
    write_env(env, "RAVE_LLM_API_KEY", "second")
    text = env.read_text()
    assert text.count("RAVE_LLM_API_KEY=") == 1
    assert "RAVE_LLM_API_KEY=second" in text and "OTHER=x" in text
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A_KEY=from_file\n# comment\nB_KEY='quoted'\n")
    monkeypatch.setenv("A_KEY", "from_environment")
    monkeypatch.delenv("B_KEY", raising=False)
    loaded = load_env(env)
    assert loaded == {"A_KEY": "from_file", "B_KEY": "quoted"}
    assert os.environ["A_KEY"] == "from_environment"
    assert os.environ["B_KEY"] == "quoted"


def test_load_env_missing_file_is_fine(tmp_path):
    assert load_env(tmp_path / "absent.env") == {}


# --- dual-language lint ------------------------------------------------------

def test_every_jargon_term_is_explained_on_first_use_per_screen():
    """GOLDEN RULE: real name + plain-word bracket on first use per screen."""
    for name, text in SCREENS.items():
        for term in JARGON:
            idx = text.find(term)
            if idx == -1:
                continue
            window = text[idx + len(term): idx + len(term) + 60]
            assert "(" in window, (
                f"screen {name!r}: {term!r} is not followed by a plain-word"
                f" bracket (got: {text[idx:idx + 80]!r})"
            )


def test_jargon_list_covers_the_spec_terms():
    for required in ("API key", "Docker", "MCP", "SearXNG", "base_url",
                     "Ollama", "tunnel"):
        assert required in JARGON
