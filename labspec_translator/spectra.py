"""spectra.py — reading the bridge's spectrum files, and the analysis done here rather than in LabSpec.
Version: 2026.11.20.44

Raman shift from wavelength with the laser line read back from the instrument; an iterative polynomial baseline;
noise from second differences; spikes; peak fits (pseudo-Voigt, Lorentz, Gauss with a local linear background);
band integrals. The analysis is ours and deterministic, so the same file always gives the same numbers.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .profile import nm_to_shift

SQRT_LN2 = math.sqrt(math.log(2.0))


# --------------------------------------------------------------------------------------------- files

def read(path: Path | str) -> dict:
    """A bridge spectrum file: '# key=value' header lines, then 'x<TAB>y' rows. Returns {x, y, meta}."""
    meta: dict = {}
    xs, ys = [], []
    from .profile import read_text
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            if "=" in body:
                k, v = body.split("=", 1)
                meta[k.strip()] = v.strip()
            continue
        parts = line.replace(",", ".").split("\t") if "\t" in line else line.split()
        if len(parts) < 2:
            continue
        try:
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
        except ValueError:
            continue
    return {"x": np.asarray(xs, float), "y": np.asarray(ys, float), "meta": meta}


def write(path: Path | str, x, y, meta: dict | None = None):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for k, v in (meta or {}).items():
            fh.write(f"# {k}={v}\n")
        for a, b in zip(x, y):
            fh.write(f"{a:.6f}\t{b:.4f}\n")
    tmp.replace(p)


def to_shift(x, unit: str, laser_nm: float | None) -> tuple[np.ndarray, str]:
    """The x axis as Raman shift (cm-1). LabSpec gives nm, 1/cm (already a shift) or eV."""
    u = (unit or "").strip().lower().replace("cm-1", "1/cm").replace("cm^-1", "1/cm")
    x = np.asarray(x, float)
    if u in ("1/cm", "rel 1/cm", "raman shift"):
        return x, "1/cm"
    if u == "nm":
        if not laser_nm:
            raise ValueError("the spectrum is in nm and the laser line is unknown")
        return nm_to_shift(x, laser_nm), "nm"
    if u == "ev":
        if not laser_nm:
            raise ValueError("the spectrum is in eV and the laser line is unknown")
        return nm_to_shift(1239.841984 / x, laser_nm), "eV"
    raise ValueError(f"unknown x unit {unit!r}")


def sort_xy(x, y):
    o = np.argsort(x)
    return np.asarray(x)[o], np.asarray(y)[o]


# --------------------------------------------------------------------------------------------- statistics

def noise_sigma(y) -> float:
    """White-noise sigma from second differences (robust: median absolute deviation); peaks barely affect it."""
    y = np.asarray(y, float)
    if y.size < 5:
        return float("nan")
    d2 = np.diff(y, 2)
    mad = np.median(np.abs(d2 - np.median(d2)))
    return float(1.4826 * mad / math.sqrt(6.0))


def spikes(y, sigma: float | None = None, k: float = 12.0) -> list[int]:
    """Indices of single- or two-pixel spikes (cosmic rays) standing k sigma above a 5-point median."""
    y = np.asarray(y, float)
    if y.size < 7:
        return []
    s = sigma if sigma is not None else noise_sigma(y)
    if not s or not math.isfinite(s):
        return []
    pad = np.pad(y, 2, mode="edge")
    med = np.median(np.lib.stride_tricks.sliding_window_view(pad, 5), axis=1)
    z = (y - med) / s
    hits = np.where(z > k)[0]
    out = []
    for i in hits:
        left = y[i - 2] if i >= 2 else y[0]
        right = y[i + 2] if i + 2 < y.size else y[-1]
        if y[i] - max(left, right) > k * s:   # narrow: falls back within two pixels on both sides
            out.append(int(i))
    return out


def saturation(y, full_scale: float, frac: float = 0.98) -> dict:
    """Pixels at or near the detector's full scale (per accumulation, as LabSpec averages accumulations)."""
    y = np.asarray(y, float)
    thr = frac * full_scale
    n = int(np.sum(y >= thr))
    flat = 0
    if y.size:
        top = y.max()
        flat = int(np.sum(np.isclose(y, top, rtol=0, atol=1e-6))) if top >= thr else 0
    return {"saturated": n > 0, "pixels": n, "max_counts": float(y.max()) if y.size else None,
            "threshold": thr, "flat_top_pixels": flat}


# --------------------------------------------------------------------------------------------- baseline

def baseline(x, y, degree: int = 3, iterations: int = 100, tol: float = 1e-4) -> np.ndarray:
    """Iterative polynomial baseline (modified polyfit): fit, clip the data to the fit, repeat until it settles.
    Peaks are left above it; broad fluorescence is removed."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if x.size < degree + 2:
        return np.zeros_like(y)
    u = (2 * x - (x.max() + x.min())) / max(x.max() - x.min(), 1e-12)
    work = y.copy()
    fit = np.zeros_like(y)
    for _ in range(iterations):
        c = np.polynomial.polynomial.polyfit(u, work, degree)
        fit = np.polynomial.polynomial.polyval(u, c)
        new = np.minimum(work, fit)
        if np.linalg.norm(new - work) <= tol * max(np.linalg.norm(work), 1e-12):
            work = new
            break
        work = new
    return fit


# --------------------------------------------------------------------------------------------- peaks

def _gauss(x, x0, w):
    return np.exp(-4.0 * math.log(2.0) * ((x - x0) / w) ** 2)


def _lorentz(x, x0, w):
    return 1.0 / (1.0 + 4.0 * ((x - x0) / w) ** 2)


def profile_fn(shape: str):
    if shape == "gauss":
        return lambda x, h, x0, w, c0, c1: h * _gauss(x, x0, w) + c0 + c1 * (x - x0)
    if shape == "lorentz":
        return lambda x, h, x0, w, c0, c1: h * _lorentz(x, x0, w) + c0 + c1 * (x - x0)
    return lambda x, h, x0, w, eta, c0, c1: h * (eta * _lorentz(x, x0, w) + (1 - eta) * _gauss(x, x0, w)) \
        + c0 + c1 * (x - x0)


def area(h: float, w: float, shape: str, eta: float = 0.5) -> float:
    lor = h * w * math.pi / 2.0
    gau = h * w * math.sqrt(math.pi) / (2.0 * SQRT_LN2)
    if shape == "gauss":
        return gau
    if shape == "lorentz":
        return lor
    return eta * lor + (1 - eta) * gau


def fit_peak(x, y, near: float, window: float = 40.0, shape: str = "pseudo_voigt", sigma: float | None = None,
             name: str | None = None, detect_snr: float = 5.0) -> dict:
    """Fits one peak near `near` (cm-1) within ±window/2 of baseline-corrected data. Returns position_cm1,
    fwhm_cm1, height, area, eta (pseudo-Voigt), stderr_position_cm1, r2, snr (height / noise), ok. A fit whose
    height is below detect_snr x noise is 'not detected' (ok false): the largest noise excursion in a window of a
    few dozen points reaches about 3-4 sigma."""
    from scipy.optimize import curve_fit
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    out: dict = {"name": name, "near_cm1": near, "window_cm1": window, "shape": shape, "ok": False}
    sel = (x >= near - window / 2) & (x <= near + window / 2)
    if sel.sum() < 7:
        out["why"] = f"fewer than 7 points between {near - window / 2:g} and {near + window / 2:g} cm-1"
        return out
    xs, ys = x[sel], y[sel]
    dx = float(np.median(np.abs(np.diff(xs)))) or 1.0
    i0 = int(np.argmax(ys))
    h0 = float(ys[i0] - np.median(ys))
    if h0 <= 0:
        out["why"] = "no peak above the local background"
        return out
    s_pre = sigma if sigma is not None else noise_sigma(y)
    if s_pre and math.isfinite(s_pre) and h0 / s_pre < detect_snr:
        # nothing stands out of the noise: not worth a fit (which would chase the noise)
        out.update(detected=False, snr=round(h0 / s_pre, 2), why=f"not detected (SNR {h0 / s_pre:.2g} < {detect_snr:g})")
        return out
    x0 = float(xs[i0])
    half = ys >= ys[i0] - h0 / 2
    w0 = max(2 * dx, float(np.sum(half)) * dx)
    c0 = float(np.median(ys))
    f = profile_fn(shape)
    if shape == "pseudo_voigt":
        p0 = [h0, x0, w0, 0.5, c0, 0.0]
        lb = [0, xs.min(), dx, 0.0, -np.inf, -np.inf]
        ub = [np.inf, xs.max(), window, 1.0, np.inf, np.inf]
    else:
        p0 = [h0, x0, w0, c0, 0.0]
        lb = [0, xs.min(), dx, -np.inf, -np.inf]
        ub = [np.inf, xs.max(), window, np.inf, np.inf]
    try:
        p, cov = curve_fit(f, xs, ys, p0=p0, bounds=(lb, ub), maxfev=3000)
    except Exception as exc:  # noqa: BLE001
        out["why"] = f"fit did not converge: {exc}"[:300]
        return out
    model = f(xs, *p)
    ss_res = float(np.sum((ys - model) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2)) or 1e-30
    perr = np.sqrt(np.clip(np.diag(cov), 0, np.inf)) if cov is not None and np.all(np.isfinite(cov)) \
        else np.full(len(p), np.nan)
    h, pos, w = float(p[0]), float(p[1]), float(p[2])
    eta = float(p[3]) if shape == "pseudo_voigt" else (1.0 if shape == "lorentz" else 0.0)
    s = sigma if sigma is not None else noise_sigma(y)
    edge = min(pos - xs.min(), xs.max() - pos) < max(w / 2, 2 * dx)
    out.update(ok=True, position_cm1=round(pos, 4), fwhm_cm1=round(w, 4), height=round(h, 3),
               area=round(area(h, w, shape, eta), 3), eta=round(eta, 4),
               stderr_position_cm1=round(float(perr[1]), 4) if math.isfinite(perr[1]) else None,
               r2=round(1 - ss_res / ss_tot, 5), snr=round(h / s, 2) if s and math.isfinite(s) else None)
    if out["snr"] is not None and out["snr"] < detect_snr:
        return {"name": name, "near_cm1": near, "window_cm1": window, "shape": shape, "ok": False,
                "detected": False, "snr": out["snr"], "why": f"not detected (SNR {out['snr']:g} < {detect_snr:g})"}
    out["detected"] = True
    if edge:
        out["warning"] = "the fitted peak sits at the edge of its window: widen window_cm1 or move near_cm1"
    return out


def find_peaks(x, y, n: int = 3, sigma: float | None = None, min_snr: float = 5.0) -> list[float]:
    """Positions (cm-1) of the n most prominent peaks above min_snr x noise."""
    from scipy.signal import find_peaks as fp
    y = np.asarray(y, float)
    s = sigma if sigma is not None else noise_sigma(y)
    if not s or not math.isfinite(s):
        s = max(float(np.std(y)) * 0.01, 1e-12)
    idx, props = fp(y, prominence=min_snr * s, distance=3)
    if not len(idx):
        return []
    order = np.argsort(props["prominences"])[::-1][:n]
    return sorted(float(np.asarray(x)[idx[i]]) for i in order)


def line_signal(x, y, line: float, window: float) -> float:
    """A line's height above its window's edges (the mean of the outer sixth on each side), after a 3-point median
    so a cosmic ray (one or two pixels) does not count: the same measure the bridge's guided loop uses. x in cm-1,
    ascending."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    sel = (x >= line - window / 2) & (x <= line + window / 2)
    ys = y[sel]
    if ys.size < 5:
        return 0.0
    med = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(ys, 1, mode="edge"), 3), axis=1)
    w = max(1, ys.size // 6)
    edge = float(np.mean(np.concatenate([med[:w], med[-w:]])))
    return float(med.max() - edge)


def band(x, y, lo: float, hi: float) -> float | None:
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    lo, hi = sorted((lo, hi))
    sel = (x >= lo) & (x <= hi)
    if sel.sum() < 2:
        return None
    return float(np.trapezoid(y[sel], x[sel])) if hasattr(np, "trapezoid") else float(np.trapz(y[sel], x[sel]))


# --------------------------------------------------------------------------------------------- one spectrum

def analyze(shift, y, process: dict, min_snr: float = 10.0, detect_snr: float = 5.0) -> dict:
    """Baseline, noise, spikes, peaks and bands of one spectrum (x in cm-1, ascending)."""
    process = process or {}
    x, y = sort_xy(shift, y)
    sigma = noise_sigma(y)
    bl_spec = process.get("baseline", {"degree": 3})
    if bl_spec in (False, None):
        base = np.zeros_like(y)
        deg = None
    else:
        deg = int((bl_spec or {}).get("degree", 3))
        base = baseline(x, y, deg)
    yc = y - base
    out: dict = {"points": int(x.size), "from_cm1": round(float(x.min()), 3) if x.size else None,
                 "to_cm1": round(float(x.max()), 3) if x.size else None,
                 "noise_sigma": round(sigma, 4) if math.isfinite(sigma) else None,
                 "baseline_degree": deg, "spikes": spikes(y, sigma)}
    specs = process.get("peaks")
    auto = False
    if not specs:
        auto = True
        specs = [{"near_cm1": p} for p in find_peaks(x, yc, int(process.get("auto_peaks", 3)), sigma)]
    peaks = []
    for i, ps in enumerate(specs):
        r = fit_peak(x, yc, float(ps["near_cm1"]), float(ps.get("window_cm1", 40.0)),
                     ps.get("shape", "pseudo_voigt"), sigma, ps.get("name") or f"peak{i}", detect_snr)
        if r.get("ok") and r.get("snr") is not None and r["snr"] < min_snr:
            r["warning"] = (r.get("warning", "") + "; " if r.get("warning") else "") + \
                f"weak: SNR {r['snr']:g} < {min_snr:g} (longer exposure or more accumulations)"
        peaks.append(r)
    out["peaks"] = peaks
    out["auto_peaks"] = auto
    # a narrow Raman line looks like a spike in one spectrum: spikes inside a fitted peak do not count
    keep = []
    for i in out["spikes"]:
        xi = float(x[i])
        if not any(pk.get("ok") and abs(xi - pk["position_cm1"]) <= 3 * max(pk["fwhm_cm1"], 1.0) for pk in peaks):
            keep.append(i)
    out["spikes"] = keep
    out["bands"] = {b.get("name", f"band{i}"): band(x, yc, float(b["from_cm1"]), float(b["to_cm1"]))
                    for i, b in enumerate(process.get("bands", []) or [])}
    out["_baseline"] = base
    out["_x"] = x
    out["_y"] = y
    return out
