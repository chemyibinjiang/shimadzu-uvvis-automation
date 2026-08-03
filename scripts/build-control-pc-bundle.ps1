[CmdletBinding()]
param(
    [string] $OutputPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$git = Get-Command 'git.exe' -ErrorAction Stop
$projectFile = Join-Path $repoRoot 'pyproject.toml'
$projectContent = Get-Content -Raw -LiteralPath $projectFile
$versionMatch = [regex]::Match($projectContent, '(?m)^version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success) {
    throw "Could not read the project version from $projectFile"
}
$projectVersion = $versionMatch.Groups[1].Value

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $repoRoot "dist\shimadzu-uvvis-control-pc-$projectVersion.zip"
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
