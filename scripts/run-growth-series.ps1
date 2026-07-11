[CmdletBinding()]
param(
    [string] $SampleName = 'growth_series',
    [string] $SeriesId = "growth_$(Get-Date -Format 'yyyyMMdd_HHmmss')",
    [string] $Profile = 'default',
    [int] $Count = 10,
    [double] $IntervalSeconds = 60.0,
    [double] $OverrunToleranceSeconds = 1.0,
    [string] $WavelengthsNm,
    [switch] $Connect,
    [switch] $AutoCorrection,
    [switch] $NoWaitExport,
    [switch] $Execute
)

$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'uvvis.ps1'
$culture = [System.Globalization.CultureInfo]::InvariantCulture

$seriesArguments = @(
    'series',
    '--sample-name', $SampleName,
    '--series-id', $SeriesId,
    '--count', $Count.ToString($culture),
    '--interval-seconds', $IntervalSeconds.ToString('G17', $culture),
    '--overrun-tolerance-seconds', $OverrunToleranceSeconds.ToString('G17', $culture),
    '--measurement-mode', '2',
    '--no-discharge',
    '--no-disconnect'
)

if ($Connect) {
    $seriesArguments += '--connect'
} else {
    $seriesArguments += '--no-connect'
}
if ($AutoCorrection) {
    $seriesArguments += @('--correction', 'auto')
} else {
    $seriesArguments += @('--correction', 'none')
}
if (-not [string]::IsNullOrWhiteSpace($Profile)) {
    $seriesArguments += @('--profile', $Profile)
}
if ($NoWaitExport) {
    $seriesArguments += '--no-wait-export'
}
if (-not [string]::IsNullOrWhiteSpace($WavelengthsNm)) {
    $seriesArguments += '--wavelengths'
    $tokens = [System.Text.RegularExpressions.Regex]::Split(
        $WavelengthsNm.Trim(),
        '[,;\s]+'
    )
    foreach ($token in $tokens) {
        $wavelength = [double]::Parse($token, $culture)
        $seriesArguments += $wavelength.ToString('G17', $culture)
    }
}

if (-not $Execute) {
    & $launcher @seriesArguments
    exit $LASTEXITCODE
}

& $launcher doctor --write-check
if ($LASTEXITCODE -ne 0) {
    throw 'Control-PC diagnostics failed. The Spectrum series was not started.'
}

$seriesArguments += '--execute'
& $launcher @seriesArguments
exit $LASTEXITCODE
