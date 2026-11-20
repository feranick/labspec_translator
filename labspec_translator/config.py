"""config.py — settings (environment variables only) and the folders ls6t writes to.
Version: 2026.11.20.5

| Variable | Meaning | Default |
|---|---|---|
| LS6T_OUT | request folders | ~/ls6t_runs |
| LS6T_HOME | profile, bridge folder, instrument lock | ~/ls6t |
| LS6T_BRIDGE_DIR | the folder the LabSpec bridge script watches | $LS6T_HOME/bridge |
| LS6T_PROFILE | the machine profile (a person edits its limits) | $LS6T_HOME/profile.json |
| LS6T_MODE | labspec (the instrument) or sim (the built-in simulator) | labspec |
| LS6T_CONFIRM | ask the operator before the first laser exposure: request, study or never | request |
| LS6T_HEARTBEAT_S | the bridge counts as gone when its heartbeat is older than this | 20 |
| LS6T_MAX_MINUTES | ceiling for one request's estimated time (overrides the profile when lower) | unset |
"""
from __future__ import annotations

import os
from pathlib import Path

MODES = ("labspec", "sim")
CONFIRM = ("request", "study", "never")


def _path(var: str, default: Path) -> Path:
    v = os.environ.get(var, "").strip()
    return Path(v).expanduser() if v else default


def home() -> Path:
    return _path("LS6T_HOME", Path.home() / "ls6t")


def out_dir() -> Path:
    return _path("LS6T_OUT", Path.home() / "ls6t_runs")


def bridge_dir() -> Path:
    return _path("LS6T_BRIDGE_DIR", home() / "bridge")


def profile_path() -> Path:
    return _path("LS6T_PROFILE", home() / "profile.json")


def mode() -> str:
    m = os.environ.get("LS6T_MODE", "labspec").strip().lower() or "labspec"
    if m not in MODES:
        raise ValueError(f"LS6T_MODE={m!r}: use one of {', '.join(MODES)}")
    return m


def confirm_policy() -> str:
    c = os.environ.get("LS6T_CONFIRM", "request").strip().lower() or "request"
    if c not in CONFIRM:
        raise ValueError(f"LS6T_CONFIRM={c!r}: use one of {', '.join(CONFIRM)}")
    return c


def heartbeat_s() -> float:
    try:
        return max(2.0, float(os.environ.get("LS6T_HEARTBEAT_S", "20")))
    except ValueError:
        return 20.0


def max_minutes_env() -> float | None:
    v = os.environ.get("LS6T_MAX_MINUTES", "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def sim_bridge_dir() -> Path:
    """The simulator's own bridge folder, so a simulated run never talks to the instrument's bridge."""
    return out_dir() / "sim_bridge"


def active_bridge_dir() -> Path:
    return sim_bridge_dir() if mode() == "sim" else bridge_dir()


def alignment_log() -> Path:
    """Every alignment check, one JSON line each (the simulator keeps its own)."""
    return (out_dir() / "sim_alignment_log.jsonl") if mode() == "sim" else (home() / "alignment_log.jsonl")


def settings() -> dict:
    return {"LS6T_MODE": mode(), "LS6T_OUT": str(out_dir()), "LS6T_HOME": str(home()),
            "LS6T_BRIDGE_DIR": str(active_bridge_dir()), "LS6T_PROFILE": str(profile_path()),
            "LS6T_CONFIRM": confirm_policy(), "LS6T_HEARTBEAT_S": heartbeat_s(),
            "LS6T_MAX_MINUTES": max_minutes_env()}
