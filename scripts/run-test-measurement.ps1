[CmdletBinding()]
param(
    [string] $SampleName = 'validation_sample',
    [string] $SampleId = "validation_$(Get-Date -Format 'yyyyMMdd_HHmmss')",
    [string] $Profile,
    [string] $WavelengthsNm,
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
if (-not [string]::IsNullOrWhiteSpace($Profile)) {
    $measurementArguments += @('--profile', $Profile)
}
if (-not [string]::IsNullOrWhiteSpace($WavelengthsNm)) {
    $measurementArguments += '--wavelengths'
    $tokens = [System.Text.RegularExpressions.Regex]::Split(
        $WavelengthsNm.Trim(),
        '[,;\s]+'
    )
    foreach ($token in $tokens) {
        $wavelength = [double]::Parse(
            $token,
            [System.Globalization.CultureInfo]::InvariantCulture
        )
        $measurementArguments += $wavelength.ToString(
            'G17',
            [System.Globalization.CultureInfo]::InvariantCulture
        )
    }
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
