[CmdletBinding()]
param(
    [string] $CommandDir = 'D:\UVVis-Automation\control',
    [string] $ExportDir = 'D:\UVVis-Automation\export',
    [string] $DataDir = 'D:\UVVis-Automation\data',
    [string] $MethodFile = 'D:\UVVis-Automation\methods\growth_scan_300_900.vspm',
    [string] $TemplateDir = 'D:\UVVis-Automation\templates',
    [string] $GeneratedMethodDir = 'D:\UVVis-Automation\methods\generated',
    [string] $AuditDir = 'D:\UVVis-Automation\logs',
    [double] $ScanStartNm = 300.0,
    [double] $ScanStopNm = 900.0,
    [double] $ScanStepNm = 1.0,
    [double] $ScanSpeedNmPerMinute = 0.0,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$venvDir = Join-Path $repoRoot '.venv'

foreach ($value in @($ScanStartNm, $ScanStopNm, $ScanStepNm)) {
    if ([double]::IsNaN($value) -or [double]::IsInfinity($value)) {
        throw 'Scan wavelength values must be finite numbers.'
    }
}
if ($ScanStartNm -eq $ScanStopNm) {
    throw 'ScanStartNm and ScanStopNm must differ.'
}
if ($ScanStartNm -le 0 -or $ScanStopNm -le 0) {
    throw 'ScanStartNm and ScanStopNm must be positive.'
}
if ($ScanStepNm -le 0) {
    throw 'ScanStepNm must be greater than zero.'
}
if ([double]::IsNaN($ScanSpeedNmPerMinute) -or
    [double]::IsInfinity($ScanSpeedNmPerMinute) -or
    $ScanSpeedNmPerMinute -lt 0) {
    throw 'ScanSpeedNmPerMinute must be zero (not registered) or a finite positive number.'
}
$intervalCount = [math]::Abs($ScanStopNm - $ScanStartNm) / $ScanStepNm
if ([math]::Abs($intervalCount - [math]::Round($intervalCount)) -gt 0.000001) {
    throw 'The scan range must be evenly divisible by ScanStepNm.'
}

function Test-Python311 {
    param([string] $Executable, [string[]] $PrefixArguments = @())
    & $Executable @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    return ($LASTEXITCODE -eq 0)
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $created = $false
    $pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher -and (Test-Python311 $pyLauncher.Source @('-3.11'))) {
        & $pyLauncher.Source -3.11 -m venv $venvDir
        $created = ($LASTEXITCODE -eq 0)
    }
    if (-not $created) {
        $systemPython = Get-Command 'python.exe' -ErrorAction SilentlyContinue
        if ($null -eq $systemPython -or -not (Test-Python311 $systemPython.Source)) {
            throw 'Python 3.11 or newer was not found. Install Python, then rerun this script.'
        }
        & $systemPython.Source -m venv $venvDir
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to create the local Python environment.'
        }
    }
}

& $venvPython -c "import sys, tomllib; print(sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    throw 'The local Python environment failed its self-check.'
}

foreach ($directory in @(
    $CommandDir,
    $ExportDir,
    $DataDir,
    $AuditDir,
    $TemplateDir,
    $GeneratedMethodDir,
    (Split-Path -Parent $MethodFile)
)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

function ConvertTo-TomlPath {
    param([string] $Value)
    return $Value.Replace('\', '\\').Replace('"', '\"')
}

function ConvertTo-InvariantNumber {
    param([double] $Value)
    return $Value.ToString('G17', [System.Globalization.CultureInfo]::InvariantCulture)
}

$configPath = Join-Path $repoRoot 'control-pc.toml'
if ((Test-Path -LiteralPath $configPath) -and -not $Force) {
    Write-Host "Preserving existing configuration: $configPath"
} else {
    $scanSpeedLine = ''
    if ($ScanSpeedNmPerMinute -gt 0) {
        $scanSpeedLine = "scan_speed_nm_per_min = $(ConvertTo-InvariantNumber $ScanSpeedNmPerMinute)"
    }
    $config = @"
[labsolutions]
command_dir = "$(ConvertTo-TomlPath $CommandDir)"
mode = "spectrum"
timeout_seconds = 600.0
poll_interval_seconds = 0.2
lock_timeout_seconds = 5.0
encoding = "utf-8"

[export]
directory = "$(ConvertTo-TomlPath $ExportDir)"
pattern = "{sample_id}*.csv"
timeout_seconds = 120.0
stable_seconds = 2.0

[spectrum]
method_file = "$(ConvertTo-TomlPath $MethodFile)"
data_dir = "$(ConvertTo-TomlPath $DataDir)"
measurement_mode = 2
connect_before_run = false
disconnect_after_run = false
correction = "none"
discharge_after_measurement = false
allow_unicode_identifiers = false

[method_generation]
output_directory = "$(ConvertTo-TomlPath $GeneratedMethodDir)"

[method_templates.spectrum_absorbance]
mode = "spectrum"
signal_type = "absorbance"
method_file = "$(ConvertTo-TomlPath (Join-Path $TemplateDir 'spectrum_absorbance.vspm'))"

[method_templates.photometric_absorbance]
mode = "photometric"
signal_type = "absorbance"
method_file = "$(ConvertTo-TomlPath (Join-Path $TemplateDir 'photometric_absorbance.vphm'))"

[method_templates.quantitation_absorbance]
mode = "quantitation"
signal_type = "absorbance"
method_file = "$(ConvertTo-TomlPath (Join-Path $TemplateDir 'quantitation_absorbance.vqum'))"

[method_templates.time_course_absorbance]
mode = "time_course"
signal_type = "absorbance"
method_file = "$(ConvertTo-TomlPath (Join-Path $TemplateDir 'time_course_absorbance.vtmm'))"

[scan_profiles.default]
method_file = "$(ConvertTo-TomlPath $MethodFile)"
start_nm = $(ConvertTo-InvariantNumber $ScanStartNm)
stop_nm = $(ConvertTo-InvariantNumber $ScanStopNm)
step_nm = $(ConvertTo-InvariantNumber $ScanStepNm)
$scanSpeedLine

[audit]
directory = "$(ConvertTo-TomlPath $AuditDir)"
"@
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($configPath, $config, $utf8NoBom)
    Write-Host "Created configuration: $configPath"
}

Write-Host ''
Write-Host 'Setup complete. Next commands:'
Write-Host '  powershell -ExecutionPolicy Bypass -File .\scripts\test-simulator.ps1'
Write-Host '  powershell -ExecutionPolicy Bypass -File .\scripts\test-live.ps1 -Ping'
Write-Host ''
Write-Host "Before the live test, place the real .vspm method at: $MethodFile"
Write-Host "Configure LabSolutions Automatic Control to watch: $CommandDir"
Write-Host "Configure LabSolutions automatic export to write: $ExportDir"
Write-Host "Save the four operator-verified base methods under: $TemplateDir"
Write-Host "Save parameter-specific method copies under: $GeneratedMethodDir"
Write-Host "Verify the saved method range is: $ScanStartNm to $ScanStopNm nm, interval $ScanStepNm nm"
if ($ScanSpeedNmPerMinute -gt 0) {
    Write-Host "Verify the saved method scan speed is: $ScanSpeedNmPerMinute nm/min"
} else {
    Write-Host 'Scan speed metadata was not registered; add it only after checking the .vspm method.'
}
