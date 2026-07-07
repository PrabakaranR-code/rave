"""MCP wiring: per-app config writers (idempotent), stdio handshake, HTTP
serving, tunnel URL parsing, background service files, connector docs."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import yaml

import mcp_server
from wizard import mcp_apps

REPO = Path(__file__).resolve().parent.parent


def test_wire_gemini_and_qwen_are_idempotent(tmp_path):
    for _ in range(2):
        ok, msg = mcp_apps.wire_gemini_cli(REPO, tmp_path)
        assert ok
    data = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert list(data["mcpServers"]) == ["rave"]
    assert data["mcpServers"]["rave"]["args"][-1].endswith("mcp_server.py")

    mcp_apps.wire_qwen_code(REPO, tmp_path)
    data = json.loads((tmp_path / ".qwen" / "settings.json").read_text())
    assert "rave" in data["mcpServers"]


def test_wire_claude_desktop_and_kimi(tmp_path):
    ok, _ = mcp_apps.wire_claude_desktop(REPO, tmp_path / "Claude")
    assert ok
    data = json.loads((tmp_path / "Claude" / "claude_desktop_config.json").read_text())
    assert "rave" in data["mcpServers"]
    ok, _ = mcp_apps.wire_kimi_cli(REPO, tmp_path)
    assert ok and (tmp_path / ".kimi" / "mcp.json").exists()


def test_wire_codex_toml_idempotent(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-4o"\n')
    mcp_apps.wire_codex_cli(REPO, tmp_path)
    mcp_apps.wire_codex_cli(REPO, tmp_path)
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert text.count("[mcp_servers.rave]") == 1
    assert 'model = "gpt-4o"' in text  # existing content preserved


def test_wire_goose_yaml(tmp_path):
    mcp_apps.wire_goose(REPO, tmp_path)
    data = yaml.safe_load((tmp_path / ".config" / "goose" / "config.yaml").read_text())
    assert data["extensions"]["rave"]["type"] == "stdio"


def test_wire_claude_code_uses_cli(tmp_path):
    calls = []

    def run(cmd, timeout=None):
        calls.append(cmd)
        return (0, "added")

    ok, msg = mcp_apps.wire_claude_code(REPO, run)
    assert ok
    assert calls[0][:4] == ["claude", "mcp", "add", "--scope"]
    ok, msg = mcp_apps.wire_claude_code(REPO, lambda c, timeout=None: (1, "boom"))
    assert not ok and "boom" in msg


def test_stdio_handshake_against_real_server():
    assert mcp_apps.handshake(REPO) is True


def test_handshake_fails_gracefully_for_missing_server(tmp_path):
    assert mcp_apps.handshake(tmp_path) is False


def test_serve_http_initialize_and_health():
    server = mcp_server.serve_http(0, host="127.0.0.1")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        health = httpx.get(base)
        assert health.status_code == 200 and health.json()["server"] == "rave"
        resp = httpx.post(base, json={"jsonrpc": "2.0", "id": 1,
                                      "method": "initialize"})
        assert resp.json()["result"]["serverInfo"]["name"] == "rave"
        bad = httpx.post(base, content=b"not json")
        assert bad.status_code == 400
    finally:
        server.shutdown()


def test_start_tunnel_parses_public_url():
    out = ("2026 INF Starting tunnel\n"
           "2026 INF +--------------------------------------+\n"
           "2026 INF https://random-words-here.trycloudflare.com\n")
    url = mcp_apps.start_tunnel(lambda cmd, timeout: out)
    assert url == "https://random-words-here.trycloudflare.com"
    assert mcp_apps.start_tunnel(lambda cmd, timeout: "no url") is None


def test_install_service_linux_writes_unit(tmp_path):
    calls = []

    def run(cmd, timeout=None):
        calls.append(cmd)
        return (0, "")

    ok, msg = mcp_apps.install_service(REPO, "ubuntu", run, tmp_path)
    assert ok and "rave-mcp" in msg
    unit = tmp_path / ".config" / "systemd" / "user" / "rave-mcp.service"
    text = unit.read_text()
    assert "--http" in text and "mcp_server.py" in text
    assert calls[0][:2] == ["systemctl", "--user"]


def test_install_service_mac_writes_plist(tmp_path):
    ok, msg = mcp_apps.install_service(
        REPO, "mac", lambda c, timeout=None: (1, ""), tmp_path
    )
    plist = tmp_path / "Library" / "LaunchAgents" / "com.rave.connector.plist"
    assert plist.exists() and not ok  # file written even when load fails
    assert "next login" in msg


def test_detect_public_ip():
    good = type("R", (), {"text": "203.0.113.9"})()
    assert mcp_apps.detect_public_ip(lambda u: good) == "203.0.113.9"
    bad = type("R", (), {"text": "<html>nope</html>"})()
    assert mcp_apps.detect_public_ip(lambda u: bad) is None


def test_connect_docs_generated(tmp_path):
    doc = mcp_apps.write_connect_doc(tmp_path, "https://x.trycloudflare.com")
    text = doc.read_text()
    assert "https://x.trycloudflare.com" in text
    assert "Add custom connector" in text
    assert "internet address of your RAVE" in text  # dual language
    guide = mcp_apps.write_chatgpt_doc(tmp_path)
    assert guide.name == "CONNECT_CHATGPT.md" and guide.exists()
