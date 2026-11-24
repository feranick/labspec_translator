"""plan.py — a study as bridge commands: the setup moves, the points, the acquisitions; and the time estimate.

Everything here is computed from the study and the profile alone: wavelengths from Raman shifts, the number of
detector windows, the stage positions (always offsets from where the operator left the stage, Z from the
operator's focus), the order of the points and how long it should take. The runner sends exactly these commands.
"""
from __future__ import annotations

import math
import numpy as np

from . import shutter as SH
from .profile import laser_key, match_label, shift_to_nm

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


def window_centre(rng: dict) -> float | None:
    """The spectrometer's centre (nm) for a one-window acquisition; None for an extended range (several windows)."""
    if rng.get("center_nm") is not None:
        return float(rng["center_nm"])
    if rng.get("windows") == 1 and rng.get("from_nm") is not None and rng.get("to_nm") is not None:
        return round((float(rng["from_nm"]) + float(rng["to_nm"])) / 2, 4)
    return None


def acq_range(study: dict, prof: dict, laser: float | None) -> dict:
    """The acquisition range: {from_cm1, to_cm1, from_nm, to_nm, windows, single}. With center_cm1, one window
    (LabSpec's From = To); with range_cm1, LabSpec stitches as many windows as it takes (extended range)."""
    acq = study.get("acquire", {})
    grating = study.get("setup", {}).get("grating")
    win_nm = None
    if grating is not None:
        g = prof.get("gratings", {}).get(f"{float(grating):g}")
        win_nm = float(g["window_nm"]) if g and g.get("window_nm") else None
    if study.get("kind") == "filters" and not acq.get("range_cm1") and acq.get("center_cm1") is None:
        acq = dict(acq, center_cm1=filters_params(study, prof)["line_cm1"])
    if study.get("kind") == "cutoff" and not acq.get("range_cm1") and acq.get("center_cm1") is None:
        cp = cutoff_params(study, prof)
        acq = dict(acq, range_cm1=[cp["from_cm1"], cp["to_cm1"]])
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
            # A range that fits one window is acquired centred on it (From = To) and cut to From..To by ls6t
            # (2026-11-24: given From != To, LabSpec put From at the window's blue edge, the data on the CCD's edge,
            # and Si read 2.09 cm-1 off where centred windows needed no correction). acquire.range_placement
            # 'labspec' keeps LabSpec's way. The width: measured with this grating and laser, else the profile's.
            wl = None
            if grating is not None:
                try:
                    wl = (window_store().get(f"{float(grating):g}@{float(laser):g}") or {}).get("width_nm")
                except (OSError, ValueError, TypeError):
                    wl = None
            wl = float(wl) if wl else win_nm
            if acq.get("range_placement", "centre") == "centre" and wl and (b - a) <= 0.9 * wl:
                c = round((a + b) / 2, 4)
                out.update(single=True, windows=1, center_nm=c, from_nm=c, to_nm=c, crop_cm1=[lo, hi],
                           note="a range within one window: acquired centred on it (From = To), cut to the range "
                                "by ls6t")
        else:
            out["windows"] = None
        return out
    c = float(acq.get("center_cm1", 1000.0))
    out = {"center_cm1": c, "single": True, "windows": 1}
    if laser:
        nm = shift_to_nm(c, laser)
        # LabSpec's own parameters for one window: From = To = the centre (nm); the window it covers is given too
        out.update(from_nm=round(nm, 4), to_nm=round(nm, 4), center_nm=round(nm, 4),
                   note="one window: LabSpec takes From = To = its centre (from_nm, to_nm); window_nm is what it "
                        "covers")
        if win_nm:
            half = win_nm / 2
            out.update(from_cm1=round(1e7 / laser - 1e7 / (nm - half), 2),
                       to_cm1=round(1e7 / laser - 1e7 / (nm + half), 2),
                       window_nm=[round(nm - half, 2), round(nm + half, 2)])
    return out


GRATING_WIDTH_TOL = 0.2    # a window this much wider or narrower than the one measured before with the grating: stop


def window_store(new: dict | None = None) -> dict:
    """The width of one detector window, measured per grating and laser (LS6T_HOME/grating_windows.json,
    "<grating>@<laser nm>": {width_nm, center_nm, time}): learnt from acquisitions whose centre was right."""
    from . import store
    if new is not None:
        try:
            return store.write("grating_windows.json", new)
        except OSError:
            return new
    return store.read("grating_windows.json")


def grating_window_check(x, x_unit: str, laser_nm, grating, prof: dict, rng: dict, learn: bool = True) -> dict:
    """Does the spectrum's own axis fit what was asked? 2026-11-23 at the XploRA (637.75 nm): with 1800 set (and
    reported) the data read "-1016 to -974 nm" (header Spectro = -996.66 nm); with 1200, 646-728 nm, centred near 687
    instead of 659.66; a second study centred at 1500 cm-1 kept the first one's window. Stops on: impossible
    wavelengths; a single window centred more than a quarter of its width from the centre asked; a width more than
    20 % off the one measured before with this grating and laser (another grating than LabSpec reports). Warns when
    the profile's window_nm is off (the XploRA's 1200 gives 85-87 nm at 638 nm, the profile said 39). Learns the
    width when the centre is right. {ok, stop, warn, width_nm, center_nm, learned_nm}."""
    x = np.asarray(x, float)
    u = (x_unit or "nm").strip().lower().replace("cm-1", "1/cm").replace("cm^-1", "1/cm")
    if u in ("1/cm", "rel 1/cm", "raman shift"):
        nm = shift_to_nm(x, float(laser_nm)) if laser_nm else None
    elif u == "ev":
        nm = 1239.841984 / x
    else:
        nm = x
    out: dict = {"ok": True, "stop": [], "warn": []}
    if nm is None or nm.size < 2:
        return out
    if not np.all(np.isfinite(nm)) or float(np.min(nm)) <= 0:
        out["stop"].append(f"the spectrum's wavelengths are impossible ({float(np.nanmin(nm)):.1f} to "
                           f"{float(np.nanmax(nm)):.1f} nm)")
        out["ok"] = False
        return out
    lo, hi = float(np.min(nm)), float(np.max(nm))
    width, center = hi - lo, (lo + hi) / 2
    out.update(width_nm=round(width, 2), center_nm=round(center, 2))
    if not rng.get("single", True):
        a, b = sorted((float(rng["from_nm"]), float(rng["to_nm"]))) if rng.get("from_nm") is not None else (lo, hi)
        if lo > a + 0.1 * (b - a) or hi < b - 0.1 * (b - a):
            out["stop"].append(f"the spectrum covers {lo:.1f}-{hi:.1f} nm, {a:.1f}-{b:.1f} nm was asked")
        out["ok"] = not out["stop"]
        return out
    asked = rng.get("center_nm") or rng.get("from_nm")
    centred = asked is None or abs(center - float(asked)) <= 0.25 * width
    if not centred:
        out["stop"].append(f"the window is centred at {center:.1f} nm, {float(asked):.1f} nm was asked")
    g = f"{float(grating):g}" if grating not in (None, "") else None
    key = f"{g}@{float(laser_nm):g}" if g and laser_nm else None
    store = window_store() if key else {}
    known = store.get(key)
    if known and known.get("width_nm"):
        out["learned_nm"] = known["width_nm"]
        if abs(width / float(known["width_nm"]) - 1) > GRATING_WIDTH_TOL:
            others = sorted((abs(width / float(v["width_nm"]) - 1), k.split("@")[0]) for k, v in store.items()
                            if k.endswith(f"@{float(laser_nm):g}") and v.get("width_nm"))
            like = others[0][1] if others and others[0][0] < GRATING_WIDTH_TOL else None
            out["stop"].append(f"the window is {width:.1f} nm wide, {float(known['width_nm']):.1f} nm was measured "
                               f"with grating {g} before" + (f": it looks like grating {like}" if like and like != g
                                                             else ""))
    pg = prof.get("gratings", {}).get(g) if g else None
    if pg and pg.get("window_nm") and abs(width / float(pg["window_nm"]) - 1) > GRATING_WIDTH_TOL:
        out["warn"].append(f"grating {g}: the window is {width:.1f} nm wide here, the profile says "
                           f"{float(pg['window_nm']):g} nm (estimates and extended-range windows use it): "
                           f"ls6t profile set gratings.{g}.window_nm {width:.1f}")
    if learn and key and centred and not out["stop"] and not known:
        store[key] = {"width_nm": round(width, 2), "center_nm": round(center, 2),
                      "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S")}
        window_store(store)
        out["learned_now"] = True
    out["ok"] = not out["stop"]
    return out


def grating_limit_nm(prof: dict, grating) -> float | None:
    """The longest wavelength a grating can send to the detector, in nm: in first order no grating diffracts beyond
    twice its groove spacing (2e6 / G nm: 2400 gr/mm -> 833 nm, 1800 -> 1111, 1200 -> 1667, 600 -> 3333), and the
    spectrometer's geometry stops it earlier; a lower practical limit for this instrument can be given per grating
    (profile gratings.<G>.max_nm, e.g. from LabSpec's SpMax once its unit is known: TESTING Q45)."""
    try:
        g = float(grating)
    except (TypeError, ValueError):
        return None
    if g <= 0:
        return None
    lim = 2e6 / g
    gp = prof.get("gratings", {}).get(f"{g:g}") or {}
    for mx in (gp.get("max_nm"), (gp.get("spectro_nm") or [None, None])[1]):
        if mx:
            lim = min(lim, float(mx))
    return lim


def grating_min_nm(prof: dict, grating) -> float | None:
    """The shortest wavelength the spectrometer reaches with a grating: not known. LabSpec's SpMin reads 100 for
    every grating on the XploRA (2026-11-20), not a plausible limit (a placeholder or another unit), so it is shown
    but not applied; a measured lower limit can be given as gratings.<G>.min_nm."""
    try:
        gp = prof.get("gratings", {}).get(f"{float(grating):g}") or {}
    except (TypeError, ValueError):
        return None
    lo = gp.get("min_nm")
    return float(lo) if lo else None


def grating_reach(prof: dict, grating, laser_nm) -> dict | None:
    """What a grating can reach with a laser: the limit in nm and the largest Raman shift (cm-1) below it."""
    lim = grating_limit_nm(prof, grating)
    if lim is None or not laser_nm:
        return None
    laser_nm = float(laser_nm)
    gp = prof.get("gratings", {}).get(f"{float(grating):g}") or {}
    sp = (gp.get("spectro_nm") or [None, None])[1]
    basis = ("LabSpec's SpMax for this grating" if sp and abs(lim - float(sp)) < 1e-6 else
             "profile max_nm" if lim < 2e6 / float(grating) - 1e-6 else "first-order limit (2 / G)")
    out = {"grating": f"{float(grating):g}", "laser_nm": laser_nm, "limit_nm": round(lim, 1), "basis": basis}
    out["max_shift_cm1"] = round(1e7 / laser_nm - 1e7 / lim, 0) if laser_nm < lim else None
    return out


def acq_mode(study: dict) -> int:
    """LabSpec's Acq Mode: ACQ_SPECTRUM + ACQ_AUTO_SHOW + spike removal + ICS (intensity correction) + dark."""
    acq = study.get("acquire", {})
    mode = 0 + 10 + SPIKE_MODES[acq.get("spike", "labspec")]
    ics = acq.get("ics", "labspec")
    if ics is True:
        mode += ACQ_ICS
    elif ics is False:
        mode += ACQ_NO_ICS
    dark = acq.get("dark", "labspec")
    if dark is True:
        mode += 2000000
    elif dark is False:
        mode += 1000000
    return mode


ACQ_NO_ICS, ACQ_ICS = 100000, 200000      # LabSpec's Acq mode digits (ActiveX manual)


_ICS_CACHE: dict = {}


def ics_files(prof: dict) -> set | None:
    """The (laser label, grating number) pairs LabSpec has an ICS response for: its
    Parameters\\ACQUISITION\\Responses\\Response_<detector>_<laser label>_<grating label>.l6s files (HORIBA's ICS script
    makes them per laser and grating). None when the folder is not found (not on the instrument PC)."""
    from . import labspec_ini
    try:
        ini = labspec_ini.find((prof.get("discovered") or {}).get("labspec_path"))
    except Exception:  # noqa: BLE001
        ini = None
    if ini is None:
        return None
    d = ini.parent / "Responses"
    if not d.is_dir():
        return None
    key = (str(d), d.stat().st_mtime)
    if key not in _ICS_CACHE:
        _ICS_CACHE.clear()
        _ICS_CACHE[key] = {f.name.lower() for f in d.glob("Response_*.l6s")}
    return _ICS_CACHE[key]


def ics_off(prof: dict, laser, grating) -> bool:
    """True where LabSpec has no ICS (intensity correction) file for this grating and laser (profile ics.off_for,
    entries "<grating>@<laser nm>"): with ICS on, LabSpec stops every acquisition there with a modal "The required
    ICS correction file does not exist ... Error Code=2070" until someone presses OK (2026-11-23: 2400 gr/mm with
    637.75 nm), and the bridge waits inside the acquisition meanwhile."""
    import re as _re
    if laser in (None, "") or grating in (None, ""):
        return False
    mg = _re.match(r"\s*(\d+(?:\.\d+)?)", str(grating))
    if not mg:
        return False
    key = f"{float(mg.group(1)):g}@{float(laser):g}"
    if key in set((prof.get("ics") or {}).get("off_for") or []):
        return True
    if not (prof.get("ics") or {}).get("auto", True):
        return False
    pairs = ics_files(prof)
    if pairs is None:
        return False
    lab = next((str(v.get("label", "")).lower() for v in (prof.get("lasers") or {}).values()
                if abs(float(v["nm"]) - float(laser)) < 0.5), None)
    if not lab:
        return False
    g = int(round(float(mg.group(1))))
    pat = _re.compile(rf"^response_.*_{_re.escape(lab)}_{g}(?:\D.*)?\.l6s$")
    return not any(pat.match(n) for n in pairs)          # no ICS response for this laser and grating


def setup_steps(study: dict, prof: dict) -> list[dict]:
    """The moves that set the instrument up. The filter goes first, so the attenuation asked for is in place
    before anything else changes (the laser shutter opens only during acquisitions)."""
    s = study.get("setup", {})
    mot = prof["motors"]
    steps = []
    if s.get("filter") is not None:
        steps.append({"verb": "move", "motor": mot["filter"], "mode": "label", "value": str(s["filter"]),
                      "why": "filter (laser power at the sample)", "role": "filter"})
    if s.get("laser_nm") is not None:
        k = laser_key(prof, s["laser_nm"])
        steps.append({"verb": "move", "motor": mot["laser"], "mode": "value",
                      "value": float(prof["lasers"][k]["nm"]) if k else float(s["laser_nm"]), "why": "laser",
                      "role": "laser_nm"})
    if s.get("grating") is not None:
        steps.append({"verb": "move", "motor": mot["grating"], "mode": "value", "value": float(s["grating"]),
                      "why": "grating", "role": "grating"})
    if s.get("objective") is not None:
        obj = match_label(s["objective"], prof.get("objectives", []))[0] or str(s["objective"])
        steps.append({"verb": "move", "motor": mot["objective"], "mode": "label", "value": obj,
                      "why": "objective" + (" (manual turret: LabSpec asks the operator)"
                                            if prof.get("manual_objective") else ""), "role": "objective"})
    for role in ("laser_pol", "raman_pol"):
        if s.get(role) is not None and mot.get(role):
            lab = match_label(s[role], (prof.get("polarizers") or {}).get(role, []))[0] or str(s[role])
            steps.append({"verb": "move", "motor": mot[role], "mode": "label", "value": lab,
                          "why": role.replace("_", " ") + "ariser", "role": role})
    if s.get("hole_um") is not None:
        steps.append({"verb": "move", "motor": mot["hole"], "mode": "value", "value": float(s["hole_um"]),
                      "why": "confocal hole", "role": "hole_um"})
    if s.get("slit_um") is not None:
        steps.append({"verb": "move", "motor": mot["slit"], "mode": "value", "value": float(s["slit_um"]),
                      "why": "slit", "role": "slit_um"})
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


# Typical working distances (µm) of metallurgical microscope objectives by magnification (Olympus MPlan N class, as on
# HORIBA's XploRA): the free space between the objective and the sample at focus. A profile can give its own
# (objectives_wd_um: {"x100_VIS": 210, ...}).
WD_BY_MAG_UM = {100: 210.0, 50: 350.0, 20: 1300.0, 10: 10600.0, 5: 20000.0}


def objective_mag(objective) -> int | None:
    import re
    m = re.search(r"(\d+)", str(objective or ""))
    return int(m.group(1)) if m else None


def working_distance_um(prof: dict, objective) -> float | None:
    """The objective's working distance (µm): the profile's objectives_wd_um, else the typical one for its
    magnification; with no objective known, the shortest of the profile's objectives (the cautious one)."""
    wd = prof.get("objectives_wd_um") or {}
    if objective is not None:
        if objective in wd:
            return float(wd[objective])
        mag = objective_mag(objective)
        if mag in WD_BY_MAG_UM:
            return WD_BY_MAG_UM[mag]
        return None
    known = [working_distance_um(prof, o) for o in prof.get("objectives", [])]
    known = [k for k in known if k]
    return min(known) if known else WD_BY_MAG_UM[100]


def z_limits(prof: dict, objective, purpose: str | None = None) -> dict:
    """How far Z may go from the focus with this objective. Toward the objective (deeper into the sample): a fraction
    of the working distance (limits.z_toward_wd_fraction, 0.1: x100 ~21 µm, x50 ~35 µm), never more than
    limits.max_dz_um; away from it: limits.max_dz_um. Until stage.z_into_sample says which way is toward, both
    directions get the toward limit. focus_valid_xy_um: how far the stage may go in XY before the focus counts as
    stale for moves toward the sample (limits.focus_valid_xy_um per magnification; none for long working
    distances)."""
    lim = prof.get("limits", {})
    max_dz = float(lim.get("max_dz_um", 100.0))
    wd = working_distance_um(prof, objective)
    frac = float(lim.get("z_toward_wd_fraction", 0.1))
    away = float(lim.get("max_dz_um", 100.0))
    if purpose == "travel":
        # moving away from the sample to travel or to search the focus from below: away up to limits.retract_um
        away = max(away, retract_um(prof))
    if purpose == "depth":
        # depth profiles into transparent samples (polymers, coatings) go further, after the operator confirms the
        # gap in LabSpec (runner): limits.depth_toward_wd_fraction, 0.3 by default (x100: ~63 µm of ~210 µm)
        frac = float(lim.get("depth_toward_wd_fraction", 0.3))
    toward = min(max_dz, frac * wd) if wd else min(max_dz, frac * WD_BY_MAG_UM[100])
    sign, known = into_sign(prof)
    fv = lim.get("focus_valid_xy_um") or {"100": 200.0, "50": 400.0, "20": 2000.0}
    mag = objective_mag(objective) if objective is not None else 100
    valid = fv.get(str(mag)) if mag is not None else fv.get("100")
    return {"objective": objective, "purpose": purpose, "working_distance_um": wd, "toward_um": round(toward, 1),
            "away_um": away, "toward_sign": sign if known else None,
            "focus_valid_xy_um": float(valid) if valid is not None else None}


def coarse_focus_step_um(prof: dict, objective) -> float:
    """The largest step of a focus search from below that cannot jump over the focus: about half the objective's
    depth of field (the measured axial FWHM when there is one, else by magnification: x100 2, x50 4, x20 10, x10 25,
    x5 50 µm)."""
    from . import perf
    try:
        rec = [v for k, v in (perf.status().get("axial") or {}).items() if str(objective) in str(k)]
        fw = [float(r["latest"]["fwhm_um"]) for r in rec if r.get("latest", {}).get("fwhm_um")]
        if fw:
            return max(0.5, min(fw) / 2)
    except Exception:  # noqa: BLE001 - the table below then
        pass
    mag = objective_mag(objective) if objective is not None else 100
    return {100: 2.0, 50: 4.0, 20: 10.0, 10: 25.0, 5: 50.0}.get(mag or 100, 2.0)


AF_ASSUMED = {"z_speed_um_s": 200.0, "stop_s": 0.5, "poll_s": 0.15}   # until measured on the instrument (T6.8)


def af_motion(prof: dict) -> dict:
    """Z's speed during LabSpec's autofocus, the bridge's poll interval and how long a stop takes: the largest
    measured (LS6T_HOME/autofocus_motion.json, from every guarded autofocus), else profile autofocus.* , else
    AF_ASSUMED (cautious); and the overshoot margin speed x (poll + stop) x 1.25 by which the trip point is set
    earlier, so the stage stops at the limit rather than starting to stop there."""
    import json
    from . import config
    meas = {}
    try:
        meas = json.loads((config.home() / "autofocus_motion.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cfg = prof.get("autofocus") or {}
    out = {}
    for k in ("z_speed_um_s", "stop_s", "poll_s"):
        if meas.get(k):
            out[k], out[k + "_from"] = float(meas[k]), "measured"
        elif cfg.get(k):
            out[k], out[k + "_from"] = float(cfg[k]), "profile"
        else:
            out[k], out[k + "_from"] = AF_ASSUMED[k], "assumed"
    out["margin_um"] = round(1.25 * out["z_speed_um_s"] * (out["poll_s"] + out["stop_s"]), 1)
    out["runs_measured"] = int(meas.get("runs", 0))
    return out


def record_af_motion(r: dict) -> None:
    """Keeps the largest Z speed, poll interval and stop time seen in a guarded autofocus (af_motion)."""
    import json
    from . import config

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    new = {k: f(r.get(k)) for k in ("z_speed_um_s", "stop_s", "poll_s")}
    if not any(v for v in new.values()):
        return
    p = config.home() / "autofocus_motion.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    for k, v in new.items():
        if v:
            cur[k] = max(float(cur.get(k) or 0.0), v)
    cur["runs"] = int(cur.get("runs", 0)) + 1
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    except OSError:
        pass


def retract_um(prof: dict) -> float:
    """How far Z goes away from the sample for a retract-travel-return (limits.retract_um, 2000 µm by default;
    away only, its own limit, not max_dz_um)."""
    return float(prof.get("limits", {}).get("retract_um", 2000.0))


def travel_mode(prof: dict, study: dict, dist_um: float, objective) -> str:
    """'retract' (Z away by retract_um, travel, Z back to the old focus minus the sample's unevenness) or 'direct'
    for an XY move of dist_um: as move.travel says, else 'retract' when sample.unevenness_um is given and the move
    goes beyond where the focus stays valid for the objective."""
    t = (study.get("move") or {}).get("travel")
    if t in ("retract", "direct"):
        return t
    un = (study.get("sample") or {}).get("unevenness_um")
    fv = z_limits(prof, objective)["focus_valid_xy_um"]
    return "retract" if un is not None and fv is not None and dist_um > fv else "direct"


def z_check(zl: dict, dz: float) -> str | None:
    """Why a Z offset from the focus is refused under z_limits, or None."""
    if dz is None or abs(dz) < 1e-9:
        return None
    s = zl["toward_sign"]
    obj = zl["objective"] or "the objective"
    if s is None:
        if abs(dz) > zl["toward_um"]:
            return (f"{dz:+g} µm from the focus: above {zl['toward_um']:g} µm, the limit toward the sample for {obj} "
                    "(applied both ways until stage.z_into_sample says which Z direction approaches the objective: "
                    "TESTING T3.17)")
        return None
    if dz * s > 0 and dz * s > zl["toward_um"]:
        key = "depth_toward_wd_fraction" if zl.get("purpose") == "depth" else "z_toward_wd_fraction"
        return (f"{dz:+g} µm from the focus is toward the sample by {abs(dz):g} µm: above {zl['toward_um']:g} µm for "
                f"{obj} (working distance ~{zl['working_distance_um']:g} µm; limits.{key})")
    if dz * s < 0 and abs(dz) > zl["away_um"]:
        return f"{dz:+g} µm from the focus, away from the sample: above limits.max_dz_um = {zl['away_um']:g} µm"
    return None


def toward(zl: dict, dz: float) -> bool:
    """Whether a Z offset approaches the sample (unknown direction: both count)."""
    s = zl["toward_sign"]
    return abs(dz) > 1e-9 if s is None else dz * s > 1e-9


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


def filters_params(study: dict, prof: dict) -> dict:
    fp = study.get("filters") or {}
    return {"line_cm1": float(fp.get("line_cm1", prof["checks"].get("si_cm1", 520.7))),
            "window_cm1": float(fp.get("window_cm1", 30.0)),
            "exposure_s": float(fp.get("exposure_s", 1.0)),
            "max_exposure_s": float(fp.get("max_exposure_s", min(30.0, prof["limits"]["max_exposure_s"]))),
            "labels": fp.get("labels")}


def filters_steps(study: dict, prof: dict, laser) -> list[dict]:
    """The filters a 'filters' study measures, strongest transmission first (the reference): those within the
    caps for the laser; each with the exposure that gives about the reference's counts (exposure x ref% / %),
    or none when that is above filters.max_exposure_s."""
    from .profile import filter_pct
    fp = filters_params(study, prof)
    caps = [float(prof["limits"].get("max_filter_pct", 100))]
    lk = laser_key(prof, laser) if laser else None
    if lk:
        caps.append(float(prof["lasers"][lk].get("max_filter_pct", 100)))
    if (study.get("sample") or {}).get("sensitive"):
        caps.append(float(prof["limits"].get("sensitive_max_filter_pct", 10)))
    cap = min(caps)
    labels = fp["labels"] or list(prof.get("filters", {}))
    rows = []
    for lab in labels:
        pct = prof.get("filters", {}).get(str(lab))
        pct = pct if pct is not None else filter_pct(lab)
        if pct is None or pct <= 0:
            continue
        if pct > cap + 1e-9:
            # listed with why, not measured (2026-11-23: a skipped filter had no note)
            rows.append({"label": str(lab), "nominal_pct": float(pct), "exposure_s": None,
                         "skip": f"above the cap for this laser ({cap:g} %)"})
            continue
        rows.append({"label": str(lab), "nominal_pct": float(pct)})
    rows.sort(key=lambda r: -r["nominal_pct"])
    usable = [r for r in rows if not r.get("skip")]
    if usable:
        ref = usable[0]["nominal_pct"]
        for r in usable:
            e = fp["exposure_s"] * ref / r["nominal_pct"]
            r["exposure_s"] = round(e, 3) if e <= fp["max_exposure_s"] else None
            if r["exposure_s"] is None:
                r["skip"] = (f"would need {e:.0f} s to match the reference's counts, more than filters.max_exposure_s "
                             f"({fp['max_exposure_s']:g} s)")
    return rows


def cutoff_params(study: dict, prof: dict) -> dict:
    cp = study.get("cutoff") or {}
    return {"from_cm1": float(cp.get("from_cm1", -100.0)), "to_cm1": float(cp.get("to_cm1", 400.0)),
            "light": cp.get("light", "white"),
            "laser_off": cp.get("laser_off", SH.default_laser_off(prof, (study.get("setup") or {}).get("laser_nm"))),
            "plateau_cm1": list(cp.get("plateau_cm1", [200.0, 380.0]))}


def already_set(step: dict, current: dict | None) -> bool:
    """Whether a set-up step's position is what LabSpec reports now (current: the bridge's status): then it is not
    sent (the runner skips it, the dry run marks it), and above all a manual objective is not asked for again."""
    if not current or not step.get("role"):
        return False
    from .profile import match_label, same_position
    cur = current.get(step["role"])
    if cur in (None, ""):
        return False
    if step["role"] == "objective":
        return same_position(cur, step["value"]) or match_label(str(step["value"]), [str(cur)])[0] is not None
    return same_position(cur, step["value"])


def marked_setup_steps(study: dict, prof: dict, current: dict | None) -> list[dict]:
    """setup_steps, each already in place (current) marked {already_set: true, note}: not sent at run time."""
    out = []
    for st in setup_steps(study, prof):
        if already_set(st, current):
            st = dict(st, already_set=True, note=f"already set ({current.get(st['role'])}): skipped, not sent")
        out.append(st)
    return out


def setup_parts(study: dict, prof: dict, current: dict | None = None) -> dict:
    """The set-up time by part (s), from the profile's timing: each motor the study sets (an upper value: LabSpec
    skips one already in place), the objective (on a manual turret, the operator's time to turn it), and the
    commands around them. Measured on the XploRA (2026-11-21): laser 1.6-2.8 s, grating 0.1-0.3 s once LabSpec has
    run a minute, filter ~1 s, pinhole 2-6 s, slit 2-3 s: set timing.* to them (ls6t profile set)."""
    t = prof["timing"]
    out = {"commands": round(t["command_s"] * 2, 2)}
    cost = {"laser_nm": ("laser", t["laser_s"]), "grating": ("grating", t["grating_s"]),
            "objective": ("objective", t["objective_s"]), "filter": ("filter", t["move_s"]),
            "hole_um": ("hole", t["move_s"]), "slit_um": ("slit", t["move_s"]),
            "laser_pol": ("laser_pol", t["move_s"]), "raman_pol": ("raman_pol", t["move_s"])}
    for st in setup_steps(study, prof):
        name, sec = cost.get(st.get("role"), (st.get("role"), t["move_s"]))
        out[name] = 0.0 if already_set(st, current) else sec
    return out


def settle_s(prof: dict, laser) -> float:
    """The pause after each spectrum before the next: timing.acquire_settle_s, or the laser's own lasers.<k>.settle_s
    if longer (2026-11-23: the XploRA's 637.75 nm laser stays in its mode with 5 s between acquisitions and hops by
    about 8 cm-1 with 0.5 s; 532 nm needs none)."""
    s = float((prof.get("timing") or {}).get("acquire_settle_s", 0.0) or 0.0)
    lk = laser_key(prof, laser) if laser is not None else None
    if lk:
        s = max(s, float(prof["lasers"][lk].get("settle_s") or 0.0))
    return s


HOLD_OPEN_DEFAULT_S = 5.0   # a laser marked mode_hops / unstable_multi without its own settle_s


def hold_open_s(prof: dict, laser) -> float:
    """The wait after the shutter hold opens the shutter, before the first spectrum (the laser settling in its mode:
    2026-11-24, the 637.75 nm laser's first held spectrum had its laser line 9.9 cm-1 wide, then about 6):
    lasers.<k>.hold_open_s; by default the laser's settle_s (the time it needs after a shutter cycle), or 5 s for a
    laser marked mode_hops or unstable_multi, else none."""
    lk = laser_key(prof, laser) if laser is not None else None
    if not lk:
        return 0.0
    L = prof["lasers"][lk]
    if L.get("hold_open_s") is not None:
        return max(0.0, float(L["hold_open_s"]))
    st = float(L.get("settle_s") or 0.0)
    if st > 0:
        return st
    return HOLD_OPEN_DEFAULT_S if (L.get("mode_hops") or L.get("unstable_multi")) else 0.0


def hold_shutter(study: dict, prof: dict, laser) -> bool:
    """Whether a study's spectra are taken with the laser shutter held open between them (acquire.shutter 'hold', or
    the laser's lasers.<k>.hold_shutter unless the study says 'auto'), as HORIBA's mapping scripts do. A series whose
    spectra are further apart than the laser's settle_s (the time it needs to recover after LabSpec closes and opens
    the shutter: 5 s for the XploRA's 637.75 nm) is not held: the beam is then off the sample between spectra."""
    a = (study.get("acquire") or {}).get("shutter")
    if a in ("hold", "auto"):
        return a == "hold"
    lk = laser_key(prof, laser) if laser is not None else None
    if not (lk and prof["lasers"][lk].get("hold_shutter")):
        return False
    if study.get("kind") == "series":
        # the time between one spectrum's end and the next one's start, with each acquisition's real duration
        # (exposure, readout, LabSpec's overheads): 6 s asked with 1 s exposures left about 3.5 s, not 5 (2026-11-24)
        try:
            busy = acquire_seconds(study, prof, acq_range(study, prof, laser))
        except (KeyError, ValueError, TypeError):
            acq = study.get("acquire") or {}
            ex = acq.get("exposure_s", 1.0)
            busy = (float(ex) if isinstance(ex, (int, float)) else 0.0) * int(acq.get("accumulations", 1) or 1)
        gap = float((study.get("series") or {}).get("interval_s", 0) or 0) - busy
        rec = float(prof["lasers"][lk].get("settle_s") or 0)
        if rec > 0 and gap >= rec:
            return False
    return True


def estimate(study: dict, prof: dict, current: dict | None = None) -> dict:
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
        return estimate(depth_as_map(study, prof), prof, current)
    if study.get("kind") == "filters":
        base = estimate(dict(study, kind="spectrum", filters=None), prof, current)
        steps = [s for s in filters_steps(study, prof, laser_nm(study, prof)) if s.get("exposure_s")]
        total = base["seconds"]["setup"] + sum(s["exposure_s"] + t["move_s"] + t["readout_s"] + 2 * t["command_s"]
                                               for s in steps)
        return {"minutes": round(total / 60.0, 2), "points": len(steps), "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "setup_parts": base["seconds"].get("setup_parts"), "per_acquisition": 0.0, "total": round(total, 1)}}
    if study.get("kind") == "spot":
        sp = spot_params(study, prof)
        base = estimate(spot_spectrum_study(study, prof), prof, current)
        total = base["seconds"]["setup"] + 15.0 + float(sp["acq_exposure_s"])
        n = 0
        if sp["method"] == "raman" or sp.get("feature_um") is not None:
            n = len(axis_values([-sp["range_um"] / 2, sp["range_um"] / 2, sp["step_um"]])) ** 2
            total += n * (t["command_s"] * 2 + t["xy_settle_s"] + float(sp["exposure_s"]) + t["readout_s"]) + 10
        return {"minutes": round(total / 60.0, 2), "points": n, "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "setup_parts": base["seconds"].get("setup_parts"), "per_acquisition": base["seconds"]["per_acquisition"],
                            "total": round(total, 1)}}
    if study.get("kind") == "focus":
        base = estimate(focus_spectrum_study(study, prof), prof, current)
        total = base["seconds"]["setup"] + scan_seconds(study, prof)
        return {"minutes": round(total / 60.0, 2), "points": len(scan_offsets(scan_params(study, prof))),
                "windows": 1, "seconds": {"setup": base["seconds"]["setup"], "setup_parts": base["seconds"].get("setup_parts"),
                                          "per_acquisition": base["seconds"]["per_acquisition"],
                                          "total": round(total, 1)}}
    if study.get("kind") == "alignment":
        base = estimate(alignment_check_study(study, prof), prof, current)
        a = study.get("alignment", {})
        n = 1 + (int(prof["alignment"].get("max_retries", 1)) if a.get("guided", True) else 0)
        total = base["seconds"]["total"] * n + (guide_params({"guide": {}}, prof)["max_s"]
                                                if a.get("focus_guide") else 0.0) + 15.0
        return {"minutes": round(total / 60.0, 2), "points": 1, "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "setup_parts": base["seconds"].get("setup_parts"), "per_acquisition": base["seconds"]["per_acquisition"],
                            "total": round(total, 1)}, "note": "with one AutoAlignment retry, if needed; the "
                                                               "operator's time for AutoAlignment is not included"}
    if study.get("kind") == "guide":
        base = estimate(guide_spectrum_study(study, prof), prof, current)
        total = base["seconds"]["setup"] + float(guide_params(study, prof)["max_s"])
        return {"minutes": round(total / 60.0, 2), "points": 1, "windows": 1,
                "seconds": {"setup": base["seconds"]["setup"], "setup_parts": base["seconds"].get("setup_parts"), "per_acquisition": base["seconds"]["per_acquisition"],
                            "total": round(total, 1)}, "note": "at most; it ends as soon as the goal is met"}
    laser = laser_nm(study, prof)
    rng = acq_range(study, prof, laser)
    pts = positions(study)
    parts = setup_parts(study, prof, current)
    setup = sum(parts.values())
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
        held_ = hold_shutter(study, prof, laser)
        if k > 0 and not held_:
            total += settle_s(prof, laser)          # the pause between spectra (a laser that needs it)
        if k == 0 and held_:
            total += hold_open_s(prof, laser)       # the laser settling after the hold opens the shutter
    vd = study.get("video") or {}
    v = prof.get("video", {})
    shots = int(bool(vd.get("before", study.get("kind") == "calibration"))) + int(bool(vd.get("after")))
    total += shots * (float(v.get("settle_s", 1.5)) + 5.0 +
                      (float(v.get("laser_s", 1.0)) if vd.get("with_laser") else 0.0))
    series_wait = 0.0
    if study.get("kind") == "series":
        series_wait = max(0.0, float(study.get("series", {}).get("interval_s", 0)) * (len(pts) - 1)
                          - acq_s * (len(pts) - 1)) if len(pts) > 1 else 0.0
        total += series_wait
    snap_s = shots * (float(v.get("settle_s", 1.5)) + 5.0 + (float(v.get("laser_s", 1.0)) if vd.get("with_laser")
                                                              else 0.0))
    return {"minutes": round(total / 60.0, 2), "points": len(pts), "windows": rng.get("windows"),
            "seconds": {"setup": round(setup, 1), "setup_parts": parts, "per_acquisition": round(acq_s, 2),
                        "acquisitions": round(acq_s * len(pts), 1), "video_snapshots": round(snap_s, 1),
                        **({"series_waits": round(series_wait, 1)} if study.get("kind") == "series" else {}),
                        "total": round(total, 1)},
            "not_included": "the operator's answers in LabSpec (the go-ahead before the first exposure, the "
                            "objective by hand)"}


def inputs(study: dict, prof: dict, max_steps: int = 40, current: dict | None = None) -> dict:
    """The bridge commands of one study, without running: setup, then the first points' commands. With current (the
    instrument's state now), set-up steps already in place are marked already_set (the runner skips them) and left
    out of the estimate."""
    if study.get("kind") == "depth":
        inner = inputs(depth_as_map(study, prof), prof, max_steps, current)
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
                "estimate": estimate(study, prof, current), "note": "no laser"}
    if study.get("kind") == "focus":
        sc = scan_params(study, prof)
        laser = laser_nm(study, prof)
        steps = [{"verb": "status"}] + marked_setup_steps(study, prof, current) + [{"verb": "status"}, {"verb": "confirm"}]
        for dz in scan_offsets(sc)[:max_steps]:
            steps += [{"verb": "move", "motor": prof["motors"]["z"], "value": f"focus{dz:+g}"},
                      {"verb": "acquire", "exposure_s": sc["exposure_s"], "center_cm1": sc["line_cm1"]}]
        steps.append({"verb": "move", "motor": prof["motors"]["z"], "value": "the best Z (parabola through the top)"})
        return {"laser_nm": laser, "range": acq_range(focus_spectrum_study(study, prof), prof, laser),
                "points": len(scan_offsets(sc)), "steps": steps, "steps_shown": len(steps),
                "estimate": estimate(study, prof, current)}
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
        inner["estimate"] = estimate(study, prof, current)
        return inner
    if study.get("kind") == "guide":
        g = guide_params(study, prof)
        laser = laser_nm(study, prof)
        rng = acq_range(guide_spectrum_study(study, prof), prof, laser)
        steps = [{"verb": "status"}] + marked_setup_steps(study, prof, current) + [
            {"verb": "status"}, {"verb": "confirm"},
            {"verb": "guide", "text": g.get("instruction"), "mode": g["mode"], "target": g.get("target"),
             "line_cm1": g["line_cm1"], "exposure_s": g["exposure_s"], "max_s": g["max_s"],
             "why": "short acquisitions in LabSpec; the panel shows the signal and STOP when the goal is met"}]
        return {"laser_nm": laser, "range": rng, "points": 1, "steps": steps, "steps_shown": len(steps),
                "estimate": estimate(study, prof, current)}
    laser = laser_nm(study, prof)
    rng = acq_range(study, prof, laser)
    steps = [{"verb": "status", "why": "record the instrument state before"}]
    steps += marked_setup_steps(study, prof, current)
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
            "steps_shown": min(len(steps), max_steps), "estimate": estimate(study, prof, current)}
