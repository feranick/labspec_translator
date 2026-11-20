"""typelib.py — read LabSpec's ActiveX type library and print every command it offers (read-only).
Version: 2026.11.19.8

LabSpec 6's ActiveX control (NFActiveX.ocx; NGSActiveX.ocx for LabSpec 5) carries a type library: the list of
every method and property of the installed version, with parameter names and types. Reading it loads no control,
starts nothing, and moves nothing. The list shows what the 2018 automation manual does not: commands added since,
or never documented (AutoAlignment, AutoCalibration, calibration settings, lamp, map progress...). Each method is
marked 'documented' (in the 2018 manual) or 'NEW', and names with words of interest are flagged.

Windows only (it uses the system's type-library loader through comtypes, or pywin32 if comtypes is missing).
Standalone: `python typelib.py [--path FILE] [--format text|markdown|json] [--out FILE]`, or `ls6t-typelib` once the
package is installed. Without --path it looks for the control in LabSpec's usual folders and in the registry.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

__all__ = ["KNOWN", "INTEREST", "find_files", "read", "render", "main"]

# the methods in "LabSpec 5/6 Automation" (HORIBA, 2018), incl. those only named in its examples
KNOWN = {
    "Acq", "Autofocus", "GetAcqID", "GetTriggerMode", "SetAutoExposure", "Send", "ConvertUnit", "CreateDataObject",
    "GetActiveData", "GetValue", "GetValueEx", "GetValueSimple", "PutDataInfo", "PutValue", "Treat",
    "GetDetectorZone", "SetDetectorZone", "AddID", "Load", "LoadAll", "Print", "Save", "Template", "Dialog",
    "Message", "MessageEx", "Paint", "TickCount", "GetMotorPosition", "ManageTemperature", "MoveMotor", "MoveXY",
    "SetScriptParamOptions", "SetSingleScriptParam", "Pause", "Exec", "SetScale", "SetScaleEx",
    "GetMappingParams", "Map", "MapEx", "Profile", "SetMappingParams", "Video", "GetMotorStatus",
    "GetMotorAction", "SetExtendedVideo",
}

# words that point to functions ls6t would use (alignment, calibration, lamp, progress, ...)
INTEREST = ("align", "calib", "zero", "koef", "coef", "lamp", "light", "illum", "shutter", "laser", "power",
            "progress", "status", "map", "swift", "duoscan", "focus", "spectro", "offset", "temperature", "abort",
            "stop", "event", "callback", "template", "method", "busy", "state")

CANDIDATES = [
    r"C:\Program Files\HORIBA Scientific\LabSpec 6\*.ocx",
    r"C:\Program Files (x86)\HORIBA Scientific\LabSpec 6\*.ocx",
    r"C:\Program Files\HORIBA\LabSpec 6\*.ocx",
    r"C:\Program Files (x86)\HORIBA\LabSpec 6\*.ocx",
    r"C:\Program Files\HORIBA Scientific\LabSpec*\*.ocx",
    r"C:\Program Files (x86)\HORIBA Scientific\LabSpec*\*.ocx",
    r"C:\Program Files\HORIBA Scientific\LabSpec*\*.tlb",
    r"C:\Program Files (x86)\HORIBA Scientific\LabSpec*\*.tlb",
]
NAMES = ("NFActiveX.ocx", "NGSActiveX.ocx")

# VARTYPE names
VT = {0: "void", 1: "null", 2: "short", 3: "long", 4: "float", 5: "double", 6: "currency", 7: "date", 8: "BSTR",
      9: "IDispatch", 10: "error", 11: "bool", 12: "VARIANT", 13: "IUnknown", 14: "decimal", 16: "char",
      17: "byte", 18: "ushort", 19: "ulong", 20: "int64", 21: "uint64", 22: "int", 23: "uint", 24: "void",
      25: "HRESULT", 26: "ptr", 27: "SAFEARRAY", 28: "array", 29: "userdefined", 30: "LPSTR", 31: "LPWSTR"}
TKIND = {0: "enum", 1: "record", 2: "module", 3: "interface", 4: "dispinterface", 5: "coclass", 6: "alias",
         7: "union"}
INVKIND = {1: "method", 2: "property get", 4: "property put", 8: "property putref"}
PARAMFLAG_FIN, PARAMFLAG_FOUT, PARAMFLAG_FOPT = 1, 2, 16


# --------------------------------------------------------------------------------------------- finding the file

def find_files() -> list[str]:
    """The ActiveX controls (and type libraries) of LabSpec on this PC: the usual folders, then the registry."""
    found: list[str] = []
    for pat in CANDIDATES:
        for f in glob.glob(pat):
            if f not in found:
                found.append(f)
    try:
        import winreg  # noqa: F401
        found += [f for f in _from_registry() if f not in found]
    except ImportError:
        pass
    pri = [f for f in found if os.path.basename(f) in NAMES]
    return pri + [f for f in found if f not in pri]


def _from_registry() -> list[str]:
    """Type libraries registered under HKEY_CLASSES_ROOT\\TypeLib whose name mentions LabSpec, NGS or NF ActiveX."""
    import winreg
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "TypeLib")
    except OSError:
        return out
    i = 0
    while True:
        try:
            guid = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            gk = winreg.OpenKey(root, guid)
            j = 0
            while True:
                try:
                    ver = winreg.EnumKey(gk, j)
                except OSError:
                    break
                j += 1
                try:
                    vk = winreg.OpenKey(gk, ver)
                    name = winreg.QueryValueEx(vk, "")[0] or ""
                    if not any(w in name.lower() for w in ("labspec", "ngsactivex", "nfactivex", "horiba")):
                        continue
                    for plat in ("0\\win64", "0\\win32"):
                        try:
                            pk = winreg.OpenKey(vk, plat)
                            path = winreg.QueryValueEx(pk, "")[0]
                            if path and os.path.exists(path):
                                out.append(path)
                        except OSError:
                            pass
                except OSError:
                    pass
        except OSError:
            pass
    return out


# --------------------------------------------------------------------------------------------- reading

def _type_name(ti, tdesc) -> str:
    vt = tdesc.vt
    if vt == 26:            # VT_PTR
        return _type_name(ti, tdesc._.lptdesc[0]) + "*"
    if vt == 27:            # VT_SAFEARRAY
        return "SAFEARRAY(" + _type_name(ti, tdesc._.lptdesc[0]) + ")"
    if vt == 29:            # VT_USERDEFINED
        try:
            return ti.GetRefTypeInfo(tdesc._.hreftype).GetDocumentation(-1)[0]
        except Exception:  # noqa: BLE001
            return "userdefined"
    return VT.get(vt, f"vt{vt}")


def _read_comtypes(path: str) -> dict:
    from comtypes import typeinfo
    tlib = typeinfo.LoadTypeLibEx(path, typeinfo.REGKIND_NONE)
    la = tlib.GetLibAttr()
    name, doc, _, _ = tlib.GetDocumentation(-1)
    lib = {"file": path, "library": name, "doc": doc or "", "guid": str(la.guid),
           "version": f"{la.wMajorVerNum}.{la.wMinorVerNum}", "types": []}
    for i in range(tlib.GetTypeInfoCount()):
        ti = tlib.GetTypeInfo(i)
        ta = ti.GetTypeAttr()
        tname, tdoc, _, _ = tlib.GetDocumentation(i)
        t = {"name": tname, "kind": TKIND.get(ta.typekind, str(ta.typekind)), "doc": tdoc or "",
             "guid": str(ta.guid), "methods": [], "properties": [], "values": []}
        for j in range(ta.cFuncs):
            fd = ti.GetFuncDesc(j)
            names = ti.GetNames(fd.memid, fd.cParams + 1)
            fdoc = ti.GetDocumentation(fd.memid)[1] or ""
            params = []
            for k in range(fd.cParams):
                ed = fd.lprgelemdescParam[k]
                flags = ed._.paramdesc.wParamFlags
                params.append({"name": names[k + 1] if k + 1 < len(names) else f"p{k}",
                               "type": _type_name(ti, ed.tdesc),
                               "dir": ("in/out" if flags & PARAMFLAG_FIN and flags & PARAMFLAG_FOUT else
                                       "out" if flags & PARAMFLAG_FOUT else "in"),
                               "optional": bool(flags & PARAMFLAG_FOPT)})
            t["methods"].append({"name": names[0], "invoke": INVKIND.get(fd.invkind, str(fd.invkind)),
                                 "returns": _type_name(ti, fd.elemdescFunc.tdesc), "params": params,
                                 "dispid": int(fd.memid), "doc": fdoc})
        for j in range(ta.cVars):
            vd = ti.GetVarDesc(j)
            vname = ti.GetNames(vd.memid, 1)[0]
            entry = {"name": vname, "type": _type_name(ti, vd.elemdescVar.tdesc)}
            if ta.typekind == 0:                       # enum: its value
                try:
                    entry["value"] = int(vd._.lpvarValue[0].value)
                except Exception:  # noqa: BLE001
                    pass
                t["values"].append(entry)
            else:
                t["properties"].append(entry)
        lib["types"].append(t)
    return lib


def _read_pywin32(path: str) -> dict:
    import pythoncom
    tlib = pythoncom.LoadTypeLib(path)
    name, doc, _, _ = tlib.GetDocumentation(-1)
    la = tlib.GetLibAttr()
    lib = {"file": path, "library": name, "doc": doc or "", "guid": str(la[0]), "version": f"{la[3]}.{la[4]}",
           "types": []}
    for i in range(tlib.GetTypeInfoCount()):
        ti = tlib.GetTypeInfo(i)
        ta = ti.GetTypeAttr()
        tname, tdoc, _, _ = tlib.GetDocumentation(i)
        t = {"name": tname, "kind": TKIND.get(ta.typekind, str(ta.typekind)), "doc": tdoc or "",
             "guid": str(ta.iid), "methods": [], "properties": [], "values": []}
        for j in range(ta.cFuncs):
            fd = ti.GetFuncDesc(j)
            names = ti.GetNames(fd.memid)
            params = []
            for k, arg in enumerate(fd.args):
                vt, flags = arg[0], arg[1]
                params.append({"name": names[k + 1] if k + 1 < len(names) else f"p{k}",
                               "type": VT.get(vt & 0x0FFF, f"vt{vt}") + ("*" if vt & 0x4000 else ""),
                               "dir": ("in/out" if flags & PARAMFLAG_FIN and flags & PARAMFLAG_FOUT else
                                       "out" if flags & PARAMFLAG_FOUT else "in"),
                               "optional": bool(flags & PARAMFLAG_FOPT)})
            rvt = fd.rettype[0]
            t["methods"].append({"name": names[0], "invoke": INVKIND.get(fd.invkind, str(fd.invkind)),
                                 "returns": VT.get(rvt & 0x0FFF, f"vt{rvt}"), "params": params,
                                 "dispid": int(fd.memid), "doc": ti.GetDocumentation(fd.memid)[1] or ""})
        for j in range(ta.cVars):
            vd = ti.GetVarDesc(j)
            entry = {"name": ti.GetNames(vd.memid)[0], "type": VT.get(vd.elemdescVar[0] & 0x0FFF, "?")}
            if ta.typekind == 0:
                entry["value"] = vd.value
                t["values"].append(entry)
            else:
                t["properties"].append(entry)
        lib["types"].append(t)
    return lib


def read(path: str) -> dict:
    """The type library of `path` as {library, version, guid, types: [{name, kind, methods, properties,
    values}]}, each method marked documented / new and flagged for words of interest."""
    if os.name != "nt":
        raise SystemExit("ls6t-typelib reads Windows type libraries: run it on the instrument PC")
    try:
        lib = _read_comtypes(path)
        lib["reader"] = "comtypes"
    except ImportError:
        try:
            lib = _read_pywin32(path)
            lib["reader"] = "pywin32"
        except ImportError:
            raise SystemExit("needs comtypes (pip install comtypes) or pywin32 (pip install pywin32)")
    return annotate(lib)


def annotate(lib: dict) -> dict:
    """Marks each method documented (in the 2018 manual) or new, and flags words of interest; adds a summary."""
    known_lower = {k.lower() for k in KNOWN}
    seen, new, interest = set(), [], []
    for t in lib["types"]:
        for m in t["methods"]:
            m["documented"] = m["name"].lower() in known_lower
            m["interest"] = [w for w in INTEREST if w in m["name"].lower()]
            if t["kind"] in ("dispinterface", "interface") and m["name"] not in seen and not _plumbing(m["name"]):
                seen.add(m["name"])
                if not m["documented"]:
                    new.append(m["name"])
                if m["interest"] and not m["documented"]:
                    interest.append(m["name"])
    lib["summary"] = {"methods": len(seen), "documented": len(seen) - len(new), "new": sorted(new),
                      "new_of_interest": sorted(interest),
                      "documented_missing": sorted(k for k in KNOWN if k.lower() not in {s.lower() for s in seen})}
    return lib


def _plumbing(name: str) -> bool:
    """IUnknown / IDispatch / OLE control methods every ActiveX has."""
    return name in {"QueryInterface", "AddRef", "Release", "GetTypeInfoCount", "GetTypeInfo", "GetIDsOfNames",
                    "Invoke", "AboutBox"}


# --------------------------------------------------------------------------------------------- printing

def _sig(m: dict) -> str:
    ps = ", ".join(("[opt] " if p["optional"] else "") + (f"{p['dir']} " if p["dir"] != "in" else "")
                   + f"{p['type']} {p['name']}" for p in m["params"])
    return f"{m['returns']} {m['name']}({ps})"


def render(lib: dict, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(lib, indent=1)
    md = fmt == "markdown"
    s = lib["summary"]
    out = []
    h = "# " if md else ""
    out.append(f"{h}{lib['library']} {lib['version']}  ({lib['file']})")
    if lib.get("doc"):
        out.append(lib["doc"])
    out.append(f"{s['methods']} methods: {s['documented']} in the 2018 manual, {len(s['new'])} not in it"
               + (f"; of interest: {', '.join(s['new_of_interest'])}" if s["new_of_interest"] else ""))
    if s["documented_missing"]:
        out.append(f"In the manual but not in this library: {', '.join(s['documented_missing'])}")
    out.append("")
    for t in lib["types"]:
        if not (t["methods"] or t["properties"] or t["values"]):
            continue
        out.append(f"{'## ' if md else ''}{t['kind']} {t['name']}" + (f" - {t['doc']}" if t.get("doc") else ""))
        if md and t["methods"]:
            out.append("")
            out.append("| | Method | Signature | Doc |")
            out.append("|---|---|---|---|")
        for m in t["methods"]:
            if _plumbing(m["name"]):
                continue
            mark = "   " if m["documented"] else "NEW"
            flag = f"  [{', '.join(m['interest'])}]" if m["interest"] and not m["documented"] else ""
            kind = "" if m["invoke"] == "method" else f" ({m['invoke']})"
            if md:
                out.append(f"| {mark.strip() or 'doc'} | {m['name']}{kind}{flag} | `{_sig(m)}` | {m.get('doc', '')} |")
            else:
                out.append(f"  {mark} {_sig(m)}{kind}{flag}" + (f"  -- {m['doc']}" if m.get("doc") else ""))
        for p in t["properties"]:
            out.append(f"{'- ' if md else '      '}property {p['type']} {p['name']}")
        for v in t["values"]:
            out.append(f"{'- ' if md else '      '}{v['name']} = {v.get('value')}")
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ls6t-typelib", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", action="append", help="the .ocx / .tlb / .dll / .exe to read (repeatable); default: "
                                                    "LabSpec's ActiveX found on this PC")
    ap.add_argument("--list", action="store_true", help="only list the files found")
    ap.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    ap.add_argument("--out", help="write to this file instead of the screen")
    a = ap.parse_args(argv)
    paths = a.path or find_files()
    if a.list or not paths:
        print("\n".join(paths) if paths else "no LabSpec ActiveX control or type library found: give --path "
                                             "(e.g. the NFActiveX.ocx in LabSpec's folder)")
        return 0 if paths else 1
    texts, libs = [], []
    for p in paths:
        try:
            lib = read(p)
        except OSError as exc:
            texts.append(f"{p}: cannot read a type library ({exc})")
            continue
        libs.append(lib)
        if a.format != "json":
            texts.append(render(lib, a.format))
    text = json.dumps(libs, indent=1) if a.format == "json" else "\n\n".join(texts)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"written: {a.out}")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
