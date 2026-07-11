[CmdletBinding()]
param(
    [string] $SampleName = 'validation_sample',
    [string] $SampleId = "validation_$(Get-Date -Format 'yyyyMMdd_HHmmss')",
    [switch] $Connect,
    [switch] $AutoCorrection,
    [switch] $Execute
)

$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'uvvis.ps1'

$measurementArguments = @(
    'spectrum',
    '--sample-name', $SampleName,
    '--sample-id', $SampleId,
    '--measurement-mode', '2',
    '--no-discharge',
    '--no-disconnect'
)
if ($Connect) {
    $measurementArguments += '--connect'
} else {
    $measurementArguments += '--no-connect'
}
if ($AutoCorrection) {
    $measurementArguments += @('--correction', 'auto')
} else {
    $measurementArguments += @('--correction', 'none')
}

if (-not $Execute) {
    & $launcher @measurementArguments
    exit $LASTEXITCODE
}

& $launcher doctor --write-check
if ($LASTEXITCODE -ne 0) {
    throw 'Control-PC diagnostics failed. Measurement was not started.'
}

$measurementArguments += '--execute'
& $launcher @measurementArguments
exit $LASTEXITCODE
