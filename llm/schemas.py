"""JSON-schema contracts for every LLM-callable tool.

Each contract is a pydantic model; the LLM client converts these to JSON
schemas for the API request and validates the returned arguments against them.
An agent can only ever move the run forward by producing arguments that
validate against one of these models.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared enums and the Finding schema (used everywhere, no exceptions)
# ---------------------------------------------------------------------------

class SourceType(str, Enum):
    primary = "primary"
    official = "official"
    peer_reviewed = "peer_reviewed"
    news = "news"
    analyst = "analyst"
    blog = "blog"
    forum = "forum"
    ai_generated = "ai_generated"


class Confidence(str, Enum):
    high = "H"
    medium = "M"
    low = "L"


CONFIDENCE_ORDER = [Confidence.low, Confidence.medium, Confidence.high]


def upgrade_confidence(c: Confidence) -> Confidence:
    """One-level deterministic upgrade: L -> M -> H (H stays H)."""
    i = CONFIDENCE_ORDER.index(c)
    return CONFIDENCE_ORDER[min(i + 1, len(CONFIDENCE_ORDER) - 1)]


class Finding(BaseModel):
    """A single sourced claim. No source, no claim."""

    claim: str = Field(min_length=1, description="One factual claim, stated plainly.")
    url: str = Field(min_length=1, description="Source URL the claim came from.")
    date: str = Field(
        default="",
        description="Publication date if known, else retrieval date (ISO preferred).",
    )
    source_type: SourceType = SourceType.news
    confidence: Confidence = Confidence.low
    quote: str = Field(
        default="",
        description="Verbatim supporting quote from the source, 25 words max.",
    )

    @field_validator("quote")
    @classmethod
    def quote_max_25_words(cls, v: str) -> str:
        if len(v.split()) > 25:
            raise ValueError("quote must be verbatim and at most 25 words")
        return v


# ---------------------------------------------------------------------------
# P0 — Intake
# ---------------------------------------------------------------------------

class TopicKind(str, Enum):
    fast_moving = "fast_moving"   # prices, policy, models, product specs, people-in-roles
    slow_moving = "slow_moving"   # history, fundamentals


class ClarifyingQuestion(BaseModel):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, max_length=6)


class ClassifyQueryArgs(BaseModel):
    """Planner call 1: classify the query and rewrite it standalone."""

    standalone_question: str = Field(
        min_length=1,
        description="The user's query rewritten as one standalone question, using any provided context.",
    )
    topic_kind: TopicKind = Field(
        description="fast_moving topics trigger the 6-month staleness rule; slow_moving are exempt."
    )
    needs_clarification: bool = Field(
        description="True only if a critical detail is ambiguous and blocks planning."
    )
    clarifying_question: ClarifyingQuestion | None = Field(
        default=None,
        description="Exactly one clarifying question, required when needs_clarification is true.",
    )


class AskUserArgs(BaseModel):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, max_length=8)
    allow_free_text: bool = True


# ---------------------------------------------------------------------------
# P1 — Decompose
# ---------------------------------------------------------------------------

class SubQuestion(BaseModel):
    id: str = Field(min_length=1, description="Short stable id, e.g. 'sq1'.")
    question: str = Field(min_length=1)
    done_criteria: str = Field(
        min_length=1, description="What evidence makes this sub-question answered."
    )


class CreateResearchPlanArgs(BaseModel):
    standalone_query: str = Field(min_length=1)
    sub_questions: list[SubQuestion] = Field(min_length=3, max_length=7)

    @field_validator("sub_questions")
    @classmethod
    def unique_ids(cls, v: list[SubQuestion]) -> list[SubQuestion]:
        ids = [s.id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("sub_question ids must be unique")
        return v


# ---------------------------------------------------------------------------
# P2 — Swarm research
# ---------------------------------------------------------------------------

class PlanPreambleArgs(BaseModel):
    """Must be the first tool call of every researcher turn."""

    current_goal: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    expected_info: str = Field(min_length=1)


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class FetchPageArgs(BaseModel):
    url: str = Field(min_length=1, pattern=r"^https?://")


class RecordFindingsArgs(BaseModel):
    """Findings are emitted only through this schema — never as free text."""

    findings: list[Finding] = Field(min_length=1)


class DoneArgs(BaseModel):
    coverage_summary: str = Field(min_length=1)
    gaps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# P4 — Critic
# ---------------------------------------------------------------------------

class IssueType(str, Enum):
    unsourced = "unsourced"
    single_source = "single_source"
    stale = "stale"
    conflict = "conflict"
    weak_source = "weak_source"
    coverage_gap = "coverage_gap"


class CritiqueIssue(BaseModel):
    id: str = Field(min_length=1)
    type: IssueType
    claim_ref: str = Field(
        default="", description="The claim text (or claim id) this issue refers to."
    )
    instruction: str = Field(
        min_length=1, description="Concrete re-search instruction to resolve the issue."
    )


class CritiqueFindingsArgs(BaseModel):
    issues: list[CritiqueIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# P5 — Report
# ---------------------------------------------------------------------------

class ReportFinding(BaseModel):
    claim: str = Field(min_length=1)
    citations: list[str] = Field(
        default_factory=list, description="Source URLs backing the claim."
    )
    confidence: Confidence = Confidence.low
    date: str = ""


class ComparisonTable(BaseModel):
    headers: list[str] = Field(min_length=2)
    rows: list[list[str]] = Field(default_factory=list)


class Disagreement(BaseModel):
    claim: str
    position_a: str
    position_b: str
    likely_reason: str = ""


class SourceEntry(BaseModel):
    url: str
    title: str = ""
    source_type: SourceType = SourceType.news
    date: str = ""


class VerificationLog(BaseModel):
    cycles_run: int = 0
    changes: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)


class WriteReportArgs(BaseModel):
    title: str = Field(min_length=1)
    exec_summary: list[str] = Field(min_length=1, max_length=5)
    findings: list[ReportFinding] = Field(default_factory=list)
    comparison_table: ComparisonTable | None = None
    disagreements: list[Disagreement] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    source_ledger: list[SourceEntry] = Field(default_factory=list)
    verification_log: VerificationLog = Field(default_factory=VerificationLog)
