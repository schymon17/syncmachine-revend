$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    py -3.11 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements-build.txt

.\.venv\Scripts\pyinstaller.exe `
    --noconfirm `
    --clean `
    --windowed `
    --name RevendSyncNew `
    --add-data "README.md;." `
    run_app.py

Write-Host "Gotowe: dist\RevendSyncNew\RevendSyncNew.exe"
