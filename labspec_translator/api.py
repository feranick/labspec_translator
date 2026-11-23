"""api.py — the functions behind the command line and the MCP server (JSON in, JSON out).
Version: 2026.11.22.13
"""
from __future__ import annotations

import base64
import json
import os
import platform
import re
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
    if prof.get("auto"):
        out["profile"]["automatic"] = {"note": "made from the bridge's inventory without a person: on the "
                                               "instrument the laser does not fire (studies, laser_on, snapshots "
                                               "with the laser) until a person reviews it; set-up and stage moves "
                                               "work",
                                       "review": prof["auto"].get("review"), "notes": prof["auto"].get("notes")}
    gate = P.review_gate(prof) if prof.get("measured") else None
    out["profile"]["reviewed"] = gate is None and bool(prof.get("measured"))
    if gate and not prof.get("auto"):
        out["profile"]["review_needed"] = gate
    d = P.drift(prof)
    if d:
        out["profile"]["drift"] = d + ["the profile is a person's: if the new inventory is right, they run ls6t "
                                       "profile propose, check profile.proposed.json and ls6t profile import it"]
    lock = runner.read_json(runner.lock_path(), None)
    out["instrument_busy"] = lock.get("request") if lock else None
    return out


def study_format() -> dict:
    return F.study_format(status=tool_status())


def validate(studies: dict) -> dict:
    return F.validate(studies)


def inputs(study: dict, name: str = "study") -> dict:
    """What a study would send to the bridge, without running it. Nothing is sent, so a profile not yet reviewed is
    a note here, not a refusal (the study itself is refused until the review): this is when it is most useful."""
    prof = P.load()
    v = F.validate({name: study})[name]
    gate = P.review_gate(prof) if prof.get("measured") else None
    errors = [e for e in v["errors"] if not (gate and e == gate)]
    if errors:
        return {"ok": False, "errors": errors}
    current = None
    try:                                    # the instrument's state now: settings already in place are not sent
        b = Bridge()
        if config.mode() == "sim":
            from . import sim as S
            S.ensure(b.dir)
        if b.alive():
            st = b.call("status", timeout_s=15, **runner.motor_args(prof))
            current = st if st.get("ok") else None
    except BridgeError:
        current = None
    out = {"ok": True, **plan.inputs(study, prof, current=current)}
    out["state_read"] = current is not None
    if current is None:
        out["state_note"] = ("the bridge is not running: steps already in place could not be marked (the runner "
                             "skips them at run time)")
    if gate and gate in v["errors"]:
        out["review_note"] = "run_studies refuses this study until the profile is reviewed: " + gate
    return out


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
    for k in ("why", "stopped_by", "reason", "config_restored", "config_restore_error", "returned", "stage_at_end"):
        if st.get(k) is not None:
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
    return {"requests": rows, "instrument_actions": action_log(10),
            "instrument_busy": lock.get("request") if lock else None}


# --------------------------------------------------------------------------------------------- the instrument

QUIET_ACTIONS = ("status", "last_error", "autocal_info")      # reads that do not go to the action log


def log_warmup(ping: dict) -> dict | None:
    """Each bridge session's warm-up (ping 'warmup': each motor's first status, s; the grating's position then), once
    per session, with its context: LabSpec's start, Windows' uptime, the last grating move ls6t made before it.
    Appended to LS6T_HOME/warmup_log.jsonl (TESTING T3.16: slow 36-51 s after bridge restarts in a running session,
    0 s after a fresh login). Returns the session's record."""
    raw = str((ping or {}).get("warmup") or "")
    if not raw:
        return None
    vals = dict(kv.split("=", 1) for kv in raw.strip(";").split(";") if "=" in kv)
    rec = {"bridge_started": vals.pop("started", None), "grating_at": vals.pop("grating_at", None),
           "motors_s": {k: float(v) for k, v in vals.items() if v not in ("", None)},
           "version": (ping or {}).get("version")}
    p = config.home() / "warmup_log.jsonl"
    try:
        seen = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        seen = []
    if any(s.get("bridge_started") == rec["bridge_started"] for s in seen):
        return next(s for s in seen if s.get("bridge_started") == rec["bridge_started"])
    from .bridge import session_context
    rec.update(session_context())
    last = next((a for a in action_log(200) if a.get("action") == "set" and "grating" in str(a.get("setup", ""))),
                None)
    if last:
        rec["last_grating_set"] = last.get("time")
    try:                                    # the last deploy before it (all slow warm-ups followed one, 2026-11-21)
        deps = [json.loads(x) for x in (config.home() / "deploy_log.jsonl").read_text(encoding="utf-8").splitlines()
                if x.strip()]
        if deps:
            rec["last_deploy"] = {k: deps[-1].get(k) for k in ("time", "version", "bridge_was", "ended")}
    except (OSError, ValueError):
        pass
    rec["logged"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


def discovery_progress() -> dict:
    """The last discovery's step log (LS6T_HOME/discovery_progress.txt, written by the bridge step by step): the
    step it was in when it stopped, whether it finished, and the steps skipped because they crashed LabSpec
    before (discovery_skip.txt)."""
    home = config.home()
    out = {"log": str(home / "discovery_progress.txt"), "finished": False, "stopped_in": None, "skip": []}
    try:
        lines = [ln.strip() for ln in P.read_text(home / "discovery_progress.txt").splitlines()]
    except OSError:
        lines = []
    for ln in lines:
        if ln.startswith("begin "):
            out["stopped_in"] = ln[6:]
        elif ln.startswith("end ") or ln == "finished":
            out["stopped_in"] = None
    out["finished"] = "finished" in lines
    try:
        out["skip"] = [s.strip() for s in P.read_text(home / "discovery_skip.txt").splitlines() if s.strip()]
    except OSError:
        pass
    return out


def action_log(limit: int = 10) -> list:
    """The latest instrument actions (discover, set, laser, camera, autocal...), newest first."""
    p = config.home() / "instrument_log.jsonl"
    try:
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    return rows[::-1][:limit]


def instrument(action: str = "status", camera=None, setup: dict | None = None, seconds: float | None = None,
               um: float | None = None) -> dict:
    """status: the bridge's ping and the instrument's state. discover: the discovery dump and a proposed profile
    (written next to the profile as profile.proposed.json; a person reviews it and applies it with `ls6t profile
    import <profile.proposed.json>`). resume: asks the operator in LabSpec to lift a halt. Every action but the
    reads goes to LS6T_HOME/instrument_log.jsonl (shown by jobs)."""
    t0 = time.time()
    r = _instrument(action, camera, setup, seconds, um)
    if action not in QUIET_ACTIONS:
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)), "action": action,
               "ok": bool(r.get("ok", True)) if isinstance(r, dict) else None,
               "seconds": round(time.time() - t0, 1)}
        if isinstance(r, dict) and r.get("error"):
            row["error"] = str(r["error"])[:300]
        if action == "discover" and isinstance(r, dict):
            row.update({k: r[k] for k in ("dump", "stopped_in") if r.get(k)})
        if action == "set" and setup:
            row["setup"] = setup
        try:
            p = config.home() / "instrument_log.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass
    return r


def _instrument(action, camera, setup, seconds, um=None) -> dict:
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    try:
        if action == "status":
            ping = b.call("ping", timeout_s=15)
            out = {"ping": ping, "status": b.call("status", timeout_s=15, **runner.motor_args(P.load())),
                   "bridge": str(b.dir)}
            w = log_warmup(ping)
            if w:
                out["warmup"] = w
            return out
        if action == "discover":
            dump = config.home() / f"discovery_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            dump.parent.mkdir(parents=True, exist_ok=True)
            try:
                r = b.call("discover", timeout_s=120, out=str(dump))
            except BridgeError as exc:
                return _discover_failed(str(exc))
            if not r["ok"]:
                return _discover_failed(r.get("error"))
            out = profile_propose(dump)
            prog = discovery_progress()
            if prog["skip"]:
                out["notes"].append("skipped (they stopped LabSpec before): " + ", ".join(prog["skip"]))
            return {**out, "dump": str(dump)}
        if action == "resume":
            # on Windows, also when LabSpec's question window exists (a dialog of LabSpec's other than ls6t's own):
            # the bridge's timings cannot tell a question put on screen late from a slow answer
            from .bridge import labspec_windows
            # any window of LabSpec's (or its helper processes') that appears during the call and is not ls6t's own:
            # on .46 a #32770-only watch found nothing (the question may be another class, or another process's)
            seen: dict = {}
            t_call = time.monotonic()
            last = [0.0]
            w0 = labspec_windows()
            before = {x["hwnd"] for x in (w0 or {}).get("all", [])}

            def watch():
                now = time.monotonic()
                if "t" in seen or now - last[0] < 0.25:
                    return
                last[0] = now
                w = labspec_windows() or {}
                new = [x for x in w.get("all", []) if x["hwnd"] not in before and not x["title"].startswith("ls6t:")]
                if new:
                    seen.update(t=round(now - t_call, 2), title=new[0]["title"], cls=new[0]["class"],
                                exe=new[0]["exe"])
            r = b.call("resume", timeout_s=600, on_wait=watch)
            if seen:
                r["dialog_appeared_after_s"], r["dialog_title"] = seen["t"], seen["title"]
                r["dialog_class"], r["dialog_process"] = seen["cls"], seen["exe"]
            elif w0 is not None:
                r["dialog_appeared_after_s"] = None      # watched, and no new LabSpec window was seen
            # where the time went: before the bridge took the command up, before the question was on screen, and
            # until the operator answered (.42/.43: 58.8 s and 99.6 s with the bridge idle and halted)
            try:
                shown, ans = float(r["prompt_shown_after_s"]), float(r["answer_after_s"])
                r["prompt_shown_after_s"], r["answer_after_s"] = round(shown, 2), round(ans, 2)   # plain numbers
                r["picked_up_after_s"] = round(max(0.0, float(r["seconds"]) - shown - ans), 2)
            except (KeyError, TypeError, ValueError):
                pass
            return r
        if action == "video_scale":
            return video_scale(b, um)
        if action == "end_bridge":
            # the bridge ends itself cleanly (laser closed, window closed, heartbeat 'stopped'): before the operator
            # uses LabSpec's live video buttons, which end a running script abruptly. A person starts it again.
            if runner.read_json(runner.lock_path(), None):
                return {"ok": False, "error": "a request holds the instrument: cancel it first"}
            r = b.call("quit", timeout_s=30)
            r["next"] = ("the bridge has ended; LabSpec's interface is free (video, camera). Start ls6t_bridge.vbs "
                         "again in LabSpec (Processing > Scripts) when done")
            return r
        if action == "last_error":
            return b.call("last_error", timeout_s=15)
        if action == "set":
            return set_instrument(setup or {})
        if action in ("laser_on", "laser_off"):
            return laser(action == "laser_on", seconds)
        if action == "calibration":
            return labspec_calibration(b)
        if action == "autocal_info":
            return autocal_info()
        if action == "autocal_run":
            return autocal_run()
        if action in ("camera_on", "camera_off"):
            prof = P.load()
            num, err = P.camera_number(prof, camera)
            if err:
                return {"ok": False, "error": err}
            r = b.call("camera", timeout_s=30, action=action[7:],
                       switch_camera=int(bool(prof.get("video", {}).get("switch_camera"))),
                       stop_video=int(bool(prof.get("video", {}).get("stop_from_script"))),
                       **({"camera": num} if num is not None else {}))
            names = {int(v): k for k, v in (prof.get("video", {}).get("cameras") or {}).items()}
            try:
                r["camera_name"] = names.get(int(float(r.get("camera"))))
            except (TypeError, ValueError):
                pass
            return r
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    raise ValueError(f"action {action!r}: status, set, laser_on, laser_off, camera_on, camera_off, calibration, "
                     "autocal_info, autocal_run, discover, resume, end_bridge or last_error")


def profile_propose(dump=None) -> dict:
    """profile.proposed.json from a discovery dump (default: the bridge's inventory, discovery_latest.txt), keeping the
    current profile's limits and per-laser caps and power. A person edits it and imports it (P.REVIEW_STEPS); the
    profile in use does not change."""
    src = Path(dump) if dump else P.latest_dump()
    if src is None or not Path(src).is_file():
        return {"ok": False, "error": "no discovery dump: start ls6t_bridge.vbs in LabSpec once (it writes "
                                      f"{config.home() / 'discovery_latest.txt'}), or give the dump's path"}
    prof, notes = P.import_discovery(P.read_text(src))
    prop = config.profile_path().with_name("profile.proposed.json")
    P.save(prof, prop)
    missing = P.laser_problems(prof)
    return {"ok": True, "from": str(src), "proposed_profile": str(prop), "notes": notes,
            "to_fill_in": missing,
            "next": f"with the maintainer, edit {prop} (" + (", ".join(missing) + "; " if missing else "") +
                    f"the limits; motors.laser_shutter once confirmed), then: ls6t profile import \"{prop}\""}


def _discover_failed(error) -> dict:
    prog = discovery_progress()
    out = {"ok": False, "error": error, "progress": prog}
    if prog["stopped_in"]:
        out["stopped_in"] = prog["stopped_in"]
        out["next"] = (f"LabSpec stopped in discovery step '{prog['stopped_in']}': the next discovery skips it "
                       f"({config.home() / 'discovery_skip.txt'}). Restart LabSpec and the bridge; send "
                       f"{prog['log']} to the ls6t maintainer. The bridge's own inventory "
                       f"({config.home() / 'discovery_latest.txt'}) already gives the profile: discovery is not "
                       "needed to use ls6t.")
    return out


SET_KEYS = ("filter", "laser_nm", "grating", "objective", "hole_um", "slit_um", "laser_pol", "raman_pol")


def autocal_info() -> dict:
    """LabSpec's AutoCalibration scripts on this PC and, from their text, whether the bridge can run them unattended
    (it asks the operator? it shares names with the bridge?). Nothing runs."""
    from . import autocal
    bridge_text = compose_vbs(config.bridge_dir(), config.home())["ls6t_bridge.vbs"]
    return {"ok": True, **autocal.info(P.load(), bridge_text)}


def autocal_run() -> dict:
    """Runs LabSpec's AutoCalibration once from the bridge (profile calibration.autocal enabled by a person; the
    operator says Yes in LabSpec first), with LabSpec's calibration read before and after. For the maintainer's test
    (TESTING T4.9); calibration studies with adjust 'auto' do the same when the calibration is off."""
    prof = P.load()
    ac = prof.get("calibration", {}).get("autocal") or {}
    if not ac.get("enabled") or not ac.get("script"):
        return {"ok": False, "error": "LabSpec's AutoCalibration is not enabled in the profile (calibration.autocal): "
                                      "see instrument autocal_info"}
    if runner.read_json(runner.lock_path(), None):
        return {"ok": False, "error": "a request holds the instrument: wait or cancel it"}
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    try:
        before = labspec_calibration(b)
        r = b.call("confirm", timeout_s=3600,
                   text=f"ls6t asks to run LabSpec's AutoCalibration ({ac['script']})."
                        + (f" Put {ac['sample']} under the objective first." if ac.get("sample") else "") + " Continue?")
        if r.get("answer") != "yes":
            return {"ok": False, "error": "the operator declined in LabSpec"}
        t0 = time.time()
        r = b.call("run_vbs", timeout_s=float(ac.get("timeout_s", 1800)), path=str(ac["script"]))
        after = labspec_calibration(b)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    out = {"ok": bool(r.get("ok")), "error": r.get("error") or None, "seconds": round(time.time() - t0, 1)}
    if before.get("ok") and after.get("ok"):
        out["changed"] = {g: {k: [v, after["gratings"].get(g, {}).get(k)] for k, v in vals.items()
                              if after["gratings"].get(g, {}).get(k) != v}
                          for g, vals in before["gratings"].items()}
        out["changed"] = {g: c for g, c in out["changed"].items() if c}
    return out


def labspec_calibration(b: Bridge | None = None) -> dict:
    """LabSpec's own spectrometer calibration per grating, read only (OperVar ACT_INSTRUMENT[...], the names HORIBA's
    service scripts use): AF, BF, Diode, Focal, Angle, Gamma, SpMin, SpMax; the grating in use is marked. Which of
    them the Maintenance dialog calls zero and coefficient is what calibration studies record (TESTING Q27)."""
    prof = P.load()
    b = b or Bridge()
    r = b.call("calib_params", timeout_s=60, m_grating=prof["motors"]["grating"])
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    labs, _ = _labels(b, prof["motors"]["grating"])
    out, cur = {}, r.get("grating_index")
    for i in range(int(float(r.get("gratings") or 0))):
        g = labs[i] if i < len(labs) else str(i)
        vals = {}
        for k, v in r.items():
            if k.startswith(f"g{i}."):
                try:
                    vals[k[len(f"g{i}."):]] = float(v)
                except (TypeError, ValueError):
                    vals[k[len(f"g{i}."):]] = v
        out[g] = vals
    try:
        cur_label = labs[int(float(cur))] if labs else None
    except (TypeError, ValueError, IndexError):
        cur_label = None
    return {"ok": True, "gratings": out, "grating_in_use": cur_label,
            "note": "read only; names as in HORIBA's service scripts (meanings: TESTING Q27, Q41)"}


def laser(on: bool, seconds: float | None = None) -> dict:
    """The laser on for viewing (the beam on the camera, aligning by eye): the laser shutter open for `seconds`
    (at most limits.max_laser_on_s, default 60 s), then closed by the bridge. Only with the laser shutter motor
    confirmed in the profile, the current filter within the caps, and the operator's Yes in LabSpec each time. The
    bridge also closes it when the operator halts it, and before any other command but quick reads. Off: at once."""
    lock = runner.read_json(runner.lock_path(), None)
    if on and lock:
        return {"ok": False, "error": f"the instrument is busy with {lock.get('request')}: wait or cancel it"}
    prof = P.load()
    shutter = prof["motors"].get("laser_shutter")
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    try:
        if not on:
            return b.call("laser", timeout_s=30, state="off")
        gate = P.review_gate(prof) if config.mode() == "labspec" else None
        if gate:
            return {"ok": False, "error": gate}
        if not shutter:
            return {"ok": False, "error": "no laser shutter motor in the profile: a person confirms it with the "
                                          "maintainer and sets it (ls6t profile set motors.laser_shutter "
                                          "\"<name from discovery>\")"}
        mx = float(prof["limits"].get("max_laser_on_s", 60.0))
        secs = float(seconds or min(30.0, mx))
        if secs <= 0 or secs > mx:
            return {"ok": False, "error": f"seconds: up to limits.max_laser_on_s = {mx:g} s (a person may change it)"}
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        from .profile import filter_pct, laser_key
        pct = prof.get("filters", {}).get(st.get("filter"))
        pct = pct if pct is not None else filter_pct(st.get("filter", ""))
        lk = laser_key(prof, st.get("laser_nm"))
        caps = [float(prof["limits"].get("max_filter_pct", 100))]
        if lk:
            caps.append(float(prof["lasers"][lk].get("max_filter_pct", 100)))
        if pct is None or pct > min(caps) + 1e-9:
            return {"ok": False, "error": f"the current filter {st.get('filter')!r} is unknown or above the cap "
                                          f"({min(caps):g} %): a stronger filter first (instrument set)"}
        r = b.call("confirm", timeout_s=3600,
                   text=f"ls6t asks to open the laser shutter for {secs:g} s to show the beam (laser "
                        f"{st.get('laser_nm')} nm, filter {st.get('filter')}, objective {st.get('objective')}). "
                        "Close the ls6t window to stop it at any time. Continue?")
        if r.get("answer") != "yes":
            return {"ok": False, "error": "the operator declined in LabSpec"}
        r = b.call("laser", timeout_s=30, state="on", seconds=secs, m_laser_shutter=shutter)
        if r.get("ok"):
            r["note"] = (f"the shutter closes by itself after {secs:g} s, or with instrument laser_off, or when the "
                         "operator closes the ls6t window")
        return r
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


def _labels(b: Bridge, motor: str) -> tuple:
    r = b.call("labels", timeout_s=30, motor=motor)
    if not r.get("ok"):
        return [], []
    n = min(int(float(r.get("size") or 0)), 60)
    return [r.get(f"label.{i}", "") for i in range(n)], [r.get(f"value.{i}") for i in range(n)]


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _grating_short(b: Bridge, prof: dict, asked, got, r: dict) -> str:
    """Why a grating move ended elsewhere, with what LabSpec says about it: LabSpec reported no error, the laser in
    use, LabSpec's spectrometer range for the grating asked (OperVar SpMin / SpMax). On 2026-11-20 1200 -> 2400
    stopped at 1800 with the 637.75 nm laser, while 1800 -> 2400 together with a switch to 785 nm worked."""
    msg = f"the grating stopped at {got} (asked {asked:g}); LabSpec reported no error"
    try:
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        msg += f"; laser {st.get('laser_nm')} nm"
        cal = labspec_calibration(b)
        for lab, vals in (cal.get("gratings") or {}).items():
            if P.same_position(str(lab).split()[0], asked):
                msg += f"; LabSpec's range for grating {lab}: SpMin {vals.get('SpMin')}, SpMax {vals.get('SpMax')}"
    except BridgeError:
        pass
    return (msg + ". LabSpec may not allow this grating with this laser (or at the spectrometer's current position): "
            "set the laser and the grating together, or ask the maintainer (TESTING T3.16)")


def _move_timing(r: dict) -> dict:
    """How long a move took: seconds (the whole bridge call), move_call_s (MoveMotor itself), wait_s (until the motor
    reported idle), all_wait_s (until every motor did), settle_s (until the read-back showed the position asked);
    settle_stable / settle_timeout when it never did."""
    out = {"seconds": r.get("seconds")}
    for k in ("move_call_s", "wait_s", "all_wait_s", "settle_s", "settle_stable", "settle_timeout", "blocking"):
        if r.get(k) not in (None, ""):
            out[k] = float(r[k]) if k.endswith("_s") else True
    if r.get("move_id") not in (None, ""):
        out["move_id"] = r["move_id"]
    if str(r.get("no_message", "")) == "1":
        out["labspec_prompt"] = "suppressed (ls6t asked the operator)"
    elif str(r.get("no_message_refused", "")) == "1":
        out["labspec_prompt"] = "LabSpec refused MOTOR_NO_MESSAGE: it may have asked the operator again"
    if r.get("bridge_move_n") not in (None, ""):
        out["bridge_move_n"] = int(float(r["bridge_move_n"]))     # 1: the first move since this bridge started
    return out


SCALE_STEP_FRACTION = 0.2      # video_scale's default move: a fifth of the field (x100 ~17 µm, x10 ~170 µm)


def _sig2(v: float) -> float:
    """v rounded to two significant figures (172.6 -> 170, 17.3 -> 17)."""
    return float(f"{v:.2g}")


def video_scale(b: Bridge, step_um=None) -> dict:
    """The video's µm per pixel and axis directions, measured: a frame, the stage moved step_um in +X (no laser,
    nothing else moves), a frame, back; the same in +Y; the image shift by phase correlation. The default move is a
    fifth of the objective's field (its measured scale, else OLConfig's field: x100 ~17 µm, x10 ~170 µm), so the
    features stay in view; a move over half the field is refused. Stored per objective and camera
    (video_scale.json) and used for every snapshot's axes from then on. On the XploRA (2026-11-22, by hand): x10
    0.98-0.99 µm/px (field 0.972), x100 0.096 (0.0963); LabSpec's axes 1.00 for both."""
    from . import video as V
    prof = P.load()
    if runner.read_json(runner.lock_path(), None):
        return {"ok": False, "error": "the instrument is busy with a request: wait or cancel it"}
    v = prof.get("video", {})
    st = b.call("status", timeout_s=15, **runner.motor_args(prof))
    x0, y0 = _fnum(st.get("x_um")), _fnum(st.get("y_um"))
    if x0 is None or y0 is None:
        return {"ok": False, "error": "the stage position could not be read"}
    folder = config.out_dir() / "video_scale" / time.strftime("%Y%m%d_%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)

    def frame(tag):
        r = b.call("video", timeout_s=90, out=str(folder / tag), settle_s=float(v.get("settle_s", 1.5)), laser_s=0,
                   formats="jpg", camera=v.get("camera"), stop_video=int(bool(v.get("stop_from_script"))),
                   switch_camera=int(bool(v.get("switch_camera"))), **runner.motor_args(prof))
        if not r.get("ok") or not r.get("out"):
            raise BridgeError(f"video frame {tag}: {r.get('error') or 'no frame'}")
        d = V.decode(r["out"], v.get("packing", "colorref"), v.get("order", "x_fastest"))
        ori, _why = V.orientation_for(d, f"{folder / tag}.jpg", r.get("camera"))
        return V.apply_orientation(d, ori), r

    cap = min(500.0, float(prof["limits"]["max_xy_um"]))
    try:
        f0, r0 = frame("home")
        if V.frame_too_dark(V._lum(f0["img"])):
            return {"ok": False, "error": "the frame is black or without contrast: turn the white light up and focus "
                                          "on a view with visible structure; nothing moved", "folder": str(folder)}
        width = None                    # the field's width in µm: the measured scale, else OLConfig's field
        sa = V.stage_axes(f0, prof, r0.get("objective"), x0, y0, r0.get("camera"))
        if sa is not None:
            width = float(abs(sa["xs"][-1] - sa["xs"][0]))
        elif f0["um"] and len(f0["xs"]) > 1:
            span = float(abs(f0["xs"][-1] - f0["xs"][0]))
            if abs(span / (f0["width"] - 1) - 1) > 0.01:      # LabSpec's axes, when not its one-per-pixel ones
                width = span
        if step_um:
            step = float(step_um)
        elif width:
            step = _sig2(SCALE_STEP_FRACTION * width)
        else:
            return {"ok": False, "error": f"no field of view known for objective {r0.get('objective')!r}: give the "
                                          "move (--um), about a fifth of the field's width; nothing moved"}
        if not 1 <= step <= cap:
            return {"ok": False, "error": f"move: 1 to {cap:g} µm; nothing moved"}
        if width and step > 0.5 * width:
            return {"ok": False, "error": f"{step:g} µm is over half the field ({width:.0f} µm wide): the features "
                                          f"would leave the view; about {_sig2(SCALE_STEP_FRACTION * width):g} µm "
                                          "works; nothing moved"}
        out = {"ok": True, "objective": r0.get("objective"), "camera": r0.get("camera"), "step_um": step,
               "step_from": "asked" if step_um else f"a fifth of the field ({width:.0f} µm)", "folder": str(folder)}
        res = {}
        for axis, dx, dy in (("x", step, 0.0), ("y", 0.0, step)):
            m = b.call("move_xy", timeout_s=120, x_um=round(x0 + dx, 3), y_um=round(y0 + dy, 3),
                       **runner.motor_args(prof))
            if not m.get("ok"):
                raise BridgeError(f"move in {axis}: {m.get('error')}")
            f1, _ = frame(f"plus_{axis}")
            b.call("move_xy", timeout_s=120, x_um=round(x0, 3), y_um=round(y0, 3), **runner.motor_args(prof))
            drow, dcol = V.image_shift(f0["img"], f1["img"])
            res[axis] = {"shift_px": [round(dcol, 2), round(drow, 2)]}
        # x_um(col) = X + (col - c) * sx, y_um(row) = Y - (row - r) * ty: a stage move +d shifts the content by
        # -d/sx columns and +d/ty rows
        sx = -step / res["x"]["shift_px"][0] if abs(res["x"]["shift_px"][0]) > 1 else None
        ty = step / res["y"]["shift_px"][1] if abs(res["y"]["shift_px"][1]) > 1 else None
        cross = max(abs(res["x"]["shift_px"][1]), abs(res["y"]["shift_px"][0]))
        out.update(measured=res, um_per_px_x=None if sx is None else round(sx, 5),
                   um_per_px_y=None if ty is None else round(ty, 5), cross_talk_px=round(cross, 2))
        if sx is None or ty is None or cross > 0.1 * step / max(abs(sx or 1), 1e-6):
            out.update(ok=False, error="no clear shift (a featureless view, or the stage did not move as asked): "
                                       "nothing stored; use a sample with visible structure")
            return out
        store = V.scale_store()
        store[f"{r0.get('objective')}@{r0.get('camera')}"] = {"sx": sx, "ty": ty, "measured": time.strftime(
            "%Y-%m-%d %H:%M:%S"), "step_um": step}
        V.scale_store(store)
        out["stored"] = f"{r0.get('objective')}@{r0.get('camera')}"
        out["note"] = ("image x grows with stage X" if sx > 0 else "image x runs against stage X") + "; " + \
                      ("image up is stage +Y" if ty > 0 else "image up is stage -Y")
        return out
    finally:
        try:
            b.call("move_xy", timeout_s=120, x_um=round(x0, 3), y_um=round(y0, 3), **runner.motor_args(prof))
        except BridgeError:
            pass


def objective_retract_wanted(prof: dict) -> bool:
    """Retract before a manual objective change: profile stage.retract_for_objective_change (off by default), and
    only with stage.z_into_sample known (TESTING T3.17)."""
    return bool((prof.get("stage") or {}).get("retract_for_objective_change")) and plan.into_sign(prof)[1]


def objective_retract(move, prof: dict, z_now) -> float | None:
    """Z limits.retract_um away from the sample before the operator turns the turret; it stays there (a lens of
    another working distance: the operator focuses again). move(**params) is the bridge's 'move' call."""
    if z_now is None:
        return None
    s, _ = plan.into_sign(prof)
    target = round(float(z_now) - s * plan.retract_um(prof), 3)
    r = move(motor=prof["motors"]["z"], mode="value", value=target)
    return _fnum(r.get("value")) if r.get("ok") else None


def objective_question(prof: dict, now, want) -> str:
    """The operator's question before LabSpec's objective changes. On a manual turret (profile manual_objective, as
    on this XploRA) the operator changes the lens by hand: LabSpec only records which one is in place, and every
    objective-dependent scale (video µm, beam offset, spot) follows that record."""
    if prof.get("manual_objective", True):
        return (f"Change the objective BY HAND to {want} (LabSpec has {now}): check the clearance between the objective "
                f"and the sample, turn the turret to {want}, then press OK. ls6t then sets LabSpec's objective to "
                f"{want}. Cancel: nothing changes.")
    return (f"ls6t asks to turn the objective turret from {now} to {want}. Is there clearance between the objective "
            "and the sample? Continue?")


def set_instrument(setup: dict) -> dict:
    """Changes the set-up now, without measuring: filter, laser_nm, grating, objective, hole_um, slit_um (any of
    them). Positions are matched to LabSpec's own names ('x100' finds 'x100_VIS'); the filter must be within the
    caps of the laser that will be in use; a new objective is asked of the operator in LabSpec first (on a manual
    turret, the operator changes the lens by hand and confirms). Nothing fires the laser. Refused while a request
    holds the instrument, and refused as a whole if any part is (nothing changes). Each move waits for its motor and
    for the read-back to show the position asked; returns each move with what LabSpec read back, and the instrument
    before and after."""
    from .profile import filter_pct, laser_key, match_label
    nothing = ": nothing was changed (a request is refused as a whole)"
    s = {k: v for k, v in (setup or {}).items() if v not in (None, "")}
    bad = [k for k in s if k not in SET_KEYS]
    if bad or not s:
        return {"ok": False, "error": f"setup keys: {', '.join(SET_KEYS)}" + (f" (not {', '.join(bad)})" if bad else "")}
    lp = runner.lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        lock = runner.read_json(lp, {})
        return {"ok": False, "error": f"the instrument is busy with {lock.get('request')}: wait or cancel it"}
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({"request": "instrument set", "pid": os.getpid(), "time": time.time()}))
    prof = P.load()
    mot = prof["motors"]
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    moves, out = [], {"ok": False}
    already: list = []                  # settings found in place before any move (the objective)
    obj_asked = False                   # ls6t asked the operator about the objective: LabSpec need not ask again
    t_all = time.time()
    hb0 = b.heartbeat() or {}
    try:
        before = b.call("status", timeout_s=120, **runner.motor_args(prof))
        t_before = time.time() - t_all
        if not before.get("ok"):
            return {"ok": False, "error": f"status: {before.get('error')}"}
        # the target laser (for the filter caps)
        lk = laser_key(prof, s["laser_nm"] if "laser_nm" in s else before.get("laser_nm"))
        if "laser_nm" in s and not laser_key(prof, s["laser_nm"]):
            have = ", ".join("%g" % v["nm"] for v in prof["lasers"].values())
            return {"ok": False, "error": f"laser {s['laser_nm']} nm: not on this instrument ({have} nm)" + nothing}
        if "filter" in s:
            labs, _ = _labels(b, mot["filter"])
            lab, err = match_label(s["filter"], labs or list(prof.get("filters", {})))
            if err:
                return {"ok": False, "error": f"filter {err}" + nothing}
            pct = prof.get("filters", {}).get(lab)
            pct = pct if pct is not None else filter_pct(lab)
            caps = [float(prof["limits"].get("max_filter_pct", 100))]
            if lk:
                caps.append(float(prof["lasers"][lk].get("max_filter_pct", 100)))
            if pct is None or pct > min(caps) + 1e-9:
                return {"ok": False, "error": f"filter {lab!r}: unknown transmission or above the cap ({min(caps):g} %)" + nothing}
            moves.append(("filter", mot["filter"], "label", lab))
        if "laser_nm" in s:
            moves.append(("laser", mot["laser"], "value", float(prof["lasers"][laser_key(prof, s["laser_nm"])]["nm"])))
        if "grating" in s:
            moves.append(("grating", mot["grating"], "value", float(s["grating"])))
        if "objective" in s:
            labs, _ = _labels(b, mot["objective"])
            lab, err = match_label(s["objective"], labs or prof.get("objectives", []))
            if err:
                return {"ok": False, "error": f"objective {err}" + nothing}
            if lab != before.get("objective"):
                retracted = None
                if objective_retract_wanted(prof):
                    retracted = objective_retract(lambda **kw: b.call("move", timeout_s=180, **kw), prof,
                                                  _fnum(before.get("z_um")))
                if config.confirm_policy() != "never":
                    r = b.call("confirm", timeout_s=3600, text=objective_question(prof, before.get("objective"), lab)
                               + (f" (ls6t lowered the stage {plan.retract_um(prof):g} µm first: focus again after "
                                  "the change)" if retracted is not None else ""))
                    if r.get("answer") != "yes":
                        return {"ok": False, "error": "the operator declined in LabSpec" + nothing
                                + (" except the retract: the stage is lowered" if retracted is not None else "")}
                    obj_asked = True
                moves.append(("objective", mot["objective"], "label", lab))
            else:
                already.append({"set": "objective", "asked": lab, "got": before.get("objective"), "ok": True,
                                "already_set": True, "error": None})
        for role in ("laser_pol", "raman_pol"):
            if role in s:
                if not mot.get(role):
                    return {"ok": False, "error": f"{role}: this instrument has no such polariser in the profile" + nothing}
                labs, _ = _labels(b, mot[role])
                lab, err = match_label(s[role], labs or (prof.get("polarizers") or {}).get(role, []))
                if err:
                    return {"ok": False, "error": f"{role} {err}" + nothing}
                moves.append((role, mot[role], "label", lab))
        for key, role in (("hole_um", "hole"), ("slit_um", "slit")):
            if key in s:
                moves.append((key, mot[role], "value", float(s[key])))
        # what the grating can reach with the laser (first-order limit, or the profile's max_nm per grating)
        las_t = float(prof["lasers"][lk]["nm"]) if lk else _fnum(before.get("laser_nm"))
        reach = plan.grating_reach(prof, s.get("grating", before.get("grating")), las_t)
        # LabSpec accepts any grating with any laser (785 nm with 2400 gr/mm, 2026-11-20): the limit applies to
        # what a study acquires (format refuses it there); set only says so
        t_checks = time.time() - t_all - t_before
        done = list(already)
        for what, motor, mode, value in moves:
            cur = before.get({"laser": "laser_nm"}.get(what, what))
            if cur not in (None, "") and P.same_position(cur, value):
                done.append({"set": what, "asked": value, "got": cur, "ok": True, "already_set": True,
                             "error": None})
                continue
            r = b.call("move", timeout_s=600 if what in ("laser", "grating", "objective") else 120, motor=motor,
                       mode=mode, value=value, **runner.move_extra(prof, motor, what == "objective" and obj_asked))
            got = r.get("label") if mode == "label" else r.get("value")
            ok = bool(r.get("ok")) and P.same_position(got, value)
            done.append({"set": what, "asked": value, "got": got, "ok": ok, "error": r.get("error") or None,
                         **_move_timing(r)})
            if r.get("ok") and not ok and what == "grating":
                done[-1]["error"] = _grating_short(b, prof, value, got, r)
            if not r.get("ok"):
                if r.get("stopped_by"):
                    done[-1]["error"] = f"stopped by the {r.get('stopped_by')}"
                break
        t_after0 = time.time()
        after = b.call("status", timeout_s=15, **runner.motor_args(prof))
        if prof.get("manual_objective", True) and after.get("objective"):
            # changed by hand only when a change was asked and made (an objective already in place was not)
            asked = any(d["set"] == "objective" and d["ok"] and not d.get("already_set") for d in done)
            done_obj = {"value": after.get("objective"),
                        "source": "changed by hand and confirmed by the operator" if asked else
                                  "LabSpec's setting: the turret is manual, ls6t cannot see which lens is in place"}
        else:
            done_obj = None
        out = {"ok": all(d["ok"] for d in done) and len(done) == len(moves) + len(already), "moves": done,
               "before": {k: before.get(k) for k in SET_KEYS if before.get(k) is not None},
               "after": {k: after.get(k) for k in SET_KEYS if after.get(k) is not None}}
        if done_obj:
            out["objective"] = done_obj
        notes = []
        if reach:
            out["reach"] = reach
            if reach["max_shift_cm1"] is None:
                notes.append(f"grating {reach['grating']} reaches no Raman shift with the {las_t:g} nm laser: it ends "
                             f"at {reach['limit_nm']:g} nm ({reach['basis']}), below the laser line; studies with "
                             "this pair are refused")
            elif reach["max_shift_cm1"] < 3200:
                notes.append(f"with grating {reach['grating']} and the {las_t:g} nm laser, Raman shifts end near "
                             f"{reach['max_shift_cm1']:g} cm-1 ({reach['limit_nm']:g} nm, {reach['basis']})")
        if any(d["set"] == "grating" and not d.get("already_set") for d in done):
            notes.append("LabSpec reports a new grating at once; whether the turret has turned is not visible to ls6t "
                         "(TESTING T3.16)")
        if notes:
            out["note"] = "; ".join(notes)
        out["timing"] = {"total_s": round(time.time() - t_all, 2), "status_before_s": round(t_before, 2),
                         "checks_s": round(t_checks, 2), "moves_s": round(sum(float(d.get("seconds") or 0)
                                                                              for d in done), 2),
                         "status_after_s": round(time.time() - t_after0, 2)}
        if hb0.get("state") == "busy" and hb0.get("verb") == "inventory":
            out["timing"]["note"] = ("the bridge was still taking its start-up inventory: the set waited for it "
                                     "(status_before_s)")
        if not moves:
            out.update(ok=True, note=(out.get("note") + "; " if out.get("note") else "") + "already set")
        return out
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            lp.unlink()
        except OSError:
            pass


def configs_dir() -> Path:
    return (config.out_dir() / "sim_configs") if config.mode() == "sim" else (config.home() / "configs")


def _configs() -> dict:
    d = configs_dir()
    return {f.stem: {"file": str(f), "bytes": f.stat().st_size,
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(f.stat().st_mtime))}
            for f in sorted(d.glob("*.ini"))} if d.is_dir() else {}


def labspec_config(action: str, name: str | None = None, show: bool = False) -> dict:
    """LabSpec's whole configuration as an .ini file (SaveConfig / LoadConfig, in LabSpec's type library), kept in
    LS6T_HOME/configs/<name>.ini: list, save NAME, load NAME. For a maintainer at the instrument: loading changes
    lasers, gratings, filters and acquisition settings at once, so the operator confirms it in LabSpec. show opens
    LabSpec's own dialog."""
    if action == "list":
        return {"ok": True, "folder": str(configs_dir()), "configs": _configs()}
    if action not in ("save", "load"):
        raise ValueError(f"action {action!r}: list, save or load")
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or name.startswith("."):
        return {"ok": False, "error": "a configuration name: letters, digits, '_', '-', '.' (at most 64)"}
    lock = runner.read_json(runner.lock_path(), None)
    if lock:
        return {"ok": False, "error": f"the instrument is busy with {lock.get('request')}: wait or cancel it"}
    path = configs_dir() / f"{name}.ini"
    if action == "load" and not path.exists():
        return {"ok": False, "error": f"no configuration {name!r}: saved are {', '.join(_configs()) or 'none'}"}
    path.parent.mkdir(parents=True, exist_ok=True)
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    try:
        if action == "load" and config.confirm_policy() != "never":
            r = b.call("confirm", timeout_s=3600,
                       text=f"ls6t asks to load LabSpec's configuration '{name}' ({path}). Lasers, gratings, filters "
                            "and acquisition settings may change. Continue?")
            if r.get("answer") != "yes":
                return {"ok": False, "error": "the operator declined in LabSpec"}
        r = b.call("config", timeout_s=600 if show else 60, action=action, path=str(path), show=int(bool(show)))
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    if not r["ok"]:
        return {"ok": False, "error": r.get("error")}
    out = {"ok": True, "action": action, "name": name, "file": str(path), "returned": r.get("returned"),
           "exists": str(r.get("exists")) == "1"}
    if action == "save" and not out["exists"]:
        out["warning"] = "LabSpec answered but wrote no file there: see TESTING.md T1.4"
    return out


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
    """The LabSpec scripts: a header with this machine's folders, the shared code, then each one's main part."""
    common = (VBS_DIR / "common.vbs").read_text(encoding="utf-8")
    out = {}
    for name, main, what in (("ls6t_bridge.vbs", "bridge_main.vbs", "the ls6t bridge: start it whenever ls6t "
                                                                     "should drive the instrument"),
                             ("ls6t_discover.vbs", "discover_main.vbs", "run once: lists the instrument for "
                                                                         "the ls6t profile"),
                             ("ls6t_script_motor.vbs", "script_motor_main.vbs", "a virtual script-controlled "
                                                                                 "motor that logs LabSpec's calls")):
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


def snapshot(with_laser: bool = False, extended: bool = False) -> dict:
    """A video frame now, with the laser position marked (LabSpec's position plus the profile's measured offset).
    with_laser opens the laser shutter for video.laser_s seconds so the beam shows: only with a laser shutter motor
    in the profile, the current filter within the profile's caps, and the operator's Yes in LabSpec. extended takes
    LabSpec's extended video instead (a still image stitched over the area set in LabSpec; the stage moves): only
    with video.allow_extended in the profile and the operator's Yes; no laser."""
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
    if extended and with_laser:
        return {"ok": False, "error": "an extended video takes no laser"}
    gate = P.review_gate(prof) if with_laser and config.mode() == "labspec" else None
    if gate:
        return {"ok": False, "error": gate}
    if extended and not v.get("allow_extended"):
        return {"ok": False, "error": "extended video is off in the profile (video.allow_extended): a person turns it "
                                      "on once it was tried at the instrument (TESTING.md T5b.6)"}
    try:
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        if extended and config.confirm_policy() != "never":
            r = b.call("confirm", timeout_s=3600,
                       text="ls6t asks for LabSpec's extended video: the stage will move over the area set in LabSpec's "
                            "extended video settings. Is that area safe for the objective? Continue?")
            if r.get("answer") != "yes":
                return {"ok": False, "error": "the operator declined in LabSpec"}
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
        r = b.call("video", timeout_s=60 + settle + laser_s + (float(v.get("extended_max_s", 600)) if extended else 0),
                   out=str(base), settle_s=settle, laser_s=laser_s, extended=int(extended),
                   formats=",".join(v.get("formats", [])), camera=v.get("camera"),
                   switch_camera=int(bool(v.get("switch_camera"))),
                   stop_video=int(bool(v.get("stop_from_script"))), **runner.motor_args(prof))
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    if not r["ok"]:
        return {"ok": False, "error": r.get("error"), "stopped_by": r.get("stopped_by")}
    snap = video.snapshot(r, prof, base, f"video {time.strftime('%H:%M:%S')}"
                          + (f", objective {r.get('objective')}" if r.get("objective") else ""), st.get("laser_nm"))
    snap.pop("_decoded", None)
    if snap.get("measured_offset_um"):
        from . import spot as SP
        rec = SP.record(prof, snap.get("objective"), st.get("laser_nm"), snap["measured_offset_um"], "shutter",
                        {"contrast_sigma": snap.get("spot_contrast_sigma")}, str(folder))
        snap["remembered"] = {k: v for k, v in rec.items() if k != "previous"}
        if rec.get("warning"):
            snap["warnings"].append(rec["warning"])
    out = {"ok": True, "folder": str(folder), **snap,
           "instrument": {k: st.get(k) for k in ("laser_nm", "filter", "objective", "x_um", "y_um", "z_um")}}
    if with_laser and str(r.get("shutter_closed")) != "1":
        out["warnings"].append("the laser shutter could not be confirmed closed: check LabSpec")
    runner.write_json(folder / "snapshot.json", out)
    return out


def alignment_status() -> dict:
    """Per laser: the recorded settings and baseline, the last check (time, passed, ratio) and its age."""
    prof = P.load()
    a = prof.get("alignment", {})
    out = {}
    for k, v in prof.get("lasers", {}).items():
        hist = runner.alignment_history(k)
        last = hist[-1] if hist else None
        age = None
        if last and last.get("time"):
            try:
                age = round((time.time() - time.mktime(time.strptime(last["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600, 1)
            except ValueError:
                pass
        out[k] = {"laser_nm": v["nm"], "settings": a.get("settings", {}).get(k), "baseline": a.get("baselines", {}).get(k),
                  "last_check": {kk: last.get(kk) for kk in ("time", "passed", "ratio", "counts_per_s", "fwhm_cm1",
                                                              "position_cm1", "request")} if last else None,
                  "age_h": age, "checks_on_record": len(hist),
                  "stale": bool(age is not None and age > float(a.get("max_age_h", 24)))}
    return {"lasers": out, "min_ratio": a.get("min_ratio"), "max_fwhm_ratio": a.get("max_fwhm_ratio"),
            "max_age_h": a.get("max_age_h"), "log": str(config.alignment_log())}


def alignment_record(request: str, study: str | None = None, attempt: int | None = None) -> dict:
    """The maintainer's step after a successful AutoAlignment: an 'alignment' (or 'calibration') study's Si check
    becomes the laser's baseline, and its settings the laser's fixed alignment settings. Command line only."""
    folder = _resolve(request)
    studies = runner.read_json(folder / "studies.json", {})
    names = [n for n, s in studies.items() if s.get("kind") in ("alignment", "calibration")]
    if study:
        names = [study] if study in studies else []
    if len(names) != 1:
        raise ValueError(f"name the study (--study): alignment or calibration studies here: {names}")
    name = names[0]
    st = studies[name]
    if st.get("kind") == "alignment":
        res = runner.read_json(folder / name / f"{name}.json", {})
        atts = (res.get("alignment") or {}).get("attempts") or []
        if not atts:
            raise ValueError(f"{name}: no check was measured")
        k = attempt or atts[-1]["attempt"]
        chk = runner.read_json(folder / name / f"check{k}" / f"check{k}.json", {})
    else:
        chk = runner.read_json(folder / name / f"{name}.json", {})
    if not chk.get("reliable"):
        raise ValueError(f"{name}: the check is not reliable ({chk.get('unreliable_because')}): measure again")
    prof = P.load()
    laser = (chk.get("instrument") or {}).get("laser_nm")
    m = runner.alignment_metrics(chk, prof, laser)
    if not m.get("counts_per_s"):
        raise ValueError(f"{name}: no Si line was fitted")
    lk = m["laser_key"]
    if not lk:
        raise ValueError(f"laser {laser} nm is not in the profile")
    req = chk.get("setup", {}).get("requested", {}) or {}
    inst = chk.get("instrument") or {}
    settings = {"grating": req.get("grating", inst.get("grating")), "filter": req.get("filter", inst.get("filter")),
                "objective": req.get("objective", inst.get("objective")), "exposure_s": m["exposure_s"],
                "accumulations": int((chk.get("spectra") or [{}])[0].get("accumulations") or 2)}
    for key in ("hole_um", "slit_um"):
        if req.get(key) is not None:
            settings[key] = req[key]
    base = {"counts_per_s": m["counts_per_s"], "fwhm_cm1": m["fwhm_cm1"], "position_cm1": m["position_cm1"],
            "filter": m["filter"], "time": m["time"], "request": str(folder), "study": name}
    prof.setdefault("alignment", {}).setdefault("settings", {})[lk] = settings
    prof["alignment"].setdefault("baselines", {})[lk] = base
    path = P.save(prof)
    return {"ok": True, "laser": lk, "settings": settings, "baseline": base, "profile": str(path)}


def profile_set(key: str, value_json: str) -> dict:
    """A person's edit of one profile value: `ls6t profile set video.laser_offset_um "[1.4, -0.6]"`."""
    try:
        value = json.loads(value_json)
    except ValueError:
        value = value_json
    prof = P.load()
    old = runner.lookup(prof, key)
    P.set_key(prof, key, value)
    was_auto = bool(prof.pop("auto", None))
    if was_auto:
        prof["source"] = str(prof.get("source", "")).replace("automatic", "changed by a person; first automatic", 1)
    out = {"ok": True, "key": key, "old": old, "new": value, "profile": str(P.save(prof))}
    if was_auto:
        out["note"] = "the profile is now a person's: newer inventories from the bridge no longer replace it"
    return out


def spot_status() -> dict:
    """The beam's remembered offsets per objective and laser: value, method, age, drift since the first record."""
    from . import spot as SP
    return SP.status(P.load())


def references() -> dict:
    """What ls6t keeps between sessions besides the profile: the spectral corrections (per laser and grating), the
    white-light backgrounds, the beam offsets, and the last alignment check per laser."""
    from . import background as BG
    from . import spot as SP
    from . import xcal
    corr = {k: {**v, "age_h": xcal.age_h(v)} for k, v in xcal.load().items()}
    from . import perf
    return {"spectral_corrections": corr, "backgrounds": BG.index(), "performance": perf.status(),
            "beam_offsets": SP.status(P.load())["records"], "labspec_configs": _configs(),
            "alignment": {k: v.get("last_check") for k, v in alignment_status()["lasers"].items()}}


def references_clear(what: str, name: str | None = None) -> dict:
    """A person's clean-up: what = correction (all, or laser@grating), background (a name), spot (all)."""
    from . import background as BG
    from . import spot as SP
    from . import xcal
    if what == "correction":
        if name:
            las, gr = name.split("@")
            return {"cleared": xcal.clear(float(las), float(gr))}
        return {"cleared": xcal.clear()}
    if what == "background":
        idx = BG.index()
        if not name or name not in idx:
            raise ValueError(f"background {name!r}: stored are {', '.join(idx) or 'none'}")
        Path(idx[name]["file"]).unlink(missing_ok=True)
        idx.pop(name)
        (BG.folder() / "index.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
        return {"deleted": name}
    if what == "spot":
        return SP.forget()
    raise ValueError("what: correction, background or spot")


def spot_forget(objective: str | None = None, laser_nm: float | None = None) -> dict:
    from . import spot as SP
    return SP.forget(objective, laser_nm)


def install_bridge(dest: Path | None = None) -> dict:
    """Writes ls6t_bridge.vbs, ls6t_discover.vbs and ls6t_script_motor.vbs (optional, TESTING.md T1.4), configured
    with this machine's folders, to LS6T_HOME/vbs."""
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


IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")
MAX_IMAGE_BYTES = 900_000           # tool results carry at most about 1 MB: larger images go as a reduced copy


def image_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    base = config.out_dir().resolve()       # requests and snapshots
    if base not in p.parents or p.suffix.lower() not in IMAGE_TYPES or not p.is_file():
        raise ValueError(f"{path}: not an image written by a study or a snapshot (under {base})")
    return p


def image_for_view(path: str) -> tuple[bytes, str, dict]:
    """An image to show (bytes, 'png' or 'jpeg', info): a PNG under MAX_IMAGE_BYTES as it is; anything larger or in
    another format (LabSpec's own snapshot formats) as a reduced copy, written next to it (<name>.view.png/.jpg)."""
    p = image_path(path)
    size = p.stat().st_size
    if p.suffix.lower() == ".png" and size <= MAX_IMAGE_BYTES:
        return p.read_bytes(), "png", {"file": str(p), "reduced": False}
    try:
        from PIL import Image as PILImage
    except ImportError as exc:
        raise ValueError(f"{p}: {size / 1e6:.1f} MB or not a PNG, and Pillow is missing to reduce it") from exc
    import io
    with PILImage.open(p) as im:
        im = im.convert("RGB")
        w0, h0 = im.size
        side = 1600
        while True:
            k = min(1.0, side / max(im.size))
            small = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k)))) if k < 1 else im
            buf = io.BytesIO()
            small.save(buf, "PNG", optimize=True)
            fmt = "png"
            if buf.tell() > MAX_IMAGE_BYTES:
                buf = io.BytesIO()
                small.save(buf, "JPEG", quality=85)
                fmt = "jpeg"
            if buf.tell() <= MAX_IMAGE_BYTES or side <= 400:
                break
            side = int(side * 0.75)
    data = buf.getvalue()
    out = p.with_name(p.stem + (".view.png" if fmt == "png" else ".view.jpg"))
    try:
        out.write_bytes(data)
    except OSError:
        pass
    return data, fmt, {"file": str(p), "reduced": True, "view": str(out), "pixels": [w0, h0],
                       "shown": list(small.size), "bytes": len(data)}


# --------------------------------------------------------------------------------------------- benchmarks

def bench(names: list | None = None, wait_s: float = 600) -> dict:
    """Runs the benchmarks on the simulator (known answers): see bench.py."""
    from . import bench as B
    return B.run(names, wait_s)


def dumps(o) -> str:
    return json.dumps(o, indent=1, default=str)
