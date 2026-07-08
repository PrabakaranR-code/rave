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


def http_server(token):
    server = mcp_server.serve_http(0, host="127.0.0.1", token=token)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def test_serve_http_requires_bearer_token():
    server, base = http_server(token="s3cret")
    try:
        # health stays open, carries no data
        health = httpx.get(base)
        assert health.status_code == 200 and health.json()["server"] == "rave"
        # unauthenticated / wrong-token POSTs are rejected
        assert httpx.post(base, json={"id": 1, "method": "initialize"}).status_code == 401
        assert httpx.post(base, json={"id": 1, "method": "initialize"},
                          headers={"Authorization": "Bearer wrong"}).status_code == 401
        # bearer header works
        ok = httpx.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                        headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert ok.json()["result"]["serverInfo"]["name"] == "rave"
        assert ok.headers.get("Mcp-Session-Id")  # minted on initialize
        # token in the URL works (connector UIs that only take an address)
        ok2 = httpx.post(f"{base}/?token=s3cret",
                         json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert ok2.status_code == 200
        assert ok2.json()["result"]["tools"][0]["name"] == "deep_research"
    finally:
        server.shutdown()


def test_serve_http_with_no_token_rejects_all_posts():
    server, base = http_server(token="")
    try:
        resp = httpx.post(base, json={"id": 1, "method": "initialize"})
        assert resp.status_code == 401
        assert "RAVE_MCP_TOKEN" in resp.json()["hint"]
    finally:
        server.shutdown()


def test_serve_http_streamable_expectations():
    server, base = http_server(token="s3cret")
    auth = {"Authorization": "Bearer s3cret"}
    try:
        # wrong content type
        r = httpx.post(base, content=b"x=1", headers={
            **auth, "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415
        # unsatisfiable Accept
        r = httpx.post(base, json={"id": 1, "method": "tools/list"},
                       headers={**auth, "Accept": "text/html"})
        assert r.status_code == 406
        # streamable-http style Accept is fine; session id passes through
        r = httpx.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={**auth,
                                "Accept": "application/json, text/event-stream",
                                "Mcp-Session-Id": "sess-42"})
        assert r.status_code == 200 and r.headers["Mcp-Session-Id"] == "sess-42"
        # GET asking for a server-push stream is refused with Allow: POST
        r = httpx.get(base, headers={"Accept": "text/event-stream"})
        assert r.status_code == 405 and r.headers["Allow"] == "POST"
        # malformed JSON body
        r = httpx.post(base, content=b"not json",
                       headers={**auth, "Content-Type": "application/json"})
        assert r.status_code == 400
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


def test_connect_doc_with_tunnel_embeds_token_and_https_note(tmp_path):
    doc = mcp_apps.write_connect_doc(tmp_path, "https://x.trycloudflare.com", "tok123")
    text = doc.read_text()
    assert "https://x.trycloudflare.com/?token=tok123" in text
    assert "Add custom connector" in text
    assert "only accepts HTTPS" in text                # requirement stated plainly
    assert "internet address of your RAVE" in text     # dual language
    assert "tunnel (a private link" in text
    guide = mcp_apps.write_chatgpt_doc(tmp_path)
    assert guide.name == "CONNECT_CHATGPT.md" and guide.exists()


def test_connect_doc_without_tunnel_offers_caddy_never_plain_http(tmp_path):
    doc = mcp_apps.write_connect_doc(tmp_path, None, "tok123",
                                     public_ip="203.0.113.9")
    text = doc.read_text()
    assert "only accepts HTTPS" in text
    assert "caddy" in text and "reverse_proxy localhost:8765" in text
    assert "203.0.113.9" in text
    assert "https://your-domain.example/?token=tok123" in text
    assert "http://203.0.113.9" not in text  # plain-http connector never offered


def test_claude_ai_flow_vps_prefers_tunnel_over_plain_ip(tmp_path, monkeypatch):
    from tests.test_wizard_flows import make_fx
    from tests.test_wizard_scan import scan_fixture
    from wizard.wizard import claude_ai_flow

    monkeypatch.setenv("RAVE_MCP_TOKEN", "")
    monkeypatch.delenv("RAVE_MCP_TOKEN", raising=False)
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir(), home.mkdir()
    fx, printed, _, _ = make_fx(run_map={("systemctl",): (0, "")})
    fx.extra["run_stream"] = lambda cmd, timeout: (
        "INF https://vps-words.trycloudflare.com\n"
    )
    claude_ai_flow(fx, scan_fixture(headless=True, os_name="ubuntu"), repo, home)

    env_text = (repo / ".env").read_text()
    assert "RAVE_MCP_TOKEN=" in env_text
    token = env_text.split("RAVE_MCP_TOKEN=")[1].strip()
    doc_text = (repo / "docs" / "CONNECT_CLAUDE.md").read_text()
    assert f"https://vps-words.trycloudflare.com/?token={token}" in doc_text
    joined = "\n".join(printed)
    assert "only accepts HTTPS" in joined
    assert "http://203" not in joined and "http://vps" not in joined


def test_claude_ai_flow_tunnel_failure_falls_back_to_caddy_doc(tmp_path, monkeypatch):
    from tests.test_wizard_flows import make_fx
    from tests.test_wizard_scan import scan_fixture
    from wizard.wizard import claude_ai_flow

    monkeypatch.setenv("RAVE_MCP_TOKEN", "fixed-token")
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir(), home.mkdir()
    fx, printed, _, _ = make_fx(run_map={("systemctl",): (0, "")})
    fx.extra["run_stream"] = lambda cmd, timeout: "no tunnel today"
    ip = type("R", (), {"text": "203.0.113.9"})()
    fx.http_get = lambda url, timeout=3.0: ip
    claude_ai_flow(fx, scan_fixture(headless=True, os_name="ubuntu"), repo, home)

    doc_text = (repo / "docs" / "CONNECT_CLAUDE.md").read_text()
    assert "caddy" in doc_text and "fixed-token" in doc_text
    assert "203.0.113.9" in doc_text
    assert any("HTTPS address" in p for p in printed)
