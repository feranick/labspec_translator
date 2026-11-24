"""runner.py — runs a request's studies on the instrument, one command at a time, and checks every result.

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
from . import store
from . import shutter as SH
from . import profile as P
from .bridge import replace_retry, Bridge, BridgeError, BridgeGone, BridgeTimeout

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
    replace_retry(tmp, path)        # Windows: a reader (study_status) holding the file must not crash the runner


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
    """The profile's LabSpec motor names, as the bridge's status, move_xy, autofocus and safe verbs take them, and
    the laser shutter's positions."""
    return {**{f"m_{k}": v for k, v in prof["motors"].items()}, **shutter_args(prof)}


def optics_record(r: dict) -> dict | None:
    """What the bridge read around one Acq: the time since the previous spectrum (and any pause), the spectrometer
    and grating before Acq, right after it returned and after the data, and whether a motor was busy then
    (2026-11-23: spectra after the first in a request shifted and broadened at the same spot)."""
    keys = ("since_last_acq_s", "settled_s", "spectro_before_acq", "spectro_after_acq_call", "spectro_after_data",
            "grating_before_acq", "grating_after_data", "motors_busy_before_acq", "motors_busy_after_acq_call")
    out = {k: _f(r.get(k)) for k in keys if r.get(k) not in (None, "")}
    if not out:
        return None
    sp = [out[k] for k in ("spectro_before_acq", "spectro_after_acq_call", "spectro_after_data") if out.get(k) is not None]
    if len(sp) >= 2:
        out["spectro_moved_nm"] = round(max(sp) - min(sp), 4)
    out["motor_busy"] = bool(out.get("motors_busy_before_acq") or out.get("motors_busy_after_acq_call"))
    return out


def warmup_s(study: dict, prof: dict, laser) -> float:
    """A discarded acquisition before the first spectrum (acquire.warmup_s, else lasers.<laser>.warmup_s): if the
    laser's mode hop is thermal (2026-11-23, 637.75 nm: after the first spectrum of a request), the measured spectra
    then all come after it."""
    w = (study.get("acquire") or {}).get("warmup_s")
    if w is None:
        lk = P.laser_key(prof, laser) if laser else None
        w = (prof["lasers"][lk].get("warmup_s") if lk else None) or 0
    return float(w or 0)


def _warmup(ctx: Ctx, acq: dict, sd: Path, seconds: float) -> dict:
    params = dict(acq, exposure_s=seconds, accumulations=1, out=str(sd / "spectra" / "warmup.txt"),
                  sample="ls6t laser warm-up (discarded)")
    params.pop("verb", None)
    params.pop("native", None)
    t0 = time.time()
    r = ctx.call("acquire", timeout_s=120 + 3 * seconds, **params)
    out = {"seconds": seconds, "ok": bool(r.get("ok")), "took_s": round(time.time() - t0, 2),
           "file": params["out"]}
    try:
        sp = spectra.read(params["out"])
        ll = laser_line_fit(spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), ctx.optics[0])[0], sp["y"])
        if ll:
            out["laser_line"] = ll
    except (OSError, ValueError, TypeError):
        pass
    return out


LASER_JUMP_CM1 = 1.5     # the laser line moving more than this between spectra: a mode hop


def calibration_window(study: dict, grating, n_lines: int) -> float:
    """The fit window for a calibration's reference lines: calibration.window_cm1 if given, else scaled with the
    grating's dispersion (30 cm-1 at 1200 gr/mm and finer, 60 at 600, at most 90; 20 for several close lines at fine
    gratings). 2026-11-23: with the 600 the Si line (FWHM 13) sat at the edge of a 30 cm-1 window: not trusted."""
    w = (study.get("calibration") or {}).get("window_cm1")
    if w:
        return float(w)
    base = 20.0 if n_lines > 1 else 30.0
    g = _f(grating)
    if g and g < 1200:
        base = min(90.0, base * 1200.0 / g)
    return base


def laser_line_fit(shift, y) -> dict | None:
    """The laser line (Rayleigh) near 0 cm-1, when the spectrum covers -15 to +15 cm-1; else None."""
    s = np.asarray(shift, float)
    if s.size < 10 or float(np.min(s)) > -15 or float(np.max(s)) < 15:
        return None
    xs, ys = spectra.sort_xy(s, y)
    pk = spectra.fit_peak(xs, ys, 0.0, 40.0, name="laser")
    return {k: pk.get(k) for k in ("ok", "position_cm1", "fwhm_cm1", "height", "snr", "why") if pk.get(k) is not None}


def laser_line_reference(laser, grating) -> float | None:
    """Where the laser line sat when this laser and grating were last calibrated within tolerance (perf 'zero'
    records it when the window included it); None: the study's first spectrum is the reference."""
    from . import perf
    rec = perf.get("zero", perf.key(laser, grating)) if laser else None
    v = ((rec or {}).get("latest") or {}).get("laser_line_cm1")
    return float(v) if v is not None else None


def same_spot_check(kind: str, per_point: list, warnings: list, unreliable: list) -> dict | None:
    """Spectra at the same spot should agree. 2026-11-23: in a request, point 0 had Si at 520.3 cm-1 (FWHM 3.9) and
    points 1-4 at 512 cm-1 (FWHM 9-10, area kept): every spectrum after the first was wrong, and nothing in LabSpec's
    records showed it. Each spectrum's first fitted peak is compared with point 0's: more than 2 cm-1 apart or 1.5 x
    as wide is flagged (unreliable for repeated spectra; a warning for a series, which may change on purpose)."""
    pts = [r for r in per_point if r.get("peaks")]
    if len(pts) < 2:
        return None
    def key(r):
        o = r.get("offset") or {}
        return tuple(round(float(o.get(k) or 0), 3) for k in ("x_um", "y_um", "dz_um"))
    first = pts[0]
    if kind != "series" and any(key(r) != key(first) for r in pts):
        return None
    p0 = next((pk for pk in first["peaks"] if pk.get("ok")), None)
    if not p0:
        return None
    rows, bad = [], []
    for r in pts[1:]:
        pk = next((q for q in r["peaks"] if q.get("ok") and q.get("name") == p0.get("name")), None)
        if not pk:
            continue
        d = float(pk["position_cm1"]) - float(p0["position_cm1"])
        w = float(pk.get("fwhm_cm1") or 0) / float(p0.get("fwhm_cm1") or 1)
        rows.append({"point": r["point"], "shift_cm1": round(d, 2), "fwhm_ratio": round(w, 2)})
        if abs(d) > 2.0 or w > 1.5:
            bad.append(rows[-1])
    out = {"peak": p0.get("name"), "reference_point": first["point"], "points": rows, "disagree": bad}
    if bad:
        msg = (f"spectra at the same spot disagree with point {first['point']}: "
               + ", ".join(f"point {b['point']} {b['shift_cm1']:+.1f} cm-1, {b['fwhm_ratio']:.1f} x as wide" for b in bad[:6])
               + ". Something changed between acquisitions (TESTING T3.21); the optics_check of each spectrum says "
                 "what the bridge read around it")
        (warnings if kind == "series" else unreliable).append(msg)
    return out


def ae_record(r: dict) -> dict | None:
    """LabSpec's AutoExposure around one acquisition, from the bridge's answer (read through its Template): the value
    before, whether ls6t switched it off, the value read after switching and after putting it back."""
    if r.get("auto_exposure_before") in (None, ""):
        return None
    before = _f(r.get("auto_exposure_before"))
    out = {"before": before, "on_before": bool(before)}
    if r.get("auto_exposure_set") not in (None, ""):
        out.update(switched_off=True, while_acquiring=_f(r.get("auto_exposure_set")))
        out["off_during"] = out["while_acquiring"] == 0
    else:
        out["switched_off"] = False
    if r.get("auto_exposure_after") not in (None, ""):
        out["after"] = _f(r.get("auto_exposure_after"))
        out["restored"] = out["after"] == before
    elif out["switched_off"]:
        out["restored"] = None
    return out


def ae_off(prof: dict) -> int:
    """1 when the bridge may switch LabSpec's AutoExposure off through its template for a fixed exposure."""
    return int(bool((prof.get("autoexposure") or {}).get("switch_off")))


def shutter_args(prof: dict) -> dict:
    """How the bridge moves the laser shutter: by its positions' labels (Off / On), read back."""
    sh = prof.get("shutter") or {}
    return {"shutter_off": str(sh.get("off", "Off")), "shutter_on": str(sh.get("on", "On")),
            "shutter_by": str(sh.get("by", "label"))}


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
        self.saved_config = None            # LabSpec's configuration saved at the start (session.restore_config)
        self.confirmed = False
        self.calls: list = []                  # every bridge command: verb, seconds, the bridge's own t_*_s phases
        self.holding = False                   # the laser shutter held open for a study's spectra
        self.hold_wanted = False               # this study asks for it (run_any)
        self.hold_log: list = []
        self.objective_by_operator = None      # the objective the operator confirmed in this request
        self.objective = None               # LabSpec's objective now (its record)
        self.focus_xy = (None, None)        # where the focus was found (the operator's spot, or the last focus)
        self.z_purpose = None               # 'depth' while a depth profile runs (its own Z limit, confirmed)
        self.focus_stale = False            # after a retract for an objective change: a fresh focus is needed
        self.last_beat = 0.0
        self.bridge_version = None
        self.optics = (None, None)          # (laser nm, grating) after the last setup: for ics.off_for
        self.last_ics = None                # the ICS state of the last acquisition sent

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
        if verb == "acquire" and "mode" in params:
            digit = (int(params["mode"]) // 100000) % 10
            if digit == 0 and plan.ics_off(self.prof, *self.optics):
                # no ICS correction file for this grating and laser: LabSpec would stop at a modal dialog (Error 2070)
                params["mode"] = int(params["mode"]) + plan.ACQ_NO_ICS
                self.last_ics = (f"off (no correction file for grating {self.optics[1]} with "
                                 f"{_f(self.optics[0]) or 0:g} nm)")
            else:
                # with and without ICS, intensities are not comparable: each spectrum says which (2026-11-23)
                self.last_ics = {1: "off (asked)", 2: "on (asked)"}.get(digit, "LabSpec's setting (a correction "
                                                                              "file exists for this pair)"
                                                                       if plan.ics_files(self.prof) is not None
                                                                       else "LabSpec's setting")
        laser_off = str(params.get("laser_off", "0")) == "1"
        self._log(f"-> {verb} " + " ".join(f"{k}={v}" for k, v in params.items()
                                             if k in ("motor", "value", "action", "kind", "state", "mode", "exposure_s")))
        if self.hold_wanted and not self.holding and verb in ("acquire", "map_native", "guide") and not laser_off:
            self._hold_on()
        if verb == "acquire":
            params.setdefault("ae_off", ae_off(self.prof))
            if self.holding:
                params.setdefault("settle_s", 0.0)       # the shutter is held open: no pause needed
            # a pause after the previous spectrum (TESTING T3.21: the spectra after the first in a request were
            # shifted and broadened)
            params.setdefault("settle_s", plan.settle_s(self.prof, self.optics[0]))
        t0 = time.monotonic()
        try:
            r = self.bridge.call(verb, timeout_s=timeout_s, should_stop=self.cancelled, on_wait=self.beat, **params)
            rec = {"verb": verb, "s": round(time.monotonic() - t0, 3)}
            rec.update({k: _f(v) for k, v in r.items() if k.startswith("t_") and k.endswith("_s") and _f(v) is not None})
            if verb == "move" and params.get("motor"):
                rec["motor"] = params["motor"]
            if str(r.get("spectro_skipped", "")) == "1":
                rec["spectro_skipped"] = True
            self.calls.append(rec)
        except BridgeGone as exc:
            raise Stop(self._held_warning(str(exc)), "bridge") from exc
        except BridgeTimeout as exc:
            raise Stop(self._held_warning(str(exc)), "timeout") from exc
        self.beat()
        self._log(f"<- {verb} ok={int(bool(r.get('ok')))} {r.get('seconds', '')} s"
                  + (f" error={r.get('error')}" if r.get("error") else ""))
        if self.holding and (verb not in HOLD_VERBS or laser_off or (verb == "shutter_hold" and params.get("state") == "off")):
            self.holding = False                 # the bridge let it go (bridge_main.vbs, LaserGuard)
            self.hold_log.append({"released": True, "by": verb})
        if verb == "acquire" and r["ok"] and params.get("out"):
            self._exposure_used(r, params)
        if not r["ok"]:
            if r.get("stopped_by") == "operator" or r.get("halted") == "1":
                raise Stop("stopped by the operator in LabSpec", "operator")
            if r.get("stopped_by") == "request" or self.cancelled():
                raise Stop("cancelled", "cancel")
        return r

    def _log(self, line: str) -> None:
        """One line to the request's runner.log, written and flushed at once (2026-11-24: after LabSpec crashed
        in a map view the log was empty: the last call before a hang or crash is now always there)."""
        try:
            with open(self.folder / "runner.log", "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {line}{' [shutter held]' if self.holding else ''}\n")
                fh.flush()
        except OSError:
            pass

    def _held_warning(self, msg: str) -> str:
        """The bridge stopped answering while ls6t held the laser shutter open: say so first."""
        if self.holding and LASER_MAY_BE_ON not in msg:
            return LASER_MAY_BE_ON + " " + msg
        return msg

    def _hold_on(self) -> None:
        """The laser shutter held open (ShutterMode manual) before this study's next acquisition, as HORIBA's
        mapping scripts do for a map; the bridge keeps it only through HOLD_VERBS."""
        r = self.call("shutter_hold", timeout_s=30, state="on", max_s=getattr(self, "hold_max_s", 3600),
                      **motor_args(self.prof))
        if not r.get("ok"):
            raise Stop(f"acquire.shutter 'hold': {r.get('error')}", "study")
        self.holding = True
        self.hold_log.append({"held": True, "shutter_before": r.get("old_index"), "mode_before": r.get("old_mode"),
                              "label_while_held": r.get("hold_label")})

    def _exposure_used(self, r: dict, params: dict) -> None:
        """The exposure LabSpec really used, from the spectrum's acquisition info. 2026-11-23: with LabSpec's
        AutoExposure on (status bar 'AE'), studies asking 2 s ran 15 s while ls6t reported 2 s. The bridge now switches
        AutoExposure off for a fixed exposure; if LabSpec still used another one, nothing measured this way is right
        (counts per second, comparisons): the study stops."""
        try:
            meta = spectra.header(params["out"])
        except OSError:
            return
        used = spectra.acq_exposure(meta)
        asked = _f(params.get("exposure_s")) or 0.0
        if meta.get("acq.AutoExposure"):
            r["labspec_auto_exposure"] = meta["acq.AutoExposure"]
        if used is None:
            return
        r["exposure_asked_s"] = asked if asked > 0 else "auto"
        r["exposure_s"] = used
        r["exposure_source"] = "LabSpec's acquisition info"
        if asked > 0 and abs(used - asked) > EXPOSURE_TOL * asked:
            r["exposure_mismatch"] = True
            raise Stop(f"LabSpec used {used:g} s per accumulation instead of the {asked:g} s asked (its AutoExposure is "
                       f"{meta.get('acq.AutoExposure', 'probably on')}; the bridge could not switch it off: "
                       f"{r.get('auto_exposure_before', '?')} -> {r.get('auto_exposure_set', '?')}): switch AutoExposure "
                       "off in LabSpec (the 'AE' in the status bar) and run again, or ask for exposure_s 'auto'", "study")


PHASES = (("t_motors_before_s", "waiting for motors before the spectrometer move (a grating turret still turning?)"),
          ("t_spectro_call_s", "MoveMotor Spectro (to the window's centre)"),
          ("t_spectro_wait_s", "until the spectrometer reported idle"),
          ("t_motors_after_s", "until every motor reported idle"),
          ("t_flush_s", "collecting earlier results from LabSpec"),
          ("t_acq_call_s", "LabSpec's Acq call (it can hold the script)"),
          ("t_until_data_s", "after Acq, until the spectrum was there (exposure, readout)"),
          ("t_write_s", "writing the spectrum file"),
          ("t_save_s", "sample, remark and the .l6s"))


OPERATOR_VERBS = ("confirm", "notice")


def timing(calls: list, per_point: list, total_s: float) -> dict:
    """Where a study's time went (2026-11-23: 42-64 s for 4 s of exposure, 10-15 s on 2026.11.22.15): per bridge
    command, the acquisition's phases inside the bridge (t_*_s), the time between the bridge and ls6t, and ls6t's
    own work (set-up checks, analysis, plots)."""
    exposure = sum((_f(r.get("exposure_s")) or 0) * (_f(r.get("accumulations")) or 1) for r in per_point)
    by_verb: dict = {}
    for c in calls:
        v = by_verb.setdefault(c["verb"] + (f" {c['motor']}" if c.get("motor") else ""), {"n": 0, "s": 0.0})
        v["n"] += 1
        v["s"] = round(v["s"] + c["s"], 2)
    acq = [c for c in calls if c["verb"] == "acquire"]
    # the operator reading and answering LabSpec's questions (the laser's Yes, the objective, a notice): not overhead
    operator = round(sum(c["s"] for c in calls if c["verb"] in OPERATOR_VERBS), 2)
    phases = {k: round(sum(c.get(k) or 0 for c in acq), 2) for k, _ in PHASES if any(c.get(k) is not None for c in acq)}
    inside = sum(c.get("t_acquire_total_s") or 0 for c in acq)
    bridge_s = sum(c["s"] for c in calls)
    out = {"total_s": round(total_s, 2), "exposure_s": round(exposure, 2), "operator_s": operator,
           "overhead_s": round(total_s - exposure - operator, 2),
           "acquire_s": round(sum(c["s"] for c in acq), 2), "bridge_commands": by_verb, "bridge_s": round(bridge_s, 2),
           "ls6t_s": round(max(0.0, total_s - bridge_s), 2)}
    if acq:
        out["acquire_phases_s"] = phases
        out["acquire_phases"] = {k: d for k, d in PHASES if k in phases}
        if inside:
            out["acquire_handoff_s"] = round(sum(c["s"] for c in acq) - inside, 2)   # files and polling, both ways
        if any(c.get("spectro_skipped") for c in acq):
            out["spectro_move_skipped"] = sum(1 for c in acq if c.get("spectro_skipped"))
    big = max(((k, v) for k, v in phases.items() if k not in ("t_until_data_s",)), key=lambda kv: kv[1],
              default=None)
    if big and big[1] > max(5.0, 0.25 * max(0.0, total_s - operator)):
        out["note"] = f"most of the overhead: {dict(PHASES)[big[0]]} ({big[1]:.1f} s)"
    return out


EXPOSURE_TOL = 0.02     # LabSpec's exposure within 2 % of the one asked


def _shutter_gate(prof: dict, laser) -> None:
    """At run time too: the shutter only where it has been shown to block this laser on Si (shutter.py)."""
    g = SH.gate(prof, laser)
    if g:
        raise Stop(f"laser_off 'shutter' refused: {g}", "study")


WATCH_MIN_S = 0.5      # the bridge read the shutter while LabSpec was still acquiring for at least this long


def shutter_report(r: dict, warnings: list, verified: bool = False) -> dict:
    """What the bridge read of the laser shutter around a laser_off acquisition, and whether it could read it during
    the acquisition at all: LabSpec's Acq can hold the script until the spectrum is taken (2026-11-23), and then the
    reads start only afterwards; LabSpec could have opened and closed the shutter unseen. The spectral test (TESTING
    T1.2 step 2) is the real check; the background's narrow-line check only catches samples with sharp lines (Si)."""
    out = {k: r.get(k) for k in ("shutter_mode_before", "shutter_label_before", "shutter_closed_label",
                                 "shutter_closed_value", "shutter_closed_mode", "shutter_closed_by", "shutter_during",
                                 "shutter_label_after") if r.get(k) not in (None, "")}
    held, after = _f(r.get("t_acq_call_s")), _f(r.get("t_until_data_s"))
    reads = int(_f(r.get("shutter_reads")) or 0)
    out.update(reads=reads, first_read_s=_f(r.get("shutter_first_read_s")), acq_call_held_s=held,
               after_acq_until_data_s=after)
    if held is not None and after is not None and held + after > 0:
        out["watched_fraction"] = round(after / (held + after), 3)
        out["during_checked"] = bool(reads and after >= WATCH_MIN_S)
        if not out["during_checked"] and verified:
            # on the XploRA Acq holds the bridge for the whole exposure (2026-11-23): the shutter-verify record is the
            # proof, not these reads
            out["during_note"] = ("LabSpec held the bridge during the acquisition (as on the XploRA): the reads are "
                                  "before and after only; the shutter's verification on Si is what shows it blocks")
        elif not out["during_checked"]:
            warnings.append(f"the bridge could not read the laser shutter during the acquisition: LabSpec's Acq held it "
                            f"for {held:.1f} s and the data came {after:.1f} s later, so only the reads before and "
                            "after count; LabSpec could have opened and closed the shutter unseen. Only the spectral "
                            "test (TESTING T1.2 step 2) shows that the beam was blocked")
    else:
        out["during_checked"] = None
    out["note"] = ("read-backs and the shutter test are the protection; the narrow-line check on the background "
                   "only catches samples with sharp Raman lines (Si), not broad features or fluorescence")
    return out


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
            raise Stop("the bridge is halted by the operator: resume with `ls6t instrument resume` (LabSpec then asks the operator to confirm)", "operator")
        st = ctx.call("status", timeout_s=15, **motor_args(prof))
        ctx.start = {k: _f(st.get(k)) for k in ("x_um", "y_um", "z_um")}
        ctx.start["laser_nm"] = _f(st.get("laser_nm"))
        ctx.home_xy = (ctx.start["x_um"], ctx.start["y_um"])
        ctx.z_focus = ctx.start["z_um"]
        ctx.focus_xy = ctx.home_xy
        ctx.objective = st.get("objective")
        write_json(folder / "start.json", {"status": st, "time": time.time()})
        if prof.get("session", {}).get("restore_config"):
            ini = folder / "labspec_before.ini"
            r = ctx.call("config", timeout_s=60, action="save", path=str(ini))
            ctx.saved_config = str(ini) if r["ok"] else None
            if not r["ok"]:
                raise Stop(f"session.restore_config: LabSpec could not save its configuration ({r.get('error')})",
                           "bridge")
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
        if stopped:
            # a halt or a cancel leaves the stage where it is: said, with where that is (2026-11-22)
            state["stage_at_end"] = _stage_now(ctx)
            state["returned"] = _not_returned(stopped.by, state["stage_at_end"])
        else:
            state["returned"] = _return_home(ctx)
            if getattr(ctx, "saved_config", None):
                r = ctx.call("config", timeout_s=60, action="load", path=ctx.saved_config)
                state["config_restored"] = bool(r["ok"])
                if not r["ok"]:
                    state["config_restore_error"] = r.get("error")
            state["stage_at_end"] = _stage_now(ctx)
    except Stop as exc:
        state.update(state="cancelled" if exc.by in ("cancel", "operator") else "failed", why=exc.why,
                     stopped_by=exc.by, finished=time.time())
        try:
            state["stage_at_end"] = _stage_now(ctx)
        except Exception:  # noqa: BLE001 - the bridge may be gone
            state["stage_at_end"] = {}
        state["returned"] = _not_returned(exc.by, state["stage_at_end"])
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
        if step["mode"] == "label":
            # LabSpec's own name for the position ('x100' -> 'x100_VIS' when the profile is not from discovery)
            lr = ctx.call("labels", timeout_s=30, motor=step["motor"])
            labs = [lr.get(f"label.{i}", "") for i in range(min(int(float(lr.get("size") or 0)), 60))] \
                if lr.get("ok") else []
            if labs:
                lab, err = P.match_label(step["value"], labs)
                if lab:
                    step = dict(step, value=lab)
        if plan.already_set(step, before):
            # already in place: not sent (and a manual objective is not asked for again)
            readback.append({"motor": step["motor"], "asked": step["value"], "got": before.get(step["role"]),
                             "ok": True, "already_set": True, "error": None})
            continue
        if step["motor"] == prof["motors"]["objective"] and prof.get("manual_objective", True) \
                and str(step["value"]) != str(before.get("objective")) and config.confirm_policy() != "never":
            # a manual turret: the operator changes the lens by hand; LabSpec only records it (then moved below)
            from .api import objective_question, objective_retract, objective_retract_wanted
            lowered = None
            if objective_retract_wanted(prof):
                lowered = objective_retract(lambda **kw: ctx.call("move", timeout_s=180, **kw), prof,
                                            _f(before.get("z_um")))
                if lowered is not None:
                    ctx.focus_stale = True          # another lens: Z toward the sample needs a fresh focus
                    warnings.append(f"the stage was lowered {plan.retract_um(prof):g} µm before the objective change: "
                                    "focus again (a 'focus' study with focus.scan.from_below) before measuring")
            ans = ctx.call("confirm", timeout_s=3600, text=objective_question(prof, before.get("objective"),
                                                                              step["value"]))
            if ans.get("answer") != "yes":
                raise Stop("the operator declined to change the objective", "operator")
            ctx.objective_by_operator = step["value"]
            asked_here = True
        else:
            asked_here = False
        p = {k: v for k, v in step.items() if k not in ("verb", "why")}
        long_move = p["motor"] in (prof["motors"]["laser"], prof["motors"]["grating"], prof["motors"]["objective"])
        r = ctx.call("move", timeout_s=600 if long_move else 120, **p, **move_extra(prof, p["motor"], asked_here))
        ok, got = _matches(step, r)
        from .api import _move_timing
        readback.append({"motor": step["motor"], "asked": step["value"], "got": r.get("label") or r.get("value"),
                         "ok": bool(r["ok"] and ok), "error": r.get("error") or None, **_move_timing(r)})
        if p["motor"] == prof["motors"]["grating"] and r.get("ok") and not ok:
            # once more, after selecting the laser again, then stop: a study on the wrong grating is wrong
            # (2026-11-23: after a scripted visit to 785 nm LabSpec kept refusing 2400 (its range ends at 690 nm) and
            # took 1800, also once back on 637.75 nm; selecting 2400 by hand cleared it)
            r, tried = retry_grating(ctx.call, prof, dict(p, **move_extra(prof, p["motor"], asked_here)), step["value"])
            ok, got = _matches(step, r)
            readback.append({"motor": step["motor"], "asked": step["value"], "got": r.get("label") or r.get("value"),
                             "ok": bool(r["ok"] and ok), "error": r.get("error") or None, "retry": tried,
                             **_move_timing(r)})
            if not (r["ok"] and ok):
                raise Stop(f"the grating did not reach {step['value']} (it reads {got}) after three moves: nothing "
                           "measured on the wrong grating. After the laser was changed by the bridge (e.g. a visit to "
                           "785 nm), LabSpec can refuse a grating the earlier laser could not use, until the bridge "
                           "script is restarted (2026-11-23): `ls6t instrument end-bridge`, start ls6t_bridge.vbs "
                           "again in LabSpec, and run the study again (or select the grating by hand)", "study")
        if p["motor"] == prof["motors"]["grating"] and r.get("ok"):
            note_grating_set(step["value"], r.get("value"), f"study {name}")
        if not r["ok"] or not ok:
            unreliable.append(f"{step['why']}: asked {step['value']}, " +
                              (f"LabSpec said: {r.get('error')}" if not r["ok"] else f"got {got}"))
    after = ctx.call("status", timeout_s=15, **motor_args(prof))
    ctx.objective = after.get("objective") or ctx.objective
    laser = _f(after.get("laser_nm")) or plan.laser_nm(study, prof) or ctx.start.get("laser_nm")
    ctx.optics = (laser, after.get("grating"))
    rng0 = plan.acq_range(study, prof, laser)
    if (study.get("setup") or {}).get("grating") is None and rng0.get("to_nm") is not None and \
            study.get("kind", "spectrum") not in ("alignment", "guide"):
        import re as _re
        mg = _re.match(r"\s*(\d+(?:\.\d+)?)", str(after.get("grating") or ""))
        reach = plan.grating_reach(prof, mg.group(1), laser) if mg else None
        if reach and float(rng0["to_nm"]) > reach["limit_nm"]:
            raise Stop(f"LabSpec's grating {reach['grating']} cannot reach {rng0.get('to_cm1')} cm-1 with the "
                       f"{laser:g} nm laser (up to {reach['max_shift_cm1']} cm-1, {reach['basis']}): nothing was "
                       "measured; give setup.grating (a coarser one) or a shorter range", "study")
    if plan.ics_off(prof, *ctx.optics) and (study.get("acquire") or {}).get("ics", "labspec") == "labspec":
        warnings.append(f"ICS (intensity correction) off for grating {after.get('grating')} with {laser:g} nm: LabSpec "
                        "has no correction file for it (profile ics.off_for); intensities are not corrected")
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
        obj_q = (f" Objective {after.get('objective')} as LabSpec has it: is that lens on the turret?"
                 if prof.get("manual_objective", True) else f" Objective {after.get('objective')}.")
        what = (f"ls6t is about to expose '{study.get('sample', {}).get('name', name)}' ({kind}, {n_points} "
                f"point(s)) with laser {laser:g} nm, filter {after.get('filter')}.{obj_q} Continue?")
        r = ctx.call("confirm", timeout_s=3600, text=what)
        if r.get("answer") != "yes":
            raise Stop("the operator declined in LabSpec", "operator")
        ctx.confirmed = True
        ctx.objective_by_operator = after.get("objective")
    # the objective is LabSpec's record: on a manual turret only the operator can tell which lens is in place
    if prof.get("manual_objective", True):
        by_op = getattr(ctx, "objective_by_operator", None) == after.get("objective")
        checks["objective"] = {"value": after.get("objective"),
                               "source": "confirmed by the operator in this request" if by_op else
                                         "LabSpec's setting, not checked (manual turret)"}
        if not by_op and kind in ("move", "spot", "focus", "map", "depth") :
            warnings.append(f"objective {after.get('objective')}: LabSpec's setting, not checked by the operator "
                            "(manual turret): if another lens is in place, µm scales and the beam offset are wrong")
    return {"before": before, "after": after, "laser": laser, "temp": temp, "readback": readback, "rng": rng}


LASER_MAY_BE_ON = ("THE LASER MAY STILL REACH THE SAMPLE: ls6t held the laser shutter open (LabSpec's ShutterMode "
                   "manual) when the bridge stopped answering. Close it now: switch the laser off in LabSpec's status "
                   "bar if LabSpec responds, otherwise switch the laser off at its power supply or key; after "
                   "restarting LabSpec, check that the laser shutter reads Off and ShutterMode is automatic (0).")
NO_HOLD_KINDS = ("move", "background", "cutoff")      # no laser, or the laser off on purpose
# the bridge commands that keep the shutter held (bridge_main.vbs HOLD_VERBS)
HOLD_VERBS = ("ping", "status", "acquire", "map_native", "guide", "move_xy", "move", "remove", "save", "labels",
              "last_error", "message", "shutter_hold")      # no display call (view, video): released before one


def run_any(name: str, study: dict, ctx: Ctx) -> dict:
    """Runs one study. With acquire.shutter 'hold' (or the laser's hold_shutter) the laser shutter is held open
    around every acquisition of the study, whatever its kind (2026-11-23: a 'filters' study with 637.75 nm was not
    held and read its second filter 0.54 x low); Ctx.call holds it before an acquisition and notes when the bridge
    lets it go; released here at the end."""
    kind = study.get("kind", "spectrum")
    laser = (study.get("setup") or {}).get("laser_nm")
    if laser is None:
        laser = (ctx.start or {}).get("laser_nm")
    ctx.hold_wanted = kind not in NO_HOLD_KINDS and plan.hold_shutter(study, ctx.prof, laser)
    ctx.hold_log = []
    if ctx.hold_wanted:
        try:
            ctx.hold_max_s = round(plan.estimate(study, ctx.prof)["seconds"]["total"] * 1.5 + 300)
        except (KeyError, ValueError, TypeError):
            ctx.hold_max_s = 3600
    try:
        res = _run_kind(name, study, ctx)
    finally:
        ctx.hold_wanted = False
        if ctx.holding:
            ctx.holding = False
            try:
                r = ctx.call("shutter_hold", timeout_s=30, state="off")
                ctx.hold_log.append({"released": bool(r.get("ok"))})
            except Stop as exc:
                ctx.hold_log.append({"released": False, "why": exc.why})
    if isinstance(res, dict) and ctx.hold_log:
        holds = [h for h in ctx.hold_log if "held" in h]
        res["shutter_hold"] = {"held": bool(holds), "times_held": len(holds),
                               "released": all(h.get("released", True) for h in ctx.hold_log if "released" in h),
                               "shutter_before": holds[0].get("shutter_before") if holds else None,
                               "mode_before": holds[0].get("mode_before") if holds else None,
                               "label_while_held": holds[0].get("label_while_held") if holds else None,
                               "log": ctx.hold_log[-20:]}
        if len(holds) > 1:
            res.setdefault("warnings", []).append(
                f"the laser shutter was held {len(holds)} times in this study: another command (a snapshot, an "
                "autofocus, the operator's question) let it go in between, and LabSpec closed and opened it")
        f = ctx.folder / name / f"{name}.json"
        if f.exists():
            write_json(f, clean(res))
    return res


def _run_kind(name: str, study: dict, ctx: Ctx) -> dict:
    kind = study.get("kind", "spectrum")
    if kind == "move":
        return run_move(name, study, ctx)
    if kind == "focus":
        return run_focus(name, study, ctx)
    if kind == "spot":
        return run_spot(name, study, ctx)
    if kind == "calibration" and (study.get("calibration") or {}).get("adjust") in ("labspec", "auto"):
        return run_calibration(name, study, ctx)
    if kind == "background":
        return run_background(name, study, ctx)
    if kind == "filters":
        return run_filters(name, study, ctx)
    if kind == "cutoff":
        return run_cutoff(name, study, ctx)
    if kind == "depth":
        derived = plan.depth_as_map(study, ctx.prof)
        derived["_kind"] = "depth"
        derived["_depth"] = dict(study.get("depth", {}))
        _confirm_depth(name, derived, ctx)
        ctx.z_purpose = "depth"
        try:
            return run_study(name, derived, ctx)
        finally:
            ctx.z_purpose = None
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
    n_calls = len(ctx.calls)
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
    # a calibration takes a frame first unless the profile's calibration.video_before is false (17-20 s each in a
    # batch of calibrations, 2026-11-23) or the study says otherwise
    want_before = vd.get("before", kind == "calibration" and bool((prof.get("calibration") or {}).get("video_before", True)))
    focus_scans: list = []
    snaps: dict = {}
    # LabSpec's own map engine: not the same as save_native (`native` above), which this name once overwrote, so no
    # .l6s was saved for ordinary spectra and their Remark could not be checked (2026-11-23)
    native_map = plan.map_engine(study, prof) == "native"
    if native_map:
        try:
            if af:
                if af == "scan":
                    fs = focus_scan(ctx, study, laser, zref if zref is not None else zcur, sd / "focus" / "start",
                                    warnings)
                    focus_scans.append(fs)
                    if fs.get("found"):
                        zref = zcur = ctx.z_focus = fs["z_um"]
                        _focus_here(ctx)
                else:
                    r = ctx.call("autofocus", timeout_s=240, how=af, **_af_bound(ctx), **motor_args(prof))
                    plan.record_af_motion(r)
                    if r["ok"] and _f(r.get("z_um")) is not None:
                        zref = zcur = ctx.z_focus = _f(r.get("z_um"))
                        _focus_here(ctx)
            if want_before:
                snaps["before"] = _snapshot(ctx, name, sd, "before", bool(vd.get("with_laser")), warnings, laser)
            t_a = time.time()
            per_point = _native_map(ctx, name, study, sd, rng, laser, acq, x0, y0, zref, warnings)
            t_acq += time.time() - t_a
        except Stop as exc:
            stop_exc = exc
    warmup_info = None
    for k, p in enumerate(pts if not native_map else []):
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
                zcur = _retract_before_xy(ctx, ax, ay, zcur, zref, f"point {k}")
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
                    r = ctx.call("autofocus", timeout_s=240, how=af, **_af_bound(ctx), **motor_args(prof))
                    plan.record_af_motion(r)
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
            if k > 0 and (prof.get("timing") or {}).get("status_before_each"):
                # diagnostic (TESTING T3.21, 2026-11-23): the bridge's status read before every spectrum, as at the
                # start of a request; the 637.75 nm laser's mode was right for the first spectrum of each request only
                ctx.call("status", timeout_s=15, **motor_args(prof))
            if k == 0 and warmup_s(study, prof, laser) > 0:
                warmup_info = _warmup(ctx, acq, sd, warmup_s(study, prof, laser))
            out = sd / "spectra" / f"p{k:05d}.txt"
            params = dict(acq)
            params.pop("verb", None)
            params["out"] = str(out)
            params["sample"] = study.get("sample", {}).get("name", name)
            # LabSpec's Title / Project / Site: the study, the request, the sample's site (or cleared)
            params.update(title=name, project=f"ls6t {ctx.folder.name}",
                          site=str(study.get("sample", {}).get("site", ""))[:100])
            if study.get("sample", {}).get("notes"):
                params["remark"] = str(study["sample"]["notes"])[:200]     # LabSpec's Remark, after "ls6t <seq>"
            if native and kind != "map":             # series too (2026-11-23: no .l6s for series spectra)
                params["native"] = str(sd / "spectra" / f"p{k:05d}.l6s")
            if kind == "series" and k == 0:
                t_series0 = time.time()
            t_a = time.time()
            r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
            # when this spectrum's acquisition was sent (series: start to start), and when LabSpec began it
            t_sent = round(t_a - t_start, 3)
            t_acq += time.time() - t_a
            if not r["ok"]:
                raise Stop(f"point {k}: acquisition failed: {r.get('error')}", "study")
            if k == 0:
                grating_guard(prof, study, after, laser, rng, out, checks, warnings, r)
            rec = {"point": k, "offset": {"x_um": p["x_um"], "y_um": p["y_um"], "dz_um": p["dz_um"]},
                   "stage": {"x_um": ax, "y_um": ay, "z_um": zcur}, "file": str(out),
                   "native": params.get("native"), "id": r.get("id"), "t": round(time.time() - t_start, 3),
                   "exposure_s": _f(r.get("exposure_s")), "accumulations": _f(r.get("accumulations")),
                   "detector_temp_c": _f(r.get("detector_temp_c")), "ics": ctx.last_ics,
                   "auto_exposure": ae_record(r), "optics_check": optics_record(r), "t_start": t_sent,
                   **({"settled_s": _f(r.get("settled_s"))} if r.get("settled_s") not in (None, "") else {})}
            per_point.append(rec)
            # live view in LabSpec: the map or the series grows as it is measured
            if kind in ("map", "series") and r.get("id") and (prof.get("display") or {}).get("live_view"):
                # off by default: on 2026-11-24 LabSpec froze, then crashed, in the MapEx live view
                zonly = kind == "map" and plan.map_shape(study)[:2] == (1, 1)     # a depth profile
                vval = rec["t"] if kind == "series" else p["dz_um"]
                try:
                    nx_, ny_, nz_ = plan.map_shape(study) if kind == "map" else (1, 1, 1)
                    grid = kind == "map" and not zonly and nz_ == 1
                    vfrom, vto = rng.get("from_nm"), rng.get("to_nm")
                    if kind == "map" and not grid and (not vfrom or not vto):
                        # Map() averages the intensity between From and To: never 0 - 0 (E_FAIL, 2026-11-23)
                        xs_ = spectra.read(out)["x"]
                        vfrom, vto = float(np.min(xs_)), float(np.max(xs_))
                    gx = {}
                    if grid:
                        m_ = study.get("map", {})
                        gx = dict(axis_x=",".join(f"{v:g}" for v in (plan.axis_values(m_["x_um"]) if m_.get("x_um") else [0.0])),
                                  axis_y=",".join(f"{v:g}" for v in (plan.axis_values(m_["y_um"]) if m_.get("y_um") else [0.0])),
                                  index=int(p.get("iy", 0)) * nx_ + int(p.get("ix", 0)))
                    if kind == "map" and not grid and not zonly:
                        view = False         # LabSpec's Map() returned E_FAIL whatever its range (2026-11-24)
                    if view is None:
                        vr = ctx.call("view", timeout_s=30, action="create", name=name, vfrom=vfrom, vto=vto,
                                      kind="mapex" if grid else ("map" if kind == "map" and not zonly else "profile"),
                                      id=r["id"], x=p["x_um"], y=p["y_um"], value=vval,
                                      label="time" if kind == "series" else ("Z" if zonly else "XY"),
                                      unit="s" if kind == "series" else "um", **gx)
                        view = vr.get("view") if vr["ok"] else False
                        if not vr["ok"]:
                            warnings.append(f"live view in LabSpec not available: {vr.get('error')}")
                    elif view:
                        vr = ctx.call("view", timeout_s=30, action="add", name=name, id=r["id"],
                                      x=p["x_um"], y=p["y_um"], value=vval, vfrom=vfrom, vto=vto, **gx)
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
    tm = timing(ctx.calls[n_calls:], per_point, time.time() - t_start)
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
                 "operator": tm["operator_s"], "analysis": round(time.time() - t_an, 2)},
        timing=tm,
        **({"warmup": warmup_info} if warmup_info else {}),
        execution={"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                   "protocol": BRIDGE_PROTOCOL})
    if not per_point:
        result["why"] = stop_exc.why if stop_exc else "no spectrum was measured"
    if kind == "calibration" and (result.get("calibration") or {}).get("fwhm_cm1"):
        _record_resolution(result, after, prof, warnings, str(ctx.folder))
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


def _focus_here(ctx: Ctx) -> None:
    """The focus was just found here: offsets toward the sample are measured from this spot's focus again."""
    ctx.focus_stale = False
    s = _stage_now(ctx)
    if s.get("x_um") is not None:
        ctx.focus_xy = (s["x_um"], s["y_um"])


def _z_guard(ctx: Ctx, target: float, what: str, focusing: bool, purpose: str | None = None) -> None:
    """Z from the focus within the objective's limits (plan.z_limits: toward the sample a fraction of its working
    distance, away limits.max_dz_um; both ways the toward limit until stage.z_into_sample is set); and toward the
    sample only while the focus is fresh: after an XY move farther than focus_valid_xy_um from where the focus was
    found (short working distances), only a focus scan or an autofocus may approach (they find it again)."""
    if ctx.z_focus is None or target is None:
        return
    zl = plan.z_limits(ctx.prof, ctx.objective, purpose or getattr(ctx, "z_purpose", None))
    dz = float(target) - float(ctx.z_focus)
    err = plan.z_check(zl, dz)
    if err:
        raise Stop(f"{what}: {err}", "study")
    if not focusing and plan.toward(zl, dz) and getattr(ctx, "focus_stale", False):
        raise Stop(f"{what}: the stage was lowered for an objective change: Z toward the sample needs a fresh focus "
                   "first (a focus scan or an autofocus)", "study")
    if focusing or not plan.toward(zl, dz) or zl["focus_valid_xy_um"] is None or None in ctx.focus_xy:
        return
    s = _stage_now(ctx)
    if s.get("x_um") is None:
        return
    d = math.hypot(s["x_um"] - ctx.focus_xy[0], s["y_um"] - ctx.focus_xy[1])
    if d > zl["focus_valid_xy_um"]:
        raise Stop(f"{what}: the focus was found {d:.0f} µm away (more than {zl['focus_valid_xy_um']:g} µm for "
                   f"{ctx.objective or 'this objective'}): Z toward the sample needs a fresh focus here first (a "
                   "focus scan or an autofocus)", "study")


def _confirm_depth(name: str, derived: dict, ctx: Ctx) -> None:
    """A depth profile going further toward the sample than ordinary studies may (past limits.z_toward_wd_fraction
    of the working distance, up to depth_toward_wd_fraction): the operator confirms the gap in LabSpec first."""
    obj = (derived.get("setup") or {}).get("objective") or ctx.objective
    lab, _ = P.match_label(str(obj), list(ctx.prof.get("objectives", []))) if obj else (None, None)
    obj = lab or obj
    normal = plan.z_limits(ctx.prof, obj)
    zs = plan.axis_values(derived["map"]["z_um"]) if (derived.get("map") or {}).get("z_um") else []
    deepest = max([abs(z) for z in zs if plan.toward(normal, z)] or [0.0])
    if deepest <= normal["toward_um"] or config.confirm_policy() == "never":
        return
    wd = normal["working_distance_um"]
    r = ctx.call("confirm", timeout_s=3600,
                 text=f"ls6t: depth profile '{name}' goes up to {deepest:g} µm from the focus TOWARD the sample with "
                      f"{obj or 'this objective'} (working distance ~{wd:g} µm: about {wd - deepest:g} µm of gap left "
                      "at the deepest point, if the surface is flat). Check the gap before going on. Continue?")
    if r.get("answer") != "yes":
        raise Stop("the operator did not confirm the depth profile's Z range", "operator")


def _z_direct(ctx: Ctx, target: float, what: str) -> float:
    """A Z move away from the sample (retract, or the descent that stops short of the old focus): checked against
    the profile's absolute stage limits only; travel_xy decides that it moves away."""
    _check_abs(ctx.prof, "z", target, what)
    r = ctx.call("move", timeout_s=180, motor=ctx.prof["motors"]["z"], mode="value", value=round(target, 3))
    if not r["ok"]:
        raise Stop(f"{what}: Z move failed: {r.get('error')}", "study")
    return _f(r.get("value")) if _f(r.get("value")) is not None else target


def travel_xy(ctx: Ctx, tx: float, ty: float, unevenness_um: float, what: str) -> dict:
    """Retract-travel-return for an uneven sample: Z goes limits.retract_um away from the sample (from the focus),
    the stage travels to (tx, ty), and Z comes back to the old focus minus the sample's unevenness, still on the
    away side: the new spot's surface is at most that much higher, so nothing is touched; the focus is then searched
    approaching from below (focus.scan.from_below). Needs stage.z_into_sample (TESTING T3.17)."""
    zl = plan.z_limits(ctx.prof, ctx.objective)
    s = zl["toward_sign"]
    if s is None:
        raise Stop(f"{what}: a retract-travel-return needs stage.z_into_sample (TESTING T3.17)", "study")
    st = ctx.call("status", timeout_s=15, **motor_args(ctx.prof))
    z0 = _f(st.get("z_um"))
    zf = ctx.z_focus if ctx.z_focus is not None else z0
    if zf is None:
        raise Stop(f"{what}: the Z position could not be read: no retract-travel-return", "study")
    R = plan.retract_um(ctx.prof)
    z_up = zf - s * R
    if z0 is not None and (z0 - z_up) * s < 0:      # already farther away than that
        z_up = z0
    z_up = _z_direct(ctx, z_up, f"{what}: retract")
    r = ctx.call("move_xy", timeout_s=300, x_um=round(tx, 3), y_um=round(ty, 3), **motor_args(ctx.prof))
    if not r["ok"]:
        raise Stop(f"{what}: XY move failed (Z left retracted at {z_up:g} µm): {r.get('error')}", "study")
    z_down = _z_direct(ctx, zf - s * float(unevenness_um), f"{what}: back down")
    return {"mode": "retract", "retract_um": R, "retracted_to_um": z_up, "old_focus_um": zf,
            "unevenness_um": float(unevenness_um), "descended_to_um": z_down,
            "xy_um": [_f(r.get("x_um")), _f(r.get("y_um"))]}


def _af_bound(ctx: Ctx) -> dict:
    """For LabSpec's autofocus inside a study: the bridge stops it as soon as Z passes the focus plus the objective's
    toward limit (its own search range is neither settable nor readable from a script). Nothing without the Z
    direction (stage.z_into_sample) or a focus."""
    zl = plan.z_limits(ctx.prof, ctx.objective)
    if zl["toward_sign"] is None or ctx.z_focus is None:
        return {}
    mo = plan.af_motion(ctx.prof)
    trip = zl["toward_um"] - mo["margin_um"]
    if trip <= 0:
        raise Stop(f"LabSpec's autofocus is refused with {ctx.objective or 'this objective'}: its overshoot margin "
                   f"({mo['margin_um']:g} µm: Z speed {mo['z_speed_um_s']:g} µm/s {mo['z_speed_um_s_from']}, stop "
                   f"{mo['stop_s']:g} s {mo['stop_s_from']}) leaves nothing of the {zl['toward_um']:g} µm allowed "
                   "toward the sample; measure it with a longer working distance objective first (TESTING T6.8)",
                   "study")
    return {"z_bound": round(ctx.z_focus + zl["toward_sign"] * trip, 3), "z_sign": zl["toward_sign"]}


def _video_focus_after_travel(ctx: Ctx, travel: dict, what: str, warnings: list) -> float:
    """The last step of a retract-travel-return without the laser: LabSpec's video autofocus (white light on, the
    live video running). LabSpec's own autofocus range applies while it runs; ls6t checks where it ended: within the
    sample's unevenness plus the objective's toward limit of the old focus it becomes the focus here, otherwise Z goes
    back to the descended height and the study says so."""
    zl = plan.z_limits(ctx.prof, ctx.objective)
    zf, z_down, un = travel["old_focus_um"], travel["descended_to_um"], travel["unevenness_um"]
    s = zl["toward_sign"]
    # the farthest LabSpec's search may go toward the sample: the new surface may be up to `un` higher (closer to
    # the objective), so at old focus + d the gap to it is WD - un - d; keep a tenth of WD; and no deeper than the
    # unevenness plus the ordinary toward limit
    wd = zl["working_distance_um"] or 0.0
    bound = min(un + zl["toward_um"], wd - un - 0.1 * wd) if wd else un + zl["toward_um"]
    mo = plan.af_motion(ctx.prof)
    trip = bound - mo["margin_um"]          # the stage stops at the bound, not starts to stop there
    if trip <= 0:
        warnings.append(f"no video autofocus with {ctx.objective or 'this objective'}: the safe approach "
                        f"({bound:g} µm, unevenness {un:g} µm, working distance ~{wd:g} µm) minus the overshoot "
                        f"margin ({mo['margin_um']:g} µm: Z speed {mo['z_speed_um_s']:g} µm/s "
                        f"{mo['z_speed_um_s_from']}, stop {mo['stop_s']:g} s {mo['stop_s_from']}) leaves nothing; "
                        "measure the margin with a longer working distance objective first (TESTING T6.8)")
        travel["video_autofocus"] = {"ok": False, "skipped": "no safe room", "bound_um": round(bound, 1),
                                     "margin_um": mo["margin_um"]}
        return z_down
    z_bound = round(zf + s * trip, 3)
    r = ctx.call("autofocus", timeout_s=240, how="video", z_bound=z_bound, z_sign=s, **motor_args(ctx.prof))
    plan.record_af_motion(r)
    znew = _f(r.get("z_um")) if r.get("ok") else None
    travel["video_autofocus"] = {"ok": bool(r.get("ok")), "z_um": znew, "error": r.get("error") or None,
                                 "z_bound_um": z_bound, "z_seen_um": [_f(r.get("z_seen_min")), _f(r.get("z_seen_max"))],
                                 "stopped_at_bound": r.get("z_bound_hit") not in (None, ""),
                                 "safe_limit_um": round(zf + s * bound, 3), "margin": mo,
                                 "measured": {k: _f(r.get(k)) for k in ("z_speed_um_s", "poll_s", "stop_s",
                                                                        "overshoot_um")}}
    if znew is None or abs(znew - zf) > bound:
        z = _z_direct(ctx, z_down, f"{what}: back down after the video autofocus")
        why = (f"was stopped at the safe bound ({z_bound:g} µm): its search range reaches farther toward the sample "
               "than allowed: reduce LabSpec's autofocus range" if travel["video_autofocus"]["stopped_at_bound"] else
               f"failed ({r.get('error')})" if znew is None else
               f"ended {znew - zf:+.1f} µm from the old focus, beyond {bound:g} µm")
        warnings.append(f"LabSpec's video autofocus {why}: Z is back {un:g} µm short of the old focus; focus by hand or "
                        "with a focus study")
        travel["video_autofocus"]["accepted"] = False
        return z
    ctx.z_focus = znew
    _focus_here(ctx)
    travel["video_autofocus"]["accepted"] = True
    return znew


def _retract_before_xy(ctx: Ctx, ax, ay, zcur, zref, what: str):
    """Before an XY move beyond where the focus stays valid (short working distances), Z goes back to the focus if
    it is offset toward the sample: the stage never travels sideways with the sample closer than at focus."""
    if zcur is None or zref is None or None in ctx.focus_xy:
        return zcur
    zl = plan.z_limits(ctx.prof, ctx.objective)
    if zl["focus_valid_xy_um"] is None or not plan.toward(zl, zcur - zref):
        return zcur
    if math.hypot(ax - ctx.focus_xy[0], ay - ctx.focus_xy[1]) <= zl["focus_valid_xy_um"]:
        return zcur
    return move_z(ctx, zref, f"{what}: back to the focus before moving away", focusing=True)


def move_z(ctx: Ctx, target: float, what: str, focusing: bool = False, away_ok: bool = False) -> float:
    _check_abs(ctx.prof, "z", target, what)
    _z_guard(ctx, target, what, focusing, "travel" if away_ok else None)
    r = ctx.call("move", timeout_s=120, motor=ctx.prof["motors"]["z"], mode="value", value=round(target, 3))
    if not r["ok"]:
        raise Stop(f"{what}: Z move failed: {r.get('error')}", "study")
    return _f(r.get("value")) if _f(r.get("value")) is not None else target


def _objective_source(ctx: Ctx, obj) -> str | None:
    """Where the objective of a result comes from: confirmed by the operator in this request, or LabSpec's record
    (a manual turret: ls6t cannot see the lens)."""
    if obj is None:
        return None
    if not ctx.prof.get("manual_objective", True):
        return "LabSpec's motorised turret"
    return ("confirmed by the operator in this request" if ctx.objective_by_operator == obj else
            "LabSpec's setting, not checked (manual turret)")


def _not_returned(by, stage: dict) -> dict:
    """The return home after a stop: none is made; said according to where the stage is."""
    fh = (stage or {}).get("from_home_um")
    if fh is not None and math.hypot(*fh) <= 0.5:
        return {"done": False, "needed": False, "why": f"stopped ({by}): nothing moved away; the stage is at the home "
                                                      "spot"}
    if fh is None:
        return {"done": False, "why": f"stopped ({by}): the stage stays where it is (its distance from the home spot "
                                      "could not be read); no return to the home spot was made"}
    return {"done": False, "needed": True, "why": f"stopped ({by}): the stage stays {math.hypot(*fh):.1f} µm from "
                                                  "the home spot; the return to it did not happen"}


def _stage_now(ctx: Ctx) -> dict:
    """Where the stage is now (X, Y, Z in µm, read back), and how far from the home spot; {} if it cannot be read."""
    try:
        st = ctx.call("status", timeout_s=15, **motor_args(ctx.prof))
    except Stop:
        return {}
    x, y, z = _f(st.get("x_um")), _f(st.get("y_um")), _f(st.get("z_um"))
    out = {"x_um": x, "y_um": y, "z_um": z}
    hx, hy = ctx.home_xy
    if None in (hx, hy) and x is not None and y is not None:
        # stopped before the request took its home spot (e.g. the bridge was halted): nothing has moved yet
        hx, hy = x, y
        out["home"] = "here: the request stopped before its first move"
    if None not in (x, y, hx, hy):
        out["from_home_um"] = [round(x - hx, 2), round(y - hy, 2)]
    return out


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
        out["done"] = True
    except Stop as exc:
        out["error"] = exc.why
        out["done"] = False
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
    if sc.get("from_below"):
        return _focus_from_below(ctx, study, laser, sc, sd, warnings)
    offs = plan.scan_offsets(sc)
    line, win = float(sc["line_cm1"]), float(sc["window_cm1"])
    nm = round(shift_to_nm(line, laser), 4)
    zs, vals = [], []
    for i, dz in enumerate(offs):
        z = move_z(ctx, z0 + dz, f"focus scan step {i}", focusing=True)
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
        res["z_now"] = move_z(ctx, z0, "focus scan: back to the start", focusing=True)
    else:
        # a Gaussian through the points above 30 % of the best (a parabola in log, weighted by the signal); a
        # three-point parabola around the best when too few points qualify
        sel = v > 0.3 * v.max()
        zb = None
        fwhm = None
        if sel.sum() >= 4:
            c2, c1, _ = np.polyfit(zs_a[sel], np.log(v[sel]), 2, w=np.sqrt(v[sel]))
            if c2 < 0:
                zb = float(-c1 / (2 * c2))
                fwhm = float(2.0 * math.sqrt(math.log(2.0) / -c2))   # of the Gaussian exp(c2 z^2 + ...)
        if zb is None or not zs_a.min() <= zb <= zs_a.max():
            y1, y2, y3 = v[i - 1], v[i], v[i + 1]
            den = (y1 - 2 * y2 + y3)
            step = zs_a[i + 1] - zs_a[i]
            frac = 0.5 * (y1 - y3) / den if den < 0 else 0.0
            zb = float(zs_a[i] + max(-0.5, min(0.5, frac)) * step)
        res.update(found=True, z_um=round(move_z(ctx, zb, "focus scan: to the best", focusing=True), 3),
                   best_signal=round(float(v.max()), 3), shift_um=round(zb - z0, 3), contrast=round(contrast, 3))
        if fwhm is not None and fwhm < 0.9 * (zs_a.max() - zs_a.min()):
            res["axial_fwhm_um"] = round(fwhm, 3)       # the depth resolution on this sample (stage µm)
        res["z_now"] = res["z_um"]
    try:
        from . import render
        res["image"] = render.focus_png(sd / "focus_scan.png", res)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"focus scan plot not drawn: {type(exc).__name__}: {exc}")
    write_json(sd / "focus_scan.json", clean(res))
    return res


def _scan_signal(ctx: Ctx, laser: float, sc: dict, out: Path) -> float:
    """One short acquisition on the focus line: its height above the window's edges, per second."""
    from .profile import shift_to_nm
    line, win = float(sc["line_cm1"]), float(sc["window_cm1"])
    nm = round(shift_to_nm(line, laser), 4)
    r = ctx.call("acquire", timeout_s=60 + 3 * float(sc["exposure_s"]), mode=110, exposure_s=float(sc["exposure_s"]),
                 accumulations=1, from_nm=nm, to_nm=nm, out=str(out), sample="focus scan")
    if not r["ok"]:
        raise Stop(f"focus search: acquisition failed: {r.get('error')}", "study")
    sp = spectra.read(out)
    x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
    x, y = spectra.sort_xy(x, sp["y"])
    return spectra.line_signal(x, y, line, win) / float(sc["exposure_s"])


def _focus_from_below(ctx: Ctx, study: dict, laser: float, sc: dict, sd: Path, warnings: list) -> dict:
    """The focus searched approaching the sample from the away side (after a retract-travel-return): coarse steps
    from where Z is toward the sample, never past the old focus plus the objective's toward limit, stopping once the
    signal has clearly passed its peak; then an ordinary fine scan around the best coarse step."""
    zl = plan.z_limits(ctx.prof, ctx.objective)
    s = zl["toward_sign"]
    if s is None:
        raise Stop("a focus search from below needs stage.z_into_sample (TESTING T3.17)", "study")
    st = ctx.call("status", timeout_s=15, **motor_args(ctx.prof))
    z_start = _f(st.get("z_um"))
    if z_start is None:
        raise Stop("the Z position could not be read: no focus search", "study")
    ref = ctx.z_focus if ctx.z_focus is not None else z_start
    z_end = ref + s * zl["toward_um"]
    span = (z_end - z_start) * s
    if span <= float(sc["step_um"]):
        raise Stop("focus search from below: Z is not on the away side of the old focus (nothing to approach)", "study")
    coarse = max(float(sc["step_um"]), min(span / 30.0, plan.coarse_focus_step_um(ctx.prof, ctx.objective)))
    zs, vals = [], []
    k = 0
    while True:
        z_t = z_start + s * k * coarse
        if (z_t - z_end) * s > 1e-9:
            break
        z = move_z(ctx, z_t, f"focus search from below, step {k}", focusing=True, away_ok=True)
        zs.append(z)
        vals.append(_scan_signal(ctx, laser, sc, sd / f"below{k:03d}.txt"))
        k += 1
        i = int(np.argmax(vals))
        base = float(np.median(vals[:3])) if len(vals) >= 3 else None
        if base is not None and vals[i] > 3 * max(base, 1e-12) and len(vals) - 1 - i >= 2 and vals[-1] < 0.5 * vals[i]:
            break                                    # a clear peak, well passed: no need to go nearer
    i = int(np.argmax(vals)) if vals else 0
    base = float(np.median(vals[:3])) if len(vals) >= 3 else (vals[0] if vals else 0.0)
    res = {"from_below": True, "z_start_um": z_start, "z_limit_um": z_end, "coarse_step_um": round(coarse, 3),
           "coarse_z_um": zs, "coarse_signal": [round(v, 3) for v in vals]}
    if not vals or vals[i] <= 3 * max(base, 1e-12) or not 0 < i < len(vals) - 1:
        res.update(found=False, why="no clear peak between the start and the limit toward the sample",
                   z_now=_z_direct(ctx, z_start, "focus search: back to the start"))
        write_json(sd / "focus_scan.json", clean(res))
        return res
    half = min(2 * coarse, max(float(sc["step_um"]), (z_end - zs[i]) * s))
    fine = dict(study, focus=dict(study.get("focus") or {}, scan=dict(sc, from_below=False, range_um=round(2 * half, 3))))
    ctx_z = ctx.z_focus
    ctx.z_focus = zs[i]                          # the fine scan's Z limits count from the coarse best
    try:
        fs = focus_scan(ctx, fine, laser, zs[i], sd / "fine", warnings)
    finally:
        ctx.z_focus = ctx_z                      # the caller sets the new focus from the result
    fs.update({k_: v for k_, v in res.items() if k_ not in ("found",)})
    write_json(sd / "focus_scan.json", clean(fs))
    return fs


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
        _focus_here(ctx)
        if fs.get("axial_fwhm_um"):
            from . import perf
            k = perf.key(su["after"].get("objective") or "?", su["laser"], _f(su["after"].get("hole_um")))
            rec = perf.record("axial", k, {"fwhm_um": fs["axial_fwhm_um"], "line_cm1": fs.get("line_cm1"),
                                           "sample": (study.get("sample") or {}).get("name")}, str(ctx.folder))
            fs["axial_resolution"] = {"key": k, "fwhm_um": fs["axial_fwhm_um"],
                                      "baseline_um": rec["baseline"].get("fwhm_um"), "measurements": rec["n"]}
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
        confirmed_now = False           # MOTOR_NO_MESSAGE only for the move the operator has just confirmed here
        if prof.get("manual_objective", True) and str(obj) != str(before.get("objective")) \
                and config.confirm_policy() != "never":
            from .api import objective_question
            ans = ctx.call("confirm", timeout_s=3600, text=objective_question(prof, before.get("objective"), obj))
            if ans.get("answer") != "yes":
                raise Stop("the operator declined to change the objective", "operator")
            ctx.objective_by_operator = str(obj)
            confirmed_now = True
        r = ctx.call("move", timeout_s=600, motor=prof["motors"]["objective"], mode="label", value=str(obj),
                     **move_extra(prof, prof["motors"]["objective"], confirmed_now))
        if not r["ok"] or not P.same_position(r.get("label"), obj):
            raise Stop(f"objective {obj}: {r.get('error') or 'got ' + str(r.get('label'))}", "study")
    hx, hy = ctx.home_xy
    if None in (hx, hy):
        raise Stop("the stage position could not be read: moves are refused", "study")
    beam = None
    cur_obj = str(obj) if obj is not None else before.get("objective")
    if m.get("to_um") is not None and prof.get("manual_objective", True) and ctx.objective_by_operator != cur_obj:
        # a point picked on a snapshot: its µm scale and the beam offset come from the objective LabSpec records; with
        # another lens on the (manual) turret the stage would go to the wrong place: the operator confirms the lens
        if config.confirm_policy() != "never":
            ans = ctx.call("confirm", timeout_s=3600,
                           text=f"ls6t will move the stage to a point picked on a video snapshot, scaled for objective "
                                f"{cur_obj} (LabSpec's setting). Is {cur_obj} the lens on the turret now? (Cancel: no "
                                f"move)")
            if ans.get("answer") != "yes":
                raise Stop(f"the operator did not confirm objective {cur_obj}: no move", "operator")
            ctx.objective_by_operator = cur_obj
        else:
            warnings.append(f"objective {cur_obj}: LabSpec's setting, not checked (manual turret, confirmations "
                            "off): if another lens is in place, the stage goes to the wrong place")
    if m.get("to_um") is not None:
        # put the beam (not LabSpec's marker) on a point picked in a snapshot: stage = point - the beam's offset
        objective = obj if obj is not None else before.get("objective")
        from . import video as V
        if V.field_um(prof, objective) and V.axes_directions(prof, objective, before.get("camera"))[2] == "assumed":
            # 2026-11-23: with the assumed directions a particle at y = -155 µm showed at +153 µm
            raise Stop(f"{name}: the snapshot's X / Y directions for objective {objective} are assumed, not measured "
                       "(they can be mirrored: on the XploRA's top camera image up is stage -Y): run `ls6t "
                       "instrument video-scale` with this objective (no laser), take the snapshot again, then pick "
                       "the point", "study")
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
    bx, by = _f(before.get("x_um")), _f(before.get("y_um"))
    dist = math.hypot(tx - (bx if bx is not None else tx), ty - (by if by is not None else ty))
    travel = None
    dz = float(m.get("dz_um", 0) or 0)
    if plan.travel_mode(prof, study, dist, cur_obj) == "retract":
        travel = travel_xy(ctx, tx, ty, float((study.get("sample") or {}).get("unevenness_um") or 0), name)
        xy, z = travel["xy_um"], travel["descended_to_um"]
        if m.get("focus") == "video":
            z = _video_focus_after_travel(ctx, travel, name, warnings)
        else:
            warnings.append(f"retract-travel-return: Z is {travel['unevenness_um']:g} µm short of the old focus, away "
                            "from the sample: focus here (move.focus 'video', or a 'focus' study with "
                            "focus.scan.from_below) before measuring")
    else:
        r = ctx.call("move_xy", timeout_s=300, x_um=round(tx, 3), y_um=round(ty, 3), **motor_args(prof))
        if not r["ok"]:
            raise Stop(f"{name}: XY move failed: {r.get('error')}", "study")
        xy = [_f(r.get("x_um")), _f(r.get("y_um"))]
        z = _f(before.get("z_um"))
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
                                       "home_um": list(ctx.home_xy), "objective": cur_obj,
                                       "objective_source": _objective_source(ctx, cur_obj), "beam": beam,
                                       "travel": travel or "direct"},
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
    params_before = _labspec_params(ctx)
    auto = (study.get("calibration") or {}).get("adjust") == "auto"
    ac = prof.get("calibration", {}).get("autocal") or {}
    autocal_runs = []
    for k in range(1, n_max + 1):
        res = run_study(name, study, ctx, sd / f"attempt{k}")
        cal = res.get("calibration") or {}
        attempts.append({"attempt": k, "pass": cal.get("pass"), "offset_cm1": cal.get("offset_cm1"),
                         "worst_error_cm1": cal.get("worst_error_cm1"), "correction": cal.get("correction"),
                         "labspec_parameters": _labspec_params(ctx)})
        if cal.get("pass") or k == n_max or not cal.get("lines_used"):
            break
        if auto and not autocal_runs and ac.get("enabled") and (ac.get("script") or ac.get("method") == "call"):
            # LabSpec's own AutoCalibration, once; then measure again
            from .api import autocal_call, autocal_question
            inst0 = res.get("instrument") or {}
            txt = (f"ls6t: the calibration of {combo(inst0.get('laser_nm'), inst0.get('grating'))} is off "
                   f"({cal.get('offset_cm1')} cm-1); LabSpec calibrates the grating in use only. "
                   + autocal_question(ac))
            ans = ctx.call("confirm", timeout_s=3600, text=txt)
            if ans.get("answer") == "yes":
                t0 = time.time()
                r = autocal_call(ctx.call, ac)
                autocal_runs.append({"ok": bool(r["ok"]), "error": r.get("error"), "seconds": round(time.time() - t0, 1)})
                if r["ok"]:
                    continue                       # measure again
            else:
                autocal_runs.append({"ok": False, "error": "the operator declined"})
        if not cal.get("trusted", True):
            # no correction from a line measured badly: the operator would move the zero by a wrong amount
            res.setdefault("warnings", []).append("no correction asked: " + "; ".join(cal.get("not_trusted_because", [])))
            break
        laser = (res.get("instrument") or {}).get("laser_nm") or 532.0
        ref, meas = cal["reference_cm1"], cal["measured_cm1"]
        err = meas - ref
        nm = shift_to_nm(ref, laser)
        dnm = -err * nm * nm / 1e7
        inst = res.get("instrument") or {}
        step = ((prof.get("calibration") or {}).get("offset_step_cm1") or {}).get(
            f"{_f(inst.get('grating')) or 0:g}@{float(laser):g}")
        units = (f" On this instrument one step of LabSpec's Offset shift moves the line by about {float(step):+.3f} "
                 f"cm-1 here: about {-err / float(step):+.0f} steps (arrows)." if step else "")
        txt = (f"ls6t: the {'Si' if cal.get('reference') == 'si' else cal.get('reference')} line at {ref} cm-1 is measured at {meas:.2f} cm-1 "
               f"({err:+.2f} cm-1) with {combo(laser, inst.get('grating'))}.\n\nCorrect the spectral zero in "
               f"LabSpec (Maintenance > Instrument calibration) for {combo(laser, inst.get('grating'))} only: the "
               f"line must move by {-err:+.2f} cm-1, about {dnm:+.4f} nm.{units} The other gratings keep their own "
               "zero: check each one you use."
               + (f" With several lines, the coefficient (dispersion) too: scale {cal['correction']['b']:.6f}."
                  if cal.get("correction") and abs(cal["correction"]["b"] - 1) > 1e-5 else "")
               + "\n\nPress OK to measure again, or Cancel to stop.")
        ans = ctx.call("notice", timeout_s=7200, text=txt)
        if ans.get("answer") != "ok":
            break
    result = dict(res or {})
    cal = dict(result.get("calibration") or {})
    cal["attempts"] = attempts
    if auto:
        cal["autocalibration"] = autocal_runs or ("not enabled in the profile (calibration.autocal): guided instead"
                                                  if not ac.get("enabled") else "not needed")
    after = attempts[-1].get("labspec_parameters") if attempts else None
    if params_before and after:
        changed = {k: [params_before.get(k), after.get(k)] for k in after if after.get(k) != params_before.get(k)}
        cal["labspec_parameters"] = {"before": params_before, "after": after, "changed": changed}
    if cal.get("pass") and len(attempts) > 1:
        inst = result.get("instrument") or {}
        n = xcal.clear(inst.get("laser_nm"), inst.get("grating"))
        if n:
            result.setdefault("warnings", []).append(f"LabSpec is calibrated again for {combo(inst.get('laser_nm'), inst.get('grating'))}: ls6t's own correction for it was dropped")
    result["calibration"] = cal
    return _finish(sd, name, result)


def _labspec_params(ctx: Ctx) -> dict | None:
    """LabSpec's calibration parameters for the grating in use (read only), or None when the bridge cannot read
    them."""
    try:
        r = ctx.call("calib_params", timeout_s=60, m_grating=ctx.prof["motors"]["grating"])
    except Stop:
        raise
    except Exception:  # noqa: BLE001
        return None
    if not r.get("ok"):
        return None
    i = r.get("grating_index")
    try:
        i = int(float(i))
    except (TypeError, ValueError):
        return None
    out = {}
    for k, v in r.items():
        if k.startswith(f"g{i}.") and not k.endswith(".error"):
            try:
                out[k[len(f"g{i}."):]] = float(v)
            except (TypeError, ValueError):
                out[k[len(f"g{i}."):]] = v
    return out or None


def _record_resolution(result: dict, after: dict, prof: dict, warnings: list, request: str):
    """The reference line's width after a calibration: the spectral resolution for this laser, grating and slit,
    compared with its first measurement and with the profile's performance.resolution_cm1, if set."""
    from . import perf
    cal = result["calibration"]
    laser = (result.get("instrument") or {}).get("laser_nm")
    grating = _f(after.get("grating"))
    slit = _f(after.get("slit_um"))
    k = perf.key(laser, grating, slit)
    fw = float(cal["fwhm_cm1"])
    rec = perf.record("resolution", k, {"fwhm_cm1": round(fw, 3), "line_cm1": cal.get("reference_cm1"),
                                        "reference": cal.get("reference")}, request)
    base = float(rec["baseline"]["fwhm_cm1"])
    pf = prof.get("performance", {})
    out = {"key": k, "fwhm_cm1": round(fw, 3), "baseline_cm1": base, "measurements": rec["n"]}
    if fw > float(pf.get("resolution_tol", 1.25)) * base:
        out["worse_than_baseline"] = True
        warnings.append(f"the {cal.get('reference')} line is {fw:.2f} cm-1 wide, {fw / base:.2f} x its first "
                        f"measurement ({base:.2f}) for {k}: alignment, focus or slit?")
    spec = (pf.get("resolution_cm1") or {}).get(f"{grating:g}" if grating else "")
    if spec is not None:
        out["specification_cm1"] = float(spec)
        if fw > float(spec):
            warnings.append(f"the line width {fw:.2f} cm-1 is above the profile's resolution for {grating:g} gr/mm "
                            f"({float(spec):g} cm-1)")
    cal["resolution"] = out


def _move_seconds(r: dict) -> dict:
    """A move's timing as the bridge reported it (seconds, wait, all motors idle, settle)."""
    return {k: _f(r.get(k)) for k in ("seconds", "wait_s", "all_wait_s", "settle_s") if _f(r.get(k)) is not None}


def run_filters(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'filters': the real transmission of each filter, from one Raman line (Si 520.7 by default) measured
    through each, strongest transmission first (the reference) with the exposure scaled so the counts stay
    alike; transmission = reference % x (signal per second / the reference's). Only filters within the caps."""
    from . import perf
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    fp = plan.filters_params(study, prof)
    _progress(sd, state="running", stage="setup", started=t_start, estimate_min=plan.estimate(study, prof)["minutes"])
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    laser = su["laser"]
    all_steps = plan.filters_steps(study, prof, laser)
    steps = [s for s in all_steps if s.get("exposure_s")]
    for s_ in all_steps:
        if not s_.get("exposure_s"):
            warnings.append(f"filter {s_['label']} not measured: {s_.get('skip')}")
    before_filter = su["before"].get("filter")
    nm = round(shift_to_nm(fp["line_cm1"], laser), 4)
    rows = []
    repeat = None

    def measure(i, s, tag):
        r = ctx.call("move", timeout_s=120, motor=prof["motors"]["filter"], mode="label", value=s["label"])
        if not r["ok"] or str(r.get("label")) != s["label"]:
            raise Stop(f"filter {s['label']}: LabSpec did not move there ({r.get('error') or r.get('label')})",
                       "study")
        mv = _move_seconds(r)
        moved_to = r.get("label")
        out = sd / "spectra" / f"filter_{tag}.txt"
        r = ctx.call("acquire", timeout_s=120 + 3 * s["exposure_s"], mode=110, exposure_s=s["exposure_s"],
                     accumulations=1, from_nm=nm, to_nm=nm, out=str(out), sample=f"filter {s['label']}")
        if not r["ok"]:
            raise Stop(f"filter {s['label']}: acquisition failed: {r.get('error')}", "study")
        used = _f(r.get("exposure_s")) or s["exposure_s"]         # LabSpec's own record (Ctx checks it)
        sp = spectra.read(out)
        x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        x, y = spectra.sort_xy(x, sp["y"])
        sat = spectra.saturation(y, prof["detector"]["full_scale"], prof["checks"]["saturation_frac"])
        sig = spectra.line_signal(x, y, fp["line_cm1"], fp["window_cm1"]) / used
        noise = spectra.noise_sigma(y) / used
        return {**s, "exposure_used_s": used, "signal_cps": round(float(sig), 3),
                "snr": round(float(sig / max(noise, 1e-9)), 1), "max_counts": sat["max_counts"],
                "saturated": sat["saturated"], "filter_read_back": moved_to, "move": mv,
                "file": str(out)}

    try:
        for i, s in enumerate(steps):
            _progress(sd, stage=f"filter {s['label']}", point=i, points=len(steps))
            rows.append(measure(i, s, f"{i:02d}"))
        if len(rows) >= 2:
            # the reference again at the end: a change means drift (focus, laser power), not the filters
            # (2026-11-23: the second step read about 3 x low; this tells drift from the filter)
            repeat = measure(len(steps), steps[0], "ref_again")
    finally:
        if before_filter:
            try:
                ctx.call("move", timeout_s=120, motor=prof["motors"]["filter"], mode="label", value=before_filter)
            except Stop:
                pass
    tol = float(prof.get("performance", {}).get("filters_tol", 0.3))
    ref = rows[0] if rows else None
    if ref and ref["saturated"]:
        unreliable.append(f"the reference filter {ref['label']} is saturated: a shorter filters.exposure_s")
    for r_ in rows[1:]:
        if r_["saturated"]:
            unreliable.append(f"filter {r_['label']} is saturated ({r_['max_counts']:.0f} counts): its exposure is "
                              "scaled up from the reference's; a shorter filters.exposure_s")
    drift = None
    if ref and repeat and ref["signal_cps"] > 0:
        drift = round(repeat["signal_cps"] / ref["signal_cps"], 3)
        if abs(drift - 1.0) > 0.1:
            unreliable.append(f"the reference filter {ref['label']} read {drift:.2f} x its first value at the end: "
                              "the signal drifted during the study (focus, laser power, sample), so the "
                              "transmissions are uncertain; refocus and measure again")
    if ref and ref["signal_cps"] <= 0:
        unreliable.append("no line through the reference filter: check the sample and its focus")
    flagged = []
    for r_ in rows:
        if ref and ref["signal_cps"] > 0:
            r_["measured_pct"] = round(ref["nominal_pct"] * r_["signal_cps"] / ref["signal_cps"], 5)
            r_["ratio"] = round(r_["measured_pct"] / r_["nominal_pct"], 3)
            if r_ is not ref and abs(r_["ratio"] - 1.0) > tol:
                flagged.append(f"{r_['label']}: measured {r_['measured_pct']:.4g} % ({r_['ratio']:.2f} x nominal)")
            if r_ is not ref and r_["snr"] < prof["checks"].get("detect_snr", 5.0):
                warnings.append(f"filter {r_['label']}: weak line (SNR {r_['snr']}): its value is uncertain")
    if flagged:
        warnings.append("filters off their nominal transmission by more than performance.filters_tol: "
                        + "; ".join(flagged) + " (relative to " + (ref["label"] if ref else "?") + ")")
    measured = {r_["label"]: r_.get("measured_pct") for r_ in rows if r_.get("measured_pct") is not None}
    rec = None
    if measured and not unreliable:
        rec = perf.record("filters", perf.key(laser), {"reference": ref["label"], "measured_pct": measured,
                                                        "line_cm1": fp["line_cm1"]}, str(ctx.folder))
    # the proposal from several runs (2026-11-23: 1 % read 1.176, 0.971 and 1.260 % in three good runs, about the
    # reference's own drift): the mean of the last runs with the same reference, offered once there are enough
    proposal, avg = None, None
    if measured and not unreliable:
        hist = [e.get("measured_pct") or {} for e in ((perf.get("filters", perf.key(laser)) or {}).get("history") or [])
                if e.get("reference") == ref["label"]][-5:]
        avg = {}
        for lab in measured:
            vals = [float(h[lab]) for h in hist if h.get(lab) is not None]
            if vals:
                m_ = sum(vals) / len(vals)
                sd_ = (sum((v - m_) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else None
                avg[lab] = {"mean_pct": round(m_, 4), "runs": len(vals),
                            **({"std_pct": round(sd_, 4)} if sd_ is not None else {})}
        need = int((prof.get("performance") or {}).get("filters_min_runs", 3))
        runs = min((a["runs"] for a in avg.values()), default=0)
        if runs >= need:
            new = dict(prof.get("filters", {}))
            new.update({k: a["mean_pct"] for k, a in avg.items()})
            proposal = ("ls6t profile set filters '" + json.dumps(new) + "'")
    if repeat:
        checks["reference_repeat"] = {"signal_cps": repeat["signal_cps"], "ratio_to_first": drift,
                                      "file": repeat["file"]}
    result = {"ok": bool(rows), "complete": len(rows) == len(steps), "reliable": bool(rows) and not unreliable,
              "unreliable_because": unreliable, "warnings": warnings, "checks": checks, "kind": "filters",
              "sample": study.get("sample", {}),
              "filters": {"reference": ref["label"] if ref else None, "line_cm1": fp["line_cm1"], "steps": rows,
                          "flagged": flagged, "measurements": rec["n"] if rec else 0,
                          "profile_command": proposal, "average": avg,
                          "skipped": [{"label": s_["label"], "why": s_.get("skip")} for s_ in
                                      plan.filters_steps(study, prof, laser) if not s_.get("exposure_s")],
                          **({} if proposal or not avg else
                             {"profile_command_note": f"offered from {int((prof.get('performance') or {}).get('filters_min_runs', 3))} "
                                                     "runs with the same reference (the mean): repeat the study"}),
                          "note": "relative to the reference filter (the strongest allowed); the profile's caps "
                                  "use the filters' values: a person decides whether to set the measured ones "
                                  "(profile_command)"},
              "instrument": {"laser_nm": laser, "objective": su["after"].get("objective"),
                             "grating": _f(su["after"].get("grating"))},
              "seconds": {"total": round(time.time() - t_start, 2)}}
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


def run_cutoff(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'cutoff': where the edge filter lets light through, from a spectrum across the laser line: with the
    white light (laser off: the shutter, or the operator blocks it) or the sample's own background with the laser.
    Transmission = (signal - dark) / (plateau - dark), the plateau taken over cutoff.plateau_cm1; reports the shifts
    at 10, 50 and 90 % (usable_from_cm1), remembered per laser and grating (later ranges below it are warned)."""
    from . import perf
    prof = ctx.prof
    sd = ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    cp = plan.cutoff_params(study, prof)
    white = cp["light"] == "white"
    _progress(sd, state="running", stage="setup", started=t_start)
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1, confirm=not white)
    laser, rng = su["laser"], su["rng"]
    if white:
        txt = ("ls6t: the edge filter's cut-off, with the white light.\n\nTurn the white light on"
               + (".\n\n" + BLOCK_BY_HAND if cp["laser_off"] == "operator" else " (ls6t keeps the laser shutter closed)")
               + "\n\nPress OK when ready, or Cancel.")
        if ctx.call("notice", timeout_s=3600, text=txt).get("answer") != "ok":
            raise Stop("the operator cancelled", "study")
    acq = plan.acquire_step(study, prof, rng)
    acq.pop("verb", None)
    out = sd / "spectra" / "cutoff.txt"
    params = dict(acq, out=str(out), sample="edge filter cut-off")
    if white and cp["laser_off"] == "shutter":
        _shutter_gate(prof, laser)
        params.update(laser_off=1, m_laser_shutter=prof["motors"]["laser_shutter"], **shutter_args(prof))
    r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
    if white and cp["laser_off"] == "operator":
        ctx.call("notice", timeout_s=3600, text="ls6t: done. Unblock the laser when you want to measure again. OK.")
    if not r["ok"]:
        raise Stop(f"cut-off acquisition failed: {r.get('error')}", "study")
    grating_guard(prof, study, su.get("after"), laser, rng, out, checks, warnings, r)
    sp = spectra.read(out)
    x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
    x, y = spectra.sort_xy(x, sp["y"])
    k = max(3, int(len(y) // 200) | 1)
    ys = np.convolve(np.pad(y, k // 2, mode="edge"), np.ones(k) / k, mode="valid")[:len(y)]
    lo, hi = cp["plateau_cm1"]
    sel = (x >= lo) & (x <= hi)
    if not white:
        sel &= np.abs(x - 520.7) > 30
    res: dict = {"light": cp["light"], "range_cm1": [float(x.min()), float(x.max())]}
    if sel.sum() < 5:
        unreliable.append(f"the plateau {lo:g}-{hi:g} cm-1 is not inside the range: widen cutoff.to_cm1")
    else:
        plateau = float(np.median(ys[sel]))
        dark = float(np.percentile(ys[x < lo], 5)) if (x < lo).any() else float(ys.min())
        res.update(plateau=round(plateau, 2), dark=round(dark, 2))
        if plateau - dark < 10 * max(spectra.noise_sigma(y), 1.0):
            unreliable.append("no clear step: too little light above the cut-off (more white light, a longer "
                              "exposure, or light 'white')")
        else:
            tr = (ys - dark) / (plateau - dark)
            idx = np.where(x < lo)[0]

            def crossing(level):
                # from the plateau down towards the laser: the first point under the level
                for j in sorted(idx, key=lambda j: -x[j]):
                    if tr[j] < level:
                        j2 = j + 1 if j + 1 < len(x) else j
                        if tr[j2] != tr[j]:
                            return float(x[j] + (level - tr[j]) * (x[j2] - x[j]) / (tr[j2] - tr[j]))
                        return float(x[j])
                return None
            for lvl, key_ in ((0.1, "edge_10_cm1"), (0.5, "edge_50_cm1"), (0.9, "usable_from_cm1")):
                v = crossing(lvl)
                res[key_] = round(v, 1) if v is not None else None
            if res.get("usable_from_cm1") is None:
                unreliable.append("the transmission never drops below 90 % in the range: start lower (cutoff.from_cm1)")
    if res.get("usable_from_cm1") is not None and not unreliable:
        grating = _f(su["after"].get("grating"))
        rec = perf.record("cutoff", perf.key(laser, grating), {k_: res.get(k_) for k_ in
                                                                ("edge_10_cm1", "edge_50_cm1", "usable_from_cm1")}
                          | {"light": cp["light"]}, str(ctx.folder))
        res["measurements"] = rec["n"]
        res["baseline_usable_from_cm1"] = rec["baseline"].get("usable_from_cm1")
    result = {"ok": True, "complete": True, "reliable": not unreliable, "unreliable_because": unreliable,
              "warnings": warnings, "checks": checks, "kind": "cutoff", "sample": study.get("sample", {}),
              "cutoff": res, "spectra": [{"file": str(out)}],
              "instrument": {"laser_nm": laser, "grating": _f(su["after"].get("grating"))},
              "seconds": {"total": round(time.time() - t_start, 2)}}
    try:
        from . import render
        result["images"] = {"cutoff": render.cutoff_png(sd / "cutoff.png", x, y, res)}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cut-off plot not drawn: {type(exc).__name__}: {exc}")
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
    off = bgk.get("laser_off", SH.default_laser_off(prof, (study.get("setup") or {}).get("laser_nm")))
    _progress(sd, state="running", stage="setup", started=t_start)
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1, confirm=False)
    laser, rng = su["laser"], su["rng"]
    txt = (f"ls6t: white-light background '{bname}'.\n\nTurn the white light on, as it will be during the "
           "measurements" + (".\n\n" + BLOCK_BY_HAND if off == "operator" else " (ls6t keeps the laser shutter closed)")
           + "\n\nPress OK when ready, or Cancel.")
    if ctx.call("notice", timeout_s=3600, text=txt).get("answer") != "ok":
        raise Stop("the operator cancelled the background", "study")
    acq = plan.acquire_step(study, prof, rng)
    acq.pop("verb", None)
    out = sd / "spectra" / "background.txt"
    params = dict(acq, out=str(out), sample=f"white-light background {bname}",
                  native=str(sd / "spectra" / "background.l6s") if study.get("save_native", True) else None)
    if off == "shutter":
        _shutter_gate(prof, laser)
        params.update(laser_off=1, m_laser_shutter=prof["motors"]["laser_shutter"], **shutter_args(prof))
    r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
    if off == "operator":
        ctx.call("notice", timeout_s=3600, text="ls6t: the background is taken. Unblock the laser when you want "
                                                 "to measure again. OK.")
    if not r["ok"]:
        raise Stop(f"background acquisition failed: {r.get('error')}", "study")
    grating_guard(prof, study, su.get("after"), laser, rng, out, checks, warnings, r)
    sp = spectra.read(out)
    ex = _f(r.get("exposure_s")) or float(acq.get("exposure_s") or 1.0)
    per_s = sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s")
    cps = sp["y"] if per_s else sp["y"] / ex
    sat = spectra.saturation(sp["y"] if not per_s else sp["y"] * ex, prof["detector"]["full_scale"],
                             prof["checks"]["saturation_frac"])
    if sat["saturated"]:
        unreliable.append("the background is saturated: a shorter exposure, or less white light")
    # narrow lines in a background: the laser not blocked? Cosmic rays first out (a 5-point median: one to three
    # pixels): on 2026-11-23 two good backgrounds (8 counts at most, no Si line) were rejected for spikes at 415 /
    # 516 / 737 and 269 / 358 / 558 cm-1, in other places each time. Then a third had 'lines' of a few counts at 480 /
    # 594 / 661: noise. A line counts only above checks.background_line_min_counts (20), and is warned about only
    # when it recurs (within 5 cm-1) in another background with this laser: real lamp or room-light lines repeat
    narrow_info = None
    try:
        from scipy.signal import medfilt
        shift, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        xs, ysr = spectra.sort_xy(shift, sp["y"])
        if per_s:
            ysr = ysr * ex                                      # in counts, as the detector saw them
        sig_raw = spectra.noise_sigma(ysr - spectra.baseline(xs, ysr, 3))     # the noise from the spectrum as taken
        ysr = medfilt(ysr, 5) if len(ysr) >= 5 else ysr
        yc = ysr - spectra.baseline(xs, ysr, 3)
        found = spectra.find_peaks(xs, yc, 5, sig_raw or None, 25.0)
        min_counts = float(prof["checks"].get("background_line_min_counts", 20.0))
        lines = [{"cm1": round(v, 1), "counts": round(float(np.interp(v, xs, yc)), 1)} for v in found]
        strong = [ln for ln in lines if ln["counts"] >= min_counts]
        hist = store.read("background_lines.json")
        key = SH._key(laser)
        prev = (hist.get(key) or [])[-10:]
        recurring = [ln for ln in strong if any(abs(ln["cm1"] - q) <= 5.0 for e in prev for q in e.get("lines", []))]
        hist.setdefault(key, []).append({"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "background": bname,
                                         "lines": [ln["cm1"] for ln in strong]})
        hist[key] = hist[key][-20:]
        store.write("background_lines.json", hist)
        narrow_info = {"min_counts": min_counts, "found": lines, "above_min": strong,
                       "recurring": [ln["cm1"] for ln in recurring], "backgrounds_compared": len(prev)}
        narrow = [ln["cm1"] for ln in recurring]
        verified = SH.records().get(SH._key(laser)) if off == "shutter" else None
        if narrow and off == "shutter":
            # the shutter has been shown to block this laser on Si (shutter.py), so these are the lamp or the room's
            # light more likely than the laser; said, kept
            warnings.append(f"the background has lines at {', '.join(f'{v:.0f}' for v in narrow)} cm-1 that recur in "
                            "earlier backgrounds (after spike removal, above "
                            f"{min_counts:g} counts) with the laser shutter closed: the white lamp or room light? The "
                            f"shutter was shown to block this laser on Si on {(verified or {}).get('date', '?')}; if these "
                            "are the sample's Raman lines, run ls6t instrument shutter-verify again")
        elif strong and off != "shutter":
            warnings.append(f"the background has narrow lines at {', '.join(str(round(ln['cm1'])) for ln in strong)} "
                            "cm-1: was the laser blocked? (a Raman line in the background would be subtracted from "
                            "samples)")
    except ValueError:
        pass
    meta = {"name": bname, "laser_nm": laser, "grating": _f(su["after"].get("grating")),
            "objective": su["after"].get("objective"), "exposure_s": ex,
            "accumulations": int(_f(r.get("accumulations")) or 1), "range": rng, "request": str(ctx.folder),
            "laser_off": off}
    shut = shutter_report(r, warnings, verified=SH.gate(prof, laser) is None) if off == "shutter" else {}
    blocked_doubt = any("NOT blocked" in u for u in unreliable)
    stored = (BG.save(bname, sp["x"], cps, sp["meta"].get("x_unit", "nm"), meta) if not blocked_doubt else
              {"name": bname, "stored": False})
    result = {"ok": True, "complete": True, "reliable": not unreliable, "unreliable_because": unreliable,
              "warnings": warnings, "checks": checks, "kind": "background", "sample": study.get("sample", {}),
              "background": {**stored, "max_cps": round(float(np.max(cps)), 3), "mean_cps": round(float(np.mean(cps)), 3)},
              "spectra": [{"file": str(out), "exposure_s": ex, "max_counts": sat["max_counts"]}],
              **({"laser_shutter": shut} if shut else {}),
              **({"narrow_lines": narrow_info} if narrow_info else {}),
              "instrument": {"laser_nm": laser, "grating": meta["grating"], "objective": meta["objective"]},
              "seconds": {"total": round(time.time() - t_start, 2),
                          # the acquisition alone (the bridge's call): two backgrounds in a row after a grating
                          # change show whether the turret moves only then (TESTING T3.16)
                          "acquire": r.get("seconds"),
                          "setup_moves": [{"motor": m["motor"], "seconds": m.get("seconds")}
                                          for m in su.get("readback", [])]}}
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
                 formats=",".join(v.get("formats", [])), camera=v.get("camera"),
                 switch_camera=int(bool(v.get("switch_camera"))),
                 stop_video=int(bool(v.get("stop_from_script"))), **motor_args(ctx.prof))
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


BLOCK_BY_HAND = ("THE LASER IS NOT TURNED OFF BY ls6t: without a confirmed laser shutter motor, LabSpec opens its shutter "
                 "for this acquisition as for any other. Press OK ONLY IF YOU ARE AT THE INSTRUMENT and the beam is "
                 "PHYSICALLY BLOCKED (a beam block in the laser path, or the laser switched off at its key or "
                 "source). Do not confirm from a remote session: nobody there can see the beam.")


def note_grating_set(asked, got, by: str) -> None:
    """The last grating change ls6t made, for the warm-up log (LS6T_HOME/last_grating_set.json): study setups too,
    not only `instrument set` (2026-11-23)."""
    try:
        (config.home() / "last_grating_set.json").write_text(json.dumps(
            {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "asked": asked, "got": got, "by": by}), encoding="utf-8")
    except OSError:
        pass


def grating_guard(prof: dict, study: dict, after: dict, laser, rng: dict, path, checks: dict | None = None,
                  warnings: list | None = None, acq_res: dict | None = None) -> None:
    """Stops the study when the first spectrum's own axis does not fit the grating LabSpec reports (plan.
    grating_window_check): 2026-11-23, LabSpec said 1800 / 1200 while the light came through 1200 / 600. Profile
    checks.grating_window false turns it off."""
    if not (prof.get("checks") or {}).get("grating_window", True):
        return
    grating = (study.get("setup") or {}).get("grating", (after or {}).get("grating"))
    try:
        sp = spectra.read(path)
    except OSError:
        return
    res = plan.grating_window_check(sp["x"], sp["meta"].get("x_unit", "nm"), laser, grating, prof, rng)
    if acq_res and acq_res.get("spectro_asked_nm") not in (None, "") and acq_res.get("spectro_nm") not in (None, ""):
        a, g = float(acq_res["spectro_asked_nm"]), float(acq_res["spectro_nm"])
        res["spectro_nm"] = {"asked": a, "got": g}
        if abs(a - g) > 1.0:
            res["stop"].append(f"the spectrometer reads {g:.2f} nm after being sent to {a:.2f} nm")
            res["ok"] = False
    if checks is not None:
        checks["grating_window"] = {"pass": res["ok"], **{k: v for k, v in res.items() if k != "ok"}}
    if warnings is not None:
        warnings.extend(res.get("warn") or [])
    if not res["ok"]:
        raise Stop("the spectrum does not fit what was asked (" + "; ".join(res["stop"]) + "): the spectrometer may "
                   "not have followed the grating or centre sent through the script (TESTING T3.16); nothing more "
                   "was measured. Check one acquisition in LabSpec by hand with the same grating and centre", "study")


def reselect_laser(call, prof: dict, via: bool = False) -> dict | None:
    """Selects the laser LabSpec has now once more, before a grating move is tried again: LabSpec has refused a grating
    that the previous laser could not use (2026-11-23: 2400 after a scripted visit to 785 nm, though the laser was
    637.75 nm again; a bridge restart cleared it). The move is sent to LabSpec whatever it reads (the bridge's move
    verb does not skip a position already in place; only set-up steps do). via=True goes through another laser
    first (the shortest wavelength, which every grating can use) and back, in case LabSpec ignores a move to where
    it is. No laser fires: this selects the laser path only. call(verb, timeout_s=..., **params) is the bridge's."""
    try:
        st = call("status", timeout_s=15, **motor_args(prof))
        las = _f(st.get("laser_nm"))
        if las is None:
            return None
        out = {"laser_nm": las, "via": None}
        if via:
            others = sorted(float(v["nm"]) for v in (prof.get("lasers") or {}).values()
                            if abs(float(v["nm"]) - las) > 1)
            if others:
                out["via"] = others[0]
                call("move", timeout_s=600, motor=prof["motors"]["laser"], mode="value", value=others[0])
        r = call("move", timeout_s=600, motor=prof["motors"]["laser"], mode="value", value=las)
        out["ok"] = bool(r.get("ok")) and P.same_position(r.get("value"), las)
        return out
    except Stop:
        raise
    except Exception:  # noqa: BLE001
        return None


def retry_grating(call, prof: dict, move_params: dict, value) -> tuple[dict, list]:
    """A grating that did not arrive: the laser selected again, the grating moved again; then, if still not there,
    through another laser and back, and the grating again. Returns the last move's answer and what was tried."""
    tried = []
    r = {}
    for via in (False, True):
        tried.append({"reselect": reselect_laser(call, prof, via=via)})
        r = call("move", timeout_s=600, **move_params)
        got = r.get("value")
        tried[-1].update(got=got, ok=bool(r.get("ok")) and P.same_position(got, value))
        if tried[-1]["ok"]:
            break
    return r, tried


def move_extra(prof: dict, motor: str, confirmed_now: bool = False) -> dict:
    """{'blocking': 1} for the motors moved the blocking way (profile blocking_motors, by role; the grating by
    default: with NO_WAIT it reported idle at once and once stopped part way, 2026-11-20). {'no_message': 1} only
    for the objective move the operator has just confirmed in ls6t's question (confirmed_now: the caller sets it
    right after that Yes, for that move alone): LabSpec then does not ask a second time, and every other objective
    move keeps LabSpec's own prompt, so there is always exactly one confirmation (profile suppress_labspec_prompt,
    on by default; 2026-11-22, T3.2)."""
    roles = prof.get("blocking_motors", []) or []
    names = {prof["motors"].get(r) for r in roles if prof["motors"].get(r)}
    out = {"blocking": 1} if motor in names else {}
    if motor == prof["motors"].get("grating"):
        # read until it is the grating asked (a passing grating can read still for 1.5 s: 2026-11-23)
        out.update(until_target=1, until_target_s=120, until_target_stable_s=10)
    if confirmed_now and motor == prof["motors"].get("objective") and prof.get("suppress_labspec_prompt", True):
        out["no_message"] = 1
    return out


def _matches(step: dict, r: dict) -> tuple[bool, str]:
    if not r.get("ok"):
        return False, ""
    if step["mode"] == "label":
        got = r.get("label", "")
        return P.same_position(got, step["value"]), got
    got = _f(r.get("value"))
    return got is not None and P.same_position(got, step["value"]), str(got)


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
        win = calibration_window(study, grating, len(ref_lines))
        process["peaks"] = [{"name": f"ref{i}" if len(ref_lines) > 1 else "Si", "near_cm1": c, "window_cm1": win}
                            for i, c in enumerate(ref_lines)]
    # ls6t's own spectral correction (from an earlier calibration), never on a calibration itself
    corr = xcal.get(laser, grating) if kind != "calibration" else None
    if kind != "calibration" and laser:
        zs = zero_state(laser, grating, prof)
        checks["zero"] = zs
        tol0 = float(prof["checks"]["si_tol_cm1"])
        if not corr and zs.get("offset_cm1") is not None and abs(float(zs["offset_cm1"])) > tol0:
            warnings.append(f"the last calibration check for {combo(laser, grating)} ({zs['time']}) found the "
                            f"reference line {float(zs['offset_cm1']):+.2f} cm-1 off: the positions here carry it. "
                            "Correct LabSpec's zero for this grating, or run a calibration with adjust 'ls6t'")
        elif zs.get("offset_cm1") is None and zs.get("other_gratings"):
            warnings.append(f"no calibration check yet for {combo(laser, grating)} (checked with this laser: "
                            + ", ".join(f"{k} {v:+.2f} cm-1" for k, v in zs["other_gratings"].items()
                                        if v is not None)
                            + "); each grating has its own zero in LabSpec")
    if corr:
        age = xcal.age_h(corr)
        warnings.append(f"spectral correction applied: true = {corr['a']:+.3f} + {corr['b']:.6f} x measured "
                        f"(calibration {corr['time']}, {corr['reference']})")
        lkc = P.laser_key(prof, laser) if laser else None
        max_h = float((prof["lasers"][lkc].get("calibration_max_age_h") if lkc else None)
                      or prof.get("calibration", {}).get("max_age_h", 24))
        if age is not None and age > max_h:
            warnings.append(f"the spectral correction is {age:.1f} h old (more than {max_h:g} h for this laser): run "
                            "a calibration" + (", or keep the laser line in the window with "
                                               "acquire.laser_reference 'correct'"
                                               if lkc and prof["lasers"][lkc].get("calibration_max_age_h") else ""))
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
    # the laser line, when the window includes it (2026-11-23: the 637.75 nm laser hopped by ~0.35 nm after the first
    # spectrum of a request: laser line -4.6 -> -12 cm-1 and Si 520.2 -> 512.4, their difference constant)
    lk_ = P.laser_key(prof, laser) if laser else None
    # the laser's own default (2026-11-24: the 637.75 nm laser's wavelength drifts, about 0.07 nm overnight and
    # +0.5 cm-1 within 30 s: 'correct' follows it whenever the laser line is in the window)
    ll_mode = study.get("acquire", {}).get("laser_reference") or \
        ((prof["lasers"][lk_].get("laser_reference") if lk_ else None) or "report")
    ll_ref = laser_line_reference(laser, grating)
    ll_ref_from = "calibration" if ll_ref is not None else None
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
        ll = laser_line_fit(shift, y)
        if ll:
            rec["laser_line"] = ll
            if ll_ref is None and ll.get("ok"):
                ll_ref, ll_ref_from = ll["position_cm1"], f"point {rec['point']}"
            if ll_mode == "correct" and ll.get("ok") and ll_ref is not None:
                d = ll["position_cm1"] - ll_ref
                shift = shift - d
                rec["laser_line_correction_cm1"] = round(-d, 3)
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
        if ll and ll.get("ok"):
            # each peak also from this spectrum's own laser line (the Raman shift by definition; LabSpec's scale may
            # carry its own offset between 0 and the band: on the XploRA Si was 524.8 from the line, 520.2 on the scale)
            base = ll["position_cm1"] + (rec.get("laser_line_correction_cm1") or 0.0)
            if corr:
                base = corr["a"] + corr["b"] * base
            for pk in a["peaks"]:
                if pk.get("ok") and pk.get("position_cm1") is not None:
                    pk["from_laser_line_cm1"] = round(float(pk["position_cm1"]) - base, 3)
        rec.update(max_counts=sat["max_counts"], saturated=sat["saturated"], saturated_pixels=sat["pixels"],
                   noise_sigma=a["noise_sigma"], spikes=len(a["spikes"]), from_cm1=a["from_cm1"],
                   to_cm1=a["to_cm1"], peaks=a["peaks"], bands=a["bands"], auto_peaks=a["auto_peaks"])
        if sat["saturated"]:
            sat_points.append(rec["point"])
        if a["spikes"]:
            spike_points.append(rec["point"])
        lo, hi = rng.get("from_cm1"), rng.get("to_cm1")
        if lo is not None and hi is not None and not rng.get("single"):
            tol_r = max(3.0, 0.005 * (hi - lo))      # a pixel or two short is not a gap (997.6 for 1000, 2026-11-23)
            if a["from_cm1"] > lo + tol_r or a["to_cm1"] < hi - tol_r:
                cover_short.append(rec["point"])
        axes.append(a["_x"])
        ys.append(a["_y"])
        if kind != "map":
            spectra.write(Path(rec["file"]).with_suffix(".shift.txt"), a["_x"], a["_y"],
                          {"x_unit": "1/cm", "y_unit": sp["meta"].get("y_unit", "cnt"), "laser_nm": laser,
                           "ics": rec.get("ics") or "unknown"})
    n = len(per_point)
    checks["saturation"] = {"pass": not sat_points, "points": sat_points[:50], "full_scale": fs, "threshold": fs * frac}
    if sat_points:
        unreliable.append(f"saturated in {len(sat_points)} of {n} spectra: choose a stronger filter or a shorter "
                          "exposure")
    checks["spikes"] = {"points": spike_points[:50], "count": len(spike_points)}
    if spike_points and spike_mode == "none":
        # with LabSpec's spike removal on, what remains is usually a narrow Raman line, not a cosmic ray
        warnings.append(f"cosmic-ray spikes left in {len(spike_points)} spectra (spike removal is off: use 'double')")
    ics_states = sorted({str(r.get("ics")) for r in per_point if r.get("ics")})
    if ics_states:
        if len(ics_states) > 1:
            warnings.append("ICS was not the same for every spectrum (" + "; ".join(ics_states) + "): intensities with "
                            "and without ICS are not comparable")
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

    lls = [(r["point"], r["laser_line"]["position_cm1"]) for r in per_point if (r.get("laser_line") or {}).get("ok")]
    if ll_mode == "correct" and len(lls) < len(per_point):
        miss = [r["point"] for r in per_point if not (r.get("laser_line") or {}).get("ok")]
        unreliable.append(f"acquire.laser_reference 'correct', but no laser line was found in spectra "
                          f"{', '.join(map(str, miss[:10]))}: the window must include about -15 to +15 cm-1 "
                          "(those spectra are not corrected)")
    if lls:
        ref0 = ll_ref if ll_ref is not None else lls[0][1]
        jumps = [(p, round(v - ref0, 2)) for p, v in lls if abs(v - ref0) > LASER_JUMP_CM1]
        checks["laser_line"] = {"reference_cm1": round(ref0, 3), "reference_from": ll_ref_from, "mode": ll_mode,
                                "positions_cm1": {str(p): round(v, 3) for p, v in lls},
                                "jumps": [{"point": p, "moved_cm1": d} for p, d in jumps]}
        if jumps:
            msg = (f"the laser line moved by {', '.join(f'{d:+.1f}' for _, d in jumps[:6])} cm-1 at point(s) "
                   f"{', '.join(str(p) for p, _ in jumps[:6])} (a laser mode hop?): every Raman shift there moves with it")
            if ll_mode == "correct":
                warnings.append(msg + "; corrected (acquire.laser_reference 'correct')")
            else:
                unreliable.append(msg + "; use acquire.laser_reference 'correct' with the laser line in the window")
    ss = same_spot_check(kind, per_point, warnings, unreliable) if kind in ("spectrum", "series") else None
    if ss:
        checks["same_spot"] = ss
    moved = [r["point"] for r in per_point if (r.get("optics_check") or {}).get("spectro_moved_nm", 0) > 0.005
             or (r.get("optics_check") or {}).get("motor_busy")]
    if moved:
        warnings.append(f"the spectrometer moved, or a motor was busy, around the acquisition of point(s) "
                        f"{', '.join(map(str, moved[:10]))} (optics_check)")
    out: dict = {"files": {"folder": str(sd)}}
    aes = [rec["auto_exposure"] for rec in per_point if rec.get("auto_exposure")]
    if aes:             # LabSpec's AutoExposure around the acquisitions (2026-11-23: a pass visible in the result)
        out["auto_exposure"] = dict(aes[0], spectra=len(aes),
                                    all_restored=all(a.get("restored") is not False for a in aes))
    if ics_states:      # the ICS state of the spectra: intensities with and without ICS are not comparable
        out["ics"] = ics_states[0] if len(ics_states) == 1 else ics_states
    if kind == "map":
        out["map"] = _map_result(name, study, per_point, axes, ys, sd, process)
        out["files"]["map"] = out["map"]["files"]
        if out["map"]["nx"] == 1 and out["map"]["ny"] == 1 and out["map"]["nz"] > 1:
            out["depth"] = depth_result(study, out["map"], prof, sd, warnings)
        if failed:
            warnings.append(f"a peak fit failed at {len(set(failed))} of {n} map points (null in the map)")
    elif kind == "series":
        out["series"] = _series_result(per_point, process)
        # start to start (2026-11-24: 2 s asked gave 1.66 s, 6 s gave 7.8 s): what was asked and what happened
        ts = [r.get("t_start") for r in per_point if r.get("t_start") is not None]
        asked = float((study.get("series") or {}).get("interval_s", 0) or 0)
        if len(ts) > 1:
            gaps = [round(b - a, 3) for a, b in zip(ts, ts[1:])]
            sp_ = {"asked_s": asked, "mean_s": round(sum(gaps) / len(gaps), 3), "min_s": min(gaps),
                   "max_s": max(gaps), "start_to_start_s": gaps}
            out["series"]["spacing"] = sp_
            if asked > 0 and abs(sp_["mean_s"] - asked) > max(0.2, 0.1 * asked):
                settled = [r.get("settled_s") for r in per_point if r.get("settled_s")]
                warnings.append(f"the spectra were {sp_['mean_s']:.2f} s apart (start to start) for the "
                                f"{asked:g} s asked" + (f": the laser's pause between acquisitions "
                                                        f"(settle_s, up to {max(settled):.1f} s) made them longer"
                                                        if settled and sp_["mean_s"] > asked else
                                                        ": each spectrum took longer than the interval"
                                                        if sp_["mean_s"] > asked else ""))
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


def combo(laser, grating) -> str:
    """'the 2400 gr/mm grating with 637.75 nm': what a calibration (LabSpec's zero, ls6t's correction) belongs to."""
    g = _f(grating)
    return (f"the {g:g} gr/mm grating" if g else "this grating") + (f" with {float(laser):g} nm" if laser else "")


def zero_state(laser, grating, prof: dict) -> dict:
    """The last calibration check for this laser and grating (perf 'zero'), and the others of this laser: each
    grating has its own zero in LabSpec (2026-11-23, 637.75 nm: +0.3 cm-1 with 1200 after AutoCalibration, +3.1
    with 2400)."""
    from . import perf
    k = perf.key(laser, grating)
    rec = perf.get("zero", k)
    out: dict = {"key": k}
    if rec:
        e = rec["latest"]
        out.update(offset_cm1=e.get("offset_cm1"), reference=e.get("reference"), time=e.get("time"),
                   checks=len(rec.get("history", [])))
    else:
        out["offset_cm1"] = None
    others = {kk: v["latest"].get("offset_cm1") for kk, v in ((perf.load().get("zero")) or {}).items()
              if kk != k and laser and kk.split("@")[0] == perf.key(laser)}
    if others:
        out["other_gratings"] = others
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
                      "fwhm_cm1": pk.get("fwhm_cm1"), "snr": pk.get("snr"), "why": pk.get("why"),
                      "warning": pk.get("warning")})
    ok = [l for l in lines if l["measured_cm1"] is not None]
    res: dict = {"reference": refname, "lines": lines, "tol_cm1": tol, "adjust": cal.get("adjust", "none"),
                 "applies_to": {"laser_nm": laser, "grating": _f(grating), "key": xcal.key(laser, grating),
                                "what": combo(laser, grating) + " only: LabSpec keeps a zero per grating"}}
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
    # good enough to correct the zero by? 2026-11-23: at 1 %, 1 s the Si line was weak and its fit sat at the edge of
    # the window (525.12 cm-1); ls6t still asked for a -4.42 cm-1 correction; AutoCalibration then found 520.64
    min_snr = float(prof["checks"].get("min_snr", 10.0))
    doubts = [f"{l['reference_cm1']:g} cm-1: " + ("the fit sits at the edge of its window" if "edge" in str(l.get("warning") or "")
                                                  else f"signal-to-noise {l['snr']:.0f} (below {min_snr:g})")
              for l in ok if "edge" in str(l.get("warning") or "") or (l.get("snr") is not None and l["snr"] < min_snr)]
    res["trusted"] = not doubts
    if doubts:
        res["not_trusted_because"] = doubts
        unreliable.append("the reference line was not measured well enough to judge the zero (" + "; ".join(doubts)
                          + "): use a stronger filter or a longer exposure, check the focus, then measure again")
    checks["calibration"] = {"pass": res["pass"], "offset_cm1": res["offset_cm1"], "worst_error_cm1": res["worst_error_cm1"],
                             "tol_cm1": tol}
    if res["trusted"]:
        from . import perf
        llc = (rec.get("laser_line") or {})
        perf.record("zero", perf.key(laser, grating), {"offset_cm1": res["offset_cm1"], "reference": refname,
                                                        "pass": res["pass"], "tol_cm1": tol,
                                                        **({"laser_line_cm1": llc["position_cm1"]}
                                                           if llc.get("ok") and res["pass"] else {})}, request)
    lk = P.laser_key(prof, laser)
    if lk and prof["lasers"][lk].get("mode_hops"):
        res["mode_note"] = (f"the {float(laser):g} nm laser has mode hops (profile lasers.{lk}.mode_hops): this "
                            "calibration holds for the laser mode it was measured in"
                            + (f" (laser line at {rec['laser_line']['position_cm1']:.2f} cm-1)"
                               if (rec.get("laser_line") or {}).get("ok") else
                               "; include the laser line in the window to record which mode"))
    # in words, so that 'pass: false' with a correction stored does not read as a failure (2026-11-23)
    if not res["trusted"]:
        res["outcome"] = "not judged: the reference line was not measured well enough"
    elif res["pass"]:
        res["outcome"] = f"within tolerance ({res['offset_cm1']:+.2f} cm-1, tolerance {tol:g})"
    elif res["adjust"] == "ls6t":
        res["outcome"] = (f"out of tolerance before correction ({res['offset_cm1']:+.2f} cm-1); corrected: ls6t "
                          f"corrects later spectra with {combo(laser, grating)}")
        res["corrected"] = True
    else:
        res["outcome"] = (f"out of tolerance ({res['offset_cm1']:+.2f} cm-1, tolerance {tol:g}): correct LabSpec's "
                          f"zero for {combo(laser, grating)}")
    checks["calibration"]["outcome"] = res["outcome"]
    if res["adjust"] == "ls6t" and res["trusted"]:
        st = xcal.store(laser, grating, a, b, refname, lines, request)
        res["stored_correction"] = st
        warnings.append(f"ls6t now corrects later spectra ({xcal.key(laser, grating)}): true = {a:+.3f} + "
                        f"{b:.6f} x measured")
    elif not res["pass"]:
        unreliable.append(f"the {'Si' if refname == 'si' else refname} line is at {first['measured_cm1']:.2f} cm-1, "
                          f"{first['measured_cm1'] - first['reference_cm1']:+.2f} from {first['reference_cm1']}: "
                          f"adjust the zero in LabSpec for {combo(laser, grating)} (calibration.adjust 'labspec' "
                          "guides it), or let ls6t correct (adjust 'ls6t')")
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
                 acq_time=mp.get("acq_time", "interface"), chunk=int(mp.get("export_chunk", 65536) or 0),
                 **params, **motor_args(prof))
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
                   accumulations=float(acq.get("accumulations", 1)), id=r.get("id"), data_via=r.get("data_via"))
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
            "saturated", "noise_sigma", "spikes", "from_cm1", "to_cm1", "peaks", "bands", "x_correction", "background",
            "ics", "auto_exposure", "optics_check")
    return {k: r.get(k) for k in keys if k not in ("auto_exposure", "optics_check") or r.get(k)}


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
