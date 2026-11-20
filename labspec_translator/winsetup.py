"""winsetup.py — what the Windows installer does beyond copying files (installer/ls6t.nsi calls it).
Version: 2026.11.20.2

The installer puts a private Python (python.org's embeddable build, with every dependency already in its
Lib/site-packages) in <install>/python and the launchers in <install>/bin. Then it runs, with that Python:

  python -m labspec_translator.winsetup install [--claude] [--sim] [--path] [--compile]
  python -m labspec_translator.winsetup uninstall
  python -m labspec_translator.winsetup check

install:   checks that every package imports; precompiles the packages (a faster first start); writes the LabSpec
           scripts (`ls6t bridge install`); adds <install>/bin to the user's PATH (--path); adds the MCP server to the
           Claude desktop app's configuration as "ls6t" (--claude) and a simulator entry "ls6t-sim" (--sim). The
           configuration is backed up first; settings already in an existing entry's env are kept.
uninstall: removes the PATH entry and the Claude entries that point to this Python. The profile, the bridge folder
           and the results (LS6T_HOME, LS6T_OUT) are kept.
Exit codes: 0 done, 1 failed, 2 done with warnings (the installer then asks to read its details).
Nothing here needs administrator rights: everything is per user.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ENTRY = "ls6t"
SIM_ENTRY = "ls6t-sim"
MODULE_ARGS = ["-m", "labspec_translator.mcp_server"]


# --------------------------------------------------------------------------------------------- pure helpers

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


def server_entry(python: str, env: dict) -> dict:
    return {"command": str(python), "args": list(MODULE_ARGS), "env": dict(env)}


def merge_server(cfg: dict, name: str, python: str, defaults: dict, forced: dict | None = None) -> tuple[dict, str]:
    """cfg with mcpServers[name] pointing to this Python. Settings already in the entry's env are kept (defaults
    fill the missing ones; forced always win). Returns (cfg, what happened)."""
    cfg = dict(cfg or {})
    servers = dict(cfg.get("mcpServers") or {})
    old = servers.get(name)
    env = dict(defaults)
    if isinstance(old, dict) and isinstance(old.get("env"), dict):
        env.update(old["env"])
    env.update(forced or {})
    servers[name] = server_entry(python, env)
    cfg["mcpServers"] = servers
    if not old:
        note = f"added '{name}'"
    elif _norm(old.get("command", "")) == _norm(python):
        note = f"updated '{name}'"
    else:
        note = f"replaced '{name}' (it ran {old.get('command')})"
    return cfg, note


def remove_server(cfg: dict, name: str, python: str) -> tuple[dict, bool]:
    """Removes mcpServers[name] only if it runs this Python (an entry of another install is left alone)."""
    cfg = dict(cfg or {})
    servers = dict(cfg.get("mcpServers") or {})
    old = servers.get(name)
    if isinstance(old, dict) and _norm(old.get("command", "")) == _norm(python):
        servers.pop(name)
        cfg["mcpServers"] = servers
        return cfg, True
    return cfg, False


# --------------------------------------------------------------------------------------------- the machine

def claude_configs(create: bool) -> list[Path]:
    """The Claude desktop app's configuration files: %APPDATA%\\Claude (created when `create`, so an app installed
    later finds the entry) and the Microsoft Store version's folder, if present."""
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        d = Path(appdata) / "Claude"
        if d.is_dir() or create:
            out.append(d / "claude_desktop_config.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for pkg in sorted((Path(local) / "Packages").glob("Claude_*")):
            d = pkg / "LocalCache" / "Roaming" / "Claude"
            if d.is_dir():
                out.append(d / "claude_desktop_config.json")
    return out


def _read_cfg(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("not a JSON object")
    return cfg


def _write_cfg(path: Path, cfg: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + f".ls6t-backup-{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".ls6t-tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


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


def _existing_env() -> dict:
    """LS6T_* settings of an existing "ls6t" Claude entry: the scripts must use the same folders as the server."""
    for p in claude_configs(create=False):
        try:
            env = (_read_cfg(p).get("mcpServers", {}).get(ENTRY) or {}).get("env") or {}
        except (OSError, ValueError):
            continue
        return {k: str(v) for k, v in env.items() if k.startswith("LS6T_")}
    return {}


def install(python: Path, bindir: Path, claude: bool, sim: bool, add_path: bool, compile_: bool, log: Log) -> int:
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
    # the LabSpec scripts, with the folders an existing server entry uses
    env = _existing_env()
    for k in ("LS6T_HOME", "LS6T_BRIDGE_DIR"):
        if env.get(k):
            os.environ[k] = env[k]
            log(f"using {k}={env[k]} from the existing Claude entry")
    try:
        from . import api
        r = api.install_bridge()
        for name, f in r["files"].items():
            log(f"LabSpec script: {f}")
        log(f"bridge folder: {r['bridge_dir']}")
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
    if claude or sim:
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        for cfgp in claude_configs(create=True):
            try:
                cfg = _read_cfg(cfgp)
            except (OSError, ValueError) as exc:
                log.warn(f"{cfgp} is not valid JSON ({exc}): left unchanged; add the server by hand (INSTALL.md)")
                continue
            notes = []
            if claude:
                cfg, n = merge_server(cfg, ENTRY, str(python), {"LS6T_CONFIRM": "request"})
                notes.append(n)
            if sim:
                cfg, n = merge_server(cfg, SIM_ENTRY, str(python), {"LS6T_OUT": str(home / "ls6t_sim_runs")},
                                      forced={"LS6T_MODE": "sim"})
                notes.append(n)
            try:
                backup = _write_cfg(cfgp, cfg)
            except OSError as exc:
                log.warn(f"{cfgp} could not be written ({exc})")
                continue
            log(f"Claude configuration {cfgp}: {', '.join(notes)}" + (f" (backup: {backup.name})" if backup else ""))
        log("restart the Claude app (Quit from its tray icon, then start it) to load the ls6t tools")
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
    for cfgp in claude_configs(create=False):
        try:
            cfg = _read_cfg(cfgp)
        except (OSError, ValueError):
            continue
        removed = []
        for name in (ENTRY, SIM_ENTRY):
            cfg, gone = remove_server(cfg, name, str(python))
            if gone:
                removed.append(name)
        if removed:
            _write_cfg(cfgp, cfg)
            log(f"Claude configuration {cfgp}: removed {', '.join(removed)}")
    log("kept: the profile, the bridge folder, the LabSpec scripts and the results (LS6T_HOME, LS6T_OUT)")
    return 2 if log.warnings else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="winsetup", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("action", choices=["install", "uninstall", "check"])
    ap.add_argument("--claude", action="store_true", help="add the 'ls6t' server to the Claude app")
    ap.add_argument("--sim", action="store_true", help="add the 'ls6t-sim' server (the simulator)")
    ap.add_argument("--path", action="store_true", help="add the launchers' folder to the user's PATH")
    ap.add_argument("--compile", action="store_true", help="precompile the packages")
    ap.add_argument("--python", help="the Python the Claude entries run (default: this one)")
    ap.add_argument("--bin", help="the launchers' folder (default: <python>/../bin)")
    a = ap.parse_args(argv)
    python = Path(a.python or sys.executable).resolve()
    bindir = Path(a.bin).resolve() if a.bin else python.parent.parent / "bin"
    log = Log(python.parent.parent / "install.log" if a.action != "check" else None)
    if a.action == "check":
        return 0 if check(log) else 1
    if os.name != "nt":
        log("winsetup install/uninstall are for Windows")
        return 1
    if a.action == "install":
        return install(python, bindir, a.claude, a.sim, a.path, a.compile, log)
    return uninstall(python, bindir, log)


if __name__ == "__main__":
    sys.exit(main())
