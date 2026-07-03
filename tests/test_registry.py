"""Registry dispatch, run-log recording, and budget hard stops."""
from __future__ import annotations

import json

import pytest

from llm.schemas import DoneArgs, WebSearchArgs
from tools.registry import Budget, BudgetExceeded, RunLog, ToolRegistry, ToolSpec


def make_registry(tmp_path, ceiling=3):
    runlog = RunLog(tmp_path / "runlog.jsonl")
    reg = ToolRegistry(runlog, Budget(ceiling))
    reg.register(ToolSpec("done", "finish", DoneArgs))  # signal tool: no executor
    reg.register(
        ToolSpec("web_search", "search", WebSearchArgs, executor=lambda a: {"hits": a.top_k})
    )
    return reg, runlog


def test_dispatch_runs_executor_and_logs(tmp_path):
    reg, runlog = make_registry(tmp_path)
    result = reg.dispatch(
        "web_search", WebSearchArgs(query="q", top_k=2), phase="P2", role="researcher"
    )
    assert result == {"hits": 2}
    lines = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    assert lines[0]["tool"] == "web_search"
    assert lines[0]["phase"] == "P2"
    assert lines[0]["args"]["query"] == "q"
    assert lines[0]["error"] is None


def test_signal_tool_returns_validated_args(tmp_path):
    reg, _ = make_registry(tmp_path)
    args = DoneArgs(coverage_summary="covered")
    assert reg.dispatch("done", args, phase="P2", role="researcher", forced=True) is args


def test_budget_ceiling_is_a_hard_stop(tmp_path):
    reg, _ = make_registry(tmp_path, ceiling=2)
    args = DoneArgs(coverage_summary="x")
    reg.dispatch("done", args, phase="P2", role="r")
    reg.dispatch("done", args, phase="P2", role="r")
    with pytest.raises(BudgetExceeded):
        reg.dispatch("done", args, phase="P2", role="r")


def test_auto_calls_are_logged_but_cost_no_budget(tmp_path):
    reg, runlog = make_registry(tmp_path, ceiling=1)
    args = DoneArgs(coverage_summary="cap reached")
    reg.dispatch("done", args, phase="P2", role="r", forced=True, auto=True)
    assert reg.budget.used == 0
    assert runlog.entries[0]["auto"] is True


def test_executor_exception_is_logged_and_reraised(tmp_path):
    runlog = RunLog(tmp_path / "runlog.jsonl")
    reg = ToolRegistry(runlog, Budget(5))

    def boom(_args):
        raise RuntimeError("fetch failed")

    reg.register(ToolSpec("web_search", "search", WebSearchArgs, executor=boom))
    with pytest.raises(RuntimeError):
        reg.dispatch("web_search", WebSearchArgs(query="q"), phase="P2", role="r")
    assert "fetch failed" in runlog.entries[0]["error"]


def test_duplicate_registration_rejected(tmp_path):
    reg, _ = make_registry(tmp_path)
    with pytest.raises(ValueError):
        reg.register(ToolSpec("done", "again", DoneArgs))
