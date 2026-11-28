"""video.py — video snapshots: decoding the bridge's frame file and drawing it with the laser position marked.

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

from .io_util import parse_kv_and_csv, to_float

UM_UNITS = ("mkm", "um", "µm", "micron", "microns", "?m")


def decode(path, packing: str = "colorref", order: str = "x_fastest") -> dict:
    """{img: (H, W, 3) uint8 or (H, W) float, xs, ys, um: bool, meta}."""
    meta, data = parse_kv_and_csv(path)
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
    swap = None                         # None: this frame cannot tell (grey, or red and blue alike)
    if dec.get("channels", 1) > 1 or dec["img"].ndim == 3:
        same = corr(o[..., 0], ref[..., 0]) + corr(o[..., 2], ref[..., 2])
        crossed = corr(o[..., 0], ref[..., 2]) + corr(o[..., 2], ref[..., 0])
        if abs(crossed - same) > 0.1 and corr(ref[..., 0], ref[..., 2]) < 0.98:
            swap = crossed > same
    second = sorted(cands.values())[-2]
    return {"flip_y": bool(fy), "flip_x": bool(fx), "swap_rb": swap, "score": round(best, 3),
            "margin": round(best - second, 3), "reference": str(native)}


def orientation_for(dec: dict, native, camera) -> tuple[dict | None, str | None]:
    """The orientation to apply to this frame, and a warning when it is not decided. Learnt from LabSpec's own image
    only when the frame can decide it (not dark or flat, a clear best match; the colour swap only from a frame
    with colour: a grey Si view cannot tell red from blue); stored per camera then. Otherwise what was stored for
    this camera is kept and applied: a black frame (the x5 with too little light, 2026-11-22) must not overwrite it."""
    store = _orientation_store()
    cam = str(camera if camera not in (None, "") else "?")
    prev = store.get(cam)
    ori = detect_orientation(dec, native)
    decided = bool(ori) and not frame_too_dark(_lum(dec["img"])) and ori["score"] >= 0.5 and ori["margin"] >= 0.05
    if decided:
        new = {k: ori[k] for k in ("flip_y", "flip_x", "score")}
        if ori["swap_rb"] is None:
            new["swap_rb"] = bool((prev or {}).get("swap_rb", False))
            new["swap_from"] = "kept (this frame has too little colour to tell)" if prev else \
                "not decided yet (a frame with colour decides it)"
        else:
            new["swap_rb"] = bool(ori["swap_rb"])
        store[cam] = new
        _orientation_store(store)
        return dict(new, margin=ori["margin"], reference=ori["reference"]), None
    why = "no LabSpec image to compare with" if not ori else \
        ("the frame is dark or flat" if frame_too_dark(_lum(dec["img"])) else
         f"no clear match with LabSpec's image (score {ori['score']}, margin {ori['margin']})")
    if prev:
        return dict(prev, reference=f"remembered for this camera ({why})"), None
    if not ori:
        return None, None               # no LabSpec image saved (formats without jpg/png): the frame as decoded
    return None, f"orientation not decided ({why}) and none remembered for camera {cam}: the frame may be upside " \
                 "down or its red and blue swapped against LabSpec's; take a snapshot of a lit, structured view"


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


BRIGHTNESS_RATIO = 2.5      # a frame this much darker (or brighter) than the last one with the same objective: warn


def _brightness_store(new: dict | None = None) -> dict:
    """The mean grey level of the last frame without the laser, per objective and camera
    (LS6T_HOME/video_brightness.json)."""
    from . import store
    if new is not None:
        try:
            return store.write("video_brightness.json", new)
        except OSError:
            return new
    return store.read("video_brightness.json")


def brightness_check(dec: dict, objective, camera) -> tuple[dict, str | None]:
    """The frame's mean grey level against the last one with the same objective and camera, and a warning when it is
    BRIGHTNESS_RATIO times darker or brighter. 2026-11-23: twice after a video restart or an objective change,
    LabSpec's indicator said bright field while the light was dark field (toggling it in LabSpec fixed it); LabSpec's
    own reading cannot catch that, the picture can."""
    import time
    mean = float(np.mean(_lum(dec["img"])))
    key = f"{objective}@{camera}"
    store = _brightness_store()
    last = store.get(key)
    store[key] = {"mean": round(mean, 2), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    _brightness_store(store)
    info = {"mean": round(mean, 2), "last_mean": last.get("mean") if last else None,
            "last_time": last.get("time") if last else None}
    if not last or not last.get("mean"):
        return info, None
    r = mean / max(float(last["mean"]), 0.5)
    if r < 1 / BRIGHTNESS_RATIO or r > BRIGHTNESS_RATIO:
        way = "darker" if r < 1 else "brighter"
        return info, (f"this frame is {1 / r if r < 1 else r:.1f}x {way} than the last one with objective "
                      f"{objective!r} (mean grey {mean:.0f} vs {float(last['mean']):.0f}, {last.get('time')}): the "
                      "illumination may not be what LabSpec shows (after a video restart or an objective change "
                      "LabSpec has shown bright field with the light in dark field: `ls6t instrument set --illumination "
                      "bright` (or dark) sets it even when LabSpec shows it already, or toggle it in LabSpec), or the "
                      "light or exposure changed. If that was on purpose, ignore this")
    return info, None


def _field_mode(label) -> str | None:
    s = str(label or "").lower().replace(" ", "")
    return "dark" if "darkfield" in s else "bright" if "brightfield" in s else None


def illumination_mismatch(illum, light) -> str | None:
    """A warning when LabSpec's two illumination controls disagree (IlluminatorV: Dark / Bright field; uLight: Off,
    BrightField, DarkField1-4, Full). 2026-11-23: twice the indicator showed bright field with the light in dark field
    after a video restart or an objective change."""
    a, b = _field_mode(illum), _field_mode(light)
    if a and b and a != b:
        return (f"LabSpec's illumination controls disagree: IlluminatorV says {illum!r}, uLight {light!r}: the view may "
                "not be lit as LabSpec shows (`ls6t instrument set --illumination bright` or dark sets it again, or "
                "toggle it in LabSpec)")
    return None


def scale_store(new: dict | None = None) -> dict:
    """µm per pixel and the axis signs measured per objective and camera (instrument video_scale):
    LS6T_HOME/video_scale.json {"<objective>@<camera>": {"sx": µm/px along +image x, "ty": µm/px along -image y}}."""
    from . import store
    if new is not None:
        try:
            return store.write("video_scale.json", new)
        except OSError:
            return new
    return store.read("video_scale.json")


def _phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(drow, dcol): how far the content of image a moved to appear in b (phase correlation, a parabola through the
    peak for sub-pixel). On the XploRA (2026-11-23) it gave 0.05-0.9 px cross-talk over 170-260 px moves."""
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


def _mismatch(A: np.ndarray, B: np.ndarray, dr: int, dc: int) -> float:
    """How unlike the parts of A and B are that show the same patch if B is A moved by (dr, dc): the mean squared
    difference with each part's mean removed (inf when they overlap on less than a quarter of the frame)."""
    h, w = A.shape
    a = A[max(0, -dr):h - max(0, dr), max(0, -dc):w - max(0, dc)]
    b = B[max(0, dr):h + min(0, dr), max(0, dc):w + min(0, dc)]
    if a.size < 0.25 * A.size:
        return float("inf")
    return float(np.mean(((a - a.mean()) - (b - b.mean())) ** 2))


def image_shift(a: np.ndarray, b: np.ndarray, axis: str | None = None) -> tuple[float, float]:
    """(drow, dcol): how far the content of image a moved to appear in b. Phase correlation first; checked against
    the frames themselves (the mean squared difference over their overlap), and when that finds a clearly better
    match, a search for it: on a 4x smaller copy over the whole range along `axis` ('x': columns, 'y': rows; +-10 %
    across it), then to the pixel and a parabola for the fraction. 2026-11-23: phase correlation alone lost a 19 µm
    move of the simulator's x100 (47 of 160 px) and returned a false peak."""
    from scipy.ndimage import zoom
    g = (lambda i: (i.astype(float).mean(axis=2) if i.ndim == 3 else i.astype(float)))
    A, B = g(a), g(b)
    h, w = A.shape
    pr, pcol = _phase_shift(a, b)
    best = _mismatch(A, B, int(round(pr)), int(round(pcol)))
    f = max(1, min(h, w) // 150)
    As, Bs = (zoom(A, 1 / f, order=1), zoom(B, 1 / f, order=1)) if f > 1 else (A, B)
    hs, ws = As.shape
    rr = range(-int(0.75 * hs), int(0.75 * hs) + 1) if axis != "x" else range(-int(0.1 * hs) - 1, int(0.1 * hs) + 2)
    cc = range(-int(0.75 * ws), int(0.75 * ws) + 1) if axis != "y" else range(-int(0.1 * ws) - 1, int(0.1 * ws) + 2)
    m, (sr, sc) = min((_mismatch(As, Bs, r, c), (r, c)) for r in rr for c in cc)
    if not np.isfinite(m):                  # no overlap anywhere: the move took the view out of the frame
        return float("nan"), float("nan")
    sr, sc = sr * f, sc * f
    if best <= 1.15 * _mismatch(A, B, sr, sc):
        return pr, pcol                     # phase correlation agrees with the frames: its sub-pixel value
    near = {(r, c): _mismatch(A, B, r, c) for r in range(sr - f - 1, sr + f + 2) for c in range(sc - f - 1, sc + f + 2)}
    (br, bc) = min(near, key=near.get)

    def frac(y0, y1, y2):
        if not all(np.isfinite((y0, y1, y2))):      # an edge of the overlap: no fraction (it gave NaN, 2026-11-23)
            return 0.0
        den = y0 - 2 * y1 + y2
        return 0.5 * (y0 - y2) / den if den > 0 else 0.0
    get = (lambda r, c: near.get((r, c), _mismatch(A, B, r, c)))
    dr = br + frac(get(br - 1, bc), get(br, bc), get(br + 1, bc))
    dc = bc + frac(get(br, bc - 1), get(br, bc), get(br, bc + 1))
    return float(dr), float(dc)


def field_um(prof: dict, objective):
    """The objective's field of view [w, h] in µm from OLConfig.ini (profile objectives_info), or None."""
    info = (prof.get("objectives_info") or {}).get(objective) or {}
    field = info.get("field_um")
    if not field:
        from .profile import match_label
        lab, _ = match_label(str(objective or ""), list((prof.get("objectives_info") or {})))
        field = ((prof.get("objectives_info") or {}).get(lab) or {}).get("field_um") if lab else None
    return field or None


def _same_camera(key: str, camera) -> bool:
    cam = key.rsplit("@", 1)[-1]
    return camera in (None, "") or cam == str(camera)


def axes_directions(prof: dict, objective, camera) -> tuple[int, int, str]:
    """(sign of X, sign of Y, where they come from) for the video's axes: +1 X when image x grows with stage X, +1 Y
    when image up is stage +Y. Measured for this objective and camera (video_scale), else learned from another
    objective's measurement on the same camera (the directions belong to the camera, not the lens), else the profile's
    video.image_x_right / image_y_up when a person set them, else assumed (X right, Y up). On the XploRA's top camera
    image up is stage -Y (2026-11-23): the assumption put a particle at y = +153 µm instead of -155."""
    store = scale_store()
    sgn = lambda v: 1 if float(v) > 0 else -1      # noqa: E731
    cal = store.get(f"{objective}@{camera}")
    if cal and cal.get("sx") and cal.get("ty"):
        return sgn(cal["sx"]), sgn(cal["ty"]), "measured"
    for k, c in sorted(store.items()):
        if _same_camera(k, camera) and c.get("sx") and c.get("ty"):
            return sgn(c["sx"]), sgn(c["ty"]), f"learned from {k} (same camera)"
    v = prof.get("video", {}) or {}
    if "image_x_right" in v and "image_y_up" in v:
        return (1 if v["image_x_right"] else -1), (1 if v["image_y_up"] else -1), \
            "profile video.image_x_right / image_y_up"
    return (1 if v.get("image_x_right", True) else -1), (1 if v.get("image_y_up", True) else -1), "assumed"


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
    field = field_um(prof, objective)
    if not field:
        return None
    w, h = dec["width"], dec["height"]
    gx, gy, src = axes_directions(prof, objective, camera)
    sx = float(field[0]) / w * gx
    sy = float(field[1]) / h * gy
    d = dict(dec)
    d["xs"] = x + (np.arange(w) - (w - 1) / 2) * sx
    d["ys"] = y - (np.arange(h) - (h - 1) / 2) * sy        # row 0 at the top: the largest Y when Y grows upward
    d["um"] = True
    d["axes_from"] = "the objective's field of view (OLConfig.ini) around the stage position"
    d["directions_from"] = src
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
    return (0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]) if img.ndim == 3 else img


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
           feats: list | None = None, marker_label: str = "") -> dict:
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
        if marker_label:            # 'assumed': no spot study has measured where the beam is
            ax.annotate(marker_label, (mx, my), xytext=(8, -14), textcoords="offset points", color="#ff2d2d",
                        fontsize=8, weight="bold")
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
    from . import store
    if new is not None:
        try:
            return store.write("video_orientation.json", new)
        except OSError:
            return new
    return store.read("video_orientation.json")


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
    ori, why = orientation_for(dec, nat, res.get("camera"))
    if why:
        out["warnings"].append(why)
    out["orientation"] = ori
    dec = apply_orientation(dec, ori)
    if res.get("illumination") or res.get("illumination_light"):
        out["illumination"] = {"illuminator": res.get("illumination"), "light": res.get("illumination_light")}
        why = illumination_mismatch(res.get("illumination"), res.get("illumination_light"))
        if why:
            out["warnings"].append(why)
    if not (float(res.get("laser_s") or 0) > 0 or float(res.get("during_acq_s") or 0) > 0):
        out["brightness"], why = brightness_check(dec, res.get("objective"), res.get("camera"))
        if why:
            out["warnings"].append(why)
    labspec_span = [float(abs(dec["xs"][-1] - dec["xs"][0])), float(abs(dec["ys"][-1] - dec["ys"][0]))] \
        if len(dec["xs"]) > 1 else None
    sa = stage_axes(dec, prof, res.get("objective"), x, y, res.get("camera"))
    if sa is not None:
        dec = sa
        out["axes_from"] = sa["axes_from"]
        out["directions_from"] = sa.get("directions_from", "measured")
        out["labspec_axes_span"] = labspec_span
        if out["directions_from"] == "assumed":
            out["warnings"].append(
                f"the X and Y directions are assumed (X right, Y up): no video-scale has run for this camera, and on "
                f"the XploRA's top camera image up is stage -Y. Positions may be mirrored; move.to_um from this "
                f"snapshot is refused. Run `ls6t instrument video-scale` with objective {res.get('objective')!r}")
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
        measured = src.startswith("measured")
        out["marker_measured"] = measured
        out["marker_source"] = (f"the beam, measured: stage position + offset ({src})" if measured else
                                "ASSUMED: the stage position" + (f" + the profile's offset ({src})" if any(off) else
                                                                 " (the image centre)") +
                                "; not where the beam was measured: run a 'spot' study before aiming at it")
        note = ("marker: the beam (measured offset)" if measured else
                "marker: ASSUMED beam position, not measured (run a 'spot' study)")
    else:
        if dec["um"]:
            marker = (float(np.mean(dec["xs"])), float(np.mean(dec["ys"])))
        else:
            marker = (dec["width"] / 2, dec["height"] / 2)
        note = "marker: image centre, ASSUMED beam position" + ("" if dec["um"] else " (video axes not in µm)")
        out["marker_measured"] = False
        out["marker_source"] = "ASSUMED: the image centre (profile video.marker 'center')"
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
        out.update(render(dec, Path(f"{base}.marked.png"), marker, title, note, spot, feats,
                          "" if out.get("marker_measured") else "assumed"))
    except ImportError:
        out["warnings"].append("matplotlib is missing (pip install labspec-translator[render]): no PNG")
    out.update(marker_um=list(marker) if marker and dec["um"] else None, um_axes=dec["um"],
               field_um=[float(abs(dec["xs"][-1] - dec["xs"][0])), float(abs(dec["ys"][-1] - dec["ys"][0]))]
               if dec["um"] else None, pixels=[dec["width"], dec["height"]],
               what="video frame; red cross: the laser position; yellow numbered circles: small features")
    return out


def _f(v):
    return to_float(v, finite=False)
