"""Researcher agent: the P2 tool loop for one sub-question.

Every turn starts with a forced `plan_preamble`; the model then chooses among
web_search / fetch_page / record_findings / done. Findings citing URLs that
were never retrieved this run are rejected in code (no source, no claim).
`done` auto-fires at the iteration cap.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents import load_prompt
from llm.client import LLMClient, tool_result_message
from llm.schemas import DoneArgs, Finding, SubQuestion
from tools.crosscheck import SubPassFindings
from tools.registry import BudgetExceeded, ToolRegistry

ACTION_TOOLS = ["web_search", "fetch_page", "record_findings", "done"]


@dataclass
class ResearchOutcome:
    subpass: SubPassFindings
    done: DoneArgs
    turns_used: int


class Researcher:
    ROLE = "researcher"
    PHASE = "P2"

    def __init__(self, client: LLMClient, registry: ToolRegistry, max_iters: int):
        self.client = client
        self.registry = registry
        self.max_iters = max_iters

    def research(self, sub_q: SubQuestion, standalone_query: str) -> ResearchOutcome:
        messages = [
            {"role": "system", "content": load_prompt(self.ROLE)},
            {
                "role": "user",
                "content": (
                    f"Main question (context only):\n{standalone_query}\n\n"
                    f"YOUR sub-question ({sub_q.id}):\n{sub_q.question}\n\n"
                    f"Done criteria:\n{sub_q.done_criteria}\n\n"
                    f"You have at most {self.max_iters} turns."
                ),
            },
        ]
        findings: list[Finding] = []
        seen_urls: set[str] = set()
        done: DoneArgs | None = None
        turns = 0

        try:
            for _turn in range(self.max_iters):
                turns += 1
                pre = self.client.forced_tool(
                    self.ROLE, self.registry.get("plan_preamble").decl(), messages
                )
                self.registry.dispatch(
                    "plan_preamble", pre, phase=self.PHASE, role=self.ROLE, forced=True
                )
                messages.append(
                    {"role": "assistant", "content": f"[tool_call plan_preamble] {pre.model_dump_json()}"}
                )
                calls = self.client.choose_tool(
                    self.ROLE, self.registry.decls(ACTION_TOOLS), messages
                )
                for call in calls:
                    messages.append({"role": "assistant", "content": call.transcript_line()})
                    if call.name == "record_findings":
                        kept, rejected = self._filter_findings(
                            call.arguments.findings, seen_urls
                        )
                        self.registry.dispatch(
                            "record_findings", call.arguments,
                            phase=self.PHASE, role=self.ROLE,
                        )
                        findings.extend(kept)
                        note = {"recorded": len(kept)}
                        if rejected:
                            note["rejected_unretrieved_urls"] = rejected
                        messages.append(tool_result_message("record_findings", note))
                    elif call.name == "done":
                        done = call.arguments
                        self.registry.dispatch(
                            "done", done, phase=self.PHASE, role=self.ROLE, forced=True
                        )
                        break
                    else:  # web_search / fetch_page
                        result = self.registry.dispatch(
                            call.name, call.arguments,
                            phase=self.PHASE, role=self.ROLE,
                        )
                        self._track_urls(call.name, result, seen_urls)
                        messages.append(tool_result_message(call.name, result))
                if done is not None:
                    break
        except BudgetExceeded:
            done = DoneArgs(
                coverage_summary="stopped early: global tool-call ceiling reached",
                gaps=[f"{sub_q.id}: research incomplete (budget ceiling)"],
            )
            self.registry.dispatch(
                "done", done, phase=self.PHASE, role=self.ROLE, forced=True, auto=True
            )

        if done is None:  # auto-fire at the iteration cap
            done = DoneArgs(
                coverage_summary=(
                    f"iteration cap ({self.max_iters}) reached with "
                    f"{len(findings)} findings recorded"
                ),
                gaps=[] if findings else [f"{sub_q.id}: no findings recorded"],
            )
            self.registry.dispatch(
                "done", done, phase=self.PHASE, role=self.ROLE, forced=True, auto=True
            )

        return ResearchOutcome(
            subpass=SubPassFindings(sub_question_id=sub_q.id, findings=findings),
            done=done,
            turns_used=turns,
        )

    @staticmethod
    def _track_urls(tool_name: str, result, seen: set[str]) -> None:
        if not isinstance(result, dict):
            return
        if tool_name == "web_search":
            for r in result.get("results", []):
                if r.get("url"):
                    seen.add(_norm(r["url"]))
        elif tool_name == "fetch_page" and result.get("url"):
            seen.add(_norm(result["url"]))

    @staticmethod
    def _filter_findings(
        candidates: list[Finding], seen_urls: set[str]
    ) -> tuple[list[Finding], list[str]]:
        kept, rejected = [], []
        for f in candidates:
            if _norm(f.url) in seen_urls:
                kept.append(f)
            else:
                rejected.append(f.url)
        return kept, rejected


def _norm(url: str) -> str:
    return url.strip().rstrip("/")
