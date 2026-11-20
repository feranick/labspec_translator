"""plan.py — a study as bridge commands: the setup moves, the points, the acquisitions; and the time estimate.
Version: 2026.11.20.2

Everything here is computed from the study and the profile alone: wavelengths from Raman shifts, the number of
detector windows, the stage positions (always offsets from where the operator left the stage, Z from the
operator's focus), the order of the points and how long it should take. The runner sends exactly these commands.
"""
from __future__ import annotations

import math

from .profile import laser_key, shift_to_nm

SPIKE_MODES = {"labspec": 0, "none": 100, "single": 200, "double": 300, "double_auto": 400}


def axis_values(spec) -> list[float]:
    """[from, to, step] (µm) -> the positions, from 'from' towards 'to', 'to' included when it falls on a step."""
    a, b, s = (float(v) for v in spec)
    if s <= 0:
        raise ValueError("step must be > 0")
    n = int(math.floor(abs(b - a) / s + 1e-9)) + 1
    sign = 1.0 if b >= a else -1.0
    return [round(a + sign * i * s, 6) for i in range(n)]


def positions(study: dict) -> list[dict]:
    """The points of a study, in the order they are measured: [{i, ix, iy, iz, x_um, y_um, dz_um}] (offsets)."""
    kind = study.get("kind", "spectrum")
    if kind == "map":
        m = study.get("map", {})
        xs = axis_values(m["x_um"]) if m.get("x_um") else [0.0]
        ys = axis_values(m["y_um"]) if m.get("y_um") else [0.0]
        zs = axis_values(m["z_um"]) if m.get("z_um") else [0.0]
        snake = m.get("snake", True)
        out = []
        for iz, z in enumerate(zs):
            for iy, y in enumerate(ys):
                row = list(enumerate(xs))
                if snake and iy % 2 == 1:
                    row.reverse()
                for ix, x in row:
                    out.append({"ix": ix, "iy": iy, "iz": iz, "x_um": x, "y_um": y, "dz_um": z})
        for i, p in enumerate(out):
            p["i"] = i
        return out
    if kind == "series":
        n = int(study.get("series", {}).get("count", 1))
        dz = float(study.get("focus", {}).get("dz_um", 0) or 0)
        return [{"i": i, "ix": i, "iy": 0, "iz": 0, "x_um": 0.0, "y_um": 0.0, "dz_um": dz} for i in range(n)]
    pts = study.get("points") or [{}]
    dz0 = float(study.get("focus", {}).get("dz_um", 0) or 0)
    out = []
    for i, p in enumerate(pts):
        out.append({"i": i, "ix": i, "iy": 0, "iz": 0, "x_um": float(p.get("x_um", 0) or 0),
                    "y_um": float(p.get("y_um", 0) or 0),
                    "dz_um": float(p["dz_um"]) if p.get("dz_um") is not None else dz0})
    return out


def map_shape(study: dict) -> tuple[int, int, int]:
    m = study.get("map", {})
    return (len(axis_values(m["x_um"])) if m.get("x_um") else 1,
            len(axis_values(m["y_um"])) if m.get("y_um") else 1,
            len(axis_values(m["z_um"])) if m.get("z_um") else 1)


def laser_nm(study: dict, prof: dict) -> float | None:
    v = study.get("setup", {}).get("laser_nm")
    if v is None:
        return None
    k = laser_key(prof, v)
    return float(prof["lasers"][k]["nm"]) if k else float(v)


def acq_range(study: dict, prof: dict, laser: float | None) -> dict:
    """The acquisition range: {from_cm1, to_cm1, from_nm, to_nm, windows, single}. With center_cm1, one window
    (LabSpec's From = To); with range_cm1, LabSpec stitches as many windows as it takes (extended range)."""
    acq = study.get("acquire", {})
    grating = study.get("setup", {}).get("grating")
    win_nm = None
    if grating is not None:
        g = prof.get("gratings", {}).get(f"{float(grating):g}")
        win_nm = float(g["window_nm"]) if g and g.get("window_nm") else None
    if study.get("kind") == "calibration" and not acq.get("range_cm1") and acq.get("center_cm1") is None:
        lines = calibration_lines(study, prof, None)
        if len(lines) > 1:
            acq = dict(acq, range_cm1=[max(50.0, min(lines) - 60.0), max(lines) + 60.0])
        else:
            acq = dict(acq, center_cm1=lines[0] if lines else prof["checks"]["si_cm1"])
    if acq.get("range_cm1"):
        lo, hi = sorted(float(v) for v in acq["range_cm1"])
        out = {"from_cm1": lo, "to_cm1": hi, "single": False}
        if laser:
            a, b = shift_to_nm(lo, laser), shift_to_nm(hi, laser)
            out.update(from_nm=round(a, 4), to_nm=round(b, 4))
            out["windows"] = max(1, math.ceil((b - a) / win_nm - 1e-9)) if win_nm else None
        else:
            out["windows"] = None
        return out
    c = float(acq.get("center_cm1", 1000.0))
    out = {"center_cm1": c, "single": True, "windows": 1}
    if laser:
        nm = shift_to_nm(c, laser)
        out.update(from_nm=round(nm, 4), to_nm=round(nm, 4))
        if win_nm:
            half = win_nm / 2
            out.update(from_cm1=round(1e7 / laser - 1e7 / (nm - half), 2),
                       to_cm1=round(1e7 / laser - 1e7 / (nm + half), 2))
    return out


def acq_mode(study: dict) -> int:
    """LabSpec's Acq Mode: ACQ_SPECTRUM + ACQ_AUTO_SHOW + spike removal + dark."""
    acq = study.get("acquire", {})
    mode = 0 + 10 + SPIKE_MODES[acq.get("spike", "labspec")]
    dark = acq.get("dark", "labspec")
    if dark is True:
        mode += 2000000
    elif dark is False:
        mode += 1000000
    return mode


def setup_steps(study: dict, prof: dict) -> list[dict]:
    """The moves that set the instrument up. The filter goes first, so the attenuation asked for is in place
    before anything else changes (the laser shutter opens only during acquisitions)."""
    s = study.get("setup", {})
    mot = prof["motors"]
    steps = []
    if s.get("filter") is not None:
        steps.append({"verb": "move", "motor": mot["filter"], "mode": "label", "value": str(s["filter"]),
                      "why": "filter (laser power at the sample)"})
    if s.get("laser_nm") is not None:
        k = laser_key(prof, s["laser_nm"])
        steps.append({"verb": "move", "motor": mot["laser"], "mode": "value",
                      "value": float(prof["lasers"][k]["nm"]) if k else float(s["laser_nm"]), "why": "laser"})
    if s.get("grating") is not None:
        steps.append({"verb": "move", "motor": mot["grating"], "mode": "value", "value": float(s["grating"]),
                      "why": "grating"})
    if s.get("objective") is not None:
        steps.append({"verb": "move", "motor": mot["objective"], "mode": "label", "value": str(s["objective"]),
                      "why": "objective" + (" (manual turret: LabSpec asks the operator)"
                                            if prof.get("manual_objective") else "")})
    if s.get("hole_um") is not None:
        steps.append({"verb": "move", "motor": mot["hole"], "mode": "value", "value": float(s["hole_um"]),
                      "why": "confocal hole"})
    if s.get("slit_um") is not None:
        steps.append({"verb": "move", "motor": mot["slit"], "mode": "value", "value": float(s["slit_um"]),
                      "why": "slit"})
    return steps


def acquire_step(study: dict, prof: dict, rng: dict) -> dict:
    acq = study.get("acquire", {})
    exp = acq.get("exposure_s", 1.0)
    step = {"verb": "acquire", "mode": acq_mode(study),
            "exposure_s": 0.0 if exp == "auto" else float(exp),
            "accumulations": int(acq.get("accumulations", 1)),
            "from_nm": rng.get("from_nm"), "to_nm": rng.get("to_nm")}
    if exp == "auto":
        ae = acq.get("auto_exposure", {})
        step.update(ae_test_s=float(ae.get("test_s", 0.1)), ae_min_s=float(ae.get("min_s", 0.1)),
                    ae_max_s=float(ae.get("max_s", 10.0)),
                    ae_target=float(ae.get("target_counts", 0.5 * prof["detector"]["full_scale"])))
    return step


def acquire_seconds(study: dict, prof: dict, rng: dict) -> float:
    acq = study.get("acquire", {})
    t = prof["timing"]
    exp = acq.get("exposure_s", 1.0)
    if exp == "auto":
        ae = acq.get("auto_exposure", {})
        exp = float(ae.get("max_s", 10.0)) + float(ae.get("test_s", 0.1))
    n = int(acq.get("accumulations", 1)) + (1 if acq.get("spike") == "double_auto" else 0)
    w = rng.get("windows") or 1
    return w * n * (float(exp) + t["readout_s"]) + (w if not rng.get("single") else 1) * t["window_move_s"] \
        + t["command_s"]


SCAN_DEFAULTS = {"range_um": 10.0, "step_um": 1.0, "window_cm1": 30.0, "exposure_s": 0.5}
AUTOFOCUS = ("laser", "raman", "video", "scan")


def calibration_lines(study: dict, prof: dict, rng: dict | None) -> list[float]:
    """The reference lines of a calibration (calibration.lines_cm1, or a named reference), within the range
    measured (15 cm-1 inside its ends) when the range is known."""
    from .xcal import REFERENCES
    cal = study.get("calibration") or {}
    lines = cal.get("lines_cm1") or REFERENCES.get(cal.get("reference", "si"), [prof["checks"]["si_cm1"]])
    if cal.get("reference", "si") == "si" and not cal.get("lines_cm1"):
        lines = [prof["checks"]["si_cm1"]]
    lines = sorted(float(v) for v in lines)
    if rng and rng.get("from_cm1") is not None and rng.get("to_cm1") is not None:
        lines = [v for v in lines if rng["from_cm1"] + 15 <= v <= rng["to_cm1"] - 15]
    return lines


def map_engine(study: dict, prof: dict) -> str:
    """'native' (LabSpec's mapping engine) or 'points' (ls6t moves and acquires point by point). 'auto' picks native
    for large maps that need nothing between points."""
    if study.get("kind") not in ("map", "depth"):
        return "points"
    m = study.get("map", {}) if study.get("kind") == "map" else study.get("depth", {})
    eng = m.get("engine", "auto")
    if eng in ("points", "native"):
        return eng
    if (study.get("focus") or {}).get("every") or (study.get("acquire") or {}).get("exposure_s") == "auto":
        return "points"
    if (study.get("acquire") or {}).get("range_cm1"):
        return "points"
    n = len(positions(study if study.get("kind") == "map" else depth_as_map(study, prof)))
    return "native" if n >= int(prof.get("mapping", {}).get("native_min_points", 400)) else "points"


def into_sign(prof: dict) -> tuple[int, bool]:
    """+1 when a larger Z focuses deeper into the sample, -1 the other way (profile stage.z_into_sample); the
    second value says whether the profile states it (otherwise +1 is assumed)."""
    v = str(prof.get("stage", {}).get("z_into_sample") or "").strip()
    if v in ("+", "+1", "1", "up", "increasing"):
        return 1, True
    if v in ("-", "-1", "down", "decreasing"):
        return -1, True
    return 1, False


def depth_as_map(study: dict, prof: dict) -> dict:
    """A depth profile as the map that measures it: Z offsets from the focus, in the order given (from_um first),
    nominal depths (stage displacement into the sample) converted with the profile's Z direction."""
    d = study.get("depth", {})
    sgn, _ = into_sign(prof)
    a, b, st = float(d.get("from_um", -5.0)), float(d.get("to_um", 20.0)), float(d.get("step_um", 1.0))
    derived = {k: v for k, v in study.items() if k not in ("depth", "kind")}
    derived.update(kind="map", map={"z_um": [sgn * a, sgn * b, st], "snake": False})
    for k in ("engine", "swift"):
        if k in d:
            derived["map"][k] = d[k]
    return derived


def scan_params(study: dict, prof: dict) -> dict:
    sc = dict(SCAN_DEFAULTS)
    sc.update((study.get("focus") or {}).get("scan") or {})
    sc.setdefault("line_cm1", prof["checks"]["si_cm1"])
    return sc


def scan_offsets(sc: dict) -> list[float]:
    """Z offsets of a focus scan: -range/2 .. +range/2 in steps, 0 included."""
    half, step = float(sc["range_um"]) / 2, float(sc["step_um"])
    n = int(math.floor(half / step + 1e-9))
    return [round(i * step, 6) for i in range(-n, n + 1)]


def scan_seconds(study: dict, prof: dict) -> float:
    sc = scan_params(study, prof)
    t = prof["timing"]
    return len(scan_offsets(sc)) * (t["move_s"] + float(sc["exposure_s"]) + t["readout_s"] + 2 * t["command_s"])


def magnification(objective) -> float:
    """100 for 'x100' or '100x LWD'; 100 when unknown."""
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", str(objective or ""))
    return float(m.group(1)) if m else 100.0


def spot_params(study: dict, prof: dict, objective=None) -> dict:
    """A spot study's settings; the Raman scan's range and step scale with the objective (x100: 4 µm in 0.5 µm
    steps; x10: 40 µm in 5 µm steps)."""
    obj = objective if objective is not None else (study.get("setup") or {}).get("objective")
    k = 100.0 / magnification(obj)
    sp = {"method": "auto", "feature_um": None, "refine": True, "range_um": round(4.0 * k, 3),
          "step_um": round(0.5 * k, 3), "line_cm1": prof["checks"]["si_cm1"], "window_cm1": 30.0,
          "exposure_s": 0.3, "acq_exposure_s": 2.0, "remember": True}
    sp.update(study.get("spot") or {})
    return sp


def spot_spectrum_study(study: dict, prof: dict) -> dict:
    sp = spot_params(study, prof)
    return {"kind": "spectrum", "sample": study.get("sample", {}), "setup": study.get("setup", {}),
            "acquire": {"center_cm1": sp["line_cm1"], "exposure_s": max(sp["exposure_s"], sp["acq_exposure_s"]),
                        "accumulations": 1, "spike": "none"}}


GUIDE_DEFAULTS = {"mode": "reach", "window_cm1": 30.0, "exposure_s": 0.2, "max_s": 120.0, "stable_n": 2,
                  "drop_frac": 0.1}


def alignment_check_study(study: dict, prof: dict) -> dict:
    """The measurement behind an alignment check: a calibration-like Si spectrum with the laser's fixed settings
    from the profile (alignment.settings.<laser>), which the study's own setup and acquire may complete."""
    s = dict(study.get("setup", {}))
    k = laser_key(prof, s.get("laser_nm")) if s.get("laser_nm") is not None else None
    fixed = dict(prof.get("alignment", {}).get("settings", {}).get(k, {})) if k else {}
    setup = {key: fixed[key] for key in ("grating", "filter", "objective", "hole_um", "slit_um") if key in fixed}
    setup.update(s)
    acq = {"center_cm1": prof["checks"]["si_cm1"], "exposure_s": fixed.get("exposure_s", 1.0),
           "accumulations": int(fixed.get("accumulations", 2))}
    acq.update(study.get("acquire", {}))
    acq["spike"] = acq.get("spike", "double" if int(acq["accumulations"]) >= 2 else "labspec")
    return {"kind": "calibration", "sample": study.get("sample") or {"name": "Si reference"}, "setup": setup,
            "acquire": acq, "save_native": study.get("save_native", True),
            "process": {"peaks": [{"name": "Si", "near_cm1": prof["checks"]["si_cm1"], "window_cm1": 30}]}}


def focus_spectrum_study(study: dict, prof: dict) -> dict:
    """The acquisitions of a focus scan, as a spectrum study (for validation and the range)."""
    sc = scan_params(study, prof)
    return {"kind": "spectrum", "sample": study.get("sample", {}), "setup": study.get("setup", {}),
            "acquire": {"center_cm1": sc["line_cm1"], "exposure_s": sc["exposure_s"], "accumulations": 1,
                        "spike": "none"}}


def guide_params(study: dict, prof: dict) -> dict:
    g = dict(GUIDE_DEFAULTS)
    g.update(study.get("guide", {}))
    g.setdefault("line_cm1", prof["checks"]["si_cm1"])
    return g


def guide_spectrum_study(study: dict, prof: dict) -> dict:
    """The acquisitions of a guided adjustment, as a spectrum study (for validation and the range)."""
    g = guide_params(study, prof)
    return {"kind": "spectrum", "sample": study.get("sample", {}), "setup": study.get("setup", {}),
            "acquire": {"center_cm1": g.get("center_cm1", g["line_cm1"]), "exposure_s": g["exposure_s"],
                        "accumulations": 1, "spike": "none"}}


def estimate(study: dict, prof: dict) -> dict:
    """{minutes, points, windows, seconds: {setup, per_point, total}}: an upper-end estimate from the profile's
    timing (measured values replace the defaults once the instrument has run)."""
    t = prof["timing"]
    if study.get("kind") == "move":
        m = study.get("move", {})
        d = math.hypot(float(m.get("x_um", 0) or 0), float(m.get("y_um", 0) or 0))
        total = 3 * t["command_s"] + t["xy_settle_s"] + d / t["xy_um_per_s"] + t["move_s"] + \
            (t["objective_s"] if (study.get("setup") or {}).get("objective") else 0) + \
            (float(prof.get("video", {}).get("settle_s", 1.5)) + 5 if m.get("snapshot", True) else 0)
        return {"minutes": round(total / 60.0, 2), "points": 0, "windows": 0,
                "seconds": {"setup": 0.0, "per_acquisition": 0.0, "total": round(total, 1)}}
    if study.get("kind") == "depth":
        return estimate(depth_as_map(study, prof), prof)
    if study.get("kind") == "spot":
        sp = spot_params(study, prof)
        base = estimate(spot_spectrum_study(study, prof), prof)
        total = base["seconds"]["setup"] + 15.0 + float(sp["acq_exposure_s"])
        n = 0
        if sp["method"] == "raman" or sp.get("feature_um") is not None:
            n = len(axis_values([-sp["range_um"] / 2, sp["range_um"] / 2, sp["step_um"]])) ** 2
            total += n * (t["command_s"] * 2 + t["xy_settle_s"] + float(sp["exposure_s"]) + t["readout_s"]) + 10
        return {"minutes": round(total / 60.0, 2), "points": n, "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "per_acquisition": base["seconds"]["per_acquisition"],
                            "total": round(total, 1)}}
    if study.get("kind") == "focus":
        base = estimate(focus_spectrum_study(study, prof), prof)
        total = base["seconds"]["setup"] + scan_seconds(study, prof)
        return {"minutes": round(total / 60.0, 2), "points": len(scan_offsets(scan_params(study, prof))),
                "windows": 1, "seconds": {"setup": base["seconds"]["setup"],
                                          "per_acquisition": base["seconds"]["per_acquisition"],
                                          "total": round(total, 1)}}
    if study.get("kind") == "alignment":
        base = estimate(alignment_check_study(study, prof), prof)
        a = study.get("alignment", {})
        n = 1 + (int(prof["alignment"].get("max_retries", 1)) if a.get("guided", True) else 0)
        total = base["seconds"]["total"] * n + (guide_params({"guide": {}}, prof)["max_s"]
                                                if a.get("focus_guide") else 0.0) + 15.0
        return {"minutes": round(total / 60.0, 2), "points": 1, "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "per_acquisition": base["seconds"]["per_acquisition"],
                            "total": round(total, 1)}, "note": "with one AutoAlignment retry, if needed; the "
                                                               "operator's time for AutoAlignment is not included"}
    if study.get("kind") == "guide":
        base = estimate(guide_spectrum_study(study, prof), prof)
        total = base["seconds"]["setup"] + float(guide_params(study, prof)["max_s"])
        return {"minutes": round(total / 60.0, 2), "points": 1, "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "per_acquisition": base["seconds"]["per_acquisition"],
                            "total": round(total, 1)}, "note": "at most; it ends as soon as the goal is met"}
    laser = laser_nm(study, prof)
    rng = acq_range(study, prof, laser)
    pts = positions(study)
    s = study.get("setup", {})
    setup = t["command_s"] * 2
    setup += t["laser_s"] if s.get("laser_nm") is not None else 0
    setup += t["grating_s"] if s.get("grating") is not None else 0
    setup += t["objective_s"] if s.get("objective") is not None else 0
    setup += t["move_s"] * sum(1 for k in ("filter", "hole_um", "slit_um") if s.get(k) is not None)
    focus = study.get("focus", {})
    af = focus.get("autofocus")
    every = int(focus.get("every", 0) or 0)
    acq_s = acquire_seconds(study, prof, rng)
    total = setup
    if map_engine(study, prof) == "native":
        acc_ = int(study.get("acquire", {}).get("accumulations", 1))
        ex_ = float(study.get("acquire", {}).get("exposure_s", 1.0))
        per = ex_ * acc_ + float(prof.get("mapping", {}).get("native_point_s", 0.05))
        total += len(pts) * per + 0.02 * len(pts) + 20.0       # the map, then writing it out
        return {"minutes": round(total / 60.0, 2), "points": len(pts), "windows": 1, "engine": "native",
                "seconds": {"setup": round(setup, 1), "per_acquisition": round(per, 3), "total": round(total, 1)}}
    prev = (0.0, 0.0)
    for k, p in enumerate(pts):
        dist = math.hypot(p["x_um"] - prev[0], p["y_um"] - prev[1])
        if dist > 0:
            total += t["command_s"] + t["xy_settle_s"] + dist / t["xy_um_per_s"]
        prev = (p["x_um"], p["y_um"])
        if p["dz_um"]:
            total += t["move_s"]
        if af and (k == 0 or (every and k % every == 0)):
            total += scan_seconds(study, prof) if af == "scan" else t["autofocus_s"]
        total += acq_s
    vd = study.get("video") or {}
    v = prof.get("video", {})
    shots = int(bool(vd.get("before", study.get("kind") == "calibration"))) + int(bool(vd.get("after")))
    total += shots * (float(v.get("settle_s", 1.5)) + 5.0 +
                      (float(v.get("laser_s", 1.0)) if vd.get("with_laser") else 0.0))
    if study.get("kind") == "series":
        total += max(0.0, float(study.get("series", {}).get("interval_s", 0)) * (len(pts) - 1)
                     - acq_s * (len(pts) - 1)) if len(pts) > 1 else 0.0
    return {"minutes": round(total / 60.0, 2), "points": len(pts), "windows": rng.get("windows"),
            "seconds": {"setup": round(setup, 1), "per_acquisition": round(acq_s, 2), "total": round(total, 1)}}


def inputs(study: dict, prof: dict, max_steps: int = 40) -> dict:
    """The bridge commands of one study, without running: setup, then the first points' commands."""
    if study.get("kind") == "depth":
        inner = inputs(depth_as_map(study, prof), prof, max_steps)
        inner["note"] = "a depth profile: Z offsets from the focus (stage.z_into_sample gives the direction)"
        return inner
    if study.get("kind") == "move":
        m = study.get("move", {})
        steps = [{"verb": "status"}]
        if (study.get("setup") or {}).get("objective"):
            steps.append({"verb": "move", "motor": prof["motors"]["objective"], "mode": "label",
                          "value": study["setup"]["objective"]})
        steps += [{"verb": "move_xy", "x_um": m.get("x_um", 0), "y_um": m.get("y_um", 0), "relative_to": "home"},
                  {"verb": "move", "motor": prof["motors"]["z"], "value": f"focus{float(m.get('dz_um', 0) or 0):+g}",
                   "why": "Z relative to the focus"}]
        if m.get("snapshot", True):
            steps.append({"verb": "video", "laser_s": 0})
        return {"laser_nm": None, "range": None, "points": 0, "steps": steps, "steps_shown": len(steps),
                "estimate": estimate(study, prof), "note": "no laser"}
    if study.get("kind") == "focus":
        sc = scan_params(study, prof)
        laser = laser_nm(study, prof)
        steps = [{"verb": "status"}] + setup_steps(study, prof) + [{"verb": "status"}, {"verb": "confirm"}]
        for dz in scan_offsets(sc)[:max_steps]:
            steps += [{"verb": "move", "motor": prof["motors"]["z"], "value": f"focus{dz:+g}"},
                      {"verb": "acquire", "exposure_s": sc["exposure_s"], "center_cm1": sc["line_cm1"]}]
        steps.append({"verb": "move", "motor": prof["motors"]["z"], "value": "the best Z (parabola through the top)"})
        return {"laser_nm": laser, "range": acq_range(focus_spectrum_study(study, prof), prof, laser),
                "points": len(scan_offsets(sc)), "steps": steps, "steps_shown": len(steps),
                "estimate": estimate(study, prof)}
    if study.get("kind") == "alignment":
        chk = alignment_check_study(study, prof)
        inner = inputs(chk, prof, max_steps)
        a = study.get("alignment", {})
        inner["steps"].append({"verb": "notice", "why": "only if the check fails and guided: the operator runs "
                                                        "AutoAlignment in LabSpec (checklist), then the check repeats"})
        inner["steps"].append({"verb": "video", "why": "a snapshot (with the laser if the profile names the "
                                                       "shutter): the beam's offset after alignment"})
        if a.get("focus_guide"):
            inner["steps"].insert(1, {"verb": "guide", "mode": "maximize", "why": "the operator focuses on the Si "
                                                                                  "until ls6t says STOP"})
        inner["check_study"] = chk
        inner["estimate"] = estimate(study, prof)
        return inner
    if study.get("kind") == "guide":
        g = guide_params(study, prof)
        laser = laser_nm(study, prof)
        rng = acq_range(guide_spectrum_study(study, prof), prof, laser)
        steps = [{"verb": "status"}] + setup_steps(study, prof) + [
            {"verb": "status"}, {"verb": "confirm"},
            {"verb": "guide", "text": g.get("instruction"), "mode": g["mode"], "target": g.get("target"),
             "line_cm1": g["line_cm1"], "exposure_s": g["exposure_s"], "max_s": g["max_s"],
             "why": "short acquisitions in LabSpec; the panel shows the signal and STOP when the goal is met"}]
        return {"laser_nm": laser, "range": rng, "points": 1, "steps": steps, "steps_shown": len(steps),
                "estimate": estimate(study, prof)}
    laser = laser_nm(study, prof)
    rng = acq_range(study, prof, laser)
    steps = [{"verb": "status", "why": "record the instrument state before"}]
    steps += setup_steps(study, prof)
    steps.append({"verb": "status", "why": "read back the setup"})
    focus = study.get("focus", {})
    acq = acquire_step(study, prof, rng)
    pts = positions(study)
    for k, p in enumerate(pts):
        if len(steps) >= max_steps:
            break
        if p["x_um"] or p["y_um"] or k > 0:
            steps.append({"verb": "move_xy", "x_um": p["x_um"], "y_um": p["y_um"], "relative_to": "start"})
        if p["dz_um"]:
            steps.append({"verb": "move", "motor": prof["motors"]["z"], "mode": "value",
                          "value": f"start{p['dz_um']:+g}", "why": "Z offset from the operator's focus"})
        if focus.get("autofocus") and (k == 0 or (focus.get("every") and k % int(focus["every"]) == 0)):
            steps.append({"verb": "autofocus", "how": focus["autofocus"]})
        steps.append(dict(acq, point=p["i"]))
    return {"laser_nm": laser, "range": rng, "points": len(pts), "steps": steps[:max_steps],
            "steps_shown": min(len(steps), max_steps), "estimate": estimate(study, prof)}
