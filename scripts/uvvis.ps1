[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $UvvisArguments
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found. Run scripts\setup-control-pc.ps1 first."
}

& $python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.11 or newer is required.'
}

$env:PYTHONPATH = Join-Path $repoRoot 'src'
$pythonArguments = @('-m', 'shimadzu_uvvis')
$configPath = Join-Path $repoRoot 'control-pc.toml'
$hasConfigArgument = $UvvisArguments -contains '--config'

if ((Test-Path -LiteralPath $configPath -PathType Leaf) -and -not $hasConfigArgument) {
    $pythonArguments += @('--config', $configPath)
}
$pythonArguments += $UvvisArguments

& $python @pythonArguments
exit $LASTEXITCODE
