# Builds the Windows installer: build\release\LiveType-Setup-<version>.exe
#   1. engine  -> build\engine\livetype-engine\   (PyInstaller, self-contained Python)
#   2. UI      -> Electron + engine packed by electron-builder (NSIS installer)
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Python <python.exe>] [-Work <dir>]
# -Work: where the heavy build files go (default: <repo>\build). Speech models are NOT bundled:
#        the app downloads them on first start (~455 MB, then works offline).
param(
    [string]$Python = "python",
    [string]$Work = "",
    [switch]$SkipEngine
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
if (-not $Work) { $Work = Join-Path $Repo "build" }
New-Item -ItemType Directory -Force $Work | Out-Null

Write-Host "== 1/2 engine (PyInstaller)"
if (-not $SkipEngine) {
& $Python -m PyInstaller --noconfirm --clean --console --name livetype-engine `
    --distpath (Join-Path $Work "engine") --workpath (Join-Path $Work "pyi-work") --specpath (Join-Path $Work "pyi-spec") `
    --paths $Repo --icon (Join-Path $Repo "assets\livetype.ico") `
    --collect-all onnx_asr --collect-all sounddevice --collect-submodules livetype `
    --hidden-import comtypes.stream --exclude-module pytest `
    --exclude-module tkinter --exclude-module _tkinter --exclude-module PIL --exclude-module pystray `
    --exclude-module huggingface_hub --exclude-module hf_xet `
    (Join-Path $Repo "packaging\engine_entry.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
}
# license texts of everything the installer ships -> resources\licenses
& $Python (Join-Path $Repo "packaging\collect_licenses.py") (Join-Path $Work "licenses")
if ($LASTEXITCODE -ne 0) { throw "collecting licenses failed" }

Write-Host "== 2/2 installer (electron-builder)"
$Stage = Join-Path $Work "ui"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
Copy-Item -Recurse (Join-Path $Repo "ui") $Stage
# point the packed engine and the output at the work folder
$pkg = Get-Content -Raw (Join-Path $Stage "package.json") | ConvertFrom-Json
$pkg.build.extraResources[0].from = (Join-Path $Work "engine\livetype-engine")
$pkg.build.extraResources[1].from = (Join-Path $Work "licenses")
$pkg.build.directories.output = (Join-Path $Work "release")
[IO.File]::WriteAllText((Join-Path $Stage "package.json"), ($pkg | ConvertTo-Json -Depth 10),
    (New-Object Text.UTF8Encoding $false))  # no BOM: npm/electron-builder cannot parse it
Push-Location $Stage
try {
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    npx electron-builder --win nsis --publish never
    if ($LASTEXITCODE -ne 0) { throw "electron-builder failed" }
} finally {
    Pop-Location
}
Get-ChildItem (Join-Path $Work "release") -Filter "*.exe" | ForEach-Object { "{0}  {1:N1} MB" -f $_.FullName, ($_.Length / 1MB) }
