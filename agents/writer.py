"""Writer agent: P5 forced `write_report` + deterministic post-checks + renderer.

Code (not the model) enforces the citation rules after validation:
  * a report finding with no citation is deleted and moved to gaps as
    "[no source found] …";
  * citations must be URLs actually seen in the dossier — unknown URLs are
    stripped (and the finding re-checked against the rule above);
  * the source ledger is rebuilt from the citations actually used.
"""
from __future__ import annotations

import json

from agents import load_prompt
from llm.client import LLMClient
from llm.schemas import (
    SourceEntry,
    VerificationLog,
    WriteReportArgs,
)
from tools.crosscheck import CrossCheckResult
from tools.registry import ToolRegistry
from tools.source_vetter import classify_source


class Writer:
    ROLE = "writer"
    PHASE = "P5"

    def __init__(self, client: LLMClient, registry: ToolRegistry):
        self.client = client
        self.registry = registry

    def write(
        self,
        standalone_query: str,
        checked: CrossCheckResult,
        coverage_notes: list[str],
        verification: VerificationLog,
    ) -> WriteReportArgs:
        dossier = self._dossier(standalone_query, checked, coverage_notes, verification)
        messages = [
            {"role": "system", "content": load_prompt(self.ROLE)},
            {"role": "user", "content": dossier},
        ]
        args: WriteReportArgs = self.client.forced_tool(
            self.ROLE, self.registry.get("write_report").decl(), messages
        )
        args = self._enforce_citations(args, checked)
        args.verification_log = verification  # dossier copy is authoritative
        self.registry.dispatch(
            "write_report", args, phase=self.PHASE, role=self.ROLE, forced=True
        )
        return args

    # -- dossier --------------------------------------------------------------

    @staticmethod
    def _dossier(
        query: str,
        checked: CrossCheckResult,
        coverage_notes: list[str],
        verification: VerificationLog,
    ) -> str:
        claims = []
        for g in checked.groups:
            claims.append(
                {
                    "claim_group": g.group_id,
                    "claim": g.rep_claim,
                    "corroborated": g.corroborated,
                    "sources": [
                        {
                            "url": f.url,
                            "date": f.date,
                            "source_type": f.source_type.value,
                            "confidence": f.confidence.value,
                            "quote": f.quote,
                        }
                        for f in g.findings
                    ],
                }
            )
        payload = {
            "question": query,
            "claims": claims,
            "contradictions": [
                {
                    "claim_a": c.claim_a, "url_a": c.url_a,
                    "claim_b": c.claim_b, "url_b": c.url_b,
                    "reason": c.reason,
                }
                for c in checked.contradictions
            ],
            "thin_subpasses": checked.thin_subpasses,
            "coverage_notes": coverage_notes,
            "verification_log": verification.model_dump(),
        }
        return (
            "Evidence dossier (the ONLY permitted source of facts):\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n\nCall write_report now."
        )

    # -- deterministic post-checks ---------------------------------------------

    @staticmethod
    def _enforce_citations(args: WriteReportArgs, checked: CrossCheckResult) -> WriteReportArgs:
        known_urls = {u.strip().rstrip("/") for urls in checked.fact_source_map.values() for u in urls}
        kept = []
        for rf in args.findings:
            cites = [c for c in rf.citations if c.strip().rstrip("/") in known_urls]
            if not cites:  # try the fact→source map before deleting
                cites = checked.sources_for(rf.claim)
            if cites:
                rf.citations = sorted(set(cites))
                kept.append(rf)
            else:
                args.gaps.append(f"[no source found] {rf.claim}")
        args.findings = kept

        used = sorted({c for rf in kept for c in rf.citations})
        by_url = {}
        for g in checked.groups:
            for f in g.findings:
                by_url.setdefault(f.url, f)
        args.source_ledger = [
            SourceEntry(
                url=u,
                title="",
                source_type=(
                    by_url[u].source_type if u in by_url else classify_source(u)
                ),
                date=by_url[u].date if u in by_url else "",
            )
            for u in used
        ]
        return args


# -- markdown rendering ----------------------------------------------------


def render_markdown(report: WriteReportArgs) -> str:
    lines: list[str] = [f"# {report.title}", ""]

    lines += ["## Executive summary", ""]
    lines += [f"- {b}" for b in report.exec_summary]
    lines.append("")

    lines += ["## Findings", ""]
    if not report.findings:
        lines += ["_No sourced findings survived verification._", ""]
    for i, f in enumerate(report.findings, 1):
        cites = " ".join(f"[{n + 1}]({u})" for n, u in enumerate(f.citations))
        corroborated = " *(corroborated)*" if len(f.citations) >= 2 else ""
        date = f" — {f.date}" if f.date else ""
        lines.append(
            f"{i}. {f.claim} — confidence **{f.confidence.value}**{corroborated}{date} {cites}"
        )
    lines.append("")

    if report.comparison_table:
        t = report.comparison_table
        lines += ["## Comparison", ""]
        lines.append("| " + " | ".join(t.headers) + " |")
        lines.append("|" + "|".join(" --- " for _ in t.headers) + "|")
        for row in t.rows:
            cells = list(row) + [""] * (len(t.headers) - len(row))
            lines.append("| " + " | ".join(cells[: len(t.headers)]) + " |")
        lines.append("")

    if report.disagreements:
        lines += ["## Disagreements between sources", ""]
        for d in report.disagreements:
            lines += [
                f"- **{d.claim}**",
                f"  - Position A: {d.position_a}",
                f"  - Position B: {d.position_b}",
            ]
            if d.likely_reason:
                lines.append(f"  - Likely reason: {d.likely_reason}")
        lines.append("")

    if report.gaps:
        lines += ["## Gaps and unknowns", ""]
        lines += [f"- {g}" for g in report.gaps]
        lines.append("")

    if report.recommendations:
        lines += ["## Recommendations", ""]
        lines += [f"- {r}" for r in report.recommendations]
        lines.append("")

    lines += ["## Source ledger", ""]
    if not report.source_ledger:
        lines.append("_No sources._")
    for s in report.source_ledger:
        bits = [s.url, f"type: {s.source_type.value}"]
        if s.date:
            bits.append(f"date: {s.date}")
        if s.title:
            bits.insert(1, s.title)
        lines.append("- " + " — ".join(bits))
    lines.append("")

    v = report.verification_log
    lines += ["## Verification log", "", f"- Verify cycles run: {v.cycles_run}"]
    for c in v.changes:
        lines.append(f"- Change: {c}")
    if v.unverified:
        lines.append("- Unverified items:")
        lines += [f"  - [unverified] {u}" for u in v.unverified]
    else:
        lines.append("- Unverified items: none")
    lines.append("")
    return "\n".join(lines)
