"""clients.py — connecting ls6t's MCP server to the LLM apps on this computer (`ls6t connect`, and the installer).

Each app keeps its MCP servers in its own file. ls6t knows nine that start local (stdio) servers:

| client  | app                                                     | file                                             |
|---------|---------------------------------------------------------|--------------------------------------------------|
| claude  | Claude desktop app                                      | %APPDATA%\\Claude\\claude_desktop_config.json (and the Microsoft Store copy's folder; macOS ~/Library/Application Support/Claude) |
| codex   | ChatGPT desktop app, Codex CLI and IDE extension (they share it) | %CODEX_HOME% or ~/.codex/config.toml (TOML) |
| gemini  | Gemini CLI                                              | ~/.gemini/settings.json                          |
| cursor  | Cursor (the editor; its agent and chat)                 | ~/.cursor/mcp.json (global: every project)       |
| claudecode | Claude Code (terminal, IDE extensions)               | %CLAUDE_CONFIG_DIR%\.claude.json or ~/.claude.json (user scope: mcpServers at the top) |
| copilot | GitHub Copilot (VS Code / Visual Studio agent, Copilot CLI) | %COPILOT_HOME%\mcp-config.json or ~/.copilot/mcp-config.json |
| windsurf | Windsurf (Cascade)                                     | ~/.codeium/windsurf/mcp_config.json (and windsurf-next's when present) |
| lmstudio | LM Studio (local models: nothing leaves the PC)        | ~/.lmstudio/mcp.json                             |
| bionic  | LM Studio Bionic (the agent app; its own list)          | ~/.lmstudio/apps/bionic/.internal/ng-mcp.json ({"servers": [...]}); and the ls6t skill: ~/.lmstudio/skills/ls6t/SKILL.md |

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
CLIENTS = ("claude", "codex", "gemini", "cursor", "claudecode", "copilot", "windsurf", "lmstudio", "bionic")
LABELS = {"claude": "Claude desktop app", "codex": "ChatGPT desktop app / Codex", "gemini": "Gemini CLI",
          "cursor": "Cursor", "claudecode": "Claude Code", "copilot": "GitHub Copilot (VS Code, Visual Studio, CLI)",
          "windsurf": "Windsurf", "lmstudio": "LM Studio", "bionic": "LM Studio Bionic"}
ALIASES = {"claude-code": "claudecode", "lm-studio": "lmstudio", "vscode": "copilot", "github-copilot": "copilot",
           "lm-studio-bionic": "bionic", "lmstudio-bionic": "bionic"}


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


def is_claudecode_process(name: str, path: str, cmdline: str) -> bool:
    """A process of Claude Code, not of the Claude desktop app (both can be claude.exe): Claude Code's native
    claude.exe lives outside the desktop app's folders (WindowsApps, AnthropicClaude), and its npm install runs as
    node.exe with @anthropic-ai/claude-code on the command line."""
    name, path, cmd = (name or "").lower(), (path or "").lower().replace("/", "\\"), (cmdline or "").lower()
    if name == "node.exe":
        return "claude-code" in cmd
    if name == "claude.exe":
        return bool(path) and not any(k in path for k in ("\\windowsapps\\", "\\anthropicclaude\\", "\\claude-3p\\"))
    return False


def claudecode_running() -> bool:
    """Is a Claude Code session running now (Windows)? It rewrites .claude.json while it runs, so a change made
    meanwhile can be lost: only then does connecting it warn (2026-11-22: a warning every time, with no session open,
    made each install end 'with warnings')."""
    if os.name != "nt":
        return False
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='claude.exe' OR Name='node.exe'\" | "
                              "ForEach-Object { $_.Name + [char]9 + $_.ExecutablePath + [char]9 + $_.CommandLine }"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (out or "").splitlines():
        parts = (line.split("\t") + ["", "", ""])[:3]
        if is_claudecode_process(*parts):
            return True
    return False


def claude_dirs(create: bool = False, running: bool = True) -> list[Path]:
    """The folders the Claude desktop app reads claude_desktop_config.json from (Windows, in this order):
    the running app's --user-data-dir; CLAUDE_USER_DATA_DIR; for each Microsoft Store package (Claude_*,
    AnthropicPBC.Claude_*) %LOCALAPPDATA%\\Claude-Data\\<package family name, lower case> once the app has said it moved
    its data there (the old folder holds its .user-data-left.<name> marker), else the package's LocalCache\\Roaming\\Claude
    (older builds) and also any Claude-Data folder: without the marker both are written, since the Store build did
    read the Packages copy on 2026-11-20 and a Claude-Data folder alone does not prove it stopped; %LOCALAPPDATA%\\Claude-3p (the variant on a third-party cloud);
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
            # only the app's own marker says it left the old folder (a Claude-Data folder existing is not enough)
            moved = any(f.name.lower() == f".user-data-left.{name}" for f in (old.iterdir() if old.is_dir() else []))
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


def claude_stale_files(existing_only: bool = True) -> list[Path]:
    """Configuration files the app no longer reads (Store builds that moved to Claude-Data, by the app's own marker):
    ls6t removes its entries there so they do not mislead. existing_only False: also the places where the file is
    gone (the install log then says so)."""
    local = os.environ.get("LOCALAPPDATA")
    if not _windows() or not local:
        return []
    active = {_norm(d) for d in claude_dirs(running=False)}
    out = []
    for pkg in sorted((Path(local) / "Packages").glob("*Claude_*")):
        old = pkg / "LocalCache" / "Roaming" / "Claude"
        if old.is_dir() and _norm(old) not in active and (not existing_only or (old / "claude_desktop_config.json").exists()):
            out.append(old / "claude_desktop_config.json")
    return out


def claude_files(create: bool = False) -> list[Path]:
    return [d / "claude_desktop_config.json" for d in claude_dirs(create)]


def codex_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or _home() / ".codex") / "config.toml"


def gemini_file() -> Path:
    return _home() / ".gemini" / "settings.json"


def cursor_file() -> Path:
    """Cursor's global MCP file (every project; a project's own .cursor/mcp.json can add more)."""
    return _home() / ".cursor" / "mcp.json"


def claudecode_file() -> Path:
    """Claude Code's user-scope servers: mcpServers at the top of .claude.json (CLAUDE_CONFIG_DIR when set). Edited
    directly rather than through `claude mcp add --scope user`, which some versions wrote under one project
    (anthropics/claude-code#16728); Claude Code must not be running meanwhile (it rewrites this file)."""
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(d) if d else _home()) / ".claude.json"


def copilot_file() -> Path:
    """GitHub Copilot's own user file (portable mcpServers format), read by the Copilot agent in VS Code and Visual
    Studio and by Copilot CLI; COPILOT_HOME replaces ~/.copilot (VS Code then checks only that one)."""
    return Path(os.environ.get("COPILOT_HOME") or _home() / ".copilot") / "mcp-config.json"


def windsurf_files(create: bool = False) -> list[Path]:
    """Windsurf's mcp_config.json: the stable build's, and the Next build's when that one is installed."""
    base = _home() / ".codeium"
    out = [d / "mcp_config.json" for d in (base / "windsurf", base / "windsurf-next") if d.is_dir()]
    return out or ([base / "windsurf" / "mcp_config.json"] if create else [])


def lmstudio_file() -> Path:
    return _home() / ".lmstudio" / "mcp.json"


def bionic_dir() -> Path:
    return _home() / ".lmstudio" / "apps" / "bionic"


def bionic_file() -> Path:
    """LM Studio Bionic's own list of MCP servers (2026-11-24, Nicola: Bionic did not take the ls6t entry from
    LM Studio's mcp.json; the one added in its Settings > MCP landed here, as {"servers": [{id, name, enabled,
    connection: {type: "stdio", command, args, env}}]})."""
    return bionic_dir() / ".internal" / "ng-mcp.json"


def bionic_install() -> Path | None:
    """Bionic.exe, from the location Bionic records (C:\\Program Files\\Bionic on the XploRA PC), else the usual
    places."""
    try:
        p = json.loads((bionic_dir() / ".internal" / "app-install-location.json").read_text(encoding="utf-8-sig"))
        if p.get("path") and Path(p["path"]).exists():
            return Path(p["path"])
    except (OSError, ValueError, AttributeError):
        pass
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA") and
                     str(Path(os.environ["LOCALAPPDATA"]) / "Programs")):
            if base and (Path(base) / "Bionic" / "Bionic.exe").exists():
                return Path(base) / "Bionic" / "Bionic.exe"
    return None


def bionic_running() -> bool:
    """Bionic open now (Windows): it reads its list when it starts, and may write it back when it quits."""
    if os.name != "nt":
        return False
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Bionic.exe", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "bionic.exe" in (out or "").lower()


def _undouble(v):
    """A Windows path typed with JSON's escaping into a form (C:\\\\Users: two backslashes in the value) back to one;
    a network path's leading \\\\ stays."""
    if not isinstance(v, str) or "\\\\" not in v:
        return v
    lead = "\\\\" if v.startswith("\\\\") and not v.startswith("\\\\\\") else ""
    body = v[len(lead):]
    while "\\\\" in body:
        body = body.replace("\\\\", "\\")
    return lead + body


SKILL_NAME = "ls6t"


def skill_source() -> Path:
    """The ls6t skill shipped with the package (Agent Skills format: SKILL.md, name ls6t)."""
    return Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md"


def bionic_skill_file() -> Path:
    """Where Bionic keeps a skill added in Settings > Skills (2026-11-25, Nicola: %USERPROFILE%\\.lmstudio\\skills\\ls6t\\
    SKILL.md; its own built-ins are under apps\\bionic\\.internal\\skills, pinned by hash: never touched)."""
    return _home() / ".lmstudio" / "skills" / SKILL_NAME / "SKILL.md"


def install_skill() -> dict:
    """The ls6t skill into Bionic's skills folder: only SKILL.md is written (a different one there is backed up);
    nothing else in the folder is touched."""
    src, dst = skill_source(), bionic_skill_file()
    text = src.read_text(encoding="utf-8")
    if dst.exists() and dst.read_text(encoding="utf-8-sig") == text:
        return {"skill": str(dst), "note": "skill 'ls6t' already up to date"}
    backup = _backup_and_write(dst, text)
    return {"skill": str(dst), "note": ("updated" if backup else "added") + " skill 'ls6t'",
            **({"skill_backup": str(backup)} if backup else {})}


def remove_skill() -> dict:
    """The ls6t skill out of Bionic: SKILL.md when it is ls6t's (its name), the folder only if then empty."""
    dst = bionic_skill_file()
    if not dst.exists():
        return {}
    head = dst.read_text(encoding="utf-8-sig")[:400]
    if not re.search(r"^name:\s*ls6t\s*$", head, flags=re.M):
        return {"skill_kept": f"{dst}: not ls6t's skill"}
    dst.unlink()
    out = {"skill_removed": str(dst)}
    try:
        dst.parent.rmdir()                  # only when empty: other files of the person's stay
    except OSError:
        out["skill_folder_kept"] = f"{dst.parent}: holds other files"
    return out


def bionic_servers(cfg: dict) -> dict:
    """Bionic's list as {name: {command, args, env}} (as the other apps' entries)."""
    out = {}
    for e in (cfg or {}).get("servers") or []:
        if isinstance(e, dict) and e.get("name"):
            c = e.get("connection") or {}
            out[e["name"]] = {"command": c.get("command"), "args": c.get("args") or [], "env": c.get("env") or {},
                              "url": c.get("url"), "enabled": e.get("enabled", True)}
    return out


def bionic_merge(cfg: dict, name: str, python: str, defaults: dict, forced: dict | None = None) -> tuple[dict, str]:
    """cfg (Bionic's ng-mcp.json) with the server `name` running this Python: its id and on/off kept, the person's
    env settings kept (paths typed with doubled backslashes made single)."""
    import uuid
    cfg = dict(cfg or {})
    servers = [dict(e) for e in (cfg.get("servers") or []) if isinstance(e, dict)]
    idx = next((i for i, e in enumerate(servers) if e.get("name") == name), None)
    old = servers[idx] if idx is not None else None
    oc = (old or {}).get("connection") or {}
    env = dict(defaults)
    if isinstance(oc.get("env"), dict):
        env.update({k: _undouble(v) for k, v in oc["env"].items() if _real(v)})
    env.update(forced or {})
    new = {**(old or {}), "id": (old or {}).get("id") or str(uuid.uuid4()), "name": name,
           "enabled": (old or {}).get("enabled", True),
           "connection": {**{k: v for k, v in oc.items() if k not in ("command", "args", "env", "url")},
                          "type": "stdio", "command": str(python), "args": list(MODULE_ARGS), "env": env}}
    if idx is None:
        servers.append(new)
        note = f"added '{name}'"
    else:
        servers[idx] = new
        oldc = _undouble(oc.get("command") or "")
        note = (f"updated '{name}'" if _norm(oldc) == _norm(python) else
                f"replaced '{name}' (it ran {oc.get('command') or oc.get('url')})")
        if not new["enabled"]:
            note += " (it is switched off in Bionic's Settings > MCP: switch it on there)"
    cfg["servers"] = servers
    return cfg, note


def bionic_remove(cfg: dict, name: str, python: str) -> tuple[dict, bool]:
    cfg = dict(cfg or {})
    servers = [e for e in (cfg.get("servers") or []) if isinstance(e, dict)]
    keep = [e for e in servers if not (e.get("name") == name and
                                       _norm(_undouble((e.get("connection") or {}).get("command") or "")) == _norm(python))]
    if len(keep) == len(servers):
        return cfg, False
    cfg["servers"] = keep
    return cfg, True


def _servers(client: str, path: Path) -> dict:
    """{name: entry} from one app's file (raises OSError / ValueError as the readers do)."""
    if client == "codex":
        return ((tomllib.loads(path.read_text(encoding="utf-8")) if (tomllib and path.exists()) else {})
                .get("mcp_servers") or {})
    if client == "bionic":
        return bionic_servers(read_json(path))
    return read_json(path).get("mcpServers") or {}


def files(client: str, create: bool = False) -> list[Path]:
    if client == "claude":
        return claude_files(create)
    if client == "codex":
        return [codex_file()]
    if client == "gemini":
        return [gemini_file()]
    if client == "cursor":
        return [cursor_file()]
    if client == "claudecode":
        return [claudecode_file()]
    if client == "copilot":
        return [copilot_file()]
    if client == "windsurf":
        return windsurf_files(True)
    if client == "lmstudio":
        return [lmstudio_file()]
    if client == "bionic":
        # Bionic writes its folder when it first runs: without it there is no list to add to
        return [bionic_file()] if (bionic_dir().is_dir() or create and bionic_install()) else []
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
    if cursor_file().parent.is_dir():
        found["cursor"].append(str(cursor_file().parent))
    if os.name == "nt" and local and (Path(local) / "Programs" / "cursor" / "Cursor.exe").exists():
        found["cursor"].append(str(Path(local) / "Programs" / "cursor"))
    if sys.platform == "darwin" and Path("/Applications/Cursor.app").is_dir():
        found["cursor"].append("/Applications/Cursor.app")
    if claudecode_file().exists() or (_home() / ".claude").is_dir():
        found["claudecode"].append(str(claudecode_file()))
    if copilot_file().parent.is_dir():
        found["copilot"].append(str(copilot_file().parent))
    found["copilot"] += [str(p) for p in sorted((_home() / ".vscode" / "extensions").glob("github.copilot*"))[:1]]
    found["windsurf"] += [str(p.parent) for p in windsurf_files()]
    if lmstudio_file().parent.is_dir():
        found["lmstudio"].append(str(lmstudio_file().parent))
    if bionic_dir().is_dir():
        found["bionic"].append(str(bionic_dir()))
    if bionic_install():
        found["bionic"].append(str(bionic_install()))
    if os.name == "nt" and local:
        for client, sub in (("windsurf", "Windsurf"), ("lmstudio", "LM Studio")):
            if (Path(local) / "Programs" / sub).is_dir():
                found[client].append(str(Path(local) / "Programs" / sub))
    for client, exe in (("codex", "codex"), ("gemini", "gemini"), ("cursor", "cursor"), ("claudecode", "claude"),
                        ("copilot", "copilot"), ("windsurf", "windsurf"), ("lmstudio", "lms")):
        w = shutil.which(exe)
        if w:
            found[client].append(w)
    return {c: sorted(set(v)) for c, v in found.items()}


# --------------------------------------------------------------------------------------------- JSON clients

def _entry(client: str, python: str, env: dict) -> dict:
    e = {"command": str(python), "args": list(MODULE_ARGS), "env": dict(env)}
    if client == "gemini":
        e.update(timeout=TOOL_TIMEOUT_S * 1000, trust=False)    # trust false: Gemini CLI asks before each tool
    if client in ("cursor", "claudecode"):
        e = {"type": "stdio", **e}            # their documented form for a local server
    if client == "copilot":
        e = {"type": "stdio", **e, "tools": ["*"]}    # the portable format: which tools; approvals still apply
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
        for k in ("timeout", "trust", "tools"):        # the person's choices (Copilot's tool list) win
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


def is_ls6t_entry(e) -> bool:
    """An entry that runs ls6t's MCP server, whichever Python (this install, an older one, a source checkout)."""
    return isinstance(e, dict) and "labspec_translator.mcp_server" in " ".join(str(a) for a in e.get("args") or [])


def remove_server(cfg: dict, name: str, python: str, any_ls6t: bool = False) -> tuple[dict, bool]:
    """cfg without mcpServers[name] when it runs this Python (any_ls6t: any ls6t server, for files no app reads)."""
    cfg = dict(cfg or {})
    servers = dict(cfg.get("mcpServers") or {})
    old = servers.get(name)
    if isinstance(old, dict) and (_norm(old.get("command", "")) == _norm(python) or (any_ls6t and is_ls6t_entry(old))):
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
                if not p.exists():
                    continue
                env = (_servers(client, p).get(ENTRY) or {}).get("env") or {}
            except (OSError, ValueError):
                continue
            env = {k: str(_undouble(v)) for k, v in env.items() if k.startswith("LS6T_") and k != "LS6T_MODE"
                   and _real(v)}
            if env:
                return env
    return {}


def connect(clients: list, python: str, sim: bool = False, main: bool = True, test: bool | None = None,
            tools: str | None = None) -> list[dict]:
    """Adds "ls6t" (main) and "ls6t-sim" (sim) to each client's file. test True / False turns test mode (the
    hardware_test tool) on / off in those entries; None leaves it as it is. Returns one report per file:
    {client, file, ok, notes | error, backup}."""
    reports = []
    shared = existing_env()
    shared.pop("LS6T_TEST_MODE", None)
    tm = {} if test is None else {"LS6T_TEST_MODE": "1" if test else "0"}
    if tools:
        # 'measure': the small tool set for small local models (flat measure_spectrum / calibrate_si, no laser_on or
        # set); 'full': everything (mcp_server, LS6T_TOOLS)
        tm["LS6T_TOOLS"] = tools
    for client in clients:
        if client == "bionic" and not files(client, create=True):
            reports.append({"client": client, "app": LABELS[client], "file": str(bionic_file()), "ok": False,
                            "error": "Bionic's folder is not there (it was never started for this user): start Bionic "
                                     "once, quit it, then `ls6t connect bionic`"})
            continue
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
                elif client == "bionic":
                    cfg = read_json(path)
                    notes = []
                    if main:
                        cfg, n = bionic_merge(cfg, ENTRY, python, {**default_env(), **shared}, forced=tm)
                        notes.append(n)
                    if sim:
                        cfg, n = bionic_merge(cfg, SIM_ENTRY, python, {**default_env(True), **_home_only(shared)},
                                              forced={"LS6T_MODE": "sim", **tm})
                        notes.append(n)
                    rep["backup"] = _str(write_json(path, cfg))
                    try:
                        sk = install_skill()
                        notes.append(sk.pop("note"))
                        rep.update(sk)
                    except OSError as exc:
                        rep["warning"] = f"the ls6t skill could not be written ({exc}): add it in Bionic's Settings > Skills"
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
                if client == "claudecode" and claudecode_running():
                    rep["warning"] = ("a Claude Code session is running, and Claude Code rewrites this file while it "
                                      "runs: end it, then check with `ls6t connect check claudecode` (if the entry "
                                      "is gone: `ls6t connect claudecode`)")
                if client == "bionic" and bionic_running():
                    rep["warning"] = ("Bionic is open: quit it fully and start it again to load ls6t (if ls6t is not "
                                      "under Settings > MCP then, run `ls6t connect bionic` with Bionic closed)")
                if client == "claude" and claude_is_running():
                    rep["warning"] = ("the Claude app is running and rewrites this file when it quits: quit it fully "
                                      "(tray icon > Quit), then run `ls6t connect claude` again")
            except (OSError, ValueError) as exc:
                rep.update(ok=False, error=f"{type(exc).__name__}: {exc}: left unchanged; add ls6t by hand (INSTALL.md)")
            reports.append(rep)
        if client == "claude":
            reports += clean_stale(python)
    return reports


def clean_stale(python: str) -> list[dict]:
    """ls6t's entries out of the Claude configuration files the app no longer reads: a Store build's Packages\\...\\LocalCache
    copy only once the app's own .user-data-left marker is there (claude_dirs); then any ls6t server there, whichever Python wrote it,
    since nothing reads the file (2026-11-22: the Packages copy kept an old entry from .24 on, silently). Each file
    gets a report: removed, or why not."""
    reports = []
    for stale in claude_stale_files(existing_only=False):
        rep = {"client": "claude", "app": LABELS["claude"], "file": str(stale)}
        if not stale.exists():          # said anyway, so the log shows the old copy was checked (2026-11-22)
            reports.append(dict(rep, ok=True, notes=["no config file here: the Claude app has moved to Claude-Data "
                                                     "(its .user-data-left marker); nothing to clean"]))
            continue
        try:
            cfg = read_json(stale)
            gone, kept = [], []
            for name in (ENTRY, SIM_ENTRY):
                cfg, g = remove_server(cfg, name, python, any_ls6t=True)
                if g:
                    gone.append(name)
                elif name in (cfg.get("mcpServers") or {}):
                    kept.append(name)
            if gone:
                rep["backup"] = _str(write_json(stale, cfg))
            rep.update(ok=True, notes=[f"removed {', '.join(gone)}: the Claude app no longer reads this file"] if gone
                       else [f"left {', '.join(kept)}: not an ls6t server"] if kept
                       else ["no ls6t entry (the Claude app no longer reads this file)"])
        except (OSError, ValueError) as exc:
            rep.update(ok=False, error=f"{type(exc).__name__}: {exc}: the Claude app no longer reads this file; "
                                       "remove its 'ls6t' entry by hand")
        reports.append(rep)
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
                elif client == "bionic":
                    cfg = read_json(path)
                    for name in (ENTRY, SIM_ENTRY):
                        cfg, gone = bionic_remove(cfg, name, python)
                        if gone:
                            rep["removed"].append(name)
                    if rep["removed"]:
                        write_json(path, cfg)
                    rep.update(remove_skill())
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
                servers = _servers(client, p)
            except (OSError, ValueError) as exc:
                entries[str(p)] = {"error": str(exc)}
                continue
            entries[str(p)] = {n: {"command": (servers.get(n) or {}).get("command"),
                                   "this_python": _norm(_undouble((servers.get(n) or {}).get("command") or ""))
                                   == _norm(python)}
                               for n in (ENTRY, SIM_ENTRY) if n in servers}
        out[client] = {"app": LABELS[client], "found": found[client], "files": entries}
    return out


# --------------------------------------------------------------------------------------------- does it start?

def _entries(client: str) -> list:
    out = []
    for p in files(client):
        try:
            servers = _servers(client, p)
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
