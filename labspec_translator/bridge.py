"""bridge.py — the Python side of the LabSpec bridge: commands as files in a folder the bridge script watches.
Version: 2026.11.23.1

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
        out = {"running": False, "dialogs": [], "windows": [], "started": None, "all": []}
        names: dict = {}

        def exe(pid):
            if pid not in names:
                h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
                name, started = "", None
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = os.path.basename(buf.value).lower()
                    c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
                    if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                        ft = (c.dwHighDateTime << 32) | c.dwLowDateTime        # 100 ns since 1601
                        started = ft / 1e7 - 11644473600.0
                    k32.CloseHandle(h)
                names[pid] = name
                if name.startswith("labspec") and started:
                    out["started"] = started                     # LabSpec's process start (Unix time)
            return names[pid]

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            if not u32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = exe(pid.value)
            # LabSpec itself, and its helper processes (OLServer32, OLLabServer, OLExtern32), which may own its windows
            if not (name.startswith("labspec") or name.startswith("ol")):
                return True
            title = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, title, 512)
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            out["all"].append({"title": title.value or "(no title)", "class": cls.value, "exe": name,
                               "hwnd": int(hwnd or 0)})
            if not name.startswith("labspec"):
                return True
            out["running"] = True
            (out["dialogs"] if cls.value == "#32770" else out["windows"]).append(title.value or "(no title)")
            return True

        u32.EnumWindows(each, 0)
        return out
    except Exception:          # noqa: BLE001 - a hint only
        return None


STUCK_ADVICE = ("the bridge cannot react to anything while it waits inside LabSpec, not even the ls6t window's "
                "Cancel (on 2026-11-20 LabSpec froze after a Cancel in this state). Wait a minute; if it stays "
                "stuck, save what you can, close LabSpec (Task Manager if it does not respond), start LabSpec again, "
                "then ls6t_bridge.vbs. Tell the ls6t maintainer the command it was in.")


def session_context() -> dict:
    """When LabSpec started and how long Windows has been up (Windows only): the context of a bridge start, for the
    slow first grating status (36, 51 s after bridge restarts in a running session; 0 s after a fresh login)."""
    out: dict = {}
    w = labspec_windows()
    if w and w.get("started"):
        out["labspec_started"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w["started"]))
        out["labspec_running_min"] = round((time.time() - w["started"]) / 60, 1)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            out["windows_up_min"] = round(ctypes.windll.kernel32.GetTickCount64() / 60000, 1)
        except Exception:  # noqa: BLE001
            pass
    return out


def labspec_hint(verb: str | None = None) -> str:
    """What to tell the operator when the bridge does not answer while busy: LabSpec's open dialog by its title,
    that LabSpec is not running any more, or (no dialog) that the bridge is stuck inside a LabSpec call."""
    w = labspec_windows()
    what = f"'{verb}'" if verb else "a command"
    if w is not None and not w["running"]:
        return ("LabSpec is not running any more (it closed or crashed): start it again, then ls6t_bridge.vbs. If it "
                f"crashed, send {config.home() / 'discovery_progress.txt'} (when a discovery ran) and the request's "
                "runner.log to the ls6t maintainer.")
    if w and w["dialogs"]:
        return ("LabSpec shows a dialog that blocks its scripts: " + "; ".join(f"'{t}'" for t in w["dialogs"]) +
                ". The operator closes it; the bridge then goes on.")
    if w is not None:
        return f"LabSpec runs and shows no dialog: the bridge is stuck inside LabSpec, in {what}: " + STUCK_ADVICE
    return (f"the bridge is in {what} and LabSpec does not let it go on: if LabSpec shows a dialog or message box, "
            "the operator closes it (scripts wait while one is open); if there is none, " + STUCK_ADVICE)


def diagnose(hb: dict | None, folder=None) -> str:
    """One explanation of a bridge that does not answer, from its heartbeat, the same whether a call was about to be
    sent or was waiting: never started, ended normally, ended by LabSpec (last heartbeat idle), or stuck in a
    command (last heartbeat busy)."""
    if not hb:
        return (f"the LabSpec bridge has not run here (no heartbeat in {folder}): start ls6t_bridge.vbs in LabSpec "
                "(Processing > Scripts)")
    age = f"{hb.get('age_s', 0):.0f} s ago"
    state, verb = hb.get("state"), hb.get("verb") or None
    if state == "stopped":
        return f"the LabSpec bridge was stopped ({age}): start ls6t_bridge.vbs in LabSpec (Processing > Scripts)"
    if state == "busy" and hb.get("blocking"):
        return (f"LabSpec's blocking move of the {hb['blocking']} has not returned for {age[:-4]} (longer than "
                f"{config.blocking_move_s():.0f} s; such a move usually takes seconds). "
                + labspec_hint(f"move {hb['blocking']}"))
    if state == "busy":
        return (f"the LabSpec bridge stopped answering {age}, busy in {verb or 'its start-up inventory'}. "
                + labspec_hint(verb or "its start-up inventory"))
    return (f"the LabSpec bridge ended without stopping (last heartbeat {state} {age}): LabSpec ended the script, as "
            "it does when the live video is started or stopped in LabSpec's interface. Start ls6t_bridge.vbs again "
            "(Processing > Scripts); if it then does not answer, start LabSpec again first.")


STARTUP_VERBS = ("inventory", "warmup")      # what the bridge does after it starts, before taking commands
STARTUP_MAX_S = 600.0                          # the longest a command waits for that (on top of its own time)


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


def replace_retry(src, dst, tries: int = 100, wait_s: float = 0.02):
    """os.replace, tried again while Windows refuses it (PermissionError while another process, e.g. a virus
    scanner or the reader, has one of the files open): a renamed file must not be lost to a moment's lock (the
    simulated bridge's thread died of it in the Windows CI now and then: bench graphene_map)."""
    for k in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(wait_s)


def _write_atomic(path: Path, text: str):
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(text.encode("utf-16"))      # with a BOM; LabSpec's FSO reads it in Unicode mode
    replace_retry(tmp, path)


class Bridge:
    def __init__(self, folder: Path | None = None):
        self.dir = Path(folder) if folder else config.active_bridge_dir()
        self.inbox = self.dir / "inbox"
        self.outbox = self.dir / "outbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ state
    def heartbeat(self) -> dict | None:
        """The bridge's heartbeat with its age. The bridge rewrites the file in place about once a second: a read
        that catches it empty or half written is tried again (up to 5 times, 20 ms apart)."""
        hb = self.dir / "heartbeat.txt"
        d, st = {}, None
        missing = 0
        for _ in range(10):
            try:
                st = hb.stat()
                d = decode(read_text(hb))
            except FileNotFoundError:
                missing += 1
                if missing >= 3:                # really absent (a rename is over within milliseconds)
                    return None
                d = {}
            except OSError:
                # Windows: the bridge is replacing the file this instant (PermissionError): a moment's lock, not a
                # missing bridge (the CI's graphene_map 'ended without stopping, last heartbeat idle 0 s ago')
                d = {}
            if d.get("state") and st is not None:
                break
            time.sleep(0.02)
        if st is None or not d.get("state"):
            return None
        d["age_s"] = round(max(0.0, time.time() - st.st_mtime), 2)
        return d

    def alive(self) -> bool:
        hb = self.heartbeat()
        if not hb or hb.get("state") == "stopped":
            return False
        if hb.get("state") == "busy" and hb.get("verb") in ("confirm", "resume", "notice", "guide", "run_vbs"):
            return True             # a modal question in LabSpec, or LabSpec's own script running: no beats
        if hb.get("state") == "busy" and hb.get("blocking"):
            return hb["age_s"] <= config.blocking_move_s()     # a blocking move (the grating): no beats until it returns
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
            time.sleep(0.3)                     # one more look before calling it gone (a file being rewritten)
            if not self.alive():
                raise BridgeGone(f"{verb}: " + diagnose(self.heartbeat(), self.dir))
        seq = f"{time.time_ns():020d}"
        cmd = self.inbox / f"{seq}.cmd"
        res = self.outbox / f"{seq}.res"
        # the bridge gets a slightly shorter limit, so it stops the command itself and answers before we give up
        _write_atomic(cmd, encode({"verb": verb, "seq": seq, "timeout_s": round(0.95 * timeout_s, 1), **params}))
        t0 = time.monotonic()
        stop_sent = None
        startup_wait = 0.0
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
                if startup_wait:
                    d["waited_for_bridge_start_s"] = round(startup_wait, 1)
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
            if now - t0 > timeout_s + startup_wait:
                hb = self.heartbeat()
                if hb is None and self.alive():
                    time.sleep(poll_s)              # the heartbeat being rewritten this instant: look again
                    continue
                hb = hb or {}
                if hb.get("state") == "busy" and hb.get("verb") in STARTUP_VERBS and startup_wait < STARTUP_MAX_S:
                    # the bridge is still starting (inventory, motor warm-up): the command waits its turn, its own
                    # time counts from when the bridge is free (2026-11-20: status timed out during a 51 s warm-up)
                    startup_wait = (now - t0) - timeout_s + 1.0        # the deadline moves on while it starts
                    time.sleep(poll_s)
                    continue
                self.request_stop(seq)
                self._withdraw(cmd)
                raise BridgeTimeout(self._timeout_text(verb, timeout_s))
            if not self.alive():
                time.sleep(0.3)                 # one more look before calling it gone (a file being rewritten)
                if not self.alive() and not res.exists():
                    self._withdraw(cmd)
                    raise BridgeGone(f"{verb}: " + diagnose(self.heartbeat(), self.dir))
            time.sleep(poll_s)

    def _timeout_text(self, verb: str, timeout_s: float) -> str:
        """Why a command gave no result in time, in the same words as diagnose(): stuck inside LabSpec (the heartbeat
        stopped while busy in it) or still working; and what ls6t did (asked the bridge to drop the command)."""
        hb = self.heartbeat() or {}
        dropped = ("ls6t asked the bridge to drop it (a stop request, which the bridge sees only between LabSpec "
                   "calls)")
        if hb.get("state") == "busy" and hb.get("blocking"):
            return (f"{verb}: no result within {timeout_s:g} s; {dropped}. LabSpec is still in a blocking move of "
                    f"the {hb['blocking']} ({hb.get('age_s', 0):.0f} s): the bridge answers when LabSpec returns")
        if hb.get("state") == "busy" and hb.get("verb") == verb and hb.get("age_s", 0) > 5:
            return (f"{verb}: no result within {timeout_s:g} s; {dropped}. The bridge is stuck inside LabSpec in "
                    f"'{verb}' (no heartbeat for {hb['age_s']:.0f} s). " + labspec_hint(verb))
        return (f"{verb}: no result within {timeout_s:g} s; {dropped}. The bridge still works (heartbeat "
                f"{hb.get('age_s', 0):.0f} s ago, {hb.get('state')} in {hb.get('verb') or '-'}): ask again later")

    def _withdraw(self, cmd: Path):
        try:
            cmd.unlink()
        except OSError:
            pass
