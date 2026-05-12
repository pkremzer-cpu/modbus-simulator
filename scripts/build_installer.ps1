<#
.SYNOPSIS
    Build the Inno Setup .exe installer for Kremzer Péter ModbusTCP.

.DESCRIPTION
    Requires Inno Setup 6 (winget install JRSoftware.InnoSetup) and a
    previously produced dist\ModbusSimulator\ folder from build_exe.ps1.
#>

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $repoRoot

if (-not (Test-Path "$repoRoot\dist\ModbusSimulator\ModbusSimulator.exe")) {
    throw "dist\ModbusSimulator\ not found. Run scripts\build_exe.ps1 first."
}

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    throw "Inno Setup not found at $iscc. Install it: winget install JRSoftware.InnoSetup"
}

Write-Host "[build_installer] compiling Inno Setup script..." -ForegroundColor Cyan
& $iscc "scripts\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }

$setupExe = Get-ChildItem -Path "$repoRoot\dist" -Filter "KremzerPeterModbusTCP-Setup-*.exe" | Select-Object -First 1
if ($null -eq $setupExe) {
    throw "expected setup .exe was not produced"
}
$size = $setupExe.Length / 1MB
Write-Host "[build_installer] OK: $($setupExe.FullName) ($([Math]::Round($size, 1)) MB)" -ForegroundColor Green
