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
from .io_util import read_json, write_json


def path(name: str) -> Path:
    """LS6T_HOME/<name> on the instrument; LS6T_OUT/sim_<name> in simulator mode."""
    return (config.out_dir() / f"sim_{name}") if config.mode() == "sim" else (config.home() / name)


def read(name: str) -> dict:
    d = read_json(path(name), {})
    return d if isinstance(d, dict) else {}


def write(name: str, data: dict) -> dict:
    """Atomic: never a half-written file for a reader (the runner, the MCP server, another app)."""
    write_json(path(name), data, indent=1, default=str)
    return data
