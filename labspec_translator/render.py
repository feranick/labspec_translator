"""render.py — PNG pictures of a study's results (matplotlib, optional extra 'render').
Version: 2026.11.20.20

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


def spot_png(path: Path, rs: dict, feature, prior) -> dict:
    """A Raman spot scan: the line's signal against the stage position relative to where the beam was expected to
    hit the feature; the cross is where it did."""
    plt = _plt()
    g = rs["grid_um"]
    V = np.array(rs["signal"], float)
    fig, ax = plt.subplots(figsize=(5.2, 4.4), dpi=110)
    st = (g[1] - g[0]) if len(g) > 1 else 1.0
    im = ax.imshow(V, origin="lower", extent=[g[0] - st / 2, g[-1] + st / 2, g[0] - st / 2, g[-1] + st / 2],
                   cmap="viridis", aspect="equal")
    fig.colorbar(im, ax=ax, label=f"{rs['line_cm1']:g} cm$^{{-1}}$ signal (counts/s)")
    ax.plot([0], [0], "w+", ms=10, label="expected (prior offset)")
    if rs.get("stage_at_hit_um"):
        hx = rs["stage_at_hit_um"][0] - (feature[0] - prior[0])
        hy = rs["stage_at_hit_um"][1] - (feature[1] - prior[1])
        ax.plot([hx], [hy], "rx", ms=10, mew=2, label="beam on the feature")
    ax.set_xlabel("stage X relative (µm)")
    ax.set_ylabel("stage Y relative (µm)")
    ax.set_title(("found: offset " + str(rs.get("offset_um"))) if rs.get("found") else
                 f"not found: {rs.get('why', '')}"[:80], fontsize=9)
    ax.legend(fontsize=7, loc="upper right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": str(path), "what": "the Raman spot scan: signal against the stage position"}


def depth_png(path: Path, dp: dict, name: str) -> dict:
    """A depth profile: each peak's height (and each band's area), normalised, against depth; interfaces marked."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 4), dpi=110)
    d = dp["depth_um"]
    for key, vals in dp["profiles"].items():
        if not key.endswith(".height") and not (key.endswith(".area") and not any(
                k.startswith(key.rsplit(".", 1)[0] + ".height") for k in dp["profiles"])):
            continue
        v = np.array([np.nan if a is None else a for a in vals], float)
        if np.nanmax(v) > 0:
            ax.plot(d, v / np.nanmax(v), marker="o", ms=3, label=key)
    for it in [i for i in dp.get("interfaces", []) if i["quantity"].endswith(".height")]:
        ax.axvline(it["depth_um"], color="0.5", ls="--", lw=0.8)
        ax.annotate(f"{it['quantity'].split('.')[0]} {it['direction']} {it['depth_um']:.1f}", (it["depth_um"], 1.02),
                    rotation=90, fontsize=7, va="bottom", ha="center")
    ax.set_xlabel("depth (µm)" + (f", n = {dp['refractive_index']}" if dp.get("refractive_index")
                                  else " (stage displacement)"))
    ax.set_ylabel("normalised signal")
    ax.set_ylim(-0.05, 1.25)
    ax.set_title(f"{name}: depth profile", fontsize=9)
    ax.legend(fontsize=7, loc="lower right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": str(path), "what": "depth profile: normalised peak heights against depth; interfaces dashed"}


def focus_png(path: Path, fs: dict) -> dict:
    """A focus scan: the line's signal against Z, with the focus chosen."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6, 3.6), dpi=110)
    z = [zz - fs["z0_um"] for zz in fs["z_steps_um"]]
    ax.plot(z, fs["signal"], marker="o", ms=4)
    if fs.get("found"):
        ax.axvline(fs["z_um"] - fs["z0_um"], color="C3", lw=1, label=f"focus: {fs['shift_um']:+.2f} µm")
        ax.legend(fontsize=8)
    ax.set_xlabel("Z offset from the starting focus (µm)")
    ax.set_ylabel(f"{fs['line_cm1']:g} cm$^{{-1}}$ signal (counts/s)")
    ax.set_title("focus scan" + ("" if fs.get("found") else f": no clear best ({fs.get('why')})")[:90], fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": str(path), "what": "the line's signal against Z; the focus chosen"}


def cutoff_png(path: Path, x, y, res: dict) -> dict:
    """The spectrum across the laser line with the edge filter's 10, 50 and 90 % points."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6.5, 3.6), dpi=110)
    ax.plot(x, y, lw=1)
    for k, lab, c in (("edge_10_cm1", "10 %", "C1"), ("edge_50_cm1", "50 %", "C2"),
                      ("usable_from_cm1", "90 %: usable from", "C3")):
        if res.get(k) is not None:
            ax.axvline(res[k], color=c, lw=1, ls="--", label=f"{lab} {res[k]:g} cm$^{{-1}}$")
    ax.set_xlabel("Raman shift (cm$^{-1}$)")
    ax.set_ylabel("counts")
    ax.set_title(f"edge filter cut-off ({res.get('light')} light)", fontsize=9)
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": str(path), "what": "the spectrum across the laser line; the edge filter's transmission points"}


def guide_png(path: Path, hist: list, guide: dict, name: str) -> dict:
    """The signal over time during a guided adjustment, with the goal and where STOP was given."""
    plt = _plt()
    t = [h[0] for h in hist]
    v = [h[1] for h in hist]
    fig, ax = plt.subplots(figsize=(7, 3.8), dpi=110)
    ax.plot(t, v, marker="o", ms=3, lw=1)
    if guide.get("target") and guide.get("mode") in ("reach", "drop"):
        ax.axhline(float(guide["target"]), color="C2", ls="--", lw=1, label="goal")
    if guide.get("t_max_s") is not None and guide.get("max_value") is not None:
        ax.plot([guide["t_max_s"]], [guide["max_value"]], "*", color="C3", ms=10, label="best")
    if t:
        ax.axvline(t[-1], color="C3", lw=0.8, alpha=0.7, label=f"STOP ({guide.get('reason')})")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("line signal (counts/s)")
    ax.set_title(f"{name}: {guide.get('instruction', '')}"[:110], fontsize=9)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": str(path), "what": "the line's signal during the guided adjustment"}


UNITS = {"position_cm1": "cm-1", "fwhm_cm1": "cm-1", "height": "counts", "area": "counts·cm-1",
         "snr": "", "value": "counts"}


def study_images(name: str, study: dict, result: dict, sd: Path) -> dict:
    img = sd / "images"
    img.mkdir(exist_ok=True)
    process = study.get("process", {})
    laser = result.get("instrument", {}).get("laser_nm")
    out: dict = {}
    kind = study.get("kind", "spectrum")
    if kind == "map" and result.get("depth"):
        out["depth"] = depth_png(img / "depth.png", result["depth"], name)
        return out
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
