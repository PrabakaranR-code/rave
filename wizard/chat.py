"""Daily chat mode: bare `rave` — one question, a depth menu, a saved report."""
from __future__ import annotations

import sys
from pathlib import Path

from wizard.smoketest import reports_dir, run_research
from wizard.texts import PHASE_LABELS, screen
from wizard.ui import Effects, ask_text, menu, say

DEPTHS = {1: "speed", 2: "balanced", 3: "quality"}


def run_chat(
    fx: Effects | None = None,
    home: Path | None = None,
    config_path: str = "config.yaml",
    orchestrator_factory=None,
) -> int:
    fx = fx or Effects()
    question = ask_text(fx, screen("chat_ask"))
    if not question:
        say(fx, "Nothing to research — see you next time.")
        return 0
    mode = DEPTHS[menu(fx, screen("chat_depth"), 3)]

    last = {"label": ""}

    def on_phase(phase: str, _msg: str) -> None:
        label = PHASE_LABELS.get(phase, "Working")
        if label != last["label"]:
            last["label"] = label
            say(fx, f"{label}…")

    from agents.orchestrator import HaltError
    from llm.client import ForcedToolError, LLMError
    from tools.web_search import SearchSetupError

    try:
        result = run_research(
            question, mode, config_path, on_phase,
            reports_dir(home), orchestrator_factory,
        )
    except HaltError as e:
        say(fx, str(e))
        return 2
    except SearchSetupError as e:
        say(fx, str(e) + "\nRun `rave setup` to fix this.")
        return 3
    except (ForcedToolError, LLMError) as e:
        say(fx, f"The AI model failed: {e}\nRun `rave setup` to pick another.")
        return 4

    print(result.markdown, file=sys.stdout)
    say(fx, f"✓ Report saved to {result.report_path}")
    if result.report_path:
        fx.open_url(result.report_path.resolve().as_uri())
    return 0
