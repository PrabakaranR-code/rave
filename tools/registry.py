"""Tool registry, run log, and budget enforcement.

The registry declares every LLM-callable tool (JSON schema + Python executor),
dispatches calls, and records everything to the run log. Budgets are enforced
here in plain code — hard stops, not LLM judgment.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from llm.client import ToolDecl


class BudgetExceeded(Exception):
    """Raised when the mode's global tool-call ceiling would be exceeded."""


class Budget:
    """Counts every dispatched tool call against the mode's hard ceiling."""

    def __init__(self, tool_call_ceiling: int):
        self.ceiling = tool_call_ceiling
        self.used = 0
        self._lock = threading.Lock()

    def spend(self, n: int = 1) -> None:
        with self._lock:
            if self.used + n > self.ceiling:
                raise BudgetExceeded(
                    f"global tool-call ceiling reached ({self.used}/{self.ceiling})"
                )
            self.used += n

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.ceiling - self.used


class RunLog:
    """Append-only JSONL log of every tool call and phase transition."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.entries: list[dict] = []
        self._lock = threading.Lock()
        if self.path:
            self.path.write_text("", encoding="utf-8")

    def record(self, **entry: Any) -> None:
        entry.setdefault("ts", round(time.time(), 3))
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.entries.append(entry)
            if self.path:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")


@dataclass
class ToolSpec:
    """An LLM-callable tool: schema contract + optional Python executor.

    Tools without an executor are pure phase-transition signals: dispatch
    simply returns the validated arguments.
    """

    name: str
    description: str
    model: type[BaseModel]
    executor: Callable[[BaseModel], Any] | None = None

    def decl(self) -> ToolDecl:
        return ToolDecl(self.name, self.description, self.model)


class ToolRegistry:
    def __init__(self, runlog: RunLog, budget: Budget | None = None):
        self.runlog = runlog
        self.budget = budget
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"tool {name!r} is not registered") from None

    def decls(self, names: list[str]) -> list[ToolDecl]:
        return [self.get(n).decl() for n in names]

    def dispatch(
        self,
        name: str,
        args: BaseModel,
        *,
        phase: str,
        role: str,
        forced: bool = False,
        auto: bool = False,
    ) -> Any:
        """Execute a validated tool call, spending budget and logging it.

        `auto=True` marks calls synthesized by the orchestrator (e.g. `done`
        auto-firing at the iteration cap); these are logged but cost no budget.
        """
        spec = self.get(name)
        if self.budget is not None and not auto:
            self.budget.spend()
        error: str | None = None
        result: Any = None
        try:
            result = spec.executor(args) if spec.executor else args
            return result
        except Exception as e:  # noqa: BLE001 — logged, then re-raised
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self.runlog.record(
                event="tool_call",
                phase=phase,
                role=role,
                tool=name,
                forced=forced,
                auto=auto,
                args=args.model_dump(mode="json"),
                result_summary=_summarize(result) if error is None else None,
                error=error,
                budget_used=self.budget.used if self.budget else None,
            )


def _summarize(result: Any, limit: int = 400) -> str:
    if result is None:
        return ""
    if isinstance(result, BaseModel):
        s = result.model_dump_json()
    else:
        s = json.dumps(result, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"
