"""sim.py — a simulated LabSpec + XploRA behind the same bridge protocol as ls6t_bridge.vbs.

It answers every verb of the bridge with the same keys, so the runner, the checks and the MCP server are tested
without the instrument (LS6T_MODE=sim, and the tests). The simulated sample is a silicon wafer with a graphene
flake on the +X side of where the stage starts: Si 520.7 cm-1 everywhere (weaker under the flake), graphene G
(1585 cm-1) and 2D (2680 cm-1) for x >= 0 (offsets from the start). Signal scales with the filter's transmission
and falls off away from focus; spectra have a fluorescence background, a CCD offset unless dark-corrected, shot and
read noise, cosmic rays when spike removal is off, and clipping at the 16-bit full scale.

Test hooks (files in the bridge folder): sim_operator_stop (the operator closes the bridge popup: the bridge halts),
sim_operator_stop_after (containing N: the same once N spectra have been acquired), sim_confirm_no (the operator
answers No), sim_time_scale (seconds per simulated second, default 0.01), sim_misaligned (containing a factor, e.g.
0.4: the signal is that fraction of normal until the operator runs AutoAlignment, i.e. answers OK to a notice about
it), sim_autoalign_fails (AutoAlignment does not help), sim_notice_cancel (the operator cancels a notice),
sim_guide_abort (the operator aborts a guided adjustment), sim_defocus (containing N: the sample's best focus is
N µm from where the operator left Z), sim_spot_offset ('dx,dy': where the beam really is relative to LabSpec's
laser position; default 1.5,-0.8). A dust particle (radius 1.5 µm) sits 15 µm to -X and 10 µm to +Y of the start:
dark on the video, and it blocks the Raman signal where the beam hits it. sim_xshift ('a,b': the spectral scale reads a + b x the true
shift until the operator corrects LabSpec's zero, i.e. answers OK to a notice about the instrument calibration),
sim_lamp (the white light is on: a broad spectrum in every acquisition). sim_film ('T,n'): a polystyrene film
T µm thick with refractive index n covers the Si; +Z focuses deeper (depth profiles).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

from . import __version__, BRIDGE_PROTOCOL, config
from .bridge import decode, encode, replace_retry
from .profile import DEFAULT_PROFILE, filter_pct, read_text

FULL_SCALE = 65535.0
PIXELS = 1024
PEAKS = {"ps": [(1001.4, 3.0, 0.6), (1031.8, 4.0, 0.2), (1602.3, 6.0, 0.3), (3054.3, 10.0, 0.4)],
         "si": [(520.7, 3.5, 1.0)], "graphene": [(1585.0, 14.0, 0.35), (2680.0, 30.0, 0.7), (1350.0, 20.0, 0.04)]}



class SimLabSpec:
    def __init__(self, folder: Path, seed: int = 1):
        self.dir = Path(folder)
        self.inbox = self.dir / "inbox"
        self.outbox = self.dir / "outbox"
        for d in (self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)
        self.rng = np.random.default_rng(seed)
        self.thread: threading.Thread | None = None
        self.hb_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"
        self.verb = ""
        self.seq = ""
        self.halted = False
        self.last_error = ""
        self.live = False
        self.camera = 0
        self.laser_until = 0.0
        self.next_id = 100
        self.views: dict = {}
        self.data: dict = {}
        self.x0, self.y0, self.zf = 1234.5, -567.8, 250.0   # stage at start; the operator's focus
        self.m = {"Laser": {"values": [532.0, 638.0, 785.0], "labels": ["532", "638", "785"], "i": 0},
                  "Grating": {"values": [600.0, 1200.0, 1800.0, 2400.0],
                              "labels": ["600", "1200", "1800", "2400"], "i": 2},
                  "Filter": {"values": [filter_pct(k) for k in DEFAULT_PROFILE["filters"]],
                             "labels": list(DEFAULT_PROFILE["filters"]), "i": 4},
                  "Objective": {"values": [10.0, 50.0, 100.0], "labels": ["x10", "x50", "x100"], "i": 2},
                  "Hole": {"value": 300.0}, "Slit": {"value": 100.0}, "Spectro": {"value": 550.0},
                  "Shutter": {"values": [0.0, 1.0], "labels": ["Off", "On"], "i": 0},
                  "LaserPol": {"values": [0.0, 1.0, 2.0], "labels": ["Circular", "Horizontal", "Vertical"], "i": 1},
                  "RamanPol": {"values": [0.0, 1.0, 2.0], "labels": ["Horizontal", "Vertical", "None"], "i": 2},
                  "IlluminatorV": {"values": [0.0, 1.0], "labels": ["Dark field", "Bright field"], "i": 1},
                  "uLight": {"values": [0.0, 1.0, 2.0], "labels": ["Off", "BrightField", "DarkField1"], "i": 1},
                  "X": {"value": self.x0}, "Y": {"value": self.y0}, "Z": {"value": self.zf}}
        self.illumination_moves = 0
        self.exposure, self.accumulations = 1.0, 1
        self.temp_c = -60.0
        self.laser_offset = (1.5, -0.8)     # where the beam really is, relative to LabSpec's laser position (µm)
        self.particle = (self.x0 - 15.0, self.y0 + 10.0, 1.5)   # a dust particle on the Si: dark, blocks Raman
        # faint specks scattered over the sample (not periodic, so image shifts are unambiguous at any step)
        srng = np.random.default_rng(7)
        n = 1500
        self.specks = np.column_stack([self.x0 + srng.uniform(-600, 600, n), self.y0 + srng.uniform(-600, 600, n),
                                       srng.uniform(0.6, 2.5, n), srng.uniform(15, 35, n)])
        self.laser_blocked = False
        self.moves = 0
        self.acquisitions = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._stop.clear()
        self._write_hb()
        self.thread = threading.Thread(target=self._loop, name="ls6t-sim", daemon=True)
        self.hb_thread = threading.Thread(target=self._hb_loop, name="ls6t-sim-hb", daemon=True)
        self.thread.start()
        self.hb_thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.state = "stopped"
        self._write_hb()
        for t in (self.thread, self.hb_thread):
            if t:
                t.join(timeout=5)

    def _write_hb(self):
        state = "halted" if self.halted and self.state != "stopped" else self.state
        txt = encode({"state": state, "verb": self.verb, "seq": self.seq, "protocol": BRIDGE_PROTOCOL,
                      "pid": os.getpid(),
                      "version": f"sim {__version__}", "system": "XploRA (simulated)"})
        tmp = self.dir / "heartbeat.tmp"
        try:
            tmp.write_text(txt, encoding="utf-8")
            replace_retry(tmp, self.dir / "heartbeat.txt", tries=10)
        except OSError:
            pass

    def _hb_loop(self):
        while not self._stop.wait(0.2):
            self._write_hb()

    def _scale(self) -> float:
        p = self.dir / "sim_time_scale"
        try:
            return float(p.read_text().strip()) if p.exists() else 0.01
        except ValueError:
            return 0.01

    def _loop(self):
        while not self._stop.is_set():
            try:
                cmds = sorted(self.inbox.glob("*.cmd"))
            except OSError:
                cmds = []
            if not cmds:
                time.sleep(0.02)
                continue
            path = cmds[0]
            try:
                params = decode(read_text(path))
            except OSError:
                time.sleep(0.02)
                continue
            for _ in range(50):                     # Windows: the file may be held a moment (scanner, the writer)
                try:
                    path.unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.02)
            else:
                continue
            self.verb, self.seq = params.get("verb", ""), params.get("seq", "")
            self.state = "busy"
            try:
                res = self.handle(params)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": 0, "error": f"{type(exc).__name__}: {exc}"}
            res = {"seq": self.seq, "ok": res.pop("ok", 1), "error": res.pop("error", ""), **res}
            out = self.outbox / f"{self.seq}.res"
            tmp = self.outbox / f"{self.seq}.tmp"
            try:
                tmp.write_text(encode(res), encoding="utf-8")
                replace_retry(tmp, out)
            except OSError:
                pass                                # the thread goes on: the caller times out on this one command
            self.state = "idle"
            self.verb = ""

    # ------------------------------------------------------------------ helpers
    def _stop_check(self) -> str | None:
        after = self.dir / "sim_operator_stop_after"
        if after.exists():
            try:
                n = int(after.read_text().strip())
            except ValueError:
                n = 0
            if self.acquisitions >= n:
                after.unlink()
                self.halted = True
                return "operator"
        if (self.dir / "sim_operator_stop").exists():
            (self.dir / "sim_operator_stop").unlink()
            self.halted = True
            return "operator"
        flag = self.dir / "stop.flag"
        if flag.exists():
            try:
                seq = decode(read_text(flag)).get("seq")
                flag.unlink()
            except OSError:
                return None
            if seq == self.seq:          # a flag for another command is stale: dropped
                return "request"
        return None

    def _wait(self, seconds: float) -> str | None:
        end = time.monotonic() + seconds * self._scale()
        while True:
            who = self._stop_check()
            if who:
                return who
            if time.monotonic() >= end:
                return None
            time.sleep(0.005)

    def _stopped(self, who: str) -> dict:
        return {"ok": 0, "error": "stopped by the operator" if who == "operator" else "stopped (stop request)",
                "stopped_by": who, "halted": 1 if self.halted else 0}

    def _cur(self, name):
        m = self.m[name]
        if "values" in m:
            return m["values"][m["i"]], m["labels"][m["i"]]
        return m["value"], f"{m['value']:g}"

    # ------------------------------------------------------------------ verbs
    def handle(self, p: dict) -> dict:
        verb = p.get("verb")
        if verb not in ("ping", "status", "camera", "labels", "last_error", "message", "laser"):
            self.laser_until = 0.0              # the bridge closes the shutter before any other command
        if self.halted and verb not in ("ping", "status", "resume", "quit", "safe", "message"):
            return {"ok": 0, "error": "halted by the operator: resume with `ls6t instrument resume` (LabSpec then asks the operator to confirm)", "halted": 1}
        fn = getattr(self, "v_" + str(verb), None)
        if not fn:
            return {"ok": 0, "error": f"unknown verb {verb!r}"}
        res = fn(p)
        if str(res.get("ok", 1)) == "0" and res.get("error"):
            self.last_error = str(res["error"])
        return res

    def v_ping(self, p):
        if not getattr(self, "warm", None):         # as the bridge's start: each motor's first status (s)
            self.warm = (f"started={time.strftime('%Y-%m-%dT%H:%M:%S')};grating_at={self._cur('Grating')[0]};"
                         "Grating=0;Filter=0;Laser=0;Hole=0;Slit=0;Spectro=0;")
        return {"protocol": BRIDGE_PROTOCOL, "version": f"sim {__version__}", "system": "XploRA",
                "labspec_version": "6.6 (simulated)", "halted": int(self.halted), "warmup": self.warm,
                "template_test": f"return=1, value=, autoexposure={int((self.dir / 'sim_autoexposure').exists())}"}

    def v_status(self, p):
        las, _ = self._cur("Laser")
        gr, _ = self._cur("Grating")
        _, fil = self._cur("Filter")
        _, obj = self._cur("Objective")
        return {"system": "XploRA", "detector_temp_c": self.temp_c, "laser_nm": las, "grating": gr,
                "filter": fil, "objective": obj, "hole_um": self.m["Hole"]["value"],
                "slit_um": self.m["Slit"]["value"], "x_um": self.m["X"]["value"], "y_um": self.m["Y"]["value"],
                "z_um": self.m["Z"]["value"], "exposure_s": self.exposure, "accumulations": self.accumulations,
                "shutter_mode": 0, "halted": int(self.halted), "last_error": self.last_error,
                "auto_exposure": int((self.dir / "sim_autoexposure").exists()),
                **({"laser_shutter_label": self.m["Shutter"]["labels"][self.m["Shutter"]["i"]]}
                   if p.get("m_laser_shutter") in self.m else {}),
                "live_video": int(self.live), "camera": self.camera,
                **({"laser_pol": self._cur("LaserPol")[1]} if p.get("m_laser_pol") else {}),
                **({"raman_pol": self._cur("RamanPol")[1]} if p.get("m_raman_pol") else {}),
                **({"illumination": self._cur("IlluminatorV")[1]} if p.get("m_illumination") else {}),
                **({"illumination_light": self._cur("uLight")[1]} if p.get("m_illumination_light") else {}), "laser_open": int(time.time() < self.laser_until)}

    def _shutter_to(self, motor: str, want_on: bool, p: dict) -> str:
        """The bridge's ShutterTo: by label (default) or by value; returns the label read back. sim_shutter_value_swap:
        value 0 is 'On' on this simulated instrument (a move by value then fails the read-back)."""
        m = self.m[motor]
        if p.get("shutter_by", "label") == "value":
            i = (1 if want_on else 0)
            if (self.dir / "sim_shutter_value_swap").exists():
                i = 1 - i
        else:
            lab = p.get("shutter_on" if want_on else "shutter_off", "On" if want_on else "Off")
            i = m["labels"].index(lab) if lab in m["labels"] else m["i"]
        m["i"] = i
        return m["labels"][i]

    def v_shutter_hold(self, p):
        """The bridge's shutter_hold: the shutter held open across a study's spectra (ShutterMode manual)."""
        if p.get("state") == "off":
            was = getattr(self, "held", False)
            self.held = False
            return {"was_held": int(was)}
        motor = p.get("m_laser_shutter", "")
        if motor not in self.m:
            return {"ok": 0, "error": "shutter_hold needs the laser shutter's motor (m_laser_shutter)"}
        old = self.m[motor]["i"]
        lab = self._shutter_to(motor, True, p)
        self.held = True
        self.hold_count = getattr(self, "hold_count", 0) + 1
        return {"held": 1, "old_mode": 0, "old_index": old, "hold_label": lab}

    def v_shutter(self, p):
        motor = p.get("motor", "")
        if motor not in self.m:
            return {"ok": 0, "error": f"motor {motor} is not present"}
        m = self.m[motor]
        out = {"mode_before": 0, "label_before": m["labels"][m["i"]], "value_before": m["values"][m["i"]]}
        if p.get("action") == "set":
            lab = self._shutter_to(motor, p.get("state") == "on", p)
            want = p.get("shutter_on" if p.get("state") == "on" else "shutter_off", "On" if p.get("state") == "on" else "Off")
            out.update(after_label=lab, after_value=m["values"][m["i"]], matches=int(lab == want))
        out.update(mode_after=int(p.get("mode", 0)) if p.get("action") == "mode" else 0,
                   label_after=m["labels"][m["i"]], value_after=m["values"][m["i"]])
        return out

    def v_laser(self, p):
        state = p.get("state")
        if state == "off":
            was = time.time() < self.laser_until
            self.laser_until = 0.0
            return {"was_open": int(was), "open": 0}
        if state != "on":
            return {"ok": 0, "error": "laser state: on or off"}
        if p.get("m_laser_shutter", "") not in self.m or float(p.get("seconds") or 0) <= 0:
            return {"ok": 0, "error": "laser on needs m_laser_shutter and seconds"}
        self.laser_until = time.time() + float(p["seconds"])
        return {"open": 1, "seconds": p["seconds"]}

    def v_autocal(self, p):
        """LabSpec's AutoCalibration called directly: in the simulator it corrects the spectral zero."""
        if str(p.get("mode", "0")) not in ("0", "2"):
            return {"ok": 0, "error": "autocal: mode 0 or 2"}
        try:
            (self.dir / "sim_xshift").unlink()
        except FileNotFoundError:
            pass
        self.autocal_runs = getattr(self, "autocal_runs", 0) + 1
        return {"code": 0, "status": "AutoCalibration done (simulated)", "seconds": "30"}

    def v_run_vbs(self, p):
        """LabSpec's AutoCalibration: in the simulator it corrects the spectral zero (removes sim_xshift)."""
        if not str(p.get("path", "")).lower().endswith(".vbs"):
            return {"ok": 0, "error": "run_vbs: only a .vbs inside LabSpec's folder"}
        if (self.dir / "sim_autocal_fails").exists():
            return {"ok": 0, "error": "running the script: Name redefined (simulated)"}
        try:
            (self.dir / "sim_xshift").unlink()
        except FileNotFoundError:
            pass
        self.autocal_runs = getattr(self, "autocal_runs", 0) + 1
        return {"seconds": "42", "path": p["path"]}

    def v_calib_params(self, p):
        """LabSpec's spectrometer calibration per grating (OperVar ACT_INSTRUMENT[...]): made-up, XploRA-like."""
        out = {"gratings": len(self.m["Grating"]["values"]), "grating_index": self.m["Grating"]["i"]}
        for i, _ in enumerate(self.m["Grating"]["values"]):
            xs = self._xshift()                         # the operator's correction moves the zero (BF here)
            out.update({f"g{i}.AF": 1.0475e-05, f"g{i}.BF": 62486.0 + 150000 * i + (round(xs[0] * 10) if xs else 0),
                        f"g{i}.Diode": 0,
                        f"g{i}.Focal": 198.8, f"g{i}.Angle": 12.2, f"g{i}.Gamma": 8.0, f"g{i}.SpMin": 0,
                        f"g{i}.SpMax": 1100})
        return out

    def v_labels(self, p):
        m = self.m.get(p.get("motor"))
        if m is None:
            return {"ok": 0, "error": f"motor {p.get('motor')} is not present"}
        if "values" not in m:
            return {"size": 0}
        out = {"size": len(m["values"]), "current": m["labels"][m["i"]]}
        for i, (v, lab) in enumerate(zip(m["values"], m["labels"])):
            out[f"label.{i}"], out[f"value.{i}"] = lab, v
        return out

    def v_camera(self, p):
        """As the bridge: the video stopped only HORIBA's way when allowed (stop_video); cameras switched only when
        allowed (switch_camera), by a restart (stop, select, start) checked once the video runs; sim_camera_locked:
        LabSpec ignores the selection (camera 0 on the XploRA, TESTING Q44)."""
        action = p.get("action", "status")
        if action not in ("on", "off", "status"):
            return {"ok": 0, "error": "camera action: on, off or status"}
        by_hand = ("end the bridge from ls6t first (instrument end_bridge: LabSpec ends a running script when its "
                   "video is stopped or started in its interface), then in LabSpec stop the video, choose the camera "
                   "and start the video, then start ls6t_bridge.vbs again")
        can_stop = str(p.get("stop_video", "0")) == "1"
        if action == "off" and self.live:
            if not can_stop:
                return {"ok": 0, "live": 1, "camera": self.camera,
                        "error": "ls6t does not stop LabSpec's live video (a script's Video(STOP_VIDEO) hung the "
                                 "bridge on this instrument): end the bridge from ls6t first (instrument end_bridge), "
                                 "then stop the video in LabSpec"}
            self.live = False
            return {"live": 0, "video_id": 0, "camera": self.camera, "video_stop_status": 0, "video_started": 0}
        out = {}
        if p.get("camera") not in (None, "") and action == "on":
            want = int(float(p["camera"]))
            if want != self.camera:
                out["camera_before"] = self.camera
                if str(p.get("switch_camera", "0")) != "1":
                    return {"ok": 0, "live": int(self.live), **out, "camera": self.camera,
                            "error": f"LabSpec's camera is {self.camera}, not {want}, and ls6t does not switch cameras "
                                     f"on this instrument unless the profile allows it (video.switch_camera). To use "
                                     f"camera {want}: " + by_hand}
                if self.live:
                    if not can_stop:
                        return {"ok": 0, "live": 1, **out, "camera": self.camera,
                                "error": f"the live video runs on camera {self.camera}: LabSpec switches cameras only "
                                         f"when the video starts, and ls6t may not stop it (video.stop_from_script). "
                                         f"To use camera {want}: " + by_hand}
                    self.live = False
                    out.update(camera_restart=1, video_stop_status=0)
                out.update(camera_asked=want, camera_set_returned=0)
                if not (self.dir / "sim_camera_locked").exists():
                    self.camera = want
                self.live = True
                out["camera_after"] = self.camera
                if self.camera != want:
                    return {"ok": 0, "live": 1, **out, "camera": self.camera,
                            "error": f"LabSpec ignored the switch: asked camera {want}, the video restarted on camera "
                                     f"{self.camera}. To use camera {want}: " + by_hand}
                return {"live": 1, "video_id": 900, "camera": self.camera, "video_started": 1, **out}
        started = action == "on" and not self.live
        if action == "on":
            self.live = True
        return {"live": int(self.live), "video_id": 900 if self.live else 0, "camera": self.camera,
                "video_started": int(started), **out}

    def v_last_error(self, p):
        return {"info": self.last_error}

    def v_config(self, p):
        """LabSpec's SaveConfig / LoadConfig: the simulator keeps its motors, exposure and accumulations in an ini."""
        action, path = p.get("action"), p.get("path")
        if not path:
            return {"ok": 0, "error": "config needs 'path'"}
        f = Path(path)
        if action == "save":
            f.parent.mkdir(parents=True, exist_ok=True)
            lines = ["[ls6t simulated LabSpec configuration]", f"exposure={self.exposure}",
                     f"accumulations={self.accumulations}"]
            for name, m in self.m.items():
                lines.append(f"{name}={m['i'] if 'values' in m else m['value']}")
            f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elif action == "load":
            if not f.exists():
                return {"ok": 0, "error": f"LoadConfig: {path} not found"}
            for line in f.read_text(encoding="utf-8").splitlines():
                k, _, v = line.partition("=")
                if k == "exposure":
                    self.exposure = float(v)
                elif k == "accumulations":
                    self.accumulations = int(float(v))
                elif k in self.m and k not in ("X", "Y", "Z"):      # LabSpec's configuration holds no stage position
                    if "values" in self.m[k]:
                        self.m[k]["i"] = int(float(v))
                    else:
                        self.m[k]["value"] = float(v)
        else:
            return {"ok": 0, "error": "config action: save or load"}
        return {"returned": 0, "path": str(f), "exists": int(f.exists())}

    def v_discover(self, p):
        lines = [f"time={time.strftime('%Y-%m-%dT%H:%M:%S')}", "system=XploRA", "labspec_version=6.6 (simulated)",
                 "labspec_path=C:\\Program Files\\HORIBA Scientific\\LabSpec 6", f"detector_temp_c={self.temp_c}"]
        for name, m in self.m.items():
            lines.append(f"motor.{name}.present=1")
            if "values" in m:
                lines.append(f"motor.{name}.size={len(m['values'])}")
                for i, (v, lab) in enumerate(zip(m["values"], m["labels"])):
                    lines += [f"motor.{name}.value.{i}={v}", f"motor.{name}.label.{i}={lab}"]
                lines += [f"motor.{name}.current={m['i']}", f"motor.{name}.current_label={m['labels'][m['i']]}"]
            else:
                lines += [f"motor.{name}.size=0", f"motor.{name}.current={m['value']}"]
        # LabSpec's spectrometer range per grating (SpMin / SpMax, nm), as the XploRA reported them (2026-11-20)
        sp = {600.0: 1138, 1200.0: 1112, 1800.0: 935, 2400.0: 690}
        for i, g in enumerate(self.m["Grating"]["values"]):
            if float(g) in sp:
                lines += [f"grating.{i}.SpMin=0", f"grating.{i}.SpMax={sp[float(g)]}"]
        lines += ["selftest.pause=1", "selftest.last_error_info=1", "selftest.status_bar=1"]
        # the bridge's step log (common.vbs DiscStart/DStep): a step begun and never ended is skipped next time
        prog = config.home() / "discovery_progress.txt"
        prog.parent.mkdir(parents=True, exist_ok=True)
        steps = ["values", "detector"] + [f"motor.{n}" for n in self.m] + ["spectro_range", "selftest.pause", "video",
                                                                            "selftest.last_error_info",
                                                                            "selftest.status_bar"]
        cf = self.dir / "sim_discover_crash"           # a test: LabSpec "crashes" in the step this file names
        crash = cf.read_text(encoding="utf-8").strip() if cf.exists() else None
        skip_f = prog.with_name("discovery_skip.txt")
        last = None
        for ln in (read_text(prog).splitlines() if prog.exists() else []):
            last = ln[6:] if ln.startswith("begin ") else (None if ln.startswith("end ") or ln == "finished" else last)
        if last:
            with open(skip_f, "a", encoding="utf-8") as fh:
                fh.write(last + "\n")
        skip = {s.strip() for s in read_text(skip_f).splitlines()} if skip_f.exists() else set()
        log = [f"start {time.strftime('%Y-%m-%d %H:%M:%S')} simulated"]
        skipped = []
        for s in steps:
            if s in skip:
                skipped.append(s)
                continue
            log.append(f"begin {s}")
            if s == crash:
                prog.write_text("\n".join(log) + "\n", encoding="utf-8")
                return {"ok": 0, "error": f"simulated crash in {s}"}
            log.append(f"end {s}")
        prog.write_text("\n".join(log + ["finished"]) + "\n", encoding="utf-8")
        if skipped:
            lines.append("discovery.skipped=" + ",".join(skipped))
        out = Path(p["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"out": str(out), "motors": len(self.m)}

    def v_move(self, p):
        name = p.get("motor")
        if str(name).replace(" ", "").lower() == "laserswitch":
            return {"ok": 0, "error": f"ls6t never moves {name} (it switches a laser on or off): do it in LabSpec"}
        if name == "IlluminatorV":
            self.illumination_moves += 1
        if name not in self.m:
            return {"ok": 0, "error": f"motor {name!r} not present"}
        m = self.m[name]
        mode = p.get("mode", "value")
        val = p.get("value", "")
        dur = 15.0 if name in ("Laser", "Grating") else 2.0
        if "values" in m:
            if mode == "label":
                if val not in m["labels"]:
                    return {"ok": 0, "error": f"{name}: no position named {val!r}"}
                i = m["labels"].index(val)
            elif mode == "index":
                i = int(float(val))
            else:
                diffs = [abs(v - float(val)) for v in m["values"]]
                i = int(np.argmin(diffs))
                if diffs[i] > 0.5:
                    return {"ok": 0, "error": f"{name}: no position at {val}"}
            # sim_laser_locks_grating: after the laser was moved to 785 nm, 2400 ends at 1800 until the laser is
            # selected again (a move to the laser it already is) - LabSpec on 2026-11-23
            if name == "Laser" and (self.dir / "sim_laser_locks_grating").exists():
                if abs(m["values"][i] - 785.0) < 1:
                    self._grating_lock = True
                elif i == m["i"] and (self.dir / "sim_laser_locks_grating").read_text().strip() != "never":
                    self._grating_lock = False
            if name == "Grating" and getattr(self, "_grating_lock", False) and abs(m["values"][i] - 2400) < 0.5:
                i = int(np.argmin([abs(v - 1800.0) for v in m["values"]]))
            stuck = self.dir / "sim_grating_stuck"      # "2400:1800": a move to 2400 ends at 1800, no error
            once = self.dir / "sim_grating_stuck_once"   # the same, for the next such move only
            for f_ in (stuck, once):
                if name == "Grating" and f_.exists():
                    to, at = f_.read_text().strip().split(":")
                    if abs(m["values"][i] - float(to)) < 0.5:
                        i = int(np.argmin([abs(v - float(at)) for v in m["values"]]))
                        if f_ is once:
                            f_.unlink()
            m["i"] = i
        else:
            m["value"] = float(val)
        who = self._wait(dur)
        if who:
            return self._stopped(who)
        self.moves += 1
        v, lab = self._cur(name)
        return {"motor": name, "value": v, "label": lab, "move_call_s": 0.0, "wait_s": 0.0, "all_wait_s": 0.0,
                "settle_s": 0.0, **({"blocking": 1} if str(p.get("blocking", "0")) == "1" else {}),
                **({"no_message": 1} if str(p.get("no_message", "0")) == "1" else {})}                    # the bridge's timings (it waits for all motors, then reads back)

    def v_move_xy(self, p):
        x, y = float(p["x_um"]), float(p["y_um"])
        d = math.hypot(x - self.m["X"]["value"], y - self.m["Y"]["value"])
        who = self._wait(0.3 + d / 2000.0)
        if who:
            return self._stopped(who)
        self.m["X"]["value"], self.m["Y"]["value"] = x, y
        return {"x_um": x, "y_um": y}

    def v_autofocus(self, p):
        who = self._wait(8.0)
        if who:
            return self._stopped(who)
        z0 = self.m["Z"]["value"]
        target = self._zf_true() + float(self.rng.normal(0, 0.2))
        # LabSpec's search sweeps +-af_range around where it starts (sim_af_range, 20 µm by default)
        f = self.dir / "sim_af_range"
        rng_ = float(f.read_text()) if f.exists() else 20.0
        lo, hi = min(z0 - rng_, target), max(z0 + rng_, target)
        zb, sg = p.get("z_bound"), int(float(p.get("z_sign", 0) or 0))
        if zb not in (None, "") and sg:
            far = hi if sg > 0 else lo                  # the search's farthest point toward the sample
            if (far - float(zb)) * sg > 0:
                self.m["Z"]["value"] = float(zb) + sg * 2.0            # it coasts a little past the trip point
                return {"ok": 0, "z_bound_hit": float(zb), "z_um": float(zb), "stop_s": 0.2, "overshoot_um": 2.0,
                        "z_speed_um_s": 50.0, "poll_s": 0.12, "z_seen_min": min(lo, z0),
                        "z_seen_max": max(hi, z0) if sg < 0 else float(zb),
                        "error": f"autofocus stopped: Z passed the safe bound {zb} toward the sample"}
        self.m["Z"]["value"] = target
        return {"z_um": target, "how": p.get("how", "laser"), "z_seen_min": lo, "z_seen_max": hi,
                "z_speed_um_s": 50.0, "poll_s": 0.12}

    def _zf_true(self) -> float:
        """The sample's real focus: the operator's (zf), unless sim_defocus says the operator missed it by N µm."""
        f = self.dir / "sim_defocus"
        try:
            return self.zf + (float(f.read_text().strip()) if f.exists() else 0.0)
        except ValueError:
            return self.zf

    def v_video(self, p):
        """A white-light frame around the stage position (160 x 120 px, field 6400/magnification µm wide): silicon,
        the flake at x >= start; with laser_s > 0 the laser spot, offset from LabSpec's position by laser_offset."""
        out = p.get("out")
        if not out:
            return {"ok": 0, "error": "video needs 'out'"}
        laser_s = float(p.get("laser_s", 0) or 0)
        acq_s = float(p.get("during_acq_s", 0) or 0)
        extended = str(p.get("extended", "0")) == "1"
        if extended and (laser_s > 0 or acq_s > 0):
            return {"ok": 0, "error": "extended video takes no laser (laser_s, during_acq_s)"}
        if laser_s > 0:
            name = p.get("m_laser_shutter", "")
            if name not in self.m:
                return {"ok": 0, "error": "video with the laser needs the laser shutter's motor name"}
        who = self._wait(float(p.get("settle_s", 1.5)) + laser_s + acq_s)
        if who:
            return self._stopped(who)
        mag = self.m["Objective"]["values"][self.m["Objective"]["i"]]
        w_um = 6400.0 / mag * (3 if extended else 1)      # extended: a 3 x 3 mosaic around the position
        W, H = (480, 360) if extended else (160, 120)
        x = self.m["X"]["value"]
        y = self.m["Y"]["value"]
        xs = x + (np.arange(W) - (W - 1) / 2) * w_um / W
        ys = y + (np.arange(H) - (H - 1) / 2) * w_um / W
        X, Y = np.meshgrid(xs, ys)          # rows: Y, columns: X (X varies fastest in the flat data)
        base = 150 + 3 * np.sin(X / 9.7) * np.cos(Y / 12.3) + self.rng.normal(0, 3, X.shape)
        sp = self.specks
        near = sp[(np.abs(sp[:, 0] - x) < w_um) & (np.abs(sp[:, 1] - y) < w_um)]
        for sxk, syk, srk, sck in near:
            base = base - sck * np.exp(-((X - sxk) ** 2 + (Y - syk) ** 2) / (2 * srk ** 2))
        flake = X >= self.x0
        r = np.where(flake, base - 35, base)
        g = np.where(flake, base - 25, base)
        b = np.where(flake, base - 10, base + 8)
        px, py, pr = self.particle
        dust = np.hypot(X - px, Y - py) <= pr
        r, g, b = np.where(dust, r - 90, r), np.where(dust, g - 90, g), np.where(dust, b - 80, b)

        def write(path, r, g, b):
            r, g, b = (np.clip(c, 0, 255).astype(int) for c in (r, g, b))
            packed = (r | (g << 8) | (b << 16)).ravel()          # Windows COLORREF: 0x00BBGGRR
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# kind=video\n# id=900\n# axis_types=X,Y,Intens\n")
                fh.write(f"# axis_sizes={W},{H},0\n# axis_units=mkm,mkm,cnt\n")
                fh.write("# axis_values=" + ",".join(f"{v:.4f}" for v in np.concatenate([xs, ys])) + "\n")
                fh.write(f"# data_dims={W * H}\n")
                for k in range(0, packed.size, 256):
                    fh.write(",".join(str(int(v)) for v in packed[k:k + 256]) + "\n")
            return int(packed.size)

        extra = {}
        if laser_s > 0 or acq_s > 0:
            write(Path(out + ".nolaser.video.txt"), r, g, b)
            extra["out_nolaser"] = out + ".nolaser.video.txt"
            fpct = self.m["Filter"]["values"][self.m["Filter"]["i"]] or 0.0
            las = self.m["Laser"]["values"][self.m["Laser"]["i"]]
            ox, oy = self._offset()
            sx, sy = x + ox, y + oy
            spot = np.exp(-((X - sx) ** 2 + (Y - sy) ** 2) / (2 * (0.6 + 0.02 * w_um) ** 2))
            amp = 255.0 * min(1.0, 0.2 + fpct / 10.0)
            col = {532.0: (0.3, 1.0, 0.3), 638.0: (1.0, 0.25, 0.2)}.get(las, (0.5, 0.5, 0.5))
            r, g, b = r + amp * col[0] * spot, g + amp * col[1] * spot, b + amp * col[2] * spot
        path = Path(out + ".video.txt")
        npix = write(path, r, g, b)
        res = {"id": 900, "out": str(path), "pixels": npix, "laser_s": laser_s or acq_s, **extra,
               "x_um": x, "y_um": y, "objective": self.m["Objective"]["labels"][self.m["Objective"]["i"]],
               **({"illumination": self._cur("IlluminatorV")[1]} if p.get("m_illumination") else {}),
               **({"illumination_light": self._cur("uLight")[1]} if p.get("m_illumination_light") else {})}
        for f in str(p.get("formats", "")).split(","):
            if f:
                res[f"saved_{f}"] = 0           # the simulator writes no LabSpec image files
        if laser_s > 0:
            res["shutter_closed"] = 1
        if extended:
            res.update(extended=1, extended_returned=0, extended_s=round(9 * 0.5, 2))
        return res

    def v_notice(self, p):
        if (self.dir / "sim_notice_cancel").exists():
            return {"answer": "cancel"}
        t_ = str(p.get("text", "")).lower()
        if "unblock the laser" in t_:
            self.laser_blocked = False
        elif "block the laser" in t_ or "physically blocked" in t_:     # the operator blocks the laser by hand
            self.laser_blocked = True
        if "instrument calibration" in str(p.get("text", "")).lower():    # the operator fixes LabSpec's zero
            try:
                (self.dir / "sim_xshift").unlink()
            except FileNotFoundError:
                pass
        if "autoalignment" in str(p.get("text", "")).lower():      # the operator runs AutoAlignment, then OK
            if not (self.dir / "sim_autoalign_fails").exists():
                try:
                    (self.dir / "sim_misaligned").unlink()
                except FileNotFoundError:
                    pass
                self.laser_offset = (0.4, 0.3)       # AutoAlignment moves LabSpec's laser marker
        return {"answer": "ok"}

    def v_guide(self, p):
        """The operator adjusting something by hand while the signal is measured: reach, the signal climbs 25 % of
        its start per step; drop, it falls 10 % per step; maximize, it passes through a best position at step 8.
        Same decisions as the bridge."""
        out = p.get("out")
        if not out:
            return {"ok": 0, "error": "guide needs 'out'"}
        mode = p.get("mode", "reach")
        target = float(p.get("target", 0) or 0)
        ex = float(p.get("exposure_s", 0.2))
        max_s = float(p.get("max_s", 120))
        stable_n = int(float(p.get("stable_n", 2)))
        drop = float(p.get("drop_frac", 0.1))
        sat = float(p.get("sat", 60000))
        v0 = {"reach": 0.3 * target if target > 0 else 100.0, "drop": 3 * target if target > 0 else 1000.0}.get(
            mode, 20000.0)
        hist, n, n_ok, n_drop, maxv, tmax, rose, reason, v = [], 0, 0, 0, -1e300, 0.0, False, "", 0.0
        t_sim = 0.0
        while True:
            if (self.dir / "sim_guide_abort").exists() and n >= 3:
                (self.dir / "sim_guide_abort").unlink()
                reason = "operator_abort"
                break
            if t_sim > max_s:
                reason = "timeout"
                break
            who = self._wait(ex + 0.2)
            if who:
                hist_write(out, hist)
                return {**self._stopped(who), "reason": "stopped", "iterations": n, "out": out}
            t_sim += ex + 0.2
            if mode == "reach":
                v = v0 * (1 + 0.25 * n)
            elif mode == "drop":
                v = v0 * max(0.0, 1 - 0.1 * n)
            else:
                v = v0 * math.exp(-((n - 8) / 4.0) ** 2)
            v *= self._align() * (1 + float(self.rng.normal(0, 0.01)))
            ymax = v * ex + 300.0
            n += 1
            hist.append((round(t_sim, 3), v, ymax))
            if v > maxv:
                maxv, tmax = v, t_sim
            if ymax >= sat:
                reason = "saturation"
            elif mode == "maximize":
                if n >= 3 and v > 0.5 * maxv:
                    rose = True
                n_drop = n_drop + 1 if rose and maxv > 0 and v < (1 - drop) * maxv else 0
                if n_drop >= 2:
                    reason = "past_max"
            else:
                ok = v <= target if mode == "drop" else v >= target
                n_ok = n_ok + 1 if ok else 0
                if n_ok >= stable_n:
                    reason = "target"
            if reason:
                break
        hist_write(out, hist)
        msg = {"target": f"STOP - goal reached ({v:.0f} counts/s)",
               "past_max": f"STOP - you just passed the best ({maxv:.0f}): go back slightly",
               "saturation": "STOP - the detector is near saturation: reduce the power",
               "timeout": "Time is up - stop adjusting"}.get(reason, "")
        res = {"reason": reason, "value": v, "iterations": n, "out": out, "message": msg}
        if n:
            res.update(max_value=maxv, t_max_s=tmax, period_s=round(t_sim / n, 3))
        return res

    def v_map_native(self, p):
        """LabSpec's mapping engine: the grid of the axes given (absolute stage positions), X fastest; one window at
        center_nm; written like the bridge's dump (spectra contiguous), each spectrum measured at its position."""
        out = p.get("out")
        if not out:
            return {"ok": 0, "error": "map_native needs 'out'"}
        ex = float(p.get("exposure_s", 1.0))
        acc = max(1, int(float(p.get("accumulations", 1))))
        axes = []
        for ax, motor in (("x", "X"), ("y", "Y"), ("z", "Z")):
            if p.get(f"{ax}_from") not in (None, ""):
                a, b, st = float(p[f"{ax}_from"]), float(p[f"{ax}_to"]), float(p[f"{ax}_step"])
                n = int(math.floor(abs(b - a) / st + 1e-9)) + 1
                sign = 1.0 if b >= a else -1.0
                axes.append((motor, [a + sign * i * st for i in range(n)]))
        if not axes:
            return {"ok": 0, "error": "no mapping axis enabled"}
        c = float(p.get("center_nm") or 0) or float(p.get("from_nm") or 0)
        nm = self._axis_nm(c, c)
        las, _ = self._cur("Laser")
        shape = [len(v) for _, v in axes]
        npts = int(np.prod(shape))
        speed = 0.02 if str(p.get("swift", "0")) == "1" else 0.05
        rows = []
        keep = {m: self.m[m]["value"] for m, _ in axes}
        for k in range(npts):
            rem = k
            for motor, vals in axes:
                self.m[motor]["value"] = vals[rem % len(vals)]
                rem //= len(vals)
            who = self._wait(ex * acc + speed)
            if who:
                for m_, v_ in keep.items():
                    self.m[m_]["value"] = v_
                return {**self._stopped(who), "cancelled": 1}
            xs_ = self._xshift()
            if xs_:
                rep = 1e7 / las - 1e7 / nm
                nm_true = 1e7 / (1e7 / las - (rep - xs_[0]) / xs_[1])
            else:
                nm_true = nm
            rate = self._signal_per_s(nm_true) + self._lamp(nm_true)
            fr = np.mean([self.rng.poisson(np.clip(rate * ex, 0, None)).astype(float) + 300.0
                          + self.rng.normal(0, 4.0, nm.size) for _ in range(acc)], axis=0)
            rows.append(np.minimum(fr, FULL_SCALE))
        self.acquisitions += npts
        data = np.concatenate(rows)
        types = ["Spectr"] + [m for m, _ in axes] + ["Intens"]
        sizes = [nm.size] + shape + [0]
        units = ["nm"] + ["mkm"] * len(axes) + ["cnt"]
        axis_vals = np.concatenate([nm] + [np.array(v) for _, v in axes])
        path = Path(out + ".map.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# kind=map\n# id=950\n# axis_types={','.join(types)}\n# axis_sizes={','.join(map(str, sizes))}\n")
            fh.write(f"# axis_units={','.join(units)}\n")
            fh.write("# axis_values=" + ",".join(f"{v:.5f}" for v in axis_vals) + "\n")
            fh.write(f"# data_dims={data.size}\n")
            for i in range(0, data.size, 256):
                fh.write(",".join(f"{v:.2f}" for v in data[i:i + 256]) + "\n")
        via = "GetValueSimplePart" if int(float(p.get("chunk", 65536) or 0)) > 0 else "GetValue"
        return {"id": 950, "out": str(path), "native_ok": 0, "text_ok": 0, "pixels": int(data.size), "data_via": via,
                "axes": " ".join(m for m, _ in axes), "seconds_map": round(npts * (ex * acc + speed), 2),
                "detector_temp_c": self.temp_c}

    def v_confirm(self, p):
        if (self.dir / "sim_confirm_no").exists():
            return {"answer": "no"}
        return {"answer": "yes"}

    def v_message(self, p):
        return {}

    def v_safe(self, p):
        self.held = False
        return {"shutter": "closed"}

    def v_resume(self, p):
        if not self.halted:
            return {"halted": 0}
        self.halted = False
        return {"answer": "yes", "prompt_shown_after_s": 0.0, "answer_after_s": 0.0}

    def v_quit(self, p):
        threading.Thread(target=self.stop, daemon=True).start()
        return {}

    def v_remove(self, p):
        self.data.pop(int(float(p.get("id", 0))), None)
        return {}

    def v_save(self, p):
        out = Path(p["path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"simulated LabSpec data {p.get('id')}\n", encoding="utf-8")
        return {"path": str(out)}

    def v_view(self, p):
        name = p.get("name", "view")
        action = p.get("action")
        if action == "create":
            self.views[name] = {"kind": p.get("kind"), "n": 1}
            return {"view": name}
        if name not in self.views:
            return {"ok": 0, "error": f"no view {name!r}"}
        if action == "add":
            self.views[name]["n"] += 1
            return {"view": name, "n": self.views[name]["n"]}
        if action == "save":
            return self.v_save({"path": p["path"], "id": name})
        return {"ok": 0, "error": f"view action {action!r}"}

    # ------------------------------------------------------------------ the spectrum model
    def _axis_nm(self, from_nm: float, to_nm: float) -> np.ndarray:
        gr, _ = self._cur("Grating")
        win = 46800.0 / gr
        disp = win / PIXELS
        if abs(to_nm - from_nm) < 1e-9:
            return from_nm - win / 2 + disp * (np.arange(PIXELS) + 0.5)
        lo, hi = sorted((from_nm, to_nm))
        n = max(PIXELS, int(math.ceil((hi - lo) / disp)))
        return lo + disp * np.arange(n)

    def _signal_per_s(self, nm: np.ndarray) -> np.ndarray:
        las, _ = self._cur("Laser")
        shift = 1e7 / las - 1e7 / nm
        ox, oy = self._offset()
        bx, by = self.m["X"]["value"] + ox, self.m["Y"]["value"] + oy      # where the beam really is
        flake = bx - self.x0 >= 0
        px, py, pr = self.particle
        d = math.hypot(bx - px, by - py)
        cover = 1.0 / (1.0 + math.exp(min(50.0, (d - pr) / 0.15)))     # 1 on the dust, 0 away from it
        fpct = (self.m["Filter"]["values"][self.m["Filter"]["i"]] or 0.0) * \
            self._filter_actual(self.m["Filter"]["labels"][self.m["Filter"]["i"]])
        dz = self.m["Z"]["value"] - self._zf_true()
        mag = self.m["Objective"]["values"][self.m["Objective"]["i"]]
        focus = math.exp(-(dz / (300.0 / mag)) ** 2)        # depth of focus: ~3 µm at x100, 30 µm at x10
        gr, _ = self._cur("Grating")
        res = 1.0 + 1800.0 / gr      # coarser gratings: broader lines
        film = self._film()
        if film:
            # a transparent polymer film (thickness T, index n) on Si; +Z focuses deeper; the focus inside the
            # medium sits n times deeper than the stage displacement and its axial response is n times longer
            t_um, n_idx = film
            zfoc = (self.m["Z"]["value"] - self._zf_true()) * n_idx
            w = (300.0 / mag) * n_idx
            r_film = 0.5 * (math.erf((t_um - zfoc) / w) - math.erf((0.0 - zfoc) / w))
            r_si = 0.5 * (1.0 - math.erf((t_um - zfoc) / w))
            sig = np.zeros_like(nm)
            for kind_, amp in (("ps", r_film), ("si", r_si)):
                for pos, wl, h in PEAKS[kind_]:
                    w2 = math.hypot(wl, res)
                    sig += amp * h * wl / w2 / (1 + 4 * ((shift - pos) / w2) ** 2)
            fluo = 0.03 * r_film * np.exp(-((shift - 1800.0) / 2500.0) ** 2)
            return 40000.0 * (fpct / 100.0) * self._align() * (sig + fluo)
        sig = np.zeros_like(nm)
        for pos, w, h in PEAKS["si"]:
            w2 = math.hypot(w, res)
            sig += (0.25 if flake else 1.0) * h * w / w2 / (1 + 4 * ((shift - pos) / w2) ** 2)
        if flake:
            for pos, w, h in PEAKS["graphene"]:
                w2 = math.hypot(w, res)
                sig += h * w / w2 / (1 + 4 * ((shift - pos) / w2) ** 2)
        fluo = 0.03 * np.exp(-((shift - 1800.0) / 2500.0) ** 2)
        return 40000.0 * (fpct / 100.0) * focus * self._align() * (1.0 - cover) * (sig + fluo)

    def _offset(self) -> tuple:
        """Where the beam is relative to LabSpec's laser position: laser_offset, or sim_spot_offset ('dx,dy')."""
        f = self.dir / "sim_spot_offset"
        try:
            if f.exists():
                a, b = f.read_text().strip().split(",")
                return float(a), float(b)
        except ValueError:
            pass
        return self.laser_offset

    def _film(self):
        """sim_film 'T,n': a transparent film of T µm and refractive index n on the Si (depth profiles)."""
        f = self.dir / "sim_film"
        try:
            if f.exists():
                a, b = f.read_text().strip().split(",")
                return float(a), float(b)
        except ValueError:
            pass
        return None

    def _xshift(self):
        f = self.dir / "sim_xshift"
        try:
            if f.exists():
                a, b = f.read_text().strip().split(",")
                return float(a), float(b)
        except ValueError:
            pass
        return None

    def _lamp(self, nm):
        """sim_lamp: the white light is on (a broad spectrum, counts/s)."""
        if not (self.dir / "sim_lamp").exists():
            return 0.0
        return 900.0 * np.exp(-((nm - 600.0) / 140.0) ** 2) + 150.0 * np.exp(-((nm - 545.0) / 8.0) ** 2)

    def _edge(self, nm):
        """The edge filter: no light below about 45 cm-1 (sim_edge_cm1 sets it), full transmission above ~60."""
        las, _ = self._cur("Laser")
        shift = 1e7 / las - 1e7 / np.asarray(nm, float)
        try:
            e = float((self.dir / "sim_edge_cm1").read_text()) if (self.dir / "sim_edge_cm1").exists() else 45.0
        except ValueError:
            e = 45.0
        return 1.0 / (1.0 + np.exp(-np.clip((shift - e) / 3.0, -50, 50)))

    def _filter_actual(self, label: str) -> float:
        """sim_filter_actual {label: factor}: a filter that transmits factor x its nominal value."""
        f = self.dir / "sim_filter_actual"
        try:
            return float(json.loads(f.read_text()).get(label, 1.0)) if f.exists() else 1.0
        except (ValueError, OSError):
            return 1.0

    def _align(self) -> float:
        f = self.dir / "sim_misaligned"
        try:
            return float(f.read_text().strip()) if f.exists() else 1.0
        except ValueError:
            return 1.0

    def v_acquire(self, p):
        laser_off = str(p.get("laser_off", "0")) == "1"
        if laser_off and p.get("m_laser_shutter", "") not in self.m:
            return {"ok": 0, "error": "laser_off needs the laser shutter's motor name"}
        extra: dict = {}
        laser_off_asked, holds = laser_off, False
        if laser_off:
            # the bridge closes the shutter by label and reads it back; it cancels if LabSpec opens it meanwhile
            sh = p["m_laser_shutter"]
            lab = self._shutter_to(sh, False, p)
            extra.update(shutter_closed_label=lab, shutter_closed_by=p.get("shutter_by", "label"))
            if lab != p.get("shutter_off", "Off"):
                return {"ok": 0, "error": f"the laser shutter {sh} did not read back '{p.get('shutter_off', 'Off')}' "
                                          f"after closing it (read: '{lab}')", **extra}
            if (self.dir / "sim_shutter_reopens").exists():     # LabSpec's acquisition opens it again
                return {"ok": 0, "shutter_opened": 1, **extra,
                        "error": "the laser shutter read back 'On' during the acquisition (LabSpec opened it): "
                                 "cancelled; laser_off 'shutter' does not block the laser on this instrument"}
            extra.update(shutter_during=lab, shutter_label_after=lab)
            # sim_acq_holds: LabSpec's Acq keeps the script until the spectrum is taken (no reads during it)
            holds = (self.dir / "sim_acq_holds").exists()
            if (self.dir / "sim_shutter_ignored").exists():     # it reads Off, but the beam still passes
                laser_off = False
        exp = float(p.get("exposure_s", 1.0))
        # LabSpec's AutoExposure (sim_autoexposure): the bridge switches it off for a fixed exposure, unless it
        # cannot (sim_ae_sticky): then LabSpec uses its own exposure
        ae_on = (self.dir / "sim_autoexposure").exists()
        if exp > 0 and ae_on:
            extra["auto_exposure_before"] = 1
            if (self.dir / "sim_ae_sticky").exists() or str(p.get("ae_off", "0")) != "1":
                extra["auto_exposure_set"] = 1
                exp = exp * 1.5
            else:
                extra.update(auto_exposure_set=0, auto_exposure_after=1)
                ae_on = False
        acc = max(1, int(float(p.get("accumulations", 1))))
        mode = int(float(p.get("mode", 10)))
        spike_mode = (mode % 1000) // 100 * 100
        dark = (mode // 1000000) % 10      # 1: no dark, 2: dark
        las, _ = self._cur("Laser")
        f_nm = float(p.get("from_nm") or 0) or 1e7 / (1e7 / las - 1000.0)
        t_nm = float(p.get("to_nm") or 0) or f_nm
        nm = self._axis_nm(f_nm, t_nm)
        # a miscalibrated spectrometer (sim_xshift 'a,b'): the light LabSpec labels nm comes from elsewhere
        xs_ = self._xshift()
        # sim_shift_after_first: every spectrum after the first since the file appeared lands 8.7 cm-1 low (the
        # XploRA, 2026-11-23: points 1-4 of a request at 512 cm-1, point 0 at 520.3)
        if (self.dir / "sim_shift_after_first").exists():
            if getattr(self, "_shift_armed", False) and not getattr(self, "held", False):
                xs_ = (-8.7, 1.0)
            self._shift_armed = True
        if xs_:
            a_, b_ = xs_
            reported_shift = 1e7 / las - 1e7 / nm
            true_shift = (reported_shift - a_) / b_
            nm_true = 1e7 / (1e7 / las - true_shift)
        else:
            nm_true = nm
        rate = np.zeros_like(nm) if laser_off or self.laser_blocked else self._signal_per_s(nm_true)
        rate = (rate + self._lamp(nm_true)) * self._edge(nm_true)
        if (self.dir / "sim_laser_line").exists() and not laser_off and not self.laser_blocked:
            # the laser line, leaking through the edge filter, where the laser really is
            rate = rate + 3000.0 * np.exp(-0.5 * ((1e7 / las - 1e7 / nm_true) / 2.0) ** 2)
        if exp <= 0:   # auto exposure: reach the target at the peak, within [min, max]
            target = float(p.get("ae_target", 30000))
            peak = float(rate.max()) or 1.0
            exp = min(max(target / peak, float(p.get("ae_min_s", 0.1))), float(p.get("ae_max_s", 10.0)))
        if spike_mode == 400:
            acc_run = acc + 1
        else:
            acc_run = acc
        windows = max(1, int(math.ceil(nm.size / PIXELS)))
        t_w = time.monotonic()
        who = self._wait(windows * acc_run * (exp + 0.5) + (2.0 * windows if windows > 1 else 0))
        if who:
            return {**self._stopped(who), "cancelled": 1}
        offset = 0.0 if dark == 2 else 300.0
        frames = []
        for _ in range(acc_run):
            fr = self.rng.poisson(np.clip(rate * exp, 0, None)).astype(float) + offset \
                + self.rng.normal(0, 4.0, nm.size)
            if spike_mode == 100 and self.rng.random() < 0.5:   # no spike removal: a cosmic ray now and then
                k = int(self.rng.integers(5, nm.size - 5))
                fr[k] += float(self.rng.uniform(2000, 8000))
            frames.append(np.minimum(fr, FULL_SCALE))
        y = np.mean(frames, axis=0)
        self.acquisitions += 1
        self.next_id += 1
        sid = self.next_id
        self.data[sid] = True
        out = Path(p["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"# id={sid}\n# x_unit=nm\n# y_unit=cnt\n")
            fh.write(f"# acq.Exposition={exp:g} s\n# acq.Accumulation={acc}\n# acq.Laser={las:g} nm\n")
            fh.write(f"# acq.AutoExposure={'On' if ae_on else 'Off'}\n")
            fh.write(f"# acq.Grating={self._cur('Grating')[0]:g}\n# acq.Filter={self._cur('Filter')[1]}\n")
            for k in ("sample", "title", "project", "site"):          # LabSpec's Custom data info, as the bridge sets it
                if p.get(k) is not None:
                    fh.write(f"# info.{k.capitalize()}={p[k]}\n")
            for a, b in zip(nm, y):
                fh.write(f"{a:.5f}\t{b:.3f}\n")
        replace_retry(tmp, out)
        if p.get("native"):
            Path(p["native"]).write_text(f"simulated LabSpec spectrum {sid}\n", encoding="utf-8")
        return {"id": sid, "out": str(out), "points": int(nm.size), "x_unit": "nm", "y_unit": "cnt",
                "max_counts": float(y.max()), "exposure_s": float(p.get("exposure_s", 1.0)) or exp, "accumulations": acc,
                **extra,
                "detector_temp_c": self.temp_c, "windows": windows,
                **({"t_acq_call_s": round(time.monotonic() - t_w, 3), "t_until_data_s": 0.02} if laser_off_asked and holds
                   else {"t_acq_call_s": 0.01, "t_until_data_s": max(round(time.monotonic() - t_w, 3), 0.6)}),
                **({"shutter_reads": 1 if holds else 12,
                    "shutter_first_read_s": round(time.monotonic() - t_w, 3) if holds else 0.01}
                   if laser_off_asked else {}),
                **({"spectro_asked_nm": f_nm, "spectro_nm": f_nm} if abs(t_nm - f_nm) < 1e-9 else {})}


def hist_write(out: str, hist: list):
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# t_s,value,ymax\n")
        for t, v, y in hist:
            fh.write(f"{t},{v},{y}\n")


_running: dict = {}
_lock = threading.Lock()


def ensure(folder: Path) -> SimLabSpec | None:
    """Starts a simulator on `folder` in this process unless one is already answering there (fresh heartbeat)."""
    from .bridge import Bridge
    folder = Path(folder)
    with _lock:
        cur = _running.get(str(folder))
        if cur and cur.thread and cur.thread.is_alive():
            return cur
        hb = Bridge(folder).heartbeat()
        # another process's simulator answers there only if that process still runs and its heartbeat is fresh
        if hb and hb.get("age_s", 99) < 2.0 and hb.get("state") not in ("stopped", None) \
                and _pid_alive(hb.get("pid")) and str(hb.get("pid")) != str(os.getpid()):
            return None
        for f in ("stop.flag",):
            try:
                (folder / f).unlink()
            except FileNotFoundError:
                pass
        sim = SimLabSpec(folder).start()
        _running[str(folder)] = sim
        return sim


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259         # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_all():
    with _lock:
        for sim in _running.values():
            sim.stop()
        _running.clear()
