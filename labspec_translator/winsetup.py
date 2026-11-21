"""winsetup.py — what the Windows installer does beyond copying files (installer/ls6t.nsi calls it).
Version: 2026.11.20.46

The installer puts a private Python (python.org's embeddable build, with every dependency already in its
Lib/site-packages) in <install>/python and the launchers in <install>/bin. Then it runs, with that Python:

  python -m labspec_translator.winsetup detect
  python -m labspec_translator.winsetup install [--claude] [--codex] [--gemini] [--sim] [--path] [--compile]
  python -m labspec_translator.winsetup uninstall
  python -m labspec_translator.winsetup check

detect:    which LLM apps are here (Claude desktop app; ChatGPT desktop app / Codex; Gemini CLI): clients.detect.
install:   checks that every package imports; precompiles the packages (a faster first start); writes the LabSpec
           scripts (`ls6t bridge install`); adds <install>/bin to the user's PATH (--path); adds the MCP server "ls6t"
           to each chosen app's configuration (--claude, --codex, --gemini) and the simulator "ls6t-sim" (--sim):
           clients.connect (backed up first; settings already in an existing entry are kept).
uninstall: removes the PATH entry and, in every app, the entries that run this Python. The profile, the bridge
           folder and the results (LS6T_HOME, LS6T_OUT) are kept.
Exit codes: 0 done, 1 failed, 2 done with warnings (the installer then asks to read its details).
Nothing here needs administrator rights: everything is per user. `ls6t connect` does the same for one app later.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import clients as C


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p))).rstrip("\\/")


def path_add(current: str, d: str) -> tuple[str, bool]:
    """The PATH string with d appended (once). Returns (new, changed)."""
    parts = [p for p in (current or "").split(";") if p]
    if any(_norm(os.path.expandvars(p)) == _norm(d) for p in parts):
        return current or "", False
    return ";".join(parts + [str(d)]), True


def path_remove(current: str, d: str) -> tuple[str, bool]:
    parts = [p for p in (current or "").split(";") if p]
    keep = [p for p in parts if _norm(os.path.expandvars(p)) != _norm(d)]
    return ";".join(keep), len(keep) != len(parts)


# --------------------------------------------------------------------------------------------- the machine

def _user_path() -> tuple[str, int]:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        try:
            v, t = winreg.QueryValueEx(k, "Path")
            return v, t
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ


def _set_user_path(value: str, kind: int):
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, kind or winreg.REG_EXPAND_SZ, value)
    # tell open programs (Explorer: new terminals) that the environment changed
    res = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(res))


class Log:
    def __init__(self, logfile: Path | None):
        self.file = logfile
        self.warnings: list[str] = []

    def __call__(self, msg: str):
        print(msg, flush=True)
        if self.file:
            try:
                with open(self.file, "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except OSError:
                pass

    def warn(self, msg: str):
        self.warnings.append(msg)
        self("WARNING: " + msg)


def check(log: Log) -> bool:
    ok = True
    for mod in ("numpy", "scipy", "matplotlib", "mcp", "labspec_translator"):
        try:
            m = __import__(mod)
            log(f"{mod} {getattr(m, '__version__', '')}".rstrip())
        except Exception as exc:  # noqa: BLE001
            log(f"FAILED: {mod}: {type(exc).__name__}: {exc}")
            ok = False
    if ok:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot  # noqa: F401
            from labspec_translator import mcp_server  # noqa: F401
            log("the MCP server and the plots load")
        except Exception as exc:  # noqa: BLE001
            log(f"FAILED: {type(exc).__name__}: {exc}")
            ok = False
    return ok


def report(reps: list, log: Log):
    for r in reps:
        if r.get("ok"):
            what = ", ".join(r.get("notes") or []) or (("removed " + ", ".join(r["removed"])) if r.get("removed") else "")
            if what:
                log(f"{r['app']}: {r['file']}: {what}" + (f" (backup: {Path(r['backup']).name})" if r.get("backup") else ""))
            if r.get("warning"):
                log.warn(f"{r['app']}: {r['warning']}")
        else:
            log.warn(f"{r['app']}: {r['file']}: {r.get('error')}")


def install(python: Path, bindir: Path, apps: list, sim: bool, add_path: bool, compile_: bool, log: Log) -> int:
    from . import __version__
    log(f"ls6t {__version__}: Python {sys.version.split()[0]} at {python}")
    if not check(log):
        log("the installed packages do not load: the installation is incomplete")
        return 1
    if compile_:
        import compileall
        import re
        site = Path(python).parent / "Lib" / "site-packages"
        log(f"precompiling {site} (a minute or two)")
        compileall.compile_dir(str(site), quiet=1, rx=re.compile(r"[\\/](tests?|testing)[\\/]"), workers=1)
    # the LabSpec scripts, with the folders an existing server entry uses (all apps and the scripts must agree)
    env = C.existing_env()
    for k in ("LS6T_HOME", "LS6T_BRIDGE_DIR"):
        if env.get(k):
            os.environ[k] = env[k]
            log(f"using {k}={env[k]} from an existing ls6t entry")
    try:
        from . import api
        r = api.install_bridge()
        for name, f in r["files"].items():
            log(f"LabSpec script: {f}")
        log(f"bridge folder: {r['bridge_dir']}")
        from . import profile as P
        prof = P.load()               # makes the profile from the bridge's last inventory, if there is one
        if prof.get("auto"):
            log(f"machine profile: made automatically from {prof['auto']['from']} (lasers capped at "
                f"{prof['auto']['max_filter_pct']} % until a person reviews it)")
        elif prof.get("measured"):
            log(f"machine profile: {prof.get('path')} ({prof.get('source')})")
        else:
            log("machine profile: none yet; the bridge takes the instrument's inventory when it first starts in "
                "LabSpec, and ls6t builds the profile from it")
    except Exception as exc:  # noqa: BLE001
        log.warn(f"the LabSpec scripts were not written ({exc}): run `ls6t bridge install` later")
    if add_path:
        try:
            cur, kind = _user_path()
            new, changed = path_add(cur, str(bindir))
            if changed:
                _set_user_path(new, kind)
                log(f"PATH: added {bindir} (new terminals see it)")
            else:
                log(f"PATH: {bindir} already there")
        except Exception as exc:  # noqa: BLE001
            log.warn(f"PATH not changed ({exc}): use the Start menu's 'ls6t PowerShell'")
    found = C.detect()
    for c in C.CLIENTS:
        log(f"{C.LABELS[c]}: " + (", ".join(found[c]) if found[c] else "not found"))
    if apps or sim:
        report(C.connect(apps or [c for c in C.CLIENTS if found[c]], str(python), sim=sim, main=bool(apps)), log)
        log("restart the apps you connected (quit them completely, then start them) to load the ls6t tools")
    else:
        log("no LLM app connected: run `ls6t connect` once one is installed")
    return 2 if log.warnings else 0


def uninstall(python: Path, bindir: Path, log: Log) -> int:
    try:
        cur, kind = _user_path()
        new, changed = path_remove(cur, str(bindir))
        if changed:
            _set_user_path(new, kind)
            log(f"PATH: removed {bindir}")
    except Exception as exc:  # noqa: BLE001
        log.warn(f"PATH not changed ({exc})")
    report(C.disconnect(list(C.CLIENTS), str(python)), log)
    log("kept: the profile, the bridge folder, the LabSpec scripts and the results (LS6T_HOME, LS6T_OUT)")
    return 2 if log.warnings else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="winsetup", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("action", choices=["install", "uninstall", "check", "detect"])
    for c in C.CLIENTS:
        ap.add_argument(f"--{c}", action="store_true", help=f"add the 'ls6t' server to the {C.LABELS[c]}")
    ap.add_argument("--sim", action="store_true", help="also add 'ls6t-sim' (the simulator) to those apps")
    ap.add_argument("--path", action="store_true", help="add the launchers' folder to the user's PATH")
    ap.add_argument("--compile", action="store_true", help="precompile the packages")
    ap.add_argument("--python", help="the Python the entries run (default: this one)")
    ap.add_argument("--bin", help="the launchers' folder (default: <python>/../bin)")
    a = ap.parse_args(argv)
    python = Path(a.python or sys.executable).resolve()
    bindir = Path(a.bin).resolve() if a.bin else python.parent.parent / "bin"
    log = Log(python.parent.parent / "install.log" if a.action in ("install", "uninstall") else None)
    if a.action == "check":
        return 0 if check(log) else 1
    if a.action == "detect":
        for c, where in C.detect().items():
            log(f"{c}={'1' if where else '0'}  {C.LABELS[c]}: {', '.join(where) or 'not found'}")
        return 0
    if os.name != "nt":
        log("winsetup install/uninstall are for Windows (elsewhere: ls6t connect)")
        return 1
    if a.action == "install":
        return install(python, bindir, [c for c in C.CLIENTS if getattr(a, c)], a.sim, a.path, a.compile, log)
    return uninstall(python, bindir, log)


if __name__ == "__main__":
    sys.exit(main())
