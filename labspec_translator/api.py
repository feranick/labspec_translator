"""api.py — the functions behind the command line and the MCP server (JSON in, JSON out).
"""
from __future__ import annotations

import math
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

from . import __version__, BRIDGE_PROTOCOL, config, plan, runner, spectra
from . import format as F
from . import profile as P
from . import shutter as SH
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
            how = "detached (outside the app's job)"
        except OSError:
            subprocess.Popen(cmd, creationflags=detached | group | nowin, **kw)
            how = ("detached, but inside the app's job: Windows ends it with the MCP server if the app ends that "
                   "process")
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
        how = "detached (own session)"
    log.close()
    return how


def log_rejected(tool: str, args, errors) -> None:
    """A call refused before anything ran (invalid studies, wrong arguments): LS6T_HOME/rejected_log.jsonl, shown by
    jobs (2026-11-25: a small local model's run_studies calls never reached ls6t, and nothing showed it)."""
    try:
        p = config.home() / "rejected_log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool,
                                 "args": json.loads(json.dumps(args, default=str))
                                 if len(json.dumps(args, default=str)) < 4000 else str(args)[:4000],
                                 "errors": errors}, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def rejected_log(n: int = 10) -> list:
    try:
        rows = (config.home() / "rejected_log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in rows[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def coerce_studies(studies, tool: str = "run_studies") -> dict:
    """{name: study} from what a client sent: a JSON object, or a JSON string of one (small models send strings);
    a single study without its name is wrapped as {"study": ...}. Anything else is a clear error (and logged)."""
    raw = studies
    if isinstance(studies, str):
        try:
            studies = json.loads(studies)
        except ValueError as exc:
            log_rejected(tool, {"studies": raw}, f"not JSON: {exc}")
            raise ValueError(f"studies: a JSON object {{name: study}} is needed; the text sent is not JSON ({exc})")
    if isinstance(studies, dict) and "kind" in studies and not any(isinstance(v, dict) and "kind" in v
                                                                  for v in studies.values()):
        studies = {"study": studies}
    if not isinstance(studies, dict) or not studies or not all(isinstance(v, dict) for v in studies.values()):
        log_rejected(tool, {"studies": raw}, "not an object of studies")
        raise ValueError('studies: an object {"name": {"kind": "spectrum", ...}} is needed (one entry per study); '
                         f"got {type(studies).__name__}")
    return studies


def laser_filter(prof: dict, laser_nm, asked=None) -> str:
    """The filter for the flat tools: what was asked, else the laser's lasers.<k>.default_filter (set by a person),
    else 1% (2026-11-25: at 1 % the 785 nm Si line was not detected in 13 spectra, SNR 1-3; 10 % gave 10-15)."""
    if asked not in (None, ""):
        return str(asked)
    lk = P.laser_key(prof, laser_nm)
    f = (prof["lasers"][lk].get("default_filter") if lk else None)
    return str(f) if f else "1%"


def snap_laser(prof: dict, laser_nm) -> float:
    """The profile's laser nearest to what was said (638 -> 637.75; 785 -> 785.04), within 5 nm."""
    want = float(laser_nm)
    have = [float(v["nm"]) for v in (prof.get("lasers") or {}).values() if v.get("nm") is not None]
    if not have:
        return want
    best = min(have, key=lambda x: abs(x - want))
    if abs(best - want) > 5:
        raise ValueError(f"laser_nm {want:g}: this instrument has {', '.join(f'{h:g}' for h in sorted(have))} nm")
    return best


def snap_objective(prof: dict, objective: str) -> str:
    """The profile's objective with the same magnification when the name differs (x100 -> x100_VIS)."""
    objs = [str(o) for o in (prof.get("objectives") or [])]
    if not objs or str(objective) in objs:
        return str(objective)
    import re as _re
    m = _re.search(r"(\d+)", str(objective))
    same = [o for o in objs if m and _re.search(r"(\d+)", o) and _re.search(r"(\d+)", o).group(1) == m.group(1)]
    return same[0] if same else str(objective)


LASER_MAY_BE_ON_TAG = "THE LASER MAY STILL REACH THE SAMPLE"


def _summary(st: dict) -> dict:
    """A finished request in a few numbers, for small models: per study reliable (or why not), peaks, calibration."""
    out = {"request": st.get("request"), "done": st.get("done"), "state": st.get("state"),
           **({"why": st["why"]} if st.get("why") else {})}
    res = {}
    for n, r in (st.get("studies") or {}).items():
        if not isinstance(r, dict):
            continue
        s = {"reliable": r.get("reliable"), "warnings": (r.get("warnings") or [])[:5]}
        if r.get("unreliable_because"):
            s["unreliable_because"] = r["unreliable_because"][:5]
        if r.get("why"):
            s["why"] = r["why"]
        pk = r.get("peaks_by_name") or {}
        if not pk and (r.get("spectra") or []):
            pk = {p.get("name"): p for p in (r["spectra"][0].get("peaks") or []) if p.get("name")}
        # the same fields for every peak, null where not fitted (T9.18)
        s["peaks"] = {k: {f: v.get(f) for f in ("position_cm1", "fwhm_cm1", "height", "snr", "ok")}
                      | ({"why": v["why"]} if v.get("why") else {}) for k, v in pk.items()}
        # how far to trust a position: the zero's expected uncertainty (T9.18: a small model needs it)
        cu_ = ((r.get("checks") or {}).get("zero") or {}).get("correction_used") or {}
        if cu_.get("expected_uncertainty_cm1") is not None:
            s["expected_uncertainty_cm1"] = cu_["expected_uncertainty_cm1"]
        cal = r.get("calibration") or {}
        if cal:
            s["calibration"] = {k: cal.get(k) for k in ("offset_cm1", "pass", "trusted", "outcome", "stored_as",
                                                        "repeats") if cal.get(k) is not None}
        imgs = r.get("images") or {}
        if imgs:
            s["images"] = {k: (v.get("path") if isinstance(v, dict) else v) for k, v in imgs.items()}
        res[n] = s
    out["studies"] = res
    if st.get("waiting_for"):
        out["waiting_for"] = st["waiting_for"]
    rel = st.get("shutter_hold_released")
    if rel is not None:
        out["shutter_hold_released"] = rel
    if rel is not True and (rel is not None or LASER_MAY_BE_ON_TAG in str(st.get("why", ""))):
        out["next"] = ("the laser shutter may still be held open: call instrument(action=\"laser_off\") now (it only "
                       "closes the shutter) and tell the user")
    elif not st.get("done") and st.get("waiting_for"):
        out["next"] = (f"waiting for the operator's go-ahead in LabSpec: tell the user someone at the instrument "
                       f"must answer it; then call study_status(request=\"{st.get('request')}\") until done")
    elif not st.get("done"):
        out["next"] = f"still running: call study_status(request=\"{st.get('request')}\") until done"
    return out


FLAT_WAIT_S = 90.0      # the flat tools answer within this (2026-11-25: a client link gave up at 140 s)
CURRENT_FLAT: dict = {}  # the request a flat tool is waiting for (its progress messages)


def flat_wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("LS6T_FLAT_WAIT_S", FLAT_WAIT_S)))
    except ValueError:
        return FLAT_WAIT_S


def flat_progress() -> str | None:
    """The progress line for a flat tool's wait: the operator dialog it waits on, else None."""
    f = CURRENT_FLAT.get("request")
    w = waiting_for(f) if f else None
    return f"waiting for {w}" if w else None


def run_and_wait(studies: dict, wait_total_s: float | None = None, inline: bool = False) -> dict:
    """Starts studies (a detached runner, as run_studies) and waits for them up to wait_total_s (LS6T_FLAT_WAIT_S,
    90 s), then returns the short summary: done, or the request to poll with study_status. The request goes on
    whether or not the caller is still there."""
    if wait_total_s is None:
        wait_total_s = flat_wait_s()
    s = start(studies, inline=inline)
    if not s.get("ok"):
        return s
    CURRENT_FLAT["request"] = s["request"]
    end = time.monotonic() + max(0.0, float(wait_total_s))
    st = status(s["request"], 0)
    while not st.get("done") and time.monotonic() < end:
        st = status(s["request"], min(30.0, max(0.5, end - time.monotonic())))
    st.setdefault("request", s["request"])
    return _summary(st)


def measure_spectrum(laser_nm=637.75, center_cm1=None, from_cm1=None, to_cm1=None, exposure_s=1.0,
                     accumulations=2, grating=1200, filter=None, objective="x100_VIS", peak_cm1=None,
                     sample="sample", wait_total_s: float | None = None, inline: bool = False) -> dict:
    """One spectrum from flat arguments (for small models): a centre, or from/to for a range; a peak fitted near
    peak_cm1 (None, the default: the strongest peaks are found; 2026-11-25: the default was 520.7, so a small model
    that left it out fitted Si on any sample)."""
    prof = P.load()
    try:
        las = snap_laser(prof, laser_nm)
        acq = {"exposure_s": float(exposure_s), "accumulations": int(accumulations),
               # one accumulation: one more taken for the spike removal (2026-11-25: "spike removal is off" warned
               # on the small model's 1-accumulation request)
               "spike": "double" if int(accumulations) >= 2 else "double_auto"}
        if from_cm1 is not None and to_cm1 is not None:
            acq["range_cm1"] = [float(from_cm1), float(to_cm1)]
        else:
            acq["center_cm1"] = float(center_cm1 if center_cm1 is not None else (peak_cm1 or 520.7))
        study = {"kind": "spectrum", "sample": {"name": str(sample)},
                 "setup": {"laser_nm": las, "grating": float(grating), "filter": laser_filter(prof, las, filter),
                           "objective": snap_objective(prof, objective)},
                 "acquire": acq}
        if peak_cm1 is not None:
            # the fit window with the grating, as calibrations do (T9.19: +-15 at 600 held fewer than 7 points)
            study["process"] = {"peaks": [{"name": "peak", "near_cm1": float(peak_cm1),
                                           "window_cm1": 60 if float(grating) <= 600 else 30}]}
    except (TypeError, ValueError) as exc:
        log_rejected("measure_spectrum", {"laser_nm": laser_nm, "center_cm1": center_cm1, "from_cm1": from_cm1,
                                          "to_cm1": to_cm1}, str(exc))
        return {"ok": False, "error": str(exc)}
    out = run_and_wait({"spectrum": study}, wait_total_s, inline)
    out["study"] = study
    return out


def calibrate_si(laser_nm=637.75, grating=1200, repeats=5, filter=None, objective="x100_VIS", center_cm1=None,
                 from_cm1=None, to_cm1=None, wait_total_s: float | None = None, inline: bool = False) -> dict:
    """A silicon calibration (520.7 cm-1) from flat arguments: the correction ls6t applies to later spectra. The
    window is centred on Si, or where the measurements will be (center_cm1, or from_cm1 + to_cm1): the zero
    differs between windows (2026-11-25, 1200: +0.66 cm-1 with the window ~0.9 nm off the calibration's)."""
    prof = P.load()
    try:
        las = snap_laser(prof, laser_nm)
        cal = {"reference": "si", "adjust": "ls6t", "repeats": int(repeats)}
        if float(grating) <= 600:
            cal["window_cm1"] = 60
        study = {"kind": "calibration", "sample": {"name": "Si wafer"},
                 "setup": {"laser_nm": las, "grating": float(grating), "filter": laser_filter(prof, las, filter),
                           "objective": snap_objective(prof, objective)},
                 "acquire": {"center_cm1": 520.7, "exposure_s": 1, "accumulations": 2, "spike": "double"},
                 "calibration": cal, "video": {"before": False}}
        if center_cm1 is not None:
            study["acquire"]["center_cm1"] = float(center_cm1)
        elif from_cm1 is not None and to_cm1 is not None:
            study["acquire"]["center_cm1"] = round((float(from_cm1) + float(to_cm1)) / 2.0, 2)
    except (TypeError, ValueError) as exc:
        log_rejected("calibrate_si", {"laser_nm": laser_nm, "grating": grating}, str(exc))
        return {"ok": False, "error": str(exc)}
    out = run_and_wait({"calibration": study}, wait_total_s, inline)
    out["study"] = study
    return out


def start(studies: dict, inline: bool = False, order: list | None = None) -> dict:
    """Validates, writes the request folder and starts its runner (a detached process, or a thread with
    inline=True). Studies run in the order given (the keys' order, or `order`: every name once; 2026-11-24: an app
    passed them sorted by name). Returns {request, studies, estimate_min} or {ok: false, errors}."""
    if order:
        if sorted(map(str, order)) != sorted(studies) or len(set(order)) != len(order):
            return {"ok": False, "errors": {"order": [f"order must name every study once: {', '.join(studies)}"]}}
        studies = {n: studies[n] for n in order}
    v = F.validate(studies)
    bad = {n: r["errors"] for n, r in v.items() if not r["ok"]}
    if bad:
        log_rejected("run_studies", {"studies": list(studies)}, bad)
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
        how = _spawn(folder)
        runner.write_json(folder / "request.json", dict(req, runner_started=how))
    out = {"ok": True, "request": str(folder), "studies": list(studies),
           "estimate_min": round(sum(req["estimate_min"].values()), 2)}
    try:
        probs = P.load().get("problems")
    except Exception:  # noqa: BLE001 - only a hint here
        probs = None
    if probs:
        out["profile_problems"] = probs
    return out


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
        # dead only when its heartbeat and its log are both old and its process is gone (2026-11-25: a live runner
        # was called 'interrupted' from its heartbeat alone)
        if not runner.runner_alive(folder):
            tail = ""
            try:
                tail = (folder / "runner.log").read_text(encoding="utf-8", errors="replace").strip()[-400:]
            except OSError:
                pass
            how = runner.read_json(folder / "request.json", {}).get("runner_started", "")
            st = dict(st, state="interrupted",
                      why="the runner stopped without finishing (the computer slept, or its process was ended"
                          + ("; it ran inside the app's job, so it ended when the app ended the MCP server: e.g. a "
                             "client that gave up on a long tool call" if "inside the app's job" in how else "")
                          + "); the instrument is free again; the studies without a result were not measured: run "
                            "them again"
                          + (f". runner.log: {tail}" if tail else ""))
            runner.write_json(folder / "state.json", st)
            runner.free_lock_of(folder)         # its runner is gone: the instrument is free (2026-11-25)
            for n in runner.read_json(folder / "studies.json", {}):
                res = folder / n / f"{n}.json"
                if not res.exists():
                    runner.write_json(res, {"ok": False, "why": st["why"], "reliable": False,
                                            "unreliable_because": ["interrupted"], "warnings": []})
    return st


def waiting_for(folder) -> str | None:
    """What a running request waits for from a person (an operator dialog in LabSpec), or None."""
    w = runner.read_json(Path(folder) / "waiting.json", None)
    if not w:
        return None
    return (f"the operator's go-ahead in LabSpec ({time.time() - float(w.get('since', time.time())):.0f} s so far)"
            + (f": {w['text']}" if w.get("text") else ""))


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
    wt = waiting_for(folder) if not out["done"] else None
    if wt:
        out["waiting_for"] = wt
    for k in ("why", "stopped_by", "reason", "config_restored", "config_restore_error", "returned", "stage_at_end",
              "shutter_hold_released", "trace", "profile_problems"):
        if st.get(k) is not None:
            out[k] = st[k]
    if wt:
        out["next"] = ("waiting for the operator's go-ahead in LabSpec (a person at the instrument answers the "
                       "question there; LS6T_CONFIRM sets when ls6t asks): tell the user, then call study_status "
                       "again")
    elif pending:
        eta = [p.get("eta_min") or p.get("estimate_min") for p in prog.values()]
        eta = [e for e in eta if e is not None]
        out["next"] = ("call study_status again" + (f"; about {max(eta):.0f} min left for the slowest study"
                                                     if eta else "") +
                       ": when it needs more than about 5 minutes, tell the user when it should finish and stop")
    else:
        out["next"] = "check 'reliable', 'unreliable_because', 'warnings' and 'checks' before using the numbers"
    return out


def run(studies: dict, wait_s: float = 45, inline: bool = False, order: list | None = None) -> dict:
    s = start(studies, inline=inline, order=order)
    if not s.get("ok"):
        return s
    return status(s["request"], wait_s)


def cancel(request: str) -> dict:
    folder = _resolve(request)
    (folder / "cancel").write_text(str(time.time()), encoding="utf-8")
    st = _state(folder)
    freed = runner.free_lock_of(folder) if (st.get("state") in runner.FINAL
                                             or not runner.holder_alive(str(folder))) else False
    return {"request": str(folder), "state": st.get("state"),
            **({"lock_released": True} if freed else {}),
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
    return {"requests": rows, "instrument_actions": action_log(10), "rejected_calls": rejected_log(10),
            "instrument_busy": lock.get("request") if lock else None,
            **({"instrument_lock_stale": "its runner is gone: the next request takes the instrument (cancel it to "
                                         "free it now)"}
               if lock and not runner.holder_alive(lock.get("request")) else {})}


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
    try:                                    # study setups write it too (runner.note_grating_set)
        lg = json.loads((config.home() / "last_grating_set.json").read_text(encoding="utf-8"))
        if not rec.get("last_grating_set") or str(lg.get("time", "")) > str(rec["last_grating_set"]):
            rec["last_grating_set"] = lg.get("time")
            rec["last_grating_set_by"] = lg.get("by")
    except (OSError, ValueError):
        pass
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


def bridge_mismatch(ping: dict) -> str | None:
    """The running bridge is not this account's own copy, or another version (2026-11-23: an account ran the other
    account's ls6t_bridge.vbs from LabSpec's shared script list)."""
    if not ping or not ping.get("ok"):
        return None
    v = ping.get("version")
    if v and v != __version__:
        return (f"the bridge running in LabSpec is {v}, this ls6t is {__version__}: in LabSpec, Processing > Scripts, "
                f"start this account's own ls6t_bridge.vbs ({config.home() / 'vbs'}); a copy from another account or "
                "an older install runs otherwise" + (f" ({ping['dir_note']})" if ping.get("dir_note") else ""))
    return None


BUSY_OK = ("status", "last_error", "laser_off", "resume", "end_bridge", "autocal_info")


def _instrument(action, camera, setup, seconds, um=None) -> dict:
    # While a request runs, only reads and safety commands: any other bridge command (camera, calibration read,
    # snapshot...) makes the bridge let a held laser shutter go in the middle of the request's spectra (2026-11-24:
    # the light went off 53 s into a held series; the shutter's read-back after every spectrum now says why)
    lock = runner.read_json(runner.lock_path(), None)
    if lock and action not in BUSY_OK:
        return {"ok": False, "error": f"the instrument is busy with {lock.get('request')}: only status, last_error, "
                                      "laser_off and resume while it runs; wait or cancel it"}
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
            mism = bridge_mismatch(ping)
            if mism:
                out["bridge_mismatch"] = mism
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
        if action == "clearance":
            return clearance(b)
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
        if action == "shutter_test":
            return shutter_test()
        if action == "shutter_verify":
            return shutter_verify(float(seconds or VERIFY_EXPOSURE_S))
        if action in ("camera_on", "camera_off"):
            prof = P.load()
            num, err = P.camera_number(prof, camera)
            if err:
                return {"ok": False, "error": err}
            shown = None
            if (prof.get("motors") or {}).get("illumination") and action == "camera_on":
                shown = b.call("status", timeout_s=15, **runner.motor_args(prof)).get("illumination")
            r = b.call("camera", timeout_s=30, action=action[7:],
                       switch_camera=int(bool(prof.get("video", {}).get("switch_camera"))),
                       stop_video=int(bool(prof.get("video", {}).get("stop_from_script"))),
                       **({"camera": num} if num is not None else {}), **runner.motor_args(prof))
            if r.get("ok") and str(r.get("camera_restart", "")) == "1" and shown:
                # after a video restart LabSpec has shown bright field with the light in dark field (2026-11-23)
                r["illumination"] = dict(illumination_set(b, prof, shown, shown),
                                         why="set again after the video restart")
            names = {int(v): k for k, v in (prof.get("video", {}).get("cameras") or {}).items()}
            try:
                r["camera_name"] = names.get(int(float(r.get("camera"))))
            except (TypeError, ValueError):
                pass
            return r
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    raise ValueError(f"action {action!r}: status, set, laser_on, laser_off, camera_on, camera_off, calibration, "
                     "autocal_info, autocal_run, shutter_test, shutter_verify, discover, resume, end_bridge or last_error")


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


SET_KEYS = ("filter", "laser_nm", "grating", "objective", "hole_um", "slit_um", "laser_pol", "raman_pol",
            "illumination")


def _illumination_label(want: str, labels: list) -> str | None:
    """'bright' / 'dark' (or a label as LabSpec names it) -> the illumination motor's label."""
    w = str(want).strip().lower()
    for lab in labels:
        if str(lab).strip().lower() == w:
            return lab
    hits = [lab for lab in labels if w.split()[0] in str(lab).lower()] if w else []
    return hits[0] if len(hits) == 1 else None


def illumination_set(b: Bridge, prof: dict, want: str, shown) -> dict:
    """Bright or dark field on the illumination motor (profile motors.illumination; the XploRA's IlluminatorV). When
    LabSpec already shows the mode asked, it goes through the other one and back: twice on 2026-11-23, after a video
    restart or an objective change, LabSpec showed bright field with the light in dark field, and only a real change
    put them in agreement. Never LaserSwitch (the bridge refuses it)."""
    mot = (prof.get("motors") or {}).get("illumination")
    if not mot:
        return {"ok": False, "error": "no illumination motor in the profile (motors.illumination; the XploRA's is "
                                      "IlluminatorV: ls6t profile set motors.illumination IlluminatorV)"}
    labs, _ = _labels(b, mot)
    labs = [x for x in labs if x] or list(prof.get("illumination_labels") or [])
    lab = _illumination_label(want, labs)
    if not lab:
        return {"ok": False, "error": f"illumination {want!r}: one of {', '.join(labs) or 'bright, dark'}"}
    steps = []
    if shown not in (None, "") and P.same_position(shown, lab):
        other = next((x for x in labs if x != lab), None)
        if other:
            steps.append(other)
    steps.append(lab)
    r: dict = {}
    for v in steps:
        r = b.call("move", timeout_s=60, motor=mot, mode="label", value=v)
        if not r.get("ok"):
            return {"ok": False, "asked": lab, "error": r.get("error")}
    got = r.get("label")
    return {"ok": P.same_position(got, lab), "asked": lab, "got": got, "shown_before": shown,
            "reasserted": len(steps) > 1, "via": steps}


def autocal_info() -> dict:
    """LabSpec's AutoCalibration scripts on this PC and, from their text, whether the bridge can run them unattended
    (it asks the operator? it shares names with the bridge?). Nothing runs."""
    from . import autocal
    bridge_text = compose_vbs(config.bridge_dir(), config.home())["ls6t_bridge.vbs"]
    return {"ok": True, **autocal.info(P.load(), bridge_text)}


def autocal_question(ac: dict) -> str:
    """The operator's question before LabSpec's AutoCalibration runs."""
    what = (f"LabSpec's AutoCalibration ({ac['script']})" if ac.get("method") != "call" else
            "LabSpec's AutoCalibration (" + ("zero order only, no sample" if ac.get("mode") == "offset" else
                                             "zero order, then the Si 520.7 cm-1 band: the laser fires") + ")")
    return (f"ls6t asks to run {what}."
            + (f" Put {ac['sample']} under the objective, in focus, first." if ac.get("sample") else
               " Put the silicon wafer under the objective, in focus, first." if ac.get("method") == "call"
               and ac.get("mode") != "offset" else "") + " Continue?")


def autocal_call(call, ac: dict) -> dict:
    """Runs LabSpec's AutoCalibration: its own routine called directly (calibration.autocal.method 'call'; LabSpec's
    help, VBS AutoCalibration: mode 0 offset and Si band, 2 offset only; result 0 done and saved, 1-3 failed), or
    HORIBA's script (calibration.autocal.script, run_vbs). call(verb, timeout_s=..., **params) is the bridge's."""
    t = float(ac.get("timeout_s", 1800))
    if ac.get("method") == "call":
        return call("autocal", timeout_s=t, mode=2 if ac.get("mode") == "offset" else 0)
    return call("run_vbs", timeout_s=t, path=str(ac["script"]))


def autocal_run() -> dict:
    """Runs LabSpec's AutoCalibration once from the bridge (profile calibration.autocal enabled by a person; the
    operator says Yes in LabSpec first), with LabSpec's calibration read before and after. For the maintainer's test
    (TESTING T4.9); calibration studies with adjust 'auto' do the same when the calibration is off."""
    prof = P.load()
    ac = prof.get("calibration", {}).get("autocal") or {}
    if not ac.get("enabled") or not (ac.get("script") or ac.get("method") == "call"):
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
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        pair = runner.combo(runner._f(st.get("laser_nm")), st.get("grating"))
        r = b.call("confirm", timeout_s=3600, text=f"ls6t: AutoCalibration for {pair} (LabSpec calibrates the "
                                                   "grating in use only). " + autocal_question(ac))
        if r.get("answer") != "yes":
            return {"ok": False, "error": "the operator declined in LabSpec"}
        t0 = time.time()
        r = autocal_call(b.call, ac)
        after = labspec_calibration(b)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    out = {"ok": bool(r.get("ok")), "error": r.get("error") or None, "seconds": round(time.time() - t0, 1),
           "applies_to": pair}
    if before.get("ok") and after.get("ok"):
        out["changed"] = {g: {k: [v, after["gratings"].get(g, {}).get(k)] for k, v in vals.items()
                              if after["gratings"].get(g, {}).get(k) != v}
                          for g, vals in before["gratings"].items()}
        out["changed"] = {g: c for g, c in out["changed"].items() if c}
    return out


SHUTTER_STEPS = (("value", "off"), ("value", "on"), ("label", "off"), ("label", "on"), ("auto_mode", "off"))


def shutter_test(motor: str | None = None) -> dict:
    """Which way of moving the laser shutter blocks the beam (2026-11-23: a laser_off 'shutter' background kept 86 %
    of the Si signal). With the operator watching LabSpec's real-time display on Si: the shutter by value 0 / 1, by
    label Off / On (ShutterMode manual), and Off with ShutterMode back to automatic; after each move ls6t reads it
    back and asks whether the Si line is there. Then LabSpec's ShutterMode and the shutter are put back. The laser is
    as the operator set it; nothing is acquired by ls6t."""
    prof = P.load()
    gate = P.review_gate(prof) if config.mode() == "labspec" else None
    if gate:
        return {"ok": False, "error": gate}
    motor = motor or prof["motors"].get("laser_shutter") or prof.get("laser_shutter_candidate")
    if not motor:
        return {"ok": False, "error": "no laser shutter motor: give it (--motor), from the discovery dump"}
    if runner.read_json(runner.lock_path(), None):
        return {"ok": False, "error": "a request holds the instrument: wait or cancel it"}
    sa = runner.shutter_args(prof)
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    steps, out = [], {"ok": True, "motor": motor, "labels": [sa["shutter_off"], sa["shutter_on"]]}
    try:
        first = b.call("shutter", timeout_s=30, motor=motor, action="read")
        if not first.get("ok"):
            return {"ok": False, "error": first.get("error")}
        out["before"] = {"mode": first.get("mode_before"), "label": first.get("label_before"),
                         "value": first.get("value_before")}
        intro = (f"ls6t shutter test ({motor}). Put silicon under the objective, in focus, with a filter that makes "
                 "the Si line unmistakable (e.g. 10 %, within the caps); start LabSpec's real-time display around 520 "
                 "cm-1 with a short exposure and the laser On in the status bar, and ZOOM the display onto the line "
                 "(a faint line on a zoomed-out display looks the same either way: 2026-11-23). ls6t will move "
                 f"{motor} {len(SHUTTER_STEPS)} times and ask each time whether the Si line is there; wait for two or "
                 "three refreshes before answering. The beam reaches the sample whenever the shutter is open. "
                 "Continue?")
        if b.call("confirm", timeout_s=3600, text=intro).get("answer") != "yes":
            return {"ok": False, "error": "the operator declined in LabSpec"}
        for by, state in SHUTTER_STEPS:
            if by == "auto_mode":
                b.call("shutter", timeout_s=30, motor=motor, action="set", state=state, manual="1", **sa)
                r = b.call("shutter", timeout_s=30, motor=motor, action="mode", mode=0)
                what = f"{sa['shutter_off']} by label, then LabSpec's ShutterMode back to automatic (0)"
                lab, val = r.get("label_after"), r.get("value_after")
            else:
                r = b.call("shutter", timeout_s=30, motor=motor, action="set", state=state, manual="1",
                           **dict(sa, shutter_by=by))
                what = (f"{'1' if state == 'on' else '0'} by value" if by == "value" else
                        f"'{sa['shutter_on'] if state == 'on' else sa['shutter_off']}' by label") + " (ShutterMode manual)"
                lab, val = r.get("after_label"), r.get("after_value")
            q = (f"ls6t moved {motor} to {what}. It reads back '{lab}' (value {val}, ShutterMode {r.get('mode_after')})."
                 "\n\nIn LabSpec's real-time display: is the Si line THERE now? (Yes: the line is there; No: gone)")
            ans = b.call("confirm", timeout_s=3600, text=q).get("answer")
            steps.append({"move": what, "asked": state, "read_label": lab, "read_value": val,
                          "shutter_mode": r.get("mode_after"), "error": r.get("error") or None,
                          "si_line_visible": ans == "yes"})
    except BridgeError as exc:
        out.update(ok=False, error=str(exc))
    finally:
        try:      # LabSpec's ShutterMode and the shutter as they were
            bf = out.get("before") or {}
            if str(bf.get("label") or "").lower() in (sa["shutter_on"].lower(), sa["shutter_off"].lower()):
                b.call("shutter", timeout_s=30, motor=motor, action="set", manual="1", **sa,
                       state="on" if str(bf["label"]).lower() == sa["shutter_on"].lower() else "off")
            if bf.get("mode") not in (None, ""):
                b.call("shutter", timeout_s=30, motor=motor, action="mode", mode=bf["mode"])
        except BridgeError:
            pass
    out["steps"] = steps
    verdict = {}
    for by in ("value", "label"):
        on = next((s for s in steps if s["move"].endswith("(ShutterMode manual)") and by in s["move"]
                   and s["asked"] == "on"), None)
        off = next((s for s in steps if s["move"].endswith("(ShutterMode manual)") and by in s["move"]
                    and s["asked"] == "off"), None)
        if on and off:
            verdict[f"by_{by}_blocks"] = bool(on["si_line_visible"] and not off["si_line_visible"])
    auto = next((s for s in steps if "automatic" in s["move"]), None)
    if auto:
        verdict["off_holds_in_automatic_mode"] = not auto["si_line_visible"]
    out["verdict"] = verdict
    if steps:
        good = [k[3:-7] for k, v in verdict.items() if k.startswith("by_") and v]
        out["next"] = (f"moving it by {' or '.join(good)} blocks the beam in manual mode: set profile shutter.by to "
                       f"'{good[0]}' if needed, then TESTING T1.2 step 2 (a background with laser_off 'shutter' must "
                       "show no Si line) before motors.laser_shutter is set" if good else
                       "no move blocked the beam while the status bar's laser was On: ls6t cannot block the laser "
                       "with this motor; keep motors.laser_shutter null and tell the ls6t maintainer")
    return out


# shutter.max_leak_frac: what may pass the closed shutter, as a fraction of the open signal. 0.5 %: on the XploRA
# (2026-11-23) the closed shutter let through 0.04-0.17 % from run to run (1.3-3.5 counts per acquisition, a burst
# more likely than a steady leak), so 0.1 % flipped between pass and fail; 0.5 % keeps a margin of about 3 over the
# worst seen and still refuses a shutter that does not block (86 % in the morning's failure)
MAX_LEAK_FRAC = 0.005
HUMAN_ANSWER_S = 0.4        # an operator's Yes faster than this was not a person reading the question


def _operator_yes(b: Bridge, text: str) -> tuple[bool, float, str | None]:
    """The operator's Yes in LabSpec, timed. 2026-11-23: a repeated laser_on took 0.2 s in all: was the question
    shown? On the instrument an answer faster than a person can read is not taken as a Yes."""
    t0 = time.time()
    r = b.call("confirm", timeout_s=3600, text=text)
    dt = round(time.time() - t0, 2)
    if r.get("answer") != "yes":
        return False, dt, "the operator declined in LabSpec"
    if config.mode() == "labspec" and dt < HUMAN_ANSWER_S:
        return False, dt, (f"LabSpec answered Yes in {dt:g} s, faster than a person reads the question: it may not "
                           "have been shown. Not taken as the operator's Yes; tell the ls6t maintainer")
    return True, dt, None


def leak_verdict(ref: dict, off: dict, detect: float, limit: float) -> dict:
    """What passes the closed shutter, as a fraction of the open signal (leak_frac), and the smallest fraction the
    measurement could have seen (sensitivity: detect_snr x the closed spectrum's noise). 'leak' when a leak above the
    limit is seen; 'not_conclusive' when the test could not see one of the limit's size; else 'blocked'."""
    r0 = float(ref["si_signal_cps"])
    sens = detect * float(off["noise_cps"]) / r0 if r0 > 0 else 1.0
    frac = max(0.0, float(off["si_signal_cps"])) / r0 if r0 > 0 else 1.0
    seen = bool(off.get("snr") is not None and off["snr"] >= detect)
    status = ("leak" if seen and frac > limit else "not_conclusive" if sens > limit else
              "blocked" if frac <= limit else "leak")
    return {"status": status, "leak_frac": round(frac, 6), "sensitivity": round(sens, 6), "max_leak_frac": limit,
            "leak_detected": seen}


def _pct(f: float) -> str:
    """A fraction as a percentage with its significant digits: 0.0004 -> '0.04 %', not '0 %'."""
    return f"{f * 100:.2g} %" if f < 0.1 else f"{f * 100:.0f} %"


VERIFY_EXPOSURE_S = 8.0     # 2026-11-23: the closed shutter passes a burst of a few counts per acquisition on every
                            # laser (532: 0.5-1.7, 637.75: 1.3-3.5, 785: 1.3-3.3); at 2 s it failed 785 nm (weak Si)
                            # on the burst alone, at 8 s it passed (0.042-0.048 %)


def shutter_verify(exposure_s: float = VERIFY_EXPOSURE_S, repeats: int = 3, burst_s: float | None = None) -> dict:
    """The spectral proof that the laser shutter blocks the beam (TESTING T1.2 step 2), with silicon under the
    objective, in focus, in the current set-up: a reference spectrum at 520 cm-1 with the laser, then `repeats`
    spectra with laser_off 'shutter' (and, with burst_s, one more of that exposure: a burst at the start or end of
    each acquisition gives the same counts at any exposure, a steady leak counts in proportion). Judged on the WORST
    closed spectrum: what passes may be at most shutter.max_leak_frac of the open signal, measured with a reference
    strong enough to see a leak of that size. Every attempt, passed or not, goes to shutter_verified.json's
    'attempts'; only a pass writes the record that lets ls6t use the shutter for this laser (shutter.py). Two
    accumulations with LabSpec's spike removal each, so a cosmic ray is not taken for the line."""
    from .profile import filter_pct, laser_key, shift_to_nm
    prof = P.load()
    gate = P.review_gate(prof) if config.mode() == "labspec" else None
    if gate:
        return {"ok": False, "error": gate}
    motor = prof["motors"].get("laser_shutter")
    if not motor:
        return {"ok": False, "error": "set motors.laser_shutter first (ls6t profile set motors.laser_shutter "
                                      "\"<name>\"): it stays unused until this test passes"}
    if runner.read_json(runner.lock_path(), None):
        return {"ok": False, "error": "a request holds the instrument: wait or cancel it"}
    ex = float(exposure_s)
    mx = float(prof["limits"].get("max_exposure_s", 60))
    repeats = int(repeats)
    if not 0 < ex <= mx or (burst_s is not None and not 0 < float(burst_s) <= mx):
        return {"ok": False, "error": "exposure_s and burst_s: above 0, within limits.max_exposure_s"}
    if not 1 <= repeats <= 10:
        return {"ok": False, "error": "repeats: 1 to 10 closed-shutter spectra"}
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    d = config.home() / "shutter_verify" / time.strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    out: dict = {"ok": False, "motor": motor, "by": str((prof.get("shutter") or {}).get("by", "label")), "folder": str(d)}
    limit = float((prof.get("shutter") or {}).get("max_leak_frac", MAX_LEAK_FRAC))
    detect = float(prof["checks"].get("detect_snr", 5.0))
    closed_plan = [(ex, f"closed_{i + 1}") for i in range(repeats)] + \
                  ([(float(burst_s), "closed_burst_check")] if burst_s else [])
    try:
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        laser, grating = runner._f(st.get("laser_nm")), runner._f(st.get("grating"))
        pct = prof.get("filters", {}).get(st.get("filter"))
        pct = pct if pct is not None else filter_pct(st.get("filter", ""))
        lk = laser_key(prof, laser)
        caps = [float(prof["limits"].get("max_filter_pct", 100))]
        if lk:
            caps.append(float(prof["lasers"][lk].get("max_filter_pct", 100)))
        if laser is None or pct is None or pct > min(caps) + 1e-9:
            return {"ok": False, "error": f"laser {st.get('laser_nm')}, filter {st.get('filter')!r}: a known laser "
                                          f"and a filter within the caps ({min(caps):g} %) first (instrument set)"}
        out.update(laser_nm=laser, grating=grating, filter=st.get("filter"), objective=st.get("objective"),
                   max_leak_frac=limit)
        txt = (f"ls6t shutter verification ({motor}): silicon under the objective, in focus. The laser fires "
               f"{1 + len(closed_plan)} times (2 accumulations each) at {laser:g} nm, filter {st.get('filter')}: once "
               f"as a reference ({ex:g} s), then {len(closed_plan)} times with the shutter closed (the Si line must "
               "then be gone). Continue?")
        yes, dt, err = _operator_yes(b, txt)
        if not yes:
            return {"ok": False, "error": err, "confirm_s": dt}
        nm = shift_to_nm(520.7, laser)
        mode = 10 + plan.SPIKE_MODES["double"] + (plan.ACQ_NO_ICS if plan.ics_off(prof, laser, grating) else 0)
        base = dict(mode=mode, accumulations=2, from_nm=round(nm, 4), to_nm=round(nm, 4),
                    ae_off=runner.ae_off(prof), **runner.motor_args(prof))

        def take(tag, seconds, extra):
            f = d / f"{tag}.txt"
            r = b.call("acquire", timeout_s=120 + 3 * seconds, out=str(f), sample=f"shutter verification {tag}",
                       exposure_s=seconds, **base, **extra)
            if not r.get("ok"):
                raise RuntimeError(f"{tag}: {r.get('error')}")
            sp = spectra.read(f)
            used = spectra.acq_exposure(sp["meta"]) or seconds
            x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
            xs, ys = spectra.sort_xy(x, sp["y"])
            sig = spectra.line_signal(xs, ys, 520.7, 30.0)
            noise = spectra.noise_sigma(ys)
            m = {"exposure_used_s": used, "si_counts": round(sig, 3),        # per accumulation, as LabSpec stores
                 "si_signal_cps": round(sig / used, 4), "noise_cps": round(noise / used, 5),
                 "snr": round(sig / noise, 1) if noise > 0 else None, "file": str(f)}
            if extra:
                m["laser_shutter"] = runner.shutter_report(r, [], verified=True)
            return m

        ref = take("reference", ex, {})
        closed = []
        for seconds, tag in closed_plan:
            m = take(tag, seconds, dict(laser_off=1))
            m.update(tag=tag, **{k: v for k, v in leak_verdict(ref, m, detect, limit).items()})
            closed.append(m)
    except RuntimeError as exc:
        out["error"] = str(exc)
        SH.attempt({"laser_nm": out.get("laser_nm"), "ok": False, "error": out["error"], "version": __version__})
        return out
    except BridgeError as exc:
        out["error"] = str(exc)
        return out
    out.update(reference=ref, closed=closed)
    fracs = [c["leak_frac"] for c in closed]
    worst = max(closed, key=lambda c: c["leak_frac"])
    out.update(worst_leak_frac=worst["leak_frac"], leak_fracs=fracs, leak_frac=worst["leak_frac"],
               ratio=worst["leak_frac"], sensitivity=max(c["sensitivity"] for c in closed),
               leak_detected=any(c["leak_detected"] for c in closed))
    # a burst at the start / end of each acquisition, or a steady leak? (2026-11-23: 1.3-3.5 counts per acquisition
    # in every run, whatever the run-to-run spread)
    same = [c for c in closed if abs(c["exposure_used_s"] - ref["exposure_used_s"]) < 1e-6]
    long_ = [c for c in closed if c["tag"] == "closed_burst_check"]
    if long_ and same:
        c_short = sum(c["si_counts"] for c in same) / len(same)
        t_ratio = long_[0]["exposure_used_s"] / same[0]["exposure_used_s"]
        c_ratio = long_[0]["si_counts"] / c_short if c_short > 0 else None
        out["burst_check"] = {
            "counts_short": round(c_short, 3), "counts_long": long_[0]["si_counts"], "exposure_ratio": round(t_ratio, 3),
            "counts_ratio": round(c_ratio, 3) if c_ratio is not None else None,
            "reading": (None if c_ratio is None else
                        "a burst per acquisition (the counts do not grow with the exposure)" if c_ratio < 1 + 0.3 * (t_ratio - 1)
                        else "a steady leak (the counts grow with the exposure)" if c_ratio > 1 + 0.7 * (t_ratio - 1)
                        else "between a burst and a steady leak")}
    statuses = [c["status"] for c in closed]
    status = "leak" if "leak" in statuses else "not_conclusive" if "not_conclusive" in statuses else "blocked"
    out["blocked"] = status == "blocked"
    attempt = {"laser_nm": out["laser_nm"], "ok": status == "blocked", "status": status, "version": __version__,
               "worst_leak_frac": out["worst_leak_frac"], "leak_fracs": fracs, "max_leak_frac": limit,
               "exposure_s": ex, "burst_check": out.get("burst_check"), "folder": str(d)}
    SH.attempt(attempt)
    if status == "not_conclusive":
        out["error"] = (f"not conclusive: the reference Si line is too weak (signal-to-noise {ref['snr']}): the "
                        f"closed-shutter spectra could only show a leak above {_pct(out['sensitivity'])} (the limit "
                        f"is {_pct(limit)}, shutter.max_leak_frac). Focus on the silicon, a stronger filter (e.g. "
                        "10 %, within the caps) or --seconds longer")
        return out
    if status == "leak":
        out["error"] = (f"NOT BLOCKED: with the shutter closed the Si line was up to {_pct(worst['leak_frac'])} of the "
                        f"reference ({', '.join(_pct(f) for f in fracs)}; the limit is {_pct(limit)}). ls6t will not "
                        "use the shutter. If the shutter is known to block (shutter-test) and the leak is a small "
                        "burst per acquisition (--burst-seconds), the limit may be the question "
                        "(shutter.max_leak_frac); otherwise keep motors.laser_shutter null and run `ls6t instrument "
                        "shutter-test` to see which move blocks")
        return out
    prev = SH.attempts(laser)[:-1]                       # before this one
    last_pass = max((i for i, a in enumerate(prev) if a.get("ok")), default=-1)
    earlier = [a for a in prev[last_pass + 1:] if not a.get("ok")]      # the failures since the previous pass
    rec = {"laser_nm": laser, "time": time.time(), "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "version": __version__, "rev": SH.REV, "motor": motor, "by": out["by"], "grating": grating,
           "filter": st.get("filter"), "objective": st.get("objective"), "leak_frac": out["worst_leak_frac"],
           "worst_leak_frac": out["worst_leak_frac"], "leak_fracs": fracs, "max_leak_frac": limit,
           "leak_detected": out["leak_detected"], "sensitivity": out["sensitivity"],
           "reference_cps": ref["si_signal_cps"], "closed_cps": [c["si_signal_cps"] for c in closed],
           "closed_counts": [c["si_counts"] for c in closed], "exposure_s": ex,
           "burst_check": out.get("burst_check"), "reference_snr": ref["snr"],
           "during_checked": (closed[0].get("laser_shutter") or {}).get("during_checked"),
           "failed_attempts_before": len(earlier),
           "attempts": [{k: a.get(k) for k in ("date", "status", "worst_leak_frac", "version")}
                        for a in SH.attempts(laser)][-20:],
           "folder": str(d)}
    SH.record(rec)
    out.update(ok=True, record=rec,
               next=f"ls6t may now use the shutter with the {laser:g} nm laser (laser_off 'shutter', laser_on, "
                    "snapshots with the laser) for "
                    f"{float((prof.get('shutter') or {}).get('verify_max_days', SH.MAX_DAYS)):g} days; verify "
                    "each laser you use the shutter with")
    if earlier:
        out["note"] = (f"{len(earlier)} earlier attempt(s) with this laser did not pass (their worst leaks: "
                       + ", ".join(_pct(a.get("worst_leak_frac") or 0) for a in earlier[-5:])
                       + "); they are kept in the record's 'attempts'")
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
            # a request measuring now would hold the shutter open again for its next spectrum: it is cancelled
            # first (2026-11-25: a laser_off during a calibration made the 637.75 nm laser hop, then the request
            # re-held and went on measuring in the hopped state)
            holder = (lock or {}).get("request")
            cancelled = None
            if holder and runner.holder_alive(holder):
                try:
                    (Path(holder) / "cancel").write_text(str(time.time()), encoding="utf-8")
                    cancelled = holder
                except OSError:
                    pass
            r = b.call("laser", timeout_s=30, state="off")
            if cancelled:
                r["cancelled_request"] = cancelled
                r["note"] = ("the request measuring now was cancelled so it does not open the shutter again; "
                             "study_status shows what it measured")
            return r
        gate = P.review_gate(prof) if config.mode() == "labspec" else None
        if gate:
            return {"ok": False, "error": gate}
        if not shutter:
            return {"ok": False, "error": "no laser shutter motor in the profile: a person confirms it with the "
                                          f"maintainer ({P.HOW}) and sets it (ls6t profile set motors.laser_shutter "
                                          "\"<name from discovery>\")"}
        cur = b.call("status", timeout_s=15, **runner.motor_args(prof))
        g = SH.gate(prof, runner._f(cur.get("laser_nm")))
        if g:      # the bridge closes it after `seconds`: only if closing is known to block the beam
            return {"ok": False, "error": g}
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
        yes, dt, err = _operator_yes(b, f"ls6t asks to open the laser shutter for {secs:g} s to show the beam (laser "
                                        f"{st.get('laser_nm')} nm, filter {st.get('filter')}, objective "
                                        f"{st.get('objective')}). Close the ls6t window to stop it at any time. "
                                        "Continue?")
        if not yes:
            return {"ok": False, "error": err, "confirm_s": dt}
        r = b.call("laser", timeout_s=30, state="on", seconds=secs, m_laser_shutter=shutter, **runner.shutter_args(prof))
        # 2026-11-23: 'seconds' (the bridge call alone, 1.5 s) read as if a repeated laser_on took 0.2 s without
        # asking; the parts are named, and 'seconds' is the whole of it, the operator's answer included
        r["laser_call_s"] = runner._f(r.get("seconds"))
        r["confirm_s"] = dt                  # how long the operator took to answer
        r["seconds"] = round(dt + (r["laser_call_s"] or 0.0), 2)
        r["open_for_s"] = secs               # how long the shutter stays open
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
    return (msg + ". After the bridge changed the laser (e.g. a visit to 785 nm), LabSpec refuses a grating the earlier "
            "laser could not use until the bridge script is restarted (2026-11-23): `ls6t instrument end-bridge`, "
            "start ls6t_bridge.vbs again in LabSpec, then set the grating again. Otherwise LabSpec may not allow this "
            "grating with this laser (or at the spectrometer's current position) (TESTING T3.16)")


def _move_timing(r: dict) -> dict:
    """How long a move took: seconds (the whole bridge call), move_call_s (MoveMotor itself), wait_s (until the motor
    reported idle), all_wait_s (until every motor did), settle_s (until the read-back showed the position asked);
    settle_stable / settle_timeout when it never did."""
    out = {"seconds": r.get("seconds")}
    for k in ("move_call_s", "wait_s", "all_wait_s", "settle_s", "settle_stable", "settle_timeout", "blocking",
              "until_target_s", "until_target_timeout", "until_target_stable"):
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


CLEARANCE_MAX_WD = 1.5        # a measured clearance over this x the nominal working distance is not believed


def clearance(b: Bridge) -> dict:
    """The objective's clearance, measured with the operator on a flat sample (the Si wafer): the operator focuses
    with the objective in place; then moves Z by hand toward the sample, slowly and watching the gap from the side,
    to the closest gap they judge safe (never touching); ls6t reads both Z positions and stores focus -> closest per
    objective (objective_clearance.json). From then on ls6t never moves toward the sample past
    limits.clearance_fraction (0.5) of it, nor past its working-distance limit (the smaller wins). Z goes back to the
    focus afterwards (away from the sample). Nothing moves toward the sample under ls6t's control here."""
    prof = P.load()
    if runner.read_json(runner.lock_path(), None):
        return {"ok": False, "error": "the instrument is busy with a request: wait or cancel it"}
    sign, known = plan.into_sign(prof)
    if not known:
        return {"ok": False, "error": "the profile does not say which Z direction approaches the objective: set "
                                      "stage.z_into_sample first (TESTING T3.17)"}
    st = b.call("status", timeout_s=15, **runner.motor_args(prof))
    obj = st.get("objective")
    if not obj:
        return {"ok": False, "error": "LabSpec reports no objective: select it in LabSpec first"}
    wd = plan.working_distance_um(prof, obj)
    q1 = (f"ls6t: clearance of the {obj} objective. Put a FLAT sample (the Si wafer) under it and focus on its surface "
          "by hand in LabSpec (white light, live video). Yes when it is in focus; No to cancel.")
    if b.call("confirm", timeout_s=3600, text=q1).get("answer") != "yes":
        return {"ok": False, "error": "cancelled by the operator", "objective": obj}
    z_focus = _fnum(b.call("status", timeout_s=15, **runner.motor_args(prof)).get("z_um"))
    q2 = (f"ls6t: now move Z TOWARD the sample by hand (LabSpec's Z control or the joystick), slowly, watching the "
          f"gap between the {obj} objective and the sample from the side, to the closest gap you judge safe. NEVER "
          f"let it touch. Its nominal working distance is about {wd or 0:g} µm. Yes when you are there; No to cancel "
          "(then move back up yourself).")
    if b.call("confirm", timeout_s=3600, text=q2).get("answer") != "yes":
        return {"ok": False, "error": "cancelled by the operator: move Z back up (away from the sample) by hand",
                "objective": obj}
    z_close = _fnum(b.call("status", timeout_s=15, **runner.motor_args(prof)).get("z_um"))
    out = {"objective": obj, "z_focus_um": z_focus, "z_closest_um": z_close, "nominal_wd_um": wd}
    if z_focus is None or z_close is None:
        return {"ok": False, "error": "the Z position could not be read", **out}
    gap = round((z_close - z_focus) * sign, 2)
    out["clearance_um"] = gap
    err = None
    if gap <= 0:
        err = ("Z did not move toward the sample (" + (f"{-gap:g} µm away" if gap < 0 else "unchanged") + "): check "
               "stage.z_into_sample, and move toward the sample")
    elif wd and gap > CLEARANCE_MAX_WD * wd:
        err = (f"{gap:g} µm is more than {CLEARANCE_MAX_WD:g} x the nominal working distance ({wd:g} µm): another "
               "objective in place, or stage.z_into_sample the wrong way round")
    back = b.call("move", timeout_s=120, motor=prof["motors"]["z"], mode="value", value=round(z_focus, 3))
    out["z_back_um"] = _fnum(back.get("value"))
    if not back.get("ok"):
        out["warning"] = f"Z could not be moved back to the focus ({back.get('error')}): move it up by hand"
    if err:
        return {"ok": False, "error": err + "; nothing stored", **out}
    store_ = plan.clearance_store()
    prev = store_.get(str(obj))
    store_[str(obj)] = {"clearance_um": gap, "z_focus_um": z_focus, "z_closest_um": z_close, "nominal_wd_um": wd,
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    plan.clearance_store(store_)
    zl = plan.z_limits(prof, obj)
    out.update(ok=True, stored=True, previous=prev, toward_limit_um=zl["toward_um"], toward_from=zl["toward_from"],
               note=(f"ls6t now moves the {obj} at most {zl['toward_um']:g} µm toward the sample from a focus "
                     f"({zl['toward_from']})"))
    if wd and gap < 0.3 * wd:
        out["warning"] = (f"the clearance ({gap:g} µm) is under a third of the nominal working distance ({wd:g} µm): "
                          "right for a thick or tilted holder, but check the focus was on the sample's surface")
    return out


SCALE_STEP_FRACTION = 0.4      # video_scale's default move: 0.4 x the field's smaller side (x100 ~23 µm, x10 ~240 µm)
SCALE_MAX_FRACTION = 0.5       # a move over half the field's width (X) or height (Y) is refused
SCALE_AGREE = 0.01             # pixel aspect (X / Y µm per px) within 1 % of other objectives on the same camera
SCALE_ROT_DEG = 0.5            # and the image's rotation within 0.5 degrees


def _sig2(v: float) -> float:
    """v rounded to two significant figures (172.6 -> 170, 17.3 -> 17)."""
    return float(f"{v:.2g}")


def video_scale(b: Bridge, step_um=None) -> dict:
    """The video's µm per pixel and axis directions, measured: a frame, the stage moved step_um in +X (no laser,
    nothing else moves), a frame, back; the same in +Y; the image shift by phase correlation. The default move is 0.4
    x the field's smaller side (its measured scale, else OLConfig's field: x100 85 x 58 µm -> 23 µm, x10 -> 240 µm):
    on 2026-11-23 the x100's 17 µm gave a 4 % X / Y mismatch (stage positioning error) that 25 µm removed. A move over
    half the field's width or height is refused (35 µm left the x100's 58 µm-tall field in Y). The pixel aspect and
    the image's rotation belong to the camera: a result that disagrees with another objective's on the same camera
    (1 %, 0.5 degrees) is stored but flagged. Stored per objective and camera
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
        width = height = None           # the field in µm: the measured scale, else OLConfig's field
        sa = V.stage_axes(f0, prof, r0.get("objective"), x0, y0, r0.get("camera"))
        if sa is not None:
            width, height = float(abs(sa["xs"][-1] - sa["xs"][0])), float(abs(sa["ys"][-1] - sa["ys"][0]))
        elif f0["um"] and len(f0["xs"]) > 1:
            span = float(abs(f0["xs"][-1] - f0["xs"][0]))
            if abs(span / (f0["width"] - 1) - 1) > 0.01:      # LabSpec's axes, when not its one-per-pixel ones
                width, height = span, float(abs(f0["ys"][-1] - f0["ys"][0]))
        side = min(x for x in (width, height) if x) if (width or height) else None
        good = _sig2(SCALE_STEP_FRACTION * side) if side else None
        if step_um:
            step = float(step_um)
        elif good:
            step = good
        else:
            return {"ok": False, "error": f"no field of view known for objective {r0.get('objective')!r}: give the "
                                          "move (--um), about 0.4 x the field's smaller side; nothing moved"}
        if not 1 <= step <= cap:
            return {"ok": False, "error": f"move: 1 to {cap:g} µm; nothing moved"}
        for size, axis, word in ((width, "X", "wide"), (height, "Y", "tall")):
            if size and step > SCALE_MAX_FRACTION * size:
                return {"ok": False, "error": f"{step:g} µm is over half the field in {axis} ({size:.0f} µm {word}): "
                                              f"the features would leave the view; about {good:g} µm works; "
                                              "nothing moved"}
        out = {"ok": True, "objective": r0.get("objective"), "camera": r0.get("camera"), "step_um": step,
               "step_from": "asked" if step_um else f"0.4 x the field's smaller side ({side:.0f} µm)",
               "folder": str(folder)}
        res = {}
        for axis, dx, dy in (("x", step, 0.0), ("y", 0.0, step)):
            m = b.call("move_xy", timeout_s=120, x_um=round(x0 + dx, 3), y_um=round(y0 + dy, 3),
                       **runner.motor_args(prof))
            if not m.get("ok"):
                raise BridgeError(f"move in {axis}: {m.get('error')}")
            f1, _ = frame(f"plus_{axis}")
            b.call("move_xy", timeout_s=120, x_um=round(x0, 3), y_um=round(y0, 3), **runner.motor_args(prof))
            drow, dcol = V.image_shift(f0["img"], f1["img"], axis=axis)
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
        # the camera's own properties: pixel aspect and rotation, the same whatever the objective
        aspect = abs(sx) / abs(ty)
        rot = math.degrees(math.atan2(res["x"]["shift_px"][1], abs(res["x"]["shift_px"][0])))
        out.update(aspect=round(aspect, 4), rotation_deg=round(rot, 2))
        store = V.scale_store()
        key = f"{r0.get('objective')}@{r0.get('camera')}"
        disagree = []
        for k, c in sorted(store.items()):
            if k == key or not V._same_camera(k, r0.get("camera")) or not (c.get("sx") and c.get("ty")):
                continue
            a2 = abs(float(c["sx"])) / abs(float(c["ty"]))
            if abs(aspect / a2 - 1) > SCALE_AGREE:
                disagree.append(f"pixel aspect {aspect:.4f} vs {a2:.4f} with {k} ({abs(aspect / a2 - 1) * 100:.1f} %)")
            if c.get("rotation_deg") is not None and abs(rot - float(c["rotation_deg"])) > SCALE_ROT_DEG:
                disagree.append(f"rotation {rot:.2f} vs {float(c['rotation_deg']):.2f} degrees with {k}")
        store[key] = {"sx": sx, "ty": ty, "aspect": round(aspect, 5), "rotation_deg": round(rot, 3),
                      "measured": time.strftime("%Y-%m-%d %H:%M:%S"), "step_um": step,
                      **({"disagrees": disagree} if disagree else {})}
        V.scale_store(store)
        out["stored"] = key
        if disagree:
            out["warnings"] = [("stored, but it disagrees with the same camera's other measurements: "
                                + "; ".join(disagree) + ". These belong to the camera, not the lens: repeat with a "
                                "larger move (--um, up to half the field) on a view with clear structure; the stage's "
                                "positioning error weighs more on small moves (2026-11-23: x100 17 µm gave 4 %, 25 µm "
                                "agreed)")]
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
                f"{want}. Bring the sample into focus by eye with {want} before any focus scan: a scan starts from "
                f"the old focus and searches only a few µm (2026-11-23: x10 -> x100, no focus found). Cancel: "
                f"nothing changes.")
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
            retried = None
            if r.get("ok") and not ok and what == "grating":
                # the laser selected once more, then the grating again (2026-11-23: 2400 refused after 785 nm)
                r, retried = runner.retry_grating(b.call, prof, dict(motor=motor, mode=mode, value=value,
                                                                     **runner.move_extra(prof, motor, False)), value)
                got = r.get("label") if mode == "label" else r.get("value")
                ok = bool(r.get("ok")) and P.same_position(got, value)
            done.append({"set": what, "asked": value, "got": got, "ok": ok, "error": r.get("error") or None,
                         **_move_timing(r), **({"retried_after_laser_reselect": retried} if retried else {})})
            if r.get("ok") and what == "grating":
                runner.note_grating_set(value, got, "instrument set")
            if r.get("ok") and not ok and what == "grating":
                done[-1]["error"] = _grating_short(b, prof, value, got, r)
            if not r.get("ok"):
                if r.get("stopped_by"):
                    done[-1]["error"] = f"stopped by the {r.get('stopped_by')}"
                break
        # the illumination: as asked, or, after an objective change, set again to what LabSpec showed before (it has
        # shown bright field with the light in dark field after such a change, 2026-11-23)
        illum = None
        obj_changed = any(d["set"] == "objective" and d["ok"] and not d.get("already_set") for d in done)
        if "illumination" in s:
            illum = illumination_set(b, prof, s["illumination"], before.get("illumination"))
        elif obj_changed and mot.get("illumination") and before.get("illumination"):
            illum = illumination_set(b, prof, before["illumination"], before.get("illumination"))
            illum["why"] = "set again after the objective change"
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
        if illum is not None:
            out["illumination"] = illum
            out["ok"] = out["ok"] and bool(illum.get("ok"))
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
        if not moves and illum is None:
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
        account = str(Path.home())
        if '"' in account:
            account = ""
        header = (f"' {name} - labspec-translator (ls6t) {__version__}: {what}.\n"
                  "' Generated by `ls6t bridge install`; add it in LabSpec 6 under Processing > Scripts.\n"
                  "' The folders are those of the Windows account that starts the script: LabSpec's script list is\n"
                  "' the same for every account, so another account may start this copy (2026-11-23: ferralis ran\n"
                  "' jalavall's, whose heartbeat went to jalavall's folder while ferralis's ls6t waited). That\n"
                  "' account's own %USERPROFILE%\\ls6t is used when it exists; else the installing account's.\n"
                  f'Const LS6T_BRIDGE_DIR_INSTALLED = "{bridge_dir}"\n'
                  f'Const LS6T_HOME_DIR_INSTALLED = "{home_dir}"\n'
                  f'Const LS6T_ACCOUNT_INSTALLED = "{account}"\n'
                  f'Const LS6T_VERSION = "{__version__}"\n'
                  f"Const LS6T_PROTOCOL = {BRIDGE_PROTOCOL}\n"
                  "Dim LS6T_BRIDGE_DIR, LS6T_HOME_DIR, LS6T_ACCOUNT, LS6T_DIR_NOTE, ls6tSh\n"
                  "LS6T_BRIDGE_DIR = LS6T_BRIDGE_DIR_INSTALLED\n"
                  "LS6T_HOME_DIR = LS6T_HOME_DIR_INSTALLED\n"
                  "LS6T_ACCOUNT = LS6T_ACCOUNT_INSTALLED\n"
                  "LS6T_DIR_NOTE = \"\"\n"
                  "On Error Resume Next\n"
                  "Set ls6tSh = CreateObject(\"WScript.Shell\")\n"
                  "LS6T_ACCOUNT = ls6tSh.ExpandEnvironmentStrings(\"%USERPROFILE%\")\n"
                  "If Err.Number <> 0 Or LS6T_ACCOUNT = \"\" Or LS6T_ACCOUNT = \"%USERPROFILE%\" Then "
                  "LS6T_ACCOUNT = LS6T_ACCOUNT_INSTALLED\n"
                  "Err.Clear\n"
                  "If LCase(LS6T_ACCOUNT) <> LCase(LS6T_ACCOUNT_INSTALLED) And LS6T_ACCOUNT_INSTALLED <> \"\" Then\n"
                  "    If CreateObject(\"Scripting.FileSystemObject\").FolderExists(LS6T_ACCOUNT & \"\\ls6t\") Then\n"
                  "        LS6T_HOME_DIR = LS6T_ACCOUNT & \"\\ls6t\"\n"
                  "        LS6T_BRIDGE_DIR = LS6T_ACCOUNT & \"\\ls6t\\bridge\"\n"
                  "        LS6T_DIR_NOTE = \"script installed by \" & LS6T_ACCOUNT_INSTALLED & \", run by \" & "
                  "LS6T_ACCOUNT & \": that account's folders\"\n"
                  "    Else\n"
                  "        LS6T_DIR_NOTE = \"script installed by \" & LS6T_ACCOUNT_INSTALLED & \", run by \" & "
                  "LS6T_ACCOUNT & \", who has no ls6t folder: the installing account's folders\"\n"
                  "    End If\n"
                  "End If\n"
                  "Err.Clear\n"
                  "On Error GoTo 0\n\n")
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
                                              f"sets it from the discovery dump once confirmed ({P.HOW})"}
            g = SH.gate(prof, runner._f(st.get("laser_nm")))
            if g:
                return {"ok": False, "error": g}
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


def _unquoted_json(text: str):
    """JSON whose double quotes the shell took away (Windows PowerShell 5.1 strips them from arguments to programs:
    '{"637.75@600": {"moved_sd_cm1": 0.3}}' arrives as {637.75@600: {moved_sd_cm1: 0.3}}): every bare word that is
    not a number, true, false or null is quoted again. None when even that is not JSON."""
    import re
    def q(m):
        w = m.group(2).strip()
        if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?|true|false|null", w):
            return m.group(1) + w
        return m.group(1) + json.dumps(w)
    try:
        return json.loads(re.sub(r"([{\[,:]\s*)([^\s\"{}\[\],:][^{}\[\],:]*?)(?=\s*[,:}\]])", q, text))
    except ValueError:
        return None


def profile_set(key: str, value_json: str) -> dict:
    """A person's edit of one profile value: `ls6t profile set video.laser_offset_um "[1.4, -0.6]"`; @file.json reads
    the value from a file. A value that should be JSON (it starts with { or [, or the key holds a group or a list
    now) is never stored as text (2026-11-25: spectrometer.repeatability set from PowerShell arrived without its
    quotes, was stored as a string, and every request then failed with "'str' object has no attribute 'get'")."""
    note = None
    if str(value_json).startswith("@") and Path(str(value_json)[1:]).is_file():
        value_json = Path(str(value_json)[1:]).read_text(encoding="utf-8-sig")
    prof = P.load()
    old = runner.lookup(prof, key)
    try:
        value = json.loads(value_json)
    except ValueError:
        value = value_json
        if str(value_json).strip()[:1] in ("{", "[") or isinstance(old, (dict, list)):
            value = _unquoted_json(str(value_json))
            if value is None:
                return {"ok": False, "key": key, "error": f"{key} takes JSON ({type(old).__name__ if old is not None else 'an object or a list'}), and this is not JSON: {value_json!r}. "
                        "Windows PowerShell removes the double quotes inside '...': write them as \\\" "
                        "('{\\\"a\\\": 1}'), or put the value in a file and give @file.json"}
            note = ("the value had lost its double quotes (Windows PowerShell does that): they were put back; "
                    "check 'new'")
    if isinstance(old, dict) and not isinstance(value, dict) or isinstance(old, list) and not isinstance(value, list):
        return {"ok": False, "key": key, "error": f"{key} is a {type(old).__name__}; {value!r} is not"}
    P.set_key(prof, key, value)
    was_auto = bool(prof.pop("auto", None))
    if was_auto:
        prof["source"] = str(prof.get("source", "")).replace("automatic", "changed by a person; first automatic", 1)
    out = {"ok": True, "key": key, "old": old, "new": value, "profile": str(P.save(prof))}
    if note:
        out["repaired"] = note
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
    from . import perf, qc
    return {"spectral_corrections": corr, "backgrounds": BG.index(), "performance": perf.status(),
            "horiba_qc": qc.summary(P.load()),
            "beam_offsets": SP.status(P.load())["records"], "labspec_configs": _configs(),
            "alignment": {k: v.get("last_check") for k, v in alignment_status()["lasers"].items()}}


def references_clear(what: str, name: str | None = None) -> dict:
    """A person's clean-up: what = correction (all, or laser@grating), background (a name), spot (all)."""
    from . import background as BG
    from . import spot as SP
    from . import xcal
    if what == "correction":
        if name:
            parts = name.split("@")
            if len(parts) == 3:          # laser@grating@centre_nm: that window's correction only
                return {"cleared": xcal.clear_centre(float(parts[0]), float(parts[1]), float(parts[2]))}
            las, gr = parts
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
    try:        # the version installed for this account: a bridge started from another account's copy compares with it
        (config.home() / "installed_version.txt").write_text(__version__, encoding="ascii")
    except OSError:
        pass
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
