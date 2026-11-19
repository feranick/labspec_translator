"""plan.py — a study as bridge commands: the setup moves, the points, the acquisitions; and the time estimate.
Version: 2026.11.19.2

Everything here is computed from the study and the profile alone: wavelengths from Raman shifts, the number of
detector windows, the stage positions (always offsets from where the operator left the stage, Z from the
operator's focus), the order of the points and how long it should take. The runner sends exactly these commands.
"""
from __future__ import annotations

import math

from .profile import laser_key, shift_to_nm

SPIKE_MODES = {"labspec": 0, "none": 100, "single": 200, "double": 300, "double_auto": 400}
AUTOFOCUS = ("laser", "raman", "video")


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
        acq = dict(acq, center_cm1=prof["checks"]["si_cm1"])
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


def estimate(study: dict, prof: dict) -> dict:
    """{minutes, points, windows, seconds: {setup, per_point, total}}: an upper-end estimate from the profile's
    timing (measured values replace the defaults once the instrument has run)."""
    t = prof["timing"]
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
    prev = (0.0, 0.0)
    for k, p in enumerate(pts):
        dist = math.hypot(p["x_um"] - prev[0], p["y_um"] - prev[1])
        if dist > 0:
            total += t["command_s"] + t["xy_settle_s"] + dist / t["xy_um_per_s"]
        prev = (p["x_um"], p["y_um"])
        if p["dz_um"]:
            total += t["move_s"]
        if af and (k == 0 or (every and k % every == 0)):
            total += t["autofocus_s"]
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
