"""render.py — PNG pictures of a study's results (matplotlib, optional extra 'render').
Version: 2026.11.19.2

spectrum / calibration / series: the spectrum (or the first and last of a series) with its baseline and fitted
peaks; map: one image per peak quantity and band (position, FWHM, height or area), positions in µm offsets.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import spectra


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def spectrum_png(path: Path, files: list, process: dict, laser: float | None, title: str,
                 peaks: list | None = None) -> dict:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=110)
    for f in files:
        sp = spectra.read(f)
        x, unit = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        x, y = spectra.sort_xy(x, sp["y"])
        ax.plot(x, y, lw=0.9, label=Path(f).stem)
        if process.get("baseline", {"degree": 3}) not in (False, None) and len(files) == 1:
            deg = int((process.get("baseline") or {}).get("degree", 3))
            ax.plot(x, spectra.baseline(x, y, deg), lw=0.8, ls="--", color="0.5", label="baseline")
    for pk in peaks or []:
        if pk.get("ok"):
            ax.axvline(pk["position_cm1"], color="C3", lw=0.6, alpha=0.6)
            ax.annotate(f"{pk.get('name') or ''} {pk['position_cm1']:.1f}", (pk["position_cm1"], 1.0),
                        xycoords=("data", "axes fraction"), rotation=90, va="top", ha="right", fontsize=7,
                        color="C3")
    ax.set_xlabel("Raman shift (cm$^{-1}$)")
    ax.set_ylabel("counts")
    ax.set_title(title, fontsize=10)
    if len(files) > 1 or process.get("baseline", {"degree": 3}) not in (False, None):
        ax.legend(fontsize=7, loc="upper right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": str(path), "what": "spectrum with baseline and fitted peaks"}


def map_png(path: Path, grid: np.ndarray, xs, ys, title: str, unit: str) -> dict:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(5.2, 4.4), dpi=110)
    g = np.asarray(grid, float)
    dx = (xs[1] - xs[0]) if len(xs) > 1 else 1.0
    dy = (ys[1] - ys[0]) if len(ys) > 1 else 1.0
    ext = [xs[0] - dx / 2, xs[-1] + dx / 2, ys[0] - dy / 2, ys[-1] + dy / 2]
    im = ax.imshow(np.ma.masked_invalid(g), origin="lower", extent=ext, aspect="equal", cmap="viridis")
    fig.colorbar(im, ax=ax, label=unit)
    ax.set_xlabel("x offset (µm)")
    ax.set_ylabel("y offset (µm)")
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    fin = g[np.isfinite(g)]
    return {"path": str(path), "what": title, "min": float(fin.min()) if fin.size else None,
            "max": float(fin.max()) if fin.size else None}


UNITS = {"position_cm1": "cm-1", "fwhm_cm1": "cm-1", "height": "counts", "area": "counts·cm-1",
         "snr": "", "value": "counts"}


def study_images(name: str, study: dict, result: dict, sd: Path) -> dict:
    img = sd / "images"
    img.mkdir(exist_ok=True)
    process = study.get("process", {})
    laser = result.get("instrument", {}).get("laser_nm")
    out: dict = {}
    kind = study.get("kind", "spectrum")
    if kind == "map":
        import json
        mj = json.loads(Path(result["map"]["files"]["json"]).read_text(encoding="utf-8"))
        xs, ys = mj["x_um"], mj["y_um"]
        for key, grid in mj["grids"].items():
            qn, f = key.rsplit(".", 1)
            if f not in ("position_cm1", "fwhm_cm1", "height", "area", "value"):
                continue
            g = np.array([[np.nan if v is None else v for v in row] for row in grid[0]], float)
            if not np.isfinite(g).any():
                continue
            if len(xs) < 2 and len(ys) < 2:
                continue
            out[f"{qn}_{f}"] = map_png(img / f"{qn}_{f}.png", g, xs, ys, f"{name}: {qn} {f}", UNITS.get(f, ""))
        return out
    files = [s["file"] for s in result.get("spectra", []) if s.get("file")]
    if not files:
        return out
    if kind == "series" and len(files) > 1:
        out["spectra"] = spectrum_png(img / "spectra.png", [files[0], files[-1]], process, laser,
                                      f"{name}: first and last of {len(files)}")
    else:
        for i, f in enumerate(files[:6]):
            pk = result["spectra"][i].get("peaks")
            out[f"spectrum_{i}"] = spectrum_png(img / f"spectrum_{i}.png", [f], process, laser,
                                                f"{name}: point {i}", pk)
    return out


def is_finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False
