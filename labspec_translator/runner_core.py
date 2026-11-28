"""runner_core.py — request execution, instrument lock, Bridge Ctx, setup, live view, and stage/Z safety guards.

Split from runner.py to separate execution plumbing and hardware safety from study procedures and spectral analysis.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from . import __version__, BRIDGE_PROTOCOL, config, plan, spectra, video
from . import spot as SP
from . import store
from . import shutter as SH
from . import profile as P
from .bridge import Bridge, BridgeError, BridgeGone, BridgeTimeout  # noqa: F401
from .io_util import (
    LASER_MAY_BE_ON, parse_kv_and_csv, pid_alive, read_json, replace_retry, to_float,  # noqa: F401
    write_json as _write_json,
)
from .analysis import laser_line_fit, SPECTRO_NM_PLAUSIBLE


FINAL = ("done", "cancelled", "failed", "interrupted")


class Stop(Exception):
    """The request ends here: cancelled, stopped by the operator, declined, or the bridge is gone."""

    def __init__(self, why: str, by: str):
        super().__init__(why)
        self.why, self.by = why, by


# --------------------------------------------------------------------------------------------- files

def write_json(path: Path, data):
    _write_json(path, data, indent=1, default=_jsonable)


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


def header_ics_denoise(path) -> tuple:
    """ICS and DeNoise as LabSpec's spectrum header gives them ('ICS correction=Off', 'DeNoise=Lite'), or None."""
    try:
        meta = spectra.header(path)
    except OSError:
        return None, None
    ics = dn = None
    for k, v in meta.items():
        kl = k.lower()
        if "ics" in kl.replace("physics", "") and "correction" in kl and ics is None:
            ics = (("on" if str(v).strip().lower().startswith("on") else "off" if str(v).strip().lower().startswith(
                "off") else str(v).strip()) + f" (LabSpec's header: {k.split('.')[-1]}={v})")
        if "denoise" in kl and dn is None:
            dn = str(v).strip()
    return ics, dn




def optics_record(r: dict) -> dict | None:
    """What the bridge read around one Acq: the time since the previous spectrum (and any pause), the spectrometer
    and grating before Acq, right after it returned and after the data, and whether a motor was busy then
    (2026-11-23: spectra after the first in a request shifted and broadened at the same spot)."""
    keys = ("since_last_acq_s", "settled_s", "spectro_before_acq", "spectro_after_acq_call", "spectro_after_data",
            "grating_before_acq", "grating_after_data", "motors_busy_before_acq", "motors_busy_after_acq_call")
    out = {k: _f(r.get(k)) for k in keys if r.get(k) not in (None, "")}
    if not out:
        return None
    # a read-back right after a grating change can be nonsense (2026-11-24: -2027.24 nm, 'moved' 2787.9 nm)
    bogus = [k for k in ("spectro_before_acq", "spectro_after_acq_call", "spectro_after_data")
             if out.get(k) is not None and not SPECTRO_NM_PLAUSIBLE[0] <= out[k] <= SPECTRO_NM_PLAUSIBLE[1]]
    if bogus:
        out["spectro_reading_invalid"] = bogus
    sp = [out[k] for k in ("spectro_before_acq", "spectro_after_acq_call", "spectro_after_data")
          if out.get(k) is not None and k not in bogus]
    if len(sp) >= 2:
        out["spectro_moved_nm"] = round(max(sp) - min(sp), 4)
    # a motor busy just before Acq, right after the bridge's own spectrometer move to the study's window, is that
    # move settling (2026-11-24: warned at point 0); a motor busy once Acq has returned is what matters
    own_move = r.get("t_spectro_call_s") not in (None, "") and str(r.get("spectro_skipped", "")) != "1"
    out["motor_busy"] = bool(out.get("motors_busy_after_acq_call") or
                             (out.get("motors_busy_before_acq") and not own_move))
    if own_move:
        out["spectro_moved_by_ls6t"] = True
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


LASER_SELECTED = "laser_selected.json"


def note_laser_selected(laser) -> None:
    """When ls6t changed to this laser (its settling is counted from then, also by a later request)."""
    try:
        d = store.read(LASER_SELECTED) or {}
        d[f"{float(laser):g}"] = time.time()
        store.write(LASER_SELECTED, d)
    except (OSError, TypeError, ValueError):
        pass


def select_wait_s(prof: dict, laser, kind, changed_now: bool) -> float:
    """What is left of the laser's settling after it was selected (plan.change_settle_s): all of it when this request
    changed to it, the rest when an earlier request did so lately, else none."""
    if laser is None:
        return 0.0
    full = plan.change_settle_s(prof, laser, kind)
    if full <= 0 or changed_now:
        return full
    try:
        t0 = float((store.read(LASER_SELECTED) or {}).get(f"{float(laser):g}") or 0)
    except (OSError, TypeError, ValueError):
        t0 = 0.0
    left = full - (time.time() - t0) if t0 else 0.0
    return round(left, 1) if left > 1.0 else 0.0


def settle_reason(prof: dict, laser, kind, changed_now: bool) -> tuple:
    """(seconds, why) of the settling wait after a laser selection, said in every result (settle.settle_waited_s:
    Nicola, 2026-11-27, 785 -> 532 showed 0.0 s with no reason)."""
    full = plan.change_settle_s(prof, laser, kind) if laser is not None else 0.0
    w = select_wait_s(prof, laser, kind, changed_now)
    if w > 0:
        return w, ("this request changed to the laser" if changed_now else
                   f"the laser was selected {full - w:.0f} s ago by an earlier request: the rest of its {full:g} s")
    if full <= 0:
        return 0.0, (f"none for a {kind} (lasers.<k>.change_settle_s applies to calibrations and laser checks, unless "
                     "set for the laser)")
    if changed_now:
        return 0.0, "none"
    try:
        t0 = float((store.read(LASER_SELECTED) or {}).get(f"{float(laser):g}") or 0)
    except (OSError, TypeError, ValueError):
        t0 = 0.0
    return 0.0, (f"no laser change in this request; ls6t selected it {time.time() - t0:.0f} s ago, more than its "
                 f"{full:g} s" if t0 else
                 "no laser change in this request, and no selection by ls6t on record (selected by hand in LabSpec?)")


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
        self.keep_hold = False                 # and kept into the next study (same laser, holds too)
        self.hold_idle_s = 60.0
        self.hold_wanted = False               # this study asks for it (run_any)
        self.hold_log: list = []
        self.objective_by_operator = None      # the objective the operator confirmed in this request
        self.objective = None               # LabSpec's objective now (its record)
        self.focus_xy = (None, None)        # where the focus was found (the operator's spot, or the last focus)
        self.z_purpose = None               # 'depth' while a depth profile runs (its own Z limit, confirmed)
        self.focus_stale = False            # after a retract for an objective change: a fresh focus is needed
        self.last_beat = 0.0
        self._beat_lock = threading.Lock()
        self.bridge_version = None
        self.optics = (None, None)          # (laser nm, grating) after the last setup: for ics.off_for
        self.last_ics = None                # the ICS state of the last acquisition sent

    def cancelled(self) -> bool:
        return (self.folder / "cancel").exists()

    def start_heartbeat(self, every_s: float = 10.0) -> None:
        """A heartbeat of its own (runner.json, and the lock while held), whatever the runner is doing: 2026-11-25,
        study_status called a live runner 'interrupted' (no beat for 120 s during its analysis), and the operator
        switched the laser off on that false report."""
        def loop():
            while not getattr(self, "finished", False):
                time.sleep(every_s)
                try:
                    self.beat(force=True)
                except Exception:  # noqa: BLE001 - the next one tries again
                    pass
        threading.Thread(target=loop, name="ls6t-runner-beat", daemon=True).start()

    def beat(self, force: bool = False):
        now = time.time()
        if not (force or now - self.last_beat > 3):
            return
        if not self._beat_lock.acquire(blocking=False):
            return                                       # the other thread is writing it now
        try:
            self._beat(now)
        finally:
            self._beat_lock.release()

    def _beat(self, now: float):
        self.last_beat = now
        write_json(self.folder / "runner.json", {"pid": os.getpid(), "time": now, "version": __version__})
        if getattr(self, "owns_lock", False):
            # only the holder keeps the lock fresh (2026-11-25: a queued request's beat kept a dead runner's
            # lock fresh, and every request after it queued forever)
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
                                             if k in ("motor", "value", "action", "kind", "state", "mode", "exposure_s",
                                                      "id", "index", "axis_x", "axis_y")))
        if self.hold_wanted and not self.holding and verb in ("acquire", "map_native", "guide") and not laser_off:
            self._hold_on()
        if verb == "acquire":
            params.setdefault("ae_off", ae_off(self.prof))
            if self.holding:
                params.setdefault("settle_s", 0.0)       # the shutter is held open: no pause needed
                params["held_expected"] = 1              # the bridge reads the hold back after it, or says it is gone
            # a pause after the previous spectrum (TESTING T3.21: the spectra after the first in a request were
            # shifted and broadened)
            params.setdefault("settle_s", plan.settle_s(self.prof, self.optics[0]))
        t0 = time.monotonic()
        wf = self.folder / "waiting.json"
        if verb in OPERATOR_VERBS:
            # the request waits for a person at LabSpec: study_status and the flat tools say so (2026-11-25: an
            # unattended small-model client saw a silent 'setup' while the go-ahead dialog waited)
            try:
                write_json(wf, {"for": "operator", "verb": verb, "text": str(params.get("text", ""))[:400],
                                "since": time.time()})
            except OSError:
                pass
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
            raise Stop(self._release_after_silence(str(exc)), "bridge") from exc
        except BridgeTimeout as exc:
            raise Stop(self._release_after_silence(str(exc)), "timeout") from exc
        finally:
            if verb in OPERATOR_VERBS:
                try:
                    wf.unlink()
                except OSError:
                    pass
        self.beat()
        self._log(f"<- {verb} ok={int(bool(r.get('ok')))} {r.get('seconds', '')} s"
                  + "".join(f" {k}={r[k]}" for k in ("id", "mapex_return", "map_create_return", "view",
                                                       "hold_after_acq_held", "hold_after_acq_shutter_mode",
                                                       "hold_after_acq_shutter_label", "max_counts")
                            if r.get(k) not in (None, ""))
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

    def _release_after_silence(self, msg: str) -> str:
        """The bridge did not answer while ls6t held the shutter: before saying the laser may still reach the sample,
        wait for the bridge to answer again (a long blocking move may have held it: 2026-11-25, the first acquisition
        after a grating change) and release the hold then; the message says which happened."""
        hb = self.bridge.heartbeat() or {}
        if not (self.holding or str(hb.get("shutter_held", "0")) == "1"):
            return msg
        from . import runner as _R
        if self.bridge.wait_alive(_R.RELEASE_WAIT_S, on_wait=self.beat):
            try:
                r = self.bridge.call("shutter_hold", timeout_s=30, state="off")
                if r.get("ok"):
                    self.holding = False
                    self.hold_log.append({"released": True, "by": "after the bridge answered again"})
                    first = " ".join(msg.replace(LASER_MAY_BE_ON, "").split()).split(". ")[0].rstrip(".")
                    return (f"{first}; it answered again afterwards, so LabSpec was only held up (no restart "
                            "needed). ls6t stopped the request and released the shutter hold: the laser shutter is "
                            "closed. Run the request again; tell the ls6t maintainer which command was held up.")
            except BridgeError:
                pass
        try:
            self.bridge.queue("safe")       # carried out as soon as the bridge is free
        except OSError:
            pass
        return self._held_warning(msg) + (" ls6t left a 'safe' command (shutter closed, hold released) for the "
                                          "bridge: it runs as soon as the bridge is free.")

    def _held_warning(self, msg: str) -> str:
        """The bridge stopped answering while ls6t held the laser shutter open: say so first."""
        if self.holding and LASER_MAY_BE_ON not in msg:
            return LASER_MAY_BE_ON + " " + msg
        return msg

    def _hold_on(self) -> None:
        """The laser shutter held open (ShutterMode manual) before this study's next acquisition, as HORIBA's
        mapping scripts do for a map; the bridge keeps it only through HOLD_VERBS."""
        w = plan.hold_open_s(self.prof, self.optics[0] if self.optics else None, getattr(self, "study_kind", None))
        # this laser selected lately (here or by an earlier request): what is left of its settling counts too
        # (2026-11-25, 532 after 637.75; 2026-11-27, 532 after 785)
        w_open = w
        w_sel, why_ = settle_reason(self.prof, self.optics[0] if self.optics else None,
                                    getattr(self, "settle_kind", None) or getattr(self, "study_kind", None),
                                    getattr(self, "laser_changed_now", False))
        w = max(w, w_sel)
        self.settle_info = {"settle_waited_s": round(w, 1), "after_selection_s": round(w_sel, 1),
                            "hold_open_s": round(w_open, 1), "why": why_, "how": "the shutter held open"}
        r = self.call("shutter_hold", timeout_s=30 + w, state="on", max_s=getattr(self, "hold_max_s", 3600),
                      idle_s=getattr(self, "hold_idle_s", 60), open_wait_s=w, **motor_args(self.prof))
        if not r.get("ok"):
            if r.get("stopped_by"):
                raise Stop("stopped by the operator in LabSpec" if r.get("stopped_by") == "operator" else "cancelled",
                           "operator" if r.get("stopped_by") == "operator" else "cancel")
            raise Stop(f"acquire.shutter 'hold': {r.get('error')}", "study")
        self.holding = True
        self.hold_log.append({"held": True, "shutter_before": r.get("old_index"), "mode_before": r.get("old_mode"),
                              "label_while_held": r.get("hold_label"),
                              **({"open_wait_s": _f(r.get("open_wait_s"))} if r.get("open_wait_s") not in (None, "")
                                 else {})})

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
          ("t_spectro_jog_s", "the spectrometer sent away (a calibration repeat)"),
          ("t_spectro_approach_s", "the spectrometer to its approach point (spectrometer.approach_nm)"),
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


LOCK_STALE_S = 90.0     # a lock nobody refreshed for this long is free


# pid_alive is imported from .io_util and re-exported here for callers


def runner_alive(folder) -> bool:
    """A request's runner still runs: a heartbeat (runner.json) or runner.log written within LOCK_STALE_S, or (when
    both are older) its process still there."""
    f = Path(folder)
    rj = read_json(f / "runner.json", {}) or {}
    beat = rj.get("time")
    if beat is None:
        beat = (read_json(f / "request.json", {}) or {}).get("created", 0)
    newest = float(beat or 0)
    try:
        newest = max(newest, (f / "runner.log").stat().st_mtime)
    except OSError:
        pass
    if time.time() - newest <= LOCK_STALE_S:
        return True
    pa = pid_alive(rj.get("pid") or (read_json(f / "state.json", {}) or {}).get("pid"))
    return bool(pa) and time.time() - newest <= 1800      # a live pid, but silent for 30 min: not a runner any more


def holder_alive(holder) -> bool:
    """Whether the request holding the instrument lock still has a runner: its state is not final and its runner
    wrote its heartbeat (runner.json) within LOCK_STALE_S."""
    if not holder:
        return False
    hf = Path(holder)
    st = read_json(hf / "state.json", {}) or {}
    if st.get("state") in FINAL:
        return False
    return runner_alive(hf)


def free_lock_of(folder: Path) -> bool:
    """Removes the instrument lock if this request holds it (a request that ended without its runner)."""
    lp = lock_path()
    if read_json(lp, {}).get("request") == str(folder):
        try:
            lp.unlink()
            return True
        except OSError:
            return False
    return False


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
            if age > LOCK_STALE_S or holder == str(folder) or not holder_alive(holder):
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
        ctx.start_heartbeat()
        _acquire_lock(folder, ctx.beat)
        ctx.owns_lock = True
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
        names = list(studies)
        for si_, (name, study) in enumerate(studies.items()):
            # keep the shutter held into the next study when it holds too, with the same laser (2026-11-24: the
            # 637.75 nm laser hopped when the shutter closed for 5-12 s between two studies; not after 22 s)
            nxt = studies[names[si_ + 1]] if si_ + 1 < len(names) else None
            ctx.keep_hold = bool(nxt is not None and _continues_hold(study, nxt, ctx))
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
            try:
                state["stage_at_end"] = _stage_now(ctx) if ctx is not None else {}
            except Exception:  # noqa: BLE001 - the bridge may be gone
                state["stage_at_end"] = {}
            state["returned"] = _not_returned(exc.by, state["stage_at_end"])
            for name in studies:
                res = folder / name / f"{name}.json"
                if not res.exists():
                    (folder / name).mkdir(exist_ok=True)
                    write_json(res, {"ok": False, "why": f"not run: {exc.why}", "reliable": False,
                                     "unreliable_because": [exc.why], "warnings": []})
        except Exception:  # noqa: BLE001 - a fault while stopping must not hide the stop or skip the release below
            state["trace"] = traceback.format_exc()[-3000:]
    except Exception as exc:  # noqa: BLE001
        state.update(state="failed", why=f"ls6t failed (its own fault, not the bridge's or LabSpec's): "
                                         f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-3000:],
                     stopped_by="ls6t", finished=time.time())
        for name in studies:
            res = folder / name / f"{name}.json"
            if not res.exists():
                (folder / name).mkdir(exist_ok=True)
                write_json(res, {"ok": False, "why": f"runner failed: {type(exc).__name__}: {exc}",
                                 "reliable": False, "unreliable_because": ["runner failed"], "warnings": []})
    finally:
        if ctx is not None:
            # on ANY end (a runner failure too) the shutter hold is released: wait for a silent bridge, else leave
            # 'safe' queued for it (2026-11-25: the runner died with the hold on; laser_off by hand closed it)
            hb = ctx.bridge.heartbeat() or {}
            held = bool(getattr(ctx, "holding", False) or str(hb.get("shutter_held", "0")) == "1")
            try:
                from . import runner as _R
                if ctx.bridge.alive() or (held and ctx.bridge.wait_alive(_R.RELEASE_WAIT_S, on_wait=ctx.beat)):
                    # a long blocking call (the hold's opening wait, an acquisition) may still be on: wait it out
                    r = ctx.bridge.call("safe", timeout_s=120 if held else 30, **motor_args(prof))
                    if held:
                        state["shutter_hold_released"] = bool(r.get("ok"))
                    ctx.bridge.call("message", timeout_s=10,
                                    text=f"ls6t: request {folder.name} {state.get('state')}")
                elif held:
                    ctx.bridge.queue("safe", **motor_args(prof))
                    state["shutter_hold_released"] = "queued: 'safe' runs as soon as the bridge is free"
            except (BridgeError, OSError) as exc:
                if held:
                    try:
                        ctx.bridge.queue("safe", **motor_args(prof))
                        state["shutter_hold_released"] = f"queued after {type(exc).__name__}"
                    except OSError:
                        state["shutter_hold_released"] = False
        if state.get("stopped_by") == "ls6t" and state.get("shutter_hold_released") is True:
            state["why"] += "; the bridge answered and ls6t released the shutter hold: the laser shutter is closed"
        if profile_problems := (prof.get("problems") if isinstance(prof, dict) else None):
            state["profile_problems"] = profile_problems
        if ctx is not None and "shutter_hold_released" not in state and any(
                h.get("by") == "after the bridge answered again" for h in ctx.hold_log):
            state["shutter_hold_released"] = True
        if ctx is not None:
            ctx.finished = True
        _release_lock(folder)
        state["seconds"] = round(time.time() - started, 1)
        write_json(folder / "state.json", state)
        if sim is not None and req.get("stop_sim", True):
            sim.stop()
    return state


def _f(v):
    return to_float(v, finite=True)


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
    rs_ = labspec_restart_note()
    if rs_ and rs_ not in warnings:
        warnings.append(rs_)
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
            if ctx.holding:
                # a laser is selected again (maybe through another one): never with the shutter held open
                # (2026-11-24: the 2400 retry reselected the laser while the 637.75's hold went on)
                ctx.holding = False
                rr = ctx.call("shutter_hold", timeout_s=30, state="off")
                ctx.hold_log.append({"released": bool(rr.get("ok")), "by": "laser reselect (grating retry)"})
            readback[-1]["retried"] = True          # superseded by the retry below: checks.setup goes by that one
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
    checks["setup"] = {"pass": all(r["ok"] or r.get("retried") for r in readback), "moves": readback}
    rng = plan.acq_range(study, prof, laser)
    if rng.get("from_nm") is None:
        raise Stop("the laser line is unknown: give setup.laser_nm", "study")
    if kind not in ("alignment", "guide", "calibration"):
        from .studies import readiness_warnings
        readiness_warnings(prof, laser, warnings)

    # ---- the operator's go-ahead
    policy = config.confirm_policy()
    sf_ = prof.get("safety") or {}
    # an interlocked enclosure (the laser off whenever a door is open): the go-ahead before the laser fires is
    # optional (safety.laser_go_ahead false, with safety.interlocked_enclosure true); every other question stays
    skip_go = bool(sf_.get("interlocked_enclosure")) and sf_.get("laser_go_ahead") is False
    if skip_go and confirm:
        checks["go_ahead"] = ("not asked: the enclosure is interlocked (profile safety.interlocked_enclosure, "
                              "safety.laser_go_ahead false)"
                              + (f"; its shutter reads '{after.get('safety_shutter')}'"
                                 if after.get("safety_shutter") not in (None, "") else ""))
    if confirm and not skip_go and (policy == "study" or (policy == "request" and not ctx.confirmed)):
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


RELEASE_WAIT_S = 90.0     # how long ls6t waits for a silent bridge to answer again before giving up on the release
NO_HOLD_KINDS = ("move", "background", "cutoff")      # no laser, or the laser off on purpose
# the bridge commands that keep the shutter held (bridge_main.vbs HOLD_VERBS)
HOLD_VERBS = ("ping", "status", "acquire", "map_native", "guide", "move_xy", "move", "view", "remove", "save",
              "labels", "last_error", "message", "shutter_hold", "paint", "cursors")      # video and others release it


def _study_laser(study: dict, ctx: Ctx):
    las = (study.get("setup") or {}).get("laser_nm")
    if las is None:
        las = (ctx.start or {}).get("laser_nm")
    return _f(las)


def _continues_hold(study: dict, nxt: dict, ctx: Ctx) -> bool:
    """The next study holds the shutter too, with the same laser: the hold is kept between them."""
    a, b = _study_laser(study, ctx), _study_laser(nxt, ctx)
    if a is None or b is None or abs(a - b) > 0.5:
        return False
    return all(s_.get("kind", "spectrum") not in NO_HOLD_KINDS and plan.hold_shutter(s_, ctx.prof, las_)
               for s_, las_ in ((study, a), (nxt, b)))


def run_any(name: str, study: dict, ctx: Ctx) -> dict:
    """Runs one study. With acquire.shutter 'hold' (or the laser's hold_shutter) the laser shutter is held open
    around every acquisition of the study, whatever its kind (2026-11-23: a 'filters' study with 637.75 nm was not
    held and read its second filter 0.54 x low); Ctx.call holds it before an acquisition and notes when the bridge
    lets it go; released here at the end."""
    kind = study.get("kind", "spectrum")
    ctx.live_band = None                     # map.live_band: what LabSpec took, this study's own
    ctx.laser_health = None                  # a laser check (process.laser_check, calibration.precheck)
    laser = (study.get("setup") or {}).get("laser_nm")
    if laser is None:
        laser = (ctx.start or {}).get("laser_nm")
    ctx.hold_wanted = kind not in NO_HOLD_KINDS and plan.hold_shutter(study, ctx.prof, laser)
    ctx.study_kind = kind
    ctx.hold_log = []
    if ctx.hold_wanted:
        try:
            ctx.hold_max_s = round(plan.estimate(study, ctx.prof)["seconds"]["total"] * 1.5 + 300)
        except (KeyError, ValueError, TypeError):
            ctx.hold_max_s = 3600
        # the bridge lets the hold go by itself after this long without a command (a lost release, ls6t gone);
        # a series waits its interval between commands
        ctx.hold_idle_s = 60 + float((study.get("series") or {}).get("interval_s", 0) or 0)
        if getattr(ctx, "keep_hold", False):
            # kept into the next study: no command comes while this study's spectra are analysed
            ctx.hold_idle_s += 120
    if ctx.holding:
        if ctx.hold_wanted:
            # held from the previous study (same laser): this study's own time limit for it
            r_ = ctx.call("shutter_hold", timeout_s=30, state="on", max_s=ctx.hold_max_s, idle_s=ctx.hold_idle_s,
                          **motor_args(ctx.prof))
            if str(r_.get("renewed", "1")) == "1" or not r_.get("ok"):
                ctx.hold_log.append({"continued": True, "from_previous_study": True, "ok": bool(r_.get("ok"))})
            else:
                # the bridge had let it go in between (no command for idle_s while the previous study was analysed,
                # say): the shutter closed and opened again
                why = r_.get("previous_release") or "unknown"
                ctx.hold_log.append({"held": True, "reopened": True, "previous_release": why,
                                     "shutter_before": r_.get("old_index"), "mode_before": r_.get("old_mode"),
                                     "label_while_held": r_.get("hold_label")})

        else:
            ctx.holding = False
            ctx.call("shutter_hold", timeout_s=30, state="off")
    try:
        res = _run_kind(name, study, ctx)
    finally:
        ctx.hold_wanted = False
        if ctx.holding and getattr(ctx, "keep_hold", False):
            ctx.hold_log.append({"kept_for_next_study": True})
        elif ctx.holding:
            ctx.holding = False
            try:
                r = ctx.call("shutter_hold", timeout_s=30, state="off")
                ctx.hold_log.append({"released": bool(r.get("ok"))})
            except Stop as exc:
                ctx.hold_log.append({"released": False, "why": exc.why})
    if isinstance(res, dict) and ctx.hold_log:
        holds = [h for h in ctx.hold_log if "held" in h]
        for h in ctx.hold_log:
            if h.get("reopened"):
                res.setdefault("warnings", []).append(
                    f"the shutter hold from the previous study had been released by the bridge "
                    f"({h.get('previous_release')}): the shutter closed and opened again before this study (a mode "
                    "hop is possible: see the laser line)")
        res["shutter_hold"] = {"held": bool(holds) or any(h.get("continued") for h in ctx.hold_log),
                               "times_held": len(holds),
                               "continued_from_previous_study": any(h.get("continued") for h in ctx.hold_log),
                               "kept_for_next_study": any(h.get("kept_for_next_study") for h in ctx.hold_log),
                               "released": ("kept for the next study (same laser)"
                                            if any(h.get("kept_for_next_study") for h in ctx.hold_log) else
                                            all(h.get("released", True) for h in ctx.hold_log if "released" in h)),
                               "shutter_before": holds[0].get("shutter_before") if holds else None,
                               "mode_before": holds[0].get("mode_before") if holds else None,
                               "label_while_held": holds[0].get("label_while_held") if holds else None,
                               "open_wait_s": holds[0].get("open_wait_s", 0.0) if holds else None,
                               **({"after_native_map": next(h["after_native_map"] for h in ctx.hold_log
                                                            if "after_native_map" in h)}
                                  if any("after_native_map" in h for h in ctx.hold_log) else {}),
                               **({"reopened_after_bridge_release": True}
                                  if any(h.get("reopened") for h in ctx.hold_log) else {}),
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
    from .studies import (run_alignment, run_background, run_calibration, run_cutoff, run_filters,
                          run_focus, run_guide, run_move, run_spot, run_study)
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


def _live_view(ctx: Ctx, view, name: str, kind: str, study: dict, rng: dict, data_id, p: dict, vval, out: Path,
               warnings: list):
    """One spectrum into LabSpec's live view (MapEx for an X/Y map, a profile for a series or a depth profile).
    Returns the view's state: None (not made yet), its name, or False (not available)."""
    zonly = kind == "map" and plan.map_shape(study)[:2] == (1, 1)     # a depth profile
    try:
        nx_, ny_, nz_ = plan.map_shape(study) if kind == "map" else (1, 1, 1)
        grid = kind == "map" and not zonly and nz_ == 1
        vfrom, vto = rng.get("from_nm"), rng.get("to_nm")
        if kind == "map" and not grid and (not vfrom or not vto):
            xs_ = spectra.read(out)["x"]
            vfrom, vto = float(np.min(xs_)), float(np.max(xs_))
        gx = {}
        if grid:
            m_ = study.get("map", {})
            gx = dict(axis_x=",".join(f"{v:g}" for v in (plan.axis_values(m_["x_um"]) if m_.get("x_um") else [0.0])),
                      axis_y=",".join(f"{v:g}" for v in (plan.axis_values(m_["y_um"]) if m_.get("y_um") else [0.0])),
                      index=int(p.get("iy", 0)) * nx_ + int(p.get("ix", 0)))
        if kind == "map" and not grid and not zonly:
            return False         # LabSpec's Map() returned E_FAIL whatever its range (2026-11-24)
        if view is None:
            bp = plan.band_params(study, ctx.optics[0] if ctx.optics else None) if grid else {}
            vr = ctx.call("view", timeout_s=30, action="create", name=name, vfrom=vfrom, vto=vto,
                          kind="mapex" if grid else "profile", id=data_id, x=p["x_um"], y=p["y_um"], value=vval,
                          label="time" if kind == "series" else ("Z" if zonly else "XY"),
                          unit="s" if kind == "series" else "um", **gx, **bp)
            if not vr["ok"]:
                warnings.append(f"live view in LabSpec not available: {vr.get('error')}")
                return False
            _band_said(ctx, vr, "band", warnings)
            return vr.get("view")
        if view:
            vr = ctx.call("view", timeout_s=30, action="add", name=name, id=data_id, x=p["x_um"], y=p["y_um"],
                          value=vval, vfrom=vfrom, vto=vto, **gx)
            if not vr["ok"]:
                warnings.append(f"live view: {vr.get('error')}")
                return False
        return view
    except Stop:
        raise


def _band_said(ctx: Ctx, r: dict, tag: str, warnings: list) -> None:
    """What LabSpec took for the live map's band (map.live_band): kept for the result; a warning if it did not."""
    if r.get(f"{tag}_set") in (None, ""):
        return
    info = {k[len(tag) + 1:]: r[k] for k in r if str(k).startswith(tag + "_")}
    for k_ in ("set", "on"):
        if info.get(k_) not in (None, ""):
            info[k_] = int(float(info[k_]))
    did = r.get("mapex_return") if r.get("mapex_return") not in (None, "") else r.get("id")
    if did not in (None, "") and info.get("set") == 1:
        info["data_id"] = int(float(did))
    # the red cursor's activation as LabSpec reads it back (index 0: 1 cursor, +2 baseline), on the data it was set
    # on (on: 0 = LabSpec's default before its own map) and, for a native map, on the map it made (map_active)
    for src_, dst_ in (("active_after", "red"), ("map_active_after", "red_on_the_map")):
        v_ = str(info.get(src_) or "").split(";")[0]
        if v_ not in ("",):
            try:
                n_ = int(float(v_))
                info[dst_] = {"cursor": bool(n_ & 1), "baseline": bool(n_ & 2)}
            except ValueError:
                pass
    ctx.live_band = info
    asked_base = any(str(r.get(k_) or "").split(";")[0] == "3"
                     for k_ in (f"{tag}_active_written", f"{tag}_map_active_written"))
    for k_ in ("red", "red_on_the_map"):
        if asked_base and isinstance(info.get(k_), dict) and not info[k_].get("baseline"):
            warnings.append("LabSpec did not take the baseline under the band (ActiveCursors written 3, read back "
                            f"{info.get('active_after') or info.get('map_active_after')}): the live map shows the "
                            "band's plain sum")
            break
    if info.get("set") != 1:
        warnings.append(f"the live map's band (map.live_band) was not set in LabSpec: {r.get(f'{tag}_error') or '?'}")


def _paint_band(ctx: Ctx, sd: Path, warnings: list, per_point: list | None = None, study: dict | None = None,
                laser=None) -> None:
    """The live map's spectrum display exported with its cursors (map.live_band), so where LabSpec put them can be
    checked without the screen (Nicola, 2026-11-25): live_band.wmf, and a .png next to it where Pillow can draw
    WMF (Windows)."""
    lb = getattr(ctx, "live_band", None)
    if not lb or lb.get("set") != 1:
        return
    band_ = plan.live_band(study) if study else None
    if band_ and laser:
        # ls6t's own picture: LabSpec's Paint keeps its stored view (T9.14: zoom sent, picture 100-2250 / 0-5800)
        try:
            from . import render
            curves = []
            for rec in (per_point or [])[:400]:
                sp = rec.get("_sp") or spectra.read(rec["file"])
                sh, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
                curves.append((sh, sp["y"]))
            pic = render.band_png(sd / "live_band_ls6t.png", curves, band_,
                                  f"{band_.get('name') or 'band'}: {band_['from_cm1']:g}-{band_['to_cm1']:g} cm-1")
            lb["picture_ls6t"] = pic["path"]
        except Exception as exc:  # noqa: BLE001 - only a picture
            lb["picture_ls6t_error"] = f"{type(exc).__name__}: {exc}"
    if lb.get("data_id") in (None, ""):
        return
    path = sd / "live_band.wmf"
    # zoomed on the band +-200 cm-1, the intensity scaled to the spectra there (T9.11: x 100-2250 and y 0-5000 for
    # counts of about 200: the Si peak was invisible)
    scale = {}
    band = plan.live_band(study) if study else None
    if band and laser:
        lo, hi = band["from_cm1"] - 200.0, band["to_cm1"] + 200.0
        ymin, ymax = None, None
        for rec in (per_point or [])[:400]:
            try:
                sp = rec.get("_sp") or spectra.read(rec["file"])        # a native map's points: in memory
                sh, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
                sel = (np.asarray(sh) >= lo) & (np.asarray(sh) <= hi)
                if sel.any():
                    yy = np.asarray(sp["y"])[sel]
                    ymin = float(yy.min()) if ymin is None else min(ymin, float(yy.min()))
                    ymax = float(yy.max()) if ymax is None else max(ymax, float(yy.max()))
            except Exception:  # noqa: BLE001 - only the picture's scale
                continue
        if ymin is not None and ymax is not None and ymax > ymin:
            pad = 0.05 * (ymax - ymin)
            scale = {"x_from_cm1": round(lo, 2), "x_to_cm1": round(hi, 2), "y_from": round(ymin - pad, 2),
                     "y_to": round(ymax + pad, 2), "laser_nm": float(laser)}
    try:
        r = ctx.call("paint", timeout_s=30, id=lb["data_id"], path=str(path), flags=10, **scale)
    except Stop as exc:
        if exc.by in ("cancel", "operator"):
            raise
        warnings.append(f"the live map's display with its cursors was not exported: {exc.why}")
        return
    if not r.get("ok"):
        lb["picture_error"] = r.get("error")
        return
    lb["picture"] = str(path)
    if r.get("scale_set"):
        lb["picture_scale"] = r["scale_set"]
    elif r.get("scale_error"):
        lb["picture_scale_error"] = r["scale_error"]
    try:
        from PIL import Image as _Img
        png = path.with_suffix(".png")
        with _Img.open(path) as im:
            try:
                im.load(dpi=200)              # a WMF drawn larger (Pillow on Windows)
            except TypeError:
                im.load()
            im.save(png)
        lb["picture_png"] = str(png)
    except Exception as exc:  # noqa: BLE001 - WMF is drawn by Pillow on Windows only
        lb["picture_png_error"] = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------------------------- stage and focus

def _check_abs(prof: dict, axis: str, value: float, what: str):
    lim = prof.get("stage", {}).get(f"{axis}_um")
    if lim and not lim[0] <= value <= lim[1]:
        raise Stop(f"{what}: stage {axis.upper()} = {value:g} µm is outside the profile's stage limits {lim}", "study")


LABSPEC_SESSION = "labspec_session.json"


def labspec_restart_note() -> str | None:
    """LabSpec started again since ls6t last saw it (its start time from Windows): Z then reads from a new zero
    (Nicola, 2026-11-27: Z read 0 after a restart with the stage still at the focus), so Z positions kept from before
    may no longer hold. Said until a focus is found in the new session."""
    try:
        from .bridge import session_context
        started = session_context().get("labspec_started")
    except Exception:  # noqa: BLE001
        started = None
    if not started:
        return None
    d = store.read(LABSPEC_SESSION) or {}
    if d.get("started") != started:
        prev = d.get("started")
        if prev is None:
            # first seen by this version: the start before it from the bridge starts' log (Nicola, 2026-11-27: the
            # restart at 16:34 went unsaid, the 15:18 start being in warmup_log.jsonl only)
            try:
                for ln in reversed((config.home() / "warmup_log.jsonl").read_text(encoding="utf-8").splitlines()):
                    ls_ = (json.loads(ln) if ln.strip() else {}).get("labspec_started")
                    if ls_ and ls_ != started:
                        prev = ls_
                        break
            except (OSError, ValueError):
                pass
        d = {"started": started, "previous": prev, "focus_found": prev is None}
        store.write(LABSPEC_SESSION, d)
    if d.get("focus_found"):
        return None
    return (f"LabSpec was restarted ({started}; before: {d.get('previous')}): its Z reads from a new zero, so Z "
            "positions kept from before (the focus, spot and clearance offsets) may no longer hold; find the focus "
            "again before relying on them")


def _focus_found_this_session() -> None:
    try:
        d = store.read(LABSPEC_SESSION) or {}
        if d.get("started") and not d.get("focus_found"):
            d["focus_found"] = True
            store.write(LABSPEC_SESSION, d)
    except (OSError, ValueError):
        pass


def axial_shape(zs, v, zb) -> dict | None:
    """The focus profile's shape (Nicola, 2026-11-27, hole 100: 951 -> 338 between +1.0 and +1.5 um, then up again to
    334 at +2.5): the half widths at half maximum on each side of the best Z, and a second local maximum (over 20 % of
    the best, beyond the half width). None with too few points."""
    zs = np.asarray(zs, float)
    v = np.asarray(v, float)
    if zs.size < 5 or v.max() <= 0:
        return None
    o_ = np.argsort(zs)
    zs, v = zs[o_], v[o_]
    i0 = int(np.argmax(v))
    half = 0.5 * v[i0]

    def cross(rng_):
        for j in rng_:
            k_ = j - 1 if rng_.step == 1 else j + 1
            if v[j] < half <= v[k_]:
                return zs[k_] + (zs[j] - zs[k_]) * (v[k_] - half) / (v[k_] - v[j])
        return None
    lo = cross(range(i0 - 1, -1, -1))
    hi = cross(range(i0 + 1, zs.size, 1))
    out: dict = {}
    if lo is not None and hi is not None:
        a_, b_ = float(zb - lo), float(hi - zb)
        out.update(half_width_below_um=round(a_, 2), half_width_above_um=round(b_, 2))
        if min(a_, b_) > 0 and max(a_, b_) / min(a_, b_) > 1.6:
            out["lopsided"] = True
    hw_ = max(out.get("half_width_below_um") or 0.5, out.get("half_width_above_um") or 0.5)
    for j in range(1, zs.size - 1):
        if j != i0 and v[j] >= v[j - 1] and v[j] >= v[j + 1] and v[j] > 0.2 * v[i0] and abs(zs[j] - zb) > hw_:
            # a real dip between: under 80 % of this maximum somewhere between it and the best
            between = v[min(j, i0):max(j, i0) + 1]
            if between.min() < 0.8 * v[j]:
                out["second_maximum"] = {"z_um": round(float(zs[j] - zb), 2), "fraction": round(float(v[j] / v[i0]), 3)}
                break
    return out or None


def _focus_here(ctx: Ctx) -> None:
    """The focus was just found here: offsets toward the sample are measured from this spot's focus again."""
    _focus_found_this_session()
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


# --------------------------------------------------------------------------------------------- guided adjustment

def study_envelope(kind: str, study: dict, ctx: Ctx, t_start: float, *, ok: bool = True,
                   complete: bool = True, reliable: bool = True, unreliable: list | None = None,
                   warnings: list | None = None, checks: dict | None = None,
                   instrument: dict | None = None, setup: dict | None = None,
                   extra: dict | None = None) -> dict:
    """Common result envelope shared across study kinds."""
    res: dict = {
        "ok": ok,
        "complete": complete,
        "reliable": reliable,
        "unreliable_because": unreliable if unreliable is not None else [],
        "warnings": warnings if warnings is not None else [],
        "kind": kind,
        "sample": study.get("sample", {}),
        "seconds": {"total": round(time.time() - t_start, 2)},
        "execution": {"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                      "protocol": BRIDGE_PROTOCOL},
    }
    if checks is not None:
        res["checks"] = checks
    if instrument is not None:
        res["instrument"] = instrument
    if setup is not None:
        res["setup"] = setup
    if extra:
        res.update(extra)
    return res


def _finish(sd: Path, name: str, result: dict) -> dict:
    result = clean(result)
    write_json(sd / f"{name}.json", result)
    return result



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
        res["spectro_nm"] = {"asked": a, "got": g, **({"first_got": _f(acq_res.get("spectro_first_nm")), "retried": True}
                                                      if str(acq_res.get("spectro_retried")) == "1" else {})}
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

