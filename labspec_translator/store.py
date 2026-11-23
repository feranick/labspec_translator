"""store.py — the small JSON files ls6t remembers between sessions (performance, spectral corrections, beam offsets,
background index, video scale and orientation, grating windows).

One place for where they live (LS6T_HOME, or the simulator's own copy in LS6T_OUT, prefixed sim_, so practice runs
never touch the instrument's records) and how they are written (a temporary file, then an atomic replace that is
retried while Windows holds the file for a moment). A missing or unreadable file reads as empty.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config


def path(name: str) -> Path:
    """LS6T_HOME/<name> on the instrument; LS6T_OUT/sim_<name> in simulator mode."""
    return (config.out_dir() / f"sim_{name}") if config.mode() == "sim" else (config.home() / name)


def read(name: str) -> dict:
    try:
        d = json.loads(path(name).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write(name: str, data: dict) -> dict:
    """Atomic: never a half-written file for a reader (the runner, the MCP server, another app)."""
    from .bridge import replace_retry
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    replace_retry(tmp, p)
    return data
