# ls6t-shell.ps1 - a PowerShell window with the ls6t commands (Start menu: "ls6t PowerShell"; installed by ls6t-setup)
$bin = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:Path = "$bin;" + $env:Path
Set-Location $HOME
$host.UI.RawUI.WindowTitle = "ls6t"
Write-Host "ls6t (labspec-translator)" -ForegroundColor Cyan
& "$bin\ls6t.cmd" --version
Write-Host ""
Write-Host "  ls6t status                     versions, settings, whether the LabSpec bridge runs"
Write-Host "  ls6t instrument status          the instrument now (the bridge must run in LabSpec)"
Write-Host "  ls6t profile import <dump>      after running ls6t_discover.vbs in LabSpec"
Write-Host "  ls6t bench                      the benchmarks on the simulator (no instrument needed)"
Write-Host "  ls6t --help                     every command"
Write-Host ""
Write-Host "LabSpec scripts: $HOME\ls6t\vbs   Guides: $bin\..\docs"
Write-Host ""
