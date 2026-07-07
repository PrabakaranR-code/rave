"""CLI entry point.

Usage:
    python main.py                      # daily chat mode (friendly)
    python main.py setup [...]          # setup wizard (add --expert to skip hand-holding)
    python main.py "your question" --mode balanced [--confirm] [--out report.md]

Research runs print phase-by-phase progress to stderr, the rendered report to
stdout, and save report.md plus runlog.jsonl.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from agents.orchestrator import HaltError, Orchestrator
from config import load_config
from llm.client import ForcedToolError, LLMError
from tools.registry import BudgetExceeded
from tools.web_search import SearchSetupError
from ui.prompt import cli_ask_user

PHASE_LABELS = {
    "P0": "intake",
    "P1": "decompose",
    "P2": "swarm research",
    "P3": "cross-check",
    "P4": "verify",
    "P5": "report",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rave",
        description="RAVE — Research And Verify Engine. Live-web research with "
                    "schema-enforced agents, deterministic cross-checking, and "
                    "an adversarial verification loop.",
    )
    p.add_argument("question", help="the question to research")
    p.add_argument("--mode", choices=["speed", "balanced", "quality"],
                   default="balanced", help="effort/budget preset (default: balanced)")
    p.add_argument("--confirm", action="store_true",
                   help="review and edit the research plan before the swarm runs")
    p.add_argument("--out", default="report.md", help="report output path")
    p.add_argument("--runlog", default="runlog.jsonl", help="JSONL run log path")
    p.add_argument("--config", default="config.yaml", help="config file path")
    p.add_argument("--context", default="", help="optional conversation/context text")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    from wizard.envfile import load_env

    load_env(Path(__file__).resolve().parent / ".env")
    if argv and argv[0] == "setup":
        from wizard.wizard import run_setup

        return run_setup(argv[1:])
    if not argv:
        from wizard.chat import run_chat

        return run_chat()

    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    console = Console(stderr=True, highlight=False)

    def on_phase(phase: str, msg: str) -> None:
        label = PHASE_LABELS.get(phase, phase)
        console.print(f"[bold cyan]{phase}[/bold cyan] [dim]{label}[/dim] — {msg}")

    try:
        orch = Orchestrator(
            cfg,
            mode=args.mode,
            out_path=args.out,
            runlog_path=args.runlog,
            confirm=args.confirm,
            ask_user_fn=cli_ask_user if sys.stdin.isatty() else None,
            on_phase=on_phase,
        )
        result = orch.run(args.question, context=args.context)
    except HaltError as e:
        print(str(e), file=sys.stderr)
        return 2
    except SearchSetupError as e:
        print(str(e), file=sys.stderr)
        return 3
    except (ForcedToolError, LLMError) as e:
        print(f"LLM failure: {e}", file=sys.stderr)
        return 4
    except BudgetExceeded as e:
        print(f"Budget stop: {e}", file=sys.stderr)
        return 5

    print(result.markdown)
    console.print(
        f"[green]Saved[/green] {result.report_path} and {result.runlog_path} "
        f"({result.tool_calls_used} tool calls used)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
