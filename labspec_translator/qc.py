"""qc.py — HORIBA's factory / installation QC reports for this instrument, read once and kept as references.

HORIBA's QC scripts leave one small text report per test under `<drive>:\\Horiba Jobin Yvon\\1-QC\\QC Auto` (per laser:
Cut-Off, Laser Power, Axial Resolution, Sensitivity, Confocality...; per grating: Spectral Resolution, Detector
Calibration, Grating Linearity; Noise, Neon Repeatability). Each report is `key=value` lines (`info=`, `moteur=`,
`value=`, `graph=`, `unite=`, `point=y=x`), UTF-16 or plain text. ls6t reads the values (not HORIBA's scripts or
documents), keeps them in LS6T_HOME/qc.json, shows them with `references`, and uses the cut-off: a range below the
edge filter's cut-off is warned (perf 'cutoff', key '<laser>@any', when no measurement of ls6t's own exists).
Nothing in the profile changes: the laser powers are compared with the profile's power_mw for a person to judge.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import store



from .io_util import read_text  # noqa: F401 - re-exported for callers


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def parse(text: str) -> dict:
    """One report: {test, motors {}, values {}, result, graphs {name: {unit, points [[x, y]]}}}."""
    out: dict = {"motors": {}, "values": {}, "graphs": {}}
    graph = None
    for line in text.splitlines():
        parts = line.strip().split("=")
        if len(parts) < 2:
            continue
        kind = parts[0]
        if kind == "info" and len(parts) >= 3:
            if parts[1] == "test":
                out["test"] = parts[2]
            elif parts[1] == "model":
                out["model"] = parts[2]
        elif kind == "moteur" and len(parts) >= 3:
            out["motors"][parts[1]] = "=".join(parts[2:])
        elif kind == "value" and len(parts) >= 3:
            out["values"][parts[1]] = _num(parts[2])
        elif kind == "test" and len(parts) >= 3 and parts[1] == "result":
            out["result"] = parts[2]
        elif kind == "graph" and len(parts) >= 3:
            graph = f"{parts[1]}:{parts[2]}"
            out["graphs"].setdefault(graph, {"points": []})
        elif kind == "unite" and len(parts) >= 3 and graph:
            out["graphs"][graph]["unit"] = "=".join(parts[2:])
        elif kind == "point" and len(parts) >= 3 and graph:
            out["graphs"][graph]["points"].append([_num(parts[2]), _num(parts[1])])
    return out


def _date(path: Path) -> str | None:
    """The folder name the QC scripts give a run: dd_mm_yyyy_hh_mm_ss."""
    for part in reversed(path.parts):
        m = re.fullmatch(r"(\d{2})_(\d{2})_(\d{4})_(\d{2})_(\d{2})_(\d{2})", part)
        if m:
            d, mo, y, h, mi, s = m.groups()
            return f"{y}-{mo}-{d}T{h}:{mi}:{s}"
    return None


KNOWN = ("Horiba Jobin Yvon/1-QC/QC Auto", "LabSpec Software & Drivers/QC/1-QC/QC Auto")
SKIP = {"windows", "$recycle.bin", "system volume information", "programdata", "users", "program files",
        "program files (x86)", "recovery", "perflogs"}


def _drive_roots() -> list[Path]:
    """The fixed drives' roots (C:\\, D:\\ ...) on Windows; none elsewhere."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        mask, k32 = ctypes.windll.kernel32.GetLogicalDrives(), ctypes.windll.kernel32
        return [Path(f"{chr(65 + i)}:/") for i in range(26)
                if mask >> i & 1 and k32.GetDriveTypeW(f"{chr(65 + i)}:\\") == 3]
    except (AttributeError, OSError):
        return [Path(f"{c}:/") for c in "CDEF" if Path(f"{c}:/").is_dir()]


def _search(root: Path, depth: int = 3) -> list[Path]:
    """Folders '1-QC/QC Auto' up to `depth` levels below a drive's root, skipping the system's folders."""
    found, todo = [], [(root, 0)]
    while todo:
        d, n = todo.pop()
        try:
            entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for e in entries:
            if e.name.lower() == "1-qc" and Path(e.path, "QC Auto").is_dir():
                found.append(Path(e.path, "QC Auto"))
            elif n + 1 < depth and not (n == 0 and e.name.lower() in SKIP) and not e.name.startswith("."):
                todo.append((Path(e.path), n + 1))
    return found


def _newest(d: Path) -> float:
    return max((f.stat().st_mtime for f in d.rglob("*.txt")), default=0.0)


def candidates() -> list[Path]:
    """Every QC folder on the fixed drives: LS6T_QC_DIR, the known places on each drive, then a short search."""
    out: list[Path] = []
    env = os.environ.get("LS6T_QC_DIR")
    if env and Path(env).is_dir():
        out.append(Path(env))
    roots = _drive_roots()
    for r in roots:
        out += [r / k for k in KNOWN if (r / k).is_dir()]
    if not out:
        for r in roots:
            out += _search(r)
    seen, uniq = set(), []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def find_dir(path=None, found: list | None = None) -> Path | None:
    """The QC folder given, else LS6T_QC_DIR, else the one with the newest report among those found on the fixed
    drives (HORIBA installs it on C: or D:, with a copy under 'LabSpec Software & Drivers'). `found` gets them all."""
    if path:
        p = Path(path)
        return p if p.is_dir() else None
    c = candidates()
    if found is not None:
        found.extend(str(p) for p in c)
    if not c:
        return None
    if os.environ.get("LS6T_QC_DIR") and c[0] == Path(os.environ["LS6T_QC_DIR"]):
        return c[0]
    return max(c, key=lambda p: (_newest(p), -c.index(p)))


def laser_for(prof: dict, label) -> float | None:
    """The profile's laser line for HORIBA's label ('638nm_Edge'): by label, else by the number in it."""
    for v in (prof.get("lasers") or {}).values():
        if str(v.get("label", "")).lower() == str(label).lower():
            return float(v["nm"])
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(label or ""))
    if not m:
        return None
    nm = float(m.group(1))
    best = min((abs(float(v["nm"]) - nm), float(v["nm"])) for v in (prof.get("lasers") or {}).values()) \
        if prof.get("lasers") else None
    return best[1] if best and best[0] <= 2.0 else nm


def import_dir(prof: dict, path=None) -> dict:
    """Reads every report under the QC folder; keeps the newest of each test (per laser / grating) in qc.json and
    the cut-offs in the performance store. Returns what was found."""
    from . import perf
    found: list = []
    d = find_dir(path, found)
    if d is None:
        where = path or "the fixed drives (" + ", ".join(KNOWN) + ", or '1-QC\\QC Auto' up to 3 folders deep)"
        return {"ok": False, "error": f"no QC folder in {where}: give its path"}
    reports = []
    for f in sorted(d.rglob("*.txt")):
        try:
            r = parse(read_text(f))
        except OSError:
            continue
        if not r.get("test"):
            continue
        r.update(file=str(f), date=_date(f))
        las = r["motors"].get("Laser Filtering")
        if las:
            r["laser_label"] = las
            r["laser_nm"] = laser_for(prof, las)
        if r["motors"].get("Grating"):
            r["grating"] = r["motors"]["Grating"]
        reports.append(r)
    newest: dict = {}
    for r in reports:
        k = "|".join(str(x) for x in (r["test"], r.get("laser_label"), r.get("grating"), r["motors"].get("Detector")))
        if k not in newest or str(r.get("date") or "") > str(newest[k].get("date") or ""):
            newest[k] = r
    kept = sorted(newest.values(), key=lambda r: (r["test"], str(r.get("laser_label")), str(r.get("grating"))))
    store.write("qc.json", {"from": str(d), "imported": time.strftime("%Y-%m-%dT%H:%M:%S"), "reports": kept})
    cut = {}
    for r in kept:
        if r["test"] == "Cut-Off" and r.get("laser_nm") and isinstance(r["values"].get("High_Cut-Off"), float):
            perf.record("cutoff", perf.key(r["laser_nm"], "any"),
                        {"usable_from_cm1": r["values"]["High_Cut-Off"], "low_cm1": r["values"].get("Low_Cut-Off"),
                         "source": "HORIBA QC", "date": r.get("date")}, str(d))
            cut[r["laser_label"]] = r["values"]["High_Cut-Off"]
    return {"ok": True, "from": str(d), **({"found": found} if len(found) > 1 else {}),
            "chosen": "as given" if path else ("LS6T_QC_DIR" if os.environ.get("LS6T_QC_DIR") == str(d) else
                                                "the folder with the newest report" if len(found) > 1 else "found"),
            "reports": len(kept), "tests": sorted({r["test"] for r in kept}),
            "cutoff_cm1": cut, "summary": summary(prof)}


def summary(prof: dict | None = None) -> dict:
    """The QC values in short, and the laser powers next to the profile's power_mw."""
    q = store.read("qc.json")
    if not q.get("reports"):
        return {}
    out: dict = {"from": q.get("from"), "imported": q.get("imported"), "lasers": {}, "gratings": {}}
    for r in q["reports"]:
        v = r["values"]
        if r.get("laser_label"):
            e = out["lasers"].setdefault(r["laser_label"], {"laser_nm": r.get("laser_nm")})
            if r["test"] == "Laser Power":
                e.update(output_mw=v.get("Output_Power"), at_sample_mw=v.get("Sample_Power"))
            elif r["test"] == "Cut-Off":
                e.update(cutoff_cm1=[v.get("Low_Cut-Off"), v.get("High_Cut-Off")])
            elif r["test"] == "Axial Resolution":
                pts = next(iter(r["graphs"].values()), {}).get("points", [])
                e.update(axial_fwhm_um=v.get("FWHM"), axial_by_hole_um={f"{x:g}": y for x, y in pts
                                                                           if isinstance(x, float)})
            elif r["test"] == "Sensitivity":
                e.update(si_snr=v.get("SN"))
            elif r["test"] == "Confocality":
                e.update(confocality=v.get("Confocalite"))
            e.setdefault("dates", {})[r["test"]] = r.get("date")
        elif r.get("grating") and r["test"] == "Spectral Resolution":
            g = out["gratings"].setdefault(r["grating"], {})
            g["best_resolution_cm1"] = {k: v for k, v in v.items() if k.startswith("Best_Resolution")}
            g["resolution_by_slit"] = {name.split(":", 1)[1] + " at " + str(gr.get("unit", "")).split(" at ")[-1]
                                       .split("=")[0]: {f"{x:g}": y for x, y in gr["points"] if isinstance(x, float)}
                                       for name, gr in r["graphs"].items()}
    if prof:
        for lab, e in out["lasers"].items():
            k = next((k for k, v in (prof.get("lasers") or {}).items()
                      if e.get("laser_nm") and abs(float(v["nm"]) - float(e["laser_nm"])) < 0.5), None)
            if k:
                e["profile_power_mw"] = prof["lasers"][k].get("power_mw")
    return out
