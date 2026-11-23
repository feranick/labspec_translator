"""Tests: the Windows installer and the LLM apps."""
from helpers import *  # noqa: F401,F403


# --------------------------------------------------------------------------------------------- Windows installer

def test_winsetup_path_and_json_entries(tmp_path, monkeypatch):
    from labspec_translator import clients as C
    from labspec_translator import winsetup as W
    new, ch = W.path_add(r"C:\a;C:\b", r"C:\ls6t\bin")
    assert ch and new.endswith(r";C:\ls6t\bin")
    assert W.path_add(new, "C:\\ls6t\\bin\\") == (new, False)
    assert W.path_remove(new, r"C:\ls6t\bin") == (r"C:\a;C:\b", True)
    py = str(tmp_path / "python" / "python.exe")
    cfg = {"mcpServers": {"other": {"command": "x"},
                          "ls6t": {"command": "old.exe", "env": {"LS6T_OUT": "D:\\runs", "LS6T_CONFIRM": "study"}}},
           "globalShortcut": "x"}
    cfg2, note = C.merge_server(cfg, "ls6t", py, {"LS6T_CONFIRM": "request"})
    e = cfg2["mcpServers"]["ls6t"]
    assert "replaced" in note and e["command"] == py and e["args"] == ["-m", "labspec_translator.mcp_server"]
    assert e["env"] == {"LS6T_OUT": "D:\\runs", "LS6T_CONFIRM": "study"}          # the person's settings kept
    assert cfg2["mcpServers"]["other"] == {"command": "x"} and cfg2["globalShortcut"] == "x"
    g, _ = C.merge_server({"mcpServers": {"ls6t": {"command": py, "trust": True}}}, "ls6t", py, {}, client="gemini")
    assert g["mcpServers"]["ls6t"]["trust"] is True and g["mcpServers"]["ls6t"]["timeout"] == 3600000
    cfg3, _ = C.merge_server(cfg2, "ls6t-sim", py, {"LS6T_OUT": "s"}, forced={"LS6T_MODE": "sim"})
    assert cfg3["mcpServers"]["ls6t-sim"]["env"] == {"LS6T_OUT": "s", "LS6T_MODE": "sim"}
    cfg4, gone = C.remove_server(cfg3, "ls6t", py)
    assert gone and "ls6t" not in cfg4["mcpServers"]
    assert C.remove_server(cfg, "ls6t", py) == (cfg, False)                       # another install's entry stays
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    backup = C.write_json(p, cfg2)
    assert backup and json.loads(backup.read_text()) == cfg and C.read_json(p) == cfg2


def test_codex_toml_entry():
    from labspec_translator import clients as C
    tomllib = C.tomllib
    py = r"C:\Users\o'neil\AppData\Local\Programs\ls6t\python\python.exe"
    text = ('model = "gpt-5"\n\n[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "c7"]\n\n'
            '[mcp_servers.ls6t]\ncommand = \'C:\\old\\python.exe\'\ntool_timeout_sec = 120\n\n'
            '[mcp_servers.ls6t.env]\nLS6T_OUT = \'D:\\runs\'\n\n[profiles.x]\nmodel = "o3"\n')
    new, note = C.toml_merge(text, "ls6t", py, {"LS6T_CONFIRM": "request", "LS6T_OUT": "x"})
    d = tomllib.loads(new)
    e = d["mcp_servers"]["ls6t"]
    assert "replaced" in note and e["command"] == py and e["args"] == ["-m", "labspec_translator.mcp_server"]
    assert e["tool_timeout_sec"] == 120 and e["env"] == {"LS6T_CONFIRM": "request", "LS6T_OUT": "D:\\runs"}
    assert d["model"] == "gpt-5" and d["mcp_servers"]["context7"]["command"] == "npx" and d["profiles"]["x"]
    new2, _ = C.toml_merge(new, "ls6t-sim", py, {}, forced={"LS6T_MODE": "sim"})
    assert tomllib.loads(new2)["mcp_servers"]["ls6t-sim"]["env"] == {"LS6T_MODE": "sim"}
    gone, ok = C.toml_remove(new2, "ls6t", py)
    d = tomllib.loads(gone)
    assert ok and "ls6t" not in d["mcp_servers"] and "ls6t-sim" in d["mcp_servers"] and d["profiles"]["x"]
    assert C.toml_remove(gone, "ls6t-sim", "other.exe") == (gone, False)
    with pytest.raises(ValueError):
        C.toml_merge("mcp_servers = { ls6t = { command = 'x' } }\n", "ls6t", py, {})
    with pytest.raises(ValueError):
        C.toml_merge("[broken\n", "ls6t", py, {})


def test_connect_detect_and_disconnect(tmp_path, monkeypatch, capsys):
    from labspec_translator import cli
    from labspec_translator import clients as C
    monkeypatch.setenv("HOME", str(tmp_path / "u"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "u"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "u" / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(C.Path, "home", classmethod(lambda cls: tmp_path / "u"))
    monkeypatch.setattr(C.shutil, "which", lambda exe: None)
    assert not any(C.detect().values())
    (tmp_path / "u" / ".gemini").mkdir(parents=True)
    (tmp_path / "u" / ".gemini" / "settings.json").write_text('{"theme": "dark"}')
    found = C.detect()
    assert found["gemini"] and not found["codex"] and not found["claude"]
    assert cli.main(["connect", "auto", "--sim"]) == 0
    g = json.loads((tmp_path / "u" / ".gemini" / "settings.json").read_text())
    assert g["theme"] == "dark" and set(g["mcpServers"]) == {"ls6t", "ls6t-sim"}
    assert g["mcpServers"]["ls6t"]["env"]["LS6T_HOME"] == g["mcpServers"]["ls6t-sim"]["env"]["LS6T_HOME"]
    assert cli.main(["connect", "codex", "claude"]) == 0
    toml = C.tomllib.loads((tmp_path / "u" / ".codex" / "config.toml").read_text())
    assert toml["mcp_servers"]["ls6t"]["command"] == sys.executable
    # Cursor: ~/.cursor/mcp.json, found by its folder; the person's other servers kept
    (tmp_path / "u" / ".cursor").mkdir()
    (tmp_path / "u" / ".cursor" / "mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}')
    assert C.detect()["cursor"]
    assert cli.main(["connect", "cursor"]) == 0
    cu = json.loads((tmp_path / "u" / ".cursor" / "mcp.json").read_text())
    assert cu["mcpServers"]["other"] == {"command": "x"}
    e = cu["mcpServers"]["ls6t"]
    assert e["type"] == "stdio" and e["command"] == sys.executable and e["args"] == C.MODULE_ARGS
    assert e["env"]["LS6T_HOME"] == g["mcpServers"]["ls6t"]["env"]["LS6T_HOME"]          # one lock for all apps
    # Claude Code (~/.claude.json: its own state kept), GitHub Copilot (portable format, the person's tool list
    # kept), Windsurf (stable and Next), LM Studio
    u = tmp_path / "u"
    (u / ".claude.json").write_text('{"numStartups": 7, "projects": {"C:/x": {"mcpServers": {}}}}')
    (u / ".copilot").mkdir()
    (u / ".copilot" / "mcp-config.json").write_text('{"mcpServers": {"ls6t": {"command": "old", "tools": ["status"]}}}')
    (u / ".codeium" / "windsurf").mkdir(parents=True)
    (u / ".codeium" / "windsurf-next").mkdir()
    (u / ".lmstudio").mkdir()
    found = C.detect()
    assert all(found[c] for c in ("claudecode", "copilot", "windsurf", "lmstudio"))
    assert cli.main(["connect", "claude-code", "copilot", "windsurf", "LM-Studio"]) == 0      # aliases, any case
    cc = json.loads((u / ".claude.json").read_text())
    assert cc["numStartups"] == 7 and cc["projects"] == {"C:/x": {"mcpServers": {}}}
    assert cc["mcpServers"]["ls6t"]["type"] == "stdio" and cc["mcpServers"]["ls6t"]["command"] == sys.executable
    # 2026-11-22: the install warned every time; only a running Claude Code session warns (not the desktop app)
    assert C.is_claudecode_process("claude.exe", r"C:\Users\f\.local\bin\claude.exe", "")
    assert C.is_claudecode_process("node.exe", r"C:\nodejs\node.exe", r"node C:\npm\@anthropic-ai\claude-code\cli.js")
    assert not C.is_claudecode_process("claude.exe", r"C:\Program Files\WindowsApps\Claude_1_x64__p\app\claude.exe", "")
    assert not C.is_claudecode_process("claude.exe", r"C:\Users\f\AppData\Local\AnthropicClaude\app-1\claude.exe", "")
    assert not C.is_claudecode_process("claude.exe", "", "") and not C.is_claudecode_process("node.exe", "", "x.js")
    co = json.loads((u / ".copilot" / "mcp-config.json").read_text())["mcpServers"]["ls6t"]
    assert co["type"] == "stdio" and co["command"] == sys.executable and co["tools"] == ["status"]
    for d in ("windsurf", "windsurf-next"):
        ws = json.loads((u / ".codeium" / d / "mcp_config.json").read_text())["mcpServers"]["ls6t"]
        assert ws["command"] == sys.executable and "type" not in ws
    lm = json.loads((u / ".lmstudio" / "mcp.json").read_text())["mcpServers"]["ls6t"]
    assert lm["args"] == C.MODULE_ARGS and lm["env"]["LS6T_HOME"] == e["env"]["LS6T_HOME"]
    st = C.status()
    assert all(st[c]["files"] and any("ls6t" in v for v in st[c]["files"].values()) for c in C.CLIENTS)
    capsys.readouterr()
    assert cli.main(["connect", *C.CLIENTS, "--remove"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(sum((r["removed"] for r in out["files"]), [])) == ["ls6t"] * 9 + ["ls6t-sim"]
    assert json.loads((u / ".claude.json").read_text())["numStartups"] == 7
    assert set(json.loads((tmp_path / "u" / ".cursor" / "mcp.json").read_text())["mcpServers"]) == {"other"}
    assert json.loads((tmp_path / "u" / ".gemini" / "settings.json").read_text())["mcpServers"] == {}


def test_installer_files(tmp_path):
    import zipfile
    sys.path.insert(0, str(ROOT / "installer"))
    try:
        import build_installer as B
    finally:
        sys.path.pop(0)
    z = tmp_path / "py.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("python312._pth", "python312.zip\r\n.\r\n\r\n#import site\r\n")
        zf.writestr("python.exe", "x")
    assert B.stage_python(z, tmp_path / "py") == "312"
    assert "import site" in (tmp_path / "py" / "python312._pth").read_text().splitlines()
    assert B.version() == __import__("labspec_translator").__version__
    nsi = (ROOT / "installer" / "ls6t.nsi").read_text()
    for needle in ("labspec_translator.winsetup install", "labspec_translator.winsetup uninstall",
                   "RequestExecutionLevel user", "ls6t-shell.ps1", "INSTALL.html", "PROMPTS.html",
                   "MUI_PAGE_CUSTOMFUNCTION_SHOW FinishShow", "Function FinishLeave", "Function .onGUIEnd",
                   'StrCpy $InstallDone "1"', "${NSD_OnClick} $PromptsBox PromptsClick",
                   "MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenGuide", '"$WINDIR\\explorer.exe"'):
        assert needle in nsi, needle
    # the finish page's third option opens the prompts, which the build renders with the other guides
    assert "PROMPTS.md" in B.DOCS and (ROOT / "PROMPTS.md").is_file()
    assert nsi.index("MUI_PAGE_CUSTOMFUNCTION_SHOW FinishShow") < nsi.index("!insertmacro MUI_PAGE_FINISH")
    for f in ("ls6t.cmd", "ls6t-mcp.cmd", "ls6t-typelib.cmd", "ls6t-shell.ps1"):
        raw = (ROOT / "installer" / "bin" / f).read_bytes()
        assert all(b < 128 for b in raw), f
    for f, mod in (("ls6t.cmd", "cli"), ("ls6t-mcp.cmd", "mcp_server"), ("ls6t-typelib.cmd", "typelib")):
        raw = (ROOT / "installer" / "bin" / f).read_bytes()
        assert f"-m labspec_translator.{mod} %*".encode() in raw and b"\r\n" in raw
