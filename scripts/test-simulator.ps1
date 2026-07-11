[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found. Run scripts\setup-control-pc.ps1 first."
}

$env:PYTHONPATH = Join-Path $repoRoot 'src'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$runtime = Join-Path $repoRoot "runtime\simulator-acceptance\$stamp"
$control = Join-Path $runtime 'control'
$export = Join-Path $runtime 'export'
$data = Join-Path $runtime 'data'
$audit = Join-Path $runtime 'audit'
$reports = Join-Path $runtime 'reports'
$method = Join-Path $runtime 'SIMULATED_ONLY.vspm'
$ready = Join-Path $runtime 'simulator.ready'
$commandLog = Join-Path $runtime 'simulator-commands.jsonl'
$configPath = Join-Path $runtime 'simulator.toml'

foreach ($directory in @($control, $export, $data, $audit, $reports)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText(
    $method,
    'SIMULATED method placeholder - not a LabSolutions method file',
    $utf8NoBom
)

function ConvertTo-TomlPath {
    param([string] $Value)
    return $Value.Replace('\', '\\').Replace('"', '\"')
}

$config = @"
[labsolutions]
command_dir = "$(ConvertTo-TomlPath $control)"
mode = "spectrum"
timeout_seconds = 5.0
poll_interval_seconds = 0.05
lock_timeout_seconds = 1.0
encoding = "utf-8"

[export]
directory = "$(ConvertTo-TomlPath $export)"
pattern = "{sample_id}*_SIMULATED.csv"
timeout_seconds = 5.0
stable_seconds = 0.1

[spectrum]
method_file = "$(ConvertTo-TomlPath $method)"
data_dir = "$(ConvertTo-TomlPath $data)"
measurement_mode = 2
connect_before_run = false
disconnect_after_run = false
correction = "none"
discharge_after_measurement = false
allow_unicode_identifiers = false

[scan_profiles.default]
method_file = "$(ConvertTo-TomlPath $method)"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0
scan_speed_nm_per_min = 600.0

[audit]
directory = "$(ConvertTo-TomlPath $audit)"
"@
[System.IO.File]::WriteAllText($configPath, $config, $utf8NoBom)

function Quote-ProcessArgument {
    param([string] $Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

$simulatorArguments = @(
    '-m',
    'shimadzu_uvvis.simulator',
    '--command-dir',
    (Quote-ProcessArgument $control),
    '--export-dir',
    (Quote-ProcessArgument $export),
    '--ready-file',
    (Quote-ProcessArgument $ready),
    '--command-log',
    (Quote-ProcessArgument $commandLog)
)
$simulator = Start-Process `
    -FilePath $python `
    -ArgumentList $simulatorArguments `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtime 'simulator.stdout.log') `
    -RedirectStandardError (Join-Path $runtime 'simulator.stderr.log') `
    -PassThru

function Invoke-CheckedStep {
    param([string] $Name, [string[]] $Arguments)
    $lines = & $python @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    [System.IO.File]::WriteAllLines(
        (Join-Path $reports "$Name.json"),
        [string[]] $lines,
        $utf8NoBom
    )
    foreach ($line in $lines) {
        Write-Host $line
    }
    if ($exitCode -ne 0) {
        throw "$Name failed with exit code $exitCode"
    }
}

try {
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path -LiteralPath $ready -PathType Leaf)) {
        if ($simulator.HasExited) {
            throw 'The simulator exited before it became ready.'
        }
        if ((Get-Date) -gt $deadline) {
            throw 'Timed out waiting for the simulator to become ready.'
        }
        Start-Sleep -Milliseconds 100
    }

    Invoke-CheckedStep 'doctor' @(
        '-m', 'shimadzu_uvvis', '--config', $configPath, 'doctor', '--write-check'
    )
    Invoke-CheckedStep 'ping' @(
        '-m', 'shimadzu_uvvis', '--config', $configPath, 'ping'
    )
    $sampleId = "sim_$stamp"
    Invoke-CheckedStep 'plan' @(
        '-m', 'shimadzu_uvvis', '--config', $configPath, 'spectrum',
        '--sample-name', 'simulator_validation', '--sample-id', $sampleId,
        '--start', '300', '--stop', '900', '--step', '1',
        '--wavelengths', '450', '550', '650'
    )
    Invoke-CheckedStep 'measurement' @(
        '-m', 'shimadzu_uvvis', '--config', $configPath, 'spectrum',
        '--sample-name', 'simulator_validation', '--sample-id', $sampleId,
        '--start', '300', '--stop', '900', '--step', '1',
        '--wavelengths', '450', '550', '650',
        '--execute'
    )
    $seriesId = "series_$stamp"
    Invoke-CheckedStep 'series-plan' @(
        '-m', 'shimadzu_uvvis', '--config', $configPath, 'series',
        '--sample-name', 'simulator_growth', '--series-id', $seriesId,
        '--profile', 'default', '--count', '3', '--interval-seconds', '0.5'
    )
    Invoke-CheckedStep 'series-measurement' @(
        '-m', 'shimadzu_uvvis', '--config', $configPath, 'series',
        '--sample-name', 'simulator_growth', '--series-id', $seriesId,
        '--profile', 'default', '--count', '3', '--interval-seconds', '0.5',
        '--execute'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $reports 'PASS.txt'),
        "Simulator acceptance passed at $(Get-Date -Format o)",
        $utf8NoBom
    )
    Write-Host ''
    Write-Host "SIMULATOR ACCEPTANCE PASSED: $reports"
} finally {
    if ($null -ne $simulator -and -not $simulator.HasExited) {
        Stop-Process -Id $simulator.Id
        $simulator.WaitForExit()
    }
}
