"""Critic agent (forced audit) + pure-code verification of its issues.

The Critic returns issues via forced `critique_findings`. Verification is
plain code: for each issue, at most two tool calls — one `web_search` on the
critic's alternate query angle, and one re-fetch of a cited URL to confirm the
page still supports the claim. New evidence is fed back through crosscheck;
nothing here asks an LLM to judge anything.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from agents import load_prompt
from llm.client import LLMClient
from llm.schemas import (
    Confidence,
    CritiqueFindingsArgs,
    CritiqueIssue,
    FetchPageArgs,
    Finding,
    SourceType,
    WebSearchArgs,
)
from tools.chunker import split_sentences
from tools.crosscheck import CrossCheckResult
from tools.ranker import tokenize
from tools.registry import BudgetExceeded, ToolRegistry

SUPPORT_THRESHOLD = 0.6   # share of claim tokens a page must contain
CHUNK_THRESHOLD = 0.5     # share of claim tokens a candidate chunk must contain
RESEARCH_CALLS_PER_ISSUE = 2


class Critic:
    ROLE = "critic"
    PHASE = "P4"

    def __init__(self, client: LLMClient, registry: ToolRegistry):
        self.client = client
        self.registry = registry

    def critique(
        self,
        standalone_query: str,
        checked: CrossCheckResult,
        coverage_notes: list[str],
        cycle: int,
    ) -> CritiqueFindingsArgs:
        inventory = {
            "question": standalone_query,
            "cycle": cycle,
            "claims": [
                {
                    "claim": g.rep_claim,
                    "corroborated": g.corroborated,
                    "sources": [
                        {"url": f.url, "date": f.date,
                         "source_type": f.source_type.value,
                         "confidence": f.confidence.value, "quote": f.quote}
                        for f in g.findings
                    ],
                }
                for g in checked.groups
            ],
            "detected_contradictions": [
                {"claim_a": c.claim_a, "claim_b": c.claim_b, "reason": c.reason}
                for c in checked.contradictions
            ],
            "thin_subpasses": checked.thin_subpasses,
            "coverage_notes": coverage_notes,
        }
        messages = [
            {"role": "system", "content": load_prompt(self.ROLE)},
            {
                "role": "user",
                "content": "Claim inventory to audit:\n"
                + json.dumps(inventory, ensure_ascii=False, indent=2)
                + "\n\nCall critique_findings with your issues (empty list if clean).",
            },
        ]
        args: CritiqueFindingsArgs = self.client.forced_tool(
            self.ROLE, self.registry.get("critique_findings").decl(), messages
        )
        self.registry.dispatch(
            "critique_findings", args, phase=self.PHASE, role=self.ROLE, forced=True
        )
        return args


# ---------------------------------------------------------------------------
# Pure-code verification (no LLM)
# ---------------------------------------------------------------------------

@dataclass
class VerifyOutcome:
    issue: CritiqueIssue
    resolved: bool
    new_findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _coverage(claim: str, text: str) -> float:
    want = set(tokenize(claim))
    if not want:
        return 0.0
    have = set(tokenize(text))
    return len(want & have) / len(want)


def claim_supported(claim: str, page_text: str) -> bool:
    return _coverage(claim, page_text) >= SUPPORT_THRESHOLD


def best_supporting_chunk(
    claim: str, results: list[dict], exclude_urls: set[str]
) -> dict | None:
    best, best_cov = None, CHUNK_THRESHOLD
    for r in results:
        url = (r.get("url") or "").strip().rstrip("/")
        if not url or url in exclude_urls:
            continue
        cov = _coverage(claim, r.get("text", ""))
        if cov >= best_cov:
            best, best_cov = r, cov
    return best


def verbatim_quote(claim: str, chunk_text: str, max_words: int = 25) -> str:
    """The chunk sentence that best covers the claim, cut verbatim to 25 words."""
    sentences = split_sentences(chunk_text) or [chunk_text]
    best = max(sentences, key=lambda s: _coverage(claim, s))
    return " ".join(best.split()[:max_words])


def verify_issue(
    issue: CritiqueIssue,
    checked: CrossCheckResult,
    registry: ToolRegistry,
) -> VerifyOutcome:
    """<=2 tool calls: re-search a different angle, re-fetch a cited URL."""
    outcome = VerifyOutcome(issue=issue, resolved=False)
    claim = issue.claim_ref or issue.instruction
    existing = {u.strip().rstrip("/") for u in checked.sources_for(claim)}
    calls = 0

    # call 1 — targeted re-search using the critic's alternate query angle
    if registry.budget is None or registry.budget.remaining >= 1:
        try:
            result = registry.dispatch(
                "web_search",
                WebSearchArgs(query=(issue.instruction or claim)[:300], top_k=3),
                phase="P4", role="verifier",
            )
            calls += 1
            chunk = best_supporting_chunk(claim, result.get("results", []), existing)
            if chunk:
                outcome.new_findings.append(Finding(
                    claim=claim,
                    url=chunk["url"],
                    date=chunk.get("date", ""),
                    source_type=SourceType(chunk.get("source_type", "news")),
                    confidence=Confidence.low,
                    quote=verbatim_quote(claim, chunk.get("text", "")),
                ))
                outcome.notes.append(
                    f"issue {issue.id} ({issue.type.value}): new source found — {chunk['url']}"
                )
            else:
                outcome.notes.append(
                    f"issue {issue.id} ({issue.type.value}): re-search found no supporting source"
                )
        except BudgetExceeded:
            outcome.notes.append(f"issue {issue.id}: verification stopped (budget ceiling)")
            return outcome

    # call 2 — re-fetch a cited URL to confirm it still supports the claim
    cited = checked.sources_for(claim)
    if cited and calls < RESEARCH_CALLS_PER_ISSUE and (
        registry.budget is None or registry.budget.remaining >= 1
    ):
        try:
            page = registry.dispatch(
                "fetch_page", FetchPageArgs(url=cited[0]), phase="P4", role="verifier"
            )
            if isinstance(page, dict) and not page.get("error"):
                if claim_supported(claim, page.get("text", "")):
                    outcome.notes.append(
                        f"issue {issue.id}: cited page still supports the claim — {cited[0]}"
                    )
                else:
                    outcome.notes.append(
                        f"issue {issue.id}: cited page NO LONGER supports the claim — {cited[0]}"
                    )
        except BudgetExceeded:
            outcome.notes.append(f"issue {issue.id}: verification stopped (budget ceiling)")

    outcome.resolved = bool(outcome.new_findings)
    return outcome
