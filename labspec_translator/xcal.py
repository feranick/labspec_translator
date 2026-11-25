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


def _usable(rec: dict, placement: str | None) -> bool:
    """A correction measured with LabSpec placing a range's window (its blue edge at From, the data on the CCD's edge:
    -2.09 cm-1 off at 676.75 nm, 2026-11-24) serves only studies acquired the same way."""
    return rec.get("placement") != "labspec" or placement == "labspec"


def range_key(range_cm1) -> str:
    lo, hi = sorted(float(v) for v in range_cm1)
    return f"{lo:g}-{hi:g}"


def get(laser_nm, grating, centre_nm=None, placement: str | None = None, range_cm1=None) -> dict | None:
    """The correction for this laser and grating, for a window centred at centre_nm: the one measured there, or
    interpolated between the two around it, or the nearest (then 'centre_far': the zero differs between windows:
    1.7 cm-1 between two windows a minute apart, 2026-11-24). Without a centre, the last one measured."""
    rec = load().get(key(laser_nm, grating))
    if not rec:
        return None
    if range_cm1 is not None and (rec.get("by_range") or {}).get(range_key(range_cm1)):
        # an extended range calibrated with the same range: LabSpec stitches its windows the same way
        return dict(rec["by_range"][range_key(range_cm1)], range_match=True)
    by = {k: v for k, v in (rec.get("by_centre") or {}).items() if _usable(v, placement)}
    if rec.get("by_centre") and not by:
        return None
    if centre_nm is None or not by:
        top = sorted(by.values(), key=lambda v: v.get("time") or "")[-1] if by else rec   # the last usable one
        if not by and not _usable(rec, placement):
            return None
        out = {k: v for k, v in top.items() if k != "by_centre"}
        if (by or centre_nm is not None) and not top.get("pooled"):
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
          centre_nm=None, laser_line_cm1=None, laser_line_fwhm_cm1=None, placement: str | None = None,
          pooled: bool = False, repeats: dict | None = None, range_cm1=None) -> dict:
    """Keeps the correction per window centre (2026-11-24: two Si calibrations a minute apart, in two windows, gave
    -0.72 and -2.40 cm-1: each had overwritten the other)."""
    d = load()
    k = key(laser_nm, grating)
    rec = {"a": round(float(a), 5), "b": round(float(b), 7), "reference": reference, "lines": lines,
           "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": request}
    if centre_nm is not None:
        rec["centre_nm"] = round(float(centre_nm), 2)
    if placement:
        rec["placement"] = placement          # centred (ls6t placed the window) or labspec (LabSpec placed a range)
    if repeats:
        rec["repeats"] = repeats              # n, mean, sd of the repeats
    if laser_line_cm1 is not None:
        # where the laser line sat when this was measured: a later spectrum is brought back to it before the
        # correction applies (laser_reference 'correct'), so the laser's move is not counted twice
        rec["laser_line_cm1"] = round(float(laser_line_cm1), 3)
        if laser_line_fwhm_cm1:
            rec["laser_line_fwhm_cm1"] = round(float(laser_line_fwhm_cm1), 3)
    if pooled:
        # one correction for every window (a coarse grating: the repeat scatter exceeds the between-window
        # difference, 2026-11-24): the per-centre entries go
        rec["pooled"] = True
        dropped = len((d.get(k) or {}).get("by_centre") or {})
        if dropped:
            rec["replaced_per_centre"] = dropped
        d[k] = rec
        _save(d)
        return rec
    br = dict((d.get(k) or {}).get("by_range") or {})
    if centre_nm is None and range_cm1 is not None:
        rec["range_cm1"] = sorted(float(v) for v in range_cm1)
        br[range_key(range_cm1)] = rec
    by = dict((d.get(k) or {}).get("by_centre") or {})
    if centre_nm is not None:
        by[f"{float(centre_nm):.2f}"] = rec
    d[k] = dict(rec, **({"by_centre": by} if by else {}), **({"by_range": br} if br else {}))
    _save(d)
    return rec


def clear_centre(laser_nm, grating, centre_nm) -> int:
    """One window centre's correction (within CENTRE_TOL_NM) out of laser@grating; the others stay."""
    d = load()
    k = key(laser_nm, grating)
    rec = d.get(k)
    if not rec:
        return 0
    by = dict(rec.get("by_centre") or {})
    gone = [c for c in by if abs(float(c) - float(centre_nm)) <= CENTRE_TOL_NM]
    for c in gone:
        by.pop(c)
    if not gone:
        return 0
    if by:
        last = max(by.values(), key=lambda v: v.get("time") or "")
        d[k] = dict(last, by_centre=by)
    else:
        d.pop(k)
    _save(d)
    return len(gone)


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
