"""api.py — the functions behind the command line and the MCP server (JSON in, JSON out).
Version: 2026.11.19.2
"""
from __future__ import annotations

import base64
import json
import os
import platform
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, BRIDGE_PROTOCOL, config, plan, runner
from . import format as F
from . import profile as P
from .bridge import Bridge, BridgeError

VBS_DIR = Path(__file__).parent / "vbs"


# --------------------------------------------------------------------------------------------- status and format

def tool_status() -> dict:
    """Versions, the mode, whether the LabSpec bridge is running, the profile in use and the settings."""
    out = {"translator": __version__, "protocol": BRIDGE_PROTOCOL, "python": platform.python_version(),
           "platform": platform.platform(terse=True)}
    try:
        out["settings"] = config.settings()
    except ValueError as exc:
        out["settings_error"] = str(exc)
        return out
    try:
        import numpy
        import scipy
        out["numpy"], out["scipy"] = numpy.__version__, scipy.__version__
    except ImportError as exc:
        out["missing"] = str(exc)
    try:
        import matplotlib
        out["matplotlib"] = matplotlib.__version__
    except ImportError:
        out["matplotlib"] = None
    b = Bridge()
    hb = b.heartbeat()
    out["bridge"] = {"dir": str(b.dir), "running": b.alive(), "heartbeat": hb}
    if config.mode() == "sim":
        out["bridge"]["note"] = "simulator: started on demand by each run"
    elif not out["bridge"]["running"]:
        out["bridge"]["note"] = ("not running: in LabSpec, Processing > Scripts, start ls6t_bridge.vbs "
                                 "(ls6t bridge install writes it)")
    prof = P.load()
    out["profile"] = {"path": prof.get("path"), "measured": prof.get("measured"), "source": prof.get("source")}
    lock = runner.read_json(runner.lock_path(), None)
    out["instrument_busy"] = lock.get("request") if lock else None
    return out


def study_format() -> dict:
    return F.study_format(status=tool_status())


def validate(studies: dict) -> dict:
    return F.validate(studies)


def inputs(study: dict, name: str = "study") -> dict:
    v = F.validate({name: study})[name]
    if not v["ok"]:
        return {"ok": False, "errors": v["errors"]}
    return {"ok": True, **plan.inputs(study, P.load())}


# --------------------------------------------------------------------------------------------- requests

def _new_folder() -> Path:
    base = config.out_dir()
    base.mkdir(parents=True, exist_ok=True)
    f = base / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
    f.mkdir()
    return f


def _spawn(folder: Path):
    cmd = [sys.executable, "-m", "labspec_translator.cli", "run-request", str(folder)]
    log = open(folder / "runner.log", "ab")
    env = dict(os.environ)
    here = str(Path(__file__).resolve().parents[1])     # the package's parent: also works from a source checkout
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, cwd=str(folder), env=env)
    if os.name == "nt":
        detached, group, breakaway, nowin = 0x00000008, 0x00000200, 0x01000000, 0x08000000
        try:   # outlive the MCP server's job object when Windows allows it
            subprocess.Popen(cmd, creationflags=detached | group | breakaway, **kw)
        except OSError:
            subprocess.Popen(cmd, creationflags=detached | group | nowin, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    log.close()


def start(studies: dict, inline: bool = False) -> dict:
    """Validates, writes the request folder and starts its runner (a detached process, or a thread with
    inline=True). Returns {request, studies, estimate_min} or {ok: false, errors} when a study is invalid."""
    v = F.validate(studies)
    bad = {n: r["errors"] for n, r in v.items() if not r["ok"]}
    if bad:
        return {"ok": False, "errors": bad, "next": "fix every error (validate_studies), then run again"}
    folder = _new_folder()
    req = {"translator": __version__, "mode": config.mode(), "bridge_dir": str(config.active_bridge_dir()),
           "created": time.time(), "studies": list(studies), "estimate_min": {n: v[n]["estimate"]["minutes"]
                                                                              for n in studies},
           "stop_sim": not inline}
    runner.write_json(folder / "request.json", req)
    runner.write_json(folder / "studies.json", studies)
    runner.write_json(folder / "validation.json", v)
    for n in studies:
        (folder / n).mkdir()
        runner.write_json(folder / n / "progress.json", {"state": "queued", "time": time.time()})
    runner.write_json(folder / "state.json", {"state": "queued", "time": time.time()})
    if inline:
        th = threading.Thread(target=runner.run_request, args=(folder,), name="ls6t-runner", daemon=True)
        th.start()
    else:
        _spawn(folder)
    return {"ok": True, "request": str(folder), "studies": list(studies),
            "estimate_min": round(sum(req["estimate_min"].values()), 2)}


def _resolve(request: str) -> Path:
    p = Path(request).expanduser()
    if not p.is_absolute():
        p = config.out_dir() / p
    p = p.resolve()
    base = config.out_dir().resolve()
    if base not in p.parents and p != base:
        raise ValueError(f"{request}: not a request under {base}")
    if not (p / "request.json").exists():
        raise ValueError(f"{request}: not a request folder")
    return p


def _state(folder: Path) -> dict:
    st = runner.read_json(folder / "state.json", {"state": "unknown"})
    if st.get("state") in ("queued", "running"):
        beat = runner.read_json(folder / "runner.json", {})
        created = runner.read_json(folder / "request.json", {}).get("created", 0)
        last = beat.get("time") or created
        if time.time() - last > 120:
            tail = ""
            try:
                tail = (folder / "runner.log").read_text(encoding="utf-8", errors="replace").strip()[-400:]
            except OSError:
                pass
            st = dict(st, state="interrupted",
                      why="the runner stopped without finishing (the computer slept, or its process was ended); "
                          "the studies without a result were not measured: run them again"
                          + (f". runner.log: {tail}" if tail else ""))
            runner.write_json(folder / "state.json", st)
            for n in runner.read_json(folder / "studies.json", {}):
                res = folder / n / f"{n}.json"
                if not res.exists():
                    runner.write_json(res, {"ok": False, "why": st["why"], "reliable": False,
                                            "unreliable_because": ["interrupted"], "warnings": []})
    return st


def status(request: str, wait_s: float = 0) -> dict:
    """A request's state and its studies' results (finished) or progress (pending), after waiting up to wait_s for
    it to finish."""
    folder = _resolve(request)
    end = time.monotonic() + max(0.0, float(wait_s))
    while True:
        st = _state(folder)
        if st.get("state") in runner.FINAL or time.monotonic() >= end:
            break
        time.sleep(0.5)
    names = list(runner.read_json(folder / "studies.json", {}))
    res, prog, pending = {}, {}, []
    for n in names:
        r = runner.read_json(folder / n / f"{n}.json")
        if r is not None:
            res[n] = r
        else:
            pending.append(n)
            prog[n] = runner.read_json(folder / n / "progress.json", {})
    out = {"request": str(folder), "translator": __version__, "state": st.get("state"),
           "done": st.get("state") in runner.FINAL, "pending": pending, "studies": res, "progress": prog}
    for k in ("why", "stopped_by", "reason"):
        if st.get(k):
            out[k] = st[k]
    if pending:
        eta = [p.get("eta_min") or p.get("estimate_min") for p in prog.values()]
        eta = [e for e in eta if e is not None]
        out["next"] = ("call study_status again" + (f"; about {max(eta):.0f} min left for the slowest study"
                                                     if eta else "") +
                       ": when it needs more than about 5 minutes, tell the user when it should finish and stop")
    else:
        out["next"] = "check 'reliable', 'unreliable_because', 'warnings' and 'checks' before using the numbers"
    return out


def run(studies: dict, wait_s: float = 45, inline: bool = False) -> dict:
    s = start(studies, inline=inline)
    if not s.get("ok"):
        return s
    return status(s["request"], wait_s)


def cancel(request: str) -> dict:
    folder = _resolve(request)
    (folder / "cancel").write_text(str(time.time()), encoding="utf-8")
    st = _state(folder)
    return {"request": str(folder), "state": st.get("state"),
            "note": "the runner stops the current command (acquisition or move) and ends the request; "
                    "study_status shows the partial results"}


def jobs(limit: int = 20, active: bool = False) -> dict:
    base = config.out_dir()
    rows = []
    if base.exists():
        for f in sorted(base.glob("run_*"), reverse=True):
            if not (f / "request.json").exists():
                continue
            st = _state(f)
            if active and st.get("state") in runner.FINAL:
                continue
            req = runner.read_json(f / "request.json", {})
            rows.append({"request": str(f), "state": st.get("state"), "studies": req.get("studies"),
                         "mode": req.get("mode"),
                         "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(req.get("created", 0)))})
            if len(rows) >= limit:
                break
    lock = runner.read_json(runner.lock_path(), None)
    return {"requests": rows, "instrument_busy": lock.get("request") if lock else None}


# --------------------------------------------------------------------------------------------- the instrument

def instrument(action: str = "status") -> dict:
    """status: the bridge's ping and the instrument's state. discover: the discovery dump and a proposed profile
    (written next to the profile as profile.proposed.json; a person applies it with `ls6t profile import`).
    resume: asks the operator in LabSpec to lift a halt."""
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    try:
        if action == "status":
            return {"ping": b.call("ping", timeout_s=15), "status": b.call("status", timeout_s=15, **runner.motor_args(P.load())),
                    "bridge": str(b.dir)}
        if action == "discover":
            dump = config.home() / f"discovery_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            dump.parent.mkdir(parents=True, exist_ok=True)
            r = b.call("discover", timeout_s=120, out=str(dump))
            if not r["ok"]:
                return {"ok": False, "error": r.get("error")}
            prof, notes = P.import_discovery(P.read_text(dump))
            prop = config.profile_path().with_name("profile.proposed.json")
            P.save(prof, prop)
            return {"ok": True, "dump": str(dump), "proposed_profile": str(prop), "notes": notes,
                    "next": "a person reviews the proposal and the limits, then runs: ls6t profile import "
                            f"\"{dump}\""}
        if action == "resume":
            return b.call("resume", timeout_s=600)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    raise ValueError(f"action {action!r}: status, discover or resume")


LASER_VERBS = ("acquire", "autofocus")


def bridge_call(verb: str, params: dict, timeout_s: float = 120, allow_laser: bool = False) -> dict:
    """One bridge command, for a maintainer at the instrument (TESTING.md). The profile's motor names are added for
    the verbs that read them. Refused while a request holds the instrument, and for acquire and autofocus unless
    allow_laser (no operator confirmation is asked on this path)."""
    if verb in LASER_VERBS and not allow_laser:
        return {"ok": False, "error": f"{verb} fires the laser: add --laser (and be at the instrument)"}
    lock = runner.read_json(runner.lock_path(), None)
    if lock:
        return {"ok": False, "error": f"the instrument is busy with {lock.get('request')}: wait or cancel it"}
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    p = dict(params)
    if verb in ("status", "move_xy", "autofocus", "safe"):
        p = {**runner.motor_args(P.load()), **p}
    try:
        return b.call(verb, timeout_s=timeout_s, **p)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


def compose_vbs(bridge_dir: Path, home_dir: Path) -> dict:
    """The two LabSpec scripts: a header with this machine's folders, the shared code, then each one's main part."""
    common = (VBS_DIR / "common.vbs").read_text(encoding="utf-8")
    out = {}
    for name, main, what in (("ls6t_bridge.vbs", "bridge_main.vbs", "the ls6t bridge: start it whenever ls6t "
                                                                     "should drive the instrument"),
                             ("ls6t_discover.vbs", "discover_main.vbs", "run once: lists the instrument for "
                                                                         "the ls6t profile")):
        for p in (str(bridge_dir), str(home_dir)):
            if '"' in p:
                raise ValueError(f"{p}: a folder with a double quote cannot be written into a VBScript string")
        header = (f"' {name} - labspec-translator (ls6t) {__version__}: {what}.\n"
                  "' Generated by `ls6t bridge install`; add it in LabSpec 6 under Processing > Scripts.\n"
                  f'Const LS6T_BRIDGE_DIR = "{bridge_dir}"\n'
                  f'Const LS6T_HOME_DIR = "{home_dir}"\n'
                  f'Const LS6T_VERSION = "{__version__}"\n'
                  f"Const LS6T_PROTOCOL = {BRIDGE_PROTOCOL}\n\n")
        body = common + "\n" + (VBS_DIR / main).read_text(encoding="utf-8")
        out[name] = (header + body).replace("@@VERSION@@", __version__)
    return out


def snapshot(with_laser: bool = False) -> dict:
    """A video frame now, with the laser position marked (LabSpec's position plus the profile's measured offset).
    with_laser opens the laser shutter for video.laser_s seconds so the beam shows: only with a laser shutter motor
    in the profile, the current filter within the profile's caps, and the operator's Yes in LabSpec."""
    from . import video
    lock = runner.read_json(runner.lock_path(), None)
    if lock:
        return {"ok": False, "error": f"the instrument is busy with {lock.get('request')}: the request's own "
                                      "snapshots are in its results (study key 'video')"}
    prof = P.load()
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    v = prof.get("video", {})
    try:
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        if with_laser:
            if not prof["motors"].get("laser_shutter"):
                return {"ok": False, "error": "no laser shutter motor in the profile (motors.laser_shutter): a person "
                                              "sets it from the discovery dump"}
            from .profile import filter_pct, laser_key
            pct = prof.get("filters", {}).get(st.get("filter"))
            pct = pct if pct is not None else filter_pct(st.get("filter", ""))
            lk = laser_key(prof, st.get("laser_nm"))
            caps = [prof["limits"]["max_filter_pct"]]
            if lk:
                caps.append(prof["lasers"][lk].get("max_filter_pct", 100))
            if pct is None or pct > min(c for c in caps if c is not None):
                return {"ok": False, "error": f"the current filter {st.get('filter')!r} is unknown or above the "
                                              f"profile's cap ({min(caps):g} %): choose a stronger filter first"}
            if config.confirm_policy() != "never":
                r = b.call("confirm", timeout_s=3600,
                           text=f"ls6t asks to open the laser shutter for {float(v.get('laser_s', 1)):g} s to show "
                                f"the beam on the video (laser {st.get('laser_nm')} nm, filter {st.get('filter')}, "
                                f"objective {st.get('objective')}). Continue?")
                if r.get("answer") != "yes":
                    return {"ok": False, "error": "the operator declined in LabSpec"}
        folder = config.out_dir() / "snapshots" / f"snap_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
        folder.mkdir(parents=True)
        base = folder / "frame"
        laser_s = float(v.get("laser_s", 1.0)) if with_laser else 0.0
        settle = float(v.get("settle_s", 1.5))
        r = b.call("video", timeout_s=60 + settle + laser_s, out=str(base), settle_s=settle, laser_s=laser_s,
                   formats=",".join(v.get("formats", [])), camera=v.get("camera"), **runner.motor_args(prof))
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    if not r["ok"]:
        return {"ok": False, "error": r.get("error"), "stopped_by": r.get("stopped_by")}
    snap = video.snapshot(r, prof, base, f"video {time.strftime('%H:%M:%S')}"
                          + (f", objective {r.get('objective')}" if r.get("objective") else ""))
    out = {"ok": True, "folder": str(folder), **snap,
           "instrument": {k: st.get(k) for k in ("laser_nm", "filter", "objective", "x_um", "y_um", "z_um")}}
    if with_laser and str(r.get("shutter_closed")) != "1":
        out["warnings"].append("the laser shutter could not be confirmed closed: check LabSpec")
    runner.write_json(folder / "snapshot.json", out)
    return out


def install_bridge(dest: Path | None = None) -> dict:
    """Writes ls6t_bridge.vbs and ls6t_discover.vbs, configured with this machine's folders, to LS6T_HOME/vbs."""
    dest = Path(dest) if dest else config.home() / "vbs"
    dest.mkdir(parents=True, exist_ok=True)
    bdir = config.bridge_dir()
    for d in (bdir / "inbox", bdir / "outbox"):
        d.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, text in compose_vbs(bdir, config.home()).items():
        target = dest / name
        # VBScript files are read as ANSI by Windows Script: keep them ASCII, CRLF line ends
        target.write_bytes(text.encode("ascii", errors="replace").replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        out[name] = str(target)
    return {"ok": True, "files": out, "bridge_dir": str(bdir),
            "next": "In LabSpec 6: Processing > Scripts, add ls6t_discover.vbs and run it once; then add "
                    "ls6t_bridge.vbs and start it whenever ls6t should drive the instrument."}


# --------------------------------------------------------------------------------------------- files

def read_file(path: str, max_mb: float = 20) -> dict:
    p = Path(path).expanduser().resolve()
    allowed = [config.out_dir().resolve(), config.home().resolve()]
    if not any(a == p or a in p.parents for a in allowed):
        raise ValueError(f"{path}: only files under {allowed[0]} or {allowed[1]}")
    size = p.stat().st_size
    if size > max_mb * 1e6:
        raise ValueError(f"{path}: {size / 1e6:.1f} MB is above max_mb={max_mb}")
    return {"path": str(p), "bytes": size, "base64": base64.b64encode(p.read_bytes()).decode("ascii")}


def image_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    base = config.out_dir().resolve()       # requests and snapshots
    if base not in p.parents or p.suffix.lower() != ".png" or not p.is_file():
        raise ValueError(f"{path}: not a PNG written by a study (under {base})")
    return p


# --------------------------------------------------------------------------------------------- benchmarks

def bench(names: list | None = None, wait_s: float = 600) -> dict:
    """Runs the benchmarks on the simulator (known answers): see bench.py."""
    from . import bench as B
    return B.run(names, wait_s)


def dumps(o) -> str:
    return json.dumps(o, indent=1, default=str)
