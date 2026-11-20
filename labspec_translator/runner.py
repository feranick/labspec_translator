"""runner.py — runs a request's studies on the instrument, one command at a time, and checks every result.
Version: 2026.11.19.8

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
from . import spot as SP
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
        self.home_xy: tuple = (None, None)   # where offsets start: the operator's spot, or where a 'move' stayed
        self.z_focus = None                 # the focus: the operator's, or the last autofocus / focus scan
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
        ctx.home_xy = (ctx.start["x_um"], ctx.start["y_um"])
        ctx.z_focus = ctx.start["z_um"]
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
        if not stopped:
            state["returned"] = _return_home(ctx)
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
           n_points: int, confirm: bool = True) -> dict:
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
    if confirm and (policy == "study" or (policy == "request" and not ctx.confirmed)):
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
    if kind == "move":
        return run_move(name, study, ctx)
    if kind == "focus":
        return run_focus(name, study, ctx)
    if kind == "spot":
        return run_spot(name, study, ctx)
    if kind == "calibration" and (study.get("calibration") or {}).get("adjust") == "labspec":
        return run_calibration(name, study, ctx)
    if kind == "background":
        return run_background(name, study, ctx)
    if kind == "depth":
        derived = plan.depth_as_map(study, ctx.prof)
        derived["_kind"] = "depth"
        derived["_depth"] = dict(study.get("depth", {}))
        return run_study(name, derived, ctx)
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
    x0, y0 = ctx.home_xy                  # the operator's spot (or where a 'move' study stayed)
    zref = ctx.z_focus                    # the operator's focus (or the last autofocus / focus scan)
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
    focus_scans: list = []
    snaps: dict = {}
    native = plan.map_engine(study, prof) == "native"
    if native:
        try:
            if af:
                if af == "scan":
                    fs = focus_scan(ctx, study, laser, zref if zref is not None else zcur, sd / "focus" / "start",
                                    warnings)
                    focus_scans.append(fs)
                    if fs.get("found"):
                        zref = zcur = ctx.z_focus = fs["z_um"]
                else:
                    r = ctx.call("autofocus", timeout_s=240, how=af, **motor_args(prof))
                    if r["ok"] and _f(r.get("z_um")) is not None:
                        zref = zcur = ctx.z_focus = _f(r.get("z_um"))
            if want_before:
                snaps["before"] = _snapshot(ctx, name, sd, "before", bool(vd.get("with_laser")), warnings, laser)
            t_a = time.time()
            per_point = _native_map(ctx, name, study, sd, rng, laser, acq, x0, y0, zref, warnings)
            t_acq += time.time() - t_a
        except Stop as exc:
            stop_exc = exc
    for k, p in enumerate(pts if not native else []):
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
                if af == "scan":
                    fs = focus_scan(ctx, study, laser, zref if zref is not None else zcur, sd / "focus" / f"p{k:05d}",
                                    warnings)
                    focus_scans.append(fs)
                    if fs.get("found"):
                        zref = zcur = fs["z_um"]
                        ctx.z_focus = zref
                    else:
                        zcur = fs.get("z_now", zcur)
                        warnings.append(f"point {k}: focus scan found no clear best ({fs.get('why')}); kept the "
                                        "previous focus")
                else:
                    r = ctx.call("autofocus", timeout_s=240, how=af, **motor_args(prof))
                    if r["ok"] and _f(r.get("z_um")) is not None:
                        zref = zcur = _f(r.get("z_um"))
                        ctx.z_focus = zref
                    else:
                        warnings.append(f"point {k}: autofocus failed ({r.get('error')}); kept the previous focus")
            if p["dz_um"] and zref is None:
                raise Stop("the Z position could not be read: Z offsets are refused", "study")
            if zref is not None:
                target = zref + p["dz_um"]       # Z is always relative to the operator's (or autofocus') focus
                if zcur is None or abs(target - zcur) > 1e-6:
                    zcur = move_z(ctx, target, f"point {k}")
            if k == 0 and want_before:
                snaps["before"] = _snapshot(ctx, name, sd, "before", bool(vd.get("with_laser")), warnings, laser)
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
                zonly = kind == "map" and plan.map_shape(study)[:2] == (1, 1)     # a depth profile
                vval = rec["t"] if kind == "series" else p["dz_um"]
                try:
                    if view is None:
                        vr = ctx.call("view", timeout_s=30, action="create", name=name, vfrom=rng.get("from_nm"),
                                      vto=rng.get("to_nm"),
                                      kind="map" if kind == "map" and not zonly else "profile", id=r["id"],
                                      x=p["x_um"], y=p["y_um"], value=vval,
                                      label="time" if kind == "series" else ("Z" if zonly else "XY"),
                                      unit="s" if kind == "series" else "um")
                        view = vr.get("view") if vr["ok"] else False
                        if not vr["ok"]:
                            warnings.append(f"live view in LabSpec not available: {vr.get('error')}")
                    elif view:
                        vr = ctx.call("view", timeout_s=30, action="add", name=name, id=r["id"],
                                      x=p["x_um"], y=p["y_um"], value=vval, vfrom=rng.get("from_nm"),
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
            snaps["after"] = _snapshot(ctx, name, sd, "after", bool(vd.get("with_laser")), warnings, laser)
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
    result = analyse(name, study, per_point, prof, laser, rng, sd, warnings, unreliable, checks,
                     grating=_f(after.get("grating")) or _f(study.get("setup", {}).get("grating")),
                     request=str(ctx.folder))
    if stop_exc is not None:
        if stop_exc.by in ("cancel", "operator", "bridge", "timeout"):
            unreliable.append(f"stopped after {len(per_point)} of {len(pts)} points: {stop_exc.why}")
        else:
            unreliable.append(stop_exc.why)
    result.update(
        ok=bool(per_point), complete=len(per_point) == len(pts) and stop_exc is None,
        reliable=bool(per_point) and not unreliable, unreliable_because=unreliable, warnings=warnings,
        checks=checks, kind=study.get("_kind", kind), sample=study.get("sample", {}),
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
    if focus_scans:
        result["focus_scans"] = focus_scans
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


# --------------------------------------------------------------------------------------------- stage and focus

def _check_abs(prof: dict, axis: str, value: float, what: str):
    lim = prof.get("stage", {}).get(f"{axis}_um")
    if lim and not lim[0] <= value <= lim[1]:
        raise Stop(f"{what}: stage {axis.upper()} = {value:g} µm is outside the profile's stage limits {lim}", "study")


def move_z(ctx: Ctx, target: float, what: str) -> float:
    _check_abs(ctx.prof, "z", target, what)
    r = ctx.call("move", timeout_s=120, motor=ctx.prof["motors"]["z"], mode="value", value=round(target, 3))
    if not r["ok"]:
        raise Stop(f"{what}: Z move failed: {r.get('error')}", "study")
    return _f(r.get("value")) if _f(r.get("value")) is not None else target


def _return_home(ctx: Ctx) -> dict:
    """At the end of a request that finished normally: Z back to the focus first (the safe height), then the
    stage back to the home spot, so the next request starts where the operator left it (or where a 'move' study
    chose to stay)."""
    out: dict = {}
    try:
        st = ctx.call("status", timeout_s=15, **motor_args(ctx.prof))
        z, x, y = _f(st.get("z_um")), _f(st.get("x_um")), _f(st.get("y_um"))
        if ctx.z_focus is not None and z is not None and abs(z - ctx.z_focus) > 0.05:
            out["z_um"] = move_z(ctx, ctx.z_focus, "returning to the focus")
        hx, hy = ctx.home_xy
        if None not in (hx, hy, x, y) and math.hypot(x - hx, y - hy) > 0.5:
            r = ctx.call("move_xy", timeout_s=300, x_um=round(hx, 3), y_um=round(hy, 3), **motor_args(ctx.prof))
            out["xy_um"] = [_f(r.get("x_um")), _f(r.get("y_um"))]
    except Stop as exc:
        out["error"] = exc.why
    return out


def focus_scan(ctx: Ctx, study: dict, laser: float, z0: float | None, sd: Path, warnings: list) -> dict:
    """Focus by the Raman signal: Z steps across focus.scan.range_um around z0, a short acquisition at each, the
    line's height above its window's edges; the best step (refined by a parabola through it and its neighbours)
    becomes the focus. Without a clear maximum inside the range, Z goes back to z0."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sc = plan.scan_params(study, prof)
    if z0 is None:
        raise Stop("the Z position could not be read: a focus scan needs it", "study")
    sd.mkdir(parents=True, exist_ok=True)
    offs = plan.scan_offsets(sc)
    line, win = float(sc["line_cm1"]), float(sc["window_cm1"])
    nm = round(shift_to_nm(line, laser), 4)
    zs, vals = [], []
    for i, dz in enumerate(offs):
        z = move_z(ctx, z0 + dz, f"focus scan step {i}")
        out = sd / f"z{i:03d}.txt"
        r = ctx.call("acquire", timeout_s=60 + 3 * float(sc["exposure_s"]), mode=110, exposure_s=float(sc["exposure_s"]),
                     accumulations=1, from_nm=nm, to_nm=nm, out=str(out), sample="focus scan")
        if not r["ok"]:
            raise Stop(f"focus scan step {i}: acquisition failed: {r.get('error')}", "study")
        sp = spectra.read(out)
        x, unit = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        x, y = spectra.sort_xy(x, sp["y"])
        zs.append(z)
        vals.append(spectra.line_signal(x, y, line, win) / float(sc["exposure_s"]))
    zs_a, v = np.array(zs), np.array(vals)
    i = int(np.argmax(v))
    res = {"z_um": None, "z0_um": z0, "offsets_um": offs, "z_steps_um": zs, "signal": [round(a, 3) for a in vals],
           "line_cm1": line, "unit": "counts/s", "found": False}
    contrast = (v.max() - v.min()) / max(abs(v.max()), 1e-12)
    interior = 0 < i < len(v) - 1
    if v.max() <= 0 or contrast < 0.2 or not interior:
        res["why"] = ("the best is at the edge of the range: widen focus.scan.range_um or start closer" if not interior
                      else "the signal hardly changes with Z: no line, or the range is far from focus")
        res["z_now"] = move_z(ctx, z0, "focus scan: back to the start")
    else:
        # a Gaussian through the points above 30 % of the best (a parabola in log, weighted by the signal); a
        # three-point parabola around the best when too few points qualify
        sel = v > 0.3 * v.max()
        zb = None
        if sel.sum() >= 4:
            c2, c1, _ = np.polyfit(zs_a[sel], np.log(v[sel]), 2, w=np.sqrt(v[sel]))
            if c2 < 0:
                zb = float(-c1 / (2 * c2))
        if zb is None or not zs_a.min() <= zb <= zs_a.max():
            y1, y2, y3 = v[i - 1], v[i], v[i + 1]
            den = (y1 - 2 * y2 + y3)
            step = zs_a[i + 1] - zs_a[i]
            frac = 0.5 * (y1 - y3) / den if den < 0 else 0.0
            zb = float(zs_a[i] + max(-0.5, min(0.5, frac)) * step)
        res.update(found=True, z_um=round(move_z(ctx, zb, "focus scan: to the best"), 3),
                   best_signal=round(float(v.max()), 3), shift_um=round(zb - z0, 3), contrast=round(contrast, 3))
        res["z_now"] = res["z_um"]
    try:
        from . import render
        res["image"] = render.focus_png(sd / "focus_scan.png", res)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"focus scan plot not drawn: {type(exc).__name__}: {exc}")
    write_json(sd / "focus_scan.json", clean(res))
    return res


def run_focus(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'focus': a focus scan on the Raman line at the current spot; the focus found becomes the request's
    focus (later studies' Z offsets start from it, and Z returns to it at the end)."""
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    _progress(sd, state="running", stage="setup", started=t_start,
              estimate_min=plan.estimate(study, ctx.prof)["minutes"])
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    _progress(sd, stage="focusing")
    z0 = _f(su["after"].get("z_um"))
    fs = focus_scan(ctx, study, su["laser"], ctx.z_focus if ctx.z_focus is not None else z0, sd / "focus", warnings)
    if fs.get("found"):
        ctx.z_focus = fs["z_um"]
    else:
        unreliable.append(f"no clear focus: {fs.get('why')}")
    result = {"ok": True, "complete": True, "reliable": fs.get("found", False) and not unreliable,
              "unreliable_because": unreliable, "warnings": warnings, "checks": checks, "kind": "focus",
              "sample": study.get("sample", {}), "focus": fs,
              "instrument": {"laser_nm": su["laser"], "detector_temp_c": su["temp"],
                             "filter": su["after"].get("filter"), "objective": su["after"].get("objective")},
              "setup": {"requested": study.get("setup", {}), "before": su["before"], "after": su["after"],
                        "moves": su["readback"]},
              "seconds": {"total": round(time.time() - t_start, 2)},
              "execution": {"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                            "protocol": BRIDGE_PROTOCOL}}
    if fs.get("image"):
        result["images"] = {"focus_scan": fs["image"]}
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


def run_move(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'move': no laser. The stage to an offset from the home spot (and Z to an offset from the focus), a
    video snapshot there; with stay (default) the new spot becomes home for the rest of the request and the start
    of the next one. Z always returns to the focus at the end of the request."""
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    m = study.get("move", {})
    warnings: list[str] = []
    _progress(sd, state="running", stage="moving", started=t_start)
    before = ctx.call("status", timeout_s=15, **motor_args(prof))
    obj = (study.get("setup") or {}).get("objective")
    if obj is not None:
        r = ctx.call("move", timeout_s=600, motor=prof["motors"]["objective"], mode="label", value=str(obj))
        if not r["ok"] or r.get("label") != str(obj):
            raise Stop(f"objective {obj}: {r.get('error') or 'got ' + str(r.get('label'))}", "study")
    hx, hy = ctx.home_xy
    if None in (hx, hy):
        raise Stop("the stage position could not be read: moves are refused", "study")
    beam = None
    if m.get("to_um") is not None:
        # put the beam (not LabSpec's marker) on a point picked in a snapshot: stage = point - the beam's offset
        objective = obj if obj is not None else before.get("objective")
        off, src = SP.offset_for(prof, objective, _f(before.get("laser_nm")))
        beam = {"to_um": [float(v) for v in m["to_um"]], "offset_um": off, "offset_source": src}
        tx, ty = beam["to_um"][0] - off[0], beam["to_um"][1] - off[1]
        if math.hypot(tx - hx, ty - hy) > prof["limits"]["max_xy_um"]:
            raise Stop(f"{name}: {math.hypot(tx - hx, ty - hy):.0f} µm from the home spot, above "
                       f"limits.max_xy_um = {prof['limits']['max_xy_um']:g} µm", "study")
        if src == "none measured":
            warnings.append("no beam offset measured for this objective and laser: the beam lands where LabSpec's "
                            "marker is (run a 'spot' study to find it)")
    else:
        tx, ty = hx + float(m.get("x_um", 0) or 0), hy + float(m.get("y_um", 0) or 0)
    _check_abs(prof, "x", tx, name)
    _check_abs(prof, "y", ty, name)
    r = ctx.call("move_xy", timeout_s=300, x_um=round(tx, 3), y_um=round(ty, 3), **motor_args(prof))
    if not r["ok"]:
        raise Stop(f"{name}: XY move failed: {r.get('error')}", "study")
    xy = [_f(r.get("x_um")), _f(r.get("y_um"))]
    z = _f(before.get("z_um"))
    dz = float(m.get("dz_um", 0) or 0)
    if ctx.z_focus is not None and (dz or (z is not None and abs(z - ctx.z_focus) > 0.05)):
        z = move_z(ctx, ctx.z_focus + dz, name)
    if m.get("stay", True):
        ctx.home_xy = (xy[0] if xy[0] is not None else tx, xy[1] if xy[1] is not None else ty)
    snap = None
    if m.get("snapshot", True):
        snap = _snapshot(ctx, name, sd, "here", False, warnings, _f(before.get("laser_nm")))
    result = {"ok": True, "complete": True, "reliable": True, "unreliable_because": [], "warnings": warnings,
              "kind": "move", "move": {"offset_um": [float(m.get("x_um", 0) or 0), float(m.get("y_um", 0) or 0)],
                                       "dz_um": dz, "stage_um": xy, "z_um": z, "stay": m.get("stay", True),
                                       "home_um": list(ctx.home_xy), "objective": obj, "beam": beam},
              "setup": {"before": before}, "seconds": {"total": round(time.time() - t_start, 2)}}
    if snap:
        result["video"] = {"here": snap}
        if snap.get("path"):
            result["images"] = {"video_here": {"path": snap["path"], "what": snap.get("what")}}
    if m.get("stay", True):
        warnings.append("the stage stays here: later offsets (in this request and the next) start from this spot")
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


# --------------------------------------------------------------------------------------------- the laser spot

def raman_spot(ctx: Ctx, study: dict, sp: dict, laser: float, objective, sd: Path, warnings: list) -> dict:
    """Find the beam by the Raman signal on a small feature seen on the video: the stage scans a small grid so the
    beam passes over the feature; where the line's signal changes most (dip for dust on Si, peak for a particle with
    the line) the beam sat on the feature. offset = feature - stage there."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sd.mkdir(parents=True, exist_ok=True)
    st = ctx.call("status", timeout_s=15, **motor_args(prof))
    s0 = (_f(st.get("x_um")), _f(st.get("y_um")))
    if None in s0:
        raise Stop("the stage position could not be read", "study")
    snap = _snapshot(ctx, "spot", sd, "feature", False, warnings, laser, remember=False)
    dec = (snap or {}).pop("_decoded", None)
    out: dict = {"method": "raman", "found": False, "snapshot": snap}
    if dec is None or not dec.get("um"):
        out["why"] = "no usable video frame (axes in µm needed) to locate the feature"
        return out
    feat = sp.get("feature_um")
    if feat in (None, "auto"):
        cx, cy = float(np.mean(dec["xs"])), float(np.mean(dec["ys"]))
        cands = [f for f in (snap.get("features") or []) if f["size_um"] <= 6 and f["strength"] >= 10]
        if not cands:
            out["why"] = "no small feature found on the video: give spot.feature_um (a point picked in a snapshot)"
            return out
        f0 = min(cands, key=lambda f: math.hypot(f["x_um"] - cx, f["y_um"] - cy))
        F = (f0["x_um"], f0["y_um"])
        out["feature_auto"] = f0
    else:
        F = (float(feat[0]), float(feat[1]))
    if sp.get("refine", True):
        rf = video.refine_feature(dec, F[0], F[1], max(2.0, float(sp["range_um"]) / 2))
        if rf:
            out["feature_refined"] = rf
            F = (rf["x_um"], rf["y_um"])
        else:
            warnings.append("the feature could not be refined on the video: its given position is used")
    out["feature_um"] = [round(F[0], 3), round(F[1], 3)]
    o0, src = SP.offset_for(prof, objective, laser)
    out["prior_offset_um"], out["prior_source"] = o0, src
    g = plan.axis_values([-float(sp["range_um"]) / 2, float(sp["range_um"]) / 2, float(sp["step_um"])])
    nm = round(shift_to_nm(float(sp["line_cm1"]), laser), 4)
    hx, hy = ctx.home_xy
    V = np.full((len(g), len(g)), np.nan)
    grid_pos = []
    for iy, gy in enumerate(g):
        row = list(enumerate(g)) if iy % 2 == 0 else list(enumerate(g))[::-1]
        for ix, gx in row:
            sx, sy = F[0] - o0[0] + gx, F[1] - o0[1] + gy
            if math.hypot(sx - hx, sy - hy) > prof["limits"]["max_xy_um"]:
                raise Stop(f"the spot scan would go {math.hypot(sx - hx, sy - hy):.0f} µm from the home spot "
                           f"(limits.max_xy_um = {prof['limits']['max_xy_um']:g})", "study")
            _check_abs(prof, "x", sx, "spot scan")
            _check_abs(prof, "y", sy, "spot scan")
            r = ctx.call("move_xy", timeout_s=120, x_um=round(sx, 3), y_um=round(sy, 3), **motor_args(prof))
            if not r["ok"]:
                raise Stop(f"spot scan: XY move failed: {r.get('error')}", "study")
            f = sd / f"g{iy:02d}_{ix:02d}.txt"
            r = ctx.call("acquire", timeout_s=60 + 3 * float(sp["exposure_s"]), mode=110,
                         exposure_s=float(sp["exposure_s"]), accumulations=1, from_nm=nm, to_nm=nm, out=str(f),
                         sample="spot scan")
            if not r["ok"]:
                raise Stop(f"spot scan: acquisition failed: {r.get('error')}", "study")
            spc = spectra.read(f)
            x, _ = spectra.to_shift(spc["x"], spc["meta"].get("x_unit", "nm"), laser)
            x, y = spectra.sort_xy(x, spc["y"])
            V[iy, ix] = spectra.line_signal(x, y, float(sp["line_cm1"]), float(sp["window_cm1"])) / float(sp["exposure_s"])
            grid_pos.append([round(sx, 3), round(sy, 3)])
    ctx.call("move_xy", timeout_s=120, x_um=round(s0[0], 3), y_um=round(s0[1], 3), **motor_args(prof))
    border = np.concatenate([V[0, :], V[-1, :], V[1:-1, 0], V[1:-1, -1]])
    b = float(np.median(border))
    noise = float(1.4826 * np.median(np.abs(border - b))) or max(abs(b) * 0.02, 1e-9)
    dev = V - b
    dark = abs(np.nanmin(dev)) > abs(np.nanmax(dev))
    c = -dev if dark else dev
    contrast = float(np.nanmax(c) / noise)
    GX, GY = np.meshgrid(g, g)
    w = np.clip(c - 0.5 * np.nanmax(c), 0, None)
    out.update(grid_um=g, signal=V.round(3).tolist(), dark=bool(dark), contrast_sigma=round(contrast, 1),
               line_cm1=float(sp["line_cm1"]))
    if contrast < 6 or not np.isfinite(contrast) or w.sum() <= 0:
        out["why"] = (f"the feature hardly changes the signal ({contrast:.1f} sigma): another feature, a longer "
                      "exposure, or the line it has (spot.line_cm1)")
    else:
        gx_hit, gy_hit = float((w * GX).sum() / w.sum()), float((w * GY).sum() / w.sum())
        edge = max(abs(gx_hit), abs(gy_hit)) > g[-1] - float(sp["step_um"])
        sx_hit, sy_hit = F[0] - o0[0] + gx_hit, F[1] - o0[1] + gy_hit
        off = [round(F[0] - sx_hit, 3), round(F[1] - sy_hit, 3)]
        out.update(stage_at_hit_um=[round(sx_hit, 3), round(sy_hit, 3)], offset_um=off)
        if edge:
            out["why"] = "the beam hit the feature at the edge of the scan: a larger spot.range_um"
        else:
            out["found"] = True
    try:
        from . import render
        out["image"] = render.spot_png(sd / "spot_scan.png", out, F, o0)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"spot scan plot not drawn: {type(exc).__name__}: {exc}")
    return out


def run_spot(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'spot': find where the beam really is, and remember it. With the laser shutter (a frame with the beam
    on), during a Raman acquisition (a frame half-way through it), or by the Raman signal on a small feature
    seen on the video; 'auto' tries the video first and falls back to the Raman scan when a feature is given (or
    found)."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    _progress(sd, state="running", stage="setup", started=t_start,
              estimate_min=plan.estimate(study, prof)["minutes"])
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    laser = su["laser"]
    objective = su["after"].get("objective")
    sp = plan.spot_params(study, prof, objective)
    method = sp["method"]
    attempts: list = []
    found = None
    _progress(sd, stage="finding the beam")
    if method in ("auto", "shutter", "acquisition"):
        use = method if method != "auto" else ("shutter" if prof["motors"].get("laser_shutter") else "acquisition")
        snap = _snapshot(ctx, name, sd, use, use == "shutter", warnings, laser,
                         during_acq_s=float(sp["acq_exposure_s"]) if use == "acquisition" else 0.0,
                         acq_nm=round(shift_to_nm(float(sp["line_cm1"]), laser), 4), remember=False)
        if snap:
            snap.pop("_decoded", None)
        att = {"method": use, "found": bool(snap and snap.get("measured_offset_um")),
               "contrast_sigma": (snap or {}).get("spot_contrast_sigma"), "snapshot": snap}
        attempts.append(att)
        if att["found"]:
            found = {"method": use, "offset_um": snap["measured_offset_um"],
                     "quality": {"contrast_sigma": snap.get("spot_contrast_sigma")}}
    if found is None and (method == "raman" or (method == "auto" and sp.get("feature_um") is not None)):
        rs = raman_spot(ctx, study, sp, laser, objective, sd / "raman", warnings)
        attempts.append({k: v for k, v in rs.items() if k != "snapshot"})
        if rs.get("found"):
            found = {"method": "raman", "offset_um": rs["offset_um"],
                     "quality": {"contrast_sigma": rs.get("contrast_sigma"), "dark": rs.get("dark"),
                                 "step_um": sp["step_um"]}}
    rec = None
    if found:
        if sp.get("remember", True):
            rec = SP.record(prof, objective, laser, found["offset_um"], found["method"], found["quality"],
                            str(ctx.folder))
            if rec.get("warning"):
                warnings.append(rec["warning"])
            if not rec.get("stored"):
                warnings.append(rec.get("why", "not remembered"))
    else:
        unreliable.append("the beam was not found: " + "; ".join(
            a.get("why") or ("no spot on the video" if not a.get("found") else "") for a in attempts if a))
    images = {}
    for a in attempts:
        if a.get("snapshot") and a["snapshot"].get("path"):
            images[f"video_{a['method'].replace(' ', '_')}"] = {"path": a["snapshot"]["path"],
                                                                  "what": a["snapshot"].get("what")}
        if a.get("image"):
            images["spot_scan"] = a["image"]
        if a.get("snapshot") is None and a.get("method") == "raman":
            pass
    result = {"ok": True, "complete": True, "reliable": found is not None and not unreliable,
              "unreliable_because": unreliable, "warnings": warnings, "checks": checks, "kind": "spot",
              "sample": study.get("sample", {}),
              "spot": {"found": found is not None, "objective": objective, "laser_nm": laser,
                       "offset_um": (found or {}).get("offset_um"), "method": (found or {}).get("method"),
                       "remembered": bool(rec and rec.get("stored")), "drift_um": (rec or {}).get("drift_um"),
                       "previous": (rec or {}).get("previous"), "attempts": attempts},
              "instrument": {"laser_nm": laser, "objective": objective, "filter": su["after"].get("filter")},
              "seconds": {"total": round(time.time() - t_start, 2)},
              "execution": {"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                            "protocol": BRIDGE_PROTOCOL}}
    if images:
        result["images"] = images
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


# --------------------------------------------------------------------------------------------- spectral zero, background

def run_calibration(name: str, study: dict, ctx: Ctx) -> dict:
    """calibration.adjust 'labspec': measure; if the lines are off, the operator corrects LabSpec's spectral zero
    (Maintenance > Instrument calibration) as LabSpec shows, presses OK, and ls6t measures again (at most
    calibration.max_retries times). Once LabSpec is right, ls6t's own correction for this laser and grating is
    dropped."""
    from . import xcal
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    attempts, res = [], None
    n_max = 1 + int(prof.get("calibration", {}).get("max_retries", 2))
    for k in range(1, n_max + 1):
        res = run_study(name, study, ctx, sd / f"attempt{k}")
        cal = res.get("calibration") or {}
        attempts.append({"attempt": k, "pass": cal.get("pass"), "offset_cm1": cal.get("offset_cm1"),
                         "worst_error_cm1": cal.get("worst_error_cm1"), "correction": cal.get("correction")})
        if cal.get("pass") or k == n_max or not cal.get("lines_used"):
            break
        laser = (res.get("instrument") or {}).get("laser_nm") or 532.0
        ref, meas = cal["reference_cm1"], cal["measured_cm1"]
        err = meas - ref
        nm = shift_to_nm(ref, laser)
        dnm = -err * nm * nm / 1e7
        txt = (f"ls6t: the {'Si' if cal.get('reference') == 'si' else cal.get('reference')} line at {ref} cm-1 is measured at {meas:.2f} cm-1 "
               f"({err:+.2f} cm-1).\n\nCorrect the spectral zero in LabSpec (Maintenance > Instrument calibration, "
               f"for this grating): the line must move by {-err:+.2f} cm-1, about {dnm:+.4f} nm."
               + (f" With several lines, the coefficient (dispersion) too: scale {cal['correction']['b']:.6f}."
                  if cal.get("correction") and abs(cal["correction"]["b"] - 1) > 1e-5 else "")
               + "\n\nPress OK to measure again, or Cancel to stop.")
        ans = ctx.call("notice", timeout_s=7200, text=txt)
        if ans.get("answer") != "ok":
            break
    result = dict(res or {})
    cal = dict(result.get("calibration") or {})
    cal["attempts"] = attempts
    if cal.get("pass") and len(attempts) > 1:
        inst = result.get("instrument") or {}
        n = xcal.clear(inst.get("laser_nm"), inst.get("grating"))
        if n:
            result.setdefault("warnings", []).append("LabSpec is calibrated again: ls6t's own correction for this "
                                                     "laser and grating was dropped")
    result["calibration"] = cal
    return _finish(sd, name, result)


def run_background(name: str, study: dict, ctx: Ctx) -> dict:
    """A white-light background: the white light on (the operator), the laser off (the shutter closed by the bridge,
    or blocked by the operator), one acquisition with the given settings; stored by name as counts per second per
    accumulation for later subtraction."""
    from . import background as BG
    prof = ctx.prof
    sd = ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    bgk = study.get("background") or {}
    bname = bgk["name"]
    off = bgk.get("laser_off", "shutter" if prof["motors"].get("laser_shutter") else "operator")
    _progress(sd, state="running", stage="setup", started=t_start)
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1, confirm=False)
    laser, rng = su["laser"], su["rng"]
    txt = (f"ls6t: white-light background '{bname}'.\n\nTurn the white light on, as it will be during the "
           "measurements" + (", and block the laser (LabSpec: Laser Off, or close its shutter by hand)"
                             if off == "operator" else " (ls6t keeps the laser shutter closed)")
           + ".\n\nPress OK when ready, or Cancel.")
    if ctx.call("notice", timeout_s=3600, text=txt).get("answer") != "ok":
        raise Stop("the operator cancelled the background", "study")
    acq = plan.acquire_step(study, prof, rng)
    acq.pop("verb", None)
    out = sd / "spectra" / "background.txt"
    params = dict(acq, out=str(out), sample=f"white-light background {bname}",
                  native=str(sd / "spectra" / "background.l6s") if study.get("save_native", True) else None)
    if off == "shutter":
        params.update(laser_off=1, m_laser_shutter=prof["motors"]["laser_shutter"])
    r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
    if off == "operator":
        ctx.call("notice", timeout_s=3600, text="ls6t: the background is taken. Unblock the laser when you want "
                                                 "to measure again. OK.")
    if not r["ok"]:
        raise Stop(f"background acquisition failed: {r.get('error')}", "study")
    sp = spectra.read(out)
    ex = _f(r.get("exposure_s")) or float(acq.get("exposure_s") or 1.0)
    per_s = sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s")
    cps = sp["y"] if per_s else sp["y"] / ex
    sat = spectra.saturation(sp["y"] if not per_s else sp["y"] * ex, prof["detector"]["full_scale"],
                             prof["checks"]["saturation_frac"])
    if sat["saturated"]:
        unreliable.append("the background is saturated: a shorter exposure, or less white light")
    # narrow lines in a background mean the laser was not blocked
    try:
        shift, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        xs, ysr = spectra.sort_xy(shift, sp["y"])
        yc = ysr - spectra.baseline(xs, ysr, 3)
        narrow = spectra.find_peaks(xs, yc, 3, None, 25.0)
        if narrow:
            warnings.append(f"the background has narrow lines at {', '.join(f'{v:.0f}' for v in narrow)} cm-1: was "
                            "the laser blocked? (a Raman line in the background would be subtracted from samples)")
    except ValueError:
        pass
    meta = {"name": bname, "laser_nm": laser, "grating": _f(su["after"].get("grating")),
            "objective": su["after"].get("objective"), "exposure_s": ex,
            "accumulations": int(_f(r.get("accumulations")) or 1), "range": rng, "request": str(ctx.folder),
            "laser_off": off}
    stored = BG.save(bname, sp["x"], cps, sp["meta"].get("x_unit", "nm"), meta)
    result = {"ok": True, "complete": True, "reliable": not unreliable, "unreliable_because": unreliable,
              "warnings": warnings, "checks": checks, "kind": "background", "sample": study.get("sample", {}),
              "background": {**stored, "max_cps": round(float(np.max(cps)), 3), "mean_cps": round(float(np.mean(cps)), 3)},
              "spectra": [{"file": str(out), "exposure_s": ex, "max_counts": sat["max_counts"]}],
              "instrument": {"laser_nm": laser, "grating": meta["grating"], "objective": meta["objective"]},
              "seconds": {"total": round(time.time() - t_start, 2)}}
    return _finish(sd, name, result)


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
        snap = _snapshot(ctx, name, sd, "after", bool(prof["motors"].get("laser_shutter")), warnings,
                         (res.get("instrument") or {}).get("laser_nm"))
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


def _snapshot(ctx: Ctx, name: str, sd: Path, tag: str, with_laser: bool, warnings: list, laser_nm=None,
              during_acq_s: float = 0.0, acq_nm: float | None = None, remember: bool = True) -> dict | None:
    """A video frame now (LabSpec's live video; with_laser: the shutter opens for video.laser_s; during_acq_s: the
    frame is taken half-way through a Raman acquisition of that length, which opens the shutter itself), drawn with
    the laser position marked. When the beam shows and is found, its offset is remembered (spot.py). A failure is a
    warning, never a reason to stop the study."""
    v = ctx.prof.get("video", {})
    base = sd / "video" / tag
    base.parent.mkdir(parents=True, exist_ok=True)
    laser_s = float(v.get("laser_s", 1.0)) if with_laser else 0.0
    settle = float(v.get("settle_s", 1.5))
    r = ctx.call("video", timeout_s=90 + settle + laser_s + 2 * during_acq_s, out=str(base), settle_s=settle,
                 laser_s=laser_s, during_acq_s=during_acq_s or None, acq_from_nm=acq_nm, acq_to_nm=acq_nm,
                 formats=",".join(v.get("formats", [])), camera=v.get("camera"), **motor_args(ctx.prof))
    if not r["ok"]:
        warnings.append(f"video snapshot ({tag}) failed: {r.get('error')}")
        return None
    snap = video.snapshot(r, ctx.prof, base, f"{name}: {tag}", laser_nm)
    if remember and snap.get("measured_offset_um"):
        rec = SP.record(ctx.prof, snap.get("objective"), laser_nm, snap["measured_offset_um"],
                        "shutter" if laser_s else "during acquisition",
                        {"contrast_sigma": snap.get("spot_contrast_sigma")}, str(ctx.folder))
        snap["remembered"] = {k: v for k, v in rec.items() if k != "previous"}
        if rec.get("warning"):
            warnings.append(rec["warning"])
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


def analyse(name, study, per_point, prof, laser, rng, sd: Path, warnings, unreliable, checks, grating=None,
            request=None) -> dict:
    from . import background as BG
    from . import xcal
    kind = study.get("kind", "spectrum")
    process = dict(study.get("process", {}))
    ref_lines = plan.calibration_lines(study, prof, rng) if kind == "calibration" else []
    if kind == "calibration" and not process.get("peaks"):
        process["peaks"] = [{"name": f"ref{i}" if len(ref_lines) > 1 else "Si", "near_cm1": c, "window_cm1": 20
                             if len(ref_lines) > 1 else 30} for i, c in enumerate(ref_lines)]
    # ls6t's own spectral correction (from an earlier calibration), never on a calibration itself
    corr = xcal.get(laser, grating) if kind != "calibration" else None
    if corr:
        age = xcal.age_h(corr)
        warnings.append(f"spectral correction applied: true = {corr['a']:+.3f} + {corr['b']:.6f} x measured "
                        f"(calibration {corr['time']}, {corr['reference']})")
        if age is not None and age > float(prof.get("calibration", {}).get("max_age_h", 24)):
            warnings.append(f"the spectral correction is {age:.0f} h old: run a calibration")
    bgname = study.get("acquire", {}).get("subtract_background")
    bg = BG.load(bgname) if bgname else None
    if bgname and bg is None:
        unreliable.append(f"the white-light background {bgname!r} is not stored (a 'background' study makes it)")
    elif bg:
        bm = bg["meta"]
        if grating and bm.get("grating") and abs(float(bm["grating"]) - float(grating)) > 0.5:
            warnings.append(f"the background {bgname!r} was taken with the {bm['grating']:g} gr/mm grating, this "
                            f"spectrum with {grating:g}")
        if laser and bm.get("laser_nm") and abs(float(bm["laser_nm"]) - float(laser)) > 1:
            warnings.append(f"the background {bgname!r} was taken with the {bm['laser_nm']:g} nm laser selected")
    fs = prof["detector"]["full_scale"]
    frac = prof["checks"]["saturation_frac"]
    min_snr = prof["checks"]["min_snr"]
    sat_points, spike_points, cover_short = [], [], []
    spike_mode = study.get("acquire", {}).get("spike", "labspec")
    axes, ys = [], []
    for rec in per_point:
        sp = rec.pop("_sp") if "_sp" in rec else spectra.read(rec["file"])
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
        if corr:
            shift = corr["a"] + corr["b"] * shift
            rec["x_correction"] = {"a": corr["a"], "b": corr["b"]}
        if bg:
            per_s = sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s")
            y, info = BG.subtract(sp["x"], y, sp["meta"].get("x_unit", "nm"),
                                  1.0 if per_s else (rec.get("exposure_s") or 1.0), bg, laser)
            rec["background"] = info
            if info["covered_fraction"] < 0.98:
                warnings.append(f"point {rec['point']}: the background covers {info['covered_fraction']:.0%} of the "
                                "spectrum")
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
    if spike_points and spike_mode == "none":
        # with LabSpec's spike removal on, what remains is usually a narrow Raman line, not a cosmic ray
        warnings.append(f"cosmic-ray spikes left in {len(spike_points)} spectra (spike removal is off: use 'double')")
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
        if out["map"]["nx"] == 1 and out["map"]["ny"] == 1 and out["map"]["nz"] > 1:
            out["depth"] = depth_result(study, out["map"], prof, sd, warnings)
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
        out["calibration"] = _calibration_result(study, prof, per_point[0], ref_lines, laser, grating, request,
                                                 checks, unreliable, warnings)
    return out


def _calibration_result(study, prof, rec, ref_lines, laser, grating, request, checks, unreliable, warnings) -> dict:
    """The reference lines measured against their true positions: the offset (the zero) and, with lines far enough
    apart, the scale (the dispersion coefficient): true = a + b x measured. adjust 'ls6t' remembers it and applies
    it to later spectra (same laser and grating)."""
    from . import xcal
    cal = study.get("calibration") or {}
    refname = cal.get("reference", "si")
    tol = float(cal.get("tol_cm1", prof["checks"]["si_tol_cm1"]))
    lines = []
    for ref, pk in zip(ref_lines, rec.get("peaks") or []):
        lines.append({"reference_cm1": ref, "measured_cm1": pk.get("position_cm1") if pk.get("ok") else None,
                      "error_cm1": round(pk["position_cm1"] - ref, 4) if pk.get("ok") else None,
                      "fwhm_cm1": pk.get("fwhm_cm1"), "snr": pk.get("snr"), "why": pk.get("why")})
    ok = [l for l in lines if l["measured_cm1"] is not None]
    res: dict = {"reference": refname, "lines": lines, "tol_cm1": tol, "adjust": cal.get("adjust", "none")}
    if not ok:
        res.update(pass_=False, why="no reference line found: check the sample and its focus")
        res["pass"] = res.pop("pass_")
        checks["calibration"] = {"pass": False}
        unreliable.append(f"no {refname} line found: check the sample and its focus")
        return res
    meas = np.array([l["measured_cm1"] for l in ok])
    refs = np.array([l["reference_cm1"] for l in ok])
    if len(ok) >= 2 and refs.max() - refs.min() >= 300:
        b, a = np.polyfit(meas, refs, 1)
        res["fit"] = "offset and scale (zero and dispersion)"
    else:
        a, b = float(np.mean(refs - meas)), 1.0
        res["fit"] = "offset only (one line, or lines too close together for the scale)"
    worst = float(np.max(np.abs(meas - refs)))
    resid = refs - (a + b * meas)
    first = ok[0]
    res.update(reference_cm1=first["reference_cm1"], measured_cm1=first["measured_cm1"],
               offset_cm1=round(float(np.mean(meas - refs)), 4), fwhm_cm1=first["fwhm_cm1"], snr=first["snr"],
               worst_error_cm1=round(worst, 4), correction={"a": round(float(a), 5), "b": round(float(b), 7)},
               residual_cm1=round(float(np.max(np.abs(resid))), 4), lines_used=len(ok), **{"pass": worst <= tol})
    res["zero_shift_cm1"] = round(float(a + (b - 1.0) * float(np.mean(refs))), 4)   # the shift at the lines' centre
    checks["calibration"] = {"pass": res["pass"], "offset_cm1": res["offset_cm1"], "worst_error_cm1": res["worst_error_cm1"],
                             "tol_cm1": tol}
    if res["adjust"] == "ls6t":
        st = xcal.store(laser, grating, a, b, refname, lines, request)
        res["stored_correction"] = st
        warnings.append(f"ls6t now corrects later spectra ({xcal.key(laser, grating)}): true = {a:+.3f} + "
                        f"{b:.6f} x measured")
    elif not res["pass"]:
        unreliable.append(f"the {'Si' if refname == 'si' else refname} line is at {first['measured_cm1']:.2f} cm-1, "
                          f"{first['measured_cm1'] - first['reference_cm1']:+.2f} from {first['reference_cm1']}: "
                          "adjust the zero in LabSpec (calibration.adjust 'labspec' guides it), or let ls6t correct "
                          "(adjust 'ls6t')")
    return res


def _native_map(ctx: Ctx, name: str, study: dict, sd: Path, rng: dict, laser: float, acq: dict, x0, y0, zref,
                warnings: list) -> list:
    """LabSpec's own mapping engine: the axes as stage positions (or offsets, profile mapping.positions), one
    detector window at the range's centre, exposure and accumulations in LabSpec's interface, SWIFT if asked;
    the map comes back as one data object, split here into per-point records (with their grid indices)."""
    prof = ctx.prof
    mp = prof.get("mapping", {})
    m = study.get("map", {})
    absolute = mp.get("positions", "absolute") == "absolute"
    params: dict = {}
    refs = {"x": x0, "y": y0, "z": zref}
    for ax in ("x", "y", "z"):
        if not m.get(f"{ax}_um"):
            continue
        a, b, st = (float(v) for v in m[f"{ax}_um"])
        if refs[ax] is None and absolute:
            raise Stop(f"the {ax.upper()} position could not be read: a native map needs it", "study")
        base = refs[ax] if absolute else 0.0
        for v in (a, b):
            _check_abs(prof, ax, (refs[ax] or 0.0) + v, f"native map {ax}")
        params.update({f"{ax}_from": round(base + a, 3), f"{ax}_to": round(base + b, 3), f"{ax}_step": st})
    flags = int(acq.get("mode", 10)) - 10
    n = len(plan.positions(study))
    per_s = float(acq.get("exposure_s") or 1.0) * int(acq.get("accumulations", 1)) + \
        float(mp.get("native_point_s", 0.05))
    _progress(sd, stage="LabSpec is mapping", point=0, points=n, eta_min=round(n * per_s / 60.0, 2))
    r = ctx.call("map_native", timeout_s=600 + 3 * n * per_s, out=str(sd / name), center_nm=rng.get("from_nm"),
                 from_nm=rng.get("from_nm"), to_nm=rng.get("to_nm"), exposure_s=acq.get("exposure_s"),
                 accumulations=acq.get("accumulations", 1), mode_flags=flags, swift=bool(m.get("swift")),
                 acq_time=mp.get("acq_time", "interface"), **params, **motor_args(prof))
    if not r["ok"]:
        if r.get("stopped_by"):
            raise Stop("stopped by the operator in LabSpec" if r.get("stopped_by") == "operator" else "cancelled",
                       "operator" if r.get("stopped_by") == "operator" else "cancel")
        raise Stop(f"native map failed: {r.get('error')}", "study")
    if str(r.get("native_ok")) != "1":
        warnings.append("LabSpec did not save the map's .l6s file")
    recs = parse_native_map(r["out"], study, prof, x0, y0, zref)
    if len(recs) != n:
        warnings.append(f"LabSpec's map has {len(recs)} points, the study {n}: check the axes (TESTING Q29)")
    for rec in recs:
        rec.update(file=r["out"], exposure_s=float(acq.get("exposure_s") or 1.0),
                   accumulations=float(acq.get("accumulations", 1)), id=r.get("id"))
    return recs


def parse_native_map(path, study: dict, prof: dict, x0, y0, zref) -> list:
    """The bridge's dump of a LabSpec map: axes (types, sizes, units, values) and the values in LabSpec's order
    (profile mapping.data_order: each spectrum contiguous, or each wavelength's image contiguous; points with the
    first spatial axis fastest). Each point becomes a record with its spectrum and grid indices (the study's
    axes)."""
    from .profile import read_text
    meta: dict = {}
    rows: list = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            k, _, v = line[1:].strip().partition("=")
            meta[k.strip()] = v.strip()
        else:
            rows.append(line)
    data = np.array([float(v) for row in rows for v in row.split(",") if v != ""], float)
    types = [t.strip() for t in meta.get("axis_types", "").split(",")]
    sizes = [int(float(v)) for v in meta.get("axis_sizes", "").split(",") if v.strip()]
    units = [u.strip() for u in meta.get("axis_units", "").split(",")]
    axis = np.array([float(v) for v in meta.get("axis_values", "").split(",") if v.strip()], float)
    vals, pos_ = {}, 0
    for t, sz in zip(types, sizes):
        vals[t] = axis[pos_:pos_ + sz]
        pos_ += sz
    spec_t = next((t for t in types if t.lower().startswith("spectr")), None)
    if spec_t is None:
        raise Stop("the native map has no spectral axis", "study")
    xs_spec = vals[spec_t]
    ns = xs_spec.size
    ui = types.index(spec_t)
    x_unit = units[ui] if ui < len(units) and units[ui] else "nm"
    sp_axes = [t for t in ("X", "Y", "Z") if t in vals and vals[t].size > 0]
    shape = [vals[t].size for t in sp_axes]
    npts = int(np.prod(shape)) if shape else 1
    if data.size != npts * ns:
        raise Stop(f"the native map holds {data.size} values, not {npts} x {ns}", "study")
    order = prof.get("mapping", {}).get("data_order", "spectral_fastest")
    spec = data.reshape(npts, ns) if order == "spectral_fastest" else data.reshape(ns, npts).T
    m = study.get("map", {})
    absolute = prof.get("mapping", {}).get("positions", "absolute") == "absolute"
    refs = {"X": x0, "Y": y0, "Z": zref}
    grid = {t: plan.axis_values(m[f"{t.lower()}_um"]) for t in ("X", "Y", "Z") if m.get(f"{t.lower()}_um")}
    recs = []
    for k in range(npts):
        rem, idx = k, {}
        for t, sz in zip(sp_axes, shape):          # the first spatial axis varies fastest
            idx[t] = rem % sz
            rem //= sz
        ixyz = {"ix": 0, "iy": 0, "iz": 0}
        stage, offs = {}, {}
        for t in sp_axes:
            v = float(vals[t][idx[t]])
            off = v - (refs[t] or 0.0) if absolute else v
            offs[t] = round(off, 4)
            stage[f"{t.lower()}_um"] = v if absolute else (refs[t] or 0.0) + v
            g = grid.get(t)
            if g:
                ixyz["i" + t.lower()] = int(np.argmin([abs(off - gv) for gv in g]))
        recs.append({"point": k, "ixyz": ixyz, "stage": stage,
                     "offset": {"x_um": offs.get("X", 0.0), "y_um": offs.get("Y", 0.0), "dz_um": offs.get("Z", 0.0)},
                     "_sp": {"x": xs_spec, "y": spec[k], "meta": {"x_unit": x_unit, "y_unit": "cnt"}}})
    return recs


def _point_summary(r: dict) -> dict:
    keys = ("point", "offset", "stage", "file", "native", "t", "exposure_s", "accumulations", "max_counts",
            "saturated", "noise_sigma", "spikes", "from_cm1", "to_cm1", "peaks", "bands", "x_correction", "background")
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


def _crossings(d: np.ndarray, u: np.ndarray, level: float) -> list[tuple[float, int]]:
    """Where u crosses `level` between samples: (interpolated d, +1 rising / -1 falling with d)."""
    out = []
    for i in range(len(u) - 1):
        a, b = u[i], u[i + 1]
        if not (np.isfinite(a) and np.isfinite(b)) or a == b:
            continue
        if (a - level) * (b - level) <= 0 and not (a == level and i > 0):
            t = (level - a) / (b - a)
            out.append((float(d[i] + t * (d[i + 1] - d[i])), 1 if b > a else -1))
    return out


def depth_result(study: dict, mres: dict, prof: dict, sd: Path, warnings: list) -> dict:
    """A depth profile's view of a Z-only map: each peak's height and area and each band's area against the nominal
    depth (stage displacement into the sample from the focus) and, with a refractive index, the corrected depth
    (paraxial: depth = n x displacement); the interfaces, where a profile crosses half-way between its lowest and
    highest value (rising: a layer starts; falling: it ends), with the 10-90 % width."""
    sgn, stated = plan.into_sign(prof)
    dp = study.get("_depth") or {}
    n = dp.get("refractive_index")
    z = np.asarray(mres["z_um"], float)
    nominal = sgn * z
    depth = nominal * float(n) if n else nominal
    grids = json.loads(Path(mres["files"]["json"]).read_text(encoding="utf-8"))["grids"]
    profiles, interfaces = {}, []
    for key, g in grids.items():
        qn, f = key.rsplit(".", 1)
        if f not in ("height", "area"):
            continue
        v = np.array([np.nan if row[0][0] is None else row[0][0] for row in g], float)
        if not np.isfinite(v).any():
            continue
        profiles[key] = [None if not np.isfinite(a) else round(float(a), 3) for a in v]
        fin = v[np.isfinite(v)]
        lo, hi = float(fin.min()), float(fin.max())
        if hi <= 0 or (hi - lo) < 0.3 * hi:
            continue
        u = (np.nan_to_num(v, nan=lo) - lo) / (hi - lo)
        order = np.argsort(depth)
        dd, uu = depth[order], u[order]
        for x50, direction in _crossings(dd, uu, 0.5):
            near = [c for c, dr in _crossings(dd, uu, 0.1) + _crossings(dd, uu, 0.9) if dr == direction]
            lo_c = max([c for c in near if c <= x50], default=None)
            hi_c = min([c for c in near if c >= x50], default=None)
            interfaces.append({"quantity": key, "direction": "rise" if direction > 0 else "fall",
                               "depth_um": round(x50, 3),
                               "nominal_um": round(x50 / float(n), 3) if n else round(x50, 3),
                               "width_10_90_um": round(hi_c - lo_c, 3) if lo_c is not None and hi_c is not None
                               else None})
    out = {"into_sign": sgn, "direction_stated": stated, "nominal_um": [round(float(a), 3) for a in nominal],
           "refractive_index": n, "depth_um": [round(float(a), 3) for a in depth], "profiles": profiles,
           "interfaces": sorted(interfaces, key=lambda i: i["depth_um"])}
    by: dict = {}
    for it in out["interfaces"]:                     # the first rise and the first fall of each quantity
        q, f = it["quantity"].rsplit(".", 1)
        by.setdefault(q, {}).setdefault(f, {}).setdefault(it["direction"], it["depth_um"])
    out["interfaces_by"] = by
    if n:
        out["note"] = ("depth = n x stage displacement (paraxial). With high-NA objectives the real focus lies deeper "
                       "and spreads (Everall, Appl. Spectrosc. 54, 2000): treat depths as estimates; an oil- or "
                       "water-immersion objective or a known layer thickness calibrates them")
    else:
        out["note"] = ("depths are stage displacements; inside a transparent sample the focus goes about n times "
                       "deeper (give depth.refractive_index to correct)")
    if not stated:
        warnings.append("the profile does not say which Z direction goes into the sample (stage.z_into_sample): "
                        "+ was assumed")
    write_json(sd / "depth.json", clean(out))
    return out


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
                p = r.get("ixyz") or pos[r["point"]]
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
        idx = np.array([[(r.get("ixyz") or pos[r["point"]])[k] for k in ("ix", "iy", "iz")] for r in per_point])
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
