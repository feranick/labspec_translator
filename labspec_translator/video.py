"""video.py — video snapshots: decoding the bridge's frame file and drawing it with the laser position marked.
Version: 2026.11.19.4

The bridge writes a cloned video frame as '# key=value' header lines (axis types, sizes, units, values) and the
pixel values in LabSpec's order. LabSpec's video axes are taken to be stage coordinates in µm ('mkm'), so the laser
position is the stage position plus the profile's video.laser_offset_um (the beam's measured offset from LabSpec's
position, TESTING T5b). How LabSpec packs colour pixels and which axis varies fastest are profile settings
(video.packing, video.order) until the instrument has confirmed them.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .profile import read_text

UM_UNITS = ("mkm", "um", "µm", "micron", "microns", "?m")


def decode(path, packing: str = "colorref", order: str = "x_fastest") -> dict:
    """{img: (H, W, 3) uint8 or (H, W) float, xs, ys, um: bool, meta}."""
    meta: dict = {}
    vals: list[str] = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            k, _, v = line[1:].strip().partition("=")
            meta[k.strip()] = v.strip()
        else:
            vals.append(line)
    data = np.array([float(v) for row in vals for v in row.split(",") if v != ""], float)
    types = [t.strip() for t in meta.get("axis_types", "").split(",") if t.strip()]
    sizes = [int(float(s)) for s in meta.get("axis_sizes", "").split(",") if s.strip()]
    units = [u.strip() for u in meta.get("axis_units", "").split(",")]
    axis = np.array([float(v) for v in meta.get("axis_values", "").split(",") if v.strip()], float)
    dims = [int(float(d)) for d in meta.get("data_dims", "").split(",") if d.strip()]
    # the X and Y axes: by type, else the first two axes with a size
    idx = {t.upper(): i for i, t in enumerate(types)}
    nz = [i for i, s in enumerate(sizes) if s > 0]
    ix = idx.get("X", nz[0] if nz else None)
    iy = idx.get("Y", nz[1] if len(nz) > 1 else None)
    if len(dims) == 2:
        W, H = dims
    elif ix is not None and iy is not None:
        W, H = sizes[ix], sizes[iy]
    else:
        raise ValueError(f"{path}: cannot tell the image's width and height")
    if data.size != W * H:
        raise ValueError(f"{path}: {data.size} pixel values for a {W} x {H} image")
    # axis values: merged in axis order, each axis its own size
    offs, pos = {}, 0
    for i, s in enumerate(sizes):
        offs[i] = (pos, pos + s)
        pos += s
    xs = axis[offs[ix][0]:offs[ix][1]] if ix is not None and axis.size >= pos else np.arange(W, dtype=float)
    ys = axis[offs[iy][0]:offs[iy][1]] if iy is not None and axis.size >= pos else np.arange(H, dtype=float)
    um = all(i is not None and i < len(units) and units[i].lower() in UM_UNITS for i in (ix, iy)) \
        and xs.size == W and ys.size == H
    grid = data.reshape(H, W) if order == "x_fastest" else data.reshape(W, H).T
    if packing in ("colorref", "rgb") and grid.max() > 255.5:
        v = grid.astype(np.int64)
        a, b, c = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
        r, g, bl = (a, b, c) if packing == "colorref" else (c, b, a)
        img = np.stack([r, g, bl], axis=-1).astype(np.uint8)
    else:
        img = grid
    return {"img": img, "xs": xs, "ys": ys, "um": bool(um), "meta": meta, "width": W, "height": H}


def _lum(img) -> np.ndarray:
    img = np.asarray(img, float)
    return img @ np.array([0.299, 0.587, 0.114]) if img.ndim == 3 else img


def find_spot(dec: dict, dark: dict | None = None) -> dict | None:
    """The laser spot in a frame taken with the shutter open: with `dark` (the same view without the laser) the
    brightest point of their difference, which holds the spot alone; without it, the brightest compact blob above
    the local background (sample edges can pull it). The weighted centroid in the image's own units; None when
    nothing stands out (8 sigma of the scatter)."""
    from scipy.ndimage import uniform_filter
    lum = _lum(dec["img"])
    h, w = lum.shape
    if dark is not None and np.shape(dark["img"])[:2] == (h, w):
        d = lum - _lum(dark["img"])
    else:
        d = lum - uniform_filter(lum, size=max(9, min(h, w) // 6), mode="nearest")
    s = max(1.4826 * float(np.median(np.abs(d - np.median(d)))), 0.5)   # at least half a grey level
    j, i = np.unravel_index(int(np.argmax(d)), d.shape)
    if d[j, i] < 8 * s:
        return None
    r = max(3, min(h, w) // 12)
    sl = (slice(max(0, j - r), j + r + 1), slice(max(0, i - r), i + r + 1))
    wts = np.clip(d[sl] - 0.2 * d[j, i], 0, None)      # the spot's core, not the surrounding noise
    jj, ii = np.mgrid[sl]
    cj, ci = float((wts * jj).sum() / wts.sum()), float((wts * ii).sum() / wts.sum())
    if dec["um"]:
        xs, ys = dec["xs"], dec["ys"]
        x = float(np.interp(ci, np.arange(w), xs))
        y = float(np.interp(cj, np.arange(h), ys))
    else:
        x, y = ci, cj
    return {"x": x, "y": y, "pixel": [round(ci, 2), round(cj, 2)], "contrast_sigma": round(float(d[j, i] / s), 1)}


def render(dec: dict, png: Path, marker: tuple | None, title: str, note: str = "", spot: dict | None = None) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    img, xs, ys = dec["img"], dec["xs"], dec["ys"]
    h, w = img.shape[:2]
    fig, ax = plt.subplots(figsize=(6.4, 6.4 * h / w + 0.6), dpi=120)
    if dec["um"]:
        dx = (xs[-1] - xs[0]) / max(w - 1, 1)
        dy = (ys[-1] - ys[0]) / max(h - 1, 1)
        ext = [xs[0] - dx / 2, xs[-1] + dx / 2, ys[-1] + dy / 2, ys[0] - dy / 2]   # row 0 at the top, as stored
        ax.imshow(img, extent=ext, origin="upper", cmap=None if img.ndim == 3 else "gray", aspect="equal")
        ax.set_xlabel("stage X (µm)")
        ax.set_ylabel("stage Y (µm)")
    else:
        ax.imshow(img, origin="upper", cmap=None if img.ndim == 3 else "gray")
        ax.set_xlabel("pixel")
        ax.set_ylabel("pixel")
    if marker is not None:
        mx, my = marker
        span = (abs(xs[-1] - xs[0]) if dec["um"] else w) * 0.04
        ax.plot([mx - 2 * span, mx - 0.6 * span], [my, my], color="#ff2d2d", lw=1.4)
        ax.plot([mx + 0.6 * span, mx + 2 * span], [my, my], color="#ff2d2d", lw=1.4)
        ax.plot([mx, mx], [my - 2 * span, my - 0.6 * span], color="#ff2d2d", lw=1.4)
        ax.plot([mx, mx], [my + 0.6 * span, my + 2 * span], color="#ff2d2d", lw=1.4)
        ax.add_patch(plt.Circle((mx, my), 0.6 * span, fill=False, color="#ff2d2d", lw=1.2))
    if spot is not None:
        span = (abs(xs[-1] - xs[0]) if dec["um"] else w) * 0.04
        ax.add_patch(plt.Circle((spot["x"], spot["y"]), 1.1 * span, fill=False, color="#00d5ff", lw=1.2, ls="--"))
    import textwrap
    ax.set_title(title + ("\n" + "\n".join(textwrap.wrap(note, 80)) if note else ""), fontsize=9)
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)
    return {"path": str(png)}


def snapshot(res: dict, prof: dict, base: Path, title: str) -> dict:
    """The snapshot record from the bridge's answer: the PNG with the laser position marked, the raw frame, the
    files LabSpec saved, the stage position and the marker, with warnings."""
    v = prof.get("video", {})
    out: dict = {"raw": res.get("out"), "native": {}, "warnings": [], "laser_s": _f(res.get("laser_s")),
                 "objective": res.get("objective")}
    for k, val in res.items():
        if k.startswith("saved_") and str(val) == "1":
            fmt = k[len("saved_"):]
            out["native"][fmt] = f"{base}.{fmt}"
    x, y = _f(res.get("x_um")), _f(res.get("y_um"))
    out["stage"] = {"x_um": x, "y_um": y}
    if not res.get("out"):
        out["warnings"].append("the bridge wrote no frame file")
        return out
    try:
        dec = decode(res["out"], v.get("packing", "colorref"), v.get("order", "x_fastest"))
    except (ValueError, OSError) as exc:
        out["warnings"].append(f"the frame could not be decoded: {exc}")
        return out
    off = v.get("laser_offset_um", [0.0, 0.0]) or [0.0, 0.0]
    marker, note = None, ""
    if dec["um"] and v.get("marker", "stage") == "stage" and x is not None and y is not None:
        marker = (x + float(off[0]), y + float(off[1]))
        inside = min(dec["xs"]) <= marker[0] <= max(dec["xs"]) and min(dec["ys"]) <= marker[1] <= max(dec["ys"])
        if not inside:
            out["warnings"].append("the laser position falls outside the frame: the video axes may not be stage "
                                   "coordinates (profile video.marker 'center' marks the image centre instead)")
            marker = None
        note = "marker: LabSpec's laser position" + (" + measured offset" if any(off) else
                                                     " (offset not measured yet)")
    else:
        if dec["um"]:
            marker = (float(np.mean(dec["xs"])), float(np.mean(dec["ys"])))
        else:
            marker = (dec["width"] / 2, dec["height"] / 2)
        note = "marker: image centre" + ("" if dec["um"] else " (video axes not in µm)")
        if not dec["um"]:
            out["warnings"].append("the video axes are not in µm: positions cannot be marked on the frame")
    spot = None
    if res.get("laser_s") and float(res["laser_s"]) > 0:
        note += f"; laser on {float(res['laser_s']):g} s"
        dark = None
        if res.get("out_nolaser"):
            try:
                dark = decode(res["out_nolaser"], v.get("packing", "colorref"), v.get("order", "x_fastest"))
                out["raw_nolaser"] = res["out_nolaser"]
            except (ValueError, OSError) as exc:
                out["warnings"].append(f"the frame without the laser could not be decoded: {exc}")
        spot = find_spot(dec, dark)
        if spot is None:
            out["warnings"].append("the laser spot was not found in the frame (too weak, out of focus, or the "
                                   "camera does not see this wavelength)")
        else:
            note += "; dashed: the spot found"
            out["spot_um" if dec["um"] else "spot_pixel"] = [round(spot["x"], 3), round(spot["y"], 3)]
            out["spot_contrast_sigma"] = spot["contrast_sigma"]
            if dec["um"] and x is not None and y is not None:
                # what to put in the profile's video.laser_offset_um so the marker sits on the beam
                out["measured_offset_um"] = [round(spot["x"] - x, 3), round(spot["y"] - y, 3)]
    try:
        out.update(render(dec, Path(f"{base}.png"), marker, title, note, spot))
    except ImportError:
        out["warnings"].append("matplotlib is missing (pip install labspec-translator[render]): no PNG")
    out.update(marker_um=list(marker) if marker and dec["um"] else None, um_axes=dec["um"],
               field_um=[float(abs(dec["xs"][-1] - dec["xs"][0])), float(abs(dec["ys"][-1] - dec["ys"][0]))]
               if dec["um"] else None, pixels=[dec["width"], dec["height"]],
               what="video frame; red cross: the laser position")
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
