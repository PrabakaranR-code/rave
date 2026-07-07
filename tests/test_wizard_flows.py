"""Wizard menu logic with fully scripted IO: local flow, API flow with key
retry, expert layer, the whole wizard end-to-end, chat mode, main routing,
and web-reach setup."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

import main as main_mod
from config import load_config
from tests.test_wizard_scan import ports_transport, scan_fixture
from wizard import websetup
from wizard.ui import Effects
from wizard.wizard import (
    flow_api,
    flow_local,
    run_setup,
    verify_api_key,
)

GOOD_KEY = "sk-good-key"


def make_fx(inputs=(), hidden=(), run_map=None, transport=None):
    """Scripted Effects: returns (fx, printed, opened_urls, commands_run)."""
    printed: list[str] = []
    opened: list[str] = []
    ran: list[list[str]] = []
    it, hid = iter(inputs), iter(hidden)

    def run(cmd, timeout=None, **_kw):
        ran.append(list(cmd))
        for prefix, result in (run_map or {}).items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return (1, "not scripted")

    fx = Effects(
        input=lambda p: next(it),
        print=printed.append,
        getpass=lambda p: next(hid),
        open_url=opened.append,
        run=run,
        sleep=lambda s: None,
        http_transport=transport,
    )
    return fx, printed, opened, ran


def api_transport():
    """LLM endpoints: GOOD_KEY passes the live test, anything else 401s."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-api-key") or \
            request.headers.get("authorization", "").removeprefix("Bearer ")
        if key == GOOD_KEY:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"error": "bad key"})

    return httpx.MockTransport(handler)


class FakeResult:
    def __init__(self, out_path: Path):
        self.markdown = "# fake report\n\nA finding."
        self.report_path = out_path


def fake_factory(record: dict):
    def factory(**kw):
        record.update(kw)

        class FakeOrch:
            def run(self, question, context=""):
                record["question"] = question
                kw["out_path"].write_text("# fake report", encoding="utf-8")
                return FakeResult(kw["out_path"])

        return FakeOrch()

    return factory


# --- step 2A: local ----------------------------------------------------------

def test_flow_local_picks_installed_model():
    fx, printed, _, _ = make_fx(inputs=["1"])
    choices = flow_local(fx, scan_fixture())
    assert choices.llm_mode == "local"
    assert choices.model == "llama3.1:8b"
    assert choices.base_url == "http://localhost:11434/v1"  # auto, never typed
    header = "\n".join(printed)
    assert "[already installed]" in header
    assert "needs about" in header and "GB of memory (RAM)" in header
    assert "Best choice for your computer:" in header


def test_flow_local_too_heavy_reasks_then_downloads():
    # 4 GB machine: everything is too heavy; user insists on a curated model.
    scan = scan_fixture(ram_gb=4.0)
    transport = ports_transport({
        11434: {"models": [{"name": "llama3.1:8b", "size": 4_900_000_000}]},
    })
    fx, printed, _, ran = make_fx(
        inputs=["2", "2", "2", "1"],  # pick curated → choose again → pick → yes
        run_map={("ollama", "pull"): (0, "pulled")},
        transport=transport,
    )
    choices = flow_local(fx, scan)
    assert choices is not None and choices.model == "qwen2.5:7b"
    assert ["ollama", "pull", "qwen2.5:7b"] in ran
    joined = "\n".join(printed)
    assert "[too heavy for this computer]" in joined
    assert "very slowly or may fail" in joined


def test_flow_local_no_runtime_opens_download_page_and_rechecks():
    scan = scan_fixture(runtimes=[], headless=False)
    state = {"installed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        # Ollama appears only after the user installs it (first probe fails).
        if request.url.port == 11434 and state["installed"]:
            return httpx.Response(200, json={"models": []})
        raise httpx.ConnectError("refused", request=request)

    def enter(prompt: str) -> str:
        state["installed"] = True  # the user installed it, then pressed Enter
        return ""

    fx, printed, opened, ran = make_fx(
        run_map={("ollama", "pull"): (0, "pulled")},
        transport=httpx.MockTransport(handler),
    )
    seq = iter(["1"])  # pick the first curated model; later Enters install

    def scripted_input(prompt: str) -> str:
        try:
            return next(seq)
        except StopIteration:
            return enter(prompt)

    fx.input = scripted_input
    choices = flow_local(fx, scan)
    assert choices is not None and choices.model == "qwen2.5:7b"
    assert "https://ollama.com" in opened
    assert any("Ollama (an app that runs AI models" in p for p in printed)


# --- step 2B: online service -------------------------------------------------

def test_api_key_live_test_openai_and_anthropic_shapes():
    ok, _ = verify_api_key("https://api.anthropic.com", "m", GOOD_KEY, api_transport())
    assert ok
    ok, err = verify_api_key("https://api.openai.com/v1", "m", "bad", api_transport())
    assert not ok and "401" in err


def test_flow_api_key_retry_then_success(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVE_LLM_API_KEY", "placeholder")
    fx, printed, opened, _ = make_fx(
        inputs=["1", "2"],                 # Anthropic → claude-sonnet-4-6
        hidden=["bad-key", GOOD_KEY],      # first paste fails the live test
        transport=api_transport(),
    )
    choices, provider = flow_api(fx, scan_fixture(), tmp_path)
    assert choices.llm_mode == "api"
    assert choices.base_url == "https://api.anthropic.com"
    assert choices.model == "claude-sonnet-4-6"
    assert provider.key == "anthropic"
    assert "https://console.anthropic.com/settings/keys" in opened
    joined = "\n".join(printed)
    assert "That key didn't work" in joined and "✓ Your key works." in joined
    env_text = (tmp_path / ".env").read_text()
    assert f"RAVE_LLM_API_KEY={GOOD_KEY}" in env_text
    assert GOOD_KEY not in (tmp_path / ".env").name  # sanity


def test_flow_api_other_provider_types_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVE_LLM_API_KEY", "placeholder")
    fx, printed, _, _ = make_fx(
        inputs=["10", "not-a-url", "https://api.custom.test/v1", "my-model"],
        hidden=[GOOD_KEY],
        transport=api_transport(),
    )
    choices, provider = flow_api(fx, scan_fixture(), tmp_path)
    assert provider is None
    assert choices.base_url == "https://api.custom.test/v1"
    assert choices.model == "my-model"
    assert any("start with https://" in p for p in printed)


# --- expert layer -------------------------------------------------------------

def test_expert_non_interactive_flags(tmp_path):
    fx, printed, _, _ = make_fx()
    rc = run_setup(
        ["--expert", "--mode", "api", "--provider", "anthropic",
         "--model", "claude-sonnet-4-6", "--search", "metasearch"],
        fx=fx, repo=tmp_path,
    )
    assert rc == 0
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.llm.mode == "api"
    assert cfg.llm.base_url == "https://api.anthropic.com"
    assert cfg.llm.model == "claude-sonnet-4-6"
    assert cfg.search.backend == "metasearch"
    assert cfg.search.metasearch_url == "http://localhost:8080"
    assert any(".env" in p for p in printed)  # key placement instruction


def test_expert_local_flags_and_domains(tmp_path):
    fx, _, _, _ = make_fx()
    rc = run_setup(
        ["--expert", "--mode", "local", "--model", "qwen2.5:7b",
         "--search", "crawler", "--domains", "a.test, b.test"],
        fx=fx, repo=tmp_path,
    )
    assert rc == 0
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.llm.mode == "local" and cfg.llm.model == "qwen2.5:7b"
    assert cfg.search.crawler.domains == ["a.test", "b.test"]


def test_expert_checklist_prints_scan_and_pipeline(tmp_path):
    fx, printed, _, _ = make_fx()
    rc = run_setup(["--expert"], fx=fx, scan=scan_fixture(), repo=tmp_path)
    assert rc == 0
    joined = "\n".join(printed)
    assert "config.yaml" in joined
    assert "mode local|api" in joined
    assert "--mode" in joined and "speed|balanced|quality" in joined
    assert "mcp_server.py" in joined


def test_welcome_letter_e_jumps_to_expert(tmp_path):
    fx, printed, _, _ = make_fx(inputs=["E"])
    rc = run_setup([], fx=fx, scan=scan_fixture(), repo=tmp_path)
    assert rc == 0
    assert any("Expert setup" in p for p in printed)


# --- full wizard end-to-end (local brain, crawler fallback, smoke test) -------

def test_full_wizard_local_crawler_smoke(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir(), home.mkdir()
    record: dict = {}
    fx, printed, opened, _ = make_fx(
        # welcome → local brain → first model → Docker enter-check →
        # fall back to crawler → default domains → sample smoke question
        inputs=["1", "1", "1", "", "2", "", ""],
    )
    rc = run_setup([], fx=fx, scan=scan_fixture(), repo=repo, home=home,
                   orchestrator_factory=fake_factory(record))
    assert rc == 0
    cfg = load_config(repo / "config.yaml")
    assert cfg.llm.mode == "local" and cfg.llm.model == "llama3.1:8b"
    assert cfg.search.backend == "crawler"
    assert cfg.search.crawler.domains == websetup.DEFAULT_TRUSTED
    joined = "\n".join(printed)
    assert "Checking this computer…" in joined
    assert "Docker (a helper program for the search engine)" in joined
    assert "✓ Done — your report is saved and now opening." in joined
    assert "RAVE is ready." in joined
    assert record["mode"] == "speed"  # smoke test runs in speed mode
    assert (home / "Documents" / "RAVE").exists()
    assert any(u.startswith("file://") for u in opened)  # report opened


def test_wizard_docker_path_starts_searxng(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir(), home.mkdir()
    searx = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": []})
    )
    record: dict = {}
    fx, printed, _, ran = make_fx(
        inputs=["1", "1", "1", ""],  # welcome, local, model, smoke Enter
        run_map={("docker",): (0, "ok")},
        transport=searx,
    )
    rc = run_setup([], fx=fx, scan=scan_fixture(docker=True), repo=repo,
                   home=home, orchestrator_factory=fake_factory(record))
    assert rc == 0
    cfg = load_config(repo / "config.yaml")
    assert cfg.search.backend == "metasearch"
    assert cfg.search.metasearch_url == "http://localhost:8080"
    assert any(c[:2] == ["docker", "run"] for c in ran)
    assert "✓ SearXNG (your private search engine) is working." in printed


# --- web setup unit ------------------------------------------------------------

def test_parse_trusted_domains():
    assert websetup.parse_trusted_domains("") == websetup.DEFAULT_TRUSTED
    got = websetup.parse_trusted_domains("https://a.com, b.org  a.com junk")
    assert got == ["a.com", "b.org"]


def test_start_searxng_writes_settings_and_polls(tmp_path):
    calls = []

    def run(cmd, timeout=None):
        calls.append(cmd)
        return (0, "cid")

    resp = type("R", (), {"status_code": 200})()
    url = websetup.start_searxng(tmp_path, run, lambda u: resp, lambda s: None)
    assert url == "http://localhost:8080"
    settings = tmp_path / ".rave" / "searxng" / "settings.yml"
    assert "json" in settings.read_text()
    assert any(c[:2] == ["docker", "run"] for c in calls)


def test_start_searxng_returns_none_when_docker_fails(tmp_path):
    url = websetup.start_searxng(
        tmp_path, lambda cmd, timeout=None: (1, "no docker"),
        lambda u: None, lambda s: None,
    )
    assert url is None


# --- chat mode + main routing ---------------------------------------------------

def test_chat_mode_runs_and_saves(tmp_path, capsys):
    from wizard.chat import run_chat

    record: dict = {}
    fx, printed, opened, _ = make_fx(inputs=["why is the sky blue?", "2"])
    rc = run_chat(fx=fx, home=tmp_path, orchestrator_factory=fake_factory(record))
    assert rc == 0
    assert record["question"] == "why is the sky blue?"
    assert record["mode"] == "balanced"
    assert "# fake report" in capsys.readouterr().out
    assert any("Report saved" in p for p in printed)
    assert str(record["out_path"]).startswith(str(tmp_path / "Documents" / "RAVE"))


def test_chat_mode_empty_question_exits_cleanly(tmp_path):
    from wizard.chat import run_chat

    fx, printed, _, _ = make_fx(inputs=[""])
    assert run_chat(fx=fx, home=tmp_path) == 0
    assert any("Nothing to research" in p for p in printed)


def test_main_routes_setup_subcommand(monkeypatch):
    import wizard.wizard as wizard_mod

    seen = {}
    monkeypatch.setattr(wizard_mod, "run_setup",
                        lambda argv: seen.update(argv=argv) or 0)
    assert main_mod.main(["setup", "--expert"]) == 0
    assert seen["argv"] == ["--expert"]


def test_main_routes_bare_invocation_to_chat(monkeypatch):
    import wizard.chat as chat_mod

    monkeypatch.setattr(chat_mod, "run_chat", lambda: 0)
    assert main_mod.main([]) == 0


def test_main_research_path_unchanged(monkeypatch, capsys):
    from agents.orchestrator import HALT_MESSAGE, HaltError

    class FakeOrch:
        def __init__(self, *a, **k):
            pass

        def run(self, q, context=""):
            raise HaltError(HALT_MESSAGE)

    monkeypatch.setattr(main_mod, "Orchestrator", FakeOrch)
    assert main_mod.main(["a question"]) == 2
    assert "HALT" in capsys.readouterr().err
