"""Wizard IO primitives with an injectable effects object.

Everything the wizard does to the outside world — reading input, printing,
hidden input, opening URLs, running commands, HTTP probes, sleeping — flows
through Effects so tests can script every interaction. Menus are loop-safe:
invalid input re-asks forever; EOF falls back to the first option.
"""
from __future__ import annotations

import getpass
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Callable

import httpx

from wizard.texts import MENU_HINT


def _default_run(cmd: list[str], timeout: float = 900.0) -> tuple[int, str]:
    """Run a command, returning (exit_code, combined_output)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out: {' '.join(cmd)}"


@dataclass
class Effects:
    input: Callable[[str], str] = input
    print: Callable[[str], None] = lambda s: print(s, file=sys.stderr)
    getpass: Callable[[str], str] = getpass.getpass
    open_url: Callable[[str], None] = lambda url: webbrowser.open(url) and None
    run: Callable[..., tuple[int, str]] = _default_run
    sleep: Callable[[float], None] = time.sleep
    http_transport: httpx.BaseTransport | None = None
    extra: dict = field(default_factory=dict)

    def http_get(self, url: str, timeout: float = 3.0) -> httpx.Response:
        with httpx.Client(transport=self.http_transport, timeout=timeout) as c:
            return c.get(url)


def say(fx: Effects, text: str) -> None:
    fx.print(text)


def menu(
    fx: Effects,
    text: str,
    n_options: int,
    letters: dict[str, int] | None = None,
) -> int:
    """Show a numbered menu; return the 1-based choice.

    `letters` maps single-letter shortcuts (e.g. "E") to a returned code
    (negative by convention). Invalid input re-asks; EOF/empty picks 1.
    """
    fx.print(text)
    fx.print(MENU_HINT)
    letters = {k.lower(): v for k, v in (letters or {}).items()}
    while True:
        try:
            raw = fx.input("> ").strip()
        except EOFError:
            return 1
        if not raw:
            return 1
        if raw.lower() in letters:
            return letters[raw.lower()]
        if raw.isdigit() and 1 <= int(raw) <= n_options:
            return int(raw)
        fx.print(f"Please type a number between 1 and {n_options}.")


def ask_text(fx: Effects, prompt: str, default: str = "") -> str:
    fx.print(prompt)
    try:
        raw = fx.input("> ").strip()
    except EOFError:
        raw = ""
    return raw or default


def ask_hidden(fx: Effects, prompt: str) -> str:
    fx.print(prompt)
    while True:
        try:
            raw = fx.getpass("(hidden) > ").strip()
        except EOFError:
            return ""
        if raw:
            return raw
        fx.print("Nothing was pasted — try again.")


def enter_loop(
    fx: Effects,
    message: str,
    check: Callable[[], bool],
    retry_message: str = "Not found yet — install it, then press Enter to check again.",
    max_tries: int | None = None,
) -> bool:
    """Print message, wait for Enter, re-run check; loop until true.

    Returns False when max_tries is exhausted (or immediately on EOF with a
    failing check), so callers can offer a fallback.
    """
    fx.print(message)
    tries = 0
    while True:
        try:
            fx.input("(press Enter when done) ")
        except EOFError:
            return check()
        if check():
            return True
        tries += 1
        if max_tries is not None and tries >= max_tries:
            return False
        fx.print(retry_message)
