# ls6t_diagnose.ps1 - why does an LLM app (Claude) not see ls6t's tools? Read-only checks; writes a report.
#
# Run on the instrument PC, as the Windows user who runs Claude (Claude may be open or closed):
#   powershell -ExecutionPolicy Bypass -File .\ls6t_diagnose.ps1
# or right-click the file > Run with PowerShell. The report goes to ls6t_diagnose.txt on the Desktop (the path is
# printed at the end): send that file back.
#
# It changes nothing. It reads Claude's configuration files and logs, and starts ls6t's MCP server for a few
# seconds to see whether it answers (that does not touch the instrument). Other servers' settings are not copied
# into the report: only their names, and the ls6t entries in full.

$ErrorActionPreference = "Continue"
$desk = [Environment]::GetFolderPath("Desktop")
if (-not $desk -or -not (Test-Path $desk)) { $desk = $env:USERPROFILE }
$report = Join-Path $desk "ls6t_diagnose.txt"
$lines = New-Object System.Collections.Generic.List[string]
function Say($text) { $lines.Add([string]$text); Write-Host $text }
function Section($title) { Say ""; Say ("=" * 100); Say "== $title"; Say ("=" * 100) }

Section "Computer"
Say ("date:     " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Say ("user:     " + $env:USERNAME + "   profile: " + $env:USERPROFILE)
Say ("windows:  " + [Environment]::OSVersion.VersionString)
Say ("LOCALAPPDATA: " + $env:LOCALAPPDATA + "   APPDATA: " + $env:APPDATA)
if ($env:CLAUDE_USER_DATA_DIR) { Say ("CLAUDE_USER_DATA_DIR: " + $env:CLAUDE_USER_DATA_DIR) }

# ---------------------------------------------------------------------------------------------------------- Claude
Section "Claude app: running processes and the folder they read (--user-data-dir)"
$dataDirs = @()
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction Stop
    if (-not $procs) { Say "Claude is not running (start it and run this again to see which folder it reads)" }
    foreach ($p in $procs) {
        $cl = [string]$p.CommandLine
        if ($cl -match '--user-data-dir=("([^"]+)"|(\S+))') {
            $d = if ($matches[2]) { $matches[2] } else { $matches[3] }
            if ($dataDirs -notcontains $d) { $dataDirs += $d }
        }
        Say ("pid " + $p.ProcessId + ": " + $p.ExecutablePath)
    }
    foreach ($d in $dataDirs) { Say ("READS: " + $d) }
} catch { Say ("cannot list processes: " + $_) }

Section "Claude app: installed packages"
Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "Claude_*" -or $_.Name -like "AnthropicPBC.Claude_*" } |
    ForEach-Object { Say ("store package: " + $_.FullName) }
try {
    Get-AppxPackage -ErrorAction Stop | Where-Object { $_.Name -like "*Claude*" } |
        ForEach-Object { Say ("appx: " + $_.Name + " " + $_.Version + " " + $_.PackageFamilyName) }
} catch { Say "Get-AppxPackage not available" }
if (Test-Path "$env:LOCALAPPDATA\AnthropicClaude") { Say "classic install: $env:LOCALAPPDATA\AnthropicClaude" }

# ---------------------------------------------------------------------------------------------------------- configs
Section "Claude configuration files (claude_desktop_config.json)"
$cands = @()
foreach ($d in $dataDirs) { $cands += (Join-Path $d "claude_desktop_config.json") }
if ($env:CLAUDE_USER_DATA_DIR) { $cands += (Join-Path $env:CLAUDE_USER_DATA_DIR "claude_desktop_config.json") }
Get-ChildItem "$env:LOCALAPPDATA\Claude-Data" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { $cands += (Join-Path $_.FullName "claude_desktop_config.json") }
Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*Claude_*" } |
    ForEach-Object { $cands += (Join-Path $_.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json") }
$cands += "$env:LOCALAPPDATA\Claude-3p\claude_desktop_config.json"
$cands += "$env:APPDATA\Claude\claude_desktop_config.json"
$cands = $cands | Select-Object -Unique
$ls6tEntries = @()
foreach ($f in $cands) {
    Say ""
    Say ("FILE " + $f)
    if (-not (Test-Path $f)) { Say "   (does not exist)"; continue }
    $item = Get-Item $f
    Say ("   size " + $item.Length + " bytes, last written " + $item.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss"))
    $dir = Split-Path $f
    Get-ChildItem $dir -Force -Filter ".user-data-left*" -ErrorAction SilentlyContinue |
        ForEach-Object { Say ("   marker " + $_.Name + "  (Claude moved away from this folder: it no longer reads this file)") }
    $bytes = [System.IO.File]::ReadAllBytes($f)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        Say "   NOTE: the file starts with a UTF-8 BOM (some editors add it); if Claude ignores the file, save it without BOM"
    }
    $raw = [System.IO.File]::ReadAllText($f)
    try {
        $cfg = $raw | ConvertFrom-Json -ErrorAction Stop
        Say "   JSON: valid"
    } catch {
        Say ("   JSON: INVALID - " + $_.Exception.Message)
        Say "   ---- the file, as text ----"
        foreach ($l in ($raw -split "`n")) { Say ("   | " + $l.TrimEnd()) }
        continue
    }
    if (-not $cfg.mcpServers) { Say "   mcpServers: none"; continue }
    $names = @($cfg.mcpServers.PSObject.Properties | ForEach-Object { $_.Name })
    Say ("   mcpServers: " + ($names -join ", "))
    foreach ($n in @("ls6t", "ls6t-sim")) {
        $e = $cfg.mcpServers.$n
        if ($e) {
            Say ("   ---- entry '" + $n + "' ----")
            foreach ($l in (($e | ConvertTo-Json -Depth 6) -split "`n")) { Say ("   | " + $l.TrimEnd()) }
            if ($e.command -and -not (Test-Path ([string]$e.command))) { Say ("   PROBLEM: command not found: " + $e.command) }
            $ls6tEntries += ,@($f, $e)
        }
    }
}
if (-not $ls6tEntries) { Say ""; Say "PROBLEM: no 'ls6t' entry in any of these files" }

# ---------------------------------------------------------------------------------------------------------- ls6t
Section "ls6t installation"
$py = "$env:LOCALAPPDATA\Programs\ls6t\python\python.exe"
foreach ($pair in $ls6tEntries) { $c = [string]$pair[1].command; if ($c -and (Test-Path $c)) { $py = $c; break } }
Say ("python: " + $py + "   exists: " + (Test-Path $py))
$reg = Get-ItemProperty "HKCU:\Software\ls6t" -ErrorAction SilentlyContinue
if ($reg) { Say ("installed: " + $reg.InstallDir + "   version " + $reg.Version) }
$log = Join-Path (Split-Path (Split-Path $py)) "install.log"
if (Test-Path $log) { Say "---- install.log (last 40 lines) ----"; Get-Content $log -Tail 40 | ForEach-Object { Say ("| " + $_) } }

function RunPy($argsList, $stdinText, $seconds) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $py
    $psi.Arguments = $argsList
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    foreach ($pair in $ls6tEntries) {
        if ($pair[1].env) { $pair[1].env.PSObject.Properties | ForEach-Object { $psi.EnvironmentVariables[$_.Name] = [string]$_.Value } }
        break
    }
    $p = [System.Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if ($stdinText) { $p.StandardInput.Write($stdinText) }
    $p.StandardInput.Close()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    if (-not $p.WaitForExit($seconds * 1000)) { $p.Kill(); Say ("   (still running after " + $seconds + " s: stopped)") }
    Say ("   exit code " + $p.ExitCode + " after " + [math]::Round($sw.Elapsed.TotalSeconds, 1) + " s")
    $o = $outTask.Result; $e = $errTask.Result
    if ($o) { Say "   ---- output ----"; foreach ($l in ($o -split "`n")) { if ($l.Trim()) { Say ("   | " + $l.TrimEnd().Substring(0, [Math]::Min(400, $l.TrimEnd().Length))) } } }
    if ($e) { Say "   ---- errors ----"; foreach ($l in ($e -split "`n" | Select-Object -Last 40)) { Say ("   | " + $l.TrimEnd()) } }
}

if (Test-Path $py) {
    Say ""
    Say "ls6t --version:"
    RunPy "-m labspec_translator.cli --version" $null 60
    Say ""
    Say "The MCP server, as Claude starts it (initialize, then tools/list):"
    $msgs = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ls6t-diagnose","version":"1"}}}' + "`n" +
            '{"jsonrpc":"2.0","method":"notifications/initialized"}' + "`n" +
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' + "`n"
    RunPy "-m labspec_translator.mcp_server" $msgs 90
    Say "   (a line with serverInfo / labspec-translator means the server works; tool names follow in the next line)"
}

# ---------------------------------------------------------------------------------------------------------- logs
Section "Claude's logs (MCP)"
$logDirs = @("$env:LOCALAPPDATA\Claude\logs", "$env:APPDATA\Claude\logs")
foreach ($d in $dataDirs) { $logDirs += (Join-Path $d "logs") }
Get-ChildItem "$env:LOCALAPPDATA\Claude-Data" -Directory -ErrorAction SilentlyContinue | ForEach-Object { $logDirs += (Join-Path $_.FullName "logs") }
Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*Claude_*" } |
    ForEach-Object { $logDirs += (Join-Path $_.FullName "LocalCache\Roaming\Claude\logs"); $logDirs += (Join-Path $_.FullName "LocalCache\Local\Claude\logs") }
foreach ($d in ($logDirs | Select-Object -Unique)) {
    if (-not (Test-Path $d)) { continue }
    Say ""
    Say ("LOGS " + $d)
    Get-ChildItem $d -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 15 |
        ForEach-Object { Say ("   " + $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm") + "  " + $_.Length + "  " + $_.Name) }
    foreach ($name in @("mcp-server-ls6t.log", "mcp.log")) {
        $lf = Join-Path $d $name
        if (Test-Path $lf) { Say ("---- " + $name + " (last 60 lines) ----"); Get-Content $lf -Tail 60 | ForEach-Object { Say ("| " + $_) } }
    }
    $hits = Get-ChildItem $d -File -Filter "*.log" -ErrorAction SilentlyContinue |
        Select-String -Pattern "ls6t|mcpServers|claude_desktop_config|policy|disabled" -ErrorAction SilentlyContinue | Select-Object -Last 40
    if ($hits) { Say "---- lines about ls6t / the configuration / policies (last 40) ----"; $hits | ForEach-Object { Say ("| " + $_.Filename + ": " + $_.Line.Trim()) } }
}

Section "Done"
$lines | Out-File -FilePath $report -Encoding utf8
Write-Host ""
Write-Host "Report written to: $report" -ForegroundColor Green
Write-Host "Send that file back. Press Enter to close."
[void][Console]::ReadLine()
