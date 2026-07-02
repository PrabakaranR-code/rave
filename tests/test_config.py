"""Config loading and schema validation basics."""
from __future__ import annotations

import pytest

from config import AppConfig, load_config
from llm.schemas import CreateResearchPlanArgs, Finding, SubQuestion


def test_load_config_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.mode("speed").researcher_iters == 2
    assert cfg.mode("quality").tool_call_ceiling == 150


def test_load_repo_config_yaml():
    cfg = load_config("config.yaml")
    assert cfg.llm.provider in ("openai", "anthropic")
    assert cfg.mode("balanced").critic_cycles == 1
    assert cfg.pipeline.stale_months == 6


def test_unknown_mode_raises():
    with pytest.raises(KeyError):
        AppConfig().mode("warp")


def test_finding_quote_word_cap():
    Finding(claim="c", url="http://a", quote="short quote is fine")
    with pytest.raises(ValueError):
        Finding(claim="c", url="http://a", quote="w " * 26)


def test_plan_requires_3_to_7_unique_sub_questions():
    sq = lambda i: SubQuestion(id=f"sq{i}", question=f"q{i}?", done_criteria="d")
    CreateResearchPlanArgs(standalone_query="q", sub_questions=[sq(1), sq(2), sq(3)])
    with pytest.raises(ValueError):
        CreateResearchPlanArgs(standalone_query="q", sub_questions=[sq(1), sq(2)])
    dup = [sq(1), sq(1), sq(2)]
    with pytest.raises(ValueError):
        CreateResearchPlanArgs(standalone_query="q", sub_questions=dup)
