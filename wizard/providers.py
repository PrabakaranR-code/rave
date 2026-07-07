"""One table holds every online provider: names, base_urls, key pages, and
numbered model menus. Updating a model name is a one-line change here.

All providers speak the OpenAI-compatible protocol except Anthropic, whose
base_url is auto-detected by the LLM client (see config.LLMConfig.protocol).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelChoice:
    id: str
    note: str  # plain-language tag shown in the menu


@dataclass(frozen=True)
class Provider:
    key: str
    label: str          # as shown in the provider menu
    base_url: str
    key_page: str       # where to create an API key
    models: tuple[ModelChoice, ...]


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        "anthropic", "Anthropic (Claude)",
        "https://api.anthropic.com",
        "https://console.anthropic.com/settings/keys",
        (
            ModelChoice("claude-haiku-4-5", "cheap & quick"),
            ModelChoice("claude-sonnet-4-6", "premium"),
            ModelChoice("claude-opus-4-8", "top-tier"),
        ),
    ),
    Provider(
        "openai", "OpenAI (ChatGPT models)",
        "https://api.openai.com/v1",
        "https://platform.openai.com/api-keys",
        (
            ModelChoice("gpt-4o-mini", "cheap & quick"),
            ModelChoice("gpt-4o", "premium"),
        ),
    ),
    Provider(
        "google", "Google (Gemini)",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://aistudio.google.com/apikey",
        (
            ModelChoice("gemini-2.5-flash", "cheap & quick"),
            ModelChoice("gemini-2.5-pro", "premium"),
        ),
    ),
    Provider(
        "deepseek", "DeepSeek",
        "https://api.deepseek.com/v1",
        "https://platform.deepseek.com/api_keys",
        (
            ModelChoice("deepseek-chat", "general"),
            ModelChoice("deepseek-reasoner", "deeper reasoning"),
        ),
    ),
    Provider(
        "kimi", "Kimi",
        "https://api.moonshot.ai/v1",
        "https://platform.moonshot.ai/console/api-keys",
        (ModelChoice("kimi-k2", "general"),),
    ),
    Provider(
        "qwen", "Qwen",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://bailian.console.alibabacloud.com/?apiKey=1",
        (
            ModelChoice("qwen-plus", "cheap & quick"),
            ModelChoice("qwen-max", "premium"),
        ),
    ),
    Provider(
        "mistral", "Mistral",
        "https://api.mistral.ai/v1",
        "https://console.mistral.ai/api-keys",
        (
            ModelChoice("mistral-small-latest", "cheap & quick"),
            ModelChoice("mistral-large-latest", "premium"),
        ),
    ),
    Provider(
        "groq", "Groq",
        "https://api.groq.com/openai/v1",
        "https://console.groq.com/keys",
        (ModelChoice("llama-3.3-70b-versatile", "fast"),),
    ),
    Provider(
        "perplexity", "Perplexity",
        "https://api.perplexity.ai",
        "https://www.perplexity.ai/settings/api",
        (
            ModelChoice("sonar", "cheap & quick"),
            ModelChoice("sonar-pro", "premium"),
        ),
    ),
)


def by_key(key: str) -> Provider:
    for p in PROVIDERS:
        if p.key == key:
            return p
    raise KeyError(f"unknown provider {key!r}")


# Bundles (step 2C): provider key → apps to wire, in order.
BUNDLES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-code", "claude-desktop", "claude-ai"),
    "openai": ("codex-cli", "chatgpt-guide"),
    "google": ("gemini-cli",),
    "qwen": ("qwen-code",),
    "kimi": ("kimi-cli",),
}
