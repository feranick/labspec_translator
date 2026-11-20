# ls6t_fix_claude.ps1 - add ls6t's MCP server to the configuration file the Claude desktop app really reads.
#
# For the Microsoft Store builds of Claude that keep their data in %LOCALAPPDATA%\Claude-Data\claude_<id>\ (ls6t
# installers before 2026.11.20.8 wrote to older locations only). Run on the instrument PC, as the Windows user who
# runs Claude:
#   powershell -ExecutionPolicy Bypass -File .\ls6t_fix_claude.ps1
#
# 1. waits until Claude is closed (it rewrites its configuration while running, which would undo the change);
# 2. adds the "ls6t" entry to claude_desktop_config.json in each Claude-Data\claude_* folder, with ls6t's own code
#    (the file is backed up first; everything else in it is kept);
# 3. checks the entry is there. Then start Claude again.

$ErrorActionPreference = "Stop"
$py = "$env:LOCALAPPDATA\Programs\ls6t\python\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "ls6t's Python was not found at $py : install ls6t first." -ForegroundColor Red
    [void](Read-Host "Press Enter to close"); exit 1
}

# 1. Claude must be closed
while (Get-Process -Name claude -ErrorAction SilentlyContinue) {
    Write-Host ""
    Write-Host "Claude is running. Quit it completely: right-click its icon in the taskbar's notification area > Quit." -ForegroundColor Yellow
    $a = Read-Host "Press Enter when it is closed, or type F and Enter to force-close it now"
    if ($a -eq "F" -or $a -eq "f") {
        Get-Process -Name claude -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 3
    }
}
Write-Host "Claude is closed." -ForegroundColor Green

# 2. the folders Claude reads
$dirs = @(Get-ChildItem "$env:LOCALAPPDATA\Claude-Data" -Directory -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -like "claude_*" -or $_.Name -like "anthropicpbc*" } | ForEach-Object { $_.FullName })
if ($dirs.Count -eq 0) {
    Write-Host "No $env:LOCALAPPDATA\Claude-Data\claude_* folder: this Claude is not a Store build that moved there." -ForegroundColor Red
    Write-Host "Send ls6t_diagnose.txt instead."
    [void](Read-Host "Press Enter to close"); exit 1
}

$code = @'
import sys
from pathlib import Path
from labspec_translator import clients as C

import re

def real(v):  # not a placeholder copied from the docs, like C:\Users\<you>\...
    return not re.search(r"<[^<>]*>", str(v))

python = sys.argv[1]
env = {**C.default_env(), **{k: v for k, v in C.existing_env().items() if real(v)}}
for d in sys.argv[2:]:
    f = Path(d) / "claude_desktop_config.json"
    try:
        cfg = C.read_json(f)
    except ValueError as exc:
        print(f"{f}: not valid JSON ({exc}): left unchanged")
        continue
    old = (cfg.get("mcpServers") or {}).get("ls6t")
    if isinstance(old, dict) and isinstance(old.get("env"), dict):
        old["env"] = {k: v for k, v in old["env"].items() if real(v)}   # drop placeholders: ours fill in
    cfg, note = C.merge_server(cfg, "ls6t", python, env)
    backup = C.write_json(f, cfg)
    print(f"{f}: {note}" + (f" (backup: {backup.name})" if backup else ""))
    e = C.read_json(f)["mcpServers"]["ls6t"]
    print("  command:", e["command"])
    print("  args:   ", " ".join(e["args"]))
    print("  env:    ", ", ".join(f"{k}={v}" for k, v in e["env"].items()))
'@
$tmp = Join-Path $env:TEMP "ls6t_fix_claude.py"
[System.IO.File]::WriteAllText($tmp, $code, (New-Object System.Text.UTF8Encoding $false))
Write-Host ""
& $py $tmp $py @dirs
$rc = $LASTEXITCODE
Remove-Item $tmp -ErrorAction SilentlyContinue
Write-Host ""
if ($rc -eq 0) {
    Write-Host "Done. Start Claude, open a NEW chat: ls6t's tools are under the + menu (Connectors / tools)." -ForegroundColor Green
    Write-Host "In Claude, Settings > Developer lists 'ls6t' as running."
} else {
    Write-Host "Something failed (exit code $rc): send the text above." -ForegroundColor Red
}
[void](Read-Host "Press Enter to close")
