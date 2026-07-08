"""The setup wizard: friendly path (steps 1–5) plus the expert layer.

Entry: `rave setup` / `python main.py setup`. Everything interactive flows
through wizard.ui.Effects, everything external (docker, installs, tunnels,
API pings) through injectable callables — the whole flow is testable with
scripted IO. Idempotent: re-running overwrites config cleanly and re-checks
the world after every Enter.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import httpx

from wizard import mcp_apps, websetup
from wizard.configwrite import ConfigChoices, write_config
from wizard.envfile import write_env
from wizard.providers import BUNDLES, PROVIDERS, Provider, by_key
from wizard.scan import (
    CURATED,
    SystemScan,
    best_choice,
    fits,
    probe_runtimes,
    render_scan,
    run_scan,
)
from wizard.smoketest import smoke_test
from wizard.texts import screen
from wizard.ui import Effects, ask_hidden, ask_text, enter_loop, menu, say

REPO_ROOT = Path(__file__).resolve().parent.parent

OLLAMA_PAGE = "https://ollama.com"
DOCKER_PAGE = "https://www.docker.com/products/docker-desktop/"
CLOUDFLARED_PAGE = (
    "https://developers.cloudflare.com/cloudflare-one/connections/"
    "connect-networks/downloads/"
)

APP_INSTALLS = {  # silent installs for headless machines (best effort)
    "claude-code": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
    "gemini-cli": ["npm", "install", "-g", "@google/gemini-cli"],
    "codex-cli": ["npm", "install", "-g", "@openai/codex"],
    "qwen-code": ["npm", "install", "-g", "@qwen-code/qwen-code"],
    "kimi-cli": [sys.executable, "-m", "pip", "install", "kimi-cli"],
}
APP_PAGES = {
    "claude-code": "https://claude.com/claude-code",
    "claude-desktop": "https://claude.ai/download",
    "gemini-cli": "https://github.com/google-gemini/gemini-cli",
    "codex-cli": "https://github.com/openai/codex",
    "qwen-code": "https://github.com/QwenLM/qwen-code",
    "kimi-cli": "https://github.com/MoonshotAI/kimi-cli",
}
APP_BINARIES = {
    "claude-code": "claude", "gemini-cli": "gemini", "codex-cli": "codex",
    "qwen-code": "qwen", "kimi-cli": "kimi",
}
APP_LABELS = {
    "claude-code": "Claude Code", "claude-desktop": "Claude Desktop",
    "gemini-cli": "Gemini CLI", "codex-cli": "Codex CLI",
    "qwen-code": "Qwen Code CLI", "kimi-cli": "Kimi CLI", "goose": "Goose",
}


# ---------------------------------------------------------------------------
# API key live test
# ---------------------------------------------------------------------------

def verify_api_key(
    base_url: str, model: str, key: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, str]:
    """One tiny live call; (ok, error snippet)."""
    try:
        with httpx.Client(transport=transport, timeout=30.0) as client:
            if "api.anthropic.com" in base_url:
                base = base_url.rstrip("/").removesuffix("/v1")
                resp = client.post(
                    f"{base}/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={"model": model, "max_tokens": 1,
                          "messages": [{"role": "user", "content": "ping"}]},
                )
            else:
                resp = client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "max_tokens": 1,
                          "messages": [{"role": "user", "content": "ping"}]},
                )
        if resp.status_code < 400:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
    except httpx.HTTPError as e:
        return False, str(e)


def _default_run_stream(cmd: list[str], timeout: float) -> str:
    """Start a long-running command; return output seen within timeout."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError as e:
        return str(e)
    import time as _time

    out, deadline = [], _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        out.append(line)
        if "trycloudflare.com" in line:
            break
    return "".join(out)


# ---------------------------------------------------------------------------
# Step 2A — local model
# ---------------------------------------------------------------------------

def flow_local(fx: Effects, scan: SystemScan) -> ConfigChoices | None:
    installed: list[tuple[str, float, str]] = []  # (model, need_gb, base_url)
    for rt in scan.runtimes:
        for m in rt.models:
            installed.append((m.name, m.need_gb, rt.base_url))
    installed_names = {n for n, _, _ in installed}
    curated = [c for c in CURATED if c.name not in installed_names]

    best, reason = best_choice(scan.ram_gb)
    entries: list[tuple[str, float, str | None]] = [
        (name, need, url) for name, need, url in installed
    ] + [(c.name, c.need_gb, None) for c in curated]

    lines = [screen("local_menu_header", model=best, reason=reason)]
    for i, (name, need, url) in enumerate(entries, 1):
        tag = " [already installed]" if url else ""
        mark = (
            "[recommended]" if fits(need, scan.ram_gb)
            else "[too heavy for this computer]"
        )
        lines.append(
            f" [{i}] {name}{tag} — needs about {need:.0f} GB of memory (RAM) {mark}"
        )
    while True:
        pick = menu(fx, "\n".join(lines), len(entries))
        name, need, url = entries[pick - 1]
        if fits(need, scan.ram_gb):
            break
        if menu(fx, screen("too_heavy"), 2) == 1:
            break
    if url is None:  # curated download → needs a local runtime
        url = _ensure_ollama_and_pull(fx, scan, name)
        if url is None:
            return None
    return ConfigChoices(llm_mode="local", base_url=url, model=name)


def _ollama_present(fx: Effects) -> str | None:
    for rt in probe_runtimes(fx.http_transport):
        if rt.key == "ollama":
            return rt.base_url
    return None


def _ensure_ollama_and_pull(fx: Effects, scan: SystemScan, model: str) -> str | None:
    url = _ollama_present(fx)
    if url is None:
        if scan.headless:
            say(fx, "Installing Ollama (an app that runs AI models on your computer)…")
            fx.run(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                   timeout=1800)
            fx.run(["sh", "-c", "(ollama serve >/dev/null 2>&1 &)"])
            fx.sleep(3.0)
            url = _ollama_present(fx)
        else:
            fx.open_url(OLLAMA_PAGE)
            enter_loop(
                fx, screen("ollama_install"),
                check=lambda: _ollama_present(fx) is not None,
            )
            url = _ollama_present(fx)
    if url is None:
        say(fx, "✗ Could not reach Ollama (the app that runs AI models). "
                "Install it and re-run: rave setup")
        return None
    say(fx, screen("model_pull", model=model))
    code, out = fx.run(["ollama", "pull", model], timeout=7200)
    if code != 0:
        say(fx, f"✗ The download failed: {out.strip()[:200]}")
        return None
    say(fx, f"✓ {model} is ready.")
    return url


# ---------------------------------------------------------------------------
# Step 2B — online service
# ---------------------------------------------------------------------------

def _pick_model(fx: Effects, provider: Provider) -> str:
    lines = [f"Which {provider.label} model?"]
    for i, m in enumerate(provider.models, 1):
        lines.append(f" [{i}] {m.id} — {m.note}")
    lines.append(f" [{len(provider.models) + 1}] type another name")
    pick = menu(fx, "\n".join(lines), len(provider.models) + 1)
    if pick <= len(provider.models):
        return provider.models[pick - 1].id
    return ask_text(fx, "Type the exact model name and press Enter.") or \
        provider.models[0].id


def _collect_key(fx: Effects, base_url: str, model: str, key_page: str,
                 repo: Path) -> str:
    fx.open_url(key_page)
    while True:
        key = ask_hidden(fx, screen("api_key"))
        ok, err = verify_api_key(base_url, model, key, fx.http_transport)
        if ok:
            say(fx, screen("api_key_ok"))
            write_env(repo / ".env", "RAVE_LLM_API_KEY", key)
            os.environ["RAVE_LLM_API_KEY"] = key
            return key
        say(fx, screen("api_key_bad") + (f" ({err})" if err else ""))


def flow_api(
    fx: Effects, scan: SystemScan, repo: Path,
    provider_preset: str | None = None,
) -> tuple[ConfigChoices, Provider | None]:
    if provider_preset:
        provider: Provider | None = by_key(provider_preset)
    else:
        pick = menu(fx, screen("provider_menu"), 10)
        provider = PROVIDERS[pick - 1] if pick <= 9 else None
    if provider is not None:
        base_url = provider.base_url
        model = _pick_model(fx, provider)
        key_page = provider.key_page
    else:  # Other
        while True:
            base_url = ask_text(fx, screen("other_base_url"))
            if base_url.startswith("http"):
                break
            say(fx, "That doesn't look like an address — it should start with https://")
        model = ask_text(fx, "Type the exact model name and press Enter.")
        key_page = base_url
    _collect_key(fx, base_url, model, key_page, repo)
    return ConfigChoices(llm_mode="api", base_url=base_url, model=model), provider


# ---------------------------------------------------------------------------
# MCP wiring, bundles, claude.ai reachability
# ---------------------------------------------------------------------------

def _wire_one(fx: Effects, app_key: str, scan: SystemScan, repo: Path,
              home: Path) -> None:
    if app_key == "claude-code":
        ok, msg = mcp_apps.wire_claude_code(repo, fx.run)
    elif app_key == "claude-desktop":
        from wizard.scan import _claude_desktop_dir

        ok, msg = mcp_apps.wire_claude_desktop(
            repo, _claude_desktop_dir(scan.os_name, home)
        )
    elif app_key == "gemini-cli":
        ok, msg = mcp_apps.wire_gemini_cli(repo, home)
    elif app_key == "codex-cli":
        ok, msg = mcp_apps.wire_codex_cli(repo, home)
    elif app_key == "qwen-code":
        ok, msg = mcp_apps.wire_qwen_code(repo, home)
    elif app_key == "kimi-cli":
        ok, msg = mcp_apps.wire_kimi_cli(repo, home)
    elif app_key == "goose":
        ok, msg = mcp_apps.wire_goose(repo, home)
    else:
        return
    label = APP_LABELS.get(app_key, app_key)
    if ok:
        say(fx, screen("app_wired", app=label))
    else:
        say(fx, f"✗ {label}: {msg}")


def _ensure_app(fx: Effects, app_key: str, scan: SystemScan) -> bool:
    binary = APP_BINARIES.get(app_key)
    import shutil as _shutil

    if binary and _shutil.which(binary):
        return True
    if app_key == "claude-desktop":
        if scan.headless:
            say(fx, "Skipping Claude Desktop — it needs a screen, and this is a server.")
            return False
        fx.open_url(APP_PAGES[app_key])
        return enter_loop(
            fx, "I've opened the Claude Desktop download page. Install it, then"
                " come back and press Enter.",
            check=lambda: True,
        )
    if scan.headless and app_key in APP_INSTALLS:
        say(fx, f"Installing {APP_LABELS.get(app_key, app_key)}…")
        code, _ = fx.run(APP_INSTALLS[app_key], timeout=900)
        return code == 0 or bool(binary and _shutil.which(binary))
    fx.open_url(APP_PAGES.get(app_key, "https://example.com"))
    return enter_loop(
        fx, f"I've opened the {APP_LABELS.get(app_key, app_key)} page. Install"
            " it, then come back and press Enter.",
        check=lambda: bool(binary and _shutil.which(binary)),
        max_tries=3,
    )


def claude_ai_flow(fx: Effects, scan: SystemScan, repo: Path, home: Path) -> None:
    # The connector token: generated once, stored only in .env (chmod 600).
    import secrets as _secrets

    token = os.environ.get("RAVE_MCP_TOKEN") or _secrets.token_urlsafe(24)
    write_env(repo / ".env", "RAVE_MCP_TOKEN", token)
    os.environ["RAVE_MCP_TOKEN"] = token

    say(fx, screen("service_note"))
    ok, msg = mcp_apps.install_service(repo, scan.os_name, fx.run, home)
    say(fx, ("✓ " if ok else "• ") + msg)

    # claude.ai requires HTTPS — a tunnel on every platform, VPS included;
    # a plain http://ip:port connector would be rejected.
    say(fx, screen("https_note"))
    import shutil as _shutil

    if not _shutil.which("cloudflared"):
        if scan.headless:
            fx.run(["sh", "-c",
                    "curl -fsSL -o /usr/local/bin/cloudflared "
                    "https://github.com/cloudflare/cloudflared/releases/"
                    "latest/download/cloudflared-linux-amd64 "
                    "&& chmod +x /usr/local/bin/cloudflared"], timeout=600)
        else:
            fx.open_url(CLOUDFLARED_PAGE)
            enter_loop(
                fx, "RAVE needs cloudflared (the tunnel helper program) to"
                    " create a tunnel (a private link from the internet to"
                    " this computer). I've opened its download page —"
                    " install it, then press Enter.",
                check=lambda: bool(_shutil.which("cloudflared")),
                max_tries=3,
            )
    say(fx, screen("tunnel_note"))
    run_stream = fx.extra.get("run_stream", _default_run_stream)
    url = mcp_apps.start_tunnel(run_stream)

    public_ip = mcp_apps.detect_public_ip(fx.http_get) if scan.headless else None
    doc = mcp_apps.write_connect_doc(repo, url, token, public_ip=public_ip)
    if url:
        say(fx, screen("public_url", url=f"{url.rstrip('/')}/?token={token}"))
        say(fx, doc.read_text(encoding="utf-8"))
    else:
        say(fx, "• Couldn't create the tunnel right now. claude.ai needs an"
                f" HTTPS address, so I saved two alternatives in {doc} —"
                " re-run rave setup any time to retry.")


def usage_followup(fx: Effects, scan: SystemScan, repo: Path, home: Path) -> None:
    found = scan.found_apps
    apps_text = ", ".join(a.label for a in found) if found else "none yet"
    pick = menu(fx, screen("usage", apps=apps_text), 3)
    if pick == 2:
        if not found:
            say(fx, "No AI apps found on this computer — skipping.")
        for app in found:
            _wire_one(fx, app.key, scan, repo, home)
        if found and mcp_apps.handshake(repo):
            say(fx, "✓ Connection test passed — the apps can reach RAVE.")
    elif pick == 3:
        claude_ai_flow(fx, scan, repo, home)


def flow_bundle(fx: Effects, scan: SystemScan, repo: Path, home: Path) -> ConfigChoices:
    order = ("anthropic", "openai", "google", "qwen", "kimi")
    pick = menu(fx, screen("bundle"), 5)
    key = order[pick - 1]
    choices, _provider = flow_api(fx, scan, repo, provider_preset=key)
    for app in BUNDLES[key]:
        if app == "claude-ai":
            claude_ai_flow(fx, scan, repo, home)
        elif app == "chatgpt-guide":
            path = mcp_apps.write_chatgpt_doc(repo)
            say(fx, f"✓ ChatGPT guide saved to {path}")
        else:
            if _ensure_app(fx, app, scan):
                _wire_one(fx, app, scan, repo, home)
    if mcp_apps.handshake(repo):
        say(fx, "✓ Connection test passed — the apps can reach RAVE.")
    return choices


# ---------------------------------------------------------------------------
# Step 3 — web reach
# ---------------------------------------------------------------------------

def _docker_ready(fx: Effects) -> bool:
    return fx.run(["docker", "info"], timeout=20)[0] == 0


def ensure_docker(fx: Effects, scan: SystemScan) -> bool:
    if scan.docker or _docker_ready(fx):
        return True
    say(fx, screen("docker_missing"))
    if scan.headless:
        say(fx, "Installing Docker (a helper program for the search engine)…")
        fx.run(["sh", "-c",
                "apt-get update -y && apt-get install -y docker.io || "
                "sudo apt-get update -y && sudo apt-get install -y docker.io"],
               timeout=1800)
        return _docker_ready(fx)
    fx.open_url(DOCKER_PAGE)
    for attempt in range(2):
        if enter_loop(
            fx, "I've opened the Docker (a helper program for the search"
                " engine) download page. Install and start it, then press Enter.",
            check=lambda: _docker_ready(fx), max_tries=1,
        ):
            return True
        if attempt == 0 and menu(fx, screen("docker_retry"), 2) == 2:
            return False
    return False


def flow_web(fx: Effects, scan: SystemScan, repo: Path, choices: ConfigChoices) -> None:
    if ensure_docker(fx, scan):
        say(fx, screen("web_docker"))
        url = websetup.start_searxng(repo, fx.run, fx.http_get, fx.sleep)
        if url:
            say(fx, screen("web_docker_ok"))
            choices.search_backend = "metasearch"
            choices.metasearch_url = url
            return
        say(fx, "• The search engine didn't start — falling back to the"
                " built-in crawler (a simpler web reader).")
    raw = ask_text(fx, screen("web_crawler"))
    choices.search_backend = "crawler"
    choices.crawler_domains = websetup.parse_trusted_domains(raw)


# ---------------------------------------------------------------------------
# Expert layer
# ---------------------------------------------------------------------------

def expert_setup(fx: Effects, args: argparse.Namespace, repo: Path,
                 scan: SystemScan | None) -> int:
    config_path = repo / "config.yaml"
    if args.mode or args.provider or args.model:  # non-interactive
        choices = ConfigChoices()
        if args.provider:
            p = by_key(args.provider)
            choices.llm_mode = "api"
            choices.base_url = args.base_url or p.base_url
            choices.model = args.model or p.models[0].id
        else:
            choices.llm_mode = args.mode or "local"
            choices.base_url = args.base_url or ""
            if args.model:
                choices.model = args.model
        if choices.llm_mode == "api" and not choices.base_url:
            say(fx, "--mode api needs --provider or --base-url")
            return 2
        choices.search_backend = args.search or "crawler"
        if choices.search_backend == "metasearch":
            choices.metasearch_url = args.metasearch_url or websetup.searxng_url()
        elif choices.search_backend == "crawler":
            choices.crawler_domains = (
                websetup.parse_trusted_domains(args.domains or "")
            )
        write_config(config_path, choices)
        say(fx, f"✓ wrote {config_path} (mode={choices.llm_mode},"
                f" model={choices.model}, search={choices.search_backend})")
        if choices.llm_mode == "api":
            say(fx, "Put the key in .env as RAVE_LLM_API_KEY=<key> (chmod 600).")
        return 0
    say(fx, screen("expert_header", config_path=config_path))
    for line in render_scan(scan) if scan else []:
        say(fx, line)
    say(fx, "Daily pipeline: python main.py \"question\" --mode"
            " speed|balanced|quality [--confirm] [--out report.md] ·"
            " tool server: python mcp_server.py [--http --port 8765]")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rave setup", add_help=True)
    p.add_argument("--expert", action="store_true")
    p.add_argument("--mode", choices=["local", "api"])
    p.add_argument("--provider", choices=[pr.key for pr in PROVIDERS])
    p.add_argument("--model")
    p.add_argument("--base-url", dest="base_url")
    p.add_argument("--search", choices=["metasearch", "crawler", "commercial"])
    p.add_argument("--metasearch-url", dest="metasearch_url")
    p.add_argument("--domains", help="comma/space separated trusted domains")
    return p


def run_setup(
    argv: list[str] | None = None,
    fx: Effects | None = None,
    scan: SystemScan | None = None,
    repo: Path = REPO_ROOT,
    home: Path | None = None,
    orchestrator_factory=None,
) -> int:
    fx = fx or Effects()
    home = home or Path.home()
    args = build_parser().parse_args(argv or [])

    if args.expert:
        s = scan if (scan or args.mode or args.provider or args.model) else run_scan(fx.http_transport)
        return expert_setup(fx, args, repo, s)

    # Step 1 — welcome (E = expert)
    choice = menu(fx, screen("welcome") + "\n [1] Start setup",
                  1, letters={"E": -1})
    scan = scan or run_scan(fx.http_transport)
    if choice == -1:
        return expert_setup(fx, argparse.Namespace(
            mode=None, provider=None, model=None, base_url=None,
            search=None, metasearch_url=None, domains=None), repo, scan)

    # System scan (live list)
    say(fx, screen("scan_header"))
    for line in render_scan(scan):
        say(fx, line)

    # Step 2 — brain
    brain = menu(fx, screen("brain"), 3)
    if brain == 1:
        choices = flow_local(fx, scan)
        if choices is None:
            return 1
    elif brain == 2:
        choices, _provider = flow_api(fx, scan, repo)
        usage_followup(fx, scan, repo, home)
    else:
        choices = flow_bundle(fx, scan, repo, home)

    # Step 3 — web reach
    flow_web(fx, scan, repo, choices)
    write_config(repo / "config.yaml", choices)

    # Step 4 — smoke test
    smoke_test(fx, str(repo / "config.yaml"), home=home,
               orchestrator_factory=orchestrator_factory)

    # Step 5 — done
    say(fx, screen("done"))
    return 0
