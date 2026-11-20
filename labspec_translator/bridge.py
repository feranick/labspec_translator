"""bridge.py — the Python side of the LabSpec bridge: commands as files in a folder the bridge script watches.
Version: 2026.11.20.24

Protocol 1 (the same for ls6t_bridge.vbs inside LabSpec and for sim.py):

  <bridge>/inbox/<seq>.cmd     a command: key=value lines; 'verb' and 'seq' first. Written as .tmp, then renamed.
  <bridge>/outbox/<seq>.res    its result: 'seq', 'ok' (1/0), 'error', then the verb's keys. Same rename.
  <bridge>/heartbeat.txt       rewritten by the bridge about once a second: state=idle|busy|halted, verb, seq,
                               protocol, version. Its age (file time) tells whether the bridge still runs.
  <bridge>/stop.flag           written here to stop the current command (cancel); the bridge removes it.

Values: numbers with a '.' decimal point (the bridge never uses the Windows locale), lists comma-separated,
booleans 1/0, a line end as %0A and '%' as %25. Commands are UTF-16 (what LabSpec's FSO reads in Unicode mode);
results come back UTF-16 from LabSpec, UTF-8 from the simulator. <seq> is a 20-digit number: the bridge runs the
lowest first. stop.flag names the command it stops (seq=...); the bridge drops a flag for any other command.
While the bridge waits for the operator (confirm, resume) it cannot write its heartbeat: that state never counts
as gone.
Spectra never travel in a result: the bridge writes them to the file named by the command's 'out' key.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import config
from .profile import escape, parse_kv, read_text


def labspec_windows() -> dict | None:
    """LabSpec's visible top-level windows on this PC (Windows only; None elsewhere or when they cannot be listed):
    {"running": bool, "dialogs": [titles of its dialog boxes], "windows": [other titles]}. A dialog box of LabSpec's
    (class #32770: message boxes, About boxes...) blocks every LabSpec script while it is open."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        out = {"running": False, "dialogs": [], "windows": []}
        names: dict = {}

        def exe(pid):
            if pid not in names:
                h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
                name = ""
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = os.path.basename(buf.value).lower()
                    k32.CloseHandle(h)
                names[pid] = name
            return names[pid]

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            if not u32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not exe(pid.value).startswith("labspec"):
                return True
            out["running"] = True
            title = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, title, 512)
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            (out["dialogs"] if cls.value == "#32770" else out["windows"]).append(title.value or "(no title)")
            return True

        u32.EnumWindows(each, 0)
        return out
    except Exception:          # noqa: BLE001 - a hint only
        return None


def labspec_hint(verb: str | None = None) -> str:
    """What to tell the operator when the bridge stops answering: LabSpec's open dialog by its title, that LabSpec
    is not running any more, or (no dialog) that the bridge is stuck inside a LabSpec call."""
    w = labspec_windows()
    if w is not None and not w["running"]:
        return ("LabSpec is not running any more (it closed or crashed): restart it, then ls6t_bridge.vbs. If it "
                f"crashed, send {config.home() / 'discovery_progress.txt'} (when a discovery ran) and the request's "
                "runner.log to the ls6t maintainer.")
    if w and w["dialogs"]:
        return ("LabSpec shows a dialog that blocks its scripts: " + "; ".join(f"'{t}'" for t in w["dialogs"]) +
                ". The operator closes it; the bridge then goes on (or restart ls6t_bridge.vbs).")
    if w is not None:
        return (f"LabSpec runs and shows no dialog: the bridge is stuck inside a LabSpec call"
                + (f" (command '{verb}')" if verb else "") + ". The operator stops the bridge with LabSpec's script "
                "stop button and starts ls6t_bridge.vbs again; if LabSpec does not respond, restart LabSpec. Tell "
                "the ls6t maintainer which command it was.")
    return ("LabSpec may be showing a dialog or message box: its scripts wait while one is open, so the operator "
            "closes it; otherwise the operator restarts ls6t_bridge.vbs"
            + (f" (it was running '{verb}')" if verb else ""))


class BridgeError(RuntimeError):
    pass


class BridgeGone(BridgeError):
    """The bridge script is not running in LabSpec (no heartbeat)."""


class BridgeTimeout(BridgeError):
    pass


def encode(params: dict) -> str:
    lines = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "1" if v else "0"
        elif isinstance(v, float):
            v = repr(v)
        elif isinstance(v, (list, tuple)):
            v = ",".join(repr(x) if isinstance(x, float) else str(x) for x in v)
        lines.append(f"{k}={escape(str(v))}")
    return "\n".join(lines) + "\n"


def decode(text: str) -> dict:
    return parse_kv(text)


def _write_atomic(path: Path, text: str):
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(text.encode("utf-16"))      # with a BOM; LabSpec's FSO reads it in Unicode mode
    os.replace(tmp, path)


class Bridge:
    def __init__(self, folder: Path | None = None):
        self.dir = Path(folder) if folder else config.active_bridge_dir()
        self.inbox = self.dir / "inbox"
        self.outbox = self.dir / "outbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ state
    def heartbeat(self) -> dict | None:
        hb = self.dir / "heartbeat.txt"
        try:
            st = hb.stat()
            d = decode(read_text(hb))
        except OSError:
            return None
        d["age_s"] = round(max(0.0, time.time() - st.st_mtime), 2)
        return d

    def alive(self) -> bool:
        hb = self.heartbeat()
        if not hb or hb.get("state") == "stopped":
            return False
        if hb.get("state") == "busy" and hb.get("verb") in ("confirm", "resume", "notice", "guide", "run_vbs"):
            return True             # a modal question in LabSpec, or LabSpec's own script running: no beats
        return hb["age_s"] <= config.heartbeat_s()

    def request_stop(self, seq: str = ""):
        _write_atomic(self.dir / "stop.flag", encode({"seq": seq, "time": time.time()}))

    def clear_stop(self):
        try:
            (self.dir / "stop.flag").unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ calls
    def call(self, verb: str, timeout_s: float = 30.0, should_stop=None, on_wait=None, poll_s: float = 0.1,
             **params) -> dict:
        """Sends one command and waits for its result (a dict with 'ok' as bool). Raises BridgeGone when the
        bridge is not running (before or during the call) and BridgeTimeout after timeout_s; should_stop() true
        asks the bridge to stop the command (its result then says so)."""
        if not self.alive():
            hb = self.heartbeat()
            raise BridgeGone("the LabSpec bridge is not running: start ls6t_bridge.vbs in LabSpec "
                             "(Processing > Scripts)" + (f"; last heartbeat {hb['age_s']:.0f} s ago, state "
                                                         f"{hb.get('state')}" if hb else "; no heartbeat file in "
                                                         f"{self.dir}"))
        seq = f"{time.time_ns():020d}"
        cmd = self.inbox / f"{seq}.cmd"
        res = self.outbox / f"{seq}.res"
        # the bridge gets a slightly shorter limit, so it stops the command itself and answers before we give up
        _write_atomic(cmd, encode({"verb": verb, "seq": seq, "timeout_s": round(0.95 * timeout_s, 1), **params}))
        t0 = time.monotonic()
        stop_sent = None
        while True:
            if res.exists():
                try:
                    d = decode(read_text(res))
                except OSError:
                    time.sleep(poll_s)
                    continue
                try:
                    res.unlink()
                except OSError:
                    pass
                d["ok"] = d.get("ok") == "1"
                d["seconds"] = round(time.monotonic() - t0, 3)
                return d
            now = time.monotonic()
            if on_wait:
                on_wait()
            if should_stop and stop_sent is None and should_stop():
                self.request_stop(seq)
                stop_sent = now
            if stop_sent is not None and now - stop_sent > 30:
                self._withdraw(cmd)
                raise BridgeTimeout(f"{verb}: no answer 30 s after the stop request")
            if now - t0 > timeout_s:
                self.request_stop(seq)
                self._withdraw(cmd)
                raise BridgeTimeout(f"{verb}: no result within {timeout_s:g} s (stop requested)")
            if not self.alive():
                self._withdraw(cmd)
                raise BridgeGone(f"{verb}: the LabSpec bridge stopped answering (heartbeat older than "
                                 f"{config.heartbeat_s():.0f} s). " + labspec_hint(verb))
            time.sleep(poll_s)

    def _withdraw(self, cmd: Path):
        try:
            cmd.unlink()
        except OSError:
            pass
