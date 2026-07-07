"""Secrets live in .env (chmod 600), never in config.yaml.

Tiny read/write helpers — no external dependency. `load_env` is called at
program start so `api_key_env` names resolve; existing environment variables
always win over file values.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path) -> dict[str, str]:
    """Load KEY=VALUE lines into os.environ (without overriding); return them."""
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            loaded[key] = value
            os.environ.setdefault(key, value)
    return loaded


def write_env(path: str | Path, key: str, value: str) -> None:
    """Set key=value in the .env file, creating it with owner-only mode."""
    p = Path(path)
    lines: list[str] = []
    if p.exists():
        lines = [
            l for l in p.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith(f"{key}=")
        ]
    lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # e.g. some Windows filesystems; best effort
