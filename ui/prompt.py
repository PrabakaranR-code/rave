"""User-interaction renderer: numbered options + a free-text escape hatch.

Works in the CLI today; the QuestionSpec payload is deliberately shaped so a
future web UI can render the same question as chips/buttons plus a text box
without touching agent code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from rich.console import Console

from llm.schemas import AskUserArgs


@dataclass
class QuestionSpec:
    question: str
    options: list[str] = field(default_factory=list)
    allow_free_text: bool = True

    @classmethod
    def from_ask_user(cls, args: AskUserArgs) -> "QuestionSpec":
        return cls(question=args.question, options=list(args.options),
                   allow_free_text=args.allow_free_text)

    def to_payload(self) -> dict:
        """Chip/button-ready wire format for a web front end."""
        return {
            "type": "question",
            "question": self.question,
            "choices": [
                {"id": i + 1, "label": opt} for i, opt in enumerate(self.options)
            ],
            "free_text": self.allow_free_text,
        }


def render_question(
    spec: QuestionSpec,
    console: Console | None = None,
    input_fn: Callable[[str], str] = input,
) -> str:
    """Show the question, return the chosen option text or free text.

    Selection rules:
      * a number in range picks that option;
      * anything else is accepted as free text (when allowed);
      * invalid input re-asks; EOF/empty falls back to the first option
        (or an explicit no-answer marker when there are no options).
    """
    console = console or Console(stderr=True)
    console.print(f"\n[bold]{spec.question}[/bold]")
    for i, opt in enumerate(spec.options, 1):
        console.print(f"  [cyan]{i}.[/cyan] {opt}")
    if spec.allow_free_text:
        hint = "number or free text" if spec.options else "free text"
    else:
        hint = "number"
    prompt = f"> ({hint}) "

    while True:
        try:
            raw = input_fn(prompt).strip()
        except EOFError:
            raw = ""
        if not raw:
            if spec.options:
                console.print(f"[dim]defaulting to: {spec.options[0]}[/dim]")
                return spec.options[0]
            return "(no answer provided)"
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(spec.options):
                return spec.options[n - 1]
            console.print(f"[red]pick a number between 1 and {len(spec.options)}[/red]")
            continue
        if spec.allow_free_text:
            return raw
        console.print("[red]free text is not allowed here; pick a number[/red]")


def cli_ask_user(args: AskUserArgs) -> str:
    """Registry executor for `ask_user` in interactive CLI runs."""
    return render_question(QuestionSpec.from_ask_user(args))
