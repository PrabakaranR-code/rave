"""System scan: OS, tooling, hardware, local LLM runtimes, MCP-capable apps.

Every probe is injectable (HTTP transport, `which`, command runner, filesystem
root) so the whole scan is unit-testable offline. Output renders as a live
✓/✗ list in dual language.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

MIN_PYTHON = (3, 11)


# ---------------------------------------------------------------------------
# Local LLM runtimes
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    name: str
    size_bytes: int | None = None

    @property
    def need_gb(self) -> float:
        return estimate_need_gb(self.name, self.size_bytes)


@dataclass
class RuntimeInfo:
    key: str
    label: str  # dual-language display name
    port: int
    models: list[ModelInfo] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}/v1"


def _parse_openai_models(data: dict) -> list[ModelInfo]:
    return [ModelInfo(m.get("id", "")) for m in data.get("data", []) if m.get("id")]


def _parse_ollama_tags(data: dict) -> list[ModelInfo]:
    return [
        ModelInfo(m.get("name", ""), m.get("size"))
        for m in data.get("models", [])
        if m.get("name")
    ]


def _parse_kobold(data: dict) -> list[ModelInfo]:
    name = data.get("result", "")
    return [ModelInfo(name)] if name else []


# (key, dual-language label, port, path, parser)
RUNTIME_PROBES = (
    ("ollama", "Ollama (an app that runs AI models on your computer)",
     11434, "/api/tags", _parse_ollama_tags),
    ("lmstudio", "LM Studio (an app that runs AI models)",
     1234, "/v1/models", _parse_openai_models),
    ("jan", "Jan (an app that runs AI models)",
     1337, "/v1/models", _parse_openai_models),
    ("gpt4all", "GPT4All (an app that runs AI models)",
     4891, "/v1/models", _parse_openai_models),
    ("llamacpp", "llama.cpp / llamafile / LocalAI (local model servers)",
     8080, "/v1/models", _parse_openai_models),
    ("vllm", "vLLM (a local model server)",
     8000, "/v1/models", _parse_openai_models),
    ("textgen", "text-generation-webui (an app that runs AI models)",
     5000, "/v1/models", _parse_openai_models),
    ("koboldcpp", "KoboldCpp (an app that runs AI models)",
     5001, "/api/v1/model", _parse_kobold),
)


def probe_runtimes(
    transport: httpx.BaseTransport | None = None, timeout: float = 1.5
) -> list[RuntimeInfo]:
    found: list[RuntimeInfo] = []
    with httpx.Client(transport=transport, timeout=timeout) as client:
        for key, label, port, path, parser in RUNTIME_PROBES:
            try:
                resp = client.get(f"http://localhost:{port}{path}")
                if resp.status_code != 200:
                    continue
                models = parser(resp.json())
            except Exception:  # noqa: BLE001 — closed port, bad JSON: not present
                continue
            found.append(RuntimeInfo(key, label, port, models))
    return found


# ---------------------------------------------------------------------------
# Memory-need estimation and the fit rule
# ---------------------------------------------------------------------------

_QUANT_BYTES = (
    ("f16", 2.1), ("fp16", 2.1), ("q8", 1.15), ("q6", 0.90),
    ("q5", 0.78), ("q4", 0.68), ("q3", 0.55), ("q2", 0.45),
)
_PARAMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.I)


def estimate_need_gb(name: str, size_bytes: int | None = None) -> float:
    """Rough RAM need: file size ×1.25 +1 GB, or params × bytes/param +1.5 GB."""
    if size_bytes:
        return round(size_bytes / 1e9 * 1.25 + 1.0, 1)
    m = _PARAMS_RE.search(name)
    params = float(m.group(1)) if m else 7.0
    lname = name.lower()
    bpp = next((b for tag, b in _QUANT_BYTES if tag in lname), 0.68)
    return round(params * bpp + 1.5, 1)


def fits(need_gb: float, ram_gb: float) -> bool:
    """A model fits when it leaves ~2 GB of headroom for the system."""
    return need_gb <= ram_gb - 2.0


# Curated download list, in the order shown in the menu (per spec).
@dataclass(frozen=True)
class CuratedModel:
    name: str
    need_gb: float
    note: str


CURATED = (
    CuratedModel("qwen2.5:7b", 7.0, "best tool discipline"),
    CuratedModel("llama3.1:8b", 7.0, "solid all-rounder"),
    CuratedModel("gemma3:4b", 5.0, "lightest — for smaller machines"),
    CuratedModel("deepseek-r1:8b", 7.0, "small reasoning model"),
    CuratedModel("qwen2.5:14b", 11.0, "larger, follows instructions well"),
    CuratedModel("deepseek-r1:14b", 11.0, "larger reasoning model"),
    CuratedModel("gemma3:12b", 10.0, "larger general model"),
)

# Preference order for the "Best choice" line: strongest model that fits.
_PREFERENCE = (
    "qwen2.5:14b", "deepseek-r1:14b", "gemma3:12b",
    "qwen2.5:7b", "llama3.1:8b", "deepseek-r1:8b", "gemma3:4b",
)


def best_choice(ram_gb: float) -> tuple[str, str]:
    """(model, plain reason) — the strongest curated model that fits."""
    by_name = {c.name: c for c in CURATED}
    for name in _PREFERENCE:
        c = by_name[name]
        if fits(c.need_gb, ram_gb):
            return c.name, (
                f"{c.note}, and it fits your {ram_gb:.0f} GB of memory"
            )
    smallest = by_name[_PREFERENCE[-1]]
    return smallest.name, (
        "the lightest option — this computer has very little memory, so"
        " expect slow answers"
    )


# ---------------------------------------------------------------------------
# MCP-capable AI apps
# ---------------------------------------------------------------------------

@dataclass
class AppInfo:
    key: str
    label: str
    found: bool
    detail: str = ""


def _claude_desktop_dir(os_name: str, home: Path) -> Path:
    if os_name == "mac":
        return home / "Library" / "Application Support" / "Claude"
    if os_name == "windows":
        return Path(os.environ.get("APPDATA", str(home / "AppData/Roaming"))) / "Claude"
    return home / ".config" / "Claude"


def detect_mcp_apps(
    os_name: str,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[AppInfo]:
    home = home or Path.home()
    apps = [
        AppInfo("claude-code", "Claude Code", bool(which("claude"))),
        AppInfo("claude-desktop", "Claude Desktop",
                _claude_desktop_dir(os_name, home).exists()),
        AppInfo("gemini-cli", "Gemini CLI", bool(which("gemini"))),
        AppInfo("codex-cli", "Codex CLI", bool(which("codex"))),
        AppInfo("qwen-code", "Qwen Code CLI", bool(which("qwen"))),
        AppInfo("goose", "Goose", bool(which("goose"))),
    ]
    return apps


# ---------------------------------------------------------------------------
# OS / hardware / tooling
# ---------------------------------------------------------------------------

def detect_os(platform_name: str | None = None, release_file: str = "/etc/os-release") -> str:
    p = platform_name or sys.platform
    if p == "darwin":
        return "mac"
    if p.startswith("win"):
        return "windows"
    try:
        text = Path(release_file).read_text(encoding="utf-8").lower()
        if "ubuntu" in text or "debian" in text:
            return "ubuntu"
    except OSError:
        pass
    return "linux"


def is_headless(os_name: str, environ: dict | None = None) -> bool:
    """Linux with no display = VPS/server: install silently, no browser."""
    env = environ if environ is not None else os.environ
    return os_name in ("ubuntu", "linux") and not (
        env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
    )


def detect_ram_gb(
    meminfo: str = "/proc/meminfo",
    run: Callable[..., tuple[int, str]] | None = None,
) -> float:
    try:
        for line in Path(meminfo).read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    if run is not None:
        code, out = run(["sysctl", "-n", "hw.memsize"])
        if code == 0 and out.strip().isdigit():
            return round(int(out.strip()) / 1e9, 1)
    return 8.0  # conservative default when undetectable


def detect_gpu(
    run: Callable[..., tuple[int, str]],
    machine: str | None = None,
    os_name: str = "linux",
) -> str | None:
    code, out = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if code == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    if os_name == "mac" and (machine or platform.machine()) == "arm64":
        return "Apple silicon (built-in graphics memory)"
    return None


@dataclass
class SystemScan:
    os_name: str
    headless: bool
    python_version: str
    python_ok: bool
    git: bool
    docker: bool
    ram_gb: float
    free_disk_gb: float
    gpu: str | None
    runtimes: list[RuntimeInfo] = field(default_factory=list)
    mcp_apps: list[AppInfo] = field(default_factory=list)

    @property
    def found_apps(self) -> list[AppInfo]:
        return [a for a in self.mcp_apps if a.found]


def run_scan(
    transport: httpx.BaseTransport | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., tuple[int, str]] | None = None,
    home: Path | None = None,
) -> SystemScan:
    from wizard.ui import _default_run

    run = run or _default_run
    os_name = detect_os()
    py = sys.version_info
    docker_ok = bool(which("docker")) and run(["docker", "info"])[0] == 0
    return SystemScan(
        os_name=os_name,
        headless=is_headless(os_name),
        python_version=f"{py.major}.{py.minor}",
        python_ok=(py.major, py.minor) >= MIN_PYTHON,
        git=bool(which("git")),
        docker=docker_ok,
        ram_gb=detect_ram_gb(run=run),
        free_disk_gb=round(shutil.disk_usage(str(home or Path.home())).free / 1e9, 1),
        gpu=detect_gpu(run, os_name=os_name),
        runtimes=probe_runtimes(transport),
        mcp_apps=detect_mcp_apps(os_name, home=home, which=which),
    )


def render_scan(scan: SystemScan) -> list[str]:
    """The live ✓/✗ list, dual language throughout."""

    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    lines = [
        f"{mark(True)} Operating system: {scan.os_name}"
        + (" (server without a screen)" if scan.headless else ""),
        f"{mark(scan.python_ok)} Python (the language RAVE runs on) —"
        f" {scan.python_version}"
        + ("" if scan.python_ok else " — needs 3.11 or newer"),
        f"{mark(scan.git)} Git (a program that downloads code) —"
        f" {'found' if scan.git else 'not found'}",
        f"{mark(scan.docker)} Docker (a helper program for the search engine) —"
        f" {'found' if scan.docker else 'not found'}",
        f"{mark(True)} Memory (RAM): {scan.ram_gb:.0f} GB",
        f"{mark(True)} Free disk space: {scan.free_disk_gb:.0f} GB",
        f"{mark(bool(scan.gpu))} Graphics card (speeds up AI models) —"
        f" {scan.gpu or 'none found'}",
    ]
    if scan.runtimes:
        for rt in scan.runtimes:
            n = len(rt.models)
            lines.append(
                f"✓ {rt.label} — {n} model{'s' if n != 1 else ''} found"
            )
    else:
        lines.append(
            "✗ Ollama (an app that runs AI models on your computer) — not found"
        )
    found = [a.label for a in scan.found_apps]
    lines.append(
        "✓ AI apps that can connect to RAVE: " + ", ".join(found)
        if found else
        "✗ AI apps that can connect to RAVE — none found"
    )
    return lines
