"""Orchestrator: a pure-Python state machine owning phases P0→P5.

Not an LLM. It performs the connectivity check, wires the tool registry,
enforces budgets, runs the agents in order, and writes report.md plus the
JSONL run log. Agents can only move the run forward through forced,
schema-validated tool calls dispatched here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from agents.critic import Critic, verify_issue
from agents.planner import Planner
from agents.researcher import Researcher, ResearchOutcome
from agents.writer import Writer, render_markdown
from config import AppConfig
from llm.client import LLMClient
from llm.schemas import (
    AskUserArgs,
    ClassifyQueryArgs,
    CreateResearchPlanArgs,
    CritiqueFindingsArgs,
    DoneArgs,
    FetchPageArgs,
    PlanPreambleArgs,
    RecordFindingsArgs,
    VerificationLog,
    WebSearchArgs,
    WriteReportArgs,
)
from tools.crosscheck import CrossCheckResult, SubPassFindings, crosscheck
from tools.fetch_page import Fetcher, FetchPageTool
from tools.ranker import make_embedder
from tools.registry import Budget, RunLog, ToolRegistry, ToolSpec
from tools.web_search import WebSearchTool, make_backend

PROBE_URLS = ("https://example.com/", "https://example.org/")

HALT_MESSAGE = (
    "HALT: no working web access.\n"
    "This engine only answers from live retrieval — never from model memory.\n"
    "Check your network connection and search backend configuration, then retry."
)


class HaltError(Exception):
    """Raised when the startup connectivity check fails (principle 1)."""


def check_connectivity(
    extra_urls: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 5.0,
) -> bool:
    """True if any probe URL answers at all (any HTTP status < 500)."""
    urls = [u for u in (extra_urls or []) if u] + list(PROBE_URLS)
    client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)
    try:
        for url in urls:
            try:
                resp = client.get(url)
                if resp.status_code < 500:
                    return True
            except httpx.HTTPError:
                continue
        return False
    finally:
        client.close()


def default_ask_user(args: AskUserArgs) -> str:
    """Non-interactive fallback: pick the first option (or decline to answer)."""
    if args.options:
        return args.options[0]
    return "(no answer provided; proceed with your best interpretation)"


@dataclass
class RunResult:
    report: WriteReportArgs
    markdown: str
    classification: ClassifyQueryArgs
    plan: CreateResearchPlanArgs
    checked: CrossCheckResult
    outcomes: list[ResearchOutcome]
    verification: VerificationLog
    runlog_path: Path | None
    report_path: Path | None
    tool_calls_used: int = 0


class Orchestrator:
    def __init__(
        self,
        cfg: AppConfig,
        mode: str = "balanced",
        *,
        out_path: str | Path | None = "report.md",
        runlog_path: str | Path | None = "runlog.jsonl",
        confirm: bool = False,
        ask_user_fn: Callable[[AskUserArgs], str] | None = None,
        on_phase: Callable[[str, str], None] | None = None,
        llm_transport: httpx.BaseTransport | None = None,
        web_transport: httpx.BaseTransport | None = None,
    ):
        self.cfg = cfg
        self.mode_name = mode
        self.mode = cfg.mode(mode)
        self.out_path = Path(out_path) if out_path else None
        self.confirm = confirm
        self.on_phase = on_phase or (lambda phase, msg: None)
        self.ask_user_fn = ask_user_fn or default_ask_user
        self._web_transport = web_transport

        self.runlog = RunLog(runlog_path)
        self.budget = Budget(self.mode.tool_call_ceiling)
        self.registry = ToolRegistry(self.runlog, self.budget)

        self.fetcher = Fetcher(transport=web_transport)
        backend = make_backend(cfg.search, self.fetcher, transport=web_transport)
        self.web_search_tool = WebSearchTool(
            backend, self.fetcher, cfg.pipeline,
            embedder=make_embedder(cfg.pipeline.embeddings),
        )
        self.fetch_page_tool = FetchPageTool(self.fetcher)
        self._register_tools()

        self.client = LLMClient(cfg.llm, transport=llm_transport)
        self.planner = Planner(self.client, self.registry)
        self.writer = Writer(self.client, self.registry)

    # -- registry ---------------------------------------------------------

    def _register_tools(self) -> None:
        reg = self.registry
        reg.register(ToolSpec(
            "classify_query",
            "Classify the user query, rewrite it standalone, and flag whether one "
            "clarifying question is needed.",
            ClassifyQueryArgs,
        ))
        reg.register(ToolSpec(
            "ask_user",
            "Ask the user exactly one question, offering numbered options plus "
            "free text. Returns the user's choice or text.",
            AskUserArgs,
            executor=lambda args: self.ask_user_fn(args),
        ))
        reg.register(ToolSpec(
            "create_research_plan",
            "Commit the research plan: the standalone query plus 3–7 mutually "
            "independent sub-questions, each with done criteria.",
            CreateResearchPlanArgs,
        ))
        reg.register(ToolSpec(
            "plan_preamble",
            "State the current goal, the next action, and the information it is "
            "expected to yield. Must be the first tool call of every turn.",
            PlanPreambleArgs,
        ))
        reg.register(ToolSpec(
            "web_search",
            "Search the live web. Returns vetted, ranked text chunks with URL, "
            "title, date, source type, and staleness metadata.",
            WebSearchArgs,
            executor=self.web_search_tool,
        ))
        reg.register(ToolSpec(
            "fetch_page",
            "Fetch one URL (must come from earlier results) and return its "
            "cleaned text, title, and best-effort publication date.",
            FetchPageArgs,
            executor=self.fetch_page_tool,
        ))
        reg.register(ToolSpec(
            "record_findings",
            "Bank one or more findings in the strict Finding schema (claim, url, "
            "date, source_type, confidence, verbatim quote of 25 words max).",
            RecordFindingsArgs,
        ))
        reg.register(ToolSpec(
            "done",
            "Finish research on this sub-question with an honest coverage summary "
            "and remaining gaps.",
            DoneArgs,
        ))
        reg.register(ToolSpec(
            "critique_findings",
            "Report audit issues found in the claim inventory (unsourced, "
            "single_source, stale, conflict, weak_source, coverage_gap).",
            CritiqueFindingsArgs,
        ))
        reg.register(ToolSpec(
            "write_report",
            "Produce the final report: title, exec summary, cited findings, "
            "disagreements, gaps, recommendations, source ledger, verification log.",
            WriteReportArgs,
        ))

    # -- phases -----------------------------------------------------------

    def run(self, question: str, context: str = "") -> RunResult:
        self._phase("P0", "connectivity check")
        probe_extra = [self.cfg.search.metasearch_url] if self.cfg.search.metasearch_url else []
        if not check_connectivity(probe_extra, transport=self._web_transport):
            self.runlog.record(event="halt", phase="P0", reason="no web access")
            raise HaltError(HALT_MESSAGE)

        self._phase("P0", "classifying query")
        classification = self.planner.classify(question, context)
        self.web_search_tool.topic_kind = classification.topic_kind

        clarification = ""
        if classification.needs_clarification:
            self._phase("P0", "asking one clarifying question")
            clarification = self.planner.clarify(classification)

        self._phase("P1", "decomposing into sub-questions")
        plan = self._plan_with_confirmation(classification.standalone_question, clarification)

        self._phase("P2", f"researching {len(plan.sub_questions)} sub-questions")
        outcomes = self._research_all(plan)

        self._phase("P3", "cross-checking findings (deterministic)")
        checked = crosscheck([o.subpass for o in outcomes])
        self.runlog.record(
            event="crosscheck", phase="P3",
            groups=len(checked.groups),
            contradictions=len(checked.contradictions),
            thin_subpasses=checked.thin_subpasses,
        )

        coverage_notes = [
            f"{o.subpass.sub_question_id}: {o.done.coverage_summary}" for o in outcomes
        ] + [g for o in outcomes for g in o.done.gaps]

        self._phase("P4", "critic → verify loop")
        checked, verification = self._critic_verify(
            classification.standalone_question, checked, outcomes, coverage_notes
        )

        self._phase("P5", "writing report")
        report = self.writer.write(
            classification.standalone_question, checked, coverage_notes, verification
        )
        markdown = render_markdown(report)
        if self.out_path:
            self.out_path.write_text(markdown, encoding="utf-8")
        self.runlog.record(event="run_complete", phase="P5",
                           tool_calls_used=self.budget.used)
        return RunResult(
            report=report,
            markdown=markdown,
            classification=classification,
            plan=plan,
            checked=checked,
            outcomes=outcomes,
            verification=verification,
            runlog_path=self.runlog.path,
            report_path=self.out_path,
            tool_calls_used=self.budget.used,
        )

    # -- helpers ----------------------------------------------------------

    def _phase(self, phase: str, msg: str) -> None:
        self.runlog.record(event="phase", phase=phase, message=msg)
        self.on_phase(phase, msg)

    def _plan_with_confirmation(
        self, standalone_query: str, clarification: str
    ) -> CreateResearchPlanArgs:
        # --confirm rendering of the Proceed/Edit gate arrives with the UX
        # phase; without it the first plan is accepted as-is.
        return self.planner.plan(standalone_query, clarification)

    def _research_all(self, plan: CreateResearchPlanArgs) -> list[ResearchOutcome]:
        """P2 swarm: one Researcher per sub-question, run in parallel."""

        def one(sub_q) -> ResearchOutcome:
            researcher = Researcher(self.client, self.registry, self.mode.researcher_iters)
            return researcher.research(sub_q, plan.standalone_query)

        async def gather():
            return await asyncio.gather(
                *(asyncio.to_thread(one, sq) for sq in plan.sub_questions)
            )

        return list(asyncio.run(gather()))

    def _critic_verify(
        self,
        standalone_query: str,
        checked: CrossCheckResult,
        outcomes: list[ResearchOutcome],
        coverage_notes: list[str],
    ) -> tuple[CrossCheckResult, VerificationLog]:
        """P4: forced critique, then <=2 tool calls of code-driven verification
        per issue, looping until the critic is clean or the cycle cap hits.

        critic_cycles = 0 (speed): the critic still audits once, report-only —
        its issues are surfaced as [unverified], with no re-search.
        """
        critic = Critic(self.client, self.registry)
        subpasses: list[SubPassFindings] = [o.subpass for o in outcomes]
        changes: list[str] = []
        open_issues = []
        cycles_run = 0

        while True:
            if self.budget.remaining < 1:
                changes.append("verification stopped: global tool-call ceiling reached")
                break
            critique = critic.critique(
                standalone_query, checked, coverage_notes, cycle=cycles_run + 1
            )
            open_issues = list(critique.issues)
            if not open_issues:
                break
            if cycles_run >= self.mode.critic_cycles:
                break  # cap reached: whatever is open stays open, reported below
            cycles_run += 1
            self._phase("P4", f"verify cycle {cycles_run}: {len(open_issues)} issue(s)")
            for issue in open_issues:
                outcome = verify_issue(issue, checked, self.registry)
                changes.extend(outcome.notes)
                if outcome.new_findings:
                    subpasses.append(SubPassFindings(
                        sub_question_id=f"vf-{issue.id}-c{cycles_run}",
                        findings=outcome.new_findings,
                    ))
            checked = crosscheck(subpasses)

        unverified = [
            f"{i.type.value}: {i.claim_ref or i.instruction}" for i in open_issues
        ]
        verification = VerificationLog(
            cycles_run=cycles_run, changes=changes, unverified=unverified
        )
        self.runlog.record(
            event="verification", phase="P4",
            cycles_run=cycles_run, changes=changes, unverified=unverified,
        )
        return checked, verification
