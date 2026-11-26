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
    if rec.get("pooled") and placement != "labspec":
        return {k_: v for k_, v in rec.items() if k_ not in ("by_centre", "by_range")}
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
          pool: str = "per_centre", repeats: dict | None = None, range_cm1=None,
          cm1_per_px: float | None = None, zero_mode: str | None = None) -> dict:
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
    if zero_mode:
        # 'anchored': measured relative to the laser line (laser_line_cm1); 'absolute': the line alone. Both from the
        # spectra as measured (any laser-line shift of the calibration study undone: Nicola, 2026-11-25)
        rec["zero_mode"] = zero_mode
    if laser_line_cm1 is not None:
        # where the laser line sat when this was measured: a later spectrum is brought back to it before the
        # correction applies (laser_reference 'correct'), so the laser's move is not counted twice
        rec["laser_line_cm1"] = round(float(laser_line_cm1), 3)
        if laser_line_fwhm_cm1:
            rec["laser_line_fwhm_cm1"] = round(float(laser_line_fwhm_cm1), 3)
    br = dict((d.get(k) or {}).get("by_range") or {})
    if centre_nm is None and range_cm1 is not None:
        rec["range_cm1"] = sorted(float(v) for v in range_cm1)
        br[range_key(range_cm1)] = rec
    by = dict((d.get(k) or {}).get("by_centre") or {})
    drift = None
    if centre_nm is not None:
        ck = f"{float(centre_nm):.2f}"
        old = by.get(ck)
        orp = (old or {}).get("repeats") or {}
        if orp and ("median_cm1" not in orp or orp.get("settled") is False):
            old = None              # repeats from before 2026.11.25.1 or not settled: no drift against them
        if old and old.get("request") != request and _t(rec) - _t(old) > POOL_WINDOW_H * 3600:
            # the same window measured again later: the drift since then, which older entries elsewhere still carry
            drift = {"centre_nm": float(centre_nm), "since": old.get("time"),
                     "cm1": round(float(rec["a"]) - float(old["a"]), 3)}
        by[ck] = rec
    if centre_nm is None and range_cm1 is not None:
        # an extended range: kept for that range, nothing to compare per window
        rec["pool"] = {"pooled": False, "why": f"kept for the range {range_key(range_cm1)} cm-1"}
        d[k] = dict((d.get(k) or {}), by_range=br)
        if not d[k].get("a"):
            d[k].update({kk: vv for kk, vv in rec.items() if kk not in ("pool",)})
        _save(d)
        return dict(rec)
    pooled, why, cents = pool_decision(by, pool, cm1_per_px, rec)
    rec["pool"] = {"pooled": pooled, "why": why}
    _fr, older = fresh_entries(by, rec)
    top = dict(rec)
    if drift:
        top["drift_at_centre"] = drift
    if older:
        top["older_centres"] = [{"centre_nm": float(c), "time": v.get("time"),
                                 **({"why": v["not_compared"]} if v.get("not_compared") else {})} for c, v in older]
    if pooled:
        # one correction for every window: the mean of the compared centres (all entries kept, so later evidence
        # can undo it)
        ents = [by[c] for c in cents] or [rec]
        top.update(a=round(float(sum(e["a"] for e in ents) / len(ents)), 5),
                   b=round(float(sum(e.get("b", 1.0) for e in ents) / len(ents)), 7), pooled=True,
                   pooled_from=sorted(float(c) for c in cents))
        top.pop("centre_nm", None)
    d[k] = dict(top, **({"by_centre": by} if by else {}), **({"by_range": br} if br else {}))
    _save(d)
    return dict(top, pool=rec["pool"])


POOL_SE = 2.0               # two centre means differ when |a1 - a2| > this x sqrt(SE1^2 + SE2^2)
POOL_PX_DEFAULT = 2.0       # without repeat data: pooled at this many cm-1 per pixel or coarser ...
POOL_NO_REPEAT_SPREAD = 1.0  # ... unless the centres already differ by more than this (cm-1)
POOL_WINDOW_H = 1.0         # entries compared and pooled: the same request, or within this many hours


def _t(rec) -> float:
    try:
        return time.mktime(time.strptime(str(rec.get("time")), "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def _se(rec) -> float | None:
    r = rec.get("repeats") or {}
    if r.get("drifting") and not r.get("settled"):
        return None                           # a trend over the repeats: their scatter is not noise
    if r.get("sem_cm1") is not None:
        return float(r["sem_cm1"])
    if r.get("sd_cm1") is not None and r.get("n"):
        return float(r["sd_cm1"]) / max(int(r["n"]), 1) ** 0.5
    return None


def fresh_entries(by: dict, newest: dict, window_h: float = POOL_WINDOW_H) -> tuple[list, list]:
    """The per-centre entries comparable with the newest one: the same request, or measured within window_h of it
    (2026-11-24: 637.75 @ 600 at 659.66 nm read -12.11 in the morning and -9.2 ... -9.9 in the evening: a drift
    larger than the window effect). Returns (fresh, older) as (centre, entry) pairs; LabSpec-placed ranges left out."""
    t0, req = _t(newest), newest.get("request")
    fresh, older = [], []
    for c, v in by.items():
        if v.get("placement") == "labspec":
            continue
        rp = v.get("repeats") or {}
        if v is not newest and rp and ("median_cm1" not in rp or rp.get("settled") is False):
            # repeats judged before 2026.11.25.1 (a step read as a settled drift), or not settled: never compared
            # (2026-11-25: every 600 calibration found a 'real window dependence' against such an entry)
            older.append((c, dict(v, not_compared="repeats from before 2026.11.25.1 or not settled")))
            continue
        same = req is not None and v.get("request") == req
        (fresh if same or abs(t0 - _t(v)) <= window_h * 3600 else older).append((c, v))
    return fresh, older


def pool_decision(by: dict, pool: str, cm1_per_px: float | None, newest: dict | None = None) -> tuple[bool, str, list]:
    """One correction for every window, or one per window centre? 'pooled' / 'per_centre' as asked. 'auto' (Nicola,
    2026-11-24): only entries from the same request or within POOL_WINDOW_H are compared (older ones would bring the
    drift in); with repeat data, pooled when every two centre means agree within POOL_SE x sqrt(SE1^2 + SE2^2)
    (repeats 5, SD 0.5: ~0.63 cm-1, so a real 0.8 cm-1 window difference stays per centre; 532 @ 1200's +4.05 / +2.18
    / +1.80 too); the SE test needs repeat data at EVERY compared centre (never on one repeated centre's SE alone);
    otherwise pooled only at >= POOL_PX_DEFAULT cm-1 per pixel with the centres within POOL_NO_REPEAT_SPREAD.
    Returns (pooled, why, the centres pooled)."""
    if pool == "pooled":
        return True, "calibration.store 'pooled'", [c for c, v in by.items() if v.get("placement") != "labspec"]
    if pool != "auto":
        return False, "per window centre", []
    fresh, older = fresh_entries(by, newest or {})
    n_old = sum(1 for _, v in older if not v.get("not_compared"))
    n_bad = len(older) - n_old
    note = ((f"; {n_old} older centre(s) not compared (measured more than {POOL_WINDOW_H:g} h apart: recalibrate "
             "them in the same request)" if n_old else "")
            + (f"; {n_bad} centre(s) not compared (repeats from before 2026.11.25.1 or not settled)" if n_bad else ""))
    if len(fresh) < 2:
        return False, "one window measured in this session: per window centre" + note, []
    ses = [_se(v) for _, v in fresh]
    if all(se is not None for se in ses):
        worst = None
        for i in range(len(fresh)):
            for j in range(i + 1, len(fresh)):
                d = abs(float(fresh[i][1]["a"]) - float(fresh[j][1]["a"]))
                lim = POOL_SE * (ses[i] ** 2 + ses[j] ** 2) ** 0.5
                if worst is None or d / max(lim, 1e-9) > worst[0]:
                    worst = (d / max(lim, 1e-9), d, lim, fresh[i][0], fresh[j][0])
        if worst[0] <= 1.0:
            return True, (f"the {len(fresh)} centres agree within {POOL_SE:g} x their standard errors (largest "
                          f"difference {worst[1]:.2f} cm-1, allowed {worst[2]:.2f}): one correction (their mean)"
                          + note), [c for c, _ in fresh]
        return False, (f"centres {worst[3]} and {worst[4]} nm differ by {worst[1]:.2f} cm-1, more than {POOL_SE:g} x "
                       f"their standard errors ({worst[2]:.2f}): a real window dependence, kept per centre" + note), []
    a_s = [float(v["a"]) for _, v in fresh]
    spread = max(a_s) - min(a_s)
    if cm1_per_px is not None and cm1_per_px >= POOL_PX_DEFAULT and spread <= POOL_NO_REPEAT_SPREAD:
        return True, (f"no repeat data for every centre; {cm1_per_px:.1f} cm-1 per pixel and the centres within "
                      f"{POOL_NO_REPEAT_SPREAD:g} cm-1 ({spread:.2f}): one correction (their mean); "
                      "calibration.repeats would tell" + note), [c for c, _ in fresh]
    return False, (f"no repeat data for every centre; the centres differ by {spread:.2f} cm-1: per window centre "
                   "(calibration.repeats would tell whether that is noise)" + note), []


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


def at_centre(laser_nm, grating, centre_nm, placement: str | None = None, range_cm1=None) -> dict | None:
    """The correction measured at this window itself (or this range), never pooled or interpolated: what a new
    calibration there is compared with (2026-11-26: at 1200 the pooled correction hid three calibrations at
    659.66 nm from the drift and jump checks)."""
    rec = load().get(key(laser_nm, grating))
    if not rec:
        return None
    if range_cm1 is not None:
        r_ = (rec.get("by_range") or {}).get(range_key(range_cm1))
        return dict(r_, range_match=True) if r_ else None
    if centre_nm is None:
        return None
    by = {k: v for k, v in (rec.get("by_centre") or {}).items() if _usable(v, placement)}
    near = min(((abs(float(k) - float(centre_nm)), k) for k in by), default=None)
    if near is None or near[0] > CENTRE_TOL_NM:
        return None
    return dict(by[near[1]], centre_nm=float(near[1]), centre_distance_nm=round(near[0], 3))


def age_min(rec: dict) -> float | None:
    """The correction's age in minutes (unrounded: the zero creeps by the minute, 2026-11-25)."""
    try:
        return (time.time() - time.mktime(time.strptime(rec["time"], "%Y-%m-%dT%H:%M:%S"))) / 60.0
    except (KeyError, ValueError):
        return None


def age_h(rec: dict) -> float | None:
    try:
        return round((time.time() - time.mktime(time.strptime(rec["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600, 1)
    except (KeyError, ValueError):
        return None
