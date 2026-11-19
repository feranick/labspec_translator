"""sim.py — a simulated LabSpec + XploRA behind the same bridge protocol as ls6t_bridge.vbs.
Version: 2026.11.19.1

It answers every verb of the bridge with the same keys, so the runner, the checks and the MCP server are tested
without the instrument (LS6T_MODE=sim, and the tests). The simulated sample is a silicon wafer with a graphene
flake on the +X side of where the stage starts: Si 520.7 cm-1 everywhere (weaker under the flake), graphene G
(1585 cm-1) and 2D (2680 cm-1) for x >= 0 (offsets from the start). Signal scales with the filter's transmission
and falls off away from focus; spectra have a fluorescence background, a CCD offset unless dark-corrected, shot and
read noise, cosmic rays when spike removal is off, and clipping at the 16-bit full scale.

Test hooks (files in the bridge folder): sim_operator_stop (the operator closes the bridge popup: the bridge halts),
sim_operator_stop_after (containing N: the same once N spectra have been acquired), sim_confirm_no (the operator
answers No), sim_time_scale (seconds per simulated second, default 0.01).
"""
from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path

import numpy as np

from . import __version__, BRIDGE_PROTOCOL
from .bridge import decode, encode
from .profile import DEFAULT_PROFILE, filter_pct, read_text

FULL_SCALE = 65535.0
PIXELS = 1024
PEAKS = {"si": [(520.7, 3.5, 1.0)], "graphene": [(1585.0, 14.0, 0.35), (2680.0, 30.0, 0.7), (1350.0, 20.0, 0.04)]}


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
                  "X": {"value": self.x0}, "Y": {"value": self.y0}, "Z": {"value": self.zf}}
        self.exposure, self.accumulations = 1.0, 1
        self.temp_c = -60.0
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
            os.replace(tmp, self.dir / "heartbeat.txt")
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
                path.unlink()
            except OSError:
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
            tmp.write_text(encode(res), encoding="utf-8")
            os.replace(tmp, out)
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
        if self.halted and verb not in ("ping", "status", "resume", "quit", "safe", "message"):
            return {"ok": 0, "error": "halted by the operator: resume in LabSpec", "halted": 1}
        fn = getattr(self, "v_" + str(verb), None)
        if not fn:
            return {"ok": 0, "error": f"unknown verb {verb!r}"}
        return fn(p)

    def v_ping(self, p):
        return {"protocol": BRIDGE_PROTOCOL, "version": f"sim {__version__}", "system": "XploRA",
                "labspec_version": "6.6 (simulated)", "halted": int(self.halted)}

    def v_status(self, p):
        las, _ = self._cur("Laser")
        gr, _ = self._cur("Grating")
        _, fil = self._cur("Filter")
        _, obj = self._cur("Objective")
        return {"system": "XploRA", "detector_temp_c": self.temp_c, "laser_nm": las, "grating": gr,
                "filter": fil, "objective": obj, "hole_um": self.m["Hole"]["value"],
                "slit_um": self.m["Slit"]["value"], "x_um": self.m["X"]["value"], "y_um": self.m["Y"]["value"],
                "z_um": self.m["Z"]["value"], "exposure_s": self.exposure, "accumulations": self.accumulations,
                "shutter_mode": 0, "halted": int(self.halted)}

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
        out = Path(p["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"out": str(out), "motors": len(self.m)}

    def v_move(self, p):
        name = p.get("motor")
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
            m["i"] = i
        else:
            m["value"] = float(val)
        who = self._wait(dur)
        if who:
            return self._stopped(who)
        self.moves += 1
        v, lab = self._cur(name)
        return {"motor": name, "value": v, "label": lab}

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
        self.m["Z"]["value"] = self.zf + float(self.rng.normal(0, 0.2))
        return {"z_um": self.m["Z"]["value"], "how": p.get("how", "laser")}

    def v_confirm(self, p):
        if (self.dir / "sim_confirm_no").exists():
            return {"answer": "no"}
        return {"answer": "yes"}

    def v_message(self, p):
        return {}

    def v_safe(self, p):
        return {"shutter": "closed"}

    def v_resume(self, p):
        self.halted = False
        return {"halted": 0}

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
        dx = self.m["X"]["value"] - self.x0
        flake = dx >= 0
        fpct = self.m["Filter"]["values"][self.m["Filter"]["i"]] or 0.0
        dz = self.m["Z"]["value"] - self.zf
        focus = math.exp(-(dz / 20.0) ** 2)
        gr, _ = self._cur("Grating")
        res = 1.0 + 1800.0 / gr      # coarser gratings: broader lines
        sig = np.zeros_like(nm)
        for pos, w, h in PEAKS["si"]:
            w2 = math.hypot(w, res)
            sig += (0.25 if flake else 1.0) * h * w / w2 / (1 + 4 * ((shift - pos) / w2) ** 2)
        if flake:
            for pos, w, h in PEAKS["graphene"]:
                w2 = math.hypot(w, res)
                sig += h * w / w2 / (1 + 4 * ((shift - pos) / w2) ** 2)
        fluo = 0.03 * np.exp(-((shift - 1800.0) / 2500.0) ** 2)
        return 40000.0 * (fpct / 100.0) * focus * (sig + fluo)

    def v_acquire(self, p):
        exp = float(p.get("exposure_s", 1.0))
        acc = max(1, int(float(p.get("accumulations", 1))))
        mode = int(float(p.get("mode", 10)))
        spike_mode = (mode % 1000) // 100 * 100
        dark = (mode // 1000000) % 10      # 1: no dark, 2: dark
        las, _ = self._cur("Laser")
        f_nm = float(p.get("from_nm") or 0) or 1e7 / (1e7 / las - 1000.0)
        t_nm = float(p.get("to_nm") or 0) or f_nm
        nm = self._axis_nm(f_nm, t_nm)
        rate = self._signal_per_s(nm)
        if exp <= 0:   # auto exposure: reach the target at the peak, within [min, max]
            target = float(p.get("ae_target", 30000))
            peak = float(rate.max()) or 1.0
            exp = min(max(target / peak, float(p.get("ae_min_s", 0.1))), float(p.get("ae_max_s", 10.0)))
        if spike_mode == 400:
            acc_run = acc + 1
        else:
            acc_run = acc
        windows = max(1, int(math.ceil(nm.size / PIXELS)))
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
            fh.write(f"# acq.Grating={self._cur('Grating')[0]:g}\n# acq.Filter={self._cur('Filter')[1]}\n")
            for a, b in zip(nm, y):
                fh.write(f"{a:.5f}\t{b:.3f}\n")
        os.replace(tmp, out)
        if p.get("native"):
            Path(p["native"]).write_text(f"simulated LabSpec spectrum {sid}\n", encoding="utf-8")
        return {"id": sid, "out": str(out), "points": int(nm.size), "x_unit": "nm", "y_unit": "cnt",
                "max_counts": float(y.max()), "exposure_s": exp, "accumulations": acc,
                "detector_temp_c": self.temp_c, "windows": windows}


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
