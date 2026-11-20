"""labspec_ini.py — what LabSpec's own hardware configuration file (OLConfig.ini) says about the instrument.
Version: 2026.11.20.16

LabSpec 6 keeps its hardware set-up in OLConfig.ini (in its installation folder, e.g. C:\\HORIBA\\LabSpec_6_8_1).
Most keys are numbers (hashes of property names); the readable parts are the motors (their internal and display
names, and their positions' names and values), the cameras, and per-objective arrays. ls6t only reads it, to
complete the machine profile with what LabSpec's automation cannot report:

- the cameras' names and numbers (the automation gives only the active camera's number): video.cameras;
- per objective: the numerical aperture and, very likely, the camera's field of view in µm;
- motors discovery does not look for (polarisers, a second filter wheel, the filter type, the light path...).

The meaning of the numeric keys is inferred from their values (TESTING.md Q41): the names used here say so.
ls6t never writes this file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

NAME = "134243433"          # a motor's internal name
DISPLAY = "1835884910"      # its name in LabSpec's interface
OBJ_NA = "141779310"        # per objective: numerical aperture (0.9 for x100...)
OBJ_FOV_W = "1826215640"    # per objective: 85.6 for x100, 863 for x10: the field of view's width (µm), inferred
OBJ_FOV_H = "1826281176"    # and its height
CAM_MODEL = "141780073"     # per camera: model and serial number
ACTIVE = "-605497649"       # Hardware_Devices: OLService[ACTIVE] = the active instrument configuration


def parse(text: str) -> dict:
    """{section: {key: value}}; keys keep their brackets (Names[0], 134243433[1]...)."""
    secs: dict = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            cur = secs.setdefault(m.group(1), {})
            continue
        if cur is not None and "=" in s:
            k, v = s.split("=", 1)
            cur[k.strip()] = v.strip()
    return secs


def read(path) -> dict:
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    return parse(text)


def _array(d: dict, key: str) -> list:
    out = {}
    for k, v in d.items():
        m = re.fullmatch(re.escape(key) + r"\[(\d+)\]", k)
        if m:
            out[int(m.group(1))] = v
    return [out[i] for i in sorted(out)]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def motors(secs: dict) -> dict:
    """{internal name: {display, config, positions: [{name, value}], arrays: {key: [...]}}} for every motor."""
    out = {}
    for sec, d in secs.items():
        m = re.fullmatch(r"Hardware_Motors_(.+)", sec)
        if not m:
            continue
        cfgs = {k.split("[", 1)[0] for k in d if k.endswith(f"[{NAME}]") or k.endswith(f"[{DISPLAY}]")}
        for cfg in sorted(cfgs):
            name = d.get(f"{cfg}[{NAME}]") or d.get(f"{cfg}[{DISPLAY}]")
            if not name:
                continue
            sub = secs.get(f"{sec}_{cfg}", {})
            names, values = _array(sub, "Names"), _array(sub, "Values")
            arrays = {}
            for k in sub:
                km = re.fullmatch(r"(-?\d+)\[\d+\]", k)
                if km:
                    arrays.setdefault(km.group(1), _array(sub, km.group(1)))
            entry = {"display": d.get(f"{cfg}[{DISPLAY}]"), "config": cfg,
                     "positions": [{"name": n, "value": _num(values[i]) if i < len(values) else None}
                                   for i, n in enumerate(names)],
                     "arrays": arrays}
            key = name if name not in out else f"{name} ({cfg})"
            out[key] = entry
    return out


def summary(secs: dict) -> dict:
    """The parts ls6t uses: the instrument, the cameras, the objectives, the other motors and their positions."""
    mot = motors(secs)
    out: dict = {"motors": {k: [p["name"] for p in v["positions"]] for k, v in mot.items()}}
    inst = secs.get("Hardware_Motors_Instrument_OLService", {})
    active = secs.get("Hardware_Devices", {}).get(f"OLService[{ACTIVE}]")
    vals, names = _array(inst, "Values"), _array(inst, "Names")
    if active in vals:
        out["instrument"] = names[vals.index(active)]
    cam = next((v for v in mot.values() if (v.get("display") or "").lower() == "camera" and v["positions"]), None)
    if cam:
        models = cam["arrays"].get(CAM_MODEL, [])
        out["cameras"] = {p["name"]: {"number": int(p["value"]) if p["value"] is not None else i,
                                      **({"model": models[i]} if i < len(models) else {})}
                          for i, p in enumerate(cam["positions"])}
    obj = secs.get("Hardware_Motors_Objective_OLService", {})
    if obj:
        names, mags = _array(obj, "Names"), _array(obj, "Values")
        na, fw, fh = _array(obj, OBJ_NA), _array(obj, OBJ_FOV_W), _array(obj, OBJ_FOV_H)
        objs = {}
        for i, n in enumerate(names):
            info = {"magnification": _num(mags[i]) if i < len(mags) else None,
                    "na": _num(na[i]) if i < len(na) else None}
            if i < len(fw) and i < len(fh) and _num(fw[i]) and _num(fh[i]):
                info["field_um"] = [round(_num(fw[i]), 2), round(_num(fh[i]), 2)]
            objs.setdefault(n, info)          # a label listed twice (unused slots) keeps its first entry
        out["objectives"] = objs
    if active:
        dev = secs.get(f"Hardware_Devices_{active}", {})
        arr = {}
        for k in dev:
            km = re.fullmatch(r"(-?\d+)\[\d+\]", k)
            if km:
                arr.setdefault(km.group(1), [_num(x) for x in _array(dev, km.group(1))])
        if arr:
            out["spectrometer_arrays"] = arr      # one value per grating; meanings not documented (Q41)
    return out


def find(labspec_path: str | None = None) -> Path | None:
    """OLConfig.ini: LS6T_LABSPEC_INI, else in LabSpec's folder (from discovery) or under C:\\HORIBA."""
    if os.environ.get("LS6T_LABSPEC_INI") and Path(os.environ["LS6T_LABSPEC_INI"]).is_file():
        return Path(os.environ["LS6T_LABSPEC_INI"])
    roots = [Path(labspec_path)] if labspec_path else []
    if os.name == "nt":
        roots.append(Path("C:/HORIBA"))
    for root in roots:
        if not root.is_dir():
            continue
        hits = []
        for depth in ("", "*/", "*/*/"):
            hits += list(root.glob(depth + "OLConfig.ini"))
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)
    return None


def enrich(prof: dict, labspec_path: str | None = None, notes: list | None = None) -> dict:
    """Adds what OLConfig.ini says to a profile being made from discovery: camera names (when none are set),
    objectives' NA and field of view, LabSpec's other motors. Notes say what was used."""
    notes = notes if notes is not None else []
    p = find(labspec_path)
    if not p:
        return prof
    try:
        s = summary(read(p))
    except (OSError, ValueError) as exc:
        notes.append(f"LabSpec's configuration {p} could not be read ({exc})")
        return prof
    prof["labspec_config"] = {"file": str(p), "motors": s.get("motors", {}),
                              "spectrometer_arrays": s.get("spectrometer_arrays", {})}
    cams = s.get("cameras") or {}
    if cams and not (prof.get("video", {}).get("cameras")):
        prof.setdefault("video", {})["cameras"] = {n.lower(): c["number"] for n, c in cams.items()}
        notes.append("cameras from LabSpec's configuration: " +
                     ", ".join(f"{n} = {c['number']}" + (f" ({c['model']})" if c.get("model") else "")
                               for n, c in cams.items()))
    objs = s.get("objectives") or {}
    have = [str(o) for o in prof.get("objectives", [])]
    info = {o: objs[o] for o in have if o in objs}
    if info:
        prof["objectives_info"] = info
        notes.append("objectives from LabSpec's configuration: " +
                     ", ".join(f"{o} NA {v.get('na')}" + (f", field {v['field_um'][0]:g} x {v['field_um'][1]:g} µm"
                                                         if v.get("field_um") else "") for o, v in info.items()))
    extra = [m for m in ("LaserPol", "RamanPol", "LaserFilter", "FilterV", "DuoPos", "Output", "IlluminatorV",
                         "Microscope", "LaserSwitch") if m in s.get("motors", {})]
    if extra:
        notes.append("other motors in LabSpec's configuration: " +
                     "; ".join(f"{m} ({', '.join(s['motors'][m])})" for m in extra))
    return prof
