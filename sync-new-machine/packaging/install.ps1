# Called by the panel's one-line installer (https://panel.revend.pl/agent/install.ps1)
# after it has redeemed the installation code and verified this package.
param(
    [Parameter(Mandatory = $true)][string]$EnrollmentFile,
    # Czysci transakcje, worki i kupony na maszynie, baze opakowan pobiera od nowa.
    [switch]$ResetData
)

$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'revend-sync\revend-sync.exe'
$installArgs = @('install', '--enrollment-file', $EnrollmentFile)
if ($ResetData) { $installArgs += '--reset-data' }
& $exe @installArgs
if ($LASTEXITCODE -ne 0) {
    throw "Instalacja agenta nie powiodla sie (kod $LASTEXITCODE)."
}
