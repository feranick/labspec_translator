"""format.py — the study format and its static checks (no instrument needed).
Version: 2026.11.19.2
"""
from __future__ import annotations

import math

from . import config, plan
from .profile import filter_pct, laser_key

KINDS = ("spectrum", "map", "series", "calibration")
TOP_KEYS = {"kind", "sample", "setup", "acquire", "focus", "points", "map", "series", "process", "expect",
            "images", "save_native", "notes", "video"}
VIDEO_KEYS = {"before", "after", "with_laser"}
SETUP_KEYS = {"laser_nm", "grating", "filter", "objective", "hole_um", "slit_um"}
ACQ_KEYS = {"range_cm1", "center_cm1", "exposure_s", "accumulations", "auto_exposure", "spike", "dark"}
AE_KEYS = {"target_counts", "min_s", "max_s", "test_s"}
FOCUS_KEYS = {"autofocus", "every", "dz_um"}
MAP_KEYS = {"x_um", "y_um", "z_um", "snake"}
SERIES_KEYS = {"count", "interval_s"}
PROCESS_KEYS = {"baseline", "peaks", "auto_peaks", "bands"}
PEAK_KEYS = {"near_cm1", "window_cm1", "shape", "name"}
SHAPES = ("pseudo_voigt", "lorentz", "gauss")
SAMPLE_KEYS = {"name", "notes", "sensitive"}

EXAMPLE = {
    "si_check": {"kind": "calibration", "sample": {"name": "Si wafer"},
                 "setup": {"laser_nm": 532, "grating": 1800, "filter": "10%", "objective": "x100"},
                 "acquire": {"exposure_s": 1, "accumulations": 2}},
    "graphene_map": {"kind": "map", "sample": {"name": "CVD graphene on SiO2", "sensitive": True},
                     "setup": {"laser_nm": 532, "grating": 600, "filter": "10%", "objective": "x100"},
                     "acquire": {"range_cm1": [1200, 2800], "exposure_s": 2, "accumulations": 2,
                                 "spike": "double"},
                     "map": {"x_um": [-10, 10, 1], "y_um": [-10, 10, 1]},
                     "focus": {"autofocus": "laser", "every": 50},
                     "process": {"peaks": [{"name": "G", "near_cm1": 1585}, {"name": "2D", "near_cm1": 2680}],
                                 "bands": [{"name": "D", "from_cm1": 1300, "to_cm1": 1400}]},
                     "images": True},
}


def study_format(status: dict | None = None) -> dict:
    """The format, compact: kinds, keys, units, conventions, the profile's choices and limits, an example."""
    from . import profile as P
    prof = P.load()
    return {
        "translator": "labspec-translator (ls6t)",
        "studies": "{name: study}; names: letters, digits, _ and -",
        "kinds": {
            "spectrum": "one spectrum at the current spot, or one per entry of 'points'",
            "map": "a grid of spectra: map.x_um / y_um / z_um = [from, to, step] (offsets, µm)",
            "series": "repeated spectra at one spot: series.count, series.interval_s (time series)",
            "calibration": "a spectrum of a silicon reference: fits the Si line and checks it against "
                           f"{prof['checks']['si_cm1']} ± {prof['checks']['si_tol_cm1']} cm-1"},
        "keys": {
            "sample": "{name, notes, sensitive: true caps the filter at limits.sensitive_max_filter_pct}",
            "setup": "{laser_nm, grating (gr/mm), filter (a label from the profile, e.g. '10%'), objective "
                     "(label), hole_um, slit_um}; a key left out keeps what LabSpec has (recorded in the result)",
            "acquire": "{range_cm1: [from, to] (extended range: LabSpec stitches windows) | center_cm1 (one "
                       "window), exposure_s (s per accumulation, or 'auto' with auto_exposure: {target_counts, "
                       "min_s, max_s, test_s}), accumulations, spike: labspec | none | single | double | "
                       "double_auto, dark: labspec | true | false}",
            "focus": "{autofocus: laser | raman | video (before the first point; map: also every N points with "
                     "'every'), dz_um: Z offset from the operator's focus (spectrum, series)}",
            "points": "[{x_um, y_um, dz_um}] (spectrum kind): offsets from where the operator left the stage",
            "map": "{x_um, y_um, z_um: [from, to, step] offsets; snake: true (default) for boustrophedon rows}",
            "series": "{count, interval_s: seconds from one start to the next}",
            "process": "{baseline: {degree: 3} | false (polynomial, iterative; default degree 3), peaks: "
                       "[{name, near_cm1, window_cm1 (default 40), shape: pseudo_voigt | lorentz | gauss}], "
                       "auto_peaks: N strongest peaks found when 'peaks' is absent (default 3), bands: "
                       "[{name, from_cm1, to_cm1}] integrated after the baseline}",
            "expect": "[{quantity: dotted result path (e.g. 'peaks.0.position_cm1', "
                      "'calibration.offset_cm1', 'map.summary.G.position_cm1.mean'), value, tol}]",
            "images": "true: PNG plots (spectrum with fits; map images of each peak and band)",
            "video": "{before: true (default for calibration), after, with_laser}: a video frame with the laser "
                     "position marked, before the first and/or after the last acquisition (results video.before/"
                     "after, images video_before/after: look with get_image); with_laser opens the laser shutter "
                     "for about a second so the beam itself shows",
            "save_native": "true (default): LabSpec also saves its own .l6s files",
        },
        "conventions": [
            "Raman shift in cm-1 (Stokes, relative to the laser line); stage positions in µm.",
            "Stage moves are offsets from where the operator left the stage at the start of the request; Z is "
            "always relative to the operator's focus. Absolute positions are never used.",
            "The laser shutter opens only during acquisitions. The operator watches in LabSpec and can stop "
            "everything from the bridge's popup or LabSpec's stop button.",
            "Before the first exposure of a request the operator confirms in LabSpec (LS6T_CONFIRM).",
            "Saturation, a warm detector, a move not reached or an operator stop make a result unreliable.",
        ],
        "profile": P.summary(prof),
        "example": EXAMPLE,
        "status": status if status is not None else {"mode": config.mode()},
    }


# --------------------------------------------------------------------------------------------- validation

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _unknown(d: dict, allowed: set, where: str, errors: list):
    for k in d:
        if k not in allowed:
            errors.append(f"{where}: unknown key '{k}' (allowed: {', '.join(sorted(allowed))})")


def _axis(spec, where: str, errors: list) -> bool:
    if not (isinstance(spec, (list, tuple)) and len(spec) == 3 and all(_num(v) for v in spec)):
        errors.append(f"{where}: give [from, to, step] in µm, e.g. [-10, 10, 1]")
        return False
    if spec[2] <= 0:
        errors.append(f"{where}: step must be > 0 (got {spec[2]})")
        return False
    return True


def validate_study(name: str, study: dict, prof: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(study, dict):
        return {"ok": False, "errors": ["a study is an object"], "warnings": []}
    if not name or not all(c.isalnum() or c in "_-" for c in name):
        errors.append(f"study name {name!r}: use letters, digits, _ and -")
    _unknown(study, TOP_KEYS, "study", errors)
    kind = study.get("kind", "spectrum")
    if kind not in KINDS:
        errors.append(f"kind {kind!r}: use one of {', '.join(KINDS)}")
        return {"ok": False, "errors": errors, "warnings": warnings}
    lim = prof["limits"]
    sample = study.get("sample", {})
    if not isinstance(sample, dict):
        errors.append("sample: an object {name, notes, sensitive}")
        sample = {}
    _unknown(sample, SAMPLE_KEYS, "sample", errors)
    if not sample.get("name"):
        warnings.append("sample.name is empty: give the sample a name (it goes into the LabSpec file)")

    # ---- setup
    s = study.get("setup", {})
    if not isinstance(s, dict):
        errors.append("setup: an object")
        s = {}
    _unknown(s, SETUP_KEYS, "setup", errors)
    lk = None
    if s.get("laser_nm") is not None:
        lk = laser_key(prof, s["laser_nm"])
        if not lk:
            have = ", ".join("%g" % v["nm"] for v in prof["lasers"].values())
            errors.append(f"setup.laser_nm {s['laser_nm']}: not on this instrument (lasers: {have} nm)")
    else:
        warnings.append("setup.laser_nm not given: the laser LabSpec has selected is used and read back")
    if s.get("grating") is not None:
        if not _num(s["grating"]) or f"{float(s['grating']):g}" not in prof["gratings"]:
            errors.append(f"setup.grating {s['grating']}: use one of {', '.join(prof['gratings'])} gr/mm")
    pct = None
    if s.get("filter") is not None:
        if str(s["filter"]) not in prof["filters"]:
            errors.append(f"setup.filter {s['filter']!r}: use one of {', '.join(map(repr, prof['filters']))}")
        else:
            pct = prof["filters"][str(s["filter"])]
            if pct is None:
                pct = filter_pct(s["filter"])
            if pct is None:
                errors.append(f"setup.filter {s['filter']!r}: its transmission is not in the profile "
                              "(a person sets it in profile.json 'filters')")
    else:
        warnings.append("setup.filter not given: the filter LabSpec has selected is used (its power is unchecked)")
    if pct is not None:
        caps = [("limits.max_filter_pct", lim["max_filter_pct"])]
        if lk:
            caps.append((f"lasers.{lk}.max_filter_pct", prof["lasers"][lk].get("max_filter_pct", 100)))
        if sample.get("sensitive"):
            caps.append(("limits.sensitive_max_filter_pct", lim["sensitive_max_filter_pct"]))
        for what, cap in caps:
            if cap is not None and pct > float(cap) + 1e-9:
                errors.append(f"setup.filter {s['filter']!r} ({pct:g}%) is above {what} = {cap:g}%: "
                              "choose a stronger attenuation")
    if s.get("objective") is not None and str(s["objective"]) not in [str(o) for o in prof["objectives"]]:
        errors.append(f"setup.objective {s['objective']!r}: use one of {', '.join(map(str, prof['objectives']))}")
    for key, allowed in (("hole_um", prof.get("holes_um")), ("slit_um", prof.get("slits_um"))):
        v = s.get(key)
        if v is None:
            continue
        if not _num(v) or v <= 0:
            errors.append(f"setup.{key}: a positive number (µm)")
        elif allowed and not any(abs(float(v) - a) < 1e-6 for a in allowed):
            errors.append(f"setup.{key} {v}: use one of {', '.join(f'{a:g}' for a in allowed)}")

    # ---- acquire
    a = study.get("acquire", {})
    if not isinstance(a, dict):
        errors.append("acquire: an object")
        a = {}
    _unknown(a, ACQ_KEYS, "acquire", errors)
    if a.get("range_cm1") is not None and a.get("center_cm1") is not None:
        errors.append("acquire: give range_cm1 or center_cm1, not both")
    if a.get("range_cm1") is not None:
        r = a["range_cm1"]
        if not (isinstance(r, (list, tuple)) and len(r) == 2 and all(_num(v) for v in r) and r[0] != r[1]):
            errors.append("acquire.range_cm1: [from, to] in cm-1, e.g. [100, 1800]")
    elif a.get("center_cm1") is not None and not _num(a["center_cm1"]):
        errors.append("acquire.center_cm1: a number (cm-1)")
    elif a.get("range_cm1") is None and a.get("center_cm1") is None and kind != "calibration":
        warnings.append("acquire: neither range_cm1 nor center_cm1: one window centred at 1000 cm-1")
    exp = a.get("exposure_s", 1.0)
    if exp == "auto":
        ae = a.get("auto_exposure", {})
        if not isinstance(ae, dict):
            errors.append("acquire.auto_exposure: an object {target_counts, min_s, max_s, test_s}")
            ae = {}
        _unknown(ae, AE_KEYS, "acquire.auto_exposure", errors)
        mx = ae.get("max_s", 10.0)
        if not _num(mx) or mx <= 0 or mx > lim["max_exposure_s"]:
            errors.append(f"acquire.auto_exposure.max_s: 0 < max_s <= limits.max_exposure_s "
                          f"({lim['max_exposure_s']:g} s)")
        tc = ae.get("target_counts")
        if tc is not None and (not _num(tc) or not 0 < tc < prof["detector"]["full_scale"]):
            errors.append(f"acquire.auto_exposure.target_counts: below the detector's full scale "
                          f"({prof['detector']['full_scale']})")
    elif not _num(exp) or exp <= 0:
        errors.append("acquire.exposure_s: seconds > 0, or 'auto'")
    elif exp > lim["max_exposure_s"]:
        errors.append(f"acquire.exposure_s {exp:g} s is above limits.max_exposure_s = {lim['max_exposure_s']:g} s")
    acc = a.get("accumulations", 1)
    if not isinstance(acc, int) or isinstance(acc, bool) or acc < 1:
        errors.append("acquire.accumulations: an integer >= 1")
    elif acc > lim["max_accumulations"]:
        errors.append(f"acquire.accumulations {acc} is above limits.max_accumulations = {lim['max_accumulations']}")
    spike = a.get("spike", "labspec")
    if spike not in plan.SPIKE_MODES:
        errors.append(f"acquire.spike {spike!r}: use one of {', '.join(plan.SPIKE_MODES)}")
    elif spike in ("single", "double") and isinstance(acc, int) and acc < 2:
        warnings.append(f"acquire.spike {spike!r} compares accumulations: use accumulations >= 2 "
                        "(or 'double_auto', which adds one)")
    if a.get("dark", "labspec") not in ("labspec", True, False):
        errors.append("acquire.dark: labspec, true or false")

    # ---- focus, points, map, series
    f = study.get("focus", {})
    if not isinstance(f, dict):
        errors.append("focus: an object")
        f = {}
    _unknown(f, FOCUS_KEYS, "focus", errors)
    if f.get("autofocus") is not None and f["autofocus"] not in plan.AUTOFOCUS:
        errors.append(f"focus.autofocus {f['autofocus']!r}: use laser, raman or video")
    if f.get("every") is not None and (not isinstance(f["every"], int) or f["every"] < 1):
        errors.append("focus.every: an integer >= 1 (points between autofocus runs)")
    if f.get("dz_um") is not None and (not _num(f["dz_um"]) or abs(f["dz_um"]) > lim["max_dz_um"]):
        errors.append(f"focus.dz_um: |dz| <= limits.max_dz_um = {lim['max_dz_um']:g} µm")
    if kind == "map":
        m = study.get("map")
        if not isinstance(m, dict) or not any(m.get(k) for k in ("x_um", "y_um", "z_um")):
            errors.append("map: give at least one of x_um, y_um, z_um as [from, to, step] (µm)")
        else:
            _unknown(m, MAP_KEYS, "map", errors)
            for ax in ("x_um", "y_um", "z_um"):
                if m.get(ax) is not None and _axis(m[ax], f"map.{ax}", errors):
                    vals = plan.axis_values(m[ax])
                    big = max(abs(v) for v in vals)
                    cap = lim["max_dz_um"] if ax == "z_um" else lim["max_xy_um"]
                    if big > cap:
                        errors.append(f"map.{ax} reaches {big:g} µm from the start: above "
                                      f"limits.{'max_dz_um' if ax == 'z_um' else 'max_xy_um'} = {cap:g} µm")
                    motor = prof["motors"][ax[0]]
                    found = prof.get("discovered", {}).get("motors")
                    if found is not None and motor not in found:
                        errors.append(f"map.{ax}: the stage axis {motor!r} was not found by the discovery script")
    elif study.get("map") is not None:
        errors.append("map: only with kind 'map'")
    if study.get("points") is not None:
        if kind != "spectrum":
            errors.append("points: only with kind 'spectrum'")
        elif not isinstance(study["points"], list) or not study["points"]:
            errors.append("points: a non-empty list of {x_um, y_um, dz_um}")
        else:
            for i, p in enumerate(study["points"]):
                if not isinstance(p, dict):
                    errors.append(f"points[{i}]: an object {{x_um, y_um, dz_um}}")
                    continue
                _unknown(p, {"x_um", "y_um", "dz_um"}, f"points[{i}]", errors)
                if any(v is not None and not _num(v) for v in p.values()):
                    errors.append(f"points[{i}]: numbers (µm)")
                    continue
                if math.hypot(p.get("x_um", 0) or 0, p.get("y_um", 0) or 0) > lim["max_xy_um"]:
                    errors.append(f"points[{i}] is more than limits.max_xy_um = {lim['max_xy_um']:g} µm "
                                  "from the start")
                if abs(p.get("dz_um", 0) or 0) > lim["max_dz_um"]:
                    errors.append(f"points[{i}].dz_um: |dz| <= limits.max_dz_um = {lim['max_dz_um']:g} µm")
    if kind == "series":
        se = study.get("series")
        if not isinstance(se, dict):
            errors.append("series: {count, interval_s}")
        else:
            _unknown(se, SERIES_KEYS, "series", errors)
            if not isinstance(se.get("count"), int) or se["count"] < 1:
                errors.append("series.count: an integer >= 1")
            if se.get("interval_s") is not None and (not _num(se["interval_s"]) or se["interval_s"] < 0):
                errors.append("series.interval_s: seconds >= 0")
    elif study.get("series") is not None:
        errors.append("series: only with kind 'series'")

    # ---- video snapshots
    vd = study.get("video")
    if vd is not None:
        if not isinstance(vd, dict):
            errors.append("video: {before, after, with_laser} (booleans)")
        else:
            _unknown(vd, VIDEO_KEYS, "video", errors)
            for k in VIDEO_KEYS:
                if k in vd and not isinstance(vd[k], bool):
                    errors.append(f"video.{k}: true or false")
            if vd.get("with_laser"):
                if not (vd.get("before", kind == "calibration") or vd.get("after")):
                    errors.append("video.with_laser: also give before or after (when to take the frame)")
                if not prof["motors"].get("laser_shutter"):
                    errors.append("video.with_laser: the profile has no laser shutter motor (motors.laser_shutter, "
                                  "from the discovery script; set by a person)")
                if s.get("filter") is None:
                    errors.append("video.with_laser: give setup.filter (the laser shines during the snapshot)")
                v = prof.get("video", {})
                if float(v.get("laser_s", 1.0)) > float(v.get("max_laser_s", 5.0)):
                    errors.append("profile video.laser_s is above video.max_laser_s")

    # ---- process, expect
    pr = study.get("process", {})
    if not isinstance(pr, dict):
        errors.append("process: an object")
        pr = {}
    _unknown(pr, PROCESS_KEYS, "process", errors)
    bl = pr.get("baseline", {"degree": 3})
    if bl not in (False, None) and not (isinstance(bl, dict) and isinstance(bl.get("degree", 3), int)
                                        and 0 <= bl.get("degree", 3) <= 9):
        errors.append("process.baseline: {degree: 0..9} or false")
    names = set()
    for i, p in enumerate(pr.get("peaks", []) or []):
        if not isinstance(p, dict) or not _num(p.get("near_cm1")):
            errors.append(f"process.peaks[{i}]: {{near_cm1, window_cm1, shape, name}}")
            continue
        _unknown(p, PEAK_KEYS, f"process.peaks[{i}]", errors)
        if p.get("shape", "pseudo_voigt") not in SHAPES:
            errors.append(f"process.peaks[{i}].shape: {', '.join(SHAPES)}")
        if p.get("window_cm1") is not None and (not _num(p["window_cm1"]) or p["window_cm1"] <= 0):
            errors.append(f"process.peaks[{i}].window_cm1: > 0")
        names.add(p.get("name", f"peak{i}"))
    for i, b in enumerate(pr.get("bands", []) or []):
        if not (isinstance(b, dict) and _num(b.get("from_cm1")) and _num(b.get("to_cm1"))
                and b["from_cm1"] != b["to_cm1"]):
            errors.append(f"process.bands[{i}]: {{name, from_cm1, to_cm1}}")
    for i, e in enumerate(study.get("expect", []) or []):
        if not (isinstance(e, dict) and isinstance(e.get("quantity"), str) and _num(e.get("value"))
                and _num(e.get("tol", 0)) and e.get("tol", 0) >= 0):
            errors.append(f"expect[{i}]: {{quantity: 'dotted.result.path', value, tol >= 0}}")

    out = {"ok": not errors, "errors": errors, "warnings": warnings}
    if errors:
        return out

    # ---- range in nm, windows, estimate (now that the study is well formed)
    laser = plan.laser_nm(study, prof)
    rng = plan.acq_range(study, prof, laser)
    if laser and rng.get("from_nm") is not None:
        if rng.get("from_cm1") is not None and rng["from_cm1"] < 0:
            warnings.append("acquire range starts below 0 cm-1 (anti-Stokes or the laser line): check the edge "
                            "filter allows it")
        if rng.get("windows") and rng["windows"] > 12:
            warnings.append(f"the range needs about {rng['windows']} detector windows: a coarser grating "
                            "covers it in fewer")
    if kind == "calibration":
        si = prof["checks"]["si_cm1"]
        lo, hi = rng.get("from_cm1"), rng.get("to_cm1")
        if lo is not None and hi is not None and not lo + 20 <= si <= hi - 20:
            errors.append(f"calibration: the range {lo:g}-{hi:g} cm-1 must include the Si line ({si} cm-1) "
                          "with 20 cm-1 to spare")
    est = plan.estimate(study, prof)
    if est["points"] > lim["max_points"]:
        errors.append(f"{est['points']} points: above limits.max_points = {lim['max_points']}")
    cap = lim["max_minutes"]
    env = config.max_minutes_env()
    if env is not None:
        cap = min(cap, env)
    if est["minutes"] > cap:
        errors.append(f"estimated {est['minutes']:g} min: above the ceiling of {cap:g} min "
                      "(limits.max_minutes or LS6T_MAX_MINUTES); fewer points, shorter exposures or two studies")
    if not prof.get("measured"):
        warnings.append("no machine profile yet: the built-in XploRA defaults are used (run the discovery script)")
    out.update(ok=not errors, errors=errors, warnings=warnings, estimate=est,
               range=rng, laser_nm=laser)
    return out


def validate(studies: dict, prof: dict | None = None) -> dict:
    from . import profile as P
    prof = prof or P.load()
    if not isinstance(studies, dict) or not studies:
        return {"_": {"ok": False, "errors": ["give the studies as {name: study}"], "warnings": []}}
    res = {name: validate_study(name, st, prof) for name, st in studies.items()}
    total = sum(r.get("estimate", {}).get("minutes", 0) for r in res.values())
    cap = prof["limits"]["max_minutes"]
    env = config.max_minutes_env()
    if env is not None:
        cap = min(cap, env)
    if total > cap and all(r["ok"] for r in res.values()):
        for r in res.values():
            r["warnings"].append(f"the request together takes about {total:g} min (ceiling {cap:g} per study)")
    return res
