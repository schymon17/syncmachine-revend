# Called by the panel's one-line installer (https://panel.revend.pl/agent/install.ps1)
# after it has redeemed the installation code and verified this package.
param(
    [Parameter(Mandatory = $true)][string]$EnrollmentFile
)

$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'revend-sync\revend-sync.exe'
& $exe install --enrollment-file $EnrollmentFile
if ($LASTEXITCODE -ne 0) {
    throw "Instalacja agenta nie powiodla sie (kod $LASTEXITCODE)."
}
