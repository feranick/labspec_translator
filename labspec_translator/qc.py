"""qc.py — HORIBA's factory / installation QC reports for this instrument, read once and kept as references.

HORIBA's QC scripts leave one small text report per test under `C:\\Horiba Jobin Yvon\\1-QC\\QC Auto` (per laser:
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

DEFAULT_DIR = Path("C:/Horiba Jobin Yvon/1-QC/QC Auto")


def read_text(p: Path) -> str:
    b = Path(p).read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    if len(b) > 1 and b[1:2] == b"\x00":
        return b.decode("utf-16-le", errors="replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


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


def find_dir(path=None) -> Path | None:
    if path:
        p = Path(path)
        return p if p.is_dir() else None
    for p in (DEFAULT_DIR, Path(os.environ.get("LS6T_QC_DIR", "") or DEFAULT_DIR)):
        if p.is_dir():
            return p
    return None


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
    d = find_dir(path)
    if d is None:
        return {"ok": False, "error": f"no QC folder at {path or DEFAULT_DIR}: give its path"}
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
    return {"ok": True, "from": str(d), "reports": len(kept), "tests": sorted({r["test"] for r in kept}),
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
