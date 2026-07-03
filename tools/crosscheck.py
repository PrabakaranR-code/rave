"""P3 cross-check: pure code, zero LLM.

Deterministic rules over the swarm's findings:
  * findings deduped by URL (within a claim group);
  * claims confirmed by >=2 independent sub-passes → confidence upgraded one level;
  * numeric/polarity contradictions inside a claim group → flagged, never smoothed;
  * sub-passes with too little evidence → flagged thin;
  * a fact → source map is built and kept for the rest of the run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from llm.schemas import Finding, upgrade_confidence
from tools.ranker import tokenize

JACCARD_THRESHOLD = 0.5
THIN_MIN_FINDINGS = 2

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_NEGATION_RE = re.compile(r"\b(not|no longer|never|discontinued|false|denied)\b", re.I)


@dataclass
class SubPassFindings:
    sub_question_id: str
    findings: list[Finding]


@dataclass
class ClaimGroup:
    group_id: str
    rep_claim: str
    findings: list[Finding] = field(default_factory=list)
    sub_passes: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    corroborated: bool = False
    upgraded: bool = False


@dataclass
class Contradiction:
    claim_a: str
    url_a: str
    claim_b: str
    url_b: str
    reason: str


@dataclass
class CrossCheckResult:
    findings: list[Finding]                     # deduped, confidence-adjusted
    groups: list[ClaimGroup]
    contradictions: list[Contradiction]
    thin_subpasses: list[str]
    fact_source_map: dict[str, list[str]]       # rep_claim -> [urls]

    def sources_for(self, claim: str) -> list[str]:
        """Best-effort lookup of sources for a claim (exact, then fuzzy)."""
        if claim in self.fact_source_map:
            return self.fact_source_map[claim]
        want = set(tokenize(claim))
        best, best_j = [], 0.0
        for rep, urls in self.fact_source_map.items():
            have = set(tokenize(rep))
            union = want | have
            j = len(want & have) / len(union) if union else 0.0
            if j > best_j:
                best, best_j = urls, j
        return best if best_j >= JACCARD_THRESHOLD else []


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM_RE.findall(text)}


def crosscheck(subpasses: list[SubPassFindings]) -> CrossCheckResult:
    # 1. dedupe: identical (claim tokens, url) pairs collapse to one finding
    seen: set[tuple[frozenset, str]] = set()
    tagged: list[tuple[str, Finding]] = []
    for sp in subpasses:
        for f in sp.findings:
            key = (frozenset(tokenize(f.claim)), f.url.strip().rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            tagged.append((sp.sub_question_id, f.model_copy(deep=True)))

    # 2. greedy claim grouping by token Jaccard similarity
    groups: list[ClaimGroup] = []
    token_cache: list[set[str]] = []
    for sq_id, f in tagged:
        toks = set(tokenize(f.claim))
        placed = None
        for gi, g in enumerate(groups):
            if _jaccard(toks, token_cache[gi]) >= JACCARD_THRESHOLD:
                placed = g
                break
        if placed is None:
            placed = ClaimGroup(group_id=f"c{len(groups) + 1}", rep_claim=f.claim)
            groups.append(placed)
            token_cache.append(toks)
        placed.findings.append(f)
        placed.sub_passes.add(sq_id)
        placed.hosts.add(_host(f.url))

    # 3. corroboration + deterministic confidence upgrade
    for g in groups:
        g.corroborated = len(g.hosts) >= 2
        if len(g.sub_passes) >= 2:
            g.upgraded = True
            for f in g.findings:
                f.confidence = upgrade_confidence(f.confidence)

    # 4. contradictions inside a group: differing numbers or flipped polarity
    contradictions: list[Contradiction] = []
    for g in groups:
        for i in range(len(g.findings)):
            for j in range(i + 1, len(g.findings)):
                a, b = g.findings[i], g.findings[j]
                na, nb = _numbers(a.claim), _numbers(b.claim)
                if na and nb and na != nb:
                    contradictions.append(
                        Contradiction(a.claim, a.url, b.claim, b.url, "numeric mismatch")
                    )
                    continue
                pa = bool(_NEGATION_RE.search(a.claim))
                pb = bool(_NEGATION_RE.search(b.claim))
                if pa != pb:
                    contradictions.append(
                        Contradiction(a.claim, a.url, b.claim, b.url, "polarity mismatch")
                    )

    # 5. thin sub-passes
    counts: dict[str, int] = {sp.sub_question_id: 0 for sp in subpasses}
    for sq_id, _f in tagged:
        counts[sq_id] = counts.get(sq_id, 0) + 1
    thin = [sq for sq, n in counts.items() if n < THIN_MIN_FINDINGS]

    fact_source_map = {
        g.rep_claim: sorted({f.url for f in g.findings}) for g in groups
    }
    return CrossCheckResult(
        findings=[f for g in groups for f in g.findings],
        groups=groups,
        contradictions=contradictions,
        thin_subpasses=sorted(thin),
        fact_source_map=fact_source_map,
    )
