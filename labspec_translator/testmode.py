"""testmode.py — the instrument tests of TESTING.md from the chat: the MCP tool `hardware_test`, and `ls6t test`.
Version: 2026.11.20.8

Off unless a person turns it on for an app (LS6T_TEST_MODE=1 in its MCP entry: `ls6t connect APP --test`). Then the
LLM walks the maintainer through TESTING.md: it reads a test's instructions and pass criteria (`step`), asks the
maintainer to act in LabSpec where only a person can, checks what it can itself (bridge state, read-backs, spectra,
snapshots), runs single bridge commands (`call`) and records the outcome (`record`). The record is a log
(LS6T_HOME/testing/log.jsonl) and a report with TESTING.md's results table filled in (testing/report.md).

`call` is `ls6t bridge call` with more guards, since an LLM asks for it:
- refused while a request holds the instrument, for `quit`, and for any verb outside the bridge's list;
- files go to LS6T_OUT/testing (the path the LLM gives is ignored);
- moves: the filter only by label and within the caps; X/Y within limits.max_xy_um and Z within limits.max_dz_um
  of where they are;
- anything that can fire the laser (acquire without laser_off, autofocus, guide, map_native, video with laser_s or
  during_acq_s): the current filter within the caps, exposure and accumulations within the limits, and the
  operator's Yes in LabSpec every time, whatever LS6T_CONFIRM says.
What changes ls6t's own settings stays with a person (profile set/import, alignment record, references clear,
bridge install): the LLM proposes the command.
TESTING.md is read from the source tree, the installer's docs folder, or LS6T_TESTING_MD.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, config

RESULTS = ("pass", "fail", "partial", "skipped", "blocked")
VERBS = ("ping", "status", "discover", "move", "move_xy", "autofocus", "acquire", "view", "save", "remove", "confirm",
         "message", "safe", "video", "camera", "map_native", "guide", "notice", "resume", "last_error")
PATH_KEYS = ("out", "native", "path")


def enabled() -> bool:
    return os.environ.get("LS6T_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on")


def folder() -> Path:
    return (config.out_dir() / "sim_testing") if config.mode() == "sim" else (config.home() / "testing")


def log_path() -> Path:
    return folder() / "log.jsonl"


def report_path() -> Path:
    return folder() / "report.md"


# --------------------------------------------------------------------------------------------- the plan

def testing_md() -> Path | None:
    here = Path(__file__).resolve().parent
    cands = [os.environ.get("LS6T_TESTING_MD"), here.parent / "TESTING.md",
             Path(sys.executable).resolve().parent.parent / "docs" / "TESTING.md", here.parent / "docs" / "TESTING.md"]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


_SECTION = re.compile(r"^## (T\d+[a-z]?)\.\s+(.*)$")
_BOLD = re.compile(r"^\*\*(T\d+[a-z]?\.\d+)\s+(.*?)\*\*\s*(.*)$")
_ROW = re.compile(r"^\|\s*(T\d+[a-z]?\.\d+)([^|]*)\|(.*)$")
_QROW = re.compile(r"^\|\s*(Q\d+)\s*\|(.*)\|\s*([^|]*)\|\s*$")


def parse(text: str) -> dict:
    """TESTING.md as {sections, steps, questions}: each step with its section, title, text, laser flag and the open
    questions it answers."""
    sections, steps, questions = {}, {}, {}
    cur_sec = cur = None
    header_row = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            m = _SECTION.match(line)
            cur = None
            cur_sec = m.group(1) if m else None
            if m:
                sections[cur_sec] = {"title": m.group(2), "text": [], "steps": []}
            continue
        q = _QROW.match(line)
        if q and not cur_sec:
            questions[q.group(1)] = {"question": q.group(2).strip(), "tests": q.group(3).strip()}
            continue
        if not cur_sec:
            continue
        m = _BOLD.match(line)
        if m:
            cur = m.group(1)
            steps[cur] = {"id": cur, "section": cur_sec, "title": m.group(2).rstrip(".").strip(),
                          "text": [line]}
            sections[cur_sec]["steps"].append(cur)
            continue
        if line.startswith("|"):
            if re.match(r"^\|\s*Step\s*\||^\|\s*ID\s*\|", line) or re.match(r"^\|\s*(Step|Prompt|Test)\b", line):
                header_row = [c.strip() for c in line.strip("|").split("|")]
            r = _ROW.match(line)
            if r:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                cur = r.group(1)
                body = cells[1:]
                if header_row and len(header_row) == len(cells):
                    body = [f"{h}: {c}" for h, c in zip(header_row[1:], cells[1:])]
                extra = r.group(2).strip()          # e.g. "(**Q12**)" after the ID
                steps[cur] = {"id": cur, "section": cur_sec, "title": cells[1][:80],
                              "text": [(extra + " " if extra else "") + " | ".join(body)]}
                sections[cur_sec]["steps"].append(cur)
                cur = None
                continue
        if cur:
            steps[cur]["text"].append(line)
        else:
            sections[cur_sec]["text"].append(line)
    # a section without separate steps (T5) is one step
    for sid, s in sections.items():
        if not s["steps"]:
            steps[sid] = {"id": sid, "section": sid, "title": s["title"], "text": s["text"]}
            s["steps"].append(sid)
    for st in steps.values():
        sec = sections[st["section"]]
        body = "\n".join(st["text"]).strip()
        st["text"] = body
        st["section_title"] = sec["title"]
        st["section_text"] = "\n".join(sec["text"]).strip()
        own = st["title"] + " " + body[:200]
        st["laser"] = bool(re.search(r"\bLASER\b", own)) or (
            "LASER" in sec["title"] and "no laser" not in sec["title"] and "no laser" not in own.lower())
        st["questions"] = sorted(set(re.findall(r"\bQ\d+\b", st["title"] + " " + body)), key=lambda q: int(q[1:]))
    return {"sections": sections, "steps": steps, "questions": questions}


def _id_key(sid: str):
    m = re.match(r"T(\d+)([a-z]?)(?:\.(\d+))?", sid)
    return (int(m.group(1)), m.group(2), int(m.group(3) or 0)) if m else (999, "", 0)


def load_plan() -> dict:
    p = testing_md()
    if not p:
        raise FileNotFoundError("TESTING.md not found (source tree, the installer's docs folder): set LS6T_TESTING_MD")
    plan = parse(p.read_text(encoding="utf-8"))
    plan["file"] = str(p)
    return plan


# --------------------------------------------------------------------------------------------- the log

def _append(entry: dict) -> dict:
    folder().mkdir(parents=True, exist_ok=True)
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": __version__, "mode": config.mode(), **entry}
    with open(log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def entries() -> list:
    try:
        return [json.loads(x) for x in log_path().read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def latest() -> dict:
    """The last record per step."""
    out = {}
    for e in entries():
        if e.get("kind") == "record":
            out[e["step"]] = e
    return out


def record(step: str, result: str, notes: str = "", values: dict | None = None, answers: dict | None = None,
           who: str = "") -> dict:
    plan = load_plan()
    if step not in plan["steps"]:
        raise ValueError(f"step {step!r} is not in TESTING.md ({plan['file']})")
    if result not in RESULTS:
        raise ValueError(f"result: {', '.join(RESULTS)}")
    bad = [q for q in (answers or {}) if q not in plan["questions"]]
    if bad:
        raise ValueError(f"not open questions of TESTING.md: {', '.join(bad)}")
    e = _append({"kind": "record", "step": step, "result": result, "notes": notes, "values": values or {},
                 "answers": answers or {}, "who": who})
    report()
    return {"ok": True, "recorded": e, "report": str(report_path())}


def report() -> dict:
    plan = load_plan()
    last = latest()
    calls: dict = {}
    for e in entries():
        if e.get("kind") == "call":
            calls[e.get("step") or "-"] = calls.get(e.get("step") or "-", 0) + 1
    ids = sorted(plan["steps"], key=_id_key)
    lines = [f"# ls6t instrument tests: report", "",
             f"Written {time.strftime('%Y-%m-%d %H:%M')} by ls6t {__version__} from {log_path()} "
             f"({sum(calls.values())} bridge commands logged). Plan: {plan['file']}.", "",
             "| ID | Date | Tester / maintainer | Result | Notes and values | Commands |", "|---|---|---|---|---|---|"]
    counts = {r: 0 for r in RESULTS}
    for sid in ids:
        r = last.get(sid)
        if r:
            counts[r["result"]] += 1
            vals = "; ".join(f"{k} = {v}" for k, v in (r.get("values") or {}).items())
            qa = "; ".join(f"{q}: {a}" for q, a in (r.get("answers") or {}).items())
            text = "; ".join(x for x in (r.get("notes"), vals, qa) if x).replace("|", "/").replace("\n", " ")
            lines.append(f"| {sid} | {r['time'][:10]} | {r.get('who') or ''} | {r['result']} | {text} | "
                         f"{calls.get(sid, 0) or ''} |")
        else:
            lines.append(f"| {sid} | | | | | {calls.get(sid, 0) or ''} |")
    lines += ["", "## Answers to the open questions", "", "| Q | Question | Answer | From | Date |", "|---|---|---|---|---|"]
    answered = {}
    for e in entries():
        if e.get("kind") == "record":
            for q, a in (e.get("answers") or {}).items():
                answered[q] = (a, e["step"], e["time"][:10])
    for q in sorted(plan["questions"], key=lambda x: int(x[1:])):
        a = answered.get(q)
        qtext = plan["questions"][q]["question"].replace("|", "/")
        lines.append(f"| {q} | {qtext} | {(a[0] if a else '').replace('|', '/')} | {a[1] if a else ''} | "
                     f"{a[2] if a else ''} |")
    folder().mkdir(parents=True, exist_ok=True)
    report_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "report": str(report_path()), "log": str(log_path()), "steps": len(ids),
            "recorded": len(last), "results": counts, "questions_answered": len(answered),
            "questions": len(plan["questions"])}


# --------------------------------------------------------------------------------------------- the plan, step by step

CLI_TO_TOOLS = {
    "ls6t status": "hardware_test(action='status')",
    "ls6t instrument status / discover / resume": "instrument(action=...)",
    "ls6t instrument last-error": "hardware_test(action='last_error')",
    "ls6t instrument config list/save/load NAME": "hardware_test(action='config', params={'action':..., 'name':...})",
    "ls6t instrument snapshot [--laser / --extended]": "snapshot(with_laser=..., extended=...)",
    "ls6t bridge call VERB k=v ...": "hardware_test(action='call', step=..., verb=VERB, params={k: v})",
    "ls6t validate / run / status REQUEST / cancel / inputs": "validate_studies / run_studies / study_status / cancel / get_inputs",
    "ls6t bench": "run_benchmarks",
    "ls6t spot status / alignment status / references show": "spot_status / alignment_status / references",
    "ls6t profile set|import, alignment record, references clear, bridge install, ls6t-typelib, pytest":
        "a person runs them: propose the exact command",
}


def plan_summary() -> dict:
    plan = load_plan()
    last = latest()
    out = []
    for sid in sorted(plan["steps"], key=_id_key):
        st = plan["steps"][sid]
        r = last.get(sid)
        out.append({"id": sid, "title": st["title"], "laser": st["laser"], "questions": st["questions"],
                    "result": r["result"] if r else None, "date": r["time"][:10] if r else None})
    nxt = next((s["id"] for s in out if not s["result"]), None)
    return {"file": plan["file"], "steps": out, "next": nxt, "report": str(report_path()),
            "sections": {k: v["title"] for k, v in plan["sections"].items()},
            "how": "go in order (T0 first); for each: step, tell the maintainer what to do, check, record"}


def step_info(sid: str) -> dict:
    plan = load_plan()
    if sid not in plan["steps"]:
        near = [s for s in plan["steps"] if s.startswith(sid)]
        raise ValueError(f"step {sid!r} is not in TESTING.md" + (f"; did you mean {', '.join(near)}" if near else ""))
    st = dict(plan["steps"][sid])
    st["questions"] = {q: plan["questions"].get(q, {}).get("question") for q in st["questions"]}
    st["last_record"] = latest().get(sid)
    st["commands_in_chat"] = CLI_TO_TOOLS
    st["remember"] = ("The maintainer is at the instrument and acts in LabSpec when the step says so; ask them to "
                      "tell you when done, and ask what only a person can see. Steps marked laser: follow the "
                      "facility's laser rules; every laser command asks the operator in LabSpec. Then record the "
                      "result with the values and the answers to the step's questions.")
    return st


# --------------------------------------------------------------------------------------------- one bridge command

def _caps(prof: dict, st: dict) -> tuple:
    from .profile import filter_pct, laser_key
    lk = laser_key(prof, st.get("laser_nm"))
    caps = [prof["limits"].get("max_filter_pct", 100.0)]
    if lk:
        caps.append(prof["lasers"][lk].get("max_filter_pct", 100))
    cap = min(float(c) for c in caps if c is not None)

    def pct(label):
        v = prof.get("filters", {}).get(label)
        return v if v is not None else filter_pct(label)
    return cap, pct


def _num(v, what):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: a number, not {v!r}") from None


def call(verb: str, params: dict | None = None, step_id: str | None = None, timeout_s: float = 120) -> dict:
    from . import api, runner
    from . import profile as P
    from .bridge import Bridge, BridgeError
    p = {str(k): v for k, v in (params or {}).items()}
    if verb == "quit":
        return {"ok": False, "error": "quit ends the bridge: the maintainer stops it in LabSpec when a test says so"}
    if verb not in VERBS:
        return {"ok": False, "error": f"verb {verb!r}: one of {', '.join(VERBS)}"}
    if runner.read_json(runner.lock_path(), None):
        return {"ok": False, "error": "a request holds the instrument: wait for it or cancel it"}
    prof = P.load()
    if verb == "move" and str(p.get("motor", "")).lower() in prof["motors"] and prof["motors"].get(
            str(p.get("motor", "")).lower()):
        p["motor"] = prof["motors"][str(p["motor"]).lower()]       # a role (filter, z...) to LabSpec's name
    if verb in ("camera", "video") and p.get("camera") not in (None, ""):
        num, err = P.camera_number(prof, p["camera"])
        if err:
            return {"ok": False, "error": err}
        p["camera"] = num
    b = Bridge()
    if config.mode() == "sim":
        from . import sim as S
        S.ensure(b.dir)
    tag = f"{time.strftime('%Y%m%d_%H%M%S')}_{verb}"
    for k in PATH_KEYS:
        if k in p or (k == "out" and verb in ("acquire", "video", "discover", "map_native", "guide")) or \
                (k == "path" and verb == "save"):
            ext = {"save": ".l6s", "discover": ".txt"}.get(verb, "")
            target = folder() / "files" / (tag + ("" if k == "out" else f".{k}") + ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            p[k] = str(target)
    try:
        st = b.call("status", timeout_s=15, **runner.motor_args(prof))
        if not st.get("ok"):
            return {"ok": False, "error": f"status: {st.get('error')}"}
        motors, lim = prof["motors"], prof["limits"]
        cap, pct = _caps(prof, st)
        if verb == "move":
            m = p.get("motor")
            if m == motors.get("filter"):
                if p.get("mode") != "label":
                    return {"ok": False, "error": "the filter moves by label here (mode=label, value=e.g. 1%)"}
                v = pct(p.get("value"))
                if v is None or v > cap:
                    return {"ok": False, "error": f"filter {p.get('value')!r}: unknown or above the cap ({cap:g} %)"}
            for ax, key, lim_key in (("x", "x_um", "max_xy_um"), ("y", "y_um", "max_xy_um"), ("z", "z_um", "max_dz_um")):
                if m == motors.get(ax):
                    d = abs(_num(p.get("value"), "value") - _num(st.get(key), key))
                    if d > float(lim[lim_key]):
                        return {"ok": False, "error": f"{ax.upper()} by {d:g} µm: more than limits.{lim_key} "
                                                      f"({float(lim[lim_key]):g} µm) from where it is"}
        if verb == "move_xy":
            for key in ("x_um", "y_um"):
                if key in p:
                    d = abs(_num(p[key], key) - _num(st.get(key), key))
                    if d > float(lim["max_xy_um"]):
                        return {"ok": False, "error": f"{key} by {d:g} µm: more than limits.max_xy_um"}
        laser = (verb in ("autofocus", "guide", "map_native")
                 or (verb == "acquire" and str(p.get("laser_off", "0")) != "1")
                 or (verb == "video" and (float(p.get("laser_s") or 0) > 0 or float(p.get("during_acq_s") or 0) > 0)))
        if laser:
            f = pct(st.get("filter", ""))
            if f is None or f > cap:
                return {"ok": False, "error": f"the current filter {st.get('filter')!r} is unknown or above the cap "
                                              f"({cap:g} %): move to a stronger filter first"}
            if float(p.get("exposure_s") or 0) > float(lim["max_exposure_s"]):
                return {"ok": False, "error": f"exposure_s above limits.max_exposure_s ({lim['max_exposure_s']})"}
            if float(p.get("accumulations") or 1) > float(lim["max_accumulations"]):
                return {"ok": False, "error": "accumulations above limits.max_accumulations"}
            if verb in ("video",) and not p.get("m_laser_shutter"):
                p["m_laser_shutter"] = motors.get("laser_shutter") or ""
            ans = b.call("confirm", timeout_s=3600,
                         text=f"ls6t test {step_id or ''}: the LLM asks to run '{verb}' with the laser on "
                              f"(laser {st.get('laser_nm')} nm, filter {st.get('filter')}, objective "
                              f"{st.get('objective')}). Continue?")
            if ans.get("answer") != "yes":
                _append({"kind": "call", "step": step_id, "verb": verb, "params": p, "ok": False, "declined": True})
                return {"ok": False, "error": "the operator declined in LabSpec"}
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    t = min(max(float(timeout_s), 5.0), 900.0)
    r = api.bridge_call(verb, p, timeout_s=t, allow_laser=True)
    out = dict(r)
    if verb == "acquire" and r.get("ok") and p.get("out"):
        out["summary"] = _spectrum_summary(Path(p["out"]), st.get("laser_nm"))
    _append({"kind": "call", "step": step_id, "verb": verb, "params": p, "ok": bool(r.get("ok")),
             "error": r.get("error")})
    return out


def _spectrum_summary(path: Path, laser_nm) -> dict:
    from . import spectra
    try:
        cand = [path] + sorted(path.parent.glob(path.name + "*"))
        f = next(c for c in cand if c.is_file())
        d = spectra.read(f)
        x, y = d["x"], d["y"]
        if not len(y):
            return {"file": str(f), "points": 0}
        unit = d["meta"].get("x_unit", "nm")
        shift, u = spectra.to_shift(x, unit, float(laser_nm) if laser_nm else None)
        i = int(y.argmax())
        return {"file": str(f), "points": int(len(y)), "max_counts": float(y.max()), "at": round(float(shift[i]), 2),
                "unit": u, "min_counts": float(y.min()), "noise_sigma": round(float(spectra.noise_sigma(y)), 2)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------------------------- the tool

def hardware_test(action: str, step: str | None = None, verb: str | None = None, params: dict | None = None,
                  result: str | None = None, notes: str = "", values: dict | None = None,
                  answers: dict | None = None, who: str = "", timeout_s: float = 120) -> dict:
    from . import api
    if not enabled():
        return {"ok": False, "error": "test mode is off: a person turns it on with `ls6t connect <app> --test`"}
    if action == "plan":
        return plan_summary()
    if action == "step":
        return step_info(step or "")
    if action == "call":
        if not verb:
            return {"ok": False, "error": "call needs verb (and params)"}
        return call(verb, params, step, timeout_s)
    if action == "record":
        return record(step or "", result or "", notes, values, answers, who)
    if action == "report":
        return report()
    if action == "status":
        return api.tool_status()
    if action == "last_error":
        return api.instrument("last_error")
    if action == "config":
        q = params or {}
        return api.labspec_config(q.get("action", "list"), q.get("name"), show=bool(q.get("show")))
    raise ValueError("action: plan, step, call, record, report, status, last_error, config")
