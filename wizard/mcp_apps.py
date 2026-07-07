"""Wire RAVE's tool server into MCP-capable AI apps, verify with a handshake,
and (for claude.ai) expose it as a background service behind a public URL or
a cloudflared tunnel.

Every filesystem/process touch takes injectable paths and runners so the
whole module unit-tests offline. All wiring is idempotent: existing entries
are replaced, never duplicated.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

import yaml

SERVER_NAME = "rave"
HTTP_PORT = 8765


def server_command(repo: Path, python: str | None = None) -> list[str]:
    return [python or sys.executable, str(repo / "mcp_server.py")]


# ---------------------------------------------------------------------------
# Per-app config writers (all idempotent)
# ---------------------------------------------------------------------------

def _write_json_mcp(path: Path, cmd: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    servers = data.setdefault("mcpServers", {})
    servers[SERVER_NAME] = {"command": cmd[0], "args": cmd[1:]}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"wrote {path}"


def wire_claude_code(repo: Path, run: Callable, python: str | None = None) -> tuple[bool, str]:
    cmd = server_command(repo, python)
    code, out = run(
        ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--"] + cmd
    )
    if code == 0:
        return True, "registered via the claude command"
    return False, f"claude mcp add failed: {out.strip()[:200]}"


def wire_claude_desktop(repo: Path, config_dir: Path, python: str | None = None) -> tuple[bool, str]:
    return True, _write_json_mcp(
        config_dir / "claude_desktop_config.json", server_command(repo, python)
    )


def wire_gemini_cli(repo: Path, home: Path, python: str | None = None) -> tuple[bool, str]:
    return True, _write_json_mcp(
        home / ".gemini" / "settings.json", server_command(repo, python)
    )


def wire_qwen_code(repo: Path, home: Path, python: str | None = None) -> tuple[bool, str]:
    return True, _write_json_mcp(
        home / ".qwen" / "settings.json", server_command(repo, python)
    )


def wire_codex_cli(repo: Path, home: Path, python: str | None = None) -> tuple[bool, str]:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = server_command(repo, python)
    block = (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f'command = "{cmd[0]}"\n'
        f"args = {json.dumps(cmd[1:])}\n"
    )
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"[mcp_servers.{SERVER_NAME}]" in text:
        text = re.sub(
            rf"\n?\[mcp_servers\.{SERVER_NAME}\][^\[]*", block, text, count=1
        )
    else:
        text += block
    path.write_text(text, encoding="utf-8")
    return True, f"wrote {path}"


def wire_kimi_cli(repo: Path, home: Path, python: str | None = None) -> tuple[bool, str]:
    return True, _write_json_mcp(
        home / ".kimi" / "mcp.json", server_command(repo, python)
    )


def wire_goose(repo: Path, home: Path, python: str | None = None) -> tuple[bool, str]:
    path = home / ".config" / "goose" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
    cmd = server_command(repo, python)
    data.setdefault("extensions", {})[SERVER_NAME] = {
        "enabled": True, "type": "stdio", "cmd": cmd[0], "args": cmd[1:],
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return True, f"wrote {path}"


# ---------------------------------------------------------------------------
# Handshake: prove the app-facing server actually answers
# ---------------------------------------------------------------------------

def handshake(repo: Path, python: str | None = None, timeout: float = 30.0) -> bool:
    """Spawn the stdio server, send initialize, expect serverInfo name 'rave'."""
    cmd = server_command(repo, python)
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return False
    try:
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        proc.stdin.write(req + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        return resp.get("result", {}).get("serverInfo", {}).get("name") == SERVER_NAME
    except Exception:  # noqa: BLE001 — any failure means no handshake
        return False
    finally:
        proc.kill()


# ---------------------------------------------------------------------------
# Background service + public reachability (for claude.ai)
# ---------------------------------------------------------------------------

SYSTEMD_UNIT = """\
[Unit]
Description=RAVE research connector

[Service]
ExecStart={python} {server} --http --port {port}
Restart=on-failure
WorkingDirectory={repo}

[Install]
WantedBy=default.target
"""

LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.rave.connector</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>{server}</string>
    <string>--http</string><string>--port</string><string>{port}</string>
  </array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
"""


def install_service(
    repo: Path,
    os_name: str,
    run: Callable,
    home: Path,
    port: int = HTTP_PORT,
    python: str | None = None,
) -> tuple[bool, str]:
    """Keep the connector running across reboots. Best effort; always returns
    a human-readable message."""
    py = python or sys.executable
    server = str(repo / "mcp_server.py")
    if os_name in ("ubuntu", "linux"):
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "rave-mcp.service"
        unit.write_text(
            SYSTEMD_UNIT.format(python=py, server=server, repo=repo, port=port),
            encoding="utf-8",
        )
        code, out = run(["systemctl", "--user", "enable", "--now", "rave-mcp.service"])
        if code == 0:
            return True, "systemd user service rave-mcp is running"
        return False, f"service file written to {unit}; start it with: systemctl --user enable --now rave-mcp.service"
    if os_name == "mac":
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "com.rave.connector.plist"
        plist.write_text(
            LAUNCHD_PLIST.format(python=py, server=server, repo=repo, port=port),
            encoding="utf-8",
        )
        code, _ = run(["launchctl", "load", "-w", str(plist)])
        if code == 0:
            return True, "launchd service com.rave.connector is running"
        return False, f"service file written to {plist}; it loads at next login"
    if os_name == "windows":
        code, out = run([
            "schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "RAVE Connector",
            "/TR", f'"{py}" "{server}" --http --port {port}',
        ])
        if code == 0:
            return True, "scheduled task 'RAVE Connector' created"
        return False, "could not create the scheduled task; run mcp_server.py --http manually"
    return False, "unknown platform; run mcp_server.py --http manually"


_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel(
    run_stream: Callable[[list[str], float], str],
    port: int = HTTP_PORT,
) -> str | None:
    """Start a quick cloudflared tunnel; return its public URL or None.

    `run_stream(cmd, timeout)` starts the command detached and returns the
    output produced within the timeout (injectable for tests).
    """
    out = run_stream(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"], 30.0
    )
    m = _TUNNEL_URL_RE.search(out or "")
    return m.group(0) if m else None


def detect_public_ip(http_get: Callable[[str], object]) -> str | None:
    """VPS path: best-effort public address discovery."""
    try:
        resp = http_get("https://api.ipify.org")
        text = getattr(resp, "text", "").strip()
        if re.fullmatch(r"[0-9a-fA-F:.]+", text) and len(text) >= 7:
            return text
    except Exception:  # noqa: BLE001
        pass
    return None


CONNECT_DOC = """\
# Connect RAVE to claude.ai

Your RAVE URL (the internet address of your RAVE): **{url}**

1. Open claude.ai → Settings → Connectors → "+ Add custom connector".
2. Paste `{url}` → Add.
3. In any chat tap "+", switch RAVE on, and say
   "Use RAVE to research …".

The connector runs from this computer via `mcp_server.py --http --port {port}`.
If the URL stops answering, re-run `rave setup` to restart the service.
"""


def write_connect_doc(repo: Path, url: str, port: int = HTTP_PORT) -> Path:
    path = repo / "docs" / "CONNECT_CLAUDE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONNECT_DOC.format(url=url, port=port), encoding="utf-8")
    return path


CHATGPT_DOC = """\
# Use RAVE alongside ChatGPT

ChatGPT cannot call self-hosted tool servers directly today. Two options:

1. **Codex CLI** (installed by the wizard): it can call RAVE as a tool —
   ask it to "use the rave tool to research …".
2. **Copy-paste flow**: run `rave` in a terminal (the command window), then
   paste the finished report into your ChatGPT conversation.
"""


def write_chatgpt_doc(repo: Path) -> Path:
    path = repo / "docs" / "CONNECT_CHATGPT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CHATGPT_DOC, encoding="utf-8")
    return path
