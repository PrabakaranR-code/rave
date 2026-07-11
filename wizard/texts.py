"""Every wizard screen text, in dual language.

GOLDEN RULE: each technical term keeps its real name followed by a
plain-word bracket on first use per screen. The test suite lints every
screen in SCREENS against JARGON, so new screens added here stay honest.
Placeholders in {braces} are filled at display time.
"""
from __future__ import annotations

# Terms that must be explained in brackets on first use per screen.
JARGON = [
    "API key",
    "Docker",
    "MCP",
    "SearXNG",
    "SCOUT",
    "SEO",
    "base_url",
    "Ollama",
    "tunnel",
    "LLM",
    "cloudflared",
]

MENU_HINT = "Type the number of your choice and press Enter."
TEXT_HINT = "type and press Enter"

SCREENS: dict[str, str] = {
    "welcome": (
        "Welcome to RAVE — Research And Verify Engine.\n"
        "A few quick questions and you're ready. (Type E for expert setup.)"
    ),
    "scan_header": "Checking this computer…",
    "brain": (
        "Where should RAVE's thinking happen?\n"
        " [1] On this computer — a local LLM (an AI model running on your own"
        " machine): free and private\n"
        " [2] An online AI service via API key (your access key): smarter and"
        " faster, costs a few cents per question\n"
        " [3] Provider bundle — one API key (your access key) powers RAVE and"
        " also connects RAVE into that company's apps through MCP (the"
        " connection standard) [recommended for VPS/servers]"
    ),
    "local_menu_header": (
        "Best choice for your computer: {model} — {reason}\n"
        "Pick the AI model to use:"
    ),
    "too_heavy": (
        "This will run very slowly or may fail — continue?\n"
        " [1] Yes\n"
        " [2] Choose again"
    ),
    "ollama_install": (
        "RAVE needs Ollama (an app that runs AI models on your computer).\n"
        "I've opened the download page — ollama.com. Install it, then come"
        " back and press Enter."
    ),
    "model_pull": "Downloading {model} — this can take a few minutes…",
    "provider_menu": (
        "Which online AI service?\n"
        " [1] Anthropic (Claude)\n"
        " [2] OpenAI (ChatGPT models)\n"
        " [3] Google (Gemini)\n"
        " [4] DeepSeek\n"
        " [5] Kimi\n"
        " [6] Qwen\n"
        " [7] Mistral\n"
        " [8] Groq\n"
        " [9] Perplexity\n"
        " [10] Other — type its base_url (the service's address)"
    ),
    "other_base_url": (
        "Type the service's base_url (the service's address, e.g."
        " https://api.example.com/v1) and press Enter."
    ),
    "api_key": (
        "Paste your API key (your access key — typing stays hidden) and press"
        " Enter.\n"
        "This comes from the provider's developer website and is separate from"
        " any chat subscription. Need one? I've opened the page for you."
    ),
    "api_key_bad": "That key didn't work — check it and paste again.",
    "api_key_ok": "✓ Your key works.",
    "usage": (
        "How will you use RAVE?\n"
        " [1] Use RAVE on its own\n"
        " [2] Also inside AI apps on this computer via MCP (the standard that"
        " connects RAVE to AI apps) (found: {apps})\n"
        " [3] Also inside claude.ai online [recommended on VPS/servers]"
    ),
    "bundle": (
        "Which provider bundle? One API key (your access key) powers"
        " everything.\n"
        " [1] Anthropic — Claude Code + Claude Desktop + claude.ai online"
        " [recommended — only bundle reaching an online chat]\n"
        " [2] OpenAI — Codex CLI + ChatGPT guide saved to docs/\n"
        " [3] Google — Gemini CLI\n"
        " [4] Qwen — Qwen Code CLI\n"
        " [5] Kimi — Kimi CLI"
    ),
    "app_wired": "✓ {app} can now call RAVE.",
    "service_note": (
        "Setting up RAVE's connector as a background service (so RAVE stays"
        " reachable)…"
    ),
    "https_note": (
        "Heads up: claude.ai only accepts HTTPS (secure) addresses, so RAVE"
        " will use a tunnel (a private link from the internet to this"
        " computer) instead of a plain address."
    ),
    "tunnel_note": (
        "Creating a tunnel (a private link from the internet to this computer)"
        " with cloudflared (the tunnel helper program)…"
    ),
    "public_url": "Your RAVE URL (the internet address of your RAVE): {url}",
    "web_choice": (
        "How should RAVE search the web?\n"
        " [1] SearXNG (your private search server) via Docker (a helper"
        " program) [recommended] — adds Google + Bing reach and ~200 engines,"
        " and its engine adapters are community-maintained: when search sites"
        " change, volunteers fix it within days — you just update\n"
        " [2] SCOUT (built in) — works instantly, nothing to install; searches"
        " independent engines, science databases, Wikipedia and your trusted"
        " news feeds directly — skipping Google/Bing's ad- and SEO-driven"
        " (rankings-chasing) results. Maintained inside this project\n"
        " [3] Crawler (built in) — searches only websites you list, for special"
        " cases"
    ),
    "scout_chosen": (
        "✓ Using SCOUT (built-in search). Nothing to install."
    ),
    "scout_outlets": (
        "Optional: type news outlets you trust, separated by spaces (their"
        " results get a small boost). Press Enter to keep good defaults."
    ),
    "web_docker": "Starting SearXNG (your private search engine)… ",
    "web_docker_ok": "✓ SearXNG (your private search engine) is working.",
    "docker_missing": (
        "Docker (a helper program for the search engine) — not found or not"
        " running."
    ),
    "docker_retry": (
        " [1] Try again\n"
        " [2] Switch to SCOUT (the built-in search — works instantly, nothing"
        " to install)"
    ),
    "web_crawler": (
        "Using the built-in crawler (a simpler web reader). Type websites you"
        " trust, or press Enter for good defaults."
    ),
    "smoke": (
        "Let's test! Type any question and press Enter (or just press Enter"
        " for a sample)."
    ),
    "smoke_done": "✓ Done — your report is saved and now opening.",
    "done": (
        "RAVE is ready.\n"
        "Daily use: type rave · Change settings: rave setup · Guides are in"
        " the docs folder."
    ),
    "chat_ask": "What shall I research? Type and press Enter.",
    "chat_depth": (
        "How deep should I go?\n"
        " [1] Quick — about a minute, simple facts\n"
        " [2] Balanced — a few minutes, checks sources properly\n"
        " [3] Deep — longest, double-checks everything"
    ),
    "expert_header": (
        "Expert setup — compact checklist. Config file: {config_path}\n"
        "Fields: mode local|api · base_url (blank in local mode ="
        " http://localhost:11434/v1) · model · api_key_env (env var NAME; the"
        " key itself lives in .env) · search backend"
        " scout|metasearch|crawler|commercial (scout is the default).\n"
        "Non-interactive example: rave setup --expert --mode api --provider"
        " anthropic --model claude-sonnet-4-6 --search scout"
    ),
}

# Plain-language phase labels for progress display.
PHASE_LABELS = {
    "P0": "Planning",
    "P1": "Planning",
    "P2": "Searching",
    "P3": "Cross-checking",
    "P4": "Verifying",
    "P5": "Writing",
}


def screen(name: str, **kwargs: object) -> str:
    text = SCREENS[name]
    return text.format(**kwargs) if kwargs else text
