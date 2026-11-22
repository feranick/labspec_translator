"""format.py — the study format and its static checks (no instrument needed).
Version: 2026.11.22.5
"""
from __future__ import annotations

import math
import os

from . import config, plan
from .profile import filter_pct, laser_key, match_label, review_gate

KINDS = ("spectrum", "map", "series", "calibration", "alignment", "guide", "focus", "move", "spot", "depth",
         "background", "filters", "cutoff")
TOP_KEYS = {"kind", "sample", "setup", "acquire", "focus", "points", "map", "series", "process", "expect",
            "images", "save_native", "notes", "video", "alignment", "guide", "move", "spot", "depth",
            "calibration", "background", "filters", "cutoff"}
FILTERS_KEYS = {"line_cm1", "window_cm1", "labels", "exposure_s", "max_exposure_s"}
CUTOFF_KEYS = {"from_cm1", "to_cm1", "light", "laser_off", "plateau_cm1"}
CAL_KEYS = {"reference", "lines_cm1", "tol_cm1", "adjust"}
BG_KEYS = {"name", "laser_off"}
DEPTH_KEYS = {"from_um", "to_um", "step_um", "refractive_index", "engine", "swift"}
MOVE_KEYS = {"x_um", "y_um", "to_um", "dz_um", "stay", "snapshot", "travel", "focus"}
SPOT_KEYS = {"method", "feature_um", "refine", "range_um", "step_um", "line_cm1", "window_cm1", "exposure_s",
             "acq_exposure_s", "remember"}
SPOT_METHODS = ("auto", "shutter", "acquisition", "raman")
SCAN_KEYS = {"range_um", "step_um", "line_cm1", "window_cm1", "exposure_s", "from_below"}
ALIGN_KEYS = {"guided", "focus_guide"}
GUIDE_KEYS = {"instruction", "mode", "target", "line_cm1", "center_cm1", "window_cm1", "exposure_s", "max_s",
              "stable_n", "drop_frac"}
GUIDE_MODES = ("reach", "drop", "maximize")
VIDEO_KEYS = {"before", "after", "with_laser"}
SETUP_KEYS = {"laser_nm", "grating", "filter", "objective", "hole_um", "slit_um", "laser_pol", "raman_pol"}
ACQ_KEYS = {"range_cm1", "center_cm1", "exposure_s", "accumulations", "auto_exposure", "spike", "dark",
            "subtract_background"}
AE_KEYS = {"target_counts", "min_s", "max_s", "test_s"}
FOCUS_KEYS = {"autofocus", "every", "dz_um", "scan"}
MAP_KEYS = {"x_um", "y_um", "z_um", "snake", "engine", "swift"}
SERIES_KEYS = {"count", "interval_s"}
PROCESS_KEYS = {"baseline", "peaks", "auto_peaks", "bands"}
PEAK_KEYS = {"near_cm1", "window_cm1", "shape", "name"}
SHAPES = ("pseudo_voigt", "lorentz", "gauss")
SAMPLE_KEYS = {"name", "notes", "sensitive", "unevenness_um"}

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
            "calibration": "the zero of the spectral scale: a reference sample's lines (Si 520.7 by default; "
                           "polystyrene, cyclohexane, naphthalene, diamond, or your own lines) against their true "
                           "positions -> offset (and scale with several lines); adjust 'labspec' guides the "
                           "operator to correct LabSpec's zero and measures again, 'ls6t' remembers the correction "
                           "and applies it to later spectra",
            "background": "a white-light background: a spectrum with the white light on and the laser off, stored "
                          "by name and subtracted from later spectra (acquire.subtract_background)",
            "filters": "each filter's real transmission, from one line (Si) measured through every filter within "
                       "the caps, strongest first; proposes the profile change (a person applies it)",
            "cutoff": "where the edge filter lets light through (cm-1 at 10/50/90 %), with the white light (laser "
                      "off) or the sample's background; later ranges below it are warned",
            "alignment": "is the system aligned for this laser? The Si reference with the laser's fixed settings, "
                         "compared with the baseline recorded after the last AutoAlignment; if it fails (guided), "
                         "the operator runs AutoAlignment in LabSpec and the check repeats; then a video snapshot "
                         "measures the beam's offset. Needs only setup.laser_nm",
            "guide": "the operator adjusts something by hand (power at the laser, focus, a mirror) while ls6t "
                     "measures a line every fraction of a second and shows it in LabSpec; STOP appears when the "
                     "goal is met (reach / drop a target, or the best position just passed: maximize)",
            "focus": "find the focus by the Raman signal: Z steps across focus.scan.range_um around the current "
                     "focus, a short acquisition at each; the best becomes the focus for the rest of the request",
            "move": "no laser: the stage to move.x_um / y_um (offsets from the home spot), or the beam onto "
                    "move.to_um (a point picked in a snapshot, using the remembered beam offset), and Z to dz_um "
                    "from the focus, then a video snapshot; stay: true (default) makes that spot home for later "
                    "studies and the next request",
            "depth": "a depth profile at the current spot: spectra at Z steps from above the surface into the "
                     "sample; each peak and band against depth, the interfaces (where a layer starts or ends), "
                     "and with depth.refractive_index the depth corrected for the medium",
            "spot": "find where the beam really is and remember it (per objective and laser): a video frame with "
                    "the beam (shutter, or during an acquisition), or the Raman signal while the beam scans over "
                    "a small feature seen on the video (dust, a particle); snapshots then mark the beam there and "
                    "move.to_um aims with it"},
        "keys": {
            "sample": "{name, notes, sensitive: true caps the filter at limits.sensitive_max_filter_pct}",
            "setup": "{laser_nm, grating (gr/mm), filter (a label from the profile, e.g. '10%'), objective "
                     "(label), hole_um, slit_um, laser_pol, raman_pol (polariser positions, when the profile has "
                     "them)}; a key left out keeps what LabSpec has (recorded in the result)",
            "acquire": "{range_cm1: [from, to] (extended range: LabSpec stitches windows) | center_cm1 (one "
                       "window), exposure_s (s per accumulation, or 'auto' with auto_exposure: {target_counts, "
                       "min_s, max_s, test_s}), accumulations, spike: labspec | none | single | double | "
                       "double_auto, dark: labspec | true | false, subtract_background: a stored background's "
                       "name}",
            "focus": "{autofocus: laser | raman | video (LabSpec's routines) | scan (ls6t's focus scan on the "
                     "Raman line) (before the first point; map: also every N points with 'every'), dz_um: Z "
                     "offset from the focus (spectrum, series), scan: {range_um (10), step_um (1), line_cm1 (Si), "
                     "window_cm1 (30), exposure_s (0.5)}}",
            "move": "(kind move) {x_um, y_um | to_um: [x, y] (absolute, from a snapshot), dz_um, stay: true, "
                    "snapshot: true}; setup may only give the objective",
            "calibration": "(kind calibration) {reference: si | polystyrene | cyclohexane | naphthalene | "
                           "diamond, lines_cm1: [...] (your own), tol_cm1 (1), adjust: none | labspec | ls6t | auto "
                           "(LabSpec's AutoCalibration when enabled in the profile, else guided like labspec)}",
            "background": "(kind background) {name, laser_off: shutter | operator}; acquire as for the spectra "
                          "it will correct (same grating and range)",
            "filters": "(kind filters) {line_cm1 (520.7), window_cm1 (30), labels (all within the caps), "
                       "exposure_s (1: through the reference), max_exposure_s (30)}; no setup.filter",
            "cutoff": "(kind cutoff) {from_cm1 (-100), to_cm1 (400), light: white | laser, laser_off: shutter | "
                      "operator, plateau_cm1 ([200, 380])}",
            "depth": "(kind depth) {from_um (-5: above the focus), to_um (20), step_um (1): nominal depth, the "
                     "stage displacement into the sample from the focus; refractive_index (optional: depth = n x "
                     "displacement, paraxial)}; with setup, acquire, process, focus, images as for a spectrum",
            "spot": "(kind spot) {method: auto | shutter | acquisition | raman, feature_um: [x, y] (from a "
                    "snapshot's 'features' or its axes) | 'auto', refine: true, range_um, step_um (by objective), "
                    "line_cm1 (Si), exposure_s (0.3), acq_exposure_s (2), remember: true}; setup needs laser_nm and "
                    "objective",
            "points": "[{x_um, y_um, dz_um}] (spectrum kind): offsets from where the operator left the stage",
            "map": "{x_um, y_um, z_um: [from, to, step] offsets; snake: true (default) for boustrophedon rows; "
                   "engine: auto | points (ls6t moves and acquires: checks and autofocus between points) | native "
                   "(LabSpec's mapping engine: faster for large maps; one detector window; swift: true for SWIFT)}",
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
            "alignment": "(kind alignment) {guided: true (default), focus_guide: false}",
            "guide": "(kind guide) {instruction (shown to the operator), mode: reach | drop | maximize, target "
                     "(counts/s of the line; reach, drop), line_cm1 (default Si 520.7), window_cm1 (30), "
                     "exposure_s (0.2: shorter is faster feedback), max_s (120), stable_n (2), drop_frac (0.1)}",
        },
        "conventions": [
            "Raman shift in cm-1 (Stokes, relative to the laser line); stage positions in µm.",
            "Stage moves are offsets from where the operator left the stage at the start of the request; Z is "
            "always relative to the operator's focus. Absolute positions are never used.",
            "The laser shutter opens only during acquisitions. The operator watches in LabSpec and can stop "
            "everything from the bridge's window (Cancel) or by cancelling the acquisition in LabSpec. LabSpec's "
            "live video buttons end the bridge: before the operator uses them, instrument end_bridge.",
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


def _blocked_by_hand(study: dict, prof: dict, errors: list, warnings: list, what: str) -> None:
    """A white-light study with laser_off 'operator': LabSpec opens the laser shutter as for any acquisition, and
    only a physical block (or the laser off at its source) keeps the beam off. Said in the warnings; and with a
    profile nobody has reviewed (no confirmed caps or power), the filter must be the strongest attenuation."""
    warnings.append(f"{what}: the laser is kept off only by the operator's physical block: run it with someone at "
                    "the instrument (LabSpec's prompt says so)")
    if config.mode() == "labspec" and config.remote_session() and os.environ.get("LS6T_HAND_BLOCK_REMOTE_OK") != "1":
        errors.append(f"{what} with the laser blocked by hand, from a Remote Desktop session: the person answering "
                      "LabSpec's prompt may not be at the instrument. Run it from the instrument's own screen (or, "
                      "with someone at the instrument blocking the beam, set LS6T_HAND_BLOCK_REMOTE_OK=1 for that "
                      "session), or use laser_off 'shutter' once the laser shutter motor is confirmed")
    if config.mode() == "labspec" and review_gate(prof):
        fl = prof.get("filters") or {}
        lowest = min(fl, key=lambda k: float(fl[k])) if fl else None
        asked = (study.get("setup") or {}).get("filter")
        if lowest is None or asked is None or not match_label(str(asked), [lowest])[0]:
            errors.append(f"{what} with the laser blocked by hand and a profile not reviewed: setup.filter must be "
                          f"the strongest attenuation ({lowest!r}), in case the block is not in place")


def _z_safety(study: dict, prof: dict, errors: list, warnings: list) -> None:
    """Z offsets from the focus against the objective's limits (plan.z_limits: toward the sample a fraction of the
    working distance, away limits.max_dz_um; both ways the toward limit until the Z direction is known), and the
    sample's stated unevenness against the gap: with sample.unevenness_um at least half the working distance, XY
    moves beyond the distance where the focus stays valid are refused."""
    s = study.get("setup") or {}
    obj = s.get("objective")
    if obj is not None:
        lab, _ = match_label(str(obj), list(prof.get("objectives", [])))
        obj = lab or obj
    zl = plan.z_limits(prof, obj, "depth" if study.get("_kind") == "depth" else None)
    kind = study.get("kind", "spectrum")
    dzs = []
    m = study.get("map") if kind == "map" else None
    if isinstance(m, dict) and isinstance(m.get("z_um"), list) and len(m["z_um"]) == 3:
        try:
            dzs += plan.axis_values(m["z_um"])
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    for p in study.get("points") or []:
        if isinstance(p, dict) and _num(p.get("dz_um")):
            dzs.append(float(p["dz_um"]))
    for blk in ("move", "focus"):
        b = study.get(blk)
        if isinstance(b, dict) and _num(b.get("dz_um")):
            dzs.append(float(b["dz_um"]))
    from_below = bool(((study.get("focus") or {}).get("scan") or {}).get("from_below"))
    if (kind == "focus" or (isinstance(study.get("focus"), dict) and study["focus"].get("autofocus") == "scan")) \
            and not from_below:
        try:
            half = float(plan.scan_params(study, prof)["range_um"]) / 2
            dzs += [half, -half]
        except (TypeError, ValueError, KeyError):
            pass
    for dz in sorted(set(dzs), key=abs, reverse=True):
        err = plan.z_check(zl, dz)
        if err:
            errors.append(err)
            break
    # the sample's unevenness against the gap left at focus
    un = (study.get("sample") or {}).get("unevenness_um")
    if un is not None:
        if not _num(un) or float(un) < 0:
            errors.append("sample.unevenness_um: how much the sample's height varies (µm, >= 0)")
            return
        far = 0.0
        if isinstance(m, dict):
            try:
                xs = [abs(v) for v in plan.axis_values(m["x_um"])] if isinstance(m.get("x_um"), list) else [0.0]
                ys = [abs(v) for v in plan.axis_values(m["y_um"])] if isinstance(m.get("y_um"), list) else [0.0]
            except (TypeError, ValueError, ZeroDivisionError):
                xs = ys = [0.0]
            far = math.hypot(max(xs), max(ys))
        for p in study.get("points") or []:
            if isinstance(p, dict):
                far = max(far, math.hypot(p.get("x_um", 0) or 0, p.get("y_um", 0) or 0))
        mv = study.get("move")
        if isinstance(mv, dict):
            far = max(far, math.hypot(mv.get("x_um", 0) or 0, mv.get("y_um", 0) or 0))
        wd, fv = zl["working_distance_um"], zl["focus_valid_xy_um"]
        retract_move = kind == "move" and plan.travel_mode(prof, study, far, obj) == "retract"
        if wd and fv is not None and float(un) >= 0.5 * wd and far > fv and not retract_move:
            errors.append(f"sample.unevenness_um = {float(un):g} µm is at least half {obj or 'the objective'}'s "
                          f"working distance (~{wd:g} µm): XY moves are kept within {fv:g} µm of the focus (this "
                          f"study goes {far:.0f} µm); a longer working distance objective, a smaller area, or a 'move' "
                          "with travel 'retract' first")


def validate_study(name: str, study: dict, prof: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(study, dict):
        return {"ok": False, "errors": ["a study is an object"], "warnings": []}
    if not name or not all(c.isalnum() or c in "_-" for c in name):
        errors.append(f"study name {name!r}: use letters, digits, _ and -")
    _unknown({k: v for k, v in study.items() if not str(k).startswith("_")}, TOP_KEYS, "study", errors)
    kind = study.get("kind", "spectrum")
    if kind not in KINDS:
        errors.append(f"kind {kind!r}: use one of {', '.join(KINDS)}")
        return {"ok": False, "errors": errors, "warnings": warnings}
    lim = prof["limits"]
    if kind == "move":
        return _validate_move(name, study, prof, errors, warnings)
    if kind == "spot":
        return _validate_spot(name, study, prof, errors, warnings)
    if study.get("calibration") is not None and kind != "calibration":
        errors.append("calibration: only with kind 'calibration'")
    if study.get("background") is not None and kind != "background":
        errors.append("background: only with kind 'background'")
    if kind == "calibration":
        cal = study.get("calibration") or {}
        if not isinstance(cal, dict):
            errors.append("calibration: {reference, lines_cm1, tol_cm1, adjust}")
            cal = {}
        _unknown(cal, CAL_KEYS, "calibration", errors)
        from .xcal import REFERENCES
        if cal.get("reference") is not None and cal["reference"] not in REFERENCES:
            errors.append(f"calibration.reference {cal['reference']!r}: {', '.join(REFERENCES)} (or lines_cm1)")
        if cal.get("lines_cm1") is not None and not (isinstance(cal["lines_cm1"], list) and cal["lines_cm1"]
                                                     and all(_num(v) for v in cal["lines_cm1"])):
            errors.append("calibration.lines_cm1: a list of Raman shifts (cm-1)")
        if cal.get("adjust", "none") not in ("none", "labspec", "ls6t", "auto"):
            errors.append("calibration.adjust: none, labspec, ls6t or auto")
        if cal.get("tol_cm1") is not None and (not _num(cal["tol_cm1"]) or cal["tol_cm1"] <= 0):
            errors.append("calibration.tol_cm1: > 0")
    for k in ("filters", "cutoff"):
        if study.get(k) is not None and kind != k:
            errors.append(f"{k}: only with kind '{k}'")
    if kind == "filters":
        fp = study.get("filters") or {}
        if not isinstance(fp, dict):
            errors.append("filters: {line_cm1, window_cm1, labels, exposure_s, max_exposure_s}")
            fp = {}
        _unknown(fp, FILTERS_KEYS, "filters", errors)
        if (study.get("setup") or {}).get("filter") is not None:
            errors.append("setup.filter: not with kind 'filters' (it steps through the filters itself)")
        if fp.get("labels") is not None:
            bad = [lab for lab in fp["labels"] if str(lab) not in prof.get("filters", {})] \
                if isinstance(fp["labels"], list) else ["?"]
            if bad:
                errors.append(f"filters.labels: from the profile's filters ({', '.join(prof.get('filters', {}))})")
        for k in ("exposure_s", "max_exposure_s"):
            if fp.get(k) is not None and (not _num(fp[k]) or not 0 < fp[k] <= lim["max_exposure_s"]):
                errors.append(f"filters.{k}: 0 < s <= limits.max_exposure_s ({lim['max_exposure_s']:g})")
        steps = plan.filters_steps(study, prof, plan.laser_nm(study, prof))
        if len([s_ for s_ in steps if s_.get("exposure_s")]) < 2:
            errors.append("filters: fewer than two filters can be measured within the caps and exposure limits")
        skipped = [s_["label"] for s_ in steps if not s_.get("exposure_s")]
        if skipped:
            warnings.append(f"filters {', '.join(skipped)}: would need more than filters.max_exposure_s: not measured")
    if kind == "cutoff":
        cp = study.get("cutoff") or {}
        if not isinstance(cp, dict):
            errors.append("cutoff: {from_cm1, to_cm1, light: white | laser, laser_off, plateau_cm1}")
            cp = {}
        _unknown(cp, CUTOFF_KEYS, "cutoff", errors)
        if cp.get("light", "white") not in ("white", "laser"):
            errors.append("cutoff.light: white (the white light, laser off) or laser (the sample's own background)")
        lo = cp.get("laser_off", "shutter" if prof["motors"].get("laser_shutter") else "operator")
        if cp.get("light", "white") == "white" and lo == "shutter" and not prof["motors"].get("laser_shutter"):
            errors.append("cutoff.laser_off 'shutter': no laser shutter motor in the profile; use 'operator'")
        if lo not in ("shutter", "operator"):
            errors.append("cutoff.laser_off: shutter or operator")
        elif cp.get("light", "white") == "white" and lo == "operator":
            _blocked_by_hand(study, prof, errors, warnings, "cutoff")
    if kind == "background":
        bgk = study.get("background")
        if not isinstance(bgk, dict):
            errors.append("background: {name, laser_off: shutter | operator}")
            bgk = {}
        _unknown(bgk, BG_KEYS, "background", errors)
        from . import background as BG
        if not BG.valid_name(bgk.get("name", "")):
            errors.append("background.name: letters, digits, _ and - (up to 60)")
        lo = bgk.get("laser_off", "shutter" if prof["motors"].get("laser_shutter") else "operator")
        if lo not in ("shutter", "operator"):
            errors.append("background.laser_off: shutter or operator")
        elif lo == "shutter" and not prof["motors"].get("laser_shutter"):
            errors.append("background.laser_off 'shutter': no laser shutter motor in the profile; use 'operator' "
                          "(the operator blocks the laser)")
        if lo == "operator":
            _blocked_by_hand(study, prof, errors, warnings, "background")
        for k in ("points", "map", "series", "process", "video", "focus"):
            if study.get(k) is not None:
                errors.append(f"{k}: not with kind 'background'")
    sb = (study.get("acquire") or {}).get("subtract_background") if isinstance(study.get("acquire"), dict) else None
    if sb is not None:
        from . import background as BG
        if not BG.valid_name(sb):
            errors.append("acquire.subtract_background: a background's name")
        elif sb not in BG.index():
            errors.append(f"acquire.subtract_background {sb!r}: no such background (stored: "
                          f"{', '.join(BG.index()) or 'none'}); a 'background' study makes it")
    if kind == "depth":
        dp = study.get("depth")
        if not isinstance(dp, dict):
            errors.append("depth: {from_um, to_um, step_um, refractive_index}")
            return {"ok": False, "errors": errors, "warnings": warnings}
        _unknown(dp, DEPTH_KEYS, "depth", errors)
        for k in ("from_um", "to_um", "step_um"):
            if dp.get(k) is not None and not _num(dp[k]):
                errors.append(f"depth.{k}: a number (µm)")
        if dp.get("step_um") is not None and _num(dp["step_um"]) and dp["step_um"] <= 0:
            errors.append("depth.step_um: > 0")
        n = dp.get("refractive_index")
        if n is not None and (not _num(n) or not 1.0 <= n <= 4.5):
            errors.append("depth.refractive_index: between 1.0 and 4.5 (e.g. 1.33 water, 1.46 silica, 1.59 PS)")
        for k in ("points", "map", "series"):
            if study.get(k) is not None:
                errors.append(f"{k}: not with kind 'depth' (it stays at the current spot; aim first with a 'move')")
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}
        derived = plan.depth_as_map(study, prof)
        derived["_kind"] = "depth"            # Z limits for depth profiles (z_limits purpose 'depth')
        inner = validate_study(name, derived, prof)
        inner["errors"] = [e.replace("map.z_um", "depth (from_um / to_um)") for e in inner["errors"]]
        if not plan.into_sign(prof)[1]:
            inner["warnings"].append("the profile does not say which Z direction goes into the sample "
                                     "(stage.z_into_sample): + is assumed")
        if inner["ok"] and inner["estimate"]["points"] < 3:
            inner["errors"].append("depth: at least 3 steps")
            inner["ok"] = False
        return _merge({}, inner, errors, warnings)
    if kind == "focus":
        for k in ("points", "map", "series", "acquire", "guide", "alignment", "process", "move"):
            if study.get(k) is not None:
                errors.append(f"{k}: not with kind 'focus' (it scans Z at the current spot; focus.scan sets it)")
        if (study.get("setup") or {}).get("laser_nm") is None:
            errors.append("focus: give setup.laser_nm")
        fo = study.get("focus") or {}
        if not isinstance(fo, dict):
            errors.append("focus: {scan: {...}}")
            fo = {}
        _unknown(fo, {"scan"}, "focus (kind focus)", errors)
        _validate_scan(fo.get("scan") or {}, prof, errors)
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}
        inner = validate_study(name, plan.focus_spectrum_study(study, prof), prof)
        inner["warnings"] = [w for w in inner["warnings"] if not w.startswith("acquire.spike")]
        out = _merge({}, inner, errors, warnings)
        if out["ok"]:
            out["estimate"] = plan.estimate(study, prof)
        return out
    if kind == "alignment":
        return _validate_alignment(name, study, prof, errors, warnings)
    if kind == "guide":
        return _validate_guide(name, study, prof, errors, warnings)
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
    if s.get("objective") is not None and match_label(s["objective"], prof["objectives"])[0] is None:
        errors.append(f"setup.objective {s['objective']!r}: use one of {', '.join(map(str, prof['objectives']))}")
    for key in ("laser_pol", "raman_pol"):
        if s.get(key) is None:
            continue
        if not prof["motors"].get(key):
            errors.append(f"setup.{key}: this instrument has no such polariser in the profile")
        elif match_label(s[key], (prof.get("polarizers") or {}).get(key, []))[0] is None:
            errors.append(f"setup.{key} {s[key]!r}: use one of "
                          f"{', '.join((prof.get('polarizers') or {}).get(key, [])) or '(none known)'}")
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
    elif a.get("range_cm1") is None and a.get("center_cm1") is None and kind not in ("calibration", "filters", "cutoff"):
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
        errors.append(f"focus.autofocus {f['autofocus']!r}: use laser, raman, video or scan")
    sc = f.get("scan")
    if sc is not None or f.get("autofocus") == "scan" or kind == "focus":
        _validate_scan(sc or {}, prof, errors)
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
            # limits.max_xy_um is a distance from the home spot (as for points and moves): the map's farthest corner
            try:
                xs = [abs(v) for v in plan.axis_values(m["x_um"])] if m.get("x_um") and isinstance(m["x_um"], list) \
                    and len(m["x_um"]) == 3 else [0.0]
                ys = [abs(v) for v in plan.axis_values(m["y_um"])] if m.get("y_um") and isinstance(m["y_um"], list) \
                    and len(m["y_um"]) == 3 else [0.0]
            except (TypeError, ValueError, ZeroDivisionError):
                xs = ys = [0.0]                 # already reported by the axis checks above
            far = math.hypot(max(xs), max(ys))
            if far > lim["max_xy_um"] and max(max(xs), max(ys)) <= lim["max_xy_um"]:
                errors.append(f"map: its farthest corner is {far:.0f} µm from the start: above limits.max_xy_um = "
                              f"{lim['max_xy_um']:g} µm (a distance from the home spot, not per axis)")
    elif study.get("map") is not None:
        errors.append("map: only with kind 'map'")
    if kind == "map" and isinstance(study.get("map"), dict):
        _validate_engine(study, study["map"], prof, errors, warnings)
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

    _z_safety(study, prof, errors, warnings)

    # ---- range in nm, windows, estimate (now that the study is well formed)
    laser = plan.laser_nm(study, prof)
    rng = plan.acq_range(study, prof, laser)
    if laser and rng.get("from_nm") is not None:
        reach = plan.grating_reach(prof, (study.get("setup") or {}).get("grating"), laser)
        if reach and float(rng.get("to_nm") or 0) > reach["limit_nm"]:
            top = reach["max_shift_cm1"]
            errors.append(f"grating {reach['grating']} cannot reach {float(rng['to_nm']):.0f} nm with the "
                          f"{laser:g} nm laser: beyond {reach['limit_nm']:g} nm ({reach['basis']})"
                          + (f", i.e. above {top:g} cm-1" if top is not None else ", the laser line itself")
                          + ": a coarser grating, or a shorter range")
        lo_nm = plan.grating_min_nm(prof, (study.get("setup") or {}).get("grating"))
        if lo_nm and float(rng.get("from_nm") or 1e9) < lo_nm:
            errors.append(f"grating {(study.get('setup') or {}).get('grating')} starts at {lo_nm:g} nm (profile "
                          f"min_nm): the range from {float(rng['from_nm']):.0f} nm is below it")
        if rng.get("from_cm1") is not None and rng["from_cm1"] < 0 and kind != "cutoff":
            warnings.append("acquire range starts below 0 cm-1 (anti-Stokes or the laser line): check the edge "
                            "filter allows it")
        if rng.get("from_cm1") is not None and kind != "cutoff":
            from . import perf
            cut = perf.cutoff_for(laser, (study.get("setup") or {}).get("grating"))
            if cut is not None and rng["from_cm1"] < cut:
                warnings.append(f"the range starts at {rng['from_cm1']:g} cm-1, below the measured cut-off "
                                f"({cut:g} cm-1 for this laser and grating): the edge filter blocks part of it")
        if rng.get("windows") and rng["windows"] > 12:
            warnings.append(f"the range needs about {rng['windows']} detector windows: a coarser grating "
                            "covers it in fewer")
    if kind == "calibration":
        if not plan.calibration_lines(study, prof, rng):
            lo, hi = rng.get("from_cm1"), rng.get("to_cm1")
            errors.append(f"calibration: no reference line inside the range {lo}-{hi} cm-1 (with 15 cm-1 to "
                          "spare): widen acquire.range_cm1 or choose the lines")
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
    # the laser fires in every kind but move, background and a white-light cutoff, and in a video with_laser
    vd_ = study.get("video") if isinstance(study.get("video"), dict) else {}
    cut_ = study.get("cutoff") if isinstance(study.get("cutoff"), dict) else {}
    fires = (kind not in ("move", "background") and not (kind == "cutoff" and cut_.get("light", "white") != "laser")) \
        or bool(vd_.get("with_laser"))
    if fires and prof.get("measured") and config.mode() == "labspec":
        gate = review_gate(prof)
        if gate:
            errors.append(gate)
    if not prof.get("measured"):
        if config.mode() == "labspec":
            errors.append("no machine profile: the built-in defaults are a generic XploRA for the simulator (laser "
                          "lines, objectives and caps not this instrument's) and are never used on the instrument. "
                          "Start ls6t_bridge.vbs in LabSpec once (it reads the instrument and ls6t builds a cautious "
                          "profile from it), then review it: ls6t profile propose, edit, ls6t profile import")
        else:
            warnings.append("no machine profile yet: the built-in XploRA defaults are used (simulator)")
    out.update(ok=not errors, errors=errors, warnings=warnings, estimate=est,
               range=rng, laser_nm=laser)
    return out


def _merge(out: dict, inner: dict, errors: list, warnings: list) -> dict:
    errors += [e for e in inner["errors"] if e not in errors]
    warnings += [w for w in inner["warnings"] if w not in warnings]
    res = {"ok": not errors, "errors": errors, "warnings": warnings}
    for k in ("estimate", "range", "laser_nm"):
        if k in inner:
            res[k] = inner[k]
    res.update(out)
    return res


def _validate_engine(study: dict, m: dict, prof: dict, errors: list, warnings: list):
    eng = m.get("engine", "auto")
    if eng not in ("auto", "points", "native"):
        errors.append("map.engine: auto, points or native")
        return
    if m.get("swift") is not None and not isinstance(m["swift"], bool):
        errors.append("map.swift: true or false")
    if m.get("swift") and eng != "native":
        errors.append("map.swift: only with engine 'native' (SWIFT is LabSpec's fast mapping)")
    if eng == "native":
        a = study.get("acquire") or {}
        if a.get("range_cm1"):
            errors.append("map.engine 'native': one detector window only: give acquire.center_cm1, not range_cm1 "
                          "(or engine 'points' for an extended range)")
        if a.get("exposure_s") == "auto":
            errors.append("map.engine 'native': a fixed exposure_s (auto-exposure is per point: engine 'points')")
        if (study.get("focus") or {}).get("every"):
            errors.append("map.engine 'native': no autofocus between points (focus.every needs engine 'points')")
        if (study.get("focus") or {}).get("autofocus"):
            warnings.append("map.engine 'native': the autofocus runs once, before the map")
        warnings.append("map.engine 'native': LabSpec runs the map; ls6t checks the spectra when it ends (a stop "
                        "still cancels it)")


def _validate_scan(sc: dict, prof: dict, errors: list):
    if not isinstance(sc, dict):
        errors.append("focus.scan: {range_um, step_um, line_cm1, window_cm1, exposure_s}")
        return
    _unknown(sc, SCAN_KEYS, "focus.scan", errors)
    p = dict(plan.SCAN_DEFAULTS, **sc)
    lim = prof["limits"]
    for k, lo, hi in (("range_um", 0.5, 2 * float(lim["max_dz_um"])), ("step_um", 0.05, 50.0),
                      ("window_cm1", 2.0, 400.0), ("exposure_s", 0.01, float(lim["max_exposure_s"]))):
        v = sc.get(k)
        if v is not None and (not _num(v) or not lo <= v <= hi):
            errors.append(f"focus.scan.{k}: between {lo:g} and {hi:g}"
                          + (" (2 x limits.max_dz_um)" if k == "range_um" else ""))
    if sc.get("line_cm1") is not None and not _num(sc["line_cm1"]):
        errors.append("focus.scan.line_cm1: a number (cm-1)")
    if sc.get("from_below") is not None:
        if not isinstance(sc["from_below"], bool):
            errors.append("focus.scan.from_below: true or false")
        elif sc["from_below"] and not plan.into_sign(prof)[1]:
            errors.append("focus.scan.from_below: needs stage.z_into_sample (which Z direction approaches the "
                          "objective: TESTING T3.17)")
    try:
        n = len(plan.scan_offsets(p))
        if n < 5:
            errors.append(f"focus.scan: {n} Z steps; give at least 5 (a smaller step_um or a larger range_um)")
        elif n > 81:
            errors.append(f"focus.scan: {n} Z steps; at most 81")
    except (TypeError, ValueError, ZeroDivisionError):
        pass


def _validate_spot(name, study, prof, errors, warnings) -> dict:
    sp = study.get("spot", {})
    if not isinstance(sp, dict):
        errors.append("spot: {method, feature_um, ...}")
        return {"ok": False, "errors": errors, "warnings": warnings}
    _unknown(sp, SPOT_KEYS, "spot", errors)
    method = sp.get("method", "auto")
    if method not in SPOT_METHODS:
        errors.append(f"spot.method {method!r}: {', '.join(SPOT_METHODS)}")
    f = sp.get("feature_um")
    if f is not None and f != "auto" and not (isinstance(f, (list, tuple)) and len(f) == 2 and all(_num(v) for v in f)):
        errors.append("spot.feature_um: [x, y] in µm (from a snapshot) or 'auto'")
    if method == "shutter" and not prof["motors"].get("laser_shutter"):
        errors.append("spot.method 'shutter': the profile has no laser shutter motor (motors.laser_shutter); use "
                      "'acquisition' or 'raman'")
    s = study.get("setup") or {}
    for k in ("laser_nm", "objective"):
        if s.get(k) is None:
            errors.append(f"spot: give setup.{k} (the beam's offset is remembered per objective and laser)")
    for k, lo, hi in (("range_um", 0.5, 200.0), ("step_um", 0.05, 50.0), ("exposure_s", 0.01, 10.0),
                      ("acq_exposure_s", 0.2, 30.0), ("window_cm1", 2.0, 400.0)):
        v = sp.get(k)
        if v is not None and (not _num(v) or not lo <= v <= hi):
            errors.append(f"spot.{k}: between {lo:g} and {hi:g}")
    for k in ("refine", "remember"):
        if k in sp and not isinstance(sp[k], bool):
            errors.append(f"spot.{k}: true or false")
    for k in ("points", "map", "series", "acquire", "guide", "alignment", "process", "move", "video", "focus"):
        if study.get(k) is not None:
            errors.append(f"{k}: not with kind 'spot'")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    p = plan.spot_params(study, prof)
    if method == "raman" or f is not None:
        n = len(plan.axis_values([-p["range_um"] / 2, p["range_um"] / 2, p["step_um"]]))
        if n < 5:
            errors.append(f"spot: {n} steps across; at least 5 (a smaller step_um)")
        elif n * n > 441:
            errors.append(f"spot: {n * n} positions; at most 441 (a larger step_um or a smaller range_um)")
        if p["range_um"] / 2 > prof["limits"]["max_xy_um"]:
            errors.append("spot.range_um: above 2 x limits.max_xy_um")
    if method == "raman" and f is None:
        warnings.append("spot.method 'raman' without feature_um: the feature nearest the centre of the video is used")
    inner = validate_study(name, plan.spot_spectrum_study(study, prof), prof)
    inner["warnings"] = [w for w in inner["warnings"] if not w.startswith("acquire.spike")]
    out = _merge({}, inner, errors, warnings)
    if out["ok"]:
        out["estimate"] = plan.estimate(study, prof)
    return out


def _validate_move(name, study, prof, errors, warnings) -> dict:
    m = study.get("move")
    lim = prof["limits"]
    if not isinstance(m, dict):
        errors.append("move: {x_um, y_um, dz_um, stay, snapshot} (kind move)")
        return {"ok": False, "errors": errors, "warnings": warnings}
    _unknown(m, MOVE_KEYS, "move", errors)
    for k in ("x_um", "y_um", "dz_um"):
        if m.get(k) is not None and not _num(m[k]):
            errors.append(f"move.{k}: a number (µm)")
    if m.get("to_um") is not None:
        t = m["to_um"]
        if not (isinstance(t, (list, tuple)) and len(t) == 2 and all(_num(v) for v in t)):
            errors.append("move.to_um: [x, y] in µm (stage coordinates, as on a snapshot's axes)")
        if m.get("x_um") is not None or m.get("y_um") is not None:
            errors.append("move: give to_um or x_um / y_um, not both")
    for k in ("stay", "snapshot"):
        if k in m and not isinstance(m[k], bool):
            errors.append(f"move.{k}: true or false")
    if not errors:
        d = math.hypot(m.get("x_um", 0) or 0, m.get("y_um", 0) or 0)
        if d > lim["max_xy_um"]:
            errors.append(f"move: {d:g} µm from the home spot, above limits.max_xy_um = {lim['max_xy_um']:g} µm (the "
                          "distance from the home spot, not per axis)")
        if abs(m.get("dz_um", 0) or 0) > lim["max_dz_um"]:
            errors.append(f"move.dz_um: |dz| <= limits.max_dz_um = {lim['max_dz_um']:g} µm")
        if m.get("focus") not in (None, "video"):
            errors.append("move.focus: 'video' (LabSpec's video autofocus after a retract-travel-return; no laser)")
        elif m.get("focus") == "video" and plan.travel_mode(prof, study, d, (study.get("setup") or {}).get("objective")) \
                != "retract":
            errors.append("move.focus 'video': only with a retract-travel-return (move.travel 'retract', or "
                          "sample.unevenness_um with a large move)")
        if m.get("travel") not in (None, "retract", "direct"):
            errors.append("move.travel: 'retract' (Z away by limits.retract_um, travel, back to the old focus minus "
                          "sample.unevenness_um) or 'direct'")
        obj = (study.get("setup") or {}).get("objective")
        if plan.travel_mode(prof, study, d, obj) == "retract":
            un = float((study.get("sample") or {}).get("unevenness_um") or 0)
            if not plan.into_sign(prof)[1]:
                errors.append("move: a retract-travel-return needs stage.z_into_sample (which Z direction approaches "
                              "the objective: TESTING T3.17)")
            elif un >= plan.retract_um(prof):
                errors.append(f"move: sample.unevenness_um ({un:g}) must be below limits.retract_um "
                              f"({plan.retract_um(prof):g})")
            elif m.get("dz_um"):
                errors.append("move.dz_um: not with a retract-travel-return (it ends short of the focus; focus first)")
            if m.get("focus") == "video":
                warnings.append(f"retract-travel-return: Z goes {plan.retract_um(prof):g} µm away, the stage travels, "
                                f"Z comes back to the old focus minus {un:g} µm, then LabSpec's video autofocus (no "
                                "laser; white light on, live video running; LabSpec's own autofocus range applies: ls6t "
                                "checks where it ends and goes back if it is out of bounds)")
            else:
                warnings.append(f"retract-travel-return: Z goes {plan.retract_um(prof):g} µm away, the stage travels, "
                                f"Z comes back to the old focus minus {un:g} µm (away from the sample): focus there "
                                "(move.focus 'video' without the laser, or a 'focus' study with focus.scan.from_below) "
                                "before measuring")
    s = study.get("setup") or {}
    if set(s) - {"objective"}:
        errors.append("setup: only the objective with kind 'move' (no laser is used)")
    elif s.get("objective") is not None and str(s["objective"]) not in [str(o) for o in prof["objectives"]]:
        errors.append(f"setup.objective {s['objective']!r}: use one of {', '.join(map(str, prof['objectives']))}")
    for k in ("points", "map", "series", "acquire", "guide", "alignment", "process", "focus", "video", "expect"):
        if study.get(k) is not None:
            errors.append(f"{k}: not with kind 'move'")
    out = {"ok": not errors, "errors": errors, "warnings": warnings}
    if not errors:
        out["estimate"] = plan.estimate(study, prof)
        if m.get("stay", True):
            warnings.append("stay: the new spot becomes home for later studies and the next request")
    return out


def _validate_alignment(name, study, prof, errors, warnings) -> dict:
    a = study.get("alignment", {})
    if not isinstance(a, dict):
        errors.append("alignment: {guided, focus_guide} (booleans)")
        a = {}
    _unknown(a, ALIGN_KEYS, "alignment", errors)
    for k in ALIGN_KEYS:
        if k in a and not isinstance(a[k], bool):
            errors.append(f"alignment.{k}: true or false")
    for k in ("points", "map", "series", "guide", "process"):
        if study.get(k) is not None:
            errors.append(f"{k}: not with kind 'alignment' (it measures the Si reference at the current spot)")
    s = study.get("setup", {}) or {}
    if s.get("laser_nm") is None:
        errors.append("alignment: give setup.laser_nm (AutoAlignment and its check are per laser)")
        return {"ok": False, "errors": errors, "warnings": warnings}
    k = laser_key(prof, s["laser_nm"])
    fixed = prof.get("alignment", {}).get("settings", {}).get(k or "", {})
    missing = [x for x in ("grating", "filter", "objective") if x not in fixed and s.get(x) is None]
    if missing:
        errors.append(f"alignment: no fixed settings for the {s['laser_nm']} nm laser in the profile "
                      f"(alignment.settings): give setup.{', setup.'.join(missing)}, or the maintainer records them "
                      "with `ls6t alignment record` after a successful AutoAlignment")
    if k and k not in prof.get("alignment", {}).get("baselines", {}):
        warnings.append(f"no alignment baseline for the {s['laser_nm']} nm laser yet: the check measures but cannot "
                        "judge; the maintainer records one after AutoAlignment (`ls6t alignment record`)")
    acq = study.get("acquire", {}) or {}
    bad = set(acq) - {"exposure_s", "accumulations"}
    if bad:
        errors.append(f"acquire: only exposure_s and accumulations with kind 'alignment' (not {', '.join(sorted(bad))})")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    inner = validate_study(name, plan.alignment_check_study(study, prof), prof)
    out = _merge({}, inner, errors, warnings)
    if out["ok"]:
        out["estimate"] = plan.estimate(study, prof)
    return out


def _validate_guide(name, study, prof, errors, warnings) -> dict:
    g = study.get("guide")
    if not isinstance(g, dict):
        errors.append("guide: {instruction, mode, target, ...} (kind guide)")
        return {"ok": False, "errors": errors, "warnings": warnings}
    _unknown(g, GUIDE_KEYS, "guide", errors)
    gl = prof.get("guide", {})
    ins = g.get("instruction")
    if not isinstance(ins, str) or not ins.strip() or len(ins) > 200:
        errors.append("guide.instruction: what the operator should do, in at most 200 characters "
                      "(e.g. 'Raise the laser power at the source slowly')")
    mode = g.get("mode", "reach")
    if mode not in GUIDE_MODES:
        errors.append(f"guide.mode {mode!r}: reach, drop or maximize")
    if mode in ("reach", "drop") and (not _num(g.get("target")) or g["target"] <= 0):
        errors.append(f"guide.target: the line's signal to {mode} (counts/s, > 0)")
    for k, lo, hi in (("exposure_s", 0.01, float(gl.get("max_exposure_s", 5.0))),
                      ("max_s", 1.0, float(gl.get("max_s", 300.0))), ("window_cm1", 2.0, 400.0),
                      ("drop_frac", 0.01, 0.9)):
        v = g.get(k)
        if v is not None and (not _num(v) or not lo <= v <= hi):
            errors.append(f"guide.{k}: between {lo:g} and {hi:g}")
    if g.get("stable_n") is not None and (not isinstance(g["stable_n"], int) or not 1 <= g["stable_n"] <= 20):
        errors.append("guide.stable_n: an integer 1..20 (readings in a row that must meet the goal)")
    for k in ("line_cm1", "center_cm1"):
        if g.get(k) is not None and not _num(g[k]):
            errors.append(f"guide.{k}: a number (cm-1)")
    for k in ("points", "map", "series", "acquire", "alignment", "process", "video"):
        if study.get(k) is not None:
            errors.append(f"{k}: not with kind 'guide' (its acquisitions are set by guide.exposure_s)")
    if (study.get("setup") or {}).get("laser_nm") is None:
        errors.append("guide: give setup.laser_nm (the line's position in nm depends on it)")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    inner = validate_study(name, plan.guide_spectrum_study(study, prof), prof)
    inner["warnings"] = [w for w in inner["warnings"] if not w.startswith("acquire.spike")]
    out = _merge({}, inner, errors, warnings)
    if out["ok"]:
        out["estimate"] = plan.estimate(study, prof)
        p = plan.guide_params(study, prof)
        out["latency_note"] = (f"one reading every ~{p['exposure_s'] + 0.3:.1f} s (exposure + LabSpec overhead): "
                               "the operator should adjust slowly")
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
