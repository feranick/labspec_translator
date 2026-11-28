"""analysis.py — spectral analysis, laser-line tracking, peak/band/map/depth summaries, laser health, and calibration evaluation.

Split from runner.py so post-acquisition analysis and calibration evaluation are separated from hardware control.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from . import config, plan, spectra, store
from . import profile as P
from .io_util import to_float
from .calibration_learn import (
    BROAD_LANDING_MIN_CM1, COARSE_CM1_PER_PX, CREEP_CAP_CM1, CREEP_T, CREEP_VERSION,
    DRIFT_MIN_PAIRS, DRIFT_RAMAN_FRACTION, DRIFT_VERSION, FIRST_LANDING_MIN_N,
    JUMP_AGE_CAP_CM1, JUMP_AGE_CM1_PER_H, JUMP_CONFIRM_MIN_CM1, LANDING_K,
    LANDING_MIN_CM1, LL_SD_DEFAULT_CM1, LL_SD_VERSION, OUTLIER_K, STEP_T, TREND_T,
    WIDTH_CHANGE_FRAC, WINDOW_PAIR_MAX_H, WINDOW_PAIR_MIN_NM, WINDOW_SAME_NM,
    WINDOW_TERM_CAP_CM1, _anchor_repeats, _clean_rep_entry, _cm1_per_px, _common_creep,
    _landing_steps, _learn_first_landing, _repeat_offsets, _robust_sd, _tight_sd,
    _u_total, _unfollowed_term, landing_threshold, learn_laser_line_sd,
    learn_window_rate, learn_zero_creep, learn_zero_drift, repeat_stats,
    spectro_repeatability, zero_creep,
)

SPECTRO_NM_PLAUSIBLE = (100.0, 2000.0)     # a spectrometer reading outside this is a bogus read-back


def _f(v):
    return to_float(v, finite=True)



LASER_JUMP_CM1 = 1.5     # the laser line moving more than this between spectra: a mode hop
ZERO_JUMP_CM1 = 3.0      # followed, but more than this: the corrected zero is uncertain (about 1 cm-1 left)
WEAK_POINT_FRAC = 0.2    # a spectrum whose signal is under this fraction of the study's median: kept out
FOLLOW_MIN_CM1 = 3.0     # 'correct' applies a laser-line step only above this (and with the Raman peaks moving too)
STALE_DRIFT_CM1 = 0.5        # the laser line this far from the calibration's: the correction is stale (warned)
UNCONFIRMED_MIN_CM1 = 5.0    # with no Raman peak to confirm it, only a step above this (flagged unconfirmed)
LASER_LINE_MAX_WIDTH = 2.0   # a calibration's laser line wider than this x its reference line: not a reference
MULTIMODE_WIDTH_RATIO = 1.3  # the reference line this much wider than at the calibration: the laser multimode


def laser_line_too_broad(ll_fwhm, lines) -> str | None:
    """Why a calibration's laser line is no reference, or None (2026-11-24, 600 gr/mm: FWHM 19.4 cm-1, three times
    Si's, fitted 1.3 cm-1 off from one spectrum to the next while Si stayed)."""
    w = [float(l_["fwhm_cm1"]) for l_ in (lines or []) if l_.get("fwhm_cm1") and l_.get("measured_cm1") is not None]
    if not ll_fwhm or not w:
        return None
    ref = float(np.median(w))
    if float(ll_fwhm) > LASER_LINE_MAX_WIDTH * ref:
        return (f"its laser line is {float(ll_fwhm):.1f} cm-1 wide, {float(ll_fwhm) / ref:.1f} x the reference line's "
                f"{ref:.1f}: too broad to be a reference (a narrow line on the edge filter)")
    return None


def screen_points(per_point: list, laser, kind: str = "spectrum", warnings: list | None = None) -> dict:
    """Spectra to keep out of a study's summaries, {point: why} (2026-11-24: a native map's point 0, taken before the
    beam was really on: about 6 counts against 150, its laser line fitted at +1.5 instead of -4.3, Si 538.8, and it
    still entered the map's summary). With the laser line in the window (most spectra have it): a spectrum without
    it, or whose laser line is under WEAK_POINT_FRAC of the study's median height (the leak through the edge filter
    hardly depends on the sample, so the beam was not on). Without it: a signal under WEAK_POINT_FRAC of the median,
    for spectra taken at one place (spectrum, series); in a map a weak point may be off the sample, so it is only
    said. Needs at least three spectra."""
    if len(per_point) < 3:
        return {}
    spans: dict = {}
    lines: dict = {}
    for rec in per_point:
        if "_sp" not in rec:
            try:
                rec["_sp"] = spectra.read(rec["file"])
            except (OSError, ValueError, KeyError):
                continue
        sp = rec["_sp"]
        y = np.asarray(sp["y"], float)
        if y.size < 5 or not np.isfinite(y).any():
            continue
        spans[rec["point"]] = float(np.nanmax(y) - np.nanmedian(y))
        try:
            sh = rec.get("_shift")
            if sh is None:
                sh, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
                rec["_shift"] = sh
            ll_ = rec.get("_ll") if "_ll" in rec else laser_line_fit(sh, y)
            rec["_ll"] = ll_
            lines[rec["point"]] = ll_
        except (ValueError, TypeError):
            pass
    out: dict = {}
    have = {p_: float(l_.get("height") or 0.0) for p_, l_ in lines.items() if l_ and l_.get("ok")}
    if len(have) >= max(2, (len(per_point) + 1) // 2):
        # the reference: the strong ones (when the beam went off for most of a study, the median of all was weak)
        hv = list(have.values())
        hmed = float(np.median([h for h in hv if h >= 0.5 * max(hv)])) if hv else 0.0
        good_before = False
        for rec in per_point:
            p_ = rec["point"]
            if p_ not in spans:
                continue
            # 2026-11-24: points 2-4 of a held series lost the light after 0-1 had it: "went off", not "not on yet"
            when = ("the beam went off (earlier spectra in this study had it)" if good_before
                    else "the beam was probably not on yet")
            if p_ not in have:
                out[p_] = f"no laser line where the study's other spectra have one: {when}"
            elif hmed > 0 and have[p_] < WEAK_POINT_FRAC * hmed:
                out[p_] = (f"its laser line ({have[p_]:.1f}) is under {WEAK_POINT_FRAC:.0%} of the study's strong spectra (median) "
                           f"({hmed:.1f}): {when}")
            else:
                good_before = True
        return out
    sv = list(spans.values())
    med = float(np.median([v for v in sv if v >= 0.5 * max(sv)])) if sv else 0.0
    weak = {p_: v for p_, v in spans.items() if med > 0 and v < WEAK_POINT_FRAC * med}
    if kind in ("spectrum", "series"):
        first_good = min((p_ for p_ in spans if p_ not in weak), default=None)
        for p_, v in weak.items():
            out[p_] = (f"its signal ({v:.1f} above its level) is under {WEAK_POINT_FRAC:.0%} of the study's strong spectra (median) "
                       f"({med:.1f}): " + ("the beam went off (earlier spectra in this study had it)"
                                           if first_good is not None and p_ > first_good
                                           else "the beam was probably not on yet"))
    elif 0 in weak and warnings is not None:
        warnings.append(f"point 0's signal is under {WEAK_POINT_FRAC:.0%} of the map's median: off the sample, or the "
                        "beam not yet on (put the laser line in the window to tell)")
    return out


def calibration_window(study: dict, grating, n_lines: int) -> float:
    """The fit window for a calibration's reference lines: calibration.window_cm1 if given, else scaled with the
    grating's dispersion (30 cm-1 at 1200 gr/mm and finer, 60 at 600, at most 90; 20 for several close lines at fine
    gratings). 2026-11-23: with the 600 the Si line (FWHM 13) sat at the edge of a 30 cm-1 window: not trusted."""
    w = (study.get("calibration") or {}).get("window_cm1")
    if w:
        return float(w)
    base = 20.0 if n_lines > 1 else 30.0
    g = _f(grating)
    if g and g < 1200:
        base = min(90.0, base * 1200.0 / g)
    return base


def laser_line_fit(shift, y) -> dict | None:
    """The laser line (Rayleigh) near 0 cm-1, when the spectrum covers -15 to +15 cm-1; else None."""
    s = np.asarray(shift, float)
    if s.size < 10 or float(np.min(s)) > -15 or float(np.max(s)) < 15:
        return None
    xs, ys = spectra.sort_xy(s, y)
    pk = spectra.fit_peak(xs, ys, 0.0, 40.0, name="laser")
    return {k: pk.get(k) for k in ("ok", "position_cm1", "fwhm_cm1", "height", "snr", "why") if pk.get(k) is not None}


def laser_line_reference(laser, grating, setup: dict | None = None) -> float | None:
    """Where the laser line sat when this laser and grating (and objective and hole: the key of the set-up's
    correction) were last calibrated within tolerance (perf 'zero' records it when the window included it); None:
    the study's first spectrum is the reference."""
    from . import perf, xcal
    rec = None
    if laser:
        rec = perf.get("zero", xcal.key(laser, grating, setup)) if setup and any(setup.values()) else None
        rec = rec or perf.get("zero", perf.key(laser, grating))
    v = ((rec or {}).get("latest") or {}).get("laser_line_cm1")
    return float(v) if v is not None else None


def _poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) for a Poisson count with mean lam."""
    if k <= 0:
        return 1.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - cdf)


LINE_P = 1e-3      # a spike position is a line when its recurrence is this unlikely by chance


def _spikes_where_meaningful(a: dict, laser, grating, ll: dict | None) -> None:
    """Not a cosmic ray: a flag on the window's first or last pixels, below the edge filter's measured cut-off, or on
    the laser line's wings (2026-11-24: of 9 flags in 5 spectra, most were there). Removes them from a's spikes."""
    from . import perf
    x = np.asarray(a.get("_x"), float)
    if not a.get("spikes") or x.size < 8:
        return
    lo_x, hi_x = float(x[3]), float(x[-4])                  # x ascending: three pixels in from each edge
    cut = perf.cutoff_for(laser, grating) if laser else None
    wing = max(20.0, 3.0 * float((ll or {}).get("fwhm_cm1") or 0))
    keep = []
    for i in a["spikes"]:
        xi = float(x[i])
        if xi < lo_x or xi > hi_x or (cut is not None and xi < float(cut)) or abs(xi) <= wing:
            continue
        keep.append(i)
    if len(keep) != len(a["spikes"]):
        kept = {round(float(x[i]), 1) for i in keep}
        a["spike_cm1"] = [c for c in a.get("spike_cm1", []) if c in kept]
        a["spike_info"] = [s for s in a.get("spike_info", []) if s["cm1"] in kept]
        a["spikes"] = keep


def recurring_spikes(per_point: list) -> list:
    """Spike positions that recur in more spectra than chance allows are lines (the Si or the laser line, as narrow
    as a cosmic ray at 600 gr/mm), not cosmic rays: taken out of every spectrum's spikes; returns where they were.
    With n spikes over P pixels in all, another spectrum has one within +/- 2 pixels by chance with mean
    lam = (n - 1) x 5 / P; a position counts as a line when at least 3 spectra have it and that many is unlikely
    (P < LINE_P) under lam, whatever the fraction of spectra: a minority phase's narrow line in 10 % of a map's
    points is a line too (Nicola, 2026-11-24). Only counts and warnings change: ls6t never alters the data."""
    allpos = [(i, float(c), float(r.get("_spike_tol") or 3.0)) for i, r in enumerate(per_point)
              for c in (r.get("spike_cm1") or [])]
    if len({i for i, _, _ in allpos}) < 3:
        return []
    npix = [int(r.get("_npix") or 0) for r in per_point if r.get("_npix")]
    p_ = float(np.median(npix)) if npix else 1024.0
    lam = (len(allpos) - 1) * 5.0 / max(p_, 1.0)
    lines = []
    for i, c, tol in allpos:
        spectra_with = {j for j, c2, _ in allpos if abs(c2 - c) <= tol}
        k_other = len(spectra_with) - 1
        if len(spectra_with) >= 3 and _poisson_tail(k_other, lam) < LINE_P \
                and not any(abs(c - l_) <= tol for l_ in lines):
            lines.append(round(c, 1))
    if not lines:
        return []
    for r in per_point:
        tol = float(r.get("_spike_tol") or 3.0)
        kept = [c for c in (r.get("spike_cm1") or []) if not any(abs(c - l_) <= tol for l_ in lines)]
        if len(kept) != len(r.get("spike_cm1") or []):
            r["spikes"] = max(0, int(r.get("spikes") or 0) - (len(r["spike_cm1"]) - len(kept)))
            r["spike_cm1"] = kept
            r["spike_info"] = [s_ for s_ in (r.get("spike_info") or []) if s_.get("cm1") in kept]
    return sorted(lines)


def same_spot_check(kind: str, per_point: list, warnings: list, unreliable: list) -> dict | None:
    """Spectra at the same spot should agree. 2026-11-23: in a request, point 0 had Si at 520.3 cm-1 (FWHM 3.9) and
    points 1-4 at 512 cm-1 (FWHM 9-10, area kept): every spectrum after the first was wrong, and nothing in LabSpec's
    records showed it. Each spectrum's first fitted peak is compared with point 0's: more than 2 cm-1 apart or 1.5 x
    as wide is flagged (unreliable for repeated spectra; a warning for a series, which may change on purpose)."""
    pts = [r for r in per_point if r.get("peaks")]
    if len(pts) < 2:
        return None
    def key(r):
        o = r.get("offset") or {}
        return tuple(round(float(o.get(k) or 0), 3) for k in ("x_um", "y_um", "dz_um"))
    first = pts[0]
    if kind != "series" and any(key(r) != key(first) for r in pts):
        return None
    p0 = next((pk for pk in first["peaks"] if pk.get("ok")), None)
    if not p0:
        return None
    rows, bad = [], []
    for r in pts[1:]:
        pk = next((q for q in r["peaks"] if q.get("ok") and q.get("name") == p0.get("name")), None)
        if not pk:
            continue
        d = float(pk["position_cm1"]) - float(p0["position_cm1"])
        w = float(pk.get("fwhm_cm1") or 0) / float(p0.get("fwhm_cm1") or 1)
        rows.append({"point": r["point"], "shift_cm1": round(d, 2), "fwhm_ratio": round(w, 2)})
        if abs(d) > 2.0 or w > 1.5:
            bad.append(rows[-1])
    out = {"peak": p0.get("name"), "reference_point": first["point"], "points": rows, "disagree": bad}
    if bad:
        msg = (f"spectra at the same spot disagree with point {first['point']}: "
               + ", ".join(f"point {b['point']} {b['shift_cm1']:+.1f} cm-1, {b['fwhm_ratio']:.1f} x as wide" for b in bad[:6])
               + ". Something changed between acquisitions (TESTING T3.21); the optics_check of each spectrum says "
                 "what the bridge read around it")
        (warnings if kind == "series" else unreliable).append(msg)
    return out


# --------------------------------------------------------------------------------------------- analysis

def _stats(vals) -> dict:
    a = np.asarray([v for v in vals if v is not None and math.isfinite(v)], float)
    if not a.size:
        return {"n": 0}
    return {"n": int(a.size), "mean": float(a.mean()), "std": float(a.std()), "min": float(a.min()),
            "max": float(a.max())}


def stitch_seams(x) -> list:
    """Where a LabSpec-stitched spectrum changes window: the point spacing breaks away from its smooth trend there
    (2026-11-24: 1.56 -> 1.12 -> 1.60 cm-1 at ~1945 cm-1; a persistent-change test missed this one-step dip)."""
    from scipy.signal import medfilt
    x = np.sort(np.asarray(x, float))
    if x.size < 60:
        return []
    dx = np.diff(x)
    trend = medfilt(dx, 41)
    trend[:20] = trend[20]
    trend[-20:] = trend[-21]
    off = np.abs(dx - trend) > 0.15 * np.abs(trend)
    out = []
    i = 5
    while i < dx.size - 5:
        if off[i]:
            j = i
            while j + 1 < dx.size and off[j + 1]:
                j += 1
            out.append(round(float(x[(i + j + 1) // 2]), 1))
            i = j + 10
        else:
            i += 1
    # and a lasting change of spacing (the next window samples differently)
    i = 20
    while i < dx.size - 20:
        a_, b_ = float(np.median(dx[i - 20:i])), float(np.median(dx[i:i + 20]))
        if a_ > 0 and abs(b_ / a_ - 1) > 0.1 and abs(dx[i] - a_) > 0.15 * a_:
            if not any(abs(float(x[i]) - o) < 30 * a_ for o in out):
                out.append(round(float(x[i]), 1))
            i += 30
        else:
            i += 1
    return sorted(out)


def window_centre_used(rng: dict, per_point: list) -> tuple:
    """The spectrometer's real centre (nm) for a study's spectra, and where it comes from. One window centred by ls6t:
    the centre asked. A range: LabSpec's Acq places the window itself (2026-11-24: -100..700 cm-1 at 637.75 nm, ls6t
    put the spectrometer at 650.19 nm, Acq moved it to 676.75 nm, the window's blue edge at From and the data cut to
    the range, so centred about 650.6): the spectrometer's reading after the data. The per-window corrections are
    kept and looked up by this centre."""
    if rng.get("single", True):
        return plan.window_centre(rng), "the centre asked (ls6t placed the spectrometer)"
    reads = [(r.get("optics_check") or {}).get("spectro_after_data") for r in per_point]
    reads = [float(v) for v in reads if v not in (None, "")
             and SPECTRO_NM_PLAUSIBLE[0] <= float(v) <= SPECTRO_NM_PLAUSIBLE[1]]
    if reads and rng.get("windows") == 1:
        return (round(float(np.median(reads)), 4),
                "the spectrometer's reading after the data (LabSpec placed the window for the range itself)")
    if rng.get("windows") == 1:
        return None, "unknown: LabSpec placed the window for the range and the spectrometer was not read"
    return None, "an extended range (several windows)"


def _analyse_zero_and_bg(study, per_point, prof, laser, rng, warnings, unreliable, checks,
                         grating, objective, hole, kind, process):
    """Stage 1 of analyse(): resolve window centre, xcal correction, expected uncertainty, and background."""
    from . import background as BG
    from . import xcal
    # ls6t's own spectral correction (from an earlier calibration), never on a calibration itself
    wc, wc_from = window_centre_used(rng, per_point)
    if not rng.get("single", True):
        checks["window_centre"] = {"nm": wc, "from": wc_from,
                                   "range_centre_nm": plan.window_centre(rng)}
    placement = "labspec" if (not rng.get("single", True)) else "centred"
    ext_range = [rng["from_cm1"], rng["to_cm1"]] if (not rng.get("single", True) and (rng.get("windows") or 1) > 1
                                                      and rng.get("from_cm1") is not None) else None
    # no correction on a calibration, nor on a laser check (raw positions: 2026-11-27 01:38, a check at 260 cm-1
    # applied a 27 h old correction from another window and added its warnings)
    setup_ = xcal.setup_of(objective, hole)
    corr = (xcal.get(laser, grating, wc, placement, range_cm1=ext_range, setup=setup_)
            if kind != "calibration" and not process.get("laser_check") else None)
    if laser and corr is None and kind != "calibration" and not process.get("laser_check"):
        oth_ = xcal.other_setups(laser, grating, setup_)
        if oth_:
            # never a correction of another hole or objective (Nicola, 2026-11-27: Si 0.53 cm-1 apart between holes
            # 100 and 300 at 532, 1.15 at 638): said, and these spectra stay uncorrected
            checks["zero_setup"] = {"this": xcal.describe(xcal.key(laser, grating, setup_)), "corrections_for": oth_}
            warnings.append(f"no spectral correction for {combo(laser, grating)} with "
                            f"{xcal.describe(xcal.key(laser, grating, setup_))} (there are for: {'; '.join(oth_)}): "
                            "these spectra are not corrected; calibrate with this objective and hole")
    if kind != "calibration" and laser:
        zs = zero_state(laser, grating, prof, setup_)
        checks["zero"] = zs
        if corr:
            # (2026-11-24: checks.zero showed the last calibration stored, -2.09 @ 676.75 nm, while the correction
            # used was -0.01 @ 650.19): the one applied here, and what the rest is
            # LabSpec places extended ranges and its own maps' windows itself: no approach there, their own entry
            native_ = kind == "map" and plan.map_engine(study, prof) == "native"
            # a native map counts as ls6t-placed only when the spectrometer was read unmoved through LabSpec's map
            rep_place = "labspec" if (placement == "labspec" or (native_ and per_point and per_point[0].get(
                "native_spectro_moved_by_labspec", True))) else "centred"
            rep_ = spectro_repeatability(prof, laser, grating, rep_place)
            age_m_ = xcal.age_min(corr)
            creep_ = rep_.get("zero_creep_cm1_per_min")
            creep_term = (min(CREEP_CAP_CM1, abs(float(creep_)) * float(age_m_))
                          if creep_ and age_m_ is not None else 0.0)
            # and over hours, from successive calibrations at one window (learned; none known, none counted)
            drh_ = rep_.get("zero_drift_cm1_per_h")
            if drh_ and age_m_ is not None and age_m_ > 15.0:
                creep_term = math.hypot(creep_term, min(CREEP_CAP_CM1, float(drh_) * float(age_m_) / 60.0))
            if age_m_ is not None:
                corr = dict(corr, age_min=round(float(age_m_), 1))
            # another window centre than the calibration's: the zero differs between windows (2026-11-25, 637.75 @
            # 1200: calibrated at 659.66 nm, measured centred ~0.9 nm away: Si +0.66 against +-0.25 expected)
            dist_ = None
            if corr.get("pooled") and wc is not None and corr.get("pooled_from"):
                # a pooled correction: the distance to the nearest centre it was measured at (T9.19: 663.86 nm
                # against 659.66 / 660.76, not reported)
                dist_ = min(abs(float(wc) - float(c_)) for c_ in corr["pooled_from"])
            elif not corr.get("pooled") and not corr.get("range_match"):
                if corr.get("interpolated_between_nm") and wc is not None:
                    dist_ = min(abs(float(wc) - float(c_)) for c_ in corr["interpolated_between_nm"])
                elif corr.get("centre_distance_nm") is not None:
                    dist_ = float(corr["centre_distance_nm"])
            # the rate is learned from calibrations at different centres (or set in the profile): none known, no term
            # (.21 assumed 0.75 cm-1/nm from a case that was 0.19 nm off, not 0.9: Nicola, 2026-11-25)
            wrate_ = rep_.get("window_zero_cm1_per_nm")
            centre_term = (min(WINDOW_TERM_CAP_CM1, float(wrate_) * dist_)
                           if wrate_ and dist_ is not None and dist_ > WINDOW_SAME_NM else 0.0)
            if centre_term < 0.1:
                centre_term = 0.0
            if dist_ is not None and dist_ > WINDOW_SAME_NM:
                corr = dict(corr, window_distance_nm=round(dist_, 3))
            if centre_term:
                if kind != "calibration" and laser and (corr.get("centre_nm") is not None):
                    c_cm1 = round(1e7 / float(laser) - 1e7 / float(wc), 1) if wc else None
                    warnings.append(f"the correction was calibrated at a window {dist_:.2f} nm from this one: the zero "
                                    f"may differ by about +-{centre_term:.1f} cm-1 here; for the best zero calibrate at "
                                    "this window" + (f" (calibrate_si with center_cm1={c_cm1:g}, or calibration "
                                                     f"acquire.center_cm1 {c_cm1:g})" if c_cm1 is not None else ""))
            # the first landing after a grating change, where no laser line can bring it back (600: ~+1.4)
            fl_ = rep_.get("first_landing_cm1")
            can_follow_ = (corr.get("laser_line_cm1") is not None
                           and not laser_line_too_broad(corr.get("laser_line_fwhm_cm1"), corr.get("lines")))
            # applied once learned from several calibrations (Nicola, 2026-11-26: 2.34 from one warm-up)
            n_fl_ = len(rep_.get("first_landing_history") or []) if rep_.get("source") != "profile" or \
                not ((((prof.get("spectrometer") or {}).get("repeatability") or {}).get(
                    f"{float(laser):g}@{_f(grating) or 0:g}") or {}).get("first_landing_cm1")) else FIRST_LANDING_MIN_N
            first_term = float(fl_) if (fl_ and checks.get("grating_changed") and not can_follow_
                                        and n_fl_ >= FIRST_LANDING_MIN_N) else 0.0
            # the correction's own uncertainty: its repeats' standard error (600: SD ~0.27 over 5 -> ~0.12)
            sem_term = float(((corr.get("repeats") or {}).get("sem_cm1")) or 0.0)
            if rep_.get("moved_sd_cm1") or creep_term or centre_term or sem_term:
                # the zero across spectrometer moves is no better than the spectrometer repeats (2026-11-24, 600:
                # SD 0.9-1.2 cm-1 moved, 0.12 without moves); and it creeps with the correction's age (2026-11-25,
                # 637.75 @ 1200: ~+0.1 cm-1 per minute after a calibration); the pass tolerance stays as it is
                mv_ = float(rep_.get("moved_sd_cm1") or 0.0)
                # its parts, so a later term for the same cause replaces rather than adds (the landing: the learned
                # first landing, or the Raman peaks' observed move: T9.18, 2.34 and 1.57 gave +-2.84)
                parts_ = {"repositioning": round(mv_, 3), "drift": round(creep_term, 3),
                          "first_landing": round(first_term, 3), "window": round(centre_term, 3),
                          "correction_sem": round(sem_term, 3)}
                corr = dict(corr, expected_uncertainty_cm1=_u_total(parts_),
                            expected_uncertainty_parts={k_: v_ for k_, v_ in parts_.items() if v_},
                            expected_uncertainty_from=f"{rep_.get('source')}, {rep_place} placement"
                            + (f"; the zero's drift since the calibration ({float(age_m_):.0f} min: "
                               + (f"creep {abs(float(creep_)):.2f} cm-1/min" if creep_ else "")
                               + (", " if creep_ and drh_ else "")
                               + (f"{float(drh_):.2f} cm-1/h between calibrations" if drh_ else "")
                               + f", learned; at most {CREEP_CAP_CM1:g} each)" if creep_term and (creep_ or drh_) else "")
                            + (f"; the first landing after a grating change ({first_term:.2f} cm-1 learned; no laser "
                               "line to bring it back)" if first_term else "")
                            + (f"; the correction's own standard error ({sem_term:.2f})" if sem_term else "")
                            + (f"; another window centre ({dist_:.2f} nm away: {float(wrate_ or 0):.2f} cm-1 per nm "
                               f"from {rep_.get('window_zero_from') or rep_.get('source')}, at most "
                               f"{WINDOW_TERM_CAP_CM1:g})" if centre_term else ""))
                rep_ = dict(rep_, moved_sd_cm1=corr["expected_uncertainty_cm1"])
                # the warning quoting it is said once, at the end of the analysis, with the final value
            zs = checks["zero"] = dict(zs, correction_used={**{k: corr.get(k) for k in (
                "a", "b", "time", "centre_nm", "interpolated_between_nm", "centre_far", "laser_line_cm1", "placement",
                "range_match", "range_cm1", "pooled", "pooled_from", "repeats", "expected_uncertainty_cm1",
                "expected_uncertainty_from", "expected_uncertainty_parts", "age_min", "window_distance_nm", "hole_um",
                "objective")
                if corr.get(k) is not None}, "key": xcal.key(laser, grating, setup_),
                **({"age_h": round(xcal.age_h(corr), 2)} if xcal.age_h(corr) is not None else {})},
                note="offset_cm1 is the last calibration check of this laser, grating, objective and hole (any "
                     "window); correction_used is the one applied to these spectra")
            diff_ = []
            if corr.get("hole_um") is not None and _f(hole) is not None and abs(float(corr["hole_um"]) - _f(hole)) > 1:
                diff_.append(f"the confocal hole {_f(hole):g} um (the calibration's {float(corr['hole_um']):g})")
            if corr.get("objective") and objective not in (None, "") and str(corr["objective"]) != str(objective):
                diff_.append(f"the objective {objective} (the calibration's {corr['objective']})")
            if diff_ and kind != "calibration":
                # Si moved 0.5-0.7 cm-1 between holes 100 and 300 (Nicola, 2026-11-27)
                zs["setup_differs"] = diff_
                warnings.append("this spectrum's set-up differs from its calibration's: " + "; ".join(diff_)
                                + ": the line's position can differ by ~0.5 cm-1 or more; calibrate with this set-up")
        tol0 = float(prof["checks"]["si_tol_cm1"])
        if not corr and zs.get("offset_cm1") is not None and abs(float(zs["offset_cm1"])) > tol0:
            warnings.append(f"the last calibration check for {combo(laser, grating)} ({zs['time']}) found the "
                            f"reference line {float(zs['offset_cm1']):+.2f} cm-1 off: the positions here carry it. "
                            "Correct LabSpec's zero for this grating, or run a calibration with adjust 'ls6t'")
        elif zs.get("offset_cm1") is None and zs.get("other_set_ups") and not checks.get("zero_setup"):
            warnings.append(f"no calibration check yet for {combo(laser, grating)}, {zs.get('setup') or ''} (checked "
                            "with this laser: "
                            + ", ".join(f"{k} {v:+.2f} cm-1" for k, v in zs["other_set_ups"].items()
                                        if v is not None)
                            + "); each grating has its own zero in LabSpec")
    if corr:
        age = xcal.age_h(corr)
        where = (f", measured with the window centred at {corr['centre_nm']:g} nm" if corr.get("centre_nm") is not None
                 else "")
        if corr.get("interpolated_between_nm"):
            where = (f", interpolated between the windows centred at {corr['interpolated_between_nm'][0]:g} and "
                     f"{corr['interpolated_between_nm'][1]:g} nm")
        warnings.append(f"spectral correction applied: true = {corr['a']:+.3f} + {corr['b']:.6f} x measured "
                        f"(calibration {corr['time']}, {corr['reference']}{where})")
        # the zero (and the dispersion) differ between spectrometer windows: 1.7 cm-1 between two windows a minute
        # apart (2026-11-24)
        if corr.get("centre_far"):
            warnings.append(f"the correction was measured with the window centred at {corr['centre_nm']:g} nm, this "
                            f"study's is {wc:g} nm ({corr['centre_distance_nm']:g} nm away): the "
                            "zero differs between windows by a cm-1 or more; calibrate with this study's window "
                            "(center_cm1 or range_cm1), or keep the laser line in the window with "
                            "acquire.laser_reference 'correct'")
        elif ext_range and not corr.get("range_match"):
            warnings.append(f"this range is stitched by LabSpec from {rng.get('windows')} windows, each with its own "
                            "zero; the correction applied was measured in one window (2026-11-24, 637.75 @ 1200, "
                            "100-3200 cm-1: Si read 1.5 cm-1 low with it): calibrate with this same range_cm1 and ls6t "
                            "keeps that correction for this range")
        elif corr.get("centre_unknown"):
            warnings.append("the correction's window centre is not known (a calibration stored before "
                            "2026.11.24.8): the zero differs between windows")
        if not corr.get("zero_mode"):
            # (2026-11-25: a correction stored from a calibration run with laser_reference correct_each, -0.64 at
            # 1200, carried that study's laser-line shift into 'report' spectra: Si 520.40 vs 521.0)
            warnings.append("the correction in use was stored before 2026.11.25.13: if its calibration ran with "
                            "acquire.laser_reference correct or correct_each, its zero carries that study's laser-line "
                            "shift; calibrate again")
        lkc = P.laser_key(prof, laser) if laser else None
        max_h = float((prof["lasers"][lkc].get("calibration_max_age_h") if lkc else None)
                      or prof.get("calibration", {}).get("max_age_h", 24))
        Lc = prof["lasers"][lkc] if lkc else {}
        if age is not None and age <= max_h and age > xcal.POOL_WINDOW_H and (Lc.get("mode_hops") or
                                                                           Lc.get("unstable_multi")):
            # how well it repositions: the profile's / measured repeatability for this grating (2026-11-25: the
            # fixed "+-1 cm-1 at 600" predated approach_nm, with which the moved SD is ~0.3)
            rp_w = spectro_repeatability(prof, laser, grating, placement)
            rp_txt = (f" (it repositions to about +-{float(rp_w['moved_sd_cm1']):.2g} cm-1 at {_f(grating) or 0:g} "
                      f"gr/mm, {rp_w.get('source')})" if rp_w.get("moved_sd_cm1") else "")
            warnings.append(f"the spectral correction is {age:.1f} h old and this laser's zero drifts (2026-11-24: "
                            "2-3 cm-1 within a day at 637.75 nm): calibrate in the same request for the best zero, "
                            "at the same centre, without moving the spectrometer in between" + rp_txt)
        if age is not None and age > max_h:
            warnings.append(f"the spectral correction is {age:.1f} h old (more than {max_h:g} h for this laser): run "
                            "a calibration" + (", or keep the laser line in the window with "
                                               "acquire.laser_reference 'correct'"
                                               if lkc and prof["lasers"][lkc].get("calibration_max_age_h") else ""))
    bgname = study.get("acquire", {}).get("subtract_background")
    bg = BG.load(bgname) if bgname else None
    if bgname and bg is None:
        unreliable.append(f"the white-light background {bgname!r} is not stored (a 'background' study makes it)")
    elif bg:
        bm = bg["meta"]
        if grating and bm.get("grating") and abs(float(bm["grating"]) - float(grating)) > 0.5:
            warnings.append(f"the background {bgname!r} was taken with the {bm['grating']:g} gr/mm grating, this "
                            f"spectrum with {grating:g}")
        if laser and bm.get("laser_nm") and abs(float(bm["laser_nm"]) - float(laser)) > 1:
            warnings.append(f"the background {bgname!r} was taken with the {bm['laser_nm']:g} nm laser selected")
    fs = prof["detector"]["full_scale"]
    frac = prof["checks"]["saturation_frac"]
    min_snr = prof["checks"]["min_snr"]
    sat_points, spike_points, cover_short = [], [], []
    spike_mode = study.get("acquire", {}).get("spike", "labspec")
    axes, ys = [], []
    # the laser line, when the window includes it (2026-11-23: the 637.75 nm laser hopped by ~0.35 nm after the first
    # spectrum of a request: laser line -4.6 -> -12 cm-1 and Si 520.2 -> 512.4, their difference constant)
    lk_ = P.laser_key(prof, laser) if laser else None
    # the laser's own default (2026-11-24: the 637.75 nm laser's wavelength drifts, about 0.07 nm overnight and
    # +0.5 cm-1 within 30 s: 'correct' follows it whenever the laser line is in the window)
    return {
        "wc": wc, "placement": placement, "ext_range": ext_range, "setup_": setup_,
        "corr": corr, "bg": bg, "fs": fs, "frac": frac, "min_snr": min_snr,
        "sat_points": sat_points, "spike_points": spike_points, "cover_short": cover_short,
        "spike_mode": spike_mode, "axes": axes, "ys": ys, "lk_": lk_,
    }


def _analyse_laser_line_tracking(study, per_point, prof, laser, warnings, checks,
                                 grating, kind, process, st):
    """Stage 2 of analyse(): screen weak points and track laser-line plateaus/hops against Raman peaks."""
    from . import xcal
    wc, corr, placement, setup_, lk_ = st["wc"], st["corr"], st["placement"], st["setup_"], st["lk_"]
    ll_asked = study.get("acquire", {}).get("laser_reference")
    ll_mode = ll_asked or ((prof["lasers"][lk_].get("laser_reference") if lk_ else None) or "report")
    ll_mode_asked = ll_mode
    # a correction measured relative to a narrow laser line (its calibration stored where the line was): later
    # spectra are brought to that line first, landing and all (Nicola, 2026-11-25: at 1200 the repeats' Si scattered
    # 0.76 cm-1 with the spectrometer's landing, Si - laser line 0.06). Asked for explicitly, the study's own choice
    corr_broad = bool(corr and laser_line_too_broad(corr.get("laser_line_fwhm_cm1"), corr.get("lines")))
    if (not ll_asked and kind != "calibration" and ll_mode in ("report", "correct") and corr
            and corr.get("laser_line_cm1") is not None):
        # a broad line (600 gr/mm) too, but only where a well fitted Raman peak moved alike (the plateau rules
        # below): it misjudges moves alone (+3.48 then +1.92 with no move, Si steady: 2026-11-26 11:44), yet a real
        # hop moves both (12:50: line -5.1, Si -6.4)
        ll_mode = ll_mode_asked = "anchor"
    # what a landing step must exceed: none for a correction anchored throughout (calibration.anchor 'always'),
    # else LANDING_K x the laser line's own scatter, learned per laser@grating (Nicola, 2026-11-25)
    # threshold 0 only for a correction anchored through its repeats (calibration.anchor 'always'); a single-spectrum
    # one stored 'anchored' before .26.5 is taken as absolute (T9.22: "over 0 cm-1" at 1200 from the 13:25 one)
    anchored_true = ((corr or {}).get("zero_mode") == "anchored"
                     and bool(((corr or {}).get("repeats") or {}).get("anchored_to_laser_line")))
    land_thr = (0.0 if anchored_true else
                float(landing_threshold(prof, laser, grating, placement)["threshold_cm1"])) if laser else 0.0
    if corr_broad:
        land_thr = max(land_thr, BROAD_LANDING_MIN_CM1)
    anchor = ll_mode == "anchor"
    if anchor:
        ll_mode = "correct"
    # 'correct' follows the laser line's steps (a mode hop), not its slow drift: following every spectrum's line
    # over-corrected a series (2026-11-24: Si 520.52 -> 520.00 while the laser line crept up; the line's measured
    # position moves more than the Raman lines, partly its shape on the edge filter). 'correct_each' is the old way.
    follow_each = ll_mode == "correct_each"
    if follow_each:
        ll_mode = "correct"
    if corr_broad and follow_each:
        # a broad laser line is never followed spectrum by spectrum (it moves on its own: 2026-11-26 11:44)
        follow_each = False
        warnings.append("the laser line is broad at this grating (clipped by the edge filter): followed only where a "
                        "fitted Raman peak moved alike, not spectrum by spectrum (correct_each set aside)")
    # The two zero corrections must not count the same thing twice: a stored correction (xcal) was measured with the
    # laser where it was then. With laser_reference 'correct' each spectrum is first brought back to the laser line
    # of THAT calibration (stored with it), then the correction applies: the stored offset relative to the laser
    # line. If the correction has no laser line (calibrated without it in the window), the spectra are brought to the
    # study's first laser line (the drift within the study) and the stored offset applies as measured: the laser's
    # move since the calibration is then not covered, and the result says so (zero_correction).
    ll_ref_cal = False
    if corr and ll_mode == "correct":
        broad = laser_line_too_broad(corr.get("laser_line_fwhm_cm1"), corr.get("lines"))
        if broad and corr.get("laser_line_cm1") is not None:
            broad = None                              # a broad line too: only Raman-confirmed steps (plateau rules)
        if corr.get("laser_line_cm1") is not None and not broad:
            ll_ref, ll_ref_from = float(corr["laser_line_cm1"]), f"the stored correction's calibration ({corr['time']})"
            ll_ref_cal = True
        else:
            ll_ref, ll_ref_from = None, None          # the study's first plateau, below
            if broad:
                warnings.append(f"the stored correction's laser line is not used as the reference ({broad}): the "
                                "study's first laser-line plateau is")
    elif corr and corr.get("laser_line_cm1") is not None:
        # the correction in use: its laser line is the repeats' median (2026-11-25: reference_cm1 showed the
        # calibration's first repeat, -3.53, while the drift was measured from the median, -4.27)
        ll_ref, ll_ref_from = float(corr["laser_line_cm1"]), f"the stored correction's calibration ({corr['time']})"
    else:
        # no correction for this set-up: no other set-up's laser line as the reference either (Nicola, 2026-11-27: a
        # hole change read as 'the laser line moved alone ... calibrate again'); the study's first spectrum is it
        other_setup_ = kind != "calibration" and not corr and bool(checks.get("zero_setup"))
        ll_ref = None if other_setup_ else laser_line_reference(laser, grating, setup_)
        ll_ref_from = "calibration" if ll_ref is not None else None
        if kind == "calibration" and laser:
            # the last stored correction's, not the last calibration check's (which may not have been stored: T9.23)
            st_ = (xcal.at_centre(laser, grating, wc, placement, setup=setup_)
                   or xcal.get(laser, grating, wc, placement, setup=setup_) or xcal.get(laser, grating, wc, placement))
            if st_ and st_.get("laser_line_cm1") is not None:
                ll_ref, ll_ref_from = float(st_["laser_line_cm1"]), f"the stored correction ({st_.get('time')})"
    from . import runner as _R
    excluded = _R.screen_points(per_point, laser, kind, warnings) if laser else {}
    for rec in per_point:
        if rec["point"] in excluded:
            rec["excluded"] = excluded[rec["point"]]
    # The laser line of every spectrum first, in acquisition order, with the Raman peaks' raw positions. A plateau
    # ends where the line steps by more than LASER_JUMP_CM1 from the current one's median AND the Raman peaks moved
    # with it (a mode hop moves everything). A step the peaks did not follow is a bad laser-line fit (2026-11-24, 600
    # gr/mm: the line about 2 px wide on the edge filter fitted +3.6 / +5.9 cm-1 off while Si stayed): that spectrum
    # keeps its plateau and its line is not used. 'correct' follows each plateau's median.
    plateau_of: dict = {}
    plateaus: list = []
    ll_bad: dict = {}
    ll_bad_idx: dict = {}
    hop_info: list = []
    pk_specs = [(float(pk_["near_cm1"]), float(pk_.get("window_cm1", 40.0))) for pk_ in (process.get("peaks") or [])
                if isinstance(pk_, dict) and pk_.get("near_cm1") is not None]
    pk_names = [str(pk_.get("name") or f"peak{i_}") for i_, pk_ in enumerate(process.get("peaks") or [])
                if isinstance(pk_, dict) and pk_.get("near_cm1") is not None]
    if not pk_specs:
        # no peaks asked: the strongest ones found in the first spectrum confirm laser-line steps too (T9.18: an
        # auto-found Si at 520.19 did not confirm a -0.57 step, "no Raman peak to confirm it")
        try:
            r0 = next(r_ for r_ in per_point if r_["point"] not in excluded)
            sp0 = r0.get("_sp") or spectra.read(r0["file"])
            sh0, _ = spectra.to_shift(sp0["x"], sp0["meta"].get("x_unit", "nm"), laser)
            x0_, y0_ = spectra.sort_xy(sh0, sp0["y"])
            yc0 = np.asarray(y0_, float) - spectra.baseline(x0_, y0_, 3)
            pk_specs = [(p_, float(min(200.0, max(30.0, 3.0 * w_))))
                        for p_, w_ in spectra.find_peaks(x0_, yc0, 3, widths=True) if abs(p_) > 40.0]
            pk_names = [f"found at {p_:.1f}" for p_, _ in pk_specs]
        except (StopIteration, KeyError, OSError, ValueError, TypeError):
            pk_specs = []
    raw: list = []
    for rec in per_point:
        if rec["point"] in excluded:
            continue
        if "_sp" not in rec:
            rec["_sp"] = spectra.read(rec["file"])
        try:
            sh_ = rec.get("_shift")
            if sh_ is None:
                sh_, _ = spectra.to_shift(rec["_sp"]["x"], rec["_sp"]["meta"].get("x_unit", "nm"), laser)
                rec["_shift"] = sh_
        except (ValueError, TypeError):
            continue
        l_ = rec.get("_ll") if "_ll" in rec else laser_line_fit(sh_, rec["_sp"]["y"])
        rec["_ll"] = l_
        if not (l_ and l_.get("ok")):
            continue
        xs_, ys_ = spectra.sort_xy(sh_, rec["_sp"]["y"])
        try:
            ys_ = np.asarray(ys_, float) - spectra.baseline(xs_, ys_, 3)
        except Exception:  # noqa: BLE001 - the raw spectrum then
            pass
        pks_ = []
        for near_, win_ in pk_specs:
            try:
                f_ = spectra.fit_peak(xs_, ys_, near_, win_)
            except Exception:                    # a fit that cannot run here only leaves the hop unconfirmed
                f_ = {}
            # only a well fitted peak confirms (T9.19: a step "confirmed" with Si not fitted in the result)
            good_ = (f_.get("ok") and f_.get("position_cm1") is not None
                     and (f_.get("r2") is None or float(f_["r2"]) >= 0.8))
            pks_.append(float(f_["position_cm1"]) if good_ else None)
        raw.append((rec["point"], float(l_["position_cm1"]), pks_))
    cur: list = []
    for pt, pos_, pks_ in raw:
        if cur:
            m_ = float(np.median([v for _, v, _ in cur]))
            d_ = pos_ - m_
            if abs(d_) > LASER_JUMP_CM1:
                moved = []
                for j, v in enumerate(pks_):
                    prev = [c[2][j] for c in cur if c[2][j] is not None]
                    if v is not None and prev:
                        moved.append(v - float(np.median(prev)))
                if moved and abs(float(np.median(moved)) - d_) > max(0.5 * abs(d_), 1.0):
                    ll_bad_idx[pt] = len(plateaus)
                    ll_bad[pt] = (f"the laser line fitted {d_:+.1f} cm-1 from its neighbours while the Raman peaks "
                                  f"moved {float(np.median(moved)):+.1f}: a bad fit (a narrow line on the edge "
                                  "filter), not a hop; not used")
                    plateau_of[pt] = m_
                    continue
                # everything moving together is a laser hop only when the spectrometer stayed put (2026-11-24: at
                # 600 gr/mm a spectrometer landing ~0.5 px elsewhere after its move looked the same, +-2.4 cm-1)
                smove = bool(((next((r_ for r_ in per_point if r_["point"] == pt), {}) or {}).get("optics_check")
                              or {}).get("spectro_moved_by_ls6t"))
                hop_info.append({"point": pt, "moved_cm1": round(d_, 2),
                                 "confirmed_by_peaks": (round(float(np.median(moved)), 2) if moved else None),
                                 "after_spectrometer_move": smove,
                                 "what": "a laser hop or the spectrometer repositioning (it was moved just before)"
                                 if smove else "a laser mode hop (the spectrometer did not move)"})
                plateaus.append(cur)
                cur = []
        cur.append((pt, pos_, pks_))
    if cur:
        plateaus.append(cur)
    raw_pk = {pt: pks_ for pl in plateaus for pt, _, pks_ in pl}
    plateaus = [[(pt, v) for pt, v, _ in pl] for pl in plateaus]
    for pl in plateaus:
        med = float(np.median([v for _, v in pl]))
        for pt, _ in pl:
            plateau_of[pt] = med
    # Which plateaus 'correct' follows (2026-11-24: three cases where following a small laser-line offset made Si
    # worse, the line on the edge filter's slope moving about twice as much as the Raman lines; 600 gr/mm: -1.28
    # applied, Si 519.18 instead of 520.46): only a step over FOLLOW_MIN_CM1 from the reference that the Raman peaks
    # follow too. The peaks' reference: where the calibration's lines would read in this window (the stored
    # correction inverted), or the study's first plateau.
    plateau_step: dict = {}
    plateau_report: list = []
    if ll_mode == "correct" and not follow_each and plateaus:
        if ll_ref is None:
            ll_ref = float(np.median([v for _, v in plateaus[0]]))
            ll_ref_from = "the study's first laser-line plateau (median)"
        ref_pk: dict = {}
        if ll_ref_cal:
            for j, (near_, win_) in enumerate(pk_specs):
                for ln in corr.get("lines") or []:
                    if ln.get("reference_cm1") is not None and abs(float(ln["reference_cm1"]) - near_) <= win_ / 2:
                        ref_pk[j] = (float(ln["reference_cm1"]) - float(corr["a"])) / float(corr["b"] or 1.0)
                        break
        else:
            for j in range(len(pk_specs)):
                v_ = [raw_pk[pt][j] for pt, _ in plateaus[0] if raw_pk.get(pt) and raw_pk[pt][j] is not None]
                if v_:
                    ref_pk[j] = float(np.median(v_))
        fmin = float(prof["checks"].get("laser_follow_min_cm1", FOLLOW_MIN_CM1))
        for pl in plateaus:
            med = float(np.median([v for _, v in pl]))
            d_ = med - ll_ref
            moved = []
            for j, r0 in ref_pk.items():
                v_ = [raw_pk[pt][j] for pt, _ in pl if raw_pk.get(pt) and raw_pk[pt][j] is not None]
                if v_:
                    moved.append(float(np.median(v_)) - r0)
            dpk = float(np.median(moved)) if moved else None
            why = None
            landing = False
            if corr_broad:
                # a broad line (600 gr/mm): any step, a landing or a hop, only where a well fitted Raman peak moved
                # alike, within max(1 cm-1, 25 %) (Nicola, 2026-11-26)
                if abs(d_) <= land_thr:
                    why = (f"{d_:+.2f} cm-1 from the calibration's laser line, within the broad line's scatter (a "
                           f"step must exceed {land_thr:g}): not followed")
                elif dpk is None:
                    why = (f"{d_:+.2f} cm-1 with a broad laser line and no fitted Raman peak to confirm it: not "
                           "followed")
                elif abs(dpk - d_) > max(1.0, 0.25 * max(abs(d_), abs(dpk))):
                    why = (f"the Raman peaks moved {dpk:+.2f} cm-1, not {d_:+.2f}: the broad laser line moved alone "
                           "(not followed)")
                else:
                    landing = abs(d_) <= fmin
            elif anchor and abs(d_) <= fmin:
                # a landing step: beyond the laser line's own scatter, and the Raman peaks moved alike (the line on
                # the edge filter's slope moves twice as much when the laser drifts: 2026-11-24)
                if abs(d_) <= land_thr:
                    why = (f"{d_:+.2f} cm-1 from the calibration's laser line, within its own scatter (a step must "
                           f"exceed {land_thr:g}): not followed")
                elif land_thr > 0 and dpk is None:
                    why = (f"{d_:+.2f} cm-1 and no Raman peak to confirm it (process.peaks): a landing step is "
                           "followed only when the peaks moved with it")
                elif dpk is not None and abs(dpk - d_) > max(0.3, 0.25 * abs(d_)):
                    why = (f"the Raman peaks moved {dpk:+.2f} cm-1, not {d_:+.2f}: not the spectrometer's landing "
                           "(the laser line moved alone)")
                else:
                    landing = True
            elif abs(d_) <= fmin:
                why = (f"{d_:+.2f} cm-1 from the reference, not over {fmin:g}: not a hop (the laser line on the edge "
                       "filter moves more than the Raman lines)")
            elif dpk is not None and abs(dpk - d_) > (max(0.3, 0.25 * abs(d_)) if corr_broad
                                                     else max(0.5 * abs(d_), 1.0)):
                # with a broad laser line a hop needs the same close confirmation as a landing (2026-11-26, 600:
                # line +3.48, Si +1.99 was applied)
                why = f"the Raman peaks moved {dpk:+.2f} cm-1, not {d_:+.2f}: not a hop"
            elif dpk is None and corr_broad:
                why = (f"{d_:+.2f} cm-1 with a broad laser line and no fitted Raman peak to confirm it: not "
                       "followed")
            elif dpk is None and abs(d_) <= UNCONFIRMED_MIN_CM1:
                # a sample without sharp Raman peaks cannot confirm it (Nicola, 2026-11-24): only a large step
                why = (f"{d_:+.2f} cm-1 and no Raman peak to confirm it (process.peaks): only a step over "
                       f"{UNCONFIRMED_MIN_CM1:g} cm-1 is applied unconfirmed")
            for pt, _ in pl:
                plateau_step[pt] = 0.0 if why else d_
            conf_ = [pk_names[j] for j in ref_pk if any(raw_pk.get(pt) and raw_pk[pt][j] is not None for pt, _ in pl)]
            plateau_report.append({"points": [pt for pt, _ in pl], "laser_line_cm1": round(med, 3),
                                   **({"confirmed_by": conf_} if dpk is not None else {}),
                                   "drift_cm1": round(pl[-1][1] - pl[0][1], 3), "step_cm1": round(d_, 3),
                                   "raman_moved_cm1": None if dpk is None else round(dpk, 3),
                                   "applied": why is None, **({"not_applied_because": why} if why else {}),
                                   **({"landing": True} if landing else {}),
                                   **({"unconfirmed": True} if why is None and dpk is None else {})})
            if why is None and dpk is None and not landing:
                warnings.append(f"zero: a laser-line step of {d_:+.1f} cm-1 (points {pl[0][0]}-{pl[-1][0]}) was applied "
                                "without Raman peaks to confirm it (unconfirmed: a hop, or a bad fit of the laser "
                                "line)")
        for pt, idx_ in ll_bad_idx.items():
            if idx_ < len(plateaus) and plateaus[idx_]:
                plateau_step[pt] = plateau_step.get(plateaus[idx_][0][0], 0.0)
    # automatic peaks from the edge filter's usable cut-off on (measured, or HORIBA's QC), at least 50 cm-1
    return {
        "ll_mode": ll_mode, "ll_mode_asked": ll_mode_asked, "ll_ref": ll_ref,
        "ll_ref_from": ll_ref_from, "follow_each": follow_each, "excluded": excluded,
        "plateaus": plateaus, "plateau_of": plateau_of, "plateau_step": plateau_step,
        "ll_bad": ll_bad, "ll_bad_idx": ll_bad_idx, "hop_info": hop_info,
        "plateau_report": plateau_report, "anchor": anchor, "land_thr": land_thr,
        "corr_broad": corr_broad,
    }


def _analyse_points(study, per_point, prof, laser, rng, warnings, checks, grating, kind,
                    process, st, ll_st):
    """Stage 3 of analyse(): per-point saturation, shift/background/crop, spectra.analyze, and spike filtering."""
    from . import background as BG
    corr, bg, ext_range = st["corr"], st["bg"], st["ext_range"]
    fs, frac, min_snr = st["fs"], st["frac"], st["min_snr"]
    sat_points, spike_points, cover_short = st["sat_points"], st["spike_points"], st["cover_short"]
    axes, ys = st["axes"], st["ys"]
    ll_mode, ll_ref, ll_ref_from = ll_st["ll_mode"], ll_st["ll_ref"], ll_st["ll_ref_from"]
    follow_each, excluded = ll_st["follow_each"], ll_st["excluded"]
    plateau_of, plateau_step, ll_bad = ll_st["plateau_of"], ll_st["plateau_step"], ll_st["ll_bad"]
    auto_proc = process
    if not process.get("peaks") and laser:
        from . import perf
        cut_ = perf.cutoff_for(laser, grating)
        auto_proc = dict(process, auto_from_cm1=max(spectra.AUTO_FROM_CM1, float(cut_ or 0.0)))
    for rec in per_point:
        sp = rec.pop("_sp") if "_sp" in rec else spectra.read(rec["file"])
        cached_shift = rec.pop("_shift", None)
        has_cached_ll = "_ll" in rec
        cached_ll = rec.pop("_ll", None)
        try:
            shift = cached_shift if cached_shift is not None else spectra.to_shift(
                sp["x"], sp["meta"].get("x_unit", "nm"), laser)[0]
        except ValueError as exc:
            warnings.append(f"point {rec['point']}: {exc}")
            continue
        y = sp["y"]
        # a range acquired centred in one window: cut to the range (by the measured axis, the same for every point)
        crop = None
        if rng.get("crop_cm1"):
            lo_c, hi_c = sorted(float(v) for v in rng["crop_cm1"])
            crop = (np.asarray(shift) >= lo_c) & (np.asarray(shift) <= hi_c)
            if crop.sum() < 10:
                crop = None
        if sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s") and rec.get("exposure_s"):
            y_counts = y * rec["exposure_s"]
        else:
            y_counts = y
        sat = spectra.saturation(y_counts, fs, frac)
        ll = (dict(cached_ll) if cached_ll else cached_ll) if has_cached_ll else laser_line_fit(shift, y)
        proc_pt = auto_proc
        if kind == "calibration" and ll and ll.get("ok") and ll_ref is not None and process.get("peaks"):
            # the laser changed mode (T9.23 run 2: the line -14.0 against -4.6, Si at 510.3 outside a +-15 window):
            # the reference lines searched where the move puts them, and the change named
            mv_ = float(ll["position_cm1"]) - float(ll_ref)
            # always centred on where the move puts the line (T9.24-pre, 01:01: a -9.5 move, under the half window
            # of 15, left Si 4 cm-1 from the edge, FWHM 11.9: every fit clipped)
            if abs(mv_) > 0.5:
                proc_pt = dict(auto_proc, peaks=[dict(p_, near_cm1=float(p_["near_cm1"]) + mv_)
                                                 for p_ in process["peaks"]])
                rec["searched_shifted_cm1"] = round(mv_, 2)
        if ll:
            rec["laser_line"] = ll
            if ll_ref is None and ll.get("ok") and rec["point"] not in excluded and rec["point"] not in ll_bad:
                if rec["point"] in plateau_of:
                    ll_ref, ll_ref_from = plateau_of[rec["point"]], "the study's first laser-line plateau (median)"
                else:
                    ll_ref, ll_ref_from = ll["position_cm1"], f"the study's first laser line (point {rec['point']})"
            if rec["point"] in ll_bad:
                ll["suspect"] = ll_bad[rec["point"]]
            if ll_mode == "correct" and ll.get("ok") and ll_ref is not None:
                if follow_each and rec["point"] not in ll_bad:
                    d = ll["position_cm1"] - ll_ref
                elif follow_each:
                    d = plateau_of.get(rec["point"], ll["position_cm1"]) - ll_ref
                else:
                    d = plateau_step.get(rec["point"])
                if d is not None:
                    shift = shift - d
                    rec["laser_line_correction_cm1"] = round(-d, 3)
        if corr:
            shift = corr["a"] + corr["b"] * shift
            rec["x_correction"] = {"a": corr["a"], "b": corr["b"]}
        if bg:
            per_s = sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s")
            y, info = BG.subtract(sp["x"], y, sp["meta"].get("x_unit", "nm"),
                                  1.0 if per_s else (rec.get("exposure_s") or 1.0), bg, laser)
            rec["background"] = info
            if info["covered_fraction"] < 0.98:
                warnings.append(f"point {rec['point']}: the background covers {info['covered_fraction']:.0%} of the "
                                "spectrum")
        if crop is not None:
            shift, y = np.asarray(shift)[crop], np.asarray(y)[crop]
        a = spectra.analyze(shift, y, proc_pt, min_snr, prof["checks"].get("detect_snr", 5.0),
                            float(prof["detector"].get("e_per_count") or 1.0))
        _spikes_where_meaningful(a, laser, grating, ll)
        if ext_range and rec["point"] == per_point[0]["point"]:
            checks["stitch"] = {"windows": rng.get("windows"), "seams_cm1": stitch_seams(a["_x"])}
        if a.get("noise_correlation") is not None and a["noise_correlation"] > 0.5 and "noise" not in checks:
            checks["noise"] = {"correlation": a["noise_correlation"], "sigma": a.get("noise_sigma"),
                               "sigma_pixel": a.get("noise_sigma_pixel"), "denoise": rec.get("denoise"),
                               "note": "neighbouring points are correlated (LabSpec's DeNoise smooths them): the noise "
                                       "is the scatter around a smooth line, not point to point"}
        if ll and ll.get("ok"):
            # each peak also from this spectrum's own laser line (the Raman shift by definition; LabSpec's scale may
            # carry its own offset between 0 and the band: on the XploRA Si was 524.8 from the line, 520.2 on the scale)
            base = ll["position_cm1"] + (rec.get("laser_line_correction_cm1") or 0.0)
            if corr:
                base = corr["a"] + corr["b"] * base
            for pk in a["peaks"]:
                if pk.get("ok") and pk.get("position_cm1") is not None:
                    pk["from_laser_line_cm1"] = round(float(pk["position_cm1"]) - base, 3)
        rec.update(max_counts=sat["max_counts"], saturated=sat["saturated"], saturated_pixels=sat["pixels"],
                   noise_sigma=a["noise_sigma"], noise_sigma_pixel=a.get("noise_sigma_pixel"),
                   noise_correlation=a.get("noise_correlation"), spikes=len(a["spikes"]), spike_cm1=a.get("spike_cm1", [])[:20],
                   spike_info=a.get("spike_info", [])[:20],
                   from_cm1=a["from_cm1"],
                   to_cm1=a["to_cm1"], peaks=a["peaks"], bands=a["bands"], auto_peaks=a["auto_peaks"],
                   **({"auto_peaks_rejected": a.get("auto_peaks_rejected") or []} if a["auto_peaks"] else {}),
                   **({"spikes_in_requested_peaks": a["spikes_in_requested_peaks"]}
                      if a.get("spikes_in_requested_peaks") else {}))
        if sat["saturated"]:
            sat_points.append(rec["point"])
        if a["spikes"]:
            spike_points.append(rec["point"])
            xs_ = np.asarray(a["_x"], float)
            rec["_spike_tol"] = 2.0 * float(np.median(np.abs(np.diff(xs_)))) if xs_.size > 1 else 3.0
            rec["_npix"] = int(xs_.size)
        lo, hi = rng.get("from_cm1"), rng.get("to_cm1")
        if lo is not None and hi is not None and (not rng.get("single") or rng.get("crop_cm1")):
            tol_r = max(3.0, 0.005 * (hi - lo))      # a pixel or two short is not a gap (997.6 for 1000, 2026-11-23)
            if a["from_cm1"] > lo + tol_r or a["to_cm1"] < hi - tol_r:
                cover_short.append(rec["point"])
        axes.append(a["_x"])
        ys.append(a["_y"])
        if kind != "map":
            spectra.write(Path(rec["file"]).with_suffix(".shift.txt"), a["_x"], a["_y"],
                          {"x_unit": "1/cm", "y_unit": sp["meta"].get("y_unit", "cnt"), "laser_nm": laser,
                           "ics": rec.get("ics") or "unknown"})
    ll_st["ll_ref"], ll_st["ll_ref_from"] = ll_ref, ll_ref_from


def _analyse_summaries(name, study, per_point, prof, laser, rng, sd, warnings, unreliable,
                       checks, grating, request, objective, hole, kind, process, ref_lines, st, ll_st):
    """Stage 4 of analyse(): post-hop screening, peak/band/laser-line summaries, map/depth, and calibration."""
    from . import xcal
    wc, placement, ext_range, setup_ = st["wc"], st["placement"], st["ext_range"], st["setup_"]
    corr, fs, frac, min_snr, spike_mode = st["corr"], st["fs"], st["frac"], st["min_snr"], st["spike_mode"]
    sat_points, spike_points, cover_short = st["sat_points"], st["spike_points"], st["cover_short"]
    axes, ys = st["axes"], st["ys"]
    ll_mode, ll_mode_asked = ll_st["ll_mode"], ll_st["ll_mode_asked"]
    ll_ref, ll_ref_from = ll_st["ll_ref"], ll_st["ll_ref_from"]
    follow_each, excluded = ll_st["follow_each"], ll_st["excluded"]
    plateaus, ll_bad, ll_bad_idx = ll_st["plateaus"], ll_st["ll_bad"], ll_st["ll_bad_idx"]
    hop_info, plateau_report = ll_st["hop_info"], ll_st["plateau_report"]
    anchor, land_thr, corr_broad = ll_st["anchor"], ll_st["land_thr"], ll_st["corr_broad"]
    n = len(per_point)
    # After a laser mode hop the 637.75 nm laser was not single-mode (2026-11-24: Si 6.1 -> 13-26 cm-1 wide, counts 171 ->
    # 51-63, corrected Si 521.4-525.0): following the laser line cannot fix that. A spectrum after a hop whose Raman
    # line is over 1.5 x as wide as before the hop, or under half as strong, is kept out of the summaries.
    if len(plateaus) > 1:
        recs_ = {r_["point"]: r_ for r_ in per_point}
        first_pts = {pt for pt, _ in plateaus[0]} | {pt for pt, i_ in ll_bad_idx.items() if i_ == 0}
        after_pts = [pt for pl in plateaus[1:] for pt, _ in pl] + [pt for pt, i_ in ll_bad_idx.items() if i_ >= 1]
        ref_w: dict = {}
        ref_h: dict = {}
        for pt in first_pts:
            for pk in (recs_.get(pt) or {}).get("peaks") or []:
                if pk.get("ok") and pk.get("name"):
                    ref_w.setdefault(pk["name"], []).append(float(pk.get("fwhm_cm1") or 0))
                    ref_h.setdefault(pk["name"], []).append(float(pk.get("height") or 0))
        ref_w = {k_: float(np.median(v_)) for k_, v_ in ref_w.items() if v_}
        ref_h = {k_: float(np.median(v_)) for k_, v_ in ref_h.items() if v_}
        multimode = []
        for pt in sorted(after_pts):
            r_ = recs_.get(pt)
            if not r_ or r_.get("excluded"):
                continue
            why_ = []
            for pk in r_.get("peaks") or []:
                nm_ = pk.get("name")
                if nm_ in ref_w and not pk.get("ok"):
                    why_.append(f"{nm_} could not be fitted (it was before the hop)")
                    continue
                if not (pk.get("ok") and nm_ in ref_w):
                    continue
                if ref_w[nm_] and float(pk.get("fwhm_cm1") or 0) > 1.5 * ref_w[nm_]:
                    why_.append(f"{nm_} {float(pk['fwhm_cm1']):.1f} cm-1 wide ({float(pk['fwhm_cm1']) / ref_w[nm_]:.1f} x "
                                "before the hop)")
                if ref_h.get(nm_) and float(pk.get("height") or 0) < 0.5 * ref_h[nm_]:
                    why_.append(f"{nm_} at {float(pk.get('height') or 0) / ref_h[nm_]:.0%} of its height before the hop")
            if why_:
                r_["excluded"] = ("after a laser mode hop the laser is not in a clean single mode: " + "; ".join(why_)
                                  + " (a pause between spectra, the laser's settle_s, prevents the hop)")
                excluded[pt] = r_["excluded"]
                multimode.append(pt)
        if multimode:
            unreliable.append(f"{len(multimode)} spectra after a laser mode hop are not single-mode (points "
                              f"{', '.join(map(str, multimode[:10]))}): kept out of the summaries; settle_s or "
                              "acquire.shutter 'hold' avoids the hop")
    checks["saturation"] = {"pass": not sat_points, "points": sat_points[:50], "full_scale": fs, "threshold": fs * frac}
    if sat_points:
        unreliable.append(f"saturated in {len(sat_points)} of {n} spectra: choose a stronger filter or a shorter "
                          "exposure")
    # a spike that comes back at the same place more often than chance allows is a line (the Si or the laser line at
    # 600 gr/mm can be as narrow as a cosmic ray), not a cosmic ray, which almost never repeats (Nicola, 2026-11-24)
    recurring = recurring_spikes(per_point)
    if recurring:
        spike_points = [r["point"] for r in per_point if r.get("spikes")]
    for r in per_point:
        r.pop("_spike_tol", None)
        r.pop("_npix", None)
    checks["spikes"] = {"points": spike_points[:50], "count": len(spike_points),
                        **({"recurring_cm1": recurring} if recurring else {})}
    if spike_points and spike_mode == "none":
        # with LabSpec's spike removal on, what remains is usually a narrow Raman line, not a cosmic ray
        warnings.append(f"cosmic-ray spikes left in {len(spike_points)} spectra (spike removal is off: use 'double')")
    ics_states = sorted({str(r.get("ics")) for r in per_point if r.get("ics")})
    if ics_states:
        if len(ics_states) > 1:
            warnings.append("ICS was not the same for every spectrum (" + "; ".join(ics_states) + "): intensities with "
                            "and without ICS are not comparable")
    checks["range"] = {"pass": not cover_short, "points": cover_short[:50]}
    if cover_short:
        got_ = next((r for r in per_point if r["point"] in cover_short), {})
        warnings.append(f"{len(cover_short)} spectra do not cover the whole range asked for (point {got_.get('point')}: "
                        f"{got_.get('from_cm1')} to {got_.get('to_cm1')} cm-1 for {rng.get('from_cm1')} to "
                        f"{rng.get('to_cm1')})" + (
                            "; the zero correction moved the axis (the cut is made before it): ask a slightly wider "
                            "range_cm1" if rng.get("crop_cm1") else
                            f"; LabSpec placed the window itself (centre {wc:g} nm): ask a slightly wider range_cm1, "
                            "or center_cm1 for one window centred where ls6t puts it"
                            if wc is not None and rng.get("windows") == 1 else ""))
    weak = [r["point"] for r in per_point for pk in r.get("peaks", [])
            if pk.get("ok") and pk.get("snr") is not None and pk["snr"] < min_snr]
    failed = [r["point"] for r in per_point for pk in r.get("peaks", [])
              if not pk.get("ok") and pk.get("detected") is not False]
    undetected = [r["point"] for r in per_point for pk in r.get("peaks", []) if pk.get("detected") is False]
    checks["snr"] = {"min_snr": min_snr, "weak_points": sorted(set(weak))[:50],
                     "not_detected_points": sorted(set(undetected))[:50]}
    if failed and kind != "map":
        warnings.append(f"a peak fit failed in {len(set(failed))} spectra (see peaks[].why)")
    # a requested peak that could not be fitted makes the result unreliable (T9.19: reliable true with the only
    # requested peak failed): in most spectra (a spectrum, a series, a calibration), or in every one (a map)
    if process.get("peaks") and per_point and kind != "calibration":
        names_ = [str(p_.get("name") or f"peak{i_}") for i_, p_ in enumerate(process["peaks"])]
        for i_, nm_ in enumerate(names_):
            fails_ = [r_["point"] for r_ in per_point if r_["point"] not in excluded
                      and len(r_.get("peaks") or []) > i_ and not r_["peaks"][i_].get("ok")]
            n_ = len([r_ for r_ in per_point if r_["point"] not in excluded])
            if n_ and (len(fails_) == n_ if kind == "map" else len(fails_) * 2 > n_):
                why_ = next((r_["peaks"][i_].get("why") for r_ in per_point if r_["point"] in fails_
                             and r_["peaks"][i_].get("why")), None)
                cut_ = [c_ for r_ in per_point if r_["point"] in fails_ for c_ in (r_.get("spikes_in_requested_peaks") or [])]
                unreliable.append(f"the requested peak {nm_} was not fitted in {len(fails_)} of {n_} spectra"
                                  + (f" ({why_})" if why_ else "")
                                  + (f"; narrow features at {', '.join(f'{c_:g}' for c_ in cut_[:4])} cm-1 inside "
                                     "its window: at a coarse grating LabSpec's multi-accumulation spike filter can "
                                     "cut a line that moved between the accumulations; acquire.spike 'single' or "
                                     "one accumulation avoids it" if cut_ else ""))
    if undetected and kind != "map" and process.get("peaks"):
        warnings.append(f"a requested peak was not detected in {len(set(undetected))} spectra (see peaks[].why)")

    lls = [(r["point"], r["laser_line"]["position_cm1"]) for r in per_point
           if (r.get("laser_line") or {}).get("ok") and r["point"] not in excluded and r["point"] not in ll_bad]
    if ll_bad:
        checks["laser_line_bad_fits"] = {str(p_): w_ for p_, w_ in list(ll_bad.items())[:50]}
        warnings.append(f"the laser-line fit is not trusted at points {', '.join(map(str, list(ll_bad)[:10]))}: the "
                        "Raman peaks did not move with it (not a hop; those spectra keep their neighbours' zero)")
    if excluded:
        checks["excluded_points"] = {str(p_): w_ for p_, w_ in list(excluded.items())[:50]}
        warnings.append(f"{len(excluded)} spectra kept out of the summaries (points "
                        f"{', '.join(map(str, list(excluded)[:10]))}): "
                        + "; ".join(f"{p_}: {w_}" for p_, w_ in list(excluded.items())[:3]))
        if len(excluded) > max(1, len(per_point) // 10):
            unreliable.append(f"{len(excluded)} of {len(per_point)} spectra are kept out of the summaries "
                              "(checks.excluded_points)")
    followed = any(r.get("laser_line_correction_cm1") is not None for r in per_point)
    # after a hop the laser line sits deeper in the edge filter (clipped) and may be multimode: following it left
    # about 1 cm-1 (2026-11-24: Si 519.57 and 520.05 after hops of -8.0 and -8.9 cm-1, lines 1.5-2 x as wide). A
    # jump over 3 cm-1, or a laser line over 1.2 x its calibration's (or the study's narrowest) width: zero uncertain
    widths = [float(r["laser_line"]["fwhm_cm1"]) for r in per_point
              if (r.get("laser_line") or {}).get("ok") and r["laser_line"].get("fwhm_cm1") and r["point"] not in ll_bad
              and r["point"] not in excluded]
    ref_w = float(corr["laser_line_fwhm_cm1"]) if corr and corr.get("laser_line_fwhm_cm1") else \
        (min(widths) if widths else None)
    uncertain = []
    # in a calibration a broader laser line alone does not make the zero uncertain: at a small confocal hole the line
    # is clipped and its width varies (Nicola, 2026-11-27, hole 100: 'zero uncertain at 2 of 5', the laser line 1.3 x
    # the narrowest, while the Si repeats agreed to SD 0.11); there the point's Raman line must be off the others too
    pos_ = {r["point"]: float(pk_["position_cm1"]) for r in per_point
            for pk_ in [next((p for p in (r.get("peaks") or []) if p.get("ok") and p.get("position_cm1") is not None),
                             None)] if pk_ is not None and r["point"] not in excluded}
    if kind == "calibration" and len(pos_) >= 3:
        med_p = float(np.median(list(pos_.values())))
        sd_p = 1.4826 * float(np.median([abs(v - med_p) for v in pos_.values()]))
        raman_off = {k: abs(v - med_p) > max(0.3, 3.0 * sd_p) for k, v in pos_.items()}
    else:
        raman_off = None
    for r in per_point:
        why = []
        c_ = r.get("laser_line_correction_cm1")
        if c_ is not None and abs(float(c_)) > ZERO_JUMP_CM1:
            why.append(f"the laser line moved {-float(c_):+.1f} cm-1 (a hop)")
        w_ = (r.get("laser_line") or {}).get("fwhm_cm1")
        if ref_w and w_ and float(w_) > 1.2 * ref_w and r["point"] not in ll_bad:
            wtxt_ = (f"the laser line is {float(w_) / ref_w:.1f} x as wide as "
                     f"{'at the calibration' if corr and corr.get('laser_line_fwhm_cm1') else 'the narrowest here'}")
            if raman_off is not None and not raman_off.get(r["point"], True):
                r["laser_line_broad"] = wtxt_ + " (the Raman line agrees with the others: not counted)"
            else:
                why.append(wtxt_)
        if why:
            r["zero_uncertain"] = "; ".join(why)
            uncertain.append(r["point"])
    if corr and followed:
        how = ("the stored correction relative to the laser line: each spectrum brought back to the laser line of the "
               "correction's calibration, then the correction" if corr.get("laser_line_cm1") is not None else
               "the laser line within the study (to its first spectrum), then the stored correction as measured: the "
               "laser's move since that calibration is not covered (calibrate with the laser line in the window)")
    elif corr:
        how = "the stored correction only (true = a + b x measured)"
    elif followed:
        how = f"the laser line only (each spectrum brought back to {ll_ref_from})"
    else:
        how = "none"
    checks["zero_correction"] = {"method": how, "laser_reference": ll_mode_asked,
                                 **({"follows": "every spectrum's laser line (slow drift too)" if follow_each else
                                     f"landing steps (over {land_thr:g} cm-1 from the calibration's laser line, the "
                                     "Raman peaks moving alike) and hops" if anchor else
                                     f"a hop only: a plateau's step from the reference over "
                                     f"{float(prof['checks'].get('laser_follow_min_cm1', FOLLOW_MIN_CM1)):g} cm-1 "
                                     "that the Raman peaks follow too; otherwise the stored correction alone",
                                     "plateaus": plateau_report[:20] if not follow_each else None}
                                    if followed else {}),
                                 **({"stored": {k: corr.get(k) for k in ("a", "b", "time", "centre_nm",
                                                                        "laser_line_cm1") if corr.get(k) is not None}}
                                    if corr else {}),
                                 **({"laser_line_reference_cm1": round(ll_ref, 3), "laser_line_reference_from":
                                     ll_ref_from} if followed and ll_ref is not None else {})}
    if corr and followed and corr.get("laser_line_cm1") is None:
        warnings.append("zero: " + how)
    if uncertain:
        checks["zero_correction"]["uncertain_points"] = uncertain[:50]
        checks["zero_correction"]["method"] += "; zero uncertain at some points (see spectra[].zero_uncertain)"
        warnings.append(f"zero uncertain at {len(uncertain)} spectra (points {', '.join(map(str, uncertain[:10]))}): "
                        "the laser line jumped or broadened"
                        + ("; positions there may be off by about a cm-1 even after following it" if followed else
                           " (not followed): see checks.zero.correction_used.expected_uncertainty_cm1"))
        if len(uncertain) > max(1, len(per_point) // 10):
            unreliable.append(f"zero uncertain at {len(uncertain)} of {len(per_point)} spectra (the laser line jumped "
                              "or broadened: checks.zero_correction.uncertain_points)")
    miss = [r["point"] for r in per_point if not (r.get("laser_line") or {}).get("ok") and r["point"] not in excluded]
    if ll_mode == "correct" and miss:
        # (2026-11-24: 'no laser line was found in spectra :' with an empty list, when every spectrum after the
        # first had a bad laser-line fit: those have a line, it is not used)
        unreliable.append(f"acquire.laser_reference 'correct', but no laser line was found in spectra "
                          f"{', '.join(map(str, miss[:10]))}: the window must include about -15 to +15 cm-1 "
                          "(those spectra are not corrected)")
    if lls:
        ref0 = ll_ref if ll_ref is not None else lls[0][1]
        # jumps judged spectrum to spectrum, against the current plateau (a hop), not against the calibration's line
        # (2026-11-24: a map steady at -4.1..-4.5 was reported as "moved by +1.6 at points 4-8"): the drift since the
        # calibration is reported on its own
        jumps = [(h["point"], h["moved_cm1"]) for h in hop_info]
        checks["laser_line"] = {"reference_cm1": round(ref0, 3), "reference_from": ll_ref_from, "mode": ll_mode_asked,
                                "positions_cm1": {str(p): round(v, 3) for p, v in lls},
                                "jumps": hop_info[:50],
                                "points_after_a_jump": [pt for pl in plateaus[1:] for pt, _ in pl][:200]}
        cal_ref = (float(corr["laser_line_cm1"]) if corr and corr.get("laser_line_cm1") is not None
                   else None if (kind != "calibration" and checks.get("zero_setup"))
                   else laser_line_reference(laser, grating, setup_))
        if kind == "calibration":
            # the last STORED correction's laser line (T9.23: the median of a calibration that was not stored,
            # -5.153, had become the reference; the stored one was -3.939)
            st_ = (xcal.at_centre(laser, grating, wc, placement, setup=setup_)
                   or xcal.get(laser, grating, wc, placement, setup=setup_)
                   or xcal.get(laser, grating, wc, placement)) if laser else None
            if st_ and st_.get("laser_line_cm1") is not None:
                cal_ref = float(st_["laser_line_cm1"])
        if cal_ref is not None:
            dr = round(float(np.median([v for _, v in lls])) - cal_ref, 2)
            checks["laser_line"]["drift_since_calibration_cm1"] = dr
            if abs(dr) > ZERO_JUMP_CM1 and kind == "calibration":
                warnings.append(f"the laser line sits {dr:+.1f} cm-1 from where it was at the last calibration: the "
                                "laser is in another mode; this calibration measures the zero in the mode it is in now")
            elif abs(dr) > ZERO_JUMP_CM1 and not corr_broad:
                # (not with a broad line: at 600 it read +3.48 and +1.92 three seconds apart, no move: 2026-11-26)
                # 2026-11-25: a native map ran with the 637.75 laser hopped (line -8.5 cm-1 from the calibration's,
                # Si mean 512.69, 'zero uncertain' at 13 of 25) and said reliable: a different mode is not drift
                unreliable.append(f"the laser line sits {dr:+.1f} cm-1 from where it was at the calibration: the laser "
                                  "is in another mode (a hop or multimode), so the stored zero does not hold here and "
                                  "every Raman shift may be off by about that"
                                  + (" (followed by laser_reference, about 1 cm-1 may remain)" if ll_mode == "correct"
                                     else "") + "; let the laser settle (or switch it off and on) and calibrate "
                                  "again in the mode it measures in")
            elif abs(dr) > STALE_DRIFT_CM1 and kind == "calibration":
                # a calibration measures the zero as it is now: the line's move since the last one is only noted
                # (2026-11-26 15:43: a good calibration refused, "moved -1.2 ... more than twice +-0.00")
                checks["laser_line"]["since_last_calibration_note"] = (
                    "this calibration measures the zero as it is now; the move since the last one is the reason to "
                    "calibrate, not a fault")
            elif abs(dr) > STALE_DRIFT_CM1:
                # (2026-11-24: a map used a 5.5 h old correction with the line +0.78 away, no warning; Si 521.30)
                followed_ = ll_mode == "correct" and (follow_each or any(p_.get("applied") for p_ in plateau_report))
                # the expected uncertainty before this move is counted in (the rule below compares with it)
                eu_before = float(((checks.get("zero") or {}).get("correction_used") or {}).get(
                    "expected_uncertainty_cm1") or 0.0)
                if followed_:
                    # (2026-11-25: "the zero has likely moved too; calibrate again; followed" read as a contradiction)
                    landed_ = any(p_.get("landing") and p_.get("applied") for p_ in plateau_report)
                    warnings.append(f"the laser line sat {dr:+.1f} cm-1 from where it was at the calibration"
                                    + (" (a landing step, the Raman lines moved alike)" if landed_ else "")
                                    + ": followed (laser_reference): the spectra were brought back to the "
                                      "calibration's laser line")
                elif corr_broad:
                    # a broad line not followed only flags it (2026-11-26, 600): the larger of the Raman peaks'
                    # own move and half the line's
                    cu_ = (checks.get("zero") or {}).get("correction_used")
                    dpks_ = [abs(float(p_["raman_moved_cm1"])) for p_ in plateau_report
                             if not p_.get("applied") and p_.get("raman_moved_cm1") is not None]
                    dpk_ = max(dpks_) if dpks_ else None
                    _unfollowed_term(cu_, dr, dpk_)
                    why_pl = next((p_.get("not_applied_because") for p_ in plateau_report if not p_.get("applied")),
                                  None)
                    # the plateau's own reason only (T9.22: "clipped by the edge filter" next to "the Raman peaks
                    # moved +2.56, not +3.59")
                    warnings.append(f"the laser line reads {dr:+.1f} cm-1 from the calibration's: not followed"
                                    + (f" ({why_pl})" if why_pl else "") + "; for the best zero calibrate again at "
                                    "this centre (calibrate_si with the same grating and center_cm1)")
                else:
                    # not followed: the Raman lines may have moved by part of it (2026-11-25: laser line +1.7, Si
                    # +0.66 against +-0.25 expected): the expected uncertainty takes it in
                    cu_ = (checks.get("zero") or {}).get("correction_used")
                    # the Raman peaks' own move from where they read at the calibration, when known (T9.14: laser
                    # line +1.79 / Si +0.93 and +1.09 / +0.45, reported +-0.25): the larger of it and half the drift
                    dpks_ = [abs(float(p_["raman_moved_cm1"])) for p_ in plateau_report
                             if not p_.get("applied") and p_.get("raman_moved_cm1") is not None]
                    dpk_ = max(dpks_) if dpks_ else None
                    _unfollowed_term(cu_, dr, dpk_)
                    if dpk_ is not None and dpk_ >= 0.3:
                        warnings.append(f"since the calibration the laser line moved {dr:+.2f} cm-1 and the Raman peaks "
                                        f"{dpk_:.2f}, not alike: not followed; calibrate again")
                    warnings.append(f"the laser line sits {dr:+.1f} cm-1 from where it was at the calibration (drift "
                                    "since then, or a calibration made with another spectrometer window), not a "
                                    "hop: the zero has likely moved too; calibrate again"
                                    + ("; not followed: under the laser_reference step threshold, or the Raman "
                                       "lines did not move with it (checks.zero_correction.plateaus)"
                                       if ll_mode == "correct" else ""))
                eu_u = eu_before
                if not followed_ and abs(dr) > 2.0 * max(eu_u, 0.1):
                    # (T9.20, 600, shutter cycling: the laser hopped, line -5.1 / Si -6.4, reported reliable at +-2.58)
                    eu_now = float(((checks.get("zero") or {}).get("correction_used") or {}).get(
                        "expected_uncertainty_cm1") or eu_u)
                    unreliable.append(f"the laser line moved {dr:+.1f} cm-1 since the calibration and nothing was "
                                      f"followed: more than twice the expected uncertainty without that move "
                                      f"(+-{eu_u:.2f}; +-{eu_now:.2f} with it): positions may be off by about that; "
                                      "calibrate again")
        if jumps:
            after_ = checks["laser_line"]["points_after_a_jump"]
            smv = any(h.get("after_spectrometer_move") for h in hop_info)
            msg = (f"the laser line moved by {', '.join(f'{d:+.1f}' for _, d in jumps[:6])} cm-1 at point(s) "
                   f"{', '.join(str(p) for p, _ in jumps[:6])} ("
                   + ("a laser hop or the spectrometer repositioning: it was moved just before; " if smv else
                      "a laser mode hop: the spectrometer did not move; ")
                   + f"the Raman peaks moved with it), {len(after_)} spectra on from there: every Raman shift there "
                   "moves with it")
            if ll_mode == "correct":
                warnings.append(msg + "; corrected (acquire.laser_reference 'correct')")
            elif kind == "calibration" and len(per_point) > 1:
                # the repeats are judged by the calibration itself: relative to a narrow laser line (a landing
                # moves both), or by their step test (2026-11-25: a -1.9 landing made a 1200 calibration unreliable
                # while Si - laser line held to 0.06)
                warnings.append(msg + "; the calibration's repeats judge it (calibration.repeats)")
            else:
                unreliable.append(msg + "; use acquire.laser_reference 'correct' with the laser line in the window")
    shifted_ = [r_["searched_shifted_cm1"] for r_ in per_point if r_.get("searched_shifted_cm1") is not None]
    mode_change_ = None
    if shifted_:
        mv_med = float(np.median(shifted_))
        sd_mc = float(landing_threshold(prof, laser, grating, placement)["laser_line_sd_used_cm1"]) if laser else 0.2
        if abs(mv_med) > max(HEALTH_MOVE_MIN_CM1, 3.0 * sd_mc):
            mode_change_ = mv_med
            checks["laser_mode_change_cm1"] = round(mv_med, 2)
            warnings.append(f"laser mode change: the laser line moved {mv_med:+.1f} cm-1 since the stored calibration; "
                            "the reference line was searched where that puts it")
    # the laser running multimode: the calibration's reference line (Si) broader than at the calibration, with nothing
    # moved (T9.22 C, 600, shutter held: Si 7.56 -> 11.39 cm-1 wide and Gaussian, eta 0.69 -> 0.03, 2.5 cm-1 high)
    w_ref, w_from, _ = (si_width_reference(laser, grating, placement, objective, hole) if laser else (None, None, None))
    if not w_ref and w_from:
        checks["linewidth"] = {"skipped": w_from}
    if w_ref:
        refs_w = [(520.7, float(w_ref))]
        broad_pts, ratios, etas = [], [], []
        n_chk = 0
        for r_ in per_point:
            if r_["point"] in excluded:
                continue
            for pk_ in r_.get("peaks") or []:
                if not pk_.get("ok") or pk_.get("fwhm_cm1") is None:
                    continue
                ref_ = next((w_ for c_, w_ in refs_w if abs(float(pk_["position_cm1"]) - c_) <= 15.0), None)
                if ref_ is None:
                    continue
                n_chk += 1
                q_ = float(pk_["fwhm_cm1"]) / ref_
                ratios.append(q_)
                if pk_.get("eta") is not None:
                    etas.append(float(pk_["eta"]))
                if q_ > MULTIMODE_WIDTH_RATIO:
                    broad_pts.append(r_["point"])
                break
        if n_chk:
            checks["linewidth"] = {"vs_calibration": round(float(np.median(ratios)), 3),
                                   "calibration_fwhm_cm1": [w_ for _, w_ in refs_w][:3], "reference_from": w_from,
                                   "line": "Si",
                                   **({"eta_median": round(float(np.median(etas)), 3)} if etas else {})}
            if len(broad_pts) * 2 > n_chk:
                gauss_ = bool(etas) and float(np.median(etas)) < 0.15
                checks["linewidth"]["multimode"] = True
                unreliable.append(f"the reference line is {float(np.median(ratios)):.2f} x as wide as at the "
                                  "calibration" + (" and Gaussian in shape" if gauss_ else "")
                                  + (f", with the laser line moved {mode_change_:+.1f} cm-1" if mode_change_ else "")
                                  + ": the laser is probably running multimode, and its positions are shifted; wait "
                                    "a few minutes or power-cycle the laser, then calibrate again"
                                  + hole_fill_note(hole))
    cu_f = (checks.get("zero") or {}).get("correction_used")
    if isinstance(cu_f, dict) and (cu_f.get("expected_uncertainty_parts") or {}).get("first_landing") and \
            any(p_.get("applied") for p_ in plateau_report):
        # a step was followed: the learned first landing no longer applies (T9.22: kept after following, "no laser
        # line to bring it back")
        parts_ = dict(cu_f["expected_uncertainty_parts"])
        parts_.pop("first_landing", None)
        cu_f["expected_uncertainty_parts"] = parts_
        cu_f["expected_uncertainty_cm1"] = _u_total(parts_)
        import re as _re
        cu_f["expected_uncertainty_from"] = _re.sub(r"; the first landing after a grating change \([^)]*\)", "",
                                                    str(cu_f.get("expected_uncertainty_from") or ""))
    if isinstance(cu_f, dict) and cu_f.get("expected_uncertainty_cm1") is not None:
        tol_u = float(prof["checks"]["si_tol_cm1"])
        eu_ = float(cu_f["expected_uncertainty_cm1"])
        if eu_ > tol_u:
            # one statement with the final value (T9.18: three warnings quoted +-2.4, +-1.6 and "about a cm-1"
            # while the field said +-2.84)
            parts_s = ", ".join(f"{k_.replace('_', ' ')} {float(v_):.2f}"
                                for k_, v_ in (cu_f.get("expected_uncertainty_parts") or {}).items() if v_)
            warnings.append(f"the zero here is good to about +-{eu_:.2f} cm-1 (checks.zero.correction_used: "
                            f"{parts_s or cu_f.get('expected_uncertainty_from')}), more than the tolerance "
                            f"({tol_u:g}): calibrate shortly before, and measure at the same centre without moving "
                            "the spectrometer in between")
    if kind == "series" and laser:
        # the laser line's jitter with the spectrometer still (a series moves nothing): its own scatter, learned
        # for the landing threshold (Nicola, 2026-11-25); successive differences, so a creep does not count
        lls_s = [float(r_["laser_line"]["position_cm1"]) for r_ in per_point
                 if (r_.get("laser_line") or {}).get("ok") and r_["point"] not in excluded
                 and r_["point"] not in ll_bad]
        if len(lls_s) >= 5 and not hop_info:
            jit = float(np.sqrt(np.mean(np.diff(lls_s) ** 2) / 2.0))
            lrn_ = learn_laser_line_sd(laser, grating, placement, jit, "a series (the spectrometer still)")
            hist_ = (store.read("spectro_repeatability.json") or {}).get(
                f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else ""), {}).get(
                "laser_line_sd_cm1_history") or []
            checks["laser_line_jitter"] = {"sd_cm1": round(jit, 4), "spectra": len(lls_s), "learned_as": lrn_,
                                           "learned_from": f"the median of the last {len(hist_)} values learned "
                                                           f"({', '.join(f'{h:g}' for h in hist_)}): this series' "
                                                           "and earlier ones"}
        zc_ = zero_creep(per_point, excluded, laser, grating, placement)
        if zc_:
            checks["zero_creep"] = zc_
            if zc_.get("significant"):
                warnings.append(f"the {zc_['peak']} line crept {zc_['cm1_per_min']:+.2f} cm-1 per minute over the series "
                                f"({zc_['total_cm1']:+.2f} in {zc_['minutes']:.1f} min) with the laser line "
                                + (f"steady, but {zc_['not_the_spectrometer']}" if zc_.get("laser_line_steady")
                                   and zc_.get("width_changed") else
                                   "steady: the spectrometer's zero creeping, not the laser"
                                   if zc_.get("laser_line_steady") else
                                   f"moving {zc_['laser_line_cm1_per_min']:+.2f} per minute")
                                + (" (learned for the expected uncertainty of later spectra)"
                                   if zc_.get("learned") else ""))
    ss = same_spot_check(kind, per_point, warnings, unreliable) if kind in ("spectrum", "series") else None
    if ss:
        checks["same_spot"] = ss
    # with a range (From != To) LabSpec's Acq places the spectrometer itself (2026-11-24: -100 ... 700 cm-1 read 676.75
    # nm after Acq for data centred at about 650.6): that move is not a warning
    moved = [r["point"] for r in per_point if (r.get("optics_check") or {}).get("motor_busy")
             or ((r.get("optics_check") or {}).get("spectro_moved_nm", 0) > 0.005
                 and not (r.get("optics_check") or {}).get("spectro_moved_by_ls6t") and rng.get("single", True))]
    if moved:
        warnings.append(f"the spectrometer moved, or a motor was busy, around the acquisition of point(s) "
                        f"{', '.join(map(str, moved[:10]))} (optics_check)")
    out: dict = {"files": {"folder": str(sd)}}
    aes = [rec["auto_exposure"] for rec in per_point if rec.get("auto_exposure")]
    if aes:             # LabSpec's AutoExposure around the acquisitions (2026-11-23: a pass visible in the result)
        out["auto_exposure"] = dict(aes[0], spectra=len(aes),
                                    all_restored=all(a.get("restored") is not False for a in aes))
    if ics_states:      # the ICS state of the spectra: intensities with and without ICS are not comparable
        out["ics"] = ics_states[0] if len(ics_states) == 1 else ics_states
    if kind == "map":
        out["map"] = _map_result(name, study, per_point, axes, ys, sd, process)
        out["files"]["map"] = out["map"]["files"]
        if out["map"]["nx"] == 1 and out["map"]["ny"] == 1 and out["map"]["nz"] > 1:
            out["depth"] = depth_result(study, out["map"], prof, sd, warnings)
        if failed:
            warnings.append(f"a peak fit failed at {len(set(failed))} of {n} map points (null in the map)")
    elif kind == "series":
        out["series"] = _series_result(per_point, process)
        # start to start (2026-11-24: 2 s asked gave 1.66 s, 6 s gave 7.8 s): what was asked and what happened
        ts = [r.get("t_start") for r in per_point if r.get("t_start") is not None]
        asked = float((study.get("series") or {}).get("interval_s", 0) or 0)
        if len(ts) > 1:
            gaps = [round(b - a, 3) for a, b in zip(ts, ts[1:])]
            # the first gap carries one-off costs (the shutter hold, LabSpec's first display): the median, and the
            # gaps after the first, are what the series keeps (2026-11-24: 9.63 s, then 3.53, 3.55, 3.52)
            later = gaps[1:] or gaps
            med = sorted(later)[len(later) // 2] if len(later) % 2 else \
                (sorted(later)[len(later) // 2 - 1] + sorted(later)[len(later) // 2]) / 2
            sp_ = {"asked_s": asked, "median_s": round(med, 3), "mean_after_first_s": round(sum(later) / len(later), 3),
                   "first_s": gaps[0], "mean_s": round(sum(gaps) / len(gaps), 3), "min_s": min(gaps),
                   "max_s": max(gaps), "start_to_start_s": gaps}
            out["series"]["spacing"] = sp_
            sp_["mean_s_all"] = sp_.pop("mean_s")
            sp_["mean_s"] = sp_["median_s"]          # what the warning below judges
            if asked > 0 and abs(sp_["mean_s"] - asked) > max(0.2, 0.1 * asked):
                settled = [r.get("settled_s") for r in per_point if r.get("settled_s")]
                warnings.append(f"the spectra were {sp_['mean_s']:.2f} s apart (start to start, median) for the "
                                f"{asked:g} s asked" + (f": the laser's pause between acquisitions "
                                                        f"(settle_s, up to {max(settled):.1f} s) made them longer"
                                                        if settled and sp_["mean_s"] > asked else
                                                        ": each spectrum took longer than the interval"
                                                        if sp_["mean_s"] > asked else ""))
        out["spectra"] = [_point_summary(r) for r in per_point]
    else:
        out["spectra"] = [_point_summary(r) for r in per_point]
    if per_point and kind != "map":
        first = per_point[0]
        out["peaks"] = first.get("peaks", [])
        out["peaks_by_name"] = {pk["name"]: pk for pk in first.get("peaks", []) if pk.get("name")}
        out["bands"] = first.get("bands", {})
        if first.get("auto_peaks"):
            out["auto_peaks_rejected"] = first.get("auto_peaks_rejected") or []
    if kind == "calibration" and per_point:
        out["calibration"] = _calibration_result(study, prof, per_point[0], ref_lines, laser, grating, rng, request,
                                                 checks, unreliable, warnings, repeats=per_point, setup=setup_)
        if len(per_point) > 1:
            # the reference line(s) as the repeats' median, not repeat 0 (T9.23: snr 54.85 and 519.929 from repeat 0,
            # the offset from the median)
            med_pk = []
            for j_, pk0 in enumerate(per_point[0].get("peaks") or []):
                vals = [r_["peaks"][j_] for r_ in per_point if r_["point"] not in excluded
                        and len(r_.get("peaks") or []) > j_ and r_["peaks"][j_].get("ok")]
                if not vals:
                    med_pk.append(pk0)
                    continue
                m_ = dict(pk0)
                for f_ in ("position_cm1", "fwhm_cm1", "height", "snr", "eta", "r2", "area", "stderr_position_cm1"):
                    v_ = [float(v[f_]) for v in vals if v.get(f_) is not None]
                    if v_:
                        m_[f_] = round(float(np.median(v_)), 4)
                m_.update(ok=True, from_repeats=len(vals))
                med_pk.append(m_)
            out["peaks"] = med_pk
            out["peaks_by_name"] = {pk["name"]: pk for pk in med_pk if pk.get("name")}
            c_ = out["calibration"]
            nm0 = (med_pk[0] if med_pk else {})
            if nm0.get("snr") is not None:
                c_["snr"] = nm0["snr"]
            if nm0.get("fwhm_cm1") is not None:
                c_["fwhm_cm1"] = nm0["fwhm_cm1"]
    return out


def analyse(name, study, per_point, prof, laser, rng, sd: Path, warnings, unreliable, checks, grating=None,
            request=None, objective=None, hole=None) -> dict:
    """Full post-acquisition analysis: zero/background setup, laser-line tracking, per-point fits, and summaries."""
    from . import background as BG
    from . import xcal
    kind = study.get("kind", "spectrum")
    process = dict(study.get("process", {}))
    ref_lines = plan.calibration_lines(study, prof, rng) if kind == "calibration" else []
    if kind == "calibration" and not process.get("peaks"):
        win = calibration_window(study, grating, len(ref_lines))
        process["peaks"] = [{"name": f"ref{i}" if len(ref_lines) > 1 else "Si", "near_cm1": c, "window_cm1": win}
                            for i, c in enumerate(ref_lines)]
    elif process.get("peaks") and _f(grating) and _f(grating) < 1200:
        # a requested peak's default window scaled with the grating, as a calibration's (T9.20: 40 at 600, while
        # measure_spectrum had 60): 40 x 1200 / grating, at most 90
        dflt_ = min(90.0, 40.0 * 1200.0 / float(_f(grating)))
        process["peaks"] = [dict(p_, window_cm1=p_.get("window_cm1", dflt_)) if isinstance(p_, dict) else p_
                            for p_ in process["peaks"]]
    st = _analyse_zero_and_bg(study, per_point, prof, laser, rng, warnings, unreliable, checks,
                              grating, objective, hole, kind, process)
    ll_st = _analyse_laser_line_tracking(study, per_point, prof, laser, warnings, checks,
                                         grating, kind, process, st)
    _analyse_points(study, per_point, prof, laser, rng, warnings, checks, grating, kind,
                    process, st, ll_st)
    return _analyse_summaries(name, study, per_point, prof, laser, rng, sd, warnings, unreliable,
                              checks, grating, request, objective, hole, kind, process, ref_lines, st, ll_st)


def combo(laser, grating) -> str:
    """'the 2400 gr/mm grating with 637.75 nm': what a calibration (LabSpec's zero, ls6t's correction) belongs to."""
    g = _f(grating)
    return (f"the {g:g} gr/mm grating" if g else "this grating") + (f" with {float(laser):g} nm" if laser else "")


def zero_state(laser, grating, prof: dict, setup: dict | None = None) -> dict:
    """The last calibration check for this laser and grating, with this objective and hole (perf 'zero', under the
    correction's full key: Nicola, 2026-11-27, a hole-300 spectrum quoted the hole-100 check's -3.55), and the others
    of this laser: each grating has its own zero in LabSpec (2026-11-23, 637.75 nm: +0.3 cm-1 with 1200 after
    AutoCalibration, +3.1 with 2400)."""
    from . import perf, xcal
    k = xcal.key(laser, grating, setup) if setup and any(setup.values()) else perf.key(laser, grating)
    rec = perf.get("zero", k)
    out: dict = {"key": k, **({"setup": xcal.describe(k)} if setup and any(setup.values()) else {})}
    if rec:
        e = rec["latest"]
        out.update(offset_cm1=e.get("offset_cm1"), reference=e.get("reference"), time=e.get("time"),
                   checks=len(rec.get("history", [])))
    else:
        out["offset_cm1"] = None
    others = {kk: v["latest"].get("offset_cm1") for kk, v in ((perf.load().get("zero")) or {}).items()
              if kk != k and laser and kk.split("@")[0] == perf.key(laser) and kk.count("@") >= 3}
    if others:
        out["other_set_ups"] = others
    return out


HEALTH_WIDTH_RATIO = 1.3       # Si this much wider than at the calibration: multimode
HEALTH_COUNTS_RATIO = 0.5      # counts per second under this fraction of the last healthy check: low signal
HEALTH_MOVE_MIN_CM1 = 3.0      # the laser line this far from the calibration's (or 3 x its scatter): a mode change
LASER_LOG = "laser_log.jsonl"


SI_WIDTH = "si_width.json"


def _sw_key(laser, grating, objective, hole=None) -> str:
    h_ = _f(hole)
    return f"{float(laser):g}@{_f(grating) or 0:g}@{objective}" + (f"@h{h_:g}" if h_ is not None else "")


def note_si_width(laser, grating, objective, fwhm, eta=None, source: str = "", hole=None) -> None:
    """Si's FWHM with this laser, grating and objective, from a good state (a stored calibration, a healthy laser
    check): the reference for the multimode tests at that objective."""
    if objective in (None, "") or not fwhm:
        return
    try:
        d = store.read(SI_WIDTH) or {}
        d[_sw_key(laser, grating, objective, hole)] = {"fwhm_cm1": round(float(fwhm), 3),
                                                 **({"eta": round(float(eta), 3)} if eta is not None else {}),
                                                 "from": source, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
        store.write(SI_WIDTH, d)
    except (OSError, TypeError, ValueError):
        pass


def si_width_reference(laser, grating, placement: str = "centred", objective=None, hole=None) -> tuple:
    """Si's FWHM at the last good state of this laser and grating: the one reference for the multimode tests, in the
    laser check and in every study (01:38: the check said healthy at 1.05 x while the study, against an older
    correction, said 1.34 x). Per objective (Nicola, 2026-11-27: 785 Si 5.44 cm-1 at x100 against 8.55 at x10, with
    the 300 um hole, the x10's larger spot broadening the line): with the objective known, only a width measured
    with it (a stored calibration or a healthy laser check, from .27.7; before, the healthy checks in the laser log);
    none -> (None, why, correction) and the width test is skipped. Without the objective: the last calibration's
    width, else the resolution baseline. Returns (width, where from, the correction in use)."""
    from . import xcal, perf
    c_ = xcal.get(laser, grating, None, placement) or {}
    if objective not in (None, ""):
        # and per confocal hole (Nicola, 2026-11-27, 532 x100: Si 7.13 cm-1 at hole 100, 10.59 at 300, 10.81 at 500)
        h_ = _f(hole)
        with_ = f"{objective}" + (f", hole {h_:g} um" if h_ is not None else "")
        e_ = (store.read(SI_WIDTH) or {}).get(_sw_key(laser, grating, objective, hole))
        best = (e_["time"], float(e_["fwhm_cm1"]), f"{e_.get('from') or 'a good state'} with {with_} "
                f"({e_['time']})") if e_ and e_.get("fwhm_cm1") else None
        if best is None:
            # the laser log's healthy checks with this objective (kept before the store existed)
            try:
                for ln in reversed((config.home() / LASER_LOG).read_text(encoding="utf-8").splitlines()[-300:]):
                    g_ = json.loads(ln)
                    if g_.get("verdict") == "healthy" and g_.get("si_fwhm_cm1") and \
                            float(g_.get("laser_nm") or 0) == float(laser) and _f(g_.get("grating")) == _f(grating) \
                            and str(g_.get("objective")) == str(objective) and _f(g_.get("hole_um")) == h_ and \
                            (g_.get("si_eta") is None or float(g_["si_eta"]) >= GAUSS_ETA):
                        best = (g_.get("time"), float(g_["si_fwhm_cm1"]),
                                f"the healthy laser check with {with_} ({g_.get('time')})")
                        break
            except (OSError, ValueError, TypeError):
                pass
        if best:
            return best[1], best[2], c_
        return None, f"no Si width measured with {with_} yet for this laser and grating", c_
    w_ = next((float(l_["fwhm_cm1"]) for l_ in (c_.get("lines") or [])
               if l_.get("fwhm_cm1") and abs(float(l_.get("reference_cm1") or 0) - 520.7) < 20), None)
    if w_:
        return w_, f"the calibration of {c_.get('time')}", c_
    try:
        b_ = (perf.get("resolution", perf.key(laser, grating, None)) or {}).get("baseline") or {}
        if b_.get("fwhm_cm1"):
            return float(b_["fwhm_cm1"]), "the resolution baseline", c_
    except Exception:  # noqa: BLE001
        pass
    return None, None, c_


def hole_fill_note(hole) -> str:
    """A broad (even Gaussian) Si is not only multimode: at a large confocal hole its width follows how the spot fills
    the hole (Nicola, 2026-11-27, 532 x100: 7.13 cm-1 eta 0.25 at hole 100, 10.59 eta 0.00 at 300; 638 +1.15 at 300
    with the laser line unchanged)."""
    h_ = _f(hole)
    if h_ is not None and h_ <= 150:
        return ""
    return (f"; or the spot filling the confocal hole differently (hole {h_:g} um: focus, pointing): check at hole "
            "100 before taking it for the laser" if h_ is not None else
            "; or the spot filling the confocal hole differently (focus, pointing): check at hole 100 before taking "
            "it for the laser")


GAUSS_ETA = 0.15       # Si with a Lorentzian share under this: Gaussian, the multimode shape (never a width reference)


CHECK_REF = "laser_check_ref.json"


def _check_ref_key(laser, grating, objective, hole, centre_cm1) -> str:
    return (f"{float(laser):g}@{_f(grating) or 0:g}@{objective}@h{_f(hole) if _f(hole) is None else f'{_f(hole):g}'}"
            f"@c{float(centre_cm1):g}")


def laser_health(prof: dict, laser, grating, file, exposure_s=None, placement: str = "centred", objective=None,
                 filter_=None, hole=None, centre_cm1=None) -> dict:
    """One Si spectrum against the last good state (Nicola, 2026-11-27: the check done by hand three times in a night):
    the laser line against the stored correction's, the counts per second against the last healthy check, Si's FWHM
    against the calibration's (or the performance.resolution baseline). Verdict: healthy | mode_change | multimode |
    low_signal (the worst found first), with the reasons."""
    out: dict = {"laser_nm": laser, "grating": _f(grating), "objective": objective, "filter": filter_,
                 "hole_um": _f(hole), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        sp = spectra.read(file)
        sh, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
    except Exception as exc:  # noqa: BLE001
        return dict(out, verdict="unknown", reasons=[f"the spectrum could not be read: {exc}"])
    xs, ys = spectra.sort_xy(sh, sp["y"])
    ys = np.asarray(ys, float)
    ref_w, ref_from, _c_any = si_width_reference(laser, grating, placement, objective, hole)
    # the correction compared with: this set-up's, else the latest of any (then said: Nicola, 2026-11-27, -9.33
    # against a 00:07 correction with no objective or hole, at another window)
    from . import xcal as _xc
    setup_h = _xc.setup_of(objective, hole)
    corr = _xc.get(laser, grating, None, placement, setup=setup_h) or {}
    corr_key = _xc.key(laser, grating, setup_h) if corr else None
    if not corr and _c_any:
        corr = _c_any
        corr_key = _xc._resolve(_xc.load(), laser, grating, None)
    ll = laser_line_fit(xs, ys)
    move = None
    # the laser line's reference measured at this check's own window (Nicola, 2026-11-27: a check at 260 cm-1
    # compared with a calibration at 520.7, -2.51 against a 0.835 threshold: the pixel position differs between
    # windows): the first healthy check there (per objective and hole); else the correction, if from the same window
    own_k = _check_ref_key(laser, grating, objective, hole, centre_cm1) if centre_cm1 is not None else None
    own = (store.read(CHECK_REF) or {}).get(own_k) if own_k else None
    ll_ref_v, ll_ref_from, same_win = None, None, False
    if own and own.get("laser_line_cm1") is not None:
        ll_ref_v, ll_ref_from, same_win = (float(own["laser_line_cm1"]),
                                           f"the first healthy check at this window ({own.get('time')})", True)
    elif corr.get("laser_line_cm1") is not None:
        try:
            same_win = (centre_cm1 is not None and corr.get("centre_nm") is not None and
                        abs(P.shift_to_nm(float(centre_cm1), laser) - float(corr["centre_nm"])) <= 1.0)
        except (TypeError, ValueError):
            same_win = False
        ll_ref_v = float(corr["laser_line_cm1"])
        ll_ref_from = (f"the stored correction's calibration ({corr.get('time')})"
                       + ("" if same_win or centre_cm1 is None else
                          ", at another window: pixel positions differ, only a hop counts"))
    if ll and ll.get("ok"):
        out["laser_line_cm1"] = round(float(ll["position_cm1"]), 3)
        if ll_ref_v is not None:
            move = float(ll["position_cm1"]) - ll_ref_v
            out["laser_line_move_cm1"] = round(move, 3)
            out["laser_line_reference_from"] = ll_ref_from
    g = _f(grating) or 1200.0
    win = min(90.0, 40.0 * 1200.0 / g)
    near = 520.7 + (move if move is not None and abs(move) > win / 2 else 0.0)
    try:
        # fitted as every study fits it (the same SNR: 01:38 gave 517 here and 67 in the study)
        si = (spectra.analyze(xs, ys, {"peaks": [{"name": "Si", "near_cm1": near, "window_cm1": win}]},
                              float(prof["checks"].get("min_snr", 10.0)),
                              float(prof["checks"].get("detect_snr", 5.0)))["peaks"] or [{}])[0]
    except Exception:  # noqa: BLE001
        si = {}
    exp_ = float(exposure_s or sp["meta"].get("acq.Exposure") or 1.0) if exposure_s or sp["meta"].get(
        "acq.Exposure") else None
    if si.get("ok"):
        out.update(si_cm1=round(float(si["position_cm1"]), 3), si_fwhm_cm1=round(float(si["fwhm_cm1"]), 3),
                   si_snr=si.get("snr"), si_eta=si.get("eta"), si_height=si.get("height"))
        if own and own.get("si_cm1") is not None:
            out["si_move_cm1"] = round(float(si["position_cm1"]) - float(own["si_cm1"]), 3)  # the same window
    out["max_counts"] = round(float(np.max(ys)), 1) if ys.size else None
    if exp_ and si.get("ok") and si.get("height") is not None:
        out["si_counts_per_s"] = round(float(si["height"]) / exp_, 2)
    reasons, verdicts = [], []
    # the last healthy check of this laser and grating: its Si counts per second
    last_ok = None
    try:
        for ln in reversed((config.home() / LASER_LOG).read_text(encoding="utf-8").splitlines()[-200:]):
            e = json.loads(ln)
            # the same objective and filter too (2026-11-27: the 01:38 baseline was taken through x10)
            if e.get("verdict") == "healthy" and float(e.get("laser_nm") or 0) == float(laser) and \
                    _f(e.get("grating")) == _f(grating) and e.get("si_counts_per_s") and \
                    str(e.get("objective")) == str(objective) and str(e.get("filter")) == str(filter_):
                last_ok = e
                break
    except (OSError, ValueError):
        pass
    if not si.get("ok"):
        verdicts.append("low_signal")
        reasons.append(f"Si not found near {near:.1f} cm-1 ({si.get('why') or 'no fit'})")
    else:
        if ref_w:
            out["width_ratio"] = round(float(si["fwhm_cm1"]) / ref_w, 3)
        else:
            out["width_check"] = f"skipped: {ref_from or 'no Si width reference'}"
        if ref_w and float(si["fwhm_cm1"]) > HEALTH_WIDTH_RATIO * ref_w:
            verdicts.append("multimode")
            reasons.append(f"Si is {float(si['fwhm_cm1']) / ref_w:.2f} x as wide as at {ref_from} "
                           f"({float(si['fwhm_cm1']):.2f} vs {ref_w:.2f} cm-1)"
                           + (", Gaussian in shape" if si.get("eta") is not None and float(si["eta"]) < 0.15 else "")
                           + hole_fill_note(hole))
        if last_ok and out.get("si_counts_per_s") is not None:
            q_ = float(out["si_counts_per_s"]) / float(last_ok["si_counts_per_s"])
            out["counts_ratio"] = round(q_, 3)
            if q_ < HEALTH_COUNTS_RATIO:
                verdicts.append("low_signal")
                reasons.append(f"Si gives {q_:.2f} x the counts of the last healthy check ({last_ok.get('time')})")
            if q_ < 0.4 or q_ > 2.5:
                # far from the last healthy check at the same objective and filter
                out["objective_suspect"] = True
                reasons.append(f"the objective may not be the one LabSpec reports ({objective}): Si gives {q_:.2f} x "
                               "the counts of the last healthy check with it and this filter; check the turret")
    if ll and ll.get("ok") and ll.get("fwhm_cm1") and corr.get("laser_line_fwhm_cm1"):
        # the laser line's own width, said separately (it is clipped by the edge filter: a clue, not the verdict)
        out["laser_line_width_ratio"] = round(float(ll["fwhm_cm1"]) / float(corr["laser_line_fwhm_cm1"]), 3)
    if move is not None:
        lt_ = landing_threshold(prof, laser, grating, placement) if laser else {"laser_line_sd_used_cm1": 0.2,
                                                                                  "threshold_cm1": 0.5}
        sd_ = float(lt_["laser_line_sd_used_cm1"])
        # two limits for two uses (01:38: -0.655 'healthy' here, a mode change for drift pairs): a mode change
        # (a hop) for measuring over max(3, 3 x scatter); steady enough to pair calibrations for drift within the
        # landing threshold
        # steady enough for drift pairs: judged only against the same window
        out["steady_for_drift_pairs"] = (abs(move) <= float(lt_["threshold_cm1"])) if same_win else None
        if abs(move) > max(HEALTH_MOVE_MIN_CM1, 3.0 * sd_):
            verdicts.append("mode_change")
            reasons.append(f"the laser line sits {move:+.2f} cm-1 from {ll_ref_from}")
    order = ("multimode", "low_signal", "mode_change")
    out["verdict"] = next((v for v in order if v in verdicts), "healthy")
    out["reasons"] = reasons
    out["same_window_reference"] = bool(same_win) if ll_ref_v is not None else None
    chk_nm_ = None
    try:
        chk_nm_ = round(P.shift_to_nm(float(centre_cm1), laser), 2) if centre_cm1 is not None else None
    except (TypeError, ValueError):
        pass
    out["compared_with"] = {"correction": corr.get("time"), "laser_line": ll_ref_from,
                            "laser_line_reference": {
                                "kind": "first healthy check at this window" if own else
                                ("stored correction" if corr.get("laser_line_cm1") is not None else None),
                                "key": own_k if own else corr_key, "window_centre_nm": (
                                    chk_nm_ if own else corr.get("centre_nm")),
                                "check_window_centre_nm": chk_nm_, "same_window": bool(same_win),
                                **({"legacy_no_objective_or_hole": True}
                                   if not own and corr_key and corr_key.count("@") == 1 else {})},
                            "si_fwhm_reference_cm1": ref_w,
                            "si_fwhm_reference_from": ref_from, "last_healthy": (last_ok or {}).get("time")}
    out["counts_baseline"] = (out["verdict"] == "healthy" and out.get("si_counts_per_s") is not None)
    if out["verdict"] == "healthy" and si.get("ok") and not ref_w and objective not in (None, "") and \
            (si.get("eta") is None or float(si["eta"]) >= GAUSS_ETA):
        # the first healthy check at this objective and hole sets the width reference; later checks never replace it
        # (Nicola, 2026-11-27: set again by a check that was not the first), calibrations do (a Gaussian Si, the
        # multimode shape, never becomes one)
        note_si_width(laser, grating, objective, si["fwhm_cm1"], si.get("eta"), "the first healthy laser check", hole)
        out["si_width_reference_set"] = True
    if not out.get("si_width_reference_set"):
        out["si_width_reference_set"] = False                # always said (Nicola, 2026-11-27)
        if not ref_w:
            out["si_width_reference_not_set_because"] = (
                f"the laser is not healthy ({out['verdict']})" if out["verdict"] != "healthy" else
                "Si is Gaussian in shape (the multimode shape)" if si.get("eta") is not None
                and float(si["eta"]) < GAUSS_ETA else "the objective is not known" if objective in (None, "")
                else "Si was not fitted")
    if out["verdict"] == "healthy" and own_k and not own and ll and ll.get("ok") and objective not in (None, ""):
        try:
            d_ = store.read(CHECK_REF) or {}
            d_[own_k] = {"laser_line_cm1": round(float(ll["position_cm1"]), 3),
                         **({"si_cm1": round(float(si["position_cm1"]), 3)} if si.get("ok") else {}),
                         "time": out["time"]}
            store.write(CHECK_REF, d_)
            out["window_reference_set"] = True
        except (OSError, TypeError, ValueError):
            pass
    out.setdefault("window_reference_set", False)
    try:
        p_ = config.home() / LASER_LOG
        p_.parent.mkdir(parents=True, exist_ok=True)
        with open(p_, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, default=str) + "\n")
    except OSError:
        pass
    return out



def _calibration_result(study, prof, rec, ref_lines, laser, grating, rng, request, checks, unreliable,
                        warnings, repeats: list | None = None, setup: dict | None = None) -> dict:
    """The reference lines measured against their true positions: the offset (the zero) and, with lines far enough
    apart, the scale (the dispersion coefficient): true = a + b x measured. adjust 'ls6t' remembers it and applies
    it to later spectra (same laser and grating)."""
    from . import xcal
    cal = study.get("calibration") or {}
    refname = cal.get("reference", "si")
    wc, wc_from = window_centre_used(rng, [rec])
    tol = float(cal.get("tol_cm1", prof["checks"]["si_tol_cm1"]))
    lines = []
    # as measured: a laser-line shift this calibration study applied (laser_reference correct / correct_each) is
    # undone, so the stored zero means the same whatever the study's mode (Nicola, 2026-11-25)
    llc0 = float(rec.get("laser_line_correction_cm1") or 0.0)
    for ref, pk in zip(ref_lines, rec.get("peaks") or []):
        pos_ = (float(pk["position_cm1"]) - llc0) if pk.get("ok") and pk.get("position_cm1") is not None else None
        lines.append({"reference_cm1": ref, "measured_cm1": pos_,
                      "error_cm1": round(pos_ - ref, 4) if pos_ is not None else None,
                      "fwhm_cm1": pk.get("fwhm_cm1"), "snr": pk.get("snr"), "why": pk.get("why"),
                      "warning": pk.get("warning")})
    ok = [l for l in lines if l["measured_cm1"] is not None]
    res: dict = {"reference": refname, "lines": lines, "tol_cm1": tol, "adjust": cal.get("adjust", "none"),
                 "applies_to": {"laser_nm": laser, "grating": _f(grating), "key": xcal.key(laser, grating),
                                "window_centre_nm": wc, "window_centre_from": wc_from,
                                "what": combo(laser, grating) + " only: LabSpec keeps a zero per grating"}}
    if not ok:
        res.update(pass_=False, why="no reference line found: check the sample and its focus")
        res["pass"] = res.pop("pass_")
        checks["calibration"] = {"pass": False}
        unreliable.append(f"no {refname} line found: check the sample and its focus")
        return res
    meas = np.array([l["measured_cm1"] for l in ok])
    refs = np.array([l["reference_cm1"] for l in ok])
    if len(ok) >= 2 and refs.max() - refs.min() >= 300:
        b, a = np.polyfit(meas, refs, 1)
        res["fit"] = "offset and scale (zero and dispersion)"
    else:
        a, b = float(np.mean(refs - meas)), 1.0
        res["fit"] = "offset only (one line, or lines too close together for the scale)"
    worst = float(np.max(np.abs(meas - refs)))
    resid = refs - (a + b * meas)
    first = ok[0]
    rep_info = None
    if repeats and len(repeats) > 1:
        # several spectra, the spectrometer re-placed before each (2026-11-24, 600 gr/mm: the same window gave
        # +9.22 / +9.47 / +9.86 cm-1, a scatter larger than the fit's own error): the correction is their mean
        from . import runner as _R
        offs = _R._repeat_offsets(repeats, ref_lines)
        stt_abs = repeat_stats(offs)
        stt = stt_abs
        amode = str(cal.get("anchor", "auto"))
        lab_placed_ = not rng.get("single", True)
        lst = None
        if amode == "always":
            anc = _R._anchor_repeats(repeats, offs, lines, cal, b)
        else:
            anc = {"why_not": "calibration.anchor is off"} if amode == "off" else None
            if amode != "off":
                # landing STEPS only (Nicola, 2026-11-25: anchoring every repeat added the laser line's ~0.2 fit noise
                # when there was no landing jump): a repeat whose laser line sits off the others' by more than
                # LANDING_K x its own scatter, with the Raman lines moved alike, is brought back by that step
                ws_ = [float((r_.get("laser_line") or {}).get("fwhm_cm1")) for r_ in repeats
                       if (r_.get("laser_line") or {}).get("ok") and (r_.get("laser_line") or {}).get("fwhm_cm1")]
                broad_r = laser_line_too_broad(float(np.median(ws_)), lines) if ws_ else None
                lst = _landing_steps(repeats, offs, prof, laser, grating, "labspec" if lab_placed_ else "centred",
                                     min_thr=BROAD_LANDING_MIN_CM1 if broad_r else 0.0)
                if lst.get("offs_eff") is not None:
                    stt = repeat_stats(lst["offs_eff"])
        anc_why_not = anc.get("why_not") if anc else None
        if anc_why_not:
            anc = None
        if anc:
            # 2026-11-25, 637.75 @ 1200: Si 521.41, 520.10, 519.62, 519.82, 519.58 (SD 0.76) but Si - laser line
            # 524.43, 524.44, 524.36, 524.51, 524.42 (SD 0.06): the scatter is the spectrometer's landing, common to
            # both lines. Judged and stored relative to the laser line (later spectra are brought to its position)
            stt = anc["stats"]
        use = stt["kept"]
        if len(use) >= 2:
            centre_o = float(stt["centre"])
            sd = float(np.std(use, ddof=1))
            if anc:
                ll_ref_c = anc["laser_line_cm1"]
                a = -(centre_o + ll_ref_c)                    # true = a + (measured - (LL - LL_ref)), b = 1
                centre_o = centre_o + ll_ref_c                # the offset with the laser line where it was
            else:
                a = a + (float(np.mean(meas - refs)) - centre_o) * b      # the zero moved to the repeats' median
            worst = (abs(float(stt_abs["centre"])) if anc and stt_abs.get("centre") is not None else abs(centre_o))
            moved = bool((study.get("calibration") or {}).get("repeats_move", True))
            rep_info = {"n": len(use), "offsets_cm1": [None if o is None else round(o, 3) for o in offs],
                        "median_cm1": round(centre_o, 3),
                        "mean_cm1": round(float(np.mean(use)) + (anc["laser_line_cm1"] if anc else 0.0), 3),
                        "sd_cm1": round(sd, 3), "sem_cm1": round(sd / math.sqrt(len(use)), 3),
                        "spectrometer_moved": moved, "outliers": stt["outliers"], "settled": stt["settled"]}
            if anc:
                rep_info["anchored_to_laser_line"] = {
                    "laser_line_cm1": round(anc["laser_line_cm1"], 3),
                    "relative_cm1": [None if v is None else round(v, 3) for v in anc["relative"]],
                    "laser_lines_cm1": [None if v is None else round(v, 3) for v in anc["laser_lines"]],
                    "landing_sd_cm1": round(float(np.std([o for o in offs if o is not None], ddof=1)), 3)
                    if sum(o is not None for o in offs) >= 2 else None,
                    "why": "the laser line is narrow and in the window: the zero is judged relative to it, which "
                           "removes the spectrometer's landing scatter (later spectra with the laser line in the "
                           "window are brought to its position: acquire.laser_reference 'anchor')"}
                rep_info["absolute_outliers"] = stt_abs["outliers"]
            elif lst is not None:
                rep_info["landing_steps"] = {k_: v_ for k_, v_ in lst.items() if k_ not in ("offs_eff", "ll_sd")}
                if lst.get("creep"):
                    # the laser line and the Raman lines crept together past the threshold: not settled, as a step
                    # of that size would be (Nicola, 2026-11-27)
                    rep_info.update(settled=False, drifting=True, laser_creep=lst["creep"])
                    warnings.append(f"the laser line crept {lst['creep']['laser_line_cm1']:+.2f} cm-1 over the "
                                    f"repeats and the Raman lines {lst['creep']['raman_cm1']:+.2f} with it (over the "
                                    f"{lst['creep']['threshold_cm1']:g} cm-1 landing threshold): the laser was not "
                                    "settled")
                if lst.get("ll_sd") is not None and lst.get("n_quiet", 0) >= 3 and \
                        "this calibration's repeats" not in str((lst.get("threshold") or {}).get("from")):
                    # learned per laser@grating, but never from repeats that set their own threshold (a step left
                    # in would raise it: 2026-11-25, 0.353 at 1200)
                    learn_laser_line_sd(laser, grating, "labspec" if lab_placed_ else "centred", lst["ll_sd"],
                                        "calibration repeats without steps")
                if "this calibration's repeats" in str((lst.get("threshold") or {}).get("from")):
                    warnings.append("the landing threshold came from this calibration's own repeats (nothing better "
                                    "known for this laser and grating: set spectrometer.repeatability."
                                    f"{float(laser):g}@{_f(grating) or 0:g}.laser_line_sd_cm1, or run a held series "
                                    "with the laser line in the window to learn it)")
                if any(s_.get("followed") for s_ in lst.get("steps") or []):
                    rep_info["offsets_corrected_cm1"] = [None if o is None else round(o, 3)
                                                         for o in lst["offs_eff"]]
                if lst.get("steps"):
                    warnings.append("landing step(s) at repeat(s) " + ", ".join(
                        f"{s_['repeat']} ({s_['laser_line_step_cm1']:+.2f} cm-1)" for s_ in lst["steps"]
                        if s_.get("followed")) + ": the laser line and the Raman lines moved together (the "
                        "spectrometer landing elsewhere); those repeats were brought back by the step"
                        if any(s_.get("followed") for s_ in lst["steps"]) else
                        "a laser-line step the Raman lines did not follow (not a landing): left as measured")
            else:
                rep_info["not_anchored_because"] = anc_why_not or "no laser line"
                res["anchored"] = True
            # spread from successive differences (sqrt(mean d^2 / 2)): a slow creep does not count as scatter
            # (2026-11-25, ext600: 9.26, 8.75, 7.85, 7.51, 8.17 passed the creep test; SD 0.70 overstated the
            # repositioning spread)
            seq_ = list(stt_abs.get("kept") or [])          # in order, outliers left out
            sd_succ = (float(np.sqrt(np.mean(np.diff(seq_) ** 2) / 2.0)) if len(seq_) >= 3 else None)
            if sd_succ is not None:
                rep_info["sd_successive_cm1"] = round(sd_succ, 3)
            if stt_abs["settled"] and not rep_info.get("laser_creep") and len(stt_abs["kept"]) >= 3:
                sd = float(np.std(stt_abs["kept"], ddof=1)) if anc else sd       # the landing scatter itself
                if sd_succ is not None and sd_succ < sd:
                    sd = sd_succ
                rec_r = store.read("spectro_repeatability.json") or {}
                lab_placed = not rng.get("single", True)        # LabSpec placed the window: no approach applies
                kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if lab_placed else "")
                rec_r.setdefault(kk, {})["moved_sd_cm1" if (moved or lab_placed) else "still_sd_cm1"] = round(sd, 3)
                if moved and not lab_placed:
                    rec_r[kk]["approach_nm"] = float((prof.get("spectrometer") or {}).get("approach_nm", 0) or 0)
                rec_r[kk]["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                store.write("spectro_repeatability.json", {k_: _clean_rep_entry(v_) for k_, v_ in rec_r.items()})
                res["repeatability"] = _clean_rep_entry(dict(rec_r[kk]))
            if stt["outliers"]:
                warnings.append(f"repeat(s) {', '.join(str(i + 1) for i in stt['outliers'])} left out as outliers "
                                "(more than 3 robust SDs from the median; the first after a grating change was one "
                                "twice on 2026-11-24)")
            if stt["trend"] and not stt["step"]:
                rep_info.update(drifting=True, trend=stt["trend"])
                warnings.append(f"the repeats creep by {stt['trend']['slope_cm1_per_repeat']:+.2f} cm-1 per spectrum "
                                "(the laser still settling?): their scatter is not noise")
            if stt["step"]:
                rep_info.update(drifting=True, step=stt["step"])
                warnings.append(
                    f"the repeats jump by {stt['step']['size_cm1']:+.2f} cm-1 after repeat {stt['step']['after_repeat']}"
                    + (" (the spectrometer landing elsewhere after its move, or a laser hop)" if moved else
                       " (a laser hop: the spectrometer did not move)")
                    + ": their scatter is not noise")
    res.update(reference_cm1=first["reference_cm1"], measured_cm1=first["measured_cm1"],
               offset_cm1=round(float(np.mean(meas - refs)), 4), fwhm_cm1=first["fwhm_cm1"], snr=first["snr"],
               worst_error_cm1=round(worst, 4), correction={"a": round(float(a), 5), "b": round(float(b), 7)},
               residual_cm1=round(float(np.max(np.abs(resid))), 4), lines_used=len(ok), **{"pass": worst <= tol})
    res["zero_shift_cm1"] = round(float(a + (b - 1.0) * float(np.mean(refs))), 4)   # the shift at the lines' centre
    px = _cm1_per_px(rec, laser, float(first["reference_cm1"]))
    if px:
        res["cm1_per_pixel"] = px
    if rep_info:
        res["repeats"] = rep_info
        # the median throughout (15:43: offset_cm1 the mean -0.879, the correction from the median +0.790, the
        # lines from repeat 0 only): the offset, the measured position and the line are the repeats' median
        med_ = float(rep_info["median_cm1"])
        res["offset_cm1"] = round(med_, 4)
        res["offset_from"] = f"the median of {rep_info['n']} repeats (repeats.offsets_cm1 lists each)"
        if len(lines) == 1 and lines[0].get("measured_cm1") is not None:
            ref_ = float(lines[0]["reference_cm1"])
            lines[0] = dict(lines[0], measured_cm1=round(ref_ + med_, 4), error_cm1=round(med_, 4),
                            measured_from=f"the median of {rep_info['n']} repeats")
            res["measured_cm1"] = round(ref_ + med_, 4)
            res["lines"] = lines
        if rep_info["sd_cm1"] > tol / 2:
            warnings.append(f"the repeats scatter by {rep_info['sd_cm1']:.2f} cm-1 (SD of {rep_info['n']}): more than "
                            f"half the tolerance; the median ({rep_info['median_cm1']:+.2f}, SEM {rep_info['sem_cm1']:.2f}) is "
                            "stored")
    elif px and px >= COARSE_CM1_PER_PX:
        warnings.append(f"{px:.1f} cm-1 per pixel: the line spans a couple of pixels and one fit repeats to about "
                        "±0.5 cm-1 (more than its own error says): calibration.repeats 5 gives the mean and its scatter")
    # good enough to correct the zero by? 2026-11-23: at 1 %, 1 s the Si line was weak and its fit sat at the edge of
    # the window (525.12 cm-1); ls6t still asked for a -4.42 cm-1 correction; AutoCalibration then found 520.64
    min_snr = float(prof["checks"].get("min_snr", 10.0))
    if repeats and len(repeats) > 1:
        # judged on the repeats, not repeat 0 (T9.23 run 2: the reason, FWHM 15.33 and SNR 31.42 came from the
        # dropped outlier): each line's median SNR and FWHM, and "at the edge" only if most repeats say so
        for i_, l_ in enumerate(lines):
            pks_ = [r_["peaks"][i_] for r_ in repeats if len(r_.get("peaks") or []) > i_ and r_["peaks"][i_].get("ok")]
            if not pks_:
                continue
            for f_ in ("snr", "fwhm_cm1"):
                v_ = [float(p_[f_]) for p_ in pks_ if p_.get(f_) is not None]
                if v_:
                    l_[f_] = round(float(np.median(v_)), 4)
            edge_n = sum("edge" in str(p_.get("warning") or "") for p_ in pks_)
            l_["warning"] = ("the fitted peak sits at the edge of its window in most repeats" if edge_n * 2 > len(pks_)
                             else None)
        ok = [l for l in lines if l.get("measured_cm1") is not None]
    doubts = [f"{l['reference_cm1']:g} cm-1: " + ("the fit sits at the edge of its window" if "edge" in str(l.get("warning") or "")
                                                  else f"signal-to-noise {l['snr']:.0f} (below {min_snr:g})")
              for l in ok if "edge" in str(l.get("warning") or "") or (l.get("snr") is not None and l["snr"] < min_snr)]
    res["trusted"] = not doubts
    if doubts:
        res["not_trusted_because"] = doubts
        laser_bad_ = checks.get("laser_mode_change_cm1") is not None or (checks.get("linewidth") or {}).get("multimode")
        unreliable.append("the reference line was not measured well enough to judge the zero (" + "; ".join(doubts)
                          + ("): the laser changed mode or runs multimode (checks.laser_mode_change_cm1, "
                             "checks.linewidth): wait a few minutes or power-cycle the laser, then calibrate again"
                             if laser_bad_ else
                             "): use a stronger filter or a longer exposure, check the focus, then measure again"))
    checks["calibration"] = {"pass": res["pass"], "offset_cm1": res["offset_cm1"], "worst_error_cm1": res["worst_error_cm1"],
                             "tol_cm1": tol}
    if res["trusted"]:
        from . import perf
        llc = (rec.get("laser_line") or {})
        # with repeats, the laser line's median over them (the first alone may be an outlier: -3.53 vs -4.27)
        ll_rec = (((rep_info or {}).get("landing_steps") or {}).get("laser_line_median_cm1")
                  or ((rep_info or {}).get("anchored_to_laser_line") or {}).get("laser_line_cm1"))
        if ll_rec is None and llc.get("ok"):
            ll_rec = llc["position_cm1"]
        perf.record("zero", xcal.key(laser, grating, setup) if setup and any(setup.values())
                    else perf.key(laser, grating), {"offset_cm1": res["offset_cm1"], "reference": refname,
                                                        "pass": res["pass"], "tol_cm1": tol,
                                                        **({"laser_line_cm1": ll_rec}
                                                           if ll_rec is not None and res["pass"] else {})}, request)
    lk = P.laser_key(prof, laser)
    if lk and prof["lasers"][lk].get("mode_hops"):
        res["mode_note"] = (f"the {float(laser):g} nm laser has mode hops (profile lasers.{lk}.mode_hops): this "
                            "calibration holds for the laser mode it was measured in"
                            + (f" (laser line at {rec['laser_line']['position_cm1']:.2f} cm-1)"
                               if (rec.get("laser_line") or {}).get("ok") else
                               "; include the laser line in the window to record which mode"))
    # in words, so that 'pass: false' with a correction stored does not read as a failure (2026-11-23)
    if not res["trusted"]:
        res["outcome"] = "not judged: the reference line was not measured well enough"
    elif res["pass"]:
        res["outcome"] = f"within tolerance ({res['offset_cm1']:+.2f} cm-1, tolerance {tol:g})"
    elif res["adjust"] == "ls6t":
        res["outcome"] = (f"out of tolerance before correction ({res['offset_cm1']:+.2f} cm-1); corrected: ls6t "
                          f"corrects later spectra with {combo(laser, grating)}")
        res["corrected"] = True
    else:
        res["outcome"] = (f"out of tolerance ({res['offset_cm1']:+.2f} cm-1, tolerance {tol:g}): correct LabSpec's "
                          f"zero for {combo(laser, grating)}")
    checks["calibration"]["outcome"] = res["outcome"]
    # the zero mode a stored correction has (or would have had): said even when nothing is stored (Nicola, 2026-11-27)
    if res["adjust"] == "ls6t":
        anc_w = ((rep_info or {}).get("anchored_to_laser_line") or {}).get("laser_line_cm1")
        res["_zero_mode_would"] = ("anchored" if anc_w is not None else "absolute", None)
    else:
        res["_zero_mode_would"] = (None, "calibration.adjust is 'labspec': ls6t keeps no correction of its own")
    unsettled = bool(rep_info and not rep_info.get("settled", True))
    if unsettled and res["adjust"] == "ls6t" and res["trusted"]:
        # 2026-11-25, 637.75 @ 600: repeats 7.78, 7.13, -0.95, -0.51, -0.00 (a hop of -7.9 cm-1 after the hold was
        # lost); their median (-0.004) stored as the correction was wrong by ~8.6 cm-1. A step or a creep: nothing is
        # stored, the correction in use stays, and the calibration is not to be trusted
        what = (f"jump by {rep_info['step']['size_cm1']:+.2f} cm-1 after repeat {rep_info['step']['after_repeat']}"
                if rep_info.get("step") else
                (f"creep with the laser line ({rep_info['laser_creep']['laser_line_cm1']:+.2f} cm-1, the Raman lines "
                 f"{rep_info['laser_creep']['raman_cm1']:+.2f}: the laser not settled)")
                if rep_info.get("laser_creep") else
                f"creep by {rep_info['trend']['slope_cm1_per_repeat']:+.2f} cm-1 per repeat" if rep_info.get("trend")
                else "do not agree")
        res["stored_as"] = "nothing stored: the repeats did not settle; the correction in use stays"
        res["outcome"] = (f"not stored: the repeats did not settle (median {rep_info['median_cm1']:+.2f} cm-1, "
                          f"{rep_info['offsets_cm1']}); the correction in use stays")
        checks["calibration"]["outcome"] = res["outcome"]
        res.pop("corrected", None)
        if rep_info.get("laser_creep"):
            lk_ = P.laser_key(prof, laser) or f"{float(laser):g}"
            unreliable.append(f"the repeats {what}: no correction is stored for {combo(laser, grating)}, the previous "
                              f"one stays; calibrate again once the laser has settled (a longer wait after selecting "
                              f"it: lasers.{lk_}.change_settle_s, now {plan.change_settle_s(prof, laser, 'calibration'):g} s)")
        else:
            unreliable.append(f"the repeats {what} (a laser hop or the spectrometer landing elsewhere): no correction "
                              f"is stored for {combo(laser, grating)}, the previous one stays; calibrate again (with the "
                              "laser settled: the shutter held, no laser_off in between)")
    elif res["adjust"] == "ls6t" and res["trusted"]:
        llc = dict(rec.get("laser_line") or {})
        broad = laser_line_too_broad(llc.get("fwhm_cm1"), lines) if llc.get("ok") else None
        if broad:
            # kept, but only to catch large landing steps (Nicola, 2026-11-25, 600: after 1200 -> 600 the laser line
            # moved +1.9 and Si +2.25 with it; a 22.6 cm-1 line still shows a step that big)
            res["laser_line_broad"] = broad
            warnings.append(f"the calibration's laser line is no fine reference ({broad}): kept only to catch large "
                            f"landing steps (over {BROAD_LANDING_MIN_CM1:g} cm-1, the Raman lines moving alike)")
        # stored only once the whole study is judged reliable (store_pending, at the end of run_any): 2026-11-25,
        # two calibrations unreliable for multimode points and laser-line jumps still stored their corrections
        anc_ll = ((rep_info or {}).get("anchored_to_laser_line") or {}).get("laser_line_cm1")
        if anc_ll is None and \
                ((rep_info or {}).get("landing_steps") or {}).get("laser_line_median_cm1") is not None:
            llc = dict(llc or {}, ok=True, position_cm1=rep_info["landing_steps"]["laser_line_median_cm1"])
        elif anc_ll is None and rep_info and llc.get("ok"):
            # repeats, not anchored: the laser line's median over them (the first alone carries its own landing)
            lls_ = [float((r_.get("laser_line") or {})["position_cm1"]) for r_ in (repeats or [])
                    if (r_.get("laser_line") or {}).get("ok") and not (r_.get("laser_line") or {}).get("suspect")]
            if lls_:
                llc = dict(llc, position_cm1=float(np.median(lls_)))
        res["_pending_store"] = {
            "args": [float(laser), _f(grating), float(a), float(b), refname, lines, request],
            # anchored: one correction per window (the landing scatter that pooling averaged is gone)
            "kw": dict(centre_nm=wc, pool=("per_centre" if anc_ll is not None and cal.get("store", "auto") == "auto"
                                           else cal.get("store", "auto")), cm1_per_px=px, repeats=rep_info,
                       laser_line_cm1=anc_ll if anc_ll is not None else
                       (llc.get("position_cm1") if llc.get("ok") else None),
                       laser_line_fwhm_cm1=llc.get("fwhm_cm1") if llc.get("ok") else None,
                       placement="labspec" if not rng.get("single", True) else "centred",
                       range_cm1=[rng["from_cm1"], rng["to_cm1"]] if (wc is None and not rng.get("single", True)
                                                                        and rng.get("from_cm1") is not None)
                       else None,
                       # anchored (every later spectrum brought to the laser line, threshold 0) only with
                       # calibration.anchor 'always'; a single spectrum too is 'absolute' (T9.21: "follows ... over 0
                       # cm-1" after one, while the landing steps use the learned threshold)
                       zero_mode="anchored" if anc_ll is not None else "absolute",
                       shutter="held" if any(str((r_.get("shutter_hold_after") or {}).get("held")) == "yes"
                                             for r_ in (repeats or [rec])) else "auto")}
    elif not res["pass"]:
        m_rep = float(res.get("measured_cm1") if res.get("measured_cm1") is not None else first["measured_cm1"])
        unreliable.append(f"the {'Si' if refname == 'si' else refname} line is at {m_rep:.2f} cm-1, "
                          f"{m_rep - first['reference_cm1']:+.2f} from {first['reference_cm1']}: "
                          f"adjust the zero in LabSpec for {combo(laser, grating)} (calibration.adjust 'labspec' "
                          "guides it), or let ls6t correct (adjust 'ls6t')")
    return res


def store_pending(res: dict, warnings: list, unreliable: list, prof: dict | None = None, objective=None,
                  hole=None) -> None:
    """A calibration's correction, stored only when the study ended reliable; otherwise nothing is stored, the
    correction in use stays, and stored_as / outcome say so (Nicola, 2026-11-25)."""
    from . import xcal
    pend = res.pop("_pending_store", None)
    if not pend:
        return
    res["zero_mode"] = pend["kw"].get("zero_mode")
    laser, grating, a, b = pend["args"][:4]
    if unreliable:
        # the first reason in full (15:43: cut mid-sentence), the rest in unreliable_because
        res["stored_as"] = ("nothing stored: the calibration is unreliable (" + unreliable[0]
                            + (f"; and {len(unreliable) - 1} more in unreliable_because" if len(unreliable) > 1 else "")
                            + "); the correction in use stays")
        res.pop("corrected", None)
        res["outcome"] = (f"measured {res.get('offset_cm1', 0):+.2f} cm-1 but NOT stored: the calibration is unreliable; "
                          "the correction in use stays")
        return
    # a large jump from the last correction at this window waits for a second calibration (Nicola, 2026-11-25:
    # 2400, -2.53 -> +6.30 cm-1 in a day, stored with a warning only; the laser line outside the window could not
    # tell a landing or a hop)
    kw_ = pend["kw"]
    setup_ = xcal.setup_of(objective, hole)
    prev = xcal.at_centre(laser, grating, kw_.get("centre_nm"), kw_.get("placement"), range_cm1=kw_.get("range_cm1"),
                          setup=setup_)
    same_win = prev is not None
    if same_win and float(b) == 1.0 and float(prev.get("b") or 1.0) == 1.0:
        mv = float((spectro_repeatability(prof or {}, laser, grating, kw_.get("placement") or "centred")
                    if prof else {}).get("moved_sd_cm1") or 0.3)
        # ordinary day-to-day drift is not a jump (Nicola, 2026-11-25): the limit grows with the last correction's
        # age, 1 cm-1 per 6 h, at most JUMP_AGE_CAP_CM1; a sudden jump still waits for a second calibration
        age_prev = xcal.age_h(prev) or 0.0
        thr = max(JUMP_CONFIRM_MIN_CM1, 3.0 * mv) + min(JUMP_AGE_CAP_CM1, JUMP_AGE_CM1_PER_H * float(age_prev))
        jump = float(a) - float(prev["a"])
        if abs(jump) > thr:
            pk = (f"{xcal.key(laser, grating, setup_)}@{kw_.get('centre_nm')}@{kw_.get('placement')}"
                  f"@{kw_.get('range_cm1')}")
            pend_all = store.read("xcal_pending.json") or {}
            p_ = pend_all.get(pk)
            agree = max(1.0, 3.0 * mv)
            fresh = p_ is not None and xcal.age_h({"time": p_.get("time")}) is not None and \
                xcal.age_h({"time": p_.get("time")}) <= 4.0
            if fresh and abs(float(a) - float(p_["a"])) <= agree:
                pend_all.pop(pk, None)
                store.write("xcal_pending.json", pend_all)
                res["jump_confirmed"] = {"from_cm1": round(-float(prev["a"]), 3), "to_cm1": round(-float(a), 3),
                                         "first_seen": p_.get("time"), "request": p_.get("request")}
                warnings.append(f"the zero moved {-jump:+.2f} cm-1 since {prev.get('time')}; a second calibration "
                                f"({p_.get('time')}) agrees, so the new correction is stored")
            else:
                pend_all[pk] = {"a": round(float(a), 5), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "request": pend["args"][6] if len(pend["args"]) > 6 else None}
                store.write("xcal_pending.json", pend_all)
                res["stored_as"] = (f"nothing stored yet: the zero moved {-jump:+.2f} cm-1 since {prev.get('time')} "
                                    f"(more than {thr:.1f}); a second calibration that agrees (within {agree:.1f}) "
                                    "stores it; the correction in use stays")
                res["outcome"] = (f"measured {res.get('offset_cm1', 0):+.2f} cm-1, NOT stored yet: a jump of "
                                  f"{-jump:+.2f} cm-1 waits for a second calibration")
                res.pop("corrected", None)
                res["needs_confirmation"] = True
                unreliable.append(f"the zero moved {-jump:+.2f} cm-1 since the last calibration at this window "
                                  f"({prev.get('time')}, {age_prev:.1f} h ago): more than {thr:.1f} cm-1 (3 x the "
                                  f"repositioning, at least {JUMP_CONFIRM_MIN_CM1:g}, plus 1 per 6 h of age up to "
                                  f"{JUMP_AGE_CAP_CM1:g}); calibrate again: a second calibration that agrees stores "
                                  "it")
                return
    if same_win and float(b) == 1.0:
        # the zero's drift over hours, from successive calibrations at this window (Nicola, 2026-11-25: 1.9 h after
        # the calibration Si read +0.93 / +0.45 off; a series right after calibrating does not see that)
        age_p = xcal.age_h(prev) or 0.0
        if age_p > 0 and not res.get("needs_confirmation"):
            ch = -(float(a) - float(prev["a"]))           # + = the reference line reads higher (as drift_at_centre)
            # this pair's rate, signed as cm1 (T9.23: 0.042 shown unsigned for a -0.452 drift, as if learned)
            zd = {"cm1": round(ch, 3), "hours": round(age_p, 2),
                  "pair_cm1_per_h": round(ch / age_p, 3) if age_p > 0 else None}
            # learned only from good pairs (Nicola, 2026-11-26: 1.06 cm-1/h learned at 1200 from two single fits
            # with the shutter cycling and the laser changing mode, against +0.01 in 2 h with the hold)
            why_not = []
            n_new = int(((res.get("repeats") or {}).get("n")) or 1)
            n_old = int(((prev.get("repeats") or {}).get("n")) or 1)
            if min(n_new, n_old) < 3:
                why_not.append(f"a calibration with fewer than 3 repeats ({n_old} and {n_new})")
            if kw_.get("shutter") != "held" or prev.get("shutter") != "held":
                why_not.append("the shutter was not held in both (the laser may change mode as it cycles)")
            ll_new, ll_old = kw_.get("laser_line_cm1"), prev.get("laser_line_cm1")
            if ll_new is not None and ll_old is not None:
                thr_ll = float(landing_threshold(prof or {}, laser, grating, kw_.get("placement") or "centred")
                               ["threshold_cm1"]) if prof else 0.5
                if abs(float(ll_new) - float(ll_old)) > thr_ll:
                    why_not.append(f"the laser line moved {float(ll_new) - float(ll_old):+.2f} cm-1 between them "
                                   f"(over {thr_ll:g}: a laser mode change, not the spectrometer's drift)")
            if not 0.5 <= age_p <= 24.0:
                why_not.append(f"{age_p:.1f} h apart (pairs 0.5 to 24 h apart are used)")
            if why_not:
                zd["not_learned_because"] = why_not
                zd["learned"] = False
            else:
                zd["learned"] = True
                zd["learned_zero_drift_cm1_per_h"] = learn_zero_drift(laser, grating, kw_.get("placement") or
                                                                      "centred", ch, age_p)
                # applied once enough good pairs exist (DRIFT_MIN_PAIRS)
                zd["applied"] = zd["learned_zero_drift_cm1_per_h"] is not None
            res["zero_drift_since_last"] = zd
    st = xcal.store(*pend["args"], **pend["kw"], hole_um=_f(hole), objective=objective, setup=setup_)
    si_ = next((l_ for l_ in (pend["args"][5] or []) if l_.get("fwhm_cm1")
                and abs(float(l_.get("reference_cm1") or 0) - 520.7) < 20), None)
    if si_:
        # the Si width per objective (Nicola, 2026-11-27: 5.44 cm-1 at x100, 8.55 at x10, with the 300 um hole)
        note_si_width(laser, grating, objective, si_["fwhm_cm1"], si_.get("eta"), "a stored calibration", hole)
    res["stored_as"] = (("one correction for every window of " if st.get("pooled") else
                         "for this range: " if st.get("range_cm1") else "per window centre: ")
                        + combo(laser, grating) + " (" + st["pool"]["why"] + ")"
                        + ("; relative to the laser line (later spectra with it in the window are brought to it "
                           "first: laser_reference 'anchor')" if pend["kw"].get("zero_mode") == "anchored" else ""))
    if st.get("drift_at_centre"):
        dr = st["drift_at_centre"]
        warnings.append(f"the zero at this window moved {dr['cm1']:+.2f} cm-1 (the reference line reads that much "
                        f"{'higher' if dr['cm1'] >= 0 else 'lower'}) since {dr['since']}"
                        + (f"; the corrections at {', '.join(str(o['centre_nm']) for o in st.get('older_centres', []))}"
                           " nm are older and still carry that drift: calibrate them again"
                           if st.get("older_centres") else ""))
    res["stored_correction"] = st
    warnings.append(f"ls6t now corrects later spectra with {combo(laser, grating)}, {xcal.describe(xcal.key(laser, grating, setup_))} "
                    f"({xcal.key(laser, grating, setup_)}): true = {a:+.3f} + {b:.6f} x measured")
    res["stored_as"] = str(res.get("stored_as") or "") + f"; for {xcal.describe(xcal.key(laser, grating, setup_))} only"
    wr = learn_window_rate(laser, grating, kw_.get("placement") or "centred", kw_.get("centre_nm"), float(a),
                           pend["args"][6] if len(pend["args"]) > 6 else None, setup=setup_)
    if wr:
        res["window_rate"] = wr



def _point_summary(r: dict) -> dict:
    keys = ("point", "offset", "stage", "file", "native", "t", "exposure_s", "accumulations", "max_counts",
            "saturated", "noise_sigma", "noise_sigma_pixel", "noise_correlation", "denoise", "spikes", "spike_cm1",
            "spike_info", "from_cm1", "to_cm1", "peaks", "bands",
            "x_correction", "laser_line_correction_cm1", "zero_uncertain", "excluded",
            "background", "auto_peaks_rejected", "spikes_in_requested_peaks",
            "ics", "auto_exposure", "optics_check", "t_start", "shutter_hold_after")
    out = {k: r.get(k) for k in keys if k not in ("auto_exposure", "optics_check", "t_start",
                                                    "laser_line_correction_cm1", "zero_uncertain", "excluded",
                                                    "shutter_hold_after", "noise_sigma_pixel", "noise_correlation",
                                                    "denoise")
           or r.get(k) is not None}
    if r.get("auto_peaks"):
        out["auto_peaks_rejected"] = r.get("auto_peaks_rejected") or []      # always there, empty or not (T9.19)
    return out


def _quantities(per_point, process) -> dict:
    """Per-point values for maps and series; a spectrum kept out (`excluded`: weak, no laser line) gives none."""
    q: dict = {}
    for r in per_point:
        out_ = bool(r.get("excluded"))
        for i, pk in enumerate(r.get("peaks", []) or []):
            nm = pk.get("name") or f"peak{i}"
            for f in ("position_cm1", "fwhm_cm1", "height", "area", "snr"):
                q.setdefault(nm, {}).setdefault(f, []).append(pk.get(f) if pk.get("ok") and not out_ else None)
        for bn, val in (r.get("bands") or {}).items():
            q.setdefault(bn, {}).setdefault("area", []).append(None if out_ else val)
        q.setdefault("max_counts", {}).setdefault("value", []).append(r.get("max_counts"))
    return q


def _crossings(d: np.ndarray, u: np.ndarray, level: float) -> list[tuple[float, int]]:
    """Where u crosses `level` between samples: (interpolated d, +1 rising / -1 falling with d)."""
    out = []
    for i in range(len(u) - 1):
        a, b = u[i], u[i + 1]
        if not (np.isfinite(a) and np.isfinite(b)) or a == b:
            continue
        if (a - level) * (b - level) <= 0 and not (a == level and i > 0):
            t = (level - a) / (b - a)
            out.append((float(d[i] + t * (d[i + 1] - d[i])), 1 if b > a else -1))
    return out


def depth_result(study: dict, mres: dict, prof: dict, sd: Path, warnings: list) -> dict:
    """A depth profile's view of a Z-only map: each peak's height and area and each band's area against the nominal
    depth (stage displacement into the sample from the focus) and, with a refractive index, the corrected depth
    (paraxial: depth = n x displacement); the interfaces, where a profile crosses half-way between its lowest and
    highest value (rising: a layer starts; falling: it ends), with the 10-90 % width."""
    sgn, stated = plan.into_sign(prof)
    dp = study.get("_depth") or {}
    n = dp.get("refractive_index")
    z = np.asarray(mres["z_um"], float)
    nominal = sgn * z
    depth = nominal * float(n) if n else nominal
    grids = json.loads(Path(mres["files"]["json"]).read_text(encoding="utf-8"))["grids"]
    profiles, interfaces = {}, []
    for key, g in grids.items():
        qn, f = key.rsplit(".", 1)
        if f not in ("height", "area"):
            continue
        v = np.array([np.nan if row[0][0] is None else row[0][0] for row in g], float)
        if not np.isfinite(v).any():
            continue
        profiles[key] = [None if not np.isfinite(a) else round(float(a), 3) for a in v]
        fin = v[np.isfinite(v)]
        lo, hi = float(fin.min()), float(fin.max())
        if hi <= 0 or (hi - lo) < 0.3 * hi:
            continue
        u = (np.nan_to_num(v, nan=lo) - lo) / (hi - lo)
        order = np.argsort(depth)
        dd, uu = depth[order], u[order]
        for x50, direction in _crossings(dd, uu, 0.5):
            near = [c for c, dr in _crossings(dd, uu, 0.1) + _crossings(dd, uu, 0.9) if dr == direction]
            lo_c = max([c for c in near if c <= x50], default=None)
            hi_c = min([c for c in near if c >= x50], default=None)
            interfaces.append({"quantity": key, "direction": "rise" if direction > 0 else "fall",
                               "depth_um": round(x50, 3),
                               "nominal_um": round(x50 / float(n), 3) if n else round(x50, 3),
                               "width_10_90_um": round(hi_c - lo_c, 3) if lo_c is not None and hi_c is not None
                               else None})
    out = {"into_sign": sgn, "direction_stated": stated, "nominal_um": [round(float(a), 3) for a in nominal],
           "refractive_index": n, "depth_um": [round(float(a), 3) for a in depth], "profiles": profiles,
           "interfaces": sorted(interfaces, key=lambda i: i["depth_um"])}
    by: dict = {}
    for it in out["interfaces"]:                     # the first rise and the first fall of each quantity
        q, f = it["quantity"].rsplit(".", 1)
        by.setdefault(q, {}).setdefault(f, {}).setdefault(it["direction"], it["depth_um"])
    out["interfaces_by"] = by
    if n:
        out["note"] = ("depth = n x stage displacement (paraxial). With high-NA objectives the real focus lies deeper "
                       "and spreads (Everall, Appl. Spectrosc. 54, 2000): treat depths as estimates; an oil- or "
                       "water-immersion objective or a known layer thickness calibrates them")
    else:
        out["note"] = ("depths are stage displacements; inside a transparent sample the focus goes about n times "
                       "deeper (give depth.refractive_index to correct)")
    if not stated:
        warnings.append("the profile does not say which Z direction goes into the sample (stage.z_into_sample): "
                        "+ was assumed")
    from .runner_core import clean, write_json
    write_json(sd / "depth.json", clean(out))
    return out


def _map_result(name, study, per_point, axes, ys, sd: Path, process) -> dict:
    nx, ny, nz = plan.map_shape(study)
    m = study.get("map", {})
    xs = plan.axis_values(m["x_um"]) if m.get("x_um") else [0.0]
    yv = plan.axis_values(m["y_um"]) if m.get("y_um") else [0.0]
    zs = plan.axis_values(m["z_um"]) if m.get("z_um") else [0.0]
    pos = plan.positions(study)
    q = _quantities(per_point, process)
    grids: dict = {}
    for qn, fields in q.items():
        for f, vals in fields.items():
            g = np.full((nz, ny, nx), np.nan)
            for r, v in zip(per_point, vals):
                p = r.get("ixyz") or pos[r["point"]]
                if v is not None:
                    g[p["iz"], p["iy"], p["ix"]] = v
            grids[f"{qn}.{f}"] = g
    summary: dict = {}
    for key, g in grids.items():
        qn, f = key.rsplit(".", 1)
        summary.setdefault(qn, {})[f] = _stats(g.ravel().tolist())
    files = {}
    if axes:
        ref = axes[0]
        spec = np.full((len(per_point), ref.size), np.nan)
        for i, (xa, ya) in enumerate(zip(axes, ys)):
            spec[i] = np.interp(ref, xa, ya, left=np.nan, right=np.nan)
        idx = np.array([[(r.get("ixyz") or pos[r["point"]])[k] for k in ("ix", "iy", "iz")] for r in per_point])
        npz = sd / f"{name}_map.npz"
        np.savez_compressed(npz, shift_cm1=ref, spectra=spec, index_xyz=idx, x_um=np.array(xs),
                            y_um=np.array(yv), z_um=np.array(zs),
                            **{k.replace(".", "__"): v for k, v in grids.items()})
        files["npz"] = str(npz)
    mj = sd / f"{name}_map.json"
    from .runner_core import clean, write_json
    write_json(mj, clean({"x_um": xs, "y_um": yv, "z_um": zs, "shape_zyx": [nz, ny, nx],
                          "grids": {k: v.tolist() for k, v in grids.items()}}))
    files["json"] = str(mj)
    if (sd / f"{name}.l6s").exists():
        files["native"] = str(sd / f"{name}.l6s")
    return {"nx": nx, "ny": ny, "nz": nz, "x_um": xs, "y_um": yv, "z_um": zs, "points": len(per_point),
            "quantities": sorted(grids), "summary": summary, "files": files}


def _series_result(per_point, process) -> dict:
    q = _quantities(per_point, process)
    return {"t_s": [r["t"] for r in per_point], "values": q,
            "summary": {qn: {f: _stats(v) for f, v in fields.items()} for qn, fields in q.items()}}


# --------------------------------------------------------------------------------------------- expect

def lookup(data, path: str):
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def evaluate_expect(expect: list, result: dict) -> list:
    out = []
    for e in expect or []:
        got = lookup(result, e["quantity"])
        tol = float(e.get("tol", 0))
        ok = isinstance(got, (int, float)) and not isinstance(got, bool) and abs(got - e["value"]) <= tol
        out.append({"quantity": e["quantity"], "value": e["value"], "tol": tol,
                    "got": got if isinstance(got, (int, float, str)) or got is None else "(not a number)",
                    "pass": bool(ok)})
    return out
