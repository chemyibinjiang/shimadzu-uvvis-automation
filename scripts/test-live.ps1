[CmdletBinding()]
param(
    [switch] $Ping
)

$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'uvvis.ps1'

& $launcher doctor --write-check
if ($LASTEXITCODE -ne 0) {
    throw 'Control-PC diagnostics failed. No LabSolutions command was sent.'
}

if (-not $Ping) {
    Write-Host ''
    Write-Host 'Filesystem diagnostics passed. No LabSolutions command was sent.'
    Write-Host 'Repeat with -Ping after LabSolutions shows Automatic Control - Waiting.'
    exit 0
}

& $launcher --timeout 15 ping
if ($LASTEXITCODE -ne 0) {
    throw 'LabSolutions Hello test failed.'
}

Write-Host ''
Write-Host 'LIVE HELLO TEST PASSED. No measurement command was sent.'
