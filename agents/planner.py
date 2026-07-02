"""Planner agent: P0 classify (+ optional single clarification) and P1 plan."""
from __future__ import annotations

from agents import load_prompt
from llm.client import LLMClient
from llm.schemas import (
    AskUserArgs,
    ClassifyQueryArgs,
    CreateResearchPlanArgs,
)
from tools.registry import ToolRegistry


class Planner:
    ROLE = "planner"

    def __init__(self, client: LLMClient, registry: ToolRegistry):
        self.client = client
        self.registry = registry

    def classify(self, query: str, context: str = "") -> ClassifyQueryArgs:
        messages = [
            {"role": "system", "content": load_prompt(self.ROLE)},
            {
                "role": "user",
                "content": (
                    f"User query:\n{query}\n\n"
                    + (f"Conversation/context:\n{context}\n\n" if context else "")
                    + "Classify this query and rewrite it standalone."
                ),
            },
        ]
        args = self.client.forced_tool(
            self.ROLE, self.registry.get("classify_query").decl(), messages
        )
        self.registry.dispatch(
            "classify_query", args, phase="P0", role=self.ROLE, forced=True
        )
        return args

    def clarify(self, classification: ClassifyQueryArgs) -> str:
        """Forced ask_user for the single clarifying question; returns the answer."""
        cq = classification.clarifying_question
        ask = AskUserArgs(
            question=cq.question if cq else "Which interpretation should research target?",
            options=cq.options if cq else [],
            allow_free_text=True,
        )
        answer = self.registry.dispatch(
            "ask_user", ask, phase="P0", role=self.ROLE, forced=True
        )
        return str(answer)

    def plan(
        self,
        standalone_query: str,
        clarification: str = "",
        feedback: str = "",
    ) -> CreateResearchPlanArgs:
        user = f"Standalone question:\n{standalone_query}\n"
        if clarification:
            user += f"\nUser clarification (binding):\n{clarification}\n"
        if feedback:
            user += f"\nUser feedback on the previous plan (binding):\n{feedback}\n"
        user += "\nProduce the research plan."
        messages = [
            {"role": "system", "content": load_prompt(self.ROLE)},
            {"role": "user", "content": user},
        ]
        args = self.client.forced_tool(
            self.ROLE, self.registry.get("create_research_plan").decl(), messages
        )
        self.registry.dispatch(
            "create_research_plan", args, phase="P1", role=self.ROLE, forced=True
        )
        return args
