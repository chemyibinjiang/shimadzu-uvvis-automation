[CmdletBinding()]
param(
    [string] $CommandDir = 'C:\UVVisControl',
    [string] $ExportDir = 'C:\UVVis-Data\Export',
    [string] $DataDir = 'C:\UVVis-Data\Data',
    [string] $MethodFile = 'C:\UVVis-Data\Parameter\growth_scan_300_900.vspm',
    [string] $AuditDir = 'C:\UVVis-Automation\Logs',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$venvDir = Join-Path $repoRoot '.venv'

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

foreach ($directory in @($CommandDir, $ExportDir, $DataDir, $AuditDir, (Split-Path -Parent $MethodFile))) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

function ConvertTo-TomlPath {
    param([string] $Value)
    return $Value.Replace('\', '\\').Replace('"', '\"')
}

$configPath = Join-Path $repoRoot 'control-pc.toml'
if ((Test-Path -LiteralPath $configPath) -and -not $Force) {
    Write-Host "Preserving existing configuration: $configPath"
} else {
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
