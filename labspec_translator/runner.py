"""runner.py — runs a request's studies on the instrument, one command at a time, and checks every result.
Version: 2026.11.19.3

A request runs in its own process (`ls6t run-request FOLDER`, started detached by api.start), so it outlives the
MCP call that started it. One request at a time holds the instrument (a lock file under LS6T_HOME); others wait
as 'queued'. Everything is written to the request folder: state.json, runner.json (the runner's heartbeat), and per
study progress.json, the spectra and <name>.json (the result, written atomically).
"""
from __future__ import annotations

import json
import math
import os
import time
import traceback
from pathlib import Path

import numpy as np

from . import __version__, BRIDGE_PROTOCOL, config, plan, spectra, video
from . import profile as P
from .bridge import Bridge, BridgeError, BridgeGone, BridgeTimeout

FINAL = ("done", "cancelled", "failed", "interrupted")


class Stop(Exception):
    """The request ends here: cancelled, stopped by the operator, declined, or the bridge is gone."""

    def __init__(self, why: str, by: str):
        super().__init__(why)
        self.why, self.by = why, by


# --------------------------------------------------------------------------------------------- files

def write_json(path: Path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_jsonable), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _jsonable(o):
    if isinstance(o, (np.floating,)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return [_jsonable(v) if isinstance(v, (np.floating, np.integer)) else v for v in o.tolist()]
    if isinstance(o, Path):
        return str(o)
    return str(o)


def clean(v):
    """NaN and inf as None, recursively (JSON has neither)."""
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if not str(k).startswith("_")}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, (float, np.floating)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, np.integer):
        return int(v)
    return v


def motor_args(prof: dict) -> dict:
    """The profile's LabSpec motor names, as the bridge's status, move_xy, autofocus and safe verbs take them."""
    return {f"m_{k}": v for k, v in prof["motors"].items()}


def lock_path() -> Path:
    return (config.out_dir() / "sim_instrument.lock") if config.mode() == "sim" else (config.home() / "instrument.lock")


# --------------------------------------------------------------------------------------------- the request

class Ctx:
    def __init__(self, folder: Path, bridge: Bridge, prof: dict):
        self.folder = folder
        self.bridge = bridge
        self.prof = prof
        self.start: dict = {}
        self.confirmed = False
        self.last_beat = 0.0
        self.bridge_version = None

    def cancelled(self) -> bool:
        return (self.folder / "cancel").exists()

    def beat(self, force: bool = False):
        now = time.time()
        if force or now - self.last_beat > 3:
            self.last_beat = now
            write_json(self.folder / "runner.json", {"pid": os.getpid(), "time": now, "version": __version__})
            try:
                os.utime(lock_path(), None)
            except OSError:
                pass

    def call(self, verb: str, timeout_s: float = 60.0, **params) -> dict:
        try:
            r = self.bridge.call(verb, timeout_s=timeout_s, should_stop=self.cancelled, on_wait=self.beat, **params)
        except BridgeGone as exc:
            raise Stop(str(exc), "bridge") from exc
        except BridgeTimeout as exc:
            raise Stop(str(exc), "timeout") from exc
        self.beat()
        if not r["ok"]:
            if r.get("stopped_by") == "operator" or r.get("halted") == "1":
                raise Stop("stopped by the operator in LabSpec", "operator")
            if r.get("stopped_by") == "request" or self.cancelled():
                raise Stop("cancelled", "cancel")
        return r


def _acquire_lock(folder: Path, ctx_beat) -> None:
    lp = lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps({"request": str(folder), "pid": os.getpid(), "time": time.time()}))
            return
        except FileExistsError:
            try:
                age = time.time() - lp.stat().st_mtime
                holder = read_json(lp, {}).get("request")
            except OSError:
                continue
            if age > 90 or holder == str(folder):
                try:
                    lp.unlink()
                except OSError:
                    pass
                continue
            write_json(folder / "state.json", {"state": "queued", "reason": f"the instrument is busy with {holder}",
                                               "time": time.time()})
            if (folder / "cancel").exists():
                raise Stop("cancelled while queued", "cancel")
            ctx_beat()
            time.sleep(2)


def _release_lock(folder: Path):
    lp = lock_path()
    if read_json(lp, {}).get("request") == str(folder):
        try:
            lp.unlink()
        except OSError:
            pass


def run_request(folder: Path | str) -> dict:
    """Runs every study of a request folder (created by api.start) in order; returns the final state."""
    folder = Path(folder)
    studies = read_json(folder / "studies.json", {})
    req = read_json(folder / "request.json", {})
    prof = P.load()
    started = time.time()
    state = {"state": "running", "started": started, "pid": os.getpid()}
    bridge_dir = Path(req.get("bridge_dir") or config.active_bridge_dir())
    sim = None
    ctx = None
    try:
        ctx = Ctx(folder, Bridge(bridge_dir), prof)
        ctx.beat(force=True)
        _acquire_lock(folder, ctx.beat)
        write_json(folder / "state.json", state)
        if req.get("mode", config.mode()) == "sim":
            from . import sim as S
            sim = S.ensure(bridge_dir)
        ping = ctx.call("ping", timeout_s=15)
        ctx.bridge_version = ping.get("version")
        if str(ping.get("protocol")) != str(BRIDGE_PROTOCOL):
            raise Stop(f"the bridge speaks protocol {ping.get('protocol')}, this ls6t {BRIDGE_PROTOCOL}: install the "
                       "bridge script of this version (ls6t bridge install)", "bridge")
        if ping.get("halted") == "1":
            raise Stop("the bridge is halted by the operator: resume it in LabSpec first", "operator")
        st = ctx.call("status", timeout_s=15, **motor_args(prof))
        ctx.start = {k: _f(st.get(k)) for k in ("x_um", "y_um", "z_um")}
        ctx.start["laser_nm"] = _f(st.get("laser_nm"))
        write_json(folder / "start.json", {"status": st, "time": time.time()})
        ctx.call("message", text=f"ls6t: request {folder.name} started ({len(studies)} studies)")
        stopped = None
        for name, study in studies.items():
            if stopped:
                write_json(folder / name / f"{name}.json",
                           {"ok": False, "why": f"not run: {stopped.why}", "reliable": False,
                            "unreliable_because": [stopped.why], "warnings": []})
                continue
            try:
                run_any(name, study, ctx)
            except Stop as exc:
                res = folder / name / f"{name}.json"
                if not res.exists():
                    write_json(res, {"ok": False, "why": f"stopped before measuring: {exc.why}", "reliable": False,
                                     "unreliable_because": [exc.why], "warnings": [], "complete": False,
                                     "points_measured": 0})
                    _progress(folder / name, state="stopped", stage="done")
                if exc.by != "study":      # a study's own problem does not stop the next studies
                    stopped = exc
        state.update(state="cancelled" if stopped else "done", finished=time.time(),
                     why=stopped.why if stopped else None, stopped_by=stopped.by if stopped else None)
    except Stop as exc:
        state.update(state="cancelled" if exc.by in ("cancel", "operator") else "failed", why=exc.why,
                     stopped_by=exc.by, finished=time.time())
        for name in studies:
            res = folder / name / f"{name}.json"
            if not res.exists():
                (folder / name).mkdir(exist_ok=True)
                write_json(res, {"ok": False, "why": f"not run: {exc.why}", "reliable": False,
                                 "unreliable_because": [exc.why], "warnings": []})
    except Exception as exc:  # noqa: BLE001
        state.update(state="failed", why=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-3000:],
                     finished=time.time())
        for name in studies:
            res = folder / name / f"{name}.json"
            if not res.exists():
                (folder / name).mkdir(exist_ok=True)
                write_json(res, {"ok": False, "why": f"runner failed: {type(exc).__name__}: {exc}",
                                 "reliable": False, "unreliable_because": ["runner failed"], "warnings": []})
    finally:
        if ctx is not None:
            try:
                if ctx.bridge.alive():
                    ctx.bridge.call("safe", timeout_s=15, **motor_args(prof))
                    ctx.bridge.call("message", timeout_s=10,
                                    text=f"ls6t: request {folder.name} {state.get('state')}")
            except (BridgeError, OSError):
                pass
        _release_lock(folder)
        state["seconds"] = round(time.time() - started, 1)
        write_json(folder / "state.json", state)
        if sim is not None and req.get("stop_sim", True):
            sim.stop()
    return state


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------- one study

def _progress(sd: Path, **kw):
    cur = read_json(sd / "progress.json", {})
    cur.update(kw, time=time.time())
    write_json(sd / "progress.json", cur)


def _setup(name: str, study: dict, ctx: Ctx, warnings: list, unreliable: list, checks: dict,
           n_points: int) -> dict:
    """The set-up moves with read-back, the detector check, and the operator's go-ahead before the first exposure.
    Returns {before, after, laser, temp, readback, rng}."""
    prof = ctx.prof
    kind = study.get("kind", "spectrum")
    before = ctx.call("status", timeout_s=15, **motor_args(prof))
    readback = []
    for step in plan.setup_steps(study, prof):
        p = {k: v for k, v in step.items() if k not in ("verb", "why")}
        long_move = p["motor"] in (prof["motors"]["laser"], prof["motors"]["grating"], prof["motors"]["objective"])
        r = ctx.call("move", timeout_s=600 if long_move else 120, **p)
        ok, got = _matches(step, r)
        readback.append({"motor": step["motor"], "asked": step["value"], "got": r.get("label") or r.get("value"),
                         "ok": bool(r["ok"] and ok), "error": r.get("error") or None})
        if not r["ok"] or not ok:
            unreliable.append(f"{step['why']}: asked {step['value']}, " +
                              (f"LabSpec said: {r.get('error')}" if not r["ok"] else f"got {got}"))
    after = ctx.call("status", timeout_s=15, **motor_args(prof))
    laser = _f(after.get("laser_nm")) or plan.laser_nm(study, prof) or ctx.start.get("laser_nm")
    temp = _f(after.get("detector_temp_c"))
    checks["detector_temp_c"] = {"value": temp, "max": prof["detector"]["max_temp_c"],
                                 "pass": temp is not None and temp <= prof["detector"]["max_temp_c"]}
    if temp is None:
        warnings.append("the detector temperature could not be read")
    elif temp > prof["detector"]["max_temp_c"]:
        unreliable.append(f"detector at {temp:g} C, warmer than {prof['detector']['max_temp_c']:g} C: let it cool")
    checks["setup"] = {"pass": all(r["ok"] for r in readback), "moves": readback}
    rng = plan.acq_range(study, prof, laser)
    if rng.get("from_nm") is None:
        raise Stop("the laser line is unknown: give setup.laser_nm", "study")
    if kind not in ("alignment", "guide", "calibration"):
        readiness_warnings(prof, laser, warnings)

    # ---- the operator's go-ahead
    policy = config.confirm_policy()
    if policy == "study" or (policy == "request" and not ctx.confirmed):
        what = (f"ls6t is about to expose '{study.get('sample', {}).get('name', name)}' ({kind}, {n_points} "
                f"point(s)) with laser {laser:g} nm, filter {after.get('filter')}, objective {after.get('objective')}."
                f" Continue?")
        r = ctx.call("confirm", timeout_s=3600, text=what)
        if r.get("answer") != "yes":
            raise Stop("the operator declined in LabSpec", "operator")
        ctx.confirmed = True
    return {"before": before, "after": after, "laser": laser, "temp": temp, "readback": readback, "rng": rng}


def run_any(name: str, study: dict, ctx: Ctx) -> dict:
    kind = study.get("kind", "spectrum")
    if kind == "guide":
        return run_guide(name, study, ctx)
    if kind == "alignment":
        return run_alignment(name, study, ctx)
    return run_study(name, study, ctx)


def run_study(name: str, study: dict, ctx: Ctx, sd: Path | None = None) -> dict:
    prof = ctx.prof
    sd = sd or ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    kind = study.get("kind", "spectrum")
    pts = plan.positions(study)
    _progress(sd, state="running", stage="setup", point=0, points=len(pts), started=t_start,
              estimate_min=plan.estimate(study, prof)["minutes"])
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    if not prof.get("measured"):
        warnings.append("no machine profile: built-in XploRA defaults (run the discovery script)")

    su = _setup(name, study, ctx, warnings, unreliable, checks, len(pts))
    before, after, laser, temp, readback, rng = (su[k] for k in ("before", "after", "laser", "temp", "readback",
                                                                 "rng"))

    # ---- points
    acq = plan.acquire_step(study, prof, rng)
    focus = study.get("focus", {})
    af = focus.get("autofocus")
    every = int(focus.get("every", 0) or 0)
    x0, y0 = ctx.start.get("x_um"), ctx.start.get("y_um")
    zref = ctx.start.get("z_um")          # the operator's focus when the request started
    zcur = _f(before.get("z_um"))
    cur_xy = (_f(before.get("x_um")), _f(before.get("y_um")))
    stage = prof.get("stage", {})
    per_point = []
    t_series0 = None
    view = None
    native = study.get("save_native", True)
    stop_exc = None
    t_acq = 0.0
    vd = study.get("video") or {}
    want_before = vd.get("before", kind == "calibration")
    snaps: dict = {}
    for k, p in enumerate(pts):
        if ctx.cancelled():
            stop_exc = Stop("cancelled", "cancel")
            break
        try:
            _progress(sd, stage="measuring", point=k, points=len(pts),
                      eta_min=_eta(t_start, k, len(pts), study, prof))
            if kind == "series" and k > 0:
                wait_until = t_series0 + k * float(study["series"].get("interval_s", 0))
                while time.time() < wait_until:
                    if ctx.cancelled():
                        raise Stop("cancelled", "cancel")
                    ctx.beat()
                    time.sleep(min(0.5, max(0.0, wait_until - time.time())))
            ax, ay = cur_xy
            if x0 is None or y0 is None:
                if p["x_um"] or p["y_um"]:
                    raise Stop("the stage position could not be read: XY moves are refused", "study")
            elif cur_xy[0] is None or cur_xy[1] is None or \
                    math.hypot(x0 + p["x_um"] - cur_xy[0], y0 + p["y_um"] - cur_xy[1]) > 0.01:
                ax, ay = x0 + p["x_um"], y0 + p["y_um"]
                for val, lim_key in ((ax, "x_um"), (ay, "y_um")):
                    lim = stage.get(lim_key)
                    if lim and not lim[0] <= val <= lim[1]:
                        raise Stop(f"point {k}: stage {lim_key[0].upper()} = {val:g} µm is outside the profile's "
                                   f"stage limits {lim}", "study")
                r = ctx.call("move_xy", timeout_s=180, x_um=round(ax, 3), y_um=round(ay, 3),
                             **motor_args(prof))
                if not r["ok"]:
                    raise Stop(f"point {k}: XY move failed: {r.get('error')}", "study")
                cur_xy = (_f(r.get("x_um")) if _f(r.get("x_um")) is not None else ax,
                          _f(r.get("y_um")) if _f(r.get("y_um")) is not None else ay)
                ax, ay = cur_xy
            if af and (k == 0 or (every and k % every == 0)):
                r = ctx.call("autofocus", timeout_s=240, how=af, **motor_args(prof))
                if r["ok"] and _f(r.get("z_um")) is not None:
                    zref = zcur = _f(r.get("z_um"))
                else:
                    warnings.append(f"point {k}: autofocus failed ({r.get('error')}); kept the previous focus")
            if p["dz_um"] and zref is None:
                raise Stop("the Z position could not be read: Z offsets are refused", "study")
            if zref is not None:
                target = zref + p["dz_um"]       # Z is always relative to the operator's (or autofocus') focus
                if zcur is None or abs(target - zcur) > 1e-6:
                    r = ctx.call("move", timeout_s=120, motor=prof["motors"]["z"], mode="value",
                                 value=round(target, 3))
                    if not r["ok"]:
                        raise Stop(f"point {k}: Z move failed: {r.get('error')}", "study")
                    zcur = target
            if k == 0 and want_before:
                snaps["before"] = _snapshot(ctx, name, sd, "before", bool(vd.get("with_laser")), warnings)
            out = sd / "spectra" / f"p{k:05d}.txt"
            params = dict(acq)
            params.pop("verb", None)
            params["out"] = str(out)
            params["sample"] = study.get("sample", {}).get("name", name)
            if native and kind not in ("map", "series"):
                params["native"] = str(sd / "spectra" / f"p{k:05d}.l6s")
            if kind == "series" and k == 0:
                t_series0 = time.time()
            t_a = time.time()
            r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
            t_acq += time.time() - t_a
            if not r["ok"]:
                raise Stop(f"point {k}: acquisition failed: {r.get('error')}", "study")
            rec = {"point": k, "offset": {"x_um": p["x_um"], "y_um": p["y_um"], "dz_um": p["dz_um"]},
                   "stage": {"x_um": ax, "y_um": ay, "z_um": zcur}, "file": str(out),
                   "native": params.get("native"), "id": r.get("id"), "t": round(time.time() - t_start, 3),
                   "exposure_s": _f(r.get("exposure_s")), "accumulations": _f(r.get("accumulations")),
                   "detector_temp_c": _f(r.get("detector_temp_c"))}
            per_point.append(rec)
            # live view in LabSpec: the map or the series grows as it is measured
            if kind in ("map", "series") and r.get("id"):
                try:
                    if view is None:
                        vr = ctx.call("view", timeout_s=30, action="create", name=name, vfrom=rng.get("from_nm"),
                                      vto=rng.get("to_nm"),
                                      kind="map" if kind == "map" else "profile", id=r["id"],
                                      x=p["x_um"], y=p["y_um"], value=rec["t"],
                                      label="time" if kind == "series" else "XY", unit="s" if kind == "series" else "um")
                        view = vr.get("view") if vr["ok"] else False
                        if not vr["ok"]:
                            warnings.append(f"live view in LabSpec not available: {vr.get('error')}")
                    elif view:
                        vr = ctx.call("view", timeout_s=30, action="add", name=name, id=r["id"],
                                      x=p["x_um"], y=p["y_um"], value=rec["t"], vfrom=rng.get("from_nm"),
                                      vto=rng.get("to_nm"))
                        if not vr["ok"]:
                            warnings.append(f"live view: {vr.get('error')}")
                            view = False
                except Stop:
                    raise
        except Stop as exc:
            stop_exc = exc
            break
    if vd.get("after") and per_point and stop_exc is None:
        try:
            snaps["after"] = _snapshot(ctx, name, sd, "after", bool(vd.get("with_laser")), warnings)
        except Stop as exc:
            stop_exc = exc
    if view and native:
        try:
            vr = ctx.call("view", timeout_s=60, action="save", name=name, path=str(sd / f"{name}.l6s"))
            if not vr["ok"]:
                warnings.append(f"LabSpec could not save the {kind} file: {vr.get('error')}")
        except Stop as exc:
            stop_exc = stop_exc or exc

    # ---- analysis and checks
    t_an = time.time()
    _progress(sd, stage="analysing")
    result = analyse(name, study, per_point, prof, laser, rng, sd, warnings, unreliable, checks)
    if stop_exc is not None:
        if stop_exc.by in ("cancel", "operator", "bridge", "timeout"):
            unreliable.append(f"stopped after {len(per_point)} of {len(pts)} points: {stop_exc.why}")
        else:
            unreliable.append(stop_exc.why)
    result.update(
        ok=bool(per_point), complete=len(per_point) == len(pts) and stop_exc is None,
        reliable=bool(per_point) and not unreliable, unreliable_because=unreliable, warnings=warnings,
        checks=checks, kind=kind, sample=study.get("sample", {}),
        setup={"requested": study.get("setup", {}), "before": before, "after": after, "moves": readback},
        instrument={"laser_nm": laser, "detector_temp_c": temp, "system": after.get("system"),
                    "filter": after.get("filter"), "objective": after.get("objective"),
                    "grating": _f(after.get("grating"))},
        range=rng, points_measured=len(per_point), points_planned=len(pts),
        seconds={"total": round(time.time() - t_start, 2), "acquisition": round(t_acq, 2),
                 "analysis": round(time.time() - t_an, 2)},
        execution={"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                   "protocol": BRIDGE_PROTOCOL})
    if not per_point:
        result["why"] = stop_exc.why if stop_exc else "no spectrum was measured"
    result["expect"] = evaluate_expect(study.get("expect", []), result)
    if any(e.get("pass") is False for e in result["expect"]):
        warnings.append("an expected value is out of tolerance (see 'expect')")
    if study.get("images") and per_point:
        try:
            from . import render
            result["images"] = render.study_images(name, study, result, sd)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"images not drawn: {type(exc).__name__}: {exc}")
    snaps = {k: v for k, v in snaps.items() if v}
    if snaps:
        result["video"] = snaps
        imgs = result.setdefault("images", {})
        for tag, sn in snaps.items():
            if sn.get("path"):
                imgs[f"video_{tag}"] = {"path": sn["path"], "what": sn.get("what")}
    result = clean(result)
    write_json(sd / f"{name}.json", result)
    _progress(sd, state="done" if stop_exc is None else "stopped", stage="done", point=len(per_point))
    if stop_exc is not None and stop_exc.by in ("cancel", "operator", "bridge", "timeout"):
        raise stop_exc
    return result


# --------------------------------------------------------------------------------------------- guided adjustment

def _finish(sd: Path, name: str, result: dict) -> dict:
    result = clean(result)
    write_json(sd / f"{name}.json", result)
    return result


def run_guide(name: str, study: dict, ctx: Ctx, sd: Path | None = None) -> dict:
    """The operator adjusts something by hand; the bridge measures the line every fraction of a second, shows it in
    LabSpec and says STOP when the goal is met. The fast loop runs in the bridge: no round trip through Python or an
    LLM between readings."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = sd or ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    _progress(sd, state="running", stage="setup", point=0, points=1, started=t_start,
              estimate_min=plan.estimate(study, prof)["minutes"])
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    laser = su["laser"]
    g = plan.guide_params(study, prof)
    line, win = float(g["line_cm1"]), float(g["window_cm1"])
    center_nm = round(shift_to_nm(float(g.get("center_cm1", line)), laser), 4)
    sat = float(prof["guide"].get("saturation_frac", 0.9)) * prof["detector"]["full_scale"]
    _progress(sd, stage="guiding", eta_min=round(float(g["max_s"]) / 60.0, 2))
    r = ctx.call("guide", timeout_s=float(g["max_s"]) + 300, text=g["instruction"], mode=g["mode"],
                 target=g.get("target", 0), from_nm=center_nm, to_nm=center_nm,
                 lo_nm=round(shift_to_nm(line - win / 2, laser), 4), hi_nm=round(shift_to_nm(line + win / 2, laser), 4),
                 lo_cm1=line - win / 2, hi_cm1=line + win / 2, exposure_s=g["exposure_s"], max_s=g["max_s"],
                 stable_n=g["stable_n"], drop_frac=g["drop_frac"], sat=round(sat, 1),
                 out=str(sd / "guide_history.txt"), ui=prof["guide"].get("ui", "dialog"), unit="counts/s")
    hist = []
    try:
        from .profile import read_text
        for line_ in read_text(sd / "guide_history.txt").splitlines():
            if line_.startswith("#") or not line_.strip():
                continue
            t_, v_, y_ = (float(x) for x in line_.split(",")[:3])
            hist.append([t_, v_, y_])
    except (OSError, ValueError):
        pass
    reason = r.get("reason", "")
    good = reason in ("target", "past_max", "operator_done")
    if reason == "saturation":
        unreliable.append("the detector came near saturation: reduce the power, or a stronger filter")
    elif reason == "timeout":
        unreliable.append(f"the goal was not met within {g['max_s']:g} s")
    elif reason in ("operator_abort", "stopped"):
        unreliable.append("the operator or ls6t stopped the adjustment")
    elif not r["ok"] and not reason:
        unreliable.append(f"the guided loop failed: {r.get('error')}")
    if r.get("ui_fallback"):
        warnings.append(f"LabSpec's dialog was not available ({r['ui_fallback']}): the status bar was used")
    guide = {"instruction": g["instruction"], "mode": g["mode"], "target": g.get("target"), "line_cm1": line,
             "reason": reason, "message": r.get("message"), "value": _f(r.get("value")),
             "max_value": _f(r.get("max_value")), "t_max_s": _f(r.get("t_max_s")),
             "iterations": int(_f(r.get("iterations")) or 0), "period_s": _f(r.get("period_s")),
             "unit": "counts/s (line height above its window's edges, per second)",
             "history": str(sd / "guide_history.txt"), "points": len(hist)}
    result = {"ok": guide["iterations"] > 0, "complete": good, "reliable": good and not unreliable,
              "unreliable_because": unreliable, "warnings": warnings, "checks": checks, "kind": "guide",
              "sample": study.get("sample", {}), "guide": guide,
              "setup": {"requested": study.get("setup", {}), "before": su["before"], "after": su["after"],
                        "moves": su["readback"]},
              "instrument": {"laser_nm": laser, "detector_temp_c": su["temp"], "filter": su["after"].get("filter"),
                             "objective": su["after"].get("objective")},
              "seconds": {"total": round(time.time() - t_start, 2)},
              "execution": {"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                            "protocol": BRIDGE_PROTOCOL}}
    if hist:
        try:
            from . import render
            result["images"] = {"guide": render.guide_png(sd / "guide.png", hist, guide, name)}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"guide plot not drawn: {type(exc).__name__}: {exc}")
    result = _finish(sd, name, result)
    _progress(sd, state="done", stage="done")
    if r.get("stopped_by") in ("operator", "request") or reason == "stopped":
        raise Stop("stopped by the operator in LabSpec" if r.get("stopped_by") == "operator" else "cancelled",
                   "operator" if r.get("stopped_by") == "operator" else "cancel")
    return result


# --------------------------------------------------------------------------------------------- alignment

CHECKLIST = ("ls6t: the alignment check failed for the {laser:g} nm laser ({ratio}).\n\n"
             "Please run AutoAlignment now: LabSpec > Maintenance tab > AutoAlignment > Start, and follow its "
             "messages. Its checklist: the HORIBA reference Si sample; the laser at full power at the source; the "
             "x100 objective; the laser focused on the Si; no dust or oil on the sample or objective.\n\n"
             "Press OK here when AutoAlignment has finished (ls6t then checks again), or Cancel to stop.")


def alignment_metrics(res: dict, prof: dict, laser: float | None) -> dict:
    """From a Si check's result: counts/s of the Si line, its width and position, compared with the laser's
    baseline. pass is None when there is no baseline to compare with."""
    from .profile import laser_key
    a = prof.get("alignment", {})
    lk = laser_key(prof, laser) if laser else None
    pk = (res.get("peaks_by_name") or {}).get("Si") or {}
    sp = (res.get("spectra") or [{}])[0]
    ex = _f(sp.get("exposure_s")) or _f((res.get("setup", {}).get("requested", {}) or {}).get("exposure_s"))
    m = {"laser_nm": laser, "laser_key": lk, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "filter": (res.get("instrument") or {}).get("filter"), "position_cm1": pk.get("position_cm1"),
         "fwhm_cm1": pk.get("fwhm_cm1"), "height": pk.get("height"), "exposure_s": ex,
         "counts_per_s": round(pk["height"] / ex, 2) if pk.get("ok") and ex else None,
         "measured": bool(pk.get("ok")), "reliable": bool(res.get("reliable"))}
    base = a.get("baselines", {}).get(lk or "")
    m["baseline"] = base
    if not m["measured"]:
        m.update(passed=False, why="no Si line: check the sample is the Si reference and in focus")
        return m
    if not base:
        m.update(passed=None, why="no baseline for this laser: the maintainer records one after AutoAlignment "
                                  "(`ls6t alignment record`)")
        return m
    cps = m["counts_per_s"]
    from .profile import filter_pct
    f_now, f_base = prof.get("filters", {}).get(m["filter"]), prof.get("filters", {}).get(base.get("filter"))
    f_now = f_now if f_now is not None else filter_pct(m["filter"] or "")
    f_base = f_base if f_base is not None else filter_pct(base.get("filter") or "")
    if f_now and f_base and abs(f_now - f_base) > 1e-9:
        cps = cps * f_base / f_now
        m["scaled_to_baseline_filter"] = base.get("filter")
    ratio = cps / base["counts_per_s"] if base.get("counts_per_s") else None
    wratio = m["fwhm_cm1"] / base["fwhm_cm1"] if base.get("fwhm_cm1") and m["fwhm_cm1"] else None
    ok = ratio is not None and ratio >= float(a.get("min_ratio", 0.7)) and \
        (wratio is None or wratio <= float(a.get("max_fwhm_ratio", 1.3)))
    m.update(ratio=round(ratio, 3) if ratio is not None else None,
             fwhm_ratio=round(wratio, 3) if wratio is not None else None, passed=bool(ok),
             why=None if ok else (f"Si signal at {ratio:.0%} of the baseline (minimum {a.get('min_ratio', 0.7):.0%})"
                                  if ratio is not None and ratio < float(a.get("min_ratio", 0.7)) else
                                  f"Si line {wratio:.2f}x wider than the baseline"))
    return m


def log_alignment(entry: dict):
    p = config.alignment_log()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(clean(entry), default=_jsonable) + "\n")


def alignment_history(laser_key_: str | None = None) -> list:
    out = []
    try:
        for line in config.alignment_log().read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if laser_key_ is None or e.get("laser_key") == laser_key_:
                out.append(e)
    except OSError:
        pass
    return out


def readiness_warnings(prof: dict, laser: float | None, warnings: list):
    """Once the maintainer has recorded alignment baselines: warn when this laser's last check failed, is missing,
    or is older than alignment.max_age_h."""
    from .profile import laser_key
    a = prof.get("alignment", {})
    if not a.get("baselines") or not laser:
        return
    lk = laser_key(prof, laser)
    hist = alignment_history(lk)
    if not hist:
        warnings.append(f"the {laser:g} nm laser has no alignment check on record: run an 'alignment' study first")
        return
    last = hist[-1]
    try:
        age_h = (time.time() - time.mktime(time.strptime(last["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600.0
    except (KeyError, ValueError):
        age_h = None
    if last.get("passed") is False:
        warnings.append(f"the last alignment check of the {laser:g} nm laser failed ({last.get('time')}): "
                        "results may be weak; run AutoAlignment and an 'alignment' study")
    elif age_h is not None and age_h > float(a.get("max_age_h", 24)):
        warnings.append(f"the last alignment check of the {laser:g} nm laser is {age_h:.0f} h old "
                        f"(more than {a.get('max_age_h', 24):g} h): run an 'alignment' study")


def run_alignment(name: str, study: dict, ctx: Ctx) -> dict:
    """The check (Si reference, the laser's fixed settings) against the baseline; if it fails and guided, the
    operator runs AutoAlignment in LabSpec (checklist; OK when done) and the check repeats; then a video snapshot
    (with the laser when the profile names the shutter) measures the beam's offset, which AutoAlignment changes."""
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    a = study.get("alignment", {})
    guided = a.get("guided", True)
    warnings: list[str] = []
    chk = plan.alignment_check_study(study, prof)
    attempts, last, res = [], None, None
    focus = None
    if a.get("focus_guide"):
        gst = {"kind": "guide", "sample": chk["sample"], "setup": chk["setup"],
               "guide": {"instruction": "Focus on the reference Si: turn the focus slowly through the best position",
                         "mode": "maximize", "exposure_s": 0.2, "max_s": 120}}
        try:
            focus = run_guide(f"{name}_focus", gst, ctx, sd / "focus")
        except Stop:
            raise
        if focus.get("guide", {}).get("reason") not in ("past_max", "operator_done", "target"):
            warnings.append(f"the focus guide ended with '{focus.get('guide', {}).get('reason')}'")
    n_max = 1 + (int(prof["alignment"].get("max_retries", 1)) if guided else 0)
    for attempt in range(1, n_max + 1):
        res = run_study(f"check{attempt}", chk, ctx, sd / f"check{attempt}")
        m = alignment_metrics(res, prof, (res.get("instrument") or {}).get("laser_nm"))
        m["attempt"] = attempt
        attempts.append(m)
        last = m
        if m["passed"] is not False or not guided or attempt == n_max:
            break
        txt = CHECKLIST.format(laser=m["laser_nm"] or 0, ratio=m.get("why") or "")
        ans = ctx.call("notice", timeout_s=7200, text=txt)
        if ans.get("answer") != "ok":
            warnings.append("the operator cancelled AutoAlignment: the check was not repeated")
            break
    for m in attempts:
        log_alignment({**{k: v for k, v in m.items() if k != "baseline"}, "request": str(ctx.folder), "study": name})
    # the beam's offset after (Auto)Alignment
    snap = None
    if res and res.get("ok"):
        snap = _snapshot(ctx, name, sd, "after", bool(prof["motors"].get("laser_shutter")), warnings)
    passed = last["passed"] if last else False
    rec = None
    if passed is False:
        rec = ("run AutoAlignment for this laser (LabSpec > Maintenance > AutoAlignment); if it fails twice, contact "
               "HORIBA service")
    elif passed is None:
        rec = "record a baseline right after a successful AutoAlignment: ls6t alignment record <request>"
    else:
        rec = "aligned; calibrate next (a 'calibration' study) if AutoAlignment ran"
    if snap and snap.get("measured_offset_um"):
        off = prof.get("video", {}).get("laser_offset_um", [0, 0])
        mo = snap["measured_offset_um"]
        if max(abs(mo[0] - off[0]), abs(mo[1] - off[1])) > 0.5:
            warnings.append(f"the beam is {mo} µm from LabSpec's laser position, the profile says {off}: the "
                            "maintainer updates it (ls6t profile set video.laser_offset_um \"[x, y]\")")
    result = dict(res or {})
    result.update(kind="alignment", ok=bool(res and res.get("ok")),
                  reliable=bool(res and res.get("reliable")) and passed is not False,
                  unreliable_because=list((res or {}).get("unreliable_because", [])) +
                  ([last["why"]] if passed is False and last and last.get("why") else []),
                  warnings=list((res or {}).get("warnings", [])) + warnings,
                  alignment={"passed": passed, "attempts": attempts, "recommendation": rec,
                             "guided": guided, "focus": (focus or {}).get("guide")},
                  seconds={"total": round(time.time() - t_start, 2)})
    if snap:
        result["video"] = {"after": snap}
        if snap.get("path"):
            result.setdefault("images", {})["video_after"] = {"path": snap["path"], "what": snap.get("what")}
    if focus and (focus.get("images") or {}).get("guide"):
        result.setdefault("images", {})["focus_guide"] = focus["images"]["guide"]
    return _finish(sd, name, result)


def _snapshot(ctx: Ctx, name: str, sd: Path, tag: str, with_laser: bool, warnings: list) -> dict | None:
    """A video frame now (LabSpec's live video; with_laser: the shutter opens for video.laser_s), drawn with the
    laser position marked. A failure is a warning, never a reason to stop the study."""
    v = ctx.prof.get("video", {})
    base = sd / "video" / tag
    base.parent.mkdir(parents=True, exist_ok=True)
    laser_s = float(v.get("laser_s", 1.0)) if with_laser else 0.0
    settle = float(v.get("settle_s", 1.5))
    r = ctx.call("video", timeout_s=60 + settle + laser_s, out=str(base), settle_s=settle, laser_s=laser_s,
                 formats=",".join(v.get("formats", [])), camera=v.get("camera"), **motor_args(ctx.prof))
    if not r["ok"]:
        warnings.append(f"video snapshot ({tag}) failed: {r.get('error')}")
        return None
    snap = video.snapshot(r, ctx.prof, base, f"{name}: {tag}")
    if with_laser and str(r.get("shutter_closed")) != "1":
        warnings.append("after the snapshot the laser shutter could not be confirmed closed: check LabSpec")
    for w in snap.get("warnings", []):
        warnings.append(f"video ({tag}): {w}")
    return snap


def _matches(step: dict, r: dict) -> tuple[bool, str]:
    if not r.get("ok"):
        return False, ""
    if step["mode"] == "label":
        got = r.get("label", "")
        return got == str(step["value"]), got
    got = _f(r.get("value"))
    return got is not None and abs(got - float(step["value"])) <= 0.5, str(got)


def _acq_timeout(study: dict, prof: dict, rng: dict) -> float:
    return 120.0 + 3.0 * plan.acquire_seconds(study, prof, rng)


def _eta(t0: float, done: int, total: int, study: dict, prof: dict) -> float | None:
    if done <= 0:
        return plan.estimate(study, prof)["minutes"]
    el = time.time() - t0
    return round(el / done * (total - done) / 60.0, 2)


# --------------------------------------------------------------------------------------------- analysis

def _stats(vals) -> dict:
    a = np.asarray([v for v in vals if v is not None and math.isfinite(v)], float)
    if not a.size:
        return {"n": 0}
    return {"n": int(a.size), "mean": float(a.mean()), "std": float(a.std()), "min": float(a.min()),
            "max": float(a.max())}


def analyse(name, study, per_point, prof, laser, rng, sd: Path, warnings, unreliable, checks) -> dict:
    kind = study.get("kind", "spectrum")
    process = dict(study.get("process", {}))
    if kind == "calibration" and not process.get("peaks"):
        process["peaks"] = [{"name": "Si", "near_cm1": prof["checks"]["si_cm1"], "window_cm1": 30}]
    fs = prof["detector"]["full_scale"]
    frac = prof["checks"]["saturation_frac"]
    min_snr = prof["checks"]["min_snr"]
    sat_points, spike_points, cover_short = [], [], []
    spike_mode = study.get("acquire", {}).get("spike", "labspec")
    axes, ys = [], []
    for rec in per_point:
        sp = spectra.read(rec["file"])
        try:
            shift, unit = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        except ValueError as exc:
            warnings.append(f"point {rec['point']}: {exc}")
            continue
        y = sp["y"]
        if sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s") and rec.get("exposure_s"):
            y_counts = y * rec["exposure_s"]
        else:
            y_counts = y
        sat = spectra.saturation(y_counts, fs, frac)
        a = spectra.analyze(shift, y, process, min_snr, prof["checks"].get("detect_snr", 5.0))
        rec.update(max_counts=sat["max_counts"], saturated=sat["saturated"], saturated_pixels=sat["pixels"],
                   noise_sigma=a["noise_sigma"], spikes=len(a["spikes"]), from_cm1=a["from_cm1"],
                   to_cm1=a["to_cm1"], peaks=a["peaks"], bands=a["bands"], auto_peaks=a["auto_peaks"])
        if sat["saturated"]:
            sat_points.append(rec["point"])
        if a["spikes"]:
            spike_points.append(rec["point"])
        lo, hi = rng.get("from_cm1"), rng.get("to_cm1")
        if lo is not None and hi is not None and not rng.get("single"):
            if a["from_cm1"] > lo + 2 or a["to_cm1"] < hi - 2:
                cover_short.append(rec["point"])
        axes.append(a["_x"])
        ys.append(a["_y"])
        if kind != "map":
            spectra.write(Path(rec["file"]).with_suffix(".shift.txt"), a["_x"], a["_y"],
                          {"x_unit": "1/cm", "y_unit": sp["meta"].get("y_unit", "cnt"), "laser_nm": laser})
    n = len(per_point)
    checks["saturation"] = {"pass": not sat_points, "points": sat_points[:50], "full_scale": fs, "threshold": fs * frac}
    if sat_points:
        unreliable.append(f"saturated in {len(sat_points)} of {n} spectra: choose a stronger filter or a shorter "
                          "exposure")
    checks["spikes"] = {"points": spike_points[:50], "count": len(spike_points)}
    if spike_points:
        warnings.append(f"cosmic-ray spikes left in {len(spike_points)} spectra"
                        + (" (spike removal is off: use 'double')" if spike_mode == "none" else ""))
    checks["range"] = {"pass": not cover_short, "points": cover_short[:50]}
    if cover_short:
        warnings.append(f"{len(cover_short)} spectra do not cover the whole range asked for")
    weak = [r["point"] for r in per_point for pk in r.get("peaks", [])
            if pk.get("ok") and pk.get("snr") is not None and pk["snr"] < min_snr]
    failed = [r["point"] for r in per_point for pk in r.get("peaks", [])
              if not pk.get("ok") and pk.get("detected") is not False]
    undetected = [r["point"] for r in per_point for pk in r.get("peaks", []) if pk.get("detected") is False]
    checks["snr"] = {"min_snr": min_snr, "weak_points": sorted(set(weak))[:50],
                     "not_detected_points": sorted(set(undetected))[:50]}
    if failed and kind != "map":
        warnings.append(f"a peak fit failed in {len(set(failed))} spectra (see peaks[].why)")
    if undetected and kind != "map" and process.get("peaks"):
        warnings.append(f"a requested peak was not detected in {len(set(undetected))} spectra (see peaks[].why)")

    out: dict = {"files": {"folder": str(sd)}}
    if kind == "map":
        out["map"] = _map_result(name, study, per_point, axes, ys, sd, process)
        out["files"]["map"] = out["map"]["files"]
        if failed:
            warnings.append(f"a peak fit failed at {len(set(failed))} of {n} map points (null in the map)")
    elif kind == "series":
        out["series"] = _series_result(per_point, process)
        out["spectra"] = [_point_summary(r) for r in per_point]
    else:
        out["spectra"] = [_point_summary(r) for r in per_point]
    if per_point and kind != "map":
        first = per_point[0]
        out["peaks"] = first.get("peaks", [])
        out["peaks_by_name"] = {pk["name"]: pk for pk in first.get("peaks", []) if pk.get("name")}
        out["bands"] = first.get("bands", {})
    if kind == "calibration" and per_point:
        pk = per_point[0]["peaks"][0] if per_point[0].get("peaks") else {}
        ref, tol = prof["checks"]["si_cm1"], prof["checks"]["si_tol_cm1"]
        if pk.get("ok"):
            off = pk["position_cm1"] - ref
            out["calibration"] = {"reference_cm1": ref, "measured_cm1": pk["position_cm1"],
                                  "offset_cm1": round(off, 4), "fwhm_cm1": pk["fwhm_cm1"], "tol_cm1": tol,
                                  "snr": pk.get("snr"), "pass": abs(off) <= tol}
            checks["calibration"] = {"pass": abs(off) <= tol, "offset_cm1": round(off, 4), "tol_cm1": tol}
            if abs(off) > tol:
                unreliable.append(f"the Si line is at {pk['position_cm1']:.2f} cm-1, {off:+.2f} from {ref}: "
                                  "calibrate the spectrometer in LabSpec (zero order and Si offset)")
        else:
            out["calibration"] = {"reference_cm1": ref, "pass": False, "why": pk.get("why", "no Si peak")}
            checks["calibration"] = {"pass": False}
            unreliable.append("no Si line found: check the sample is silicon and in focus")
    return out


def _point_summary(r: dict) -> dict:
    keys = ("point", "offset", "stage", "file", "native", "t", "exposure_s", "accumulations", "max_counts",
            "saturated", "noise_sigma", "spikes", "from_cm1", "to_cm1", "peaks", "bands")
    return {k: r.get(k) for k in keys}


def _quantities(per_point, process) -> dict:
    q: dict = {}
    for r in per_point:
        for i, pk in enumerate(r.get("peaks", []) or []):
            nm = pk.get("name") or f"peak{i}"
            for f in ("position_cm1", "fwhm_cm1", "height", "area", "snr"):
                q.setdefault(nm, {}).setdefault(f, []).append(pk.get(f) if pk.get("ok") else None)
        for bn, val in (r.get("bands") or {}).items():
            q.setdefault(bn, {}).setdefault("area", []).append(val)
        q.setdefault("max_counts", {}).setdefault("value", []).append(r.get("max_counts"))
    return q


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
                p = pos[r["point"]]
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
        idx = np.array([[pos[r["point"]]["ix"], pos[r["point"]]["iy"], pos[r["point"]]["iz"]] for r in per_point])
        npz = sd / f"{name}_map.npz"
        np.savez_compressed(npz, shift_cm1=ref, spectra=spec, index_xyz=idx, x_um=np.array(xs),
                            y_um=np.array(yv), z_um=np.array(zs),
                            **{k.replace(".", "__"): v for k, v in grids.items()})
        files["npz"] = str(npz)
    mj = sd / f"{name}_map.json"
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
