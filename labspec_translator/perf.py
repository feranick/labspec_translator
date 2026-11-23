"""perf.py — the instrument's measured performance, remembered between sessions.
Version: 2026.11.23.1

Four kinds of measurement, each kept per key with its history (the first record is the baseline):

| kind       | key                         | from                         | what                                          |
|------------|-----------------------------|------------------------------|-----------------------------------------------|
| filters    | laser                       | a 'filters' study            | each filter's measured transmission (%)       |
| axial      | objective@laser@hole        | a focus scan                 | the depth resolution: FWHM of signal vs Z (µm) |
| resolution | laser@grating@slit          | a calibration study          | the reference line's FWHM (cm-1)              |
| cutoff     | laser@grating               | a 'cutoff' study             | where the edge filter lets light through (cm-1)|

Records are measurements, not limits: ls6t writes them itself (LS6T_HOME/performance.json; the simulator keeps its
own). They feed warnings (a range below the cut-off, a resolution worse than the baseline) and are shown by
`references`; a person decides whether the profile changes (e.g. the filters' transmissions).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config

KINDS = ("filters", "axial", "resolution", "cutoff")


def path() -> Path:
    return (config.out_dir() / "sim_performance.json") if config.mode() == "sim" else (config.home() / "performance.json")


def _num(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "?"


def key(*parts) -> str:
    return "@".join(_num(p) if not isinstance(p, str) else (p or "?") for p in parts)


def load() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def record(kind: str, k: str, entry: dict, request: str | None = None) -> dict:
    """Stores a measurement; returns {latest, baseline, n}. The first record of a key is its baseline."""
    if kind not in KINDS:
        raise ValueError(kind)
    d = load()
    e = {**entry, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": request}
    slot = d.setdefault(kind, {}).setdefault(k, {"history": []})
    slot["history"].append(e)
    slot["history"] = slot["history"][-50:]
    slot.setdefault("baseline", e)
    slot["latest"] = e
    _save(d)
    return {"latest": e, "baseline": slot["baseline"], "n": len(slot["history"])}


def get(kind: str, k: str) -> dict | None:
    return (load().get(kind) or {}).get(k)


def status() -> dict:
    d = load()
    return {kind: {k: {"latest": v.get("latest"), "baseline": v.get("baseline"), "measurements": len(v.get("history", []))}
                   for k, v in (d.get(kind) or {}).items()} for kind in KINDS if d.get(kind)}


def cutoff_for(laser_nm, grating) -> float | None:
    """The lowest usable Raman shift measured for this laser and grating (90 % of full transmission), if any."""
    rec = get("cutoff", key(laser_nm, grating))
    try:
        return float(rec["latest"]["usable_from_cm1"])
    except (TypeError, KeyError, ValueError):
        return None
