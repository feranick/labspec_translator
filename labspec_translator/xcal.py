"""xcal.py — the spectral (Raman shift) calibration: reference lines, and the correction ls6t may apply itself.

At the start of a session the zero of the spectral scale is checked on a sample with strong, well-known lines
(silicon, polystyrene...). LabSpec's own correction (the spectrometer's zero and its dispersion coefficient, in
Maintenance > Instrument calibration) has no automation command, so ls6t either guides the operator to adjust it
and measures again, or remembers a correction of its own (true = a + b x measured, per laser and grating) and
applies it to every later spectrum, saying so in each result. Corrections live in LS6T_HOME/xcal.json.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import store as jstore

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
    return jstore.path("xcal.json")


def key(laser_nm, grating) -> str:
    def f(v):
        try:
            return f"{float(v):g}"
        except (TypeError, ValueError):
            return "?"
    return f"{f(laser_nm)}@{f(grating)}"


def load() -> dict:
    return jstore.read("xcal.json")


def _save(d: dict):
    jstore.write("xcal.json", d)


CENTRE_TOL_NM = 1.0     # a correction measured within this of a window's centre applies as it is


def get(laser_nm, grating, centre_nm=None) -> dict | None:
    """The correction for this laser and grating, for a window centred at centre_nm: the one measured there, or
    interpolated between the two around it, or the nearest (then 'centre_far': the zero differs between windows:
    1.7 cm-1 between two windows a minute apart, 2026-11-24). Without a centre, the last one measured."""
    rec = load().get(key(laser_nm, grating))
    if not rec:
        return None
    by = rec.get("by_centre") or {}
    if centre_nm is None or not by:
        out = {k: v for k, v in rec.items() if k != "by_centre"}
        if by or centre_nm is not None:
            out["centre_unknown"] = True
        return out
    c = float(centre_nm)
    pts = sorted((float(k), v) for k, v in by.items())
    near = min(pts, key=lambda kv: abs(kv[0] - c))
    if abs(near[0] - c) <= CENTRE_TOL_NM:
        return dict(near[1], centre_nm=near[0], centre_distance_nm=round(abs(near[0] - c), 3))
    lo = [kv for kv in pts if kv[0] < c]
    hi = [kv for kv in pts if kv[0] > c]
    if lo and hi:
        (c0, r0), (c1, r1) = lo[-1], hi[0]
        f = (c - c0) / (c1 - c0)
        out = dict(r1 if f >= 0.5 else r0, a=round(r0["a"] + f * (r1["a"] - r0["a"]), 5),
                   b=round(r0["b"] + f * (r1["b"] - r0["b"]), 7), centre_nm=round(c, 3),
                   interpolated_between_nm=[c0, c1])
        if r0.get("laser_line_cm1") is not None and r1.get("laser_line_cm1") is not None:
            out["laser_line_cm1"] = round(r0["laser_line_cm1"] + f * (r1["laser_line_cm1"] - r0["laser_line_cm1"]), 3)
        else:
            out.pop("laser_line_cm1", None)
        return out
    return dict(near[1], centre_nm=near[0], centre_distance_nm=round(abs(near[0] - c), 3), centre_far=True)


def store(laser_nm, grating, a: float, b: float, reference: str, lines: list, request: str | None = None,
          centre_nm=None, laser_line_cm1=None) -> dict:
    """Keeps the correction per window centre (2026-11-24: two Si calibrations a minute apart, in two windows, gave
    -0.72 and -2.40 cm-1: each had overwritten the other)."""
    d = load()
    k = key(laser_nm, grating)
    rec = {"a": round(float(a), 5), "b": round(float(b), 7), "reference": reference, "lines": lines,
           "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": request}
    if centre_nm is not None:
        rec["centre_nm"] = round(float(centre_nm), 2)
    if laser_line_cm1 is not None:
        # where the laser line sat when this was measured: a later spectrum is brought back to it before the
        # correction applies (laser_reference 'correct'), so the laser's move is not counted twice
        rec["laser_line_cm1"] = round(float(laser_line_cm1), 3)
    by = dict((d.get(k) or {}).get("by_centre") or {})
    if centre_nm is not None:
        by[f"{float(centre_nm):.2f}"] = rec
    d[k] = dict(rec, **({"by_centre": by} if by else {}))
    _save(d)
    return rec


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
