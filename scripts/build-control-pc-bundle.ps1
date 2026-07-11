[CmdletBinding()]
param(
    [string] $OutputPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$git = Get-Command 'git.exe' -ErrorAction Stop

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $repoRoot 'dist\shimadzu-uvvis-control-pc-0.3.0.zip'
}
$output = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $output
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

$dirty = & $git.Source -C $repoRoot status --porcelain
if ($LASTEXITCODE -ne 0) {
    throw 'Could not inspect the Git repository.'
}
if ($dirty) {
    throw 'Commit the repository before building a control-PC bundle.'
}

& $git.Source -C $repoRoot archive `
    --format=zip `
    --prefix='shimadzu-uvvis-automation/' `
    --output=$output `
    HEAD
if ($LASTEXITCODE -ne 0) {
    throw 'git archive failed.'
}

Write-Host "Created control-PC bundle: $output"
