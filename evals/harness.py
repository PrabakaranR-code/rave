"""Eval harness: run the pipeline per benchmark question and score it.

Scores per run:
  * citation coverage % — report findings carrying at least one citation;
  * corroboration %     — findings carrying two or more citations;
  * unverified count    — items the verify loop could not confirm;
  * fact hit rate       — expected key facts present in the rendered report.

One row is appended to results.tsv (tab-separated):
    commit  score  mode  status  description
status is decided deterministically: "keep" if the score is at least the best
previous score in the file, else "discard". results.tsv stays untracked by
git — the header is created on first run.

Workflow: change a prompt or module → run the harness → keep the commit if
scores improve, revert if not.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RESULTS_HEADER = "commit\tscore\tmode\tstatus\tdescription"
EVALS_DIR = Path(__file__).resolve().parent


@dataclass
class EvalQuestion:
    id: str
    question: str
    mode: str = "balanced"
    expected_facts: list[str] = field(default_factory=list)


@dataclass
class EvalOutcome:
    question_id: str
    ok: bool
    citation_coverage: float = 0.0
    corroboration: float = 0.0
    unverified: int = 0
    fact_hit_rate: float = 0.0
    error: str = ""

    @property
    def score(self) -> float:
        """0–100 composite; unverified items cost 2 points each."""
        base = 100 * (
            0.5 * self.citation_coverage
            + 0.3 * self.corroboration
            + 0.2 * self.fact_hit_rate
        )
        return max(0.0, round(base - 2 * self.unverified, 1))


def load_questions(path: str | Path) -> list[EvalQuestion]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [EvalQuestion(**q) for q in raw["questions"]]


def fact_hit_rate(expected: list[str], report_md: str) -> float:
    """Each expected entry may hold 'A OR B' alternatives."""
    if not expected:
        return 1.0
    text = report_md.lower()
    hits = sum(
        1 for entry in expected
        if any(alt.strip().lower() in text for alt in entry.split(" OR "))
    )
    return hits / len(expected)


def score_run(q: EvalQuestion, result) -> EvalOutcome:
    findings = result.report.findings
    cited = [f for f in findings if f.citations]
    corroborated = [f for f in findings if len(f.citations) >= 2]
    n = len(findings) or 1
    return EvalOutcome(
        question_id=q.id,
        ok=True,
        citation_coverage=len(cited) / n,
        corroboration=len(corroborated) / n,
        unverified=len(result.verification.unverified),
        fact_hit_rate=fact_hit_rate(q.expected_facts, result.markdown),
    )


def current_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def best_previous_score(results_path: Path) -> float | None:
    if not results_path.exists():
        return None
    best = None
    for line in results_path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                s = float(parts[1])
            except ValueError:
                continue
            best = s if best is None else max(best, s)
    return best


def append_result(results_path: Path, score: float, mode: str, description: str) -> str:
    prev_best = best_previous_score(results_path)
    status = "keep" if prev_best is None or score >= prev_best else "discard"
    if not results_path.exists():
        results_path.write_text(RESULTS_HEADER + "\n", encoding="utf-8")
    row = f"{current_commit()}\t{score}\t{mode}\t{status}\t{description}"
    with results_path.open("a", encoding="utf-8") as f:
        f.write(row + "\n")
    return status


def default_factory(config_path: str):
    """Build a real orchestrator per question (needs a live backend + LLM)."""
    from agents.orchestrator import Orchestrator
    from config import load_config

    cfg = load_config(config_path)

    def run(question: str, mode: str):
        orch = Orchestrator(
            cfg, mode=mode,
            out_path=None, runlog_path=None,
        )
        return orch.run(question)

    return run


def run_evals(
    questions_path: str | Path,
    results_path: str | Path,
    mode_override: str | None = None,
    description: str = "",
    runner=None,
    config_path: str = "config.yaml",
    log=print,
) -> tuple[float, str, list[EvalOutcome]]:
    """Run every benchmark question; append one aggregate row to results.tsv."""
    questions = load_questions(questions_path)
    runner = runner or default_factory(config_path)
    outcomes: list[EvalOutcome] = []
    for q in questions:
        mode = mode_override or q.mode
        log(f"[{q.id}] {q.question} (mode={mode})")
        try:
            result = runner(q.question, mode)
            outcome = score_run(q, result)
        except Exception as e:  # noqa: BLE001 — a failed question scores zero
            outcome = EvalOutcome(question_id=q.id, ok=False, error=str(e))
        outcomes.append(outcome)
        log(f"    score={outcome.score}"
            + (f" error={outcome.error}" if outcome.error else ""))

    avg = round(sum(o.score for o in outcomes) / (len(outcomes) or 1), 1)
    status = append_result(
        Path(results_path), avg, mode_override or "per-question", description
    )
    log(f"aggregate score={avg} status={status}")
    return avg, status, outcomes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rave-evals", description=__doc__)
    p.add_argument("--questions", default=str(EVALS_DIR / "questions.yaml"))
    p.add_argument("--results", default=str(EVALS_DIR / "results.tsv"))
    p.add_argument("--mode", default=None,
                   choices=["speed", "balanced", "quality"],
                   help="override the per-question mode")
    p.add_argument("--desc", default="", help="description column for results.tsv")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args(argv)
    run_evals(
        args.questions, args.results,
        mode_override=args.mode, description=args.desc, config_path=args.config,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
