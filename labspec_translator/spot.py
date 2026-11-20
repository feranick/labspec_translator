"""spot.py — where the laser beam really is: the measured offsets, remembered per objective and laser.
Version: 2026.11.20.14

LabSpec marks the laser at the stage position (the image centre); the beam drifts away from it. Each 'spot' study
(or snapshot with the laser) that finds the beam reliably records the offset (beam - LabSpec's position, µm) for
the objective and laser in use. Snapshots draw the marker with the latest record, and `move.to_um` puts the beam
(not LabSpec's marker) on a point picked in a snapshot. Records are measurements, not limits, so ls6t writes them
itself (LS6T_HOME/spot.json; the simulator keeps its own); the profile's spot settings bound what is accepted.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from . import config


def path() -> Path:
    return (config.out_dir() / "sim_spot.json") if config.mode() == "sim" else (config.home() / "spot.json")


def key(objective, laser_nm) -> str:
    try:
        las = f"{float(laser_nm):g}"
    except (TypeError, ValueError):
        las = "?"
    return f"{objective or '?'}@{las}"


def load() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"records": {}, "history": []}


def _save(d: dict):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def current(objective, laser_nm) -> dict | None:
    return load()["records"].get(key(objective, laser_nm))


def offset_for(prof: dict, objective, laser_nm) -> tuple[list, str]:
    """The offset to use: the latest record for this objective and laser, else the profile's video.laser_offset_um
    (a person's value), else none. Returns (offset, source)."""
    rec = current(objective, laser_nm)
    if rec:
        return list(rec["offset_um"]), f"measured {rec.get('time')} ({rec.get('method')})"
    off = prof.get("video", {}).get("laser_offset_um") or [0.0, 0.0]
    if any(off):
        return list(off), "profile video.laser_offset_um"
    return [0.0, 0.0], "none measured"


def record(prof: dict, objective, laser_nm, offset_um, method: str, quality: dict | None = None,
           request: str | None = None) -> dict:
    """Remembers a measured offset if it is plausible (within spot.max_offset_um). Returns {stored, why, drift_um,
    previous}: drift is the change from the previous record of the same objective and laser."""
    s = prof.get("spot", {})
    mx = float(s.get("max_offset_um", 10.0))
    off = [round(float(offset_um[0]), 3), round(float(offset_um[1]), 3)]
    out: dict = {"stored": False, "key": key(objective, laser_nm), "offset_um": off}
    if math.hypot(*off) > mx:
        out["why"] = f"{math.hypot(*off):.1f} µm is more than spot.max_offset_um = {mx:g} µm: not remembered"
        return out
    d = load()
    k = out["key"]
    prev = d["records"].get(k)
    entry = {"offset_um": off, "objective": objective, "laser_nm": laser_nm, "method": method,
             "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "quality": quality or {}, "request": request}
    d["records"][k] = entry
    d.setdefault("history", []).append(entry)
    _save(d)
    out.update(stored=True, previous=prev)
    if prev:
        drift = math.hypot(off[0] - prev["offset_um"][0], off[1] - prev["offset_um"][1])
        out["drift_um"] = round(drift, 3)
        if drift > float(s.get("drift_warn_um", 1.0)):
            out["warning"] = (f"the beam moved {drift:.2f} µm since {prev.get('time')} ({k}): offsets measured "
                              "earlier no longer hold")
    return out


def status(prof: dict) -> dict:
    d = load()
    s = prof.get("spot", {})
    now = time.time()
    recs = {}
    for k, r in d["records"].items():
        try:
            age = round((now - time.mktime(time.strptime(r["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600, 1)
        except (KeyError, ValueError):
            age = None
        hist = [h for h in d.get("history", []) if key(h.get("objective"), h.get("laser_nm")) == k]
        drift = None
        if len(hist) >= 2:
            a, b = hist[0]["offset_um"], hist[-1]["offset_um"]
            drift = round(math.hypot(b[0] - a[0], b[1] - a[1]), 3)
        recs[k] = {**r, "age_h": age, "stale": age is not None and age > float(s.get("max_age_h", 24)),
                   "measurements": len(hist), "drift_since_first_um": drift}
    return {"records": recs, "file": str(path()), "profile_offset_um": prof.get("video", {}).get("laser_offset_um"),
            "settings": s}


def forget(objective=None, laser_nm=None) -> dict:
    d = load()
    if objective is None and laser_nm is None:
        n = len(d["records"])
        d["records"] = {}
    else:
        k = key(objective, laser_nm)
        n = 1 if d["records"].pop(k, None) else 0
    _save(d)
    return {"forgotten": n}
