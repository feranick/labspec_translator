"""autocal.py — LabSpec's own AutoCalibration: where its script is, and whether the bridge can run it.
Version: 2026.11.22.10

ls6t checks the spectral calibration itself (calibration studies). To correct LabSpec's own calibration, the best
tool is HORIBA's: LabSpec's AutoCalibration, a VBS script (OLSetup.ini names it, e.g. ...\\AutoCalibration\\
UniversalAutoCalibrationv3.vbs). The bridge can run such a script inside itself the way HORIBA's scripts include
each other (bridge verb run_vbs, ExecuteGlobal). Whether that works depends on the script: if it asks the operator
through dialogs it still works but needs them at the screen; if it declares a name the bridge already uses, the
run fails at once ("Name redefined"). info() reads the script (without running it or copying it anywhere) and says
which. Running it is off until a person enables it after trying it with the maintainer (profile calibration.autocal,
TESTING T4.9).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path


def candidates(prof: dict) -> list[Path]:
    """The AutoCalibration scripts on this PC: the one OLSetup.ini names, and any .vbs in an AutoCalibration folder
    of LabSpec's installations."""
    out = []
    named = (prof.get("labspec_config") or {}).get("autocalibration_script")
    if named:
        out.append(Path(named))
    roots = []
    lp = (prof.get("discovered") or {}).get("labspec_path")
    if lp:
        roots.append(Path(lp))
    if os.name == "nt":
        roots += sorted(Path("C:/HORIBA").glob("LabSpec*"))
    for r in roots:
        for d in (r / "AutoCalibration", r / "AUTOCALIBRATION"):
            if d.is_dir():
                out += sorted(d.glob("*.vbs"))
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _top_names(code: str) -> set:
    """Names the script declares at its top level (Dim, Const, Sub, Function, Class)."""
    names, depth = set(), 0
    for raw in code.splitlines():
        s = raw.split("'")[0].strip()
        low = s.lower()
        if re.match(r"^(public\s+|private\s+)?(sub|function|class)\s", low):
            m = re.match(r"^(?:public\s+|private\s+)?(?:sub|function|class)\s+(\w+)", s, re.I)
            if m and depth == 0:
                names.add(m.group(1).lower())
            depth += 1
            continue
        if re.match(r"^end\s+(sub|function|class)\b", low):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            m = re.match(r"^(?:dim|public|private|const)\s+(.+)$", s, re.I)
            if m:
                for part in m.group(1).split(","):
                    n = re.match(r"\s*(\w+)", part)
                    if n:
                        names.add(n.group(1).lower())
    return names


def assess(path: Path, bridge_text: str = "") -> dict:
    """What the script does, from its text: LabSpec calls, whether it asks the operator, what it changes, which
    files it includes, and the names it shares with the bridge (each one stops a run inside the bridge)."""
    raw = Path(path).read_bytes()
    code = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("cp1252", errors="replace")
    calls: dict = {}
    for m in re.finditer(r"\bLabSpec\.(\w+)", code, re.I):
        k = m.group(1)
        k = next((c for c in calls if c.lower() == k.lower()), k)
        calls[k] = calls.get(k, 0) + 1
    low = code.lower()
    asks = [w for w, pat in (("LabSpec dialogs", r"labspec\.dialog"), ("Yes/No questions", r"mb_yesno|mb_okcancel"),
                             ("waits for the operator", r"mb_wait_for_event|mb_ok\b"), ("MsgBox", r"\bmsgbox\b"),
                             ("InputBox", r"\binputbox\b"), ("LabSpec GUI panels", r"labspec\.gui\b"))
            if re.search(pat, low)]
    changes = [w for w, pat in (("LabSpec's calibration (OperVar write)", r"opervar\s*\(?\s*1\s*,"),
                                ("motors", r"labspec\.movemotor"), ("settings (PutValue)", r"labspec\.putvalue"),
                                ("acquisitions", r"labspec\.acq\b"), ("files", r"createtextfile|\.save\b"))
               if re.search(pat, low)]
    includes = sorted(set(re.findall(r'(?:includefile|executeglobal)[^\n"]*"([^"]+)"', code, re.I)))
    clashes = sorted(_top_names(code) & _top_names(bridge_text)) if bridge_text else []
    st = Path(path).stat()
    return {"file": str(path), "bytes": st.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "lines": code.count("\n") + 1, "labspec_calls": dict(sorted(calls.items(), key=lambda kv: -kv[1])),
            "asks_the_operator": asks, "changes": changes, "includes": includes,
            "names_shared_with_the_bridge": clashes,
            "runs_inside_the_bridge": not clashes,
            "unattended": not clashes and not asks}


def info(prof: dict, bridge_text: str = "") -> dict:
    found = candidates(prof)
    out = {"scripts": [], "enabled": bool((prof.get("calibration", {}).get("autocal") or {}).get("enabled")),
           "configured_script": (prof.get("calibration", {}).get("autocal") or {}).get("script")}
    for p in found:
        if p.is_file():
            try:
                out["scripts"].append(assess(p, bridge_text))
            except OSError as exc:
                out["scripts"].append({"file": str(p), "error": str(exc)})
        else:
            out["scripts"].append({"file": str(p), "missing": True})
    if not any(s.get("bytes") for s in out["scripts"]):
        out["next"] = ("no AutoCalibration script found on this PC: look for an AutoCalibration folder in LabSpec's "
                       "folder; LabSpec's own menu may still offer AutoCalibration (do it there, by hand)")
    else:
        out["next"] = ("with the maintainer (TESTING T4.9): try the script from ls6t once, then a person enables it: "
                       "ls6t profile set calibration.autocal '{\"enabled\": true, \"script\": \"<path>\", "
                       "\"timeout_s\": 1800, \"sample\": \"<what it needs under the objective>\"}'")
    return out
