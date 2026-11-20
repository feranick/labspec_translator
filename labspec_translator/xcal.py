"""xcal.py — the spectral (Raman shift) calibration: reference lines, and the correction ls6t may apply itself.
Version: 2026.11.20.1

At the start of a session the zero of the spectral scale is checked on a sample with strong, well-known lines
(silicon, polystyrene...). LabSpec's own correction (the spectrometer's zero and its dispersion coefficient, in
Maintenance > Instrument calibration) has no automation command, so ls6t either guides the operator to adjust it
and measures again, or remembers a correction of its own (true = a + b x measured, per laser and grating) and
applies it to every later spectrum, saying so in each result. Corrections live in LS6T_HOME/xcal.json.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config

# reference Raman shifts (cm-1)
REFERENCES = {
    "si": [520.7],
    # ASTM E1840 polystyrene
    "polystyrene": [620.9, 1001.4, 1031.8, 1155.3, 1450.5, 1583.1, 1602.3, 2852.4, 2904.5, 3054.3],
    # naphthalene, cyclohexane (ASTM E1840)
    "naphthalene": [513.8, 763.8, 1021.6, 1147.2, 1382.2, 1464.5, 1576.6, 3056.4],
    "cyclohexane": [384.1, 426.3, 801.3, 1028.3, 1157.6, 1266.4, 1444.4, 2664.4, 2852.9, 2923.8, 2938.3],
    "diamond": [1332.5],
}


def path() -> Path:
    return (config.out_dir() / "sim_xcal.json") if config.mode() == "sim" else (config.home() / "xcal.json")


def key(laser_nm, grating) -> str:
    def f(v):
        try:
            return f"{float(v):g}"
        except (TypeError, ValueError):
            return "?"
    return f"{f(laser_nm)}@{f(grating)}"


def load() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def get(laser_nm, grating) -> dict | None:
    return load().get(key(laser_nm, grating))


def store(laser_nm, grating, a: float, b: float, reference: str, lines: list, request: str | None = None) -> dict:
    d = load()
    k = key(laser_nm, grating)
    d[k] = {"a": round(float(a), 5), "b": round(float(b), 7), "reference": reference, "lines": lines,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": request}
    _save(d)
    return d[k]


def clear(laser_nm=None, grating=None) -> int:
    d = load()
    if laser_nm is None and grating is None:
        n = len(d)
        d = {}
    else:
        n = 1 if d.pop(key(laser_nm, grating), None) else 0
    _save(d)
    return n


def age_h(rec: dict) -> float | None:
    try:
        return round((time.time() - time.mktime(time.strptime(rec["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600, 1)
    except (KeyError, ValueError):
        return None
