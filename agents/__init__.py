"""Agent roles. Each agent = a prompt + a restricted toolset + forced calls."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(role: str) -> str:
    text = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
    return f"{text}\n\nToday's date is {_dt.date.today().isoformat()}."
