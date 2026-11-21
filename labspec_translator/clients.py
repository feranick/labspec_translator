"""clients.py — connecting ls6t's MCP server to the LLM apps on this computer (`ls6t connect`, and the installer).
Version: 2026.11.20.36

Each app keeps its MCP servers in its own file. ls6t knows three that start local (stdio) servers:

| client  | app                                                     | file                                             |
|---------|---------------------------------------------------------|--------------------------------------------------|
| claude  | Claude desktop app                                      | %APPDATA%\\Claude\\claude_desktop_config.json (and the Microsoft Store copy's folder; macOS ~/Library/Application Support/Claude) |
| codex   | ChatGPT desktop app, Codex CLI and IDE extension (they share it) | %CODEX_HOME% or ~/.codex/config.toml (TOML) |
| gemini  | Gemini CLI                                              | ~/.gemini/settings.json                          |

The Gemini app and ChatGPT on the web take only remote servers (an internet address): ls6t does not offer the
instrument to the internet, so they cannot use it.

The entry runs this Python: `<python> -m labspec_translator.mcp_server`, named "ls6t" ("ls6t-sim": the simulator).
Every entry gets the same LS6T_HOME and LS6T_OUT, so all apps share one instrument lock, bridge and profile.
Settings already in an existing entry's env are kept; the file is backed up before it is changed; a file that
cannot be read safely (invalid JSON or TOML, an entry written in a form ls6t does not edit) is left alone with a
warning. Removing takes out only entries that run this same Python.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:            # Python 3.10
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

ENTRY = "ls6t"
SIM_ENTRY = "ls6t-sim"
MODULE_ARGS = ["-m", "labspec_translator.mcp_server"]
TOOL_TIMEOUT_S = 3600          # a tool may wait for the operator's answer in LabSpec
CLIENTS = ("claude", "codex", "gemini")
LABELS = {"claude": "Claude desktop app", "codex": "ChatGPT desktop app / Codex", "gemini": "Gemini CLI"}


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p))).rstrip("\\/")


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) if os.name == "nt" else Path.home()


# --------------------------------------------------------------------------------------------- where

def _windows() -> bool:
    return os.name == "nt"


@functools.lru_cache(maxsize=1)
def claude_running_dirs() -> tuple:
    """The --user-data-dir of every running Claude.exe (Windows): the folder it reads its configuration from now."""
    if os.name != "nt":
        return ()
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
                              "ForEach-Object { $_.CommandLine }"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    dirs = re.findall(r'--user-data-dir=(?:"([^"]+)"|(\S+))', out or "")
    return tuple(sorted({Path(a or b) for a, b in dirs}))


def claude_is_running() -> bool:
    if os.name != "nt":
        return False
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude.exe" in (out or "").lower()


def claude_dirs(create: bool = False, running: bool = True) -> list[Path]:
    """The folders the Claude desktop app reads claude_desktop_config.json from (Windows, in this order):
    the running app's --user-data-dir; CLAUDE_USER_DATA_DIR; for each Microsoft Store package (Claude_*,
    AnthropicPBC.Claude_*) %LOCALAPPDATA%\\Claude-Data\\<package family name, lower case> once the app moved its data
    there (current builds; the old folder then holds a .user-data-left.<name> marker), else the package's
    LocalCache\\Roaming\\Claude (older builds); %LOCALAPPDATA%\\Claude-3p (the variant on a third-party cloud);
    %APPDATA%\\Claude (the installer from claude.ai, or when nothing else is found and `create`)."""
    if sys.platform == "darwin" and not _windows():
        d = Path.home() / "Library" / "Application Support" / "Claude"
        return [d] if (d.is_dir() or create) else []
    if not _windows():
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"
        return [d] if (d.is_dir() or create) else []
    out: list[Path] = []
    if running:
        out += list(claude_running_dirs())
    if os.environ.get("CLAUDE_USER_DATA_DIR"):
        out.append(Path(os.environ["CLAUDE_USER_DATA_DIR"]))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        data = Path(local) / "Claude-Data"
        pkgs = sorted(list((Path(local) / "Packages").glob("Claude_*")) +
                      list((Path(local) / "Packages").glob("AnthropicPBC.Claude_*")))
        for pkg in pkgs:
            name = pkg.name.lower()
            old = pkg / "LocalCache" / "Roaming" / "Claude"
            moved = (data / name).is_dir() or any(f.name.lower() == f".user-data-left.{name}"
                                                  for f in (old.iterdir() if old.is_dir() else []))
            out.append(data / name if moved else old)
        if data.is_dir():
            out += [d for d in sorted(data.iterdir()) if d.is_dir() and d.name.lower().startswith(("claude_",
                                                                                                    "anthropicpbc"))]
        if (Path(local) / "Claude-3p").is_dir():
            out.append(Path(local) / "Claude-3p")
    appdata = os.environ.get("APPDATA")
    if appdata:
        d = Path(appdata) / "Claude"
        if d.is_dir() or (create and not out):
            out.append(d)
    seen, uniq = set(), []
    for d in out:
        k = _norm(d)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def claude_stale_files() -> list[Path]:
    """Configuration files the app no longer reads (Store builds that moved to Claude-Data): ls6t removes its entries
    there so they do not mislead."""
    local = os.environ.get("LOCALAPPDATA")
    if not _windows() or not local:
        return []
    active = {_norm(d) for d in claude_dirs(running=False)}
    out = []
    for pkg in sorted((Path(local) / "Packages").glob("*Claude_*")):
        old = pkg / "LocalCache" / "Roaming" / "Claude"
        if _norm(old) not in active and (old / "claude_desktop_config.json").exists():
            out.append(old / "claude_desktop_config.json")
    return out


def claude_files(create: bool = False) -> list[Path]:
    return [d / "claude_desktop_config.json" for d in claude_dirs(create)]


def codex_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or _home() / ".codex") / "config.toml"


def gemini_file() -> Path:
    return _home() / ".gemini" / "settings.json"


def files(client: str, create: bool = False) -> list[Path]:
    if client == "claude":
        return claude_files(create)
    if client == "codex":
        return [codex_file()]
    if client == "gemini":
        return [gemini_file()]
    raise ValueError(f"client {client!r}: {', '.join(CLIENTS)}")


def detect() -> dict:
    """Which apps are on this computer (for this user): {client: [what was found]}; empty lists: not found."""
    found = {c: [] for c in CLIENTS}
    for p in claude_files(create=False):
        found["claude"].append(str(p.parent))
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        found["claude"] += [str(p) for p in (Path(local) / "Packages").glob("AnthropicPBC.Claude_*")]
        if (Path(local) / "AnthropicClaude").is_dir():
            found["claude"].append(str(Path(local) / "AnthropicClaude"))
        for pat in ("OpenAI.ChatGPT*", "OpenAI.Codex*"):
            found["codex"] += [str(p) for p in (Path(local) / "Packages").glob(pat)]
    if codex_file().parent.is_dir():
        found["codex"].append(str(codex_file().parent))
    if gemini_file().parent.is_dir():
        found["gemini"].append(str(gemini_file().parent))
    for client in ("codex", "gemini"):         # the CLIs, installed with npm or winget
        w = shutil.which(client)
        if w:
            found[client].append(w)
    return {c: sorted(set(v)) for c, v in found.items()}


# --------------------------------------------------------------------------------------------- JSON clients

def _entry(client: str, python: str, env: dict) -> dict:
    e = {"command": str(python), "args": list(MODULE_ARGS), "env": dict(env)}
    if client == "gemini":
        e.update(timeout=TOOL_TIMEOUT_S * 1000, trust=False)    # trust false: Gemini CLI asks before each tool
    return e


def _real(v) -> bool:
    """A setting someone filled in, not a placeholder copied from the docs (C:\\Users\\<you>\\...)."""
    return not re.search(r"<[^<>]*>", str(v))


def merge_server(cfg: dict, name: str, python: str, defaults: dict, forced: dict | None = None,
                 client: str = "claude") -> tuple[dict, str]:
    """cfg (a JSON client's) with mcpServers[name] running this Python. Settings already in the entry's env are kept
    (defaults fill the missing ones; forced always win). Returns (cfg, what happened)."""
    cfg = dict(cfg or {})
    servers = dict(cfg.get("mcpServers") or {})
    old = servers.get(name)
    env = dict(defaults)
    if isinstance(old, dict) and isinstance(old.get("env"), dict):
        env.update({k: v for k, v in old["env"].items() if _real(v)})
    env.update(forced or {})
    new = _entry(client, python, env)
    if isinstance(old, dict):                 # keep the person's other keys (timeout, trust, includeTools...)
        new = {**{k: v for k, v in old.items() if k not in ("command", "args", "env", "url", "httpUrl")}, **new}
        for k in ("timeout", "trust"):
            if k in old:
                new[k] = old[k]
    servers[name] = new
    cfg["mcpServers"] = servers
    if not old:
        note = f"added '{name}'"
    elif _norm(old.get("command", "")) == _norm(python):
        note = f"updated '{name}'"
    else:
        note = f"replaced '{name}' (it ran {old.get('command') or old.get('url')})"
    return cfg, note


def remove_server(cfg: dict, name: str, python: str) -> tuple[dict, bool]:
    cfg = dict(cfg or {})
    servers = dict(cfg.get("mcpServers") or {})
    old = servers.get(name)
    if isinstance(old, dict) and _norm(old.get("command", "")) == _norm(python):
        servers.pop(name)
        cfg["mcpServers"] = servers
        return cfg, True
    return cfg, False


def read_json(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("not a JSON object")
    return cfg


def _backup_and_write(path: Path, text: str) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + f".ls6t-backup-{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".ls6t-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return backup


def write_json(path: Path, cfg: dict) -> Path | None:
    return _backup_and_write(path, json.dumps(cfg, indent=2) + "\n")


# --------------------------------------------------------------------------------------------- TOML (Codex)

def _toml_str(s: str) -> str:
    s = str(s)
    if "'" not in s and "\n" not in s:
        return f"'{s}'"                      # a literal string: Windows paths need no escaping
    return json.dumps(s)                     # a basic string (JSON escapes are valid TOML)


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _table_path(header: str) -> list[str]:
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([A-Za-z0-9_-]+)', header)
    return [a or b or c for a, b, c in parts]


def _strip_table(text: str, name: str) -> str:
    """text without the tables [mcp_servers.<name>] and [mcp_servers.<name>.*] (any quoting of the keys)."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line)
        if m:
            p = _table_path(m.group(1))
            skip = len(p) >= 2 and p[0] == "mcp_servers" and p[1] == name
        if not skip:
            out.append(line)
    return "".join(out)


def toml_block(name: str, python: str, env: dict, old: dict | None = None) -> str:
    keep = {k: v for k, v in (old or {}).items() if k in ("startup_timeout_sec", "tool_timeout_sec", "enabled",
                                                         "enabled_tools", "disabled_tools",
                                                         "default_tools_approval_mode")}
    lines = [f"[mcp_servers.{json.dumps(name) if not re.fullmatch(r'[A-Za-z0-9_-]+', name) else name}]",
             f"command = {_toml_str(python)}",
             "args = [" + ", ".join(json.dumps(a) for a in MODULE_ARGS) + "]",
             f"startup_timeout_sec = {keep.pop('startup_timeout_sec', 60)}",
             f"tool_timeout_sec = {keep.pop('tool_timeout_sec', TOOL_TIMEOUT_S)}"]
    for k, v in keep.items():
        lines.append(f"{k} = {json.dumps(v)}")
    lines.append("")
    lines.append(f"[mcp_servers.{name}.env]")
    for k, v in env.items():
        lines.append(f"{k} = {_toml_str(v)}")
    return "\n".join(lines) + "\n"


def toml_merge(text: str, name: str, python: str, defaults: dict, forced: dict | None = None) -> tuple[str, str]:
    """The Codex config.toml text with [mcp_servers.<name>] running this Python; the rest of the file unchanged.
    Raises ValueError when the file is not valid TOML or holds the entry in a form ls6t does not edit."""
    if tomllib is None:
        raise ValueError("no TOML reader in this Python (install tomli)")
    cfg = tomllib.loads(text) if text.strip() else {}
    old = (cfg.get("mcp_servers") or {}).get(name)
    env = dict(defaults)
    if isinstance(old, dict) and isinstance(old.get("env"), dict):
        env.update({k: v for k, v in old["env"].items() if _real(v)})
    env.update(forced or {})
    rest = _strip_table(text, name)
    if name in ((tomllib.loads(rest) if rest.strip() else {}).get("mcp_servers") or {}):
        raise ValueError(f"mcp_servers.{name} is written inline or as dotted keys: edit it by hand")
    new = rest.rstrip("\n") + ("\n\n" if rest.strip() else "") + toml_block(name, python, env, old)
    tomllib.loads(new)                                   # still valid
    if not old:
        note = f"added '{name}'"
    elif _norm(old.get("command", "")) == _norm(python):
        note = f"updated '{name}'"
    else:
        note = f"replaced '{name}' (it ran {old.get('command') or old.get('url')})"
    return new, note


def toml_remove(text: str, name: str, python: str) -> tuple[str, bool]:
    if tomllib is None or not text.strip():
        return text, False
    old = (tomllib.loads(text).get("mcp_servers") or {}).get(name)
    if not (isinstance(old, dict) and _norm(old.get("command", "")) == _norm(python)):
        return text, False
    new = _strip_table(text, name)
    tomllib.loads(new)
    return new.rstrip("\n") + "\n", True


# --------------------------------------------------------------------------------------------- connect

def default_env(sim: bool = False) -> dict:
    """The settings every entry gets: the same LS6T_HOME (lock, bridge, profile) for all apps."""
    from . import config
    env = {"LS6T_HOME": str(config.home())}
    if sim:
        env.update(LS6T_MODE="sim", LS6T_OUT=str(_home() / "ls6t_sim_runs"))
    else:
        env.update(LS6T_CONFIRM="request", LS6T_OUT=str(config.out_dir()))
    return env


def existing_env() -> dict:
    """LS6T_* settings of an existing "ls6t" entry in any app: kept for the others (and for the LabSpec scripts)."""
    for client in CLIENTS:
        for p in files(client):
            try:
                if client == "codex":
                    if tomllib is None or not p.exists():
                        continue
                    env = ((tomllib.loads(p.read_text(encoding="utf-8")).get("mcp_servers") or {}).get(ENTRY) or {}
                           ).get("env") or {}
                else:
                    env = (read_json(p).get("mcpServers", {}).get(ENTRY) or {}).get("env") or {}
            except (OSError, ValueError):
                continue
            env = {k: str(v) for k, v in env.items() if k.startswith("LS6T_") and k != "LS6T_MODE" and _real(v)}
            if env:
                return env
    return {}


def connect(clients: list, python: str, sim: bool = False, main: bool = True, test: bool | None = None) -> list[dict]:
    """Adds "ls6t" (main) and "ls6t-sim" (sim) to each client's file. test True / False turns test mode (the
    hardware_test tool) on / off in those entries; None leaves it as it is. Returns one report per file:
    {client, file, ok, notes | error, backup}."""
    reports = []
    shared = existing_env()
    shared.pop("LS6T_TEST_MODE", None)
    tm = {} if test is None else {"LS6T_TEST_MODE": "1" if test else "0"}
    for client in clients:
        for path in files(client, create=True):
            rep = {"client": client, "app": LABELS[client], "file": str(path)}
            try:
                if client == "codex":
                    text = path.read_text(encoding="utf-8") if path.exists() else ""
                    notes = []
                    if main:
                        text, n = toml_merge(text, ENTRY, python, {**default_env(), **shared}, forced=tm)
                        notes.append(n)
                    if sim:
                        text, n = toml_merge(text, SIM_ENTRY, python, {**default_env(True), **_home_only(shared)},
                                             forced={"LS6T_MODE": "sim", **tm})
                        notes.append(n)
                    rep["backup"] = _str(_backup_and_write(path, text))
                else:
                    cfg = read_json(path)
                    notes = []
                    if main:
                        cfg, n = merge_server(cfg, ENTRY, python, {**default_env(), **shared}, forced=tm,
                                              client=client)
                        notes.append(n)
                    if sim:
                        cfg, n = merge_server(cfg, SIM_ENTRY, python, {**default_env(True), **_home_only(shared)},
                                              forced={"LS6T_MODE": "sim", **tm}, client=client)
                        notes.append(n)
                    rep["backup"] = _str(write_json(path, cfg))
                rep.update(ok=True, notes=notes)
                if client == "claude" and claude_is_running():
                    rep["warning"] = ("the Claude app is running and rewrites this file when it quits: quit it fully "
                                      "(tray icon > Quit), then run `ls6t connect claude` again")
            except (OSError, ValueError) as exc:
                rep.update(ok=False, error=f"{type(exc).__name__}: {exc}: left unchanged; add ls6t by hand (INSTALL.md)")
            reports.append(rep)
        if client == "claude":
            for stale in claude_stale_files():          # a folder the app no longer reads: take ours out
                try:
                    cfg = read_json(stale)
                    gone = []
                    for name in (ENTRY, SIM_ENTRY):
                        cfg, g = remove_server(cfg, name, python)
                        if g:
                            gone.append(name)
                    if gone:
                        write_json(stale, cfg)
                        reports.append({"client": client, "app": LABELS[client], "file": str(stale), "ok": True,
                                        "notes": [f"removed {', '.join(gone)}: Claude no longer reads this file"]})
                except (OSError, ValueError):
                    pass
    return reports


def disconnect(clients: list, python: str) -> list[dict]:
    reports = []
    for client in clients:
        for path in files(client) + (claude_stale_files() if client == "claude" else []):
            if not path.exists():
                continue
            rep = {"client": client, "app": LABELS[client], "file": str(path), "removed": []}
            try:
                if client == "codex":
                    text = path.read_text(encoding="utf-8")
                    for name in (ENTRY, SIM_ENTRY):
                        text, gone = toml_remove(text, name, python)
                        if gone:
                            rep["removed"].append(name)
                    if rep["removed"]:
                        _backup_and_write(path, text)
                else:
                    cfg = read_json(path)
                    for name in (ENTRY, SIM_ENTRY):
                        cfg, gone = remove_server(cfg, name, python)
                        if gone:
                            rep["removed"].append(name)
                    if rep["removed"]:
                        write_json(path, cfg)
                rep["ok"] = True
            except (OSError, ValueError) as exc:
                rep.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            reports.append(rep)
    return reports


def _home_only(env: dict) -> dict:
    return {k: v for k, v in env.items() if k == "LS6T_HOME"}


def _str(p):
    return str(p) if p else None


def status(python: str | None = None) -> dict:
    """Per app: found or not, its file(s), and whether "ls6t" / "ls6t-sim" are in them and run which Python."""
    python = python or sys.executable
    found = detect()
    out = {}
    for client in CLIENTS:
        entries = {}
        for p in files(client):
            try:
                if client == "codex":
                    servers = ((tomllib.loads(p.read_text(encoding="utf-8")) if (tomllib and p.exists()) else {})
                               .get("mcp_servers") or {})
                else:
                    servers = read_json(p).get("mcpServers") or {}
            except (OSError, ValueError) as exc:
                entries[str(p)] = {"error": str(exc)}
                continue
            entries[str(p)] = {n: {"command": (servers.get(n) or {}).get("command"),
                                   "this_python": _norm((servers.get(n) or {}).get("command", "")) == _norm(python)}
                               for n in (ENTRY, SIM_ENTRY) if n in servers}
        out[client] = {"app": LABELS[client], "found": found[client], "files": entries}
    return out


# --------------------------------------------------------------------------------------------- does it start?

def _entries(client: str) -> list:
    out = []
    for p in files(client):
        try:
            if client == "codex":
                servers = (tomllib.loads(p.read_text(encoding="utf-8")).get("mcp_servers") or {}) \
                    if (tomllib and p.exists()) else {}
            else:
                servers = read_json(p).get("mcpServers") or {}
        except (OSError, ValueError):
            continue
        if isinstance(servers.get(ENTRY), dict):
            out.append((p, servers[ENTRY]))
    return out


def handshake(command: str, args: list, env: dict | None = None, timeout_s: float = 60.0) -> dict:
    """Starts an MCP server as an app would (stdio) and asks for its tools: {ok, seconds, tools | error, stderr}."""
    import subprocess
    import threading
    t0 = time.monotonic()
    full_env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    try:
        proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=full_env, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except OSError as exc:
        return {"ok": False, "error": f"cannot start {command}: {exc}"}
    lines: list = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait_for(mid):
        while time.monotonic() - t0 < timeout_s:
            for ln in list(lines):
                try:
                    msg = json.loads(ln)
                except ValueError:
                    continue
                if msg.get("id") == mid:
                    return msg
            if proc.poll() is not None:
                return None
            time.sleep(0.05)
        return None

    out: dict = {"command": command, "args": args}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "ls6t-connect-check", "version": "1"}}})
        init = wait_for(1)
        if not init or "result" not in init:
            raise RuntimeError("no answer to initialize" if not init else f"initialize: {init.get('error')}")
        out["initialize_s"] = round(time.monotonic() - t0, 2)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = wait_for(2)
        if not tl or "result" not in tl:
            raise RuntimeError("no answer to tools/list")
        out.update(ok=True, seconds=round(time.monotonic() - t0, 2),
                   tools=[t["name"] for t in tl["result"].get("tools", [])])
    except (RuntimeError, OSError, ValueError) as exc:
        out.update(ok=False, error=str(exc))
    finally:
        try:
            proc.kill()
            _, err = proc.communicate(timeout=5)
        except (OSError, subprocess.SubprocessError):
            err = ""
        if not out.get("ok"):
            out["stderr"] = (err or "")[-3000:]
    return out


def check(clients: list | None = None) -> dict:
    """For each app: which file ls6t's entry is in, and whether that entry's server starts and lists its tools."""
    res = {}
    for client in clients or CLIENTS:
        ents = _entries(client)
        if not ents:
            res[client] = {"app": LABELS[client], "ok": False,
                           "error": "no 'ls6t' entry in " + (", ".join(str(p) for p in files(client)) or "any file") +
                                    f": run `ls6t connect {client}`"}
            continue
        p, e = ents[0]
        r = handshake(e.get("command", ""), e.get("args") or [], e.get("env") or {})
        res[client] = {"app": LABELS[client], "file": str(p), **r}
        if client == "claude":
            res[client]["claude_running"] = claude_is_running()
            res[client]["running_app_reads"] = [str(d) for d in claude_running_dirs()]
    return res
