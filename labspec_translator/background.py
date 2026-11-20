"""background.py — white-light backgrounds: spectra taken with the white light on and the laser off, kept by name and
subtracted from later spectra taken with the same light.
Version: 2026.11.20.1

A background is stored as counts per second per accumulation against wavelength (nm), with the settings it was
taken with (laser line, grating, objective, range). Subtraction scales it by each spectrum's exposure and
interpolates it onto the spectrum's axis; spectra keep their raw files, and results say what was subtracted.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import numpy as np

from . import config


def folder() -> Path:
    return (config.out_dir() / "sim_backgrounds") if config.mode() == "sim" else (config.home() / "backgrounds")


def _index_path() -> Path:
    return folder() / "index.json"


def index() -> dict:
    try:
        return json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", str(name or "")))


def save(name: str, x, y_cps, x_unit: str, meta: dict) -> dict:
    f = folder()
    f.mkdir(parents=True, exist_ok=True)
    data = f / f"{name}.txt"
    tmp = data.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"# x_unit={x_unit}\n# y_unit=cnt/s per accumulation\n")
        for a, b in zip(x, y_cps):
            fh.write(f"{a:.6f}\t{b:.6f}\n")
    os.replace(tmp, data)
    idx = index()
    idx[name] = {**meta, "x_unit": x_unit, "file": str(data), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "points": int(len(x))}
    tmp = _index_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    os.replace(tmp, _index_path())
    return idx[name]


def load(name: str) -> dict | None:
    meta = index().get(name)
    if not meta:
        return None
    from . import spectra
    sp = spectra.read(meta["file"])
    return {"x": sp["x"], "y": sp["y"], "meta": meta}


def subtract(x, y, x_unit: str, exposure_s: float, bg: dict, laser_nm: float | None) -> tuple[np.ndarray, dict]:
    """y minus the background scaled to this exposure, on this spectrum's axis (converted through the laser line
    when the units differ). Returns (y_corrected, info)."""
    from .profile import nm_to_shift, shift_to_nm
    bx, by = np.asarray(bg["x"], float), np.asarray(bg["y"], float)
    bu = bg["meta"].get("x_unit", "nm")
    xu = (x_unit or "nm").lower()
    if bu.lower() != xu and laser_nm:
        bx = nm_to_shift(bx, laser_nm) if "cm" in xu else shift_to_nm(bx, laser_nm)
    o = np.argsort(bx)
    bx, by = bx[o], by[o]
    xs = np.asarray(x, float)
    inside = (xs >= bx.min()) & (xs <= bx.max())
    ybg = np.interp(xs, bx, by) * float(exposure_s or 1.0)
    out = np.asarray(y, float) - np.where(inside, ybg, 0.0)
    info = {"name": bg["meta"].get("name"), "covered_fraction": round(float(inside.mean()), 3),
            "max_subtracted": round(float(ybg[inside].max()) if inside.any() else 0.0, 3),
            "taken": bg["meta"].get("time")}
    return out, info
