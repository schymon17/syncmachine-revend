# Builds the agent release zip on Windows: revend-sync-<version>.zip + .sha256
# Requires Python 3.8 (py -3.8). Usage: powershell -File packaging\build-agent.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# GitHub Actions (setup-python) sets pythonLocation; locally use the py launcher.
if ($env:pythonLocation) { $py = @((Join-Path $env:pythonLocation 'python.exe')) } else { $py = @('py', '-3.8') }
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)

$version = (& $pyExe @pyArgs -c "import revend_sync; print(revend_sync.__version__)").Trim()
$venv = Join-Path $root '.build-venv'
if (-not (Test-Path $venv)) { & $pyExe @pyArgs -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$venv\Scripts\python.exe" -m pip install --quiet -r packaging\requirements-agent.txt pyinstaller==5.13.2

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean --console --name revend-sync `
    --collect-data certifi --hidden-import revend_sync.supervisor packaging\agent_entry.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$reported = (& dist\revend-sync\revend-sync.exe --version).Trim()
if ($reported -ne $version) { throw "Built exe reports '$reported', expected '$version'" }

$package = Join-Path $root 'dist\package'
New-Item -ItemType Directory -Path $package | Out-Null
Move-Item dist\revend-sync (Join-Path $package 'revend-sync')
Copy-Item packaging\install.ps1, packaging\install.cmd, packaging\README.txt $package

$zip = Join-Path $root "dist\revend-sync-$version.zip"
Compress-Archive -Path (Join-Path $package '*') -DestinationPath $zip -Force
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLowerInvariant()
Set-Content -Path "$zip.sha256" -Value $hash -Encoding ascii
Write-Host "Built $zip"
Write-Host "SHA-256 $hash"
