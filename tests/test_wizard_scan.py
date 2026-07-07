"""System scan: runtime probes (mocked ports), fit rule, app detection."""
from __future__ import annotations

from pathlib import Path

import httpx

from wizard.scan import (
    CURATED,
    AppInfo,
    ModelInfo,
    RuntimeInfo,
    SystemScan,
    best_choice,
    detect_mcp_apps,
    detect_os,
    detect_ram_gb,
    estimate_need_gb,
    fits,
    is_headless,
    probe_runtimes,
    render_scan,
)


def ports_transport(open_ports: dict[int, dict]) -> httpx.MockTransport:
    """Simulate localhost: listed ports answer with the given JSON; others refuse."""

    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        if port in open_ports:
            return httpx.Response(200, json=open_ports[port])
        raise httpx.ConnectError("refused", request=request)

    return httpx.MockTransport(handler)


def test_probe_finds_ollama_with_model_sizes():
    transport = ports_transport({
        11434: {"models": [{"name": "llama3.1:8b", "size": 4_900_000_000}]},
    })
    found = probe_runtimes(transport)
    assert [r.key for r in found] == ["ollama"]
    m = found[0].models[0]
    assert m.name == "llama3.1:8b"
    assert m.need_gb == 7.1  # 4.9 GB file * 1.25 + 1
    assert found[0].base_url == "http://localhost:11434/v1"


def test_probe_finds_multiple_runtimes_and_ignores_closed_ports():
    transport = ports_transport({
        1234: {"data": [{"id": "some-model-q4"}]},           # LM Studio
        5001: {"result": "another-model-13b"},               # KoboldCpp
        8000: {"data": []},                                  # vLLM, no models
    })
    found = {r.key: r for r in probe_runtimes(transport)}
    assert set(found) == {"lmstudio", "koboldcpp", "vllm"}
    assert found["lmstudio"].models[0].name == "some-model-q4"
    assert found["koboldcpp"].models[0].name == "another-model-13b"


def test_estimate_need_from_name_params_and_quant():
    assert estimate_need_gb("model-7b-q4") == round(7.0 * 0.68 + 1.5, 1)
    assert estimate_need_gb("model-13b-q8") == round(13 * 1.15 + 1.5, 1)
    assert estimate_need_gb("mystery-model") == round(7.0 * 0.68 + 1.5, 1)  # default 7B q4


def test_fit_rule_and_best_choice():
    assert fits(7.0, 16.0)
    assert not fits(11.0, 12.0)  # needs 2 GB headroom
    name, reason = best_choice(16.0)
    assert name == "qwen2.5:14b" and "fits" in reason
    name, _ = best_choice(10.0)
    assert name == "qwen2.5:7b"  # strongest that fits 10 GB with headroom
    name, _ = best_choice(8.0)
    assert name == "gemma3:4b"   # 7 GB models don't leave 2 GB headroom on 8 GB
    name, reason = best_choice(4.0)
    assert name == "gemma3:4b" and "little memory" in reason


def test_curated_list_is_the_spec_menu_order():
    assert [c.name for c in CURATED] == [
        "qwen2.5:7b", "llama3.1:8b", "gemma3:4b", "deepseek-r1:8b",
        "qwen2.5:14b", "deepseek-r1:14b", "gemma3:12b",
    ]


def test_detect_os_variants(tmp_path):
    assert detect_os("darwin") == "mac"
    assert detect_os("win32") == "windows"
    rel = tmp_path / "os-release"
    rel.write_text('ID=ubuntu\nPRETTY_NAME="Ubuntu 24.04"\n')
    assert detect_os("linux", release_file=str(rel)) == "ubuntu"
    assert detect_os("linux", release_file=str(tmp_path / "missing")) == "linux"


def test_headless_detection():
    assert is_headless("ubuntu", {}) is True
    assert is_headless("ubuntu", {"DISPLAY": ":0"}) is False
    assert is_headless("mac", {}) is False


def test_ram_from_meminfo(tmp_path):
    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal:       16384000 kB\nMemFree: 1 kB\n")
    assert detect_ram_gb(meminfo=str(mem)) == 15.6


def test_detect_mcp_apps(tmp_path):
    (tmp_path / ".config" / "Claude").mkdir(parents=True)

    def which(name):
        return "/usr/bin/" + name if name in ("claude", "goose") else None

    apps = {a.key: a.found for a in detect_mcp_apps("linux", tmp_path, which)}
    assert apps["claude-code"] and apps["claude-desktop"] and apps["goose"]
    assert not apps["gemini-cli"] and not apps["codex-cli"]


def scan_fixture(**over) -> SystemScan:
    base = dict(
        os_name="ubuntu", headless=False, python_version="3.12", python_ok=True,
        git=True, docker=False, ram_gb=16.0, free_disk_gb=100.0, gpu=None,
        runtimes=[RuntimeInfo("ollama", "Ollama (an app that runs AI models on"
                              " your computer)", 11434,
                              [ModelInfo("llama3.1:8b", 4_900_000_000)])],
        mcp_apps=[AppInfo("claude-code", "Claude Code", True)],
    )
    base.update(over)
    return SystemScan(**base)


def test_render_scan_is_dual_language_live_list():
    lines = render_scan(scan_fixture())
    joined = "\n".join(lines)
    assert "✓ Ollama (an app that runs AI models on your computer) — 1 model found" in joined
    assert "✗ Docker (a helper program for the search engine) — not found" in joined
    assert "✓ Git (a program that downloads code) — found" in joined
    assert "Memory (RAM): 16 GB" in joined
    assert "Claude Code" in joined


def test_render_scan_no_runtime_marks_ollama_missing():
    lines = "\n".join(render_scan(scan_fixture(runtimes=[])))
    assert "✗ Ollama (an app that runs AI models on your computer) — not found" in lines
