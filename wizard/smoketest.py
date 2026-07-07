"""Step 4 — smoke test: one real speed-mode run with plain-language progress."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Callable

from wizard.texts import PHASE_LABELS, screen
from wizard.ui import Effects, ask_text, say

SAMPLE_QUESTION = "What is the tallest completed building in the world?"


def reports_dir(home: Path | None = None) -> Path:
    d = (home or Path.home()) / "Documents" / "RAVE"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_research(
    question: str,
    mode: str,
    config_path: str,
    on_phase: Callable[[str, str], None],
    out_dir: Path,
    orchestrator_factory: Callable | None = None,
):
    """Run one report; returns the RunResult. Factory injectable for tests."""
    if orchestrator_factory is None:
        from agents.orchestrator import Orchestrator
        from config import load_config

        def orchestrator_factory(**kw):  # noqa: F811 — default real factory
            return Orchestrator(load_config(config_path), **kw)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"report-{stamp}.md"
    orch = orchestrator_factory(
        mode=mode,
        out_path=out_path,
        runlog_path=out_dir / f"runlog-{stamp}.jsonl",
        on_phase=on_phase,
    )
    return orch.run(question)


def smoke_test(
    fx: Effects,
    config_path: str,
    home: Path | None = None,
    orchestrator_factory: Callable | None = None,
) -> bool:
    question = ask_text(fx, screen("smoke"), default=SAMPLE_QUESTION)
    last = {"label": ""}

    def on_phase(phase: str, _msg: str) -> None:
        label = PHASE_LABELS.get(phase, "Working")
        if label != last["label"]:
            last["label"] = label
            say(fx, f"{label}…")

    try:
        result = run_research(
            question, "speed", config_path, on_phase,
            reports_dir(home), orchestrator_factory,
        )
    except Exception as e:  # noqa: BLE001 — wizard shows the failure plainly
        say(fx, f"✗ The test run failed: {e}")
        return False
    say(fx, screen("smoke_done"))
    if result.report_path:
        fx.open_url(result.report_path.resolve().as_uri())
    return True
