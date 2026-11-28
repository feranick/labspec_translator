"""calibration_learn.py — spectrometer repeatability, laser-line scatter, zero creep, drift, and window-rate learning.

Split from runner.py so calibration uncertainty modeling lives in its own focused module.
"""
from __future__ import annotations

import math
import time

import numpy as np

from . import plan, spectra, store
from .io_util import to_float


def _f(v):
    return to_float(v, finite=True)



def _clean_rep_entry(e: dict) -> dict:
    """Learned values from before their version are dropped from the store itself, not only ignored (2026-11-26: the
    1.061 cm-1/h at 1200 still showed in the calibration's repeatability block)."""
    for v_key, fields in (("zero_drift_v", ("zero_drift_cm1_per_h", "zero_drift_history", "zero_drift_time")),
                          ("laser_line_sd_v", ("laser_line_sd_cm1", "laser_line_sd_cm1_history")),
                          ("zero_creep_v", ("zero_creep_cm1_per_min", "zero_creep_history",
                                            "zero_creep_cm1_per_min_history"))):
        want = {"zero_drift_v": DRIFT_VERSION, "laser_line_sd_v": LL_SD_VERSION,
                "zero_creep_v": CREEP_VERSION}[v_key]
        if any(f in e for f in fields) and e.get(v_key) != want:
            for f in fields:
                e.pop(f, None)
    return e


def spectro_repeatability(prof: dict, laser, grating, placement: str = "centred") -> dict:
    """How well the spectrometer repeats at laser@grating: the profile's spectrometer.repeatability["<laser>@<grating>"]
    (set by a person), else the last measured (LS6T_HOME/spectro_repeatability.json); a moved SD measured with another
    approach_nm than the profile's now does not count. Windows LabSpec places itself (extended ranges, its own maps)
    get no approach: their entry is "<laser>@<grating>@labspec", never the approach-based one (Nicola, 2026-11-25)."""
    kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "")
    sp_ = prof.get("spectrometer") or {}
    rp_ = sp_.get("repeatability") if isinstance(sp_.get("repeatability"), dict) else {}
    own = rp_.get(kk)
    rec = dict((store.read("spectro_repeatability.json") or {}).get(kk) or {})
    if rec.get("zero_drift_cm1_per_h") is not None and rec.get("zero_drift_v") != DRIFT_VERSION:
        rec.pop("zero_drift_cm1_per_h")            # learned before 2026.11.26.6 (single fits, a cycling shutter)
    if rec.get("zero_creep_cm1_per_min") is not None and rec.get("zero_creep_v") != CREEP_VERSION:
        rec.pop("zero_creep_cm1_per_min")          # one series' rate from before 2026.11.25.18: not applied
    if rec.get("laser_line_sd_cm1") is not None and rec.get("laser_line_sd_v") != LL_SD_VERSION:
        # learned before 2026.11.25.18 from all the repeats, steps included (637.75 @ 1200: 0.353): not used
        rec.pop("laser_line_sd_cm1")
    if placement != "labspec" and rec.get("moved_sd_cm1") is not None and \
            float(rec.get("approach_nm", 0) or 0) != float(sp_.get("approach_nm", 0) or 0):
        rec.pop("moved_sd_cm1")
    if isinstance(own, dict) and own:
        # a person's values over the measured ones (the laser line's scatter and the zero's creep are learned too)
        return dict(rec, **own, source="profile")
    return dict(rec, source="measured") if rec else {}


LANDING_K = 3.0               # a landing step: the laser line off the calibration's by this x its own scatter
LANDING_MIN_CM1 = 0.5         # and at least this
BROAD_LANDING_MIN_CM1 = 1.0   # with a broad laser line (600 gr/mm, ~22 cm-1): large steps only
LL_SD_DEFAULT_CM1 = 0.2       # the laser line's own scatter until learned (637.75 @ 1200, 2026-11-25: ~0.2)
LL_SD_VERSION = 2             # learned laser_line_sd_cm1 values from 2026.11.25.18 on (older ones are ignored)
CREEP_CAP_CM1 = 2.0           # the zero's creep counted into the expected uncertainty, at most
WINDOW_SAME_NM = 0.05          # within this of the calibration's centre: the same window
WINDOW_PAIR_MIN_NM = 1.0       # two calibrations this far apart (or more) give a window rate (closer: noise)
WINDOW_PAIR_MAX_H = 0.25       # and within this time of each other, or in the same request (drift stays out)
DRIFT_RAMAN_FRACTION = 0.5     # an unfollowed laser-line drift moves the Raman lines about this much of it (the
#                                line on the edge filter moves about twice as much: 2026-11-24)
WINDOW_TERM_CAP_CM1 = 2.0      # the window term, at most (between windows: a cm-1 or more, 2026-11-24)
JUMP_CONFIRM_MIN_CM1 = 2.0     # a new correction this far (or 3 x the repositioning) from the last one at that
#                                window waits for a second calibration to confirm it (2400: -2.53 -> +6.30)
JUMP_AGE_CM1_PER_H = 1.0 / 6   # plus the drift the last correction's age allows (637.75: 2-3 cm-1 within a day)
JUMP_AGE_CAP_CM1 = 4.0         # at most this much for the age


def landing_threshold(prof: dict, laser, grating, placement: str = "centred", fallback_sd=None) -> dict:
    """How far the laser line must sit from the calibration's to be a landing step (Nicola, 2026-11-25: follow the
    rare 1.5-2 cm-1 landings, not the line's own ~0.2 fit noise in every spectrum): LANDING_K x its scatter,
    learned per laser@grating (spectro_repeatability: laser_line_sd_cm1, a person's value in the profile first),
    at least LANDING_MIN_CM1."""
    rep = spectro_repeatability(prof, laser, grating, placement) if laser else {}
    sd = rep.get("laser_line_sd_cm1")
    kk_ = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "") if laser else ""
    own_ = (((prof.get("spectrometer") or {}).get("repeatability") or {}).get(kk_) or {}) if laser else {}
    src = None
    if sd is not None:
        src = ("profile: laser_line_sd_cm1" if isinstance(own_, dict) and own_.get("laser_line_sd_cm1") is not None
               else f"measured: laser_line_sd_cm1 (learned from {rep.get('laser_line_sd_from') or '?'})")
    if sd is None and rep.get("still_sd_cm1"):
        # no laser-line scatter known yet: the Si line's scatter without moves stands in (Nicola, 2026-11-25: the
        # profile's still/moved values were ignored and 5 repeats with a step in them set the threshold)
        sd = rep["still_sd_cm1"]
        src = f"{rep.get('source')}: still_sd_cm1 (Si without moves, standing in for the laser line's own scatter)"
    if sd is None:
        sd, src = ((fallback_sd, "this calibration's repeats (the tightest 3 of them: a last resort)") if fallback_sd
                   else (LL_SD_DEFAULT_CM1, "default"))
    return {"threshold_cm1": round(max(LANDING_MIN_CM1, LANDING_K * float(sd)), 3),
            "laser_line_sd_used_cm1": round(float(sd), 3), "from": src}



COARSE_CM1_PER_PX = 2.0    # a grating this coarse at the reference line: per-centre corrections are pooled (auto)


def _cm1_per_px(rec: dict, laser, near_cm1: float) -> float | None:
    """The spectrum's sampling (cm-1 per pixel) near a line, from its own axis (600 gr/mm at 660 nm: ~4.5)."""
    try:
        sp = spectra.read(rec["file"])
        sh, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        sh = np.sort(np.asarray(sh, float))
        sel = np.abs(sh - near_cm1) < 50
        d = np.diff(sh[sel]) if sel.sum() > 3 else np.diff(sh)
        return round(float(np.median(np.abs(d))), 3) if d.size else None
    except Exception:  # noqa: BLE001 - only a hint for how to store
        return None


OUTLIER_K = 6.0   # an outlier repeat: over 6 x the others' SD from their mean (and 0.3 cm-1)
STEP_T = 5.0      # a step between two runs of repeats: their means over 5 x the pooled standard error
TREND_T = 5.0     # a creep: a slope over 5 x its standard error (few repeats: a 1 % false alarm, not 5 %)


def repeat_stats(offs: list) -> dict:
    """Robust statistics of calibration repeats (Nicola, 2026-11-24 .20: with the spectrometer moved away and back
    before each, 637.75 @ 600 gave 11.09, 8.96, 9.17, 8.94, 9.51 and 8.93, 9.32, 10.01, 12.33, 11.71: a first spectrum
    off after a grating change, and a step, the spectrometer landing ~0.5 px elsewhere; a slope test read the step as
    a drift and kept it). An outlier: more than 3 robust SDs (and 0.3 cm-1) from the median. A step: two runs whose
    means differ by over 4 x their pooled standard error (each run >= 2 spectra). The centre is the median of the kept
    values; with a step the scatter is not noise (settled false: not used to compare windows)."""
    v = np.asarray([o for o in offs if o is not None], float)
    out: dict = {"n_all": int(v.size)}
    if v.size < 2:
        return dict(out, centre=float(v[0]) if v.size else None, kept=v.tolist(), outliers=[], step=None,
                    settled=True)
    # an outlier: off the others' mean by more than OUTLIER_K x their SD (as a prediction for one more), and 0.3 cm-1
    # (2026-11-25: 9.597 among 8.90s was missed when the jump itself inflated the noise estimate); at most one per
    # five repeats, the worst first. Pure noise: ~4 % of 5-repeat sets lose one, under 1 % from 8 repeats.
    idx: list = []
    if v.size >= 4:
        for _ in range(max(1, v.size // 5)):
            rest = [i for i in range(v.size) if i not in idx]
            worst = None
            for i in rest:
                o = v[[j for j in rest if j != i]]
                lim_i = max(OUTLIER_K * float(np.std(o, ddof=1)) * math.sqrt(1 + 1 / o.size), 0.3)
                dev = abs(v[i] - float(np.mean(o)))
                if dev > lim_i and (worst is None or dev / lim_i > worst[0]):
                    worst = (dev / lim_i, i)
            if worst is None:
                break
            idx.append(worst[1])
        idx.sort()
    keep = np.asarray([v[i] for i in range(v.size) if i not in idx])
    step = None
    if keep.size >= 4:
        best = None
        for k in range(2, keep.size - 1):
            g1, g2 = keep[:k], keep[k:]
            sw2 = ((g1.size - 1) * np.var(g1, ddof=1) + (g2.size - 1) * np.var(g2, ddof=1)) / (keep.size - 2)
            se = math.sqrt(max(sw2, 1e-6) * (1 / g1.size + 1 / g2.size))
            d = float(np.mean(g2) - np.mean(g1))
            t = abs(d) / se
            if best is None or t > best[0]:
                best = (t, k, d)
        if best and best[0] > STEP_T and abs(best[2]) > 0.3:
            step = {"after_repeat": int(best[1]), "size_cm1": round(best[2], 3), "t": round(best[0], 1)}
    trend = None
    if step is None and keep.size >= 5:
        # a steady creep (the laser settling after its shutter opened): a slope over 3 x its standard error
        t_ = np.arange(keep.size, dtype=float)
        k_, c_ = np.polyfit(t_, keep, 1)
        res_ = keep - (k_ * t_ + c_)
        se_ = math.sqrt(float(np.sum(res_ ** 2)) / (keep.size - 2) / float(np.sum((t_ - t_.mean()) ** 2)))
        if abs(k_) > TREND_T * max(se_, 1e-6) and abs(k_) * (keep.size - 1) > 0.3:
            trend = {"slope_cm1_per_repeat": round(float(k_), 3)}
    return dict(out, centre=float(np.median(keep)), kept=keep.tolist(), outliers=idx, step=step, trend=trend,
                settled=step is None and trend is None)


def _repeat_offsets(per_point, ref_lines) -> list:
    """Each repeat's offset (mean of its lines' measured - reference), as measured: a laser-line shift the study
    applied (laser_reference correct / correct_each) is undone, so the correction never depends on the calibration
    study's mode (Nicola, 2026-11-25); None where no line was fitted."""
    out = []
    for r_ in per_point:
        llc_ = float(r_.get("laser_line_correction_cm1") or 0.0)
        e = [pk["position_cm1"] - llc_ - ref for ref, pk in zip(ref_lines, r_.get("peaks") or [])
             if pk.get("ok") and pk.get("position_cm1") is not None]
        out.append(float(np.mean(e)) if e else None)
    return out


def _anchor_repeats(repeats: list, offs: list, lines: list, cal: dict, b: float) -> dict | None:
    """Calibration repeats judged relative to the laser line (Nicola, 2026-11-25): when every repeat has a laser line
    narrow enough to be a reference (not at 600 gr/mm: ~21 cm-1 wide on the edge filter) and the fit is an offset
    only. Returns the stats of (line - laser line), the laser line's median, or None. calibration.anchor 'off'
    keeps the absolute repeats."""
    if str(cal.get("anchor", "auto")) == "off":
        return {"why_not": "calibration.anchor is off"}
    if float(b) != 1.0:
        return {"why_not": "the fit has a scale (two reference lines): offset-only fits are anchored"}
    lls, ws = [], []
    for r_ in repeats:
        l_ = r_.get("laser_line") or {}
        ok_ = bool(l_.get("ok")) and l_.get("position_cm1") is not None and not l_.get("suspect")
        lls.append(float(l_["position_cm1"]) if ok_ else None)
        if ok_ and l_.get("fwhm_cm1"):
            ws.append(float(l_["fwhm_cm1"]))
    if not ws:
        return {"why_not": "no laser line in the window (it must include about -15 to +15 cm-1)"}
    broad = laser_line_too_broad(float(np.median(ws)), lines)
    if broad:
        return {"why_not": broad}
    # offs are as measured (_repeat_offsets undoes this study's laser-line shifts)
    rel = [None if (o is None or l_ is None) else o - l_ for o, l_ in zip(offs, lls)]
    n_off = sum(o is not None for o in offs)
    if sum(v is not None for v in rel) < max(2, n_off - 1):
        return {"why_not": "the laser line was not fitted in enough repeats"}
    # only when it helps: the scatter relative to the laser line below the scatter on its own (2026-11-25, 532 @
    # 1200: a 15 cm-1 wide line on the edge filter, Si - laser line scattering 0.30-0.36)
    rv = [v for v in rel if v is not None]
    av = [o for o in offs if o is not None]
    if len(rv) >= 3 and len(av) >= 3:
        sr, sa = _robust_sd(rv), _robust_sd(av)
        if sr >= sa:
            return {"why_not": f"relative to the laser line the repeats scatter {sr:.2f} cm-1, not less than on their "
                               f"own ({sa:.2f}): the laser line adds its own scatter (a broad line on the edge filter)"}
    return {"stats": repeat_stats(rel), "relative": rel, "laser_lines": lls,
            "laser_line_cm1": float(np.median([v for v in lls if v is not None]))}


WIDTH_CHANGE_FRAC = 0.08    # a line's width trending by more than this fraction over a series: the sample changed


def zero_creep(per_point: list, excluded: dict, laser, grating, placement: str) -> dict | None:
    """A series' slow zero creep (Nicola, 2026-11-25, 637.75 @ 1200: Si 521.07 -> 521.48 over minutes with the laser
    line steady): the first fitted peak's slope against time, with its t value, and the laser line's. Significant
    (t >= 3 and >= 0.3 cm-1 over the series) with the laser line steady: learned per laser@grating
    (zero_creep_cm1_per_min), so later spectra's expected uncertainty grows with the correction's age in minutes."""
    rows = []
    for r_ in per_point:
        if r_["point"] in (excluded or {}) or r_.get("t") is None:
            continue
        pk = next((p_ for p_ in (r_.get("peaks") or []) if p_.get("ok") and p_.get("position_cm1") is not None), None)
        if pk is None:
            continue
        ll = r_.get("laser_line") or {}
        rows.append((float(r_["t"]) / 60.0, float(pk["position_cm1"]), pk.get("name") or "peak",
                     float(ll["position_cm1"]) if ll.get("ok") else None,
                     float(pk["fwhm_cm1"]) if pk.get("fwhm_cm1") else None))
    if len(rows) < 4:
        return None
    t = np.array([r_[0] for r_ in rows])
    y = np.array([r_[1] for r_ in rows])
    span = float(t.max() - t.min())
    if span <= 0:
        return None
    A = np.vstack([t, np.ones_like(t)]).T
    coef, res_, *_ = np.linalg.lstsq(A, y, rcond=None)
    slope = float(coef[0])
    resid = y - A @ coef
    se = float(np.sqrt(np.sum(resid ** 2) / max(1, len(y) - 2) / np.sum((t - t.mean()) ** 2))) if len(y) > 2 else 0.0
    tval = abs(slope) / se if se > 0 else float("inf")
    out = {"peak": rows[0][2], "cm1_per_min": round(slope, 4), "t_value": round(min(tval, 999.0), 1),
           "minutes": round(span, 2), "total_cm1": round(slope * span, 3), "points": len(rows)}
    lls = [(r_[0], r_[3]) for r_ in rows if r_[3] is not None]
    if len(lls) >= 4:
        lt = np.array([v[0] for v in lls])
        ly = np.array([v[1] for v in lls])
        ls_ = float(np.polyfit(lt, ly, 1)[0])
        out["laser_line_cm1_per_min"] = round(ls_, 4)
        out["laser_line_steady"] = abs(ls_) < 0.3 * abs(slope) if slope else True
    out["significant"] = bool(tval >= 3.0 and abs(slope * span) >= 0.3)
    ws_ = [(r_[0], r_[4]) for r_ in rows if r_[4] is not None]
    if len(ws_) >= 4:
        # the line's width over the series: the spectrometer's zero moving does not change it; the sample does (Nicola,
        # 2026-11-27, 532 at x100: Si +0.48 in 4.8 min while it narrowed 7.96 -> 6.57 cm-1 and the counts fell ~18 %,
        # the laser line steady: heating or focus, not the spectrometer)
        wt = np.array([v[0] for v in ws_])
        wy = np.array([v[1] for v in ws_])
        kw_, cw_ = np.polyfit(wt, wy, 1)
        rw_ = wy - (kw_ * wt + cw_)
        sew_ = float(np.sqrt(np.sum(rw_ ** 2) / max(1, len(wy) - 2) / np.sum((wt - wt.mean()) ** 2)))
        dw_ = float(kw_) * span
        out["width_change_cm1"] = round(dw_, 3)
        if abs(dw_) > WIDTH_CHANGE_FRAC * float(np.median(wy)) and abs(kw_) > 3.0 * max(sew_, 1e-9):
            out["width_changed"] = True
            out["not_the_spectrometer"] = (f"the {rows[0][2]} line's width changed {dw_:+.2f} cm-1 over the series: the "
                                           "sample changed (laser heating or focus), not the spectrometer's zero; "
                                           "not learned as a zero creep")
    # every series of a few minutes counts (Nicola, 2026-11-25: ~0.1 cm-1/min once, -0.002 over 20 min an hour
    # later): a rate is applied only when it recurs, in 2 of the last 3 series
    if span >= 3.0:
        learned = learn_zero_creep(laser, grating, placement, abs(slope),
                                   bool(out["significant"] and out.get("laser_line_steady")
                                        and not out.get("width_changed")))
        if learned is not None:
            out["learned"] = learned
    return out


def _learn_first_landing(result: dict, warm: dict, laser, grating, rng: dict) -> None:
    """The first landing after a grating change against the repeats' median (Nicola, 2026-11-25: at 600, where no
    laser line can be followed, spectra right after a grating change read ~+1.4 with +-0.33 reported): the
    calibration's discarded warm-up spectrum is that landing; |its offset - the median| is learned per laser@grating
    (first_landing_cm1, median of the last 5) and added to later spectra's expected uncertainty after a change."""
    cal = (result or {}).get("calibration") or {}
    rep = cal.get("repeats") or {}
    ref = cal.get("reference_cm1")
    if not rep or rep.get("median_cm1") is None or ref is None or not warm.get("file") or not laser:
        return
    try:
        sp = spectra.read(warm["file"])
        sh, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        xs, ys = spectra.sort_xy(sh, sp["y"])
        pk = spectra.fit_peak(xs, ys, float(ref), 30.0)
    except Exception:  # noqa: BLE001 - only a learned spread
        return
    if not pk.get("ok") or pk.get("position_cm1") is None:
        return
    off = float(pk["position_cm1"]) - float(ref)
    d = off - float(rep["median_cm1"])
    placement = "labspec" if not rng.get("single", True) else "centred"
    rec_r = store.read("spectro_repeatability.json") or {}
    kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "")
    e = rec_r.setdefault(kk, {})
    hist = (list(e.get("first_landing_history") or []) + [round(abs(d), 4)])[-5:]
    e.update(first_landing_history=hist, first_landing_cm1=round(float(np.median(hist)), 4))
    store.write("spectro_repeatability.json", {k_: _clean_rep_entry(v_) for k_, v_ in rec_r.items()})
    cal["first_landing"] = {"offset_cm1": round(off, 3), "vs_repeats_median_cm1": round(d, 3),
                            "learned_cm1": e["first_landing_cm1"]}


DRIFT_VERSION = 2       # zero-drift rates learned from 2026.11.26.6 on (older ones: single fits, a cycling shutter)
DRIFT_MIN_PAIRS = 2     # a rate counts once learned from this many good pairs


def learn_zero_drift(laser, grating, placement: str, cm1: float, hours: float) -> float | None:
    """The zero's drift per hour between calibrations at one window, per laser@grating, from good pairs only (the
    caller's rules): the time-weighted rate over the last 5 pairs, sum |change| / sum hours (2026-11-26: the plain
    mean of 0.04 over 13 h and 2.08 over 0.5 h gave 1.06), applied once DRIFT_MIN_PAIRS pairs exist."""
    rec_r = store.read("spectro_repeatability.json") or {}
    kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "")
    e = rec_r.setdefault(kk, {})
    hist = list(e.get("zero_drift_history") or []) if e.get("zero_drift_v") == DRIFT_VERSION else []
    hist = (hist + [{"cm1": round(abs(float(cm1)), 4), "hours": round(float(hours), 3),
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S")}])[-5:]
    e.update(zero_drift_history=hist, zero_drift_v=DRIFT_VERSION, zero_drift_time=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if len(hist) >= DRIFT_MIN_PAIRS:
        e["zero_drift_cm1_per_h"] = round(sum(h["cm1"] for h in hist) / max(sum(h["hours"] for h in hist), 1e-6), 4)
    else:
        e.pop("zero_drift_cm1_per_h", None)
    store.write("spectro_repeatability.json", {k_: _clean_rep_entry(v_) for k_, v_ in rec_r.items()})
    return e.get("zero_drift_cm1_per_h")


def learn_window_rate(laser, grating, placement: str, centre_nm, a: float, request=None,
                      setup: dict | None = None) -> dict | None:
    """How much the zero differs between window centres, per nm, from this calibration and another one of the same
    laser@grating at least WINDOW_PAIR_MIN_NM away and within WINDOW_PAIR_MAX_H (closer pairs are mostly the
    repositioning's noise): learned per laser@grating as the median of the last 5 (window_zero_cm1_per_nm)."""
    from . import xcal
    if centre_nm is None:
        return None
    rec = xcal.load().get(xcal.key(laser, grating, setup)) or {}
    now = time.time()
    samples = []
    for c_, e_ in (rec.get("by_centre") or {}).items():
        try:
            dc = abs(float(c_) - float(centre_nm))
            age = (now - time.mktime(time.strptime(e_["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600.0
        except (KeyError, ValueError, TypeError):
            continue
        paired = (request is not None and e_.get("request") == request) or age <= WINDOW_PAIR_MAX_H
        if dc >= WINDOW_PAIR_MIN_NM and paired and e_.get("placement", "centred") == placement:
            samples.append((round(abs(float(e_["a"]) - float(a)) / dc, 4), float(c_)))
    if not samples:
        return None
    rec_r = store.read("spectro_repeatability.json") or {}
    kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "")
    e = rec_r.setdefault(kk, {})
    hist = (list(e.get("window_zero_history") or []) + [s_[0] for s_ in samples])[-5:]
    e.update(window_zero_history=hist, window_zero_cm1_per_nm=round(float(np.median(hist)), 4),
             window_zero_from=f"calibrations at different centres ({len(hist)})")
    store.write("spectro_repeatability.json", {k_: _clean_rep_entry(v_) for k_, v_ in rec_r.items()})
    return {"pairs": [{"other_centre_nm": c_, "cm1_per_nm": v} for v, c_ in samples],
            "learned_cm1_per_nm": e["window_zero_cm1_per_nm"]}


FIRST_LANDING_MIN_N = 3  # the first-landing term counts once learned from this many calibrations


def _unfollowed_term(cu_, dr: float, dpk) -> None:
    """A move seen in this study but not followed, into the expected uncertainty as its own part (unfollowed_move:
    the larger of the Raman peaks' own move and half the laser line's), and said in expected_uncertainty_from
    (T9.22: it had been filed under first_landing, and the text omitted it)."""
    if not isinstance(cu_, dict):
        return
    t_ = max(DRIFT_RAMAN_FRACTION * abs(float(dr)), float(dpk or 0.0))
    parts_ = dict(cu_.get("expected_uncertainty_parts") or {})
    parts_["unfollowed_move"] = round(t_, 3)
    cu_["expected_uncertainty_parts"] = parts_
    cu_["expected_uncertainty_cm1"] = _u_total(parts_)
    cu_["expected_uncertainty_from"] = (str(cu_.get("expected_uncertainty_from") or "")
                                        + f"; a move not followed ({t_:.2f}: the laser line {float(dr):+.2f} cm-1 "
                                        + (f"and the Raman peaks {float(dpk):.2f} " if dpk is not None else "")
                                        + "from the calibration; the larger of the peaks' move and half the line's)"
                                        ).lstrip("; ")


def _u_total(parts: dict) -> float:
    """The expected uncertainty from its independent parts (in quadrature); the learned first landing and a move seen
    in this study but not followed are the same landing: only the larger of the two counts."""
    land = max(float(parts.get("first_landing") or 0.0), float(parts.get("unfollowed_move") or 0.0))
    rest = [float(v) for k, v in parts.items() if v and k not in ("first_landing", "unfollowed_move")]
    return round(math.sqrt(sum(v ** 2 for v in rest) + land ** 2), 3)


CREEP_VERSION = 2       # zero-creep rates learned from 2026.11.25.18 on (a single series' rate is no longer applied)


def learn_zero_creep(laser, grating, placement: str, rate: float, significant: bool) -> float | None:
    """The zero's creep per laser@grating from the last 3 series: applied (zero_creep_cm1_per_min) only when 2 of
    them found a significant creep with the laser line steady, as their median; otherwise none."""
    rec_r = store.read("spectro_repeatability.json") or {}
    kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "")
    e = rec_r.setdefault(kk, {})
    hist = list(e.get("zero_creep_history") or []) if e.get("zero_creep_v") == CREEP_VERSION else []
    hist = (hist + [{"rate": round(float(rate), 4), "significant": bool(significant),
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S")}])[-3:]
    sig = [h["rate"] for h in hist if h["significant"]]
    e.update(zero_creep_history=hist, zero_creep_v=CREEP_VERSION)
    if len(sig) >= 2:
        e["zero_creep_cm1_per_min"] = round(float(np.median(sig)), 4)
    else:
        e.pop("zero_creep_cm1_per_min", None)
    store.write("spectro_repeatability.json", {k_: _clean_rep_entry(v_) for k_, v_ in rec_r.items()})
    return e.get("zero_creep_cm1_per_min")


CREEP_T = 3.0     # the laser line's slope over its standard error: a creep, not scatter


def _common_creep(lls: list, offs: list, thr: float) -> dict | None:
    """The laser line and the Raman lines drifting together across the repeats by more than the landing threshold
    (Nicola, 2026-11-27, 532 just selected: the laser line -7.78, -7.87, -7.34, -6.94, -6.84, Si +0.83 with it; every
    step under the 0.572 threshold, so nothing was flagged, SD 0.35 and 'settled'). A steady creep counts at least as
    much as one step: the laser is not settled. Fitted lines over the repeat number; None when there is no such
    creep."""
    pts = [(i, float(l_), float(o)) for i, (l_, o) in enumerate(zip(lls, offs)) if l_ is not None and o is not None]
    if len(pts) < 4:
        return None
    t_ = np.array([p[0] for p in pts], float)
    ll_ = np.array([p[1] for p in pts], float)
    pk_ = np.array([p[2] for p in pts], float)
    k_ll, c_ll = np.polyfit(t_, ll_, 1)
    k_pk, _ = np.polyfit(t_, pk_, 1)
    res_ = ll_ - (k_ll * t_ + c_ll)
    se_ = math.sqrt(float(np.sum(res_ ** 2)) / (len(pts) - 2) / float(np.sum((t_ - t_.mean()) ** 2)))
    span = float(t_[-1] - t_[0])
    d_ll, d_pk = float(k_ll) * span, float(k_pk) * span
    if abs(d_ll) <= thr or abs(k_ll) <= CREEP_T * max(se_, 1e-6):
        return None
    if d_ll * d_pk <= 0 or not 0.5 * abs(d_ll) <= abs(d_pk) <= 1.5 * abs(d_ll):
        return None          # the Raman lines did not go with it: not the laser (nor the spectrometer) moving
    return {"laser_line_cm1": round(d_ll, 3), "raman_cm1": round(d_pk, 3), "over_repeats": len(pts),
            "threshold_cm1": round(float(thr), 3), "laser_line_t": round(abs(float(k_ll)) / max(se_, 1e-6), 1)}


def _landing_steps(repeats: list, offs: list, prof: dict, laser, grating, placement: str,
                   min_thr: float = 0.0) -> dict:
    """Calibration repeats with a landing step: a laser line off the repeats' median by more than the landing
    threshold (LANDING_K x the line's own scatter) AND the Raman lines moved by the same (within max(0.3, 0.25 x
    step)): that repeat's offset is brought back by the step. Returns offs_eff (None if the laser line is not in
    enough repeats), the steps, the laser line's median and its scatter without the steps (learned)."""
    lls = []
    for r_ in repeats:
        l_ = r_.get("laser_line") or {}
        ok_ = bool(l_.get("ok")) and l_.get("position_cm1") is not None and not l_.get("suspect")
        lls.append(float(l_["position_cm1"]) if ok_ else None)
    have = [v for v in lls if v is not None]
    if len(have) < 3:
        return {"offs_eff": None, "why_not": "the laser line was not fitted in enough repeats (it must be in the "
                                             "window: about -15 to +15 cm-1)"}
    med = float(np.median(have))
    # the spread from the median absolute deviation: one step in five repeats must not raise the threshold enough
    # to hide itself (Nicola, 2026-11-25, 1200: SD 0.35 with the step in it; the step -0.59 not followed)
    sd_own = _tight_sd(have)
    thr = landing_threshold(prof, laser, grating, placement, fallback_sd=max(sd_own, 0.05))
    if min_thr and float(thr["threshold_cm1"]) < min_thr:
        thr = dict(thr, threshold_cm1=float(min_thr), broad_line_minimum=True)    # a broad line: large steps only
    t_ = float(thr["threshold_cm1"])
    ov = [o for o in offs if o is not None]
    o_med = float(np.median(ov)) if ov else 0.0
    eff, steps, quiet = list(offs), [], []
    for i, (o, l_) in enumerate(zip(offs, lls)):
        if o is None or l_ is None:
            continue
        d = l_ - med
        if abs(d) <= t_:
            quiet.append(l_)
            continue
        dpk = o - o_med
        if abs(dpk - d) <= max(0.3, 0.25 * abs(d)):
            eff[i] = o - d
            steps.append({"repeat": i + 1, "laser_line_step_cm1": round(d, 3), "raman_moved_cm1": round(dpk, 3),
                          "followed": True})
        else:
            # not a landing, and not the laser line's ordinary scatter either (a bad fit, the line moving alone):
            # left out of what is learned (2026-11-25, 600: a -1.09 jump left in made the learned value 0.65)
            steps.append({"repeat": i + 1, "laser_line_step_cm1": round(d, 3), "raman_moved_cm1": round(dpk, 3),
                          "followed": False, "why": "the Raman lines did not move with it"})
    q_sd = float(np.std(quiet, ddof=1)) if len(quiet) >= 3 else None
    creep = _common_creep(lls, offs, t_)
    if creep:
        q_sd = None             # a creeping line's scatter is not the landing scatter: nothing learned from it
    return {"offs_eff": eff, "steps": steps, "laser_line_median_cm1": round(med, 3), "threshold": thr,
            **({"creep": creep} if creep else {}),
            # the scatter of the repeats without steps (what a later threshold may learn), not the one used above
            "laser_line_sd_quiet_cm1": round(q_sd, 4) if q_sd is not None else None, "n_quiet": len(quiet),
            "ll_sd": round(q_sd, 4) if q_sd is not None else None}


def _tight_sd(v: list) -> float:
    """The scatter of the tightest ~60 % of the values (at least 3): with 5 repeats two may be off (Nicola,
    2026-11-25: laser lines -3.53, -4.86, -4.27, -4.29, -4.03: SD 0.35, 1.4826 x MAD 0.36, the tightest three
    0.14). A low estimate, used only when nothing better is known; the 0.5 cm-1 minimum and the Raman lines'
    confirmation guard the threshold."""
    a = np.sort(np.asarray(v, float))
    m = max(3, int(math.ceil(0.6 * a.size)))
    if a.size < m:
        return float(np.std(a, ddof=1)) if a.size >= 2 else 0.0
    best = min(range(a.size - m + 1), key=lambda i: a[i + m - 1] - a[i])
    return float(np.std(a[best:best + m], ddof=1))


def learn_laser_line_sd(laser, grating, placement: str, value: float, source: str, keep: int = 5) -> float:
    """The laser line's own scatter, learned per laser@grating (LL_SD_VERSION; a history from before is dropped):
    from series (the spectrometer still: its jitter alone) and from calibrations whose threshold did not come from
    their own repeats."""
    rec_r = store.read("spectro_repeatability.json") or {}
    kk = f"{float(laser):g}@{_f(grating) or 0:g}" + ("@labspec" if placement == "labspec" else "")
    e = rec_r.setdefault(kk, {})
    hist = list(e.get("laser_line_sd_cm1_history") or []) if e.get("laser_line_sd_v") == LL_SD_VERSION else []
    hist = (hist + [round(float(value), 4)])[-keep:]
    e.update(laser_line_sd_cm1_history=hist, laser_line_sd_cm1=round(float(np.median(hist)), 4),
             laser_line_sd_v=LL_SD_VERSION, laser_line_sd_from=source,
             laser_line_sd_time=time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.write("spectro_repeatability.json", {k_: _clean_rep_entry(v_) for k_, v_ in rec_r.items()})
    return e["laser_line_sd_cm1"]


def _robust_sd(v: list) -> float:
    """The spread of repeats with one outlier allowed for: the SD of all but the value farthest from the median."""
    a = np.asarray(v, float)
    if a.size >= 4:
        a = np.delete(a, int(np.argmax(np.abs(a - np.median(a)))))
    return float(np.std(a, ddof=1)) if a.size >= 2 else 0.0



LASER_LINE_MAX_WIDTH = 2.0   # a calibration's laser line wider than this x its reference line: not a reference


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
