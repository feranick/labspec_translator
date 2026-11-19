"""profile.py — the machine profile: what the instrument has (from the discovery script) and the limits a person sets.
Version: 2026.11.19.1

The profile is a JSON file (LS6T_PROFILE, default ~/ls6t/profile.json). Studies never change it: an LLM can only
ask for what the profile allows. Without a file, the built-in XploRA defaults below apply and every result says so
in a warning; run the discovery script in LabSpec and `ls6t profile import <dump>` to replace them.
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
from pathlib import Path

from . import config

PROFILE_VERSION = 1

DEFAULT_PROFILE: dict = {
    "profile_version": PROFILE_VERSION,
    "source": "built-in XploRA defaults (not measured: run the discovery script)",
    "system": "XploRA",
    # LabSpec motor names, from the discovery script. Filter and objective names vary by configuration.
    "motors": {"laser": "Laser", "grating": "Grating", "filter": "Filter", "objective": "Objective",
               "hole": "Hole", "slit": "Slit", "x": "X", "y": "Y", "z": "Z"},
    "lasers": {"532": {"nm": 532.0, "label": "532", "power_mw": None, "max_filter_pct": 100},
               "638": {"nm": 638.0, "label": "638", "power_mw": None, "max_filter_pct": 100},
               "785": {"nm": 785.0, "label": "785", "power_mw": None, "max_filter_pct": 100}},
    # window_nm: the spectral width of one detector window (for time estimates and the extended-range count)
    "gratings": {"600": {"window_nm": 78.0}, "1200": {"window_nm": 39.0},
                 "1800": {"window_nm": 26.0}, "2400": {"window_nm": 19.5}},
    "filters": {"100%": 100.0, "50%": 50.0, "25%": 25.0, "10%": 10.0, "1%": 1.0, "0.1%": 0.1},
    "objectives": ["x10", "x50", "x100"],
    "manual_objective": True,      # LabSpec asks the operator to turn the turret
    "holes_um": None,              # null: any value LabSpec accepts
    "slits_um": None,
    "detector": {"full_scale": 65535, "max_temp_c": -50.0, "pixels": 1024},
    "stage": {"x_um": None, "y_um": None},   # absolute stage limits [min, max]; null: only max_xy_um applies
    "limits": {"max_xy_um": 5000.0,          # farthest a study may move from where the operator left the stage
               "max_dz_um": 100.0,           # farthest from the operator's focus (Z is always relative)
               "max_exposure_s": 600.0, "max_accumulations": 500, "max_points": 10000,
               "max_minutes": 480.0, "max_filter_pct": 100.0, "sensitive_max_filter_pct": 10.0},
    "timing": {"command_s": 0.3, "move_s": 2.0, "laser_s": 15.0, "grating_s": 15.0, "objective_s": 20.0,
               "xy_um_per_s": 2000.0, "xy_settle_s": 0.3, "autofocus_s": 10.0, "readout_s": 0.5,
               "window_move_s": 2.0},
    "checks": {"min_snr": 10.0, "detect_snr": 5.0, "si_cm1": 520.7, "si_tol_cm1": 1.0, "saturation_frac": 0.98},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("lasers", "gratings", "filters"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path: Path | None = None) -> dict:
    """The profile in use: the file merged over the defaults ('measured': false when there is no file)."""
    p = Path(path) if path else config.profile_path()
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        prof = _merge(DEFAULT_PROFILE, data)
        prof["measured"] = True
        prof["path"] = str(p)
    else:
        prof = copy.deepcopy(DEFAULT_PROFILE)
        prof["measured"] = False
        prof["path"] = None
    return prof


def save(prof: dict, path: Path | None = None) -> Path:
    p = Path(path) if path else config.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in prof.items() if k not in ("measured", "path")}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def filter_pct(label: str) -> float | None:
    """A filter label as a transmission in %: '10%' -> 10, 'D1' or 'OD1' -> 10, '100' -> 100."""
    s = str(label).strip().replace(",", ".")
    m = re.fullmatch(r"([0-9.]+)\s*%?", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = re.fullmatch(r"(?:O?D)\s*([0-9.]+)", s, flags=re.I)
    if m:
        try:
            return round(100.0 * 10 ** (-float(m.group(1))), 6)
        except ValueError:
            return None
    return None


def laser_key(prof: dict, laser) -> str | None:
    """The profile key of a laser given as a number (nm) or its key/label."""
    lasers = prof.get("lasers", {})
    if str(laser) in lasers:
        return str(laser)
    try:
        nm = float(laser)
    except (TypeError, ValueError):
        for k, v in lasers.items():
            if str(v.get("label")) == str(laser):
                return k
        return None
    best = min(lasers.items(), key=lambda kv: abs(float(kv[1]["nm"]) - nm), default=None)
    if best and abs(float(best[1]["nm"]) - nm) <= 1.0:
        return best[0]
    return None


# --------------------------------------------------------------------------------------------- discovery import

def read_text(path) -> str:
    """A text file from the bridge or the simulator: UTF-16 with a BOM (LabSpec's FSO in Unicode mode), UTF-8
    with or without a BOM, or else the Windows ANSI page."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def unescape(v: str) -> str:
    return v.replace("%0A", "\n").replace("%25", "%")


def escape(v: str) -> str:
    return v.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def parse_kv(text: str) -> dict:
    """key=value lines (the bridge's format); later keys win; '#' lines are comments; %0A is a line end, %25 a %."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip("\r\n\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = unescape(v)
    return out


def _motor(d: dict, name: str) -> dict | None:
    pre = f"motor.{name}."
    if d.get(pre + "present") != "1":
        return None
    try:
        size = int(float(d.get(pre + "size", "0")))
    except ValueError:
        size = 0
    values = [d.get(f"{pre}value.{i}") for i in range(size)]
    labels = [d.get(f"{pre}label.{i}") for i in range(size)]
    return {"name": name, "size": size, "values": values, "labels": labels,
            "current": d.get(pre + "current"), "current_label": d.get(pre + "current_label")}


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "."))
    except (TypeError, ValueError):
        return None


def import_discovery(text: str, base: dict | None = None) -> tuple[dict, list[str]]:
    """A profile from the discovery script's dump, keeping the limits and timing of `base` (the current profile).
    Returns (profile, notes): the notes say what was found and what a person should check."""
    d = parse_kv(text)
    prof = copy.deepcopy(base or load())
    prof.pop("measured", None)
    prof.pop("path", None)
    notes: list[str] = []
    prof["source"] = f"discovery {d.get('time', '')} (LabSpec {d.get('labspec_version', '?')})".strip()
    if d.get("system"):
        prof["system"] = d["system"]
    names = {k[len("motor."):-len(".present")] for k, v in d.items()
             if k.startswith("motor.") and k.endswith(".present") and v == "1"}
    notes.append("motors found: " + ", ".join(sorted(names)))

    las = _motor(d, prof["motors"]["laser"])
    if las and las["size"] > 0:
        lasers = {}
        for v, lab in zip(las["values"], las["labels"]):
            nm = _num(v)
            if nm is None or nm <= 0:
                continue
            key = f"{nm:g}"
            old = (base or {}).get("lasers", {}).get(key, {})
            lasers[key] = {"nm": nm, "label": lab or key, "power_mw": old.get("power_mw"),
                           "max_filter_pct": old.get("max_filter_pct", 100)}
        if lasers:
            prof["lasers"] = lasers
            notes.append("lasers: " + ", ".join(f"{v['nm']:g} nm" for v in lasers.values())
                         + " (set power_mw and max_filter_pct per laser by hand)")

    gr = _motor(d, prof["motors"]["grating"])
    if gr and gr["size"] > 0:
        gratings = {}
        for v in gr["values"]:
            g = _num(v)
            if g and g > 0:
                key = f"{g:g}"
                old = prof.get("gratings", {}).get(key, {})
                gratings[key] = {"window_nm": old.get("window_nm", round(46800.0 / g, 2))}
        if gratings:
            prof["gratings"] = gratings
            notes.append("gratings: " + ", ".join(gratings) + " gr/mm (window_nm estimated: measure it)")

    for role, cand in (("filter", ["Filter", "Density", "ND", "Attenuator"]),
                       ("objective", ["Objective", "Objectives", "Microscope", "Nosepiece"])):
        m = None
        for name in [prof["motors"].get(role)] + cand:
            if name and name in names:
                m = _motor(d, name)
                prof["motors"][role] = name
                break
        if not m:
            notes.append(f"no {role} motor found among {', '.join(cand)}: check the motor list in the dump")
            continue
        labels = [lab for lab in m["labels"] if lab]
        if role == "filter":
            filters = {lab: filter_pct(lab) for lab in labels}
            unknown = [lab for lab, p in filters.items() if p is None]
            prof["filters"] = filters
            notes.append(f"filters ({m['name']}): " + ", ".join(labels)
                         + (f"; set the % of {unknown} by hand" if unknown else ""))
        else:
            prof["objectives"] = labels
            notes.append(f"objectives ({m['name']}): " + ", ".join(labels))

    for role, key in (("hole", "holes_um"), ("slit", "slits_um")):
        m = _motor(d, prof["motors"][role])
        if m and m["size"] > 0:
            prof[key] = [x for x in (_num(v) for v in m["values"]) if x is not None]
    for ax in ("x", "y", "z"):
        if prof["motors"][ax] not in names:
            notes.append(f"stage axis {ax.upper()} ({prof['motors'][ax]}) not found: maps along it are refused")
    det = _num(d.get("detector_temp_c"))
    if det is not None:
        notes.append(f"detector at {det:g} C (max_temp_c {prof['detector']['max_temp_c']:g})")
    prof["discovered"] = {"time": d.get("time"), "labspec_path": d.get("labspec_path"),
                          "motors": sorted(names)}
    return prof, notes


def summary(prof: dict) -> dict:
    """The compact view study_format shows: what may be asked for."""
    return {"system": prof.get("system"), "measured": prof.get("measured", False), "source": prof.get("source"),
            "lasers_nm": [v["nm"] for v in prof.get("lasers", {}).values()],
            "gratings": [float(g) for g in prof.get("gratings", {})],
            "filters": list(prof.get("filters", {})),
            "objectives": list(prof.get("objectives", [])),
            "holes_um": prof.get("holes_um"), "slits_um": prof.get("slits_um"),
            "limits": prof.get("limits"),
            "laser_limits": {k: {"max_filter_pct": v.get("max_filter_pct", 100), "power_mw": v.get("power_mw")}
                             for k, v in prof.get("lasers", {}).items()}}


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def nm_to_shift(nm, laser_nm: float):
    """Raman shift (cm-1) of absolute wavelengths (nm) for a laser line (nm). Works on numbers and numpy arrays."""
    return 1e7 / laser_nm - 1e7 / nm


def shift_to_nm(cm1, laser_nm: float):
    return 1e7 / (1e7 / laser_nm - cm1)


def isfinite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
