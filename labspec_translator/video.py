"""video.py — video snapshots: decoding the bridge's frame file and drawing it with the laser position marked.
Version: 2026.11.22.8

The bridge writes a cloned video frame as '# key=value' header lines (axis types, sizes, units, values) and the
pixel values in LabSpec's order. LabSpec's video axes are taken to be stage coordinates in µm ('mkm'), so the laser
position is the stage position plus the profile's video.laser_offset_um (the beam's measured offset from LabSpec's
position, TESTING T5b). How LabSpec packs colour pixels and which axis varies fastest are profile settings
(video.packing, video.order) until the instrument has confirmed them.
"""
from __future__ import annotations

import math
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
    if len(dims) == 3 and dims.count(3) + dims.count(4) >= 1:      # e.g. 888, 606, 3: a colour channel axis
        ch = dims.index(3) if 3 in dims else dims.index(4)
        W, H = [d for i, d in enumerate(dims) if i != ch][:2]
    elif len(dims) == 2:
        W, H = dims
    elif ix is not None and iy is not None:
        W, H = sizes[ix], sizes[iy]
    else:
        raise ValueError(f"{path}: cannot tell the image's width and height")
    # one value per pixel (gray, or a packed colour), or 3 / 4 per pixel (colour channels; the XploRA's top camera
    # gave 888 x 606 x 3 values, 2026-11-20)
    if data.size % (W * H) or data.size // (W * H) not in (1, 3, 4):
        raise ValueError(f"{path}: {data.size} pixel values for a {W} x {H} image (expected 1, 3 or 4 per pixel)")
    nch = data.size // (W * H)
    # axis values: merged in axis order, each axis its own size
    offs, pos = {}, 0
    for i, s in enumerate(sizes):
        offs[i] = (pos, pos + s)
        pos += s
    xs = axis[offs[ix][0]:offs[ix][1]] if ix is not None and axis.size >= pos else np.arange(W, dtype=float)
    ys = axis[offs[iy][0]:offs[iy][1]] if iy is not None and axis.size >= pos else np.arange(H, dtype=float)
    um = all(i is not None and i < len(units) and units[i].lower() in UM_UNITS for i in (ix, iy)) \
        and xs.size == W and ys.size == H
    if nch > 1:
        img = _channels(data, W, H, nch, packing, order)
        return {"img": img, "xs": xs, "ys": ys, "um": bool(um), "meta": meta, "width": W, "height": H,
                "channels": nch}
    grid = data.reshape(H, W) if order == "x_fastest" else data.reshape(W, H).T
    if packing in ("colorref", "rgb") and grid.max() > 255.5:
        v = grid.astype(np.int64)
        a, b, c = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
        r, g, bl = (a, b, c) if packing == "colorref" else (c, b, a)
        img = np.stack([r, g, bl], axis=-1).astype(np.uint8)
    else:
        img = grid
    return {"img": img, "xs": xs, "ys": ys, "um": bool(um), "meta": meta, "width": W, "height": H}


def _channels(data, W: int, H: int, nch: int, packing: str, order: str) -> np.ndarray:
    """(H, W, 3) uint8 from 3 or 4 values per pixel. The values may come pixel by pixel (r,g,b,r,g,b...) or plane by
    plane (all of one channel, then the next): the layout that gives the smoother image is taken (a camera image
    varies little from one pixel to the next). Channel order as the packing's bytes: colorref (0x00BBGGRR) R, G, B;
    rgb (0x00RRGGBB) B, G, R (TESTING T5b.2: red and blue swapped -> the other packing)."""
    def grid(a):                 # (n) -> (H, W)
        return a.reshape(H, W) if order == "x_fastest" else a.reshape(W, H).T
    inter = [grid(data[c::nch]) for c in range(nch)]
    plane = [grid(data[c * W * H:(c + 1) * W * H]) for c in range(nch)]

    def rough(chs):
        g = np.mean(chs[:3], axis=0)
        return float(np.mean(np.abs(np.diff(g, axis=1)))) + float(np.mean(np.abs(np.diff(g, axis=0))))
    chs = inter if rough(inter) <= rough(plane) else plane
    chs = chs[:3]
    if packing == "rgb":
        chs = chs[::-1]
    img = np.stack(chs, axis=-1)
    if img.max() > 255.5:        # 16-bit or so: scaled into 8 bits
        img = img * (255.0 / img.max())
    return np.clip(img, 0, 255).astype(np.uint8)


def detect_orientation(dec: dict, native: str | Path | None) -> dict | None:
    """How ls6t's decoded frame relates to LabSpec's own saved image of the same frame (its JPEG / PNG, as LabSpec
    shows it): rows reversed (flip_y), columns reversed (flip_x), red and blue swapped (swap_rb), chosen by the best
    correlation. On the XploRA (2026-11-22) the decoded rows were reversed and red/blue swapped. None without
    LabSpec's image or Pillow."""
    if not native or not Path(native).is_file():
        return None
    try:
        from PIL import Image as PILImage
        with PILImage.open(native) as im:
            ref = np.asarray(im.convert("RGB").resize((dec["width"], dec["height"])), dtype=float)
    except Exception:  # noqa: BLE001 - no reference: nothing changed
        return None
    img = dec["img"].astype(float)
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)

    def corr(a, b):
        a, b = a - a.mean(), b - b.mean()
        d = math.sqrt(float((a * a).sum() * (b * b).sum()))
        return float((a * b).sum() / d) if d > 0 else 0.0
    gray, rgray = img.mean(axis=2), ref.mean(axis=2)
    cands = {(fy, fx): corr(gray[::-1 if fy else 1, ::-1 if fx else 1], rgray) for fy in (0, 1) for fx in (0, 1)}
    (fy, fx), best = max(cands.items(), key=lambda kv: kv[1])
    o = img[::-1 if fy else 1, ::-1 if fx else 1]
    swap = False
    if dec.get("channels", 1) > 1 or dec["img"].ndim == 3:
        same = corr(o[..., 0], ref[..., 0]) + corr(o[..., 2], ref[..., 2])
        crossed = corr(o[..., 0], ref[..., 2]) + corr(o[..., 2], ref[..., 0])
        swap = crossed > same + 0.05
    second = sorted(cands.values())[-2]
    return {"flip_y": bool(fy), "flip_x": bool(fx), "swap_rb": bool(swap), "score": round(best, 3),
            "margin": round(best - second, 3), "reference": str(native)}


def apply_orientation(dec: dict, ori: dict | None) -> dict:
    """The frame turned as LabSpec shows it (rows top to bottom), its axis values following the pixels."""
    if not ori:
        return dec
    d = dict(dec)
    img = d["img"]
    if ori.get("flip_y"):
        img, d["ys"] = img[::-1], d["ys"][::-1]
    if ori.get("flip_x"):
        img, d["xs"] = img[:, ::-1], d["xs"][::-1]
    if ori.get("swap_rb") and img.ndim == 3:
        img = img[..., ::-1]
    d["img"] = np.ascontiguousarray(img)
    return d


def scale_store(new: dict | None = None) -> dict:
    """µm per pixel and the axis signs measured per objective and camera (instrument video_scale):
    LS6T_HOME/video_scale.json {"<objective>@<camera>": {"sx": µm/px along +image x, "ty": µm/px along -image y}}."""
    import json
    from . import config
    p = config.home() / "video_scale.json"
    if new is not None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(new, indent=1), encoding="utf-8")
        except OSError:
            pass
        return new
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def image_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(drow, dcol): how far the content of image a moved to appear in b (phase correlation, a parabola through the
    peak for sub-pixel)."""
    def g(i):
        i = i.astype(float)
        i = i.mean(axis=2) if i.ndim == 3 else i
        return (i - i.mean()) * np.outer(np.hanning(i.shape[0]), np.hanning(i.shape[1]))
    A, B = np.fft.fft2(g(a)), np.fft.fft2(g(b))
    R = np.conj(A) * B
    R /= np.abs(R) + 1e-12
    r = np.fft.ifft2(R).real
    i, j = np.unravel_index(int(np.argmax(r)), r.shape)
    h, w = r.shape

    def sub(_, m, n, axis):
        y0 = r[(m - 1) % h, n] if axis == 0 else r[m, (n - 1) % w]
        y2 = r[(m + 1) % h, n] if axis == 0 else r[m, (n + 1) % w]
        den = y0 - 2 * r[m, n] + y2
        return 0.5 * (y0 - y2) / den if den < 0 else 0.0
    di = i + sub(None, i, j, 0)
    dj = j + sub(None, i, j, 1)
    if di > h / 2:
        di -= h
    if dj > w / 2:
        dj -= w
    return float(di), float(dj)


def stage_axes(dec: dict, prof: dict, objective, x, y, camera=None) -> dict | None:
    """Axes in stage µm from the objective's field of view (OLConfig.ini, objectives_info) around the stage
    position: LabSpec's own video axes on the XploRA were one 'µm' per pixel from 0 (887 x 605 for an 862.9 x 590.0
    µm field). The image is as LabSpec shows it (row 0 at the top); X grows to the right and Y upward unless the
    profile says otherwise (video.image_x_right / video.image_y_up; TESTING T5b.2 checks them)."""
    if x is None or y is None:
        return None
    cal = scale_store().get(f"{objective}@{camera}")
    if cal and cal.get("sx") and cal.get("ty"):
        w, h = dec["width"], dec["height"]
        d = dict(dec)
        d["xs"] = x + (np.arange(w) - (w - 1) / 2) * float(cal["sx"])
        d["ys"] = y - (np.arange(h) - (h - 1) / 2) * float(cal["ty"])
        d["um"] = True
        d["axes_from"] = "measured (instrument video_scale)"
        return d
    info = (prof.get("objectives_info") or {}).get(objective) or {}
    field = info.get("field_um")
    if not field:
        from .profile import match_label
        lab, _ = match_label(str(objective or ""), list((prof.get("objectives_info") or {})))
        field = ((prof.get("objectives_info") or {}).get(lab) or {}).get("field_um") if lab else None
    if not field:
        return None
    w, h = dec["width"], dec["height"]
    v = prof.get("video", {})
    sx = float(field[0]) / w * (1 if v.get("image_x_right", True) else -1)
    sy = float(field[1]) / h * (1 if v.get("image_y_up", True) else -1)
    d = dict(dec)
    d["xs"] = x + (np.arange(w) - (w - 1) / 2) * sx
    d["ys"] = y - (np.arange(h) - (h - 1) / 2) * sy        # row 0 at the top: the largest Y when Y grows upward
    d["um"] = True
    d["axes_from"] = "the objective's field of view (OLConfig.ini) around the stage position"
    return d


DOG_NOISE_FLOOR = 0.1      # grey levels (0-255): the smallest DoG noise a quantised frame is credited with


def frame_too_dark(lum) -> bool:
    """True when the frame holds nothing to look at: almost black (99th percentile under 8 of 255) or flat (under 4
    grey levels between its 1st and 99th percentiles). 2026-11-22: the x5 with too little light gave a black frame."""
    lum = np.asarray(lum, float)
    lo, hi = np.percentile(lum, [1, 99])
    return bool(hi < 8 or hi - lo < 4)


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


def features(dec: dict, n: int = 5) -> list:
    """Small isolated features (dust, particles, pits: 0.5-4 µm across) the beam can be aimed at to find it by the
    Raman signal: extrema of a difference of Gaussians over a few scales, strongest first, away from the frame's
    edge. Edges respond too but weaker than a blob of the matching size."""
    from scipy.ndimage import gaussian_filter, maximum_filter
    if not dec["um"]:
        return []
    lum = _lum(dec["img"])
    if frame_too_dark(lum):
        return []                       # nothing to see (2026-11-22: a black x5 frame gave strengths of 1e9)
    h, w = lum.shape
    xs, ys = dec["xs"], dec["ys"]
    um_px = abs(xs[-1] - xs[0]) / max(w - 1, 1)
    best = np.zeros_like(lum)
    sig_of = np.zeros_like(lum)
    for r_um in (0.5, 1.0, 2.0, 4.0):
        sg = max(0.8, r_um / um_px / 1.41)
        if sg > min(h, w) / 6:
            continue
        dog = (gaussian_filter(lum, sg) - gaussian_filter(lum, 1.6 * sg)) * sg     # scale-normalised
        upd = np.abs(dog) > np.abs(best)
        best = np.where(upd, dog, best)
        sig_of = np.where(upd, sg, sig_of)
    mag = np.abs(best)
    # the noise, never below what 8-bit grey levels can resolve: a flat or black frame has a spread of ~1e-15
    # (rounding in the filters), and dividing by it made any speck a 'feature' of strength 1e9 (2026-11-22)
    noise = max(1.4826 * float(np.median(np.abs(mag - np.median(mag)))), DOG_NOISE_FLOOR)
    peaks = (mag == maximum_filter(mag, size=7)) & (mag > 8 * noise)
    m = max(3, int(min(h, w) * 0.05))
    peaks[:m, :] = peaks[-m:, :] = False
    peaks[:, :m] = peaks[:, -m:] = False
    jj, ii = np.nonzero(peaks)
    order = np.argsort(mag[jj, ii])[::-1][:n]
    out = []
    for k in order:
        j, i = int(jj[k]), int(ii[k])
        out.append({"x_um": round(float(np.interp(i, np.arange(w), xs)), 3),
                    "y_um": round(float(np.interp(j, np.arange(h), ys)), 3),
                    "size_um": round(float(2 * 1.41 * sig_of[j, i] * um_px), 2), "dark": bool(best[j, i] < 0),
                    "strength": round(float(mag[j, i] / noise), 1)})
    for k, f in enumerate(out, 1):
        f["n"] = k
    return out


def refine_feature(dec: dict, x: float, y: float, r_um: float = 3.0) -> dict | None:
    """The centre of the feature near (x, y): the weighted centroid of its contrast (dark or bright) against the
    surroundings, within r_um. None when nothing stands out there."""
    from scipy.ndimage import median_filter
    if not dec["um"]:
        return None
    lum = _lum(dec["img"])
    h, w = lum.shape
    xs, ys = dec["xs"], dec["ys"]
    um_px = abs(xs[-1] - xs[0]) / max(w - 1, 1)
    X, Y = np.meshgrid(xs, ys)
    near = np.hypot(X - x, Y - y) <= r_um
    if near.sum() < 9:
        return None
    bg = median_filter(lum, size=max(5, int(3 * r_um / um_px) | 1))
    d = lum - bg
    sel = d[near]
    dark = abs(sel.min()) > abs(sel.max())
    c = np.clip(-d if dark else d, 0, None) * near
    s = 1.4826 * np.median(np.abs(d - np.median(d))) or 1e-9
    if c.max() < 6 * s:
        return None
    c = np.clip(c - 0.3 * c.max(), 0, None)
    tot = c.sum()
    return {"x_um": round(float((c * X).sum() / tot), 3), "y_um": round(float((c * Y).sum() / tot), 3),
            "dark": bool(dark), "contrast_sigma": round(float(c.max() / s), 1)}


def render(dec: dict, png: Path, marker: tuple | None, title: str, note: str = "", spot: dict | None = None,
           feats: list | None = None) -> dict:
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
    for f in feats or []:
        span = abs(xs[-1] - xs[0]) * 0.025
        ax.add_patch(plt.Circle((f["x_um"], f["y_um"]), max(span, f["size_um"] / 2 + 0.5), fill=False, color="#ffd400",
                                lw=1.0))
        ax.annotate(str(f["n"]), (f["x_um"], f["y_um"]), xytext=(4, 4), textcoords="offset points",
                    color="#ffd400", fontsize=8, weight="bold")
    if spot is not None:
        span = (abs(xs[-1] - xs[0]) if dec["um"] else w) * 0.04
        ax.add_patch(plt.Circle((spot["x"], spot["y"]), 1.1 * span, fill=False, color="#00d5ff", lw=1.2, ls="--"))
    import textwrap
    ax.set_title(title + ("\n" + "\n".join(textwrap.wrap(note, 80)) if note else ""), fontsize=9)
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)
    return {"path": str(png)}


def _orientation_store(new: dict | None = None) -> dict:
    """The frame orientation found per camera (LS6T_HOME/video_orientation.json): read, or written when new."""
    import json
    from . import config
    p = config.home() / "video_orientation.json"
    if new is not None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(new, indent=1), encoding="utf-8")
        except OSError:
            pass
        return new
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def snapshot(res: dict, prof: dict, base: Path, title: str, laser_nm=None) -> dict:
    """The snapshot record from the bridge's answer: the PNG with the laser position marked (LabSpec's position
    plus the beam's remembered offset for this objective and laser), the raw frame, the files LabSpec saved, the
    stage position, the marker, small features to aim at (numbered on the PNG), and with the laser on: the spot
    found and the offset measured."""
    from . import spot as SP
    v = prof.get("video", {})
    out: dict = {"raw": res.get("out"), "native": {}, "warnings": [], "laser_s": _f(res.get("laser_s")),
                 "objective": res.get("objective")}
    if prof.get("manual_objective", True) and res.get("objective"):
        out["objective_source"] = ("LabSpec's setting, not checked (manual turret): the µm scale and the laser marker "
                                   "assume this lens is in place")
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
        nat = next((out["native"][f] for f in ("png", "jpg", "bmp", "tif") if f in out["native"]), None)
        if nat:
            out["view"] = nat
            out["warnings"].append(f"LabSpec's own image (no laser marker): get_image {nat}")
        return out
    out["channels"] = dec.get("channels", 1)
    # turned as LabSpec shows it: from LabSpec's own image of this frame when it saved one, else as last found for
    # this camera (LS6T_HOME/video_orientation.json)
    nat = next((out["native"][f] for f in ("jpg", "png", "bmp", "tif") if f in out["native"]), None)
    ori = detect_orientation(dec, nat)
    store = _orientation_store()
    cam = str(res.get("camera") if res.get("camera") not in (None, "") else "?")
    if ori and ori["score"] >= 0.5 and ori["margin"] >= 0.05:
        store[cam] = {k: ori[k] for k in ("flip_y", "flip_x", "swap_rb", "score")}
        _orientation_store(store)
    elif store.get(cam):
        ori = dict(store[cam], reference="remembered for this camera")
    else:
        ori = None
    out["orientation"] = ori
    dec = apply_orientation(dec, ori)
    labspec_span = [float(abs(dec["xs"][-1] - dec["xs"][0])), float(abs(dec["ys"][-1] - dec["ys"][0]))] \
        if len(dec["xs"]) > 1 else None
    sa = stage_axes(dec, prof, res.get("objective"), x, y, res.get("camera"))
    if sa is not None:
        dec = sa
        out["axes_from"] = sa["axes_from"]
        out["labspec_axes_span"] = labspec_span
    else:
        out["axes_from"] = "LabSpec's video axes"
        per_px = labspec_span and abs(labspec_span[0] / max(dec["width"] - 1, 1) - 1) < 0.01
        if per_px:
            out["warnings"].append(f"no field of view or measured scale for the objective {res.get('objective')!r}: "
                                   "the axes are LabSpec's, one per pixel whatever the objective, so positions "
                                   "in µm are not to be trusted; run `ls6t instrument video-scale` with this "
                                   "objective")
    off, src = SP.offset_for(prof, res.get("objective"), laser_nm)
    out["offset_used_um"], out["offset_source"] = off, src
    marker, note = None, ""
    if dec["um"] and v.get("marker", "stage") == "stage" and x is not None and y is not None:
        marker = (x + float(off[0]), y + float(off[1]))
        inside = min(dec["xs"]) <= marker[0] <= max(dec["xs"]) and min(dec["ys"]) <= marker[1] <= max(dec["ys"])
        if not inside:
            out["warnings"].append("the laser position falls outside the frame: the video axes may not be stage "
                                   "coordinates (profile video.marker 'center' marks the image centre instead)")
            marker = None
        note = "marker: LabSpec's laser position" + (f" + offset ({src})" if any(off) else
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
    lit = float(res.get("laser_s") or 0) > 0 or float(res.get("during_acq_s") or 0) > 0
    if lit:
        note += (f"; frame during a {float(res['during_acq_s']):g} s acquisition" if float(res.get("during_acq_s") or 0)
                 > 0 else f"; laser on {float(res['laser_s']):g} s")
        dark = None
        if res.get("out_nolaser"):
            try:
                dark = decode(res["out_nolaser"], v.get("packing", "colorref"), v.get("order", "x_fastest"))
                dark = apply_orientation(dark, ori)
                if sa is not None:
                    dark = dict(dark, xs=dec["xs"], ys=dec["ys"], um=True)
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
    feats = []
    if frame_too_dark(_lum(dec["img"])) and not spot:
        out["frame_dark"] = True
        out["warnings"].append("the frame is black or without contrast (too little light, the camera's exposure, or "
                               "the video stopped): no features; turn the white light up or check LabSpec's image")
    elif dec["um"]:
        try:
            feats = features(dec)
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append(f"feature detection failed: {exc}")
    out["features"] = feats
    out["_decoded"] = dec
    if feats:
        note += "; yellow: features to aim at"
    try:
        # its own name: LabSpec saves its own image as <base>.png (2026-11-20: the plot replaced it)
        out.update(render(dec, Path(f"{base}.marked.png"), marker, title, note, spot, feats))
    except ImportError:
        out["warnings"].append("matplotlib is missing (pip install labspec-translator[render]): no PNG")
    out.update(marker_um=list(marker) if marker and dec["um"] else None, um_axes=dec["um"],
               field_um=[float(abs(dec["xs"][-1] - dec["xs"][0])), float(abs(dec["ys"][-1] - dec["ys"][0]))]
               if dec["um"] else None, pixels=[dec["width"], dec["height"]],
               what="video frame; red cross: the laser position; yellow numbered circles: small features")
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
