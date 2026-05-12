<#
.SYNOPSIS
    Build the Windows .exe bundle for Kremzer Péter ModbusTCP.

.DESCRIPTION
    Cleans dist/, runs PyInstaller against the spec file, and prints the
    output path on success. Requires `uv` to be on PATH and `pyinstaller`
    available either as a top-level dev dep (preferred) or installed
    on-the-fly via `uv pip install pyinstaller`.

.EXAMPLE
    pwsh scripts/build_exe.ps1
#>

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $repoRoot

Write-Host "[build_exe] cleaning previous build artifacts..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "$repoRoot\build" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$repoRoot\dist\ModbusSimulator" -ErrorAction SilentlyContinue
Remove-Item -Force "$repoRoot\dist\ModbusSimulator.exe" -ErrorAction SilentlyContinue

Write-Host "[build_exe] syncing dependencies (incl. dev for PyInstaller)..." -ForegroundColor Cyan
uv sync --extra dev
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

Write-Host "[build_exe] running PyInstaller..." -ForegroundColor Cyan
uv run pyinstaller --noconfirm --clean "scripts\modbus_simulator.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = "$repoRoot\dist\ModbusSimulator\ModbusSimulator.exe"
if (-not (Test-Path $exe)) {
    throw "expected $exe was not produced"
}
$size = (Get-Item $exe).Length / 1MB
Write-Host "[build_exe] OK: $exe ($([Math]::Round($size, 1)) MB)" -ForegroundColor Green
Write-Host "[build_exe] bundle directory: $repoRoot\dist\ModbusSimulator\" -ForegroundColor Green
Write-Host "[build_exe] next: run scripts\build_installer.ps1 for an Inno Setup installer." -ForegroundColor Yellow
