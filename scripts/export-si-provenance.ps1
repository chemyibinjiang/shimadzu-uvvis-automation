[CmdletBinding()]
param(
    [string] $OutputDirectory,
    [string] $ControlConfig = 'D:\UVVis-Automation\control-pc.toml',
    [string] $InstrumentManufacturer = 'Shimadzu Corporation',
    [string] $InstrumentModel = 'UV-2700i',
    [string] $LabSolutionsVersion = '1.13',
    [string] $LabSolutionsReleaseDocument = '207-90427G',
    [switch] $AllowDirty,
    [switch] $SkipArchive
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$git = Get-Command 'git.exe' -ErrorAction Stop
$projectFile = Join-Path $repoRoot 'pyproject.toml'

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot 'dist\uvvis_instrument_configuration_and_software_versions'
}
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
$archive = "$output.zip"
$generatedFileNames = @(
    'README.md',
    'hardware_and_software_versions.csv',
    'repository_commit.txt',
    'instrument_control_configuration.toml',
    'method_file_hashes.csv'
)

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] [AllowEmptyString()] [string] $Content
    )

    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Write-CsvUtf8NoBom {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] [object[]] $Rows
    )

    $content = ($Rows | ConvertTo-Csv -NoTypeInformation) -join "`r`n"
    Write-Utf8NoBom -Path $Path -Content ($content + "`r`n")
}

function Invoke-RepositoryGit {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Arguments)

    $result = & $git.Source -C $repoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')"
    }
    return (($result | Out-String).Trim())
}

function Read-ProjectVersion {
    $content = Get-Content -Raw -LiteralPath $projectFile
    $match = [regex]::Match($content, '(?m)^version\s*=\s*"([^"]+)"\s*$')
    if (-not $match.Success) {
        throw "Could not read the project version from $projectFile"
    }
    return $match.Groups[1].Value
}

function Read-TomlStringValue {
    param([Parameter(Mandatory = $true)] [string] $Value)

    return $Value.Replace('\\', '\').Replace('\"', '"')
}

function Read-MethodTemplates {
    param([Parameter(Mandatory = $true)] [string] $Path)

    $templates = @{}
    $currentName = $null
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*\[method_templates\.([^\]]+)\]\s*$') {
            $currentName = $Matches[1]
            $templates[$currentName] = [ordered]@{}
            continue
        }
        if ($line -match '^\s*\[') {
            $currentName = $null
            continue
        }
        if ($null -ne $currentName -and
            $line -match '^\s*(mode|signal_type|method_file|sha256)\s*=\s*"((?:\\.|[^"])*)"\s*$') {
            $templates[$currentName][$Matches[1]] = Read-TomlStringValue -Value $Matches[2]
        }
    }
    return $templates
}

if (-not (Test-Path -LiteralPath $ControlConfig -PathType Leaf)) {
    throw "Control configuration does not exist: $ControlConfig"
}

$configText = Get-Content -Raw -LiteralPath $ControlConfig
$secretAssignment = [regex]::Match(
    $configText,
    '(?im)^\s*(?:token|password|secret|api[_-]?key)\s*=\s*"[^\"]+"\s*$'
)
if ($secretAssignment.Success) {
    throw 'The control configuration contains a possible secret assignment and was not exported.'
}

$status = Invoke-RepositoryGit status --porcelain --untracked-files=all
$isDirty = -not [string]::IsNullOrWhiteSpace($status)
if ($isDirty -and -not $AllowDirty) {
    throw 'The repository has uncommitted changes. Commit or remove them before generating the final SI provenance package, or use -AllowDirty for a draft package.'
}

$projectVersion = Read-ProjectVersion
$commit = Invoke-RepositoryGit rev-parse HEAD
$branch = Invoke-RepositoryGit rev-parse --abbrev-ref HEAD
$remote = & $git.Source -C $repoRoot config --get remote.origin.url
if ($LASTEXITCODE -ne 0) {
    $remote = ''
} else {
    $remote = (($remote | Out-String).Trim())
}
$generatedUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')

$osCaption = [System.Environment]::OSVersion.VersionString
$osVersion = [System.Environment]::OSVersion.Version.ToString()
try {
    $operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $buildNumber = [int]$operatingSystem.BuildNumber
    if ($buildNumber -ge 22000) {
        $osCaption = 'Windows 11'
    } elseif ($buildNumber -ge 10240) {
        $osCaption = 'Windows 10'
    } else {
        $osCaption = 'Windows'
    }
    $osVersion = "$($operatingSystem.Version) (build $($operatingSystem.BuildNumber))"
} catch {
    # Environment.OSVersion is retained as a portable fallback.
}

$pythonVersion = ''
$pythonCandidates = @(
    (Join-Path $repoRoot '.venv\Scripts\python.exe'),
    'python.exe'
)
foreach ($candidate in $pythonCandidates) {
    try {
        if ($candidate -eq 'python.exe' -or (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $versionText = & $candidate --version 2>&1
            if ($LASTEXITCODE -eq 0) {
                $pythonVersion = (($versionText | Out-String).Trim()) -replace '^Python\s+', ''
                break
            }
        }
    } catch {
        continue
    }
}

$parent = Split-Path -Parent $output
New-Item -ItemType Directory -Path $parent -Force | Out-Null
if (Test-Path -LiteralPath $output) {
    if (-not (Test-Path -LiteralPath $output -PathType Container)) {
        throw "The output path exists and is not a directory: $output"
    }
    $unexpected = @(Get-ChildItem -LiteralPath $output -Force | Where-Object {
        $_.Name -notin $generatedFileNames
    })
    if ($unexpected.Count -gt 0) {
        $unexpectedNames = ($unexpected | Select-Object -ExpandProperty Name) -join ', '
        throw "The output directory contains files not created by this script: $unexpectedNames"
    }
    foreach ($name in $generatedFileNames) {
        $existing = Join-Path $output $name
        if (Test-Path -LiteralPath $existing -PathType Leaf) {
            Remove-Item -LiteralPath $existing -Force
        }
    }
}
New-Item -ItemType Directory -Path $output -Force | Out-Null

$configurationCopy = Join-Path $output 'instrument_control_configuration.toml'
Copy-Item -LiteralPath $ControlConfig -Destination $configurationCopy
$configurationHash = (Get-FileHash -LiteralPath $configurationCopy -Algorithm SHA256).Hash

$hardwareRows = @(
    [pscustomobject][ordered]@{
        component = 'UV-Vis spectrophotometer'
        manufacturer = $InstrumentManufacturer
        model = $InstrumentModel
        version = ''
        evidence = 'Instrument registered on the laboratory control computer'
    },
    [pscustomobject][ordered]@{
        component = 'Instrument software'
        manufacturer = $InstrumentManufacturer
        model = 'LabSolutions UV-Vis'
        version = $LabSolutionsVersion
        evidence = "Release Notes $LabSolutionsReleaseDocument and laboratory installation"
    },
    [pscustomobject][ordered]@{
        component = 'Instrument interface service'
        manufacturer = ''
        model = 'shimadzu-uvvis-automation'
        version = $projectVersion
        evidence = 'pyproject.toml and repository commit'
    },
    [pscustomobject][ordered]@{
        component = 'Control-computer operating system'
        manufacturer = 'Microsoft'
        model = $osCaption
        version = $osVersion
        evidence = 'Win32_OperatingSystem queried during package generation'
    },
    [pscustomobject][ordered]@{
        component = 'Python runtime'
        manufacturer = 'Python Software Foundation'
        model = 'Python'
        version = $pythonVersion
        evidence = 'Runtime queried during package generation'
    },
    [pscustomobject][ordered]@{
        component = 'PowerShell runtime'
        manufacturer = 'Microsoft'
        model = 'PowerShell'
        version = $PSVersionTable.PSVersion.ToString()
        evidence = 'Runtime queried during package generation'
    }
)
Write-CsvUtf8NoBom -Path (Join-Path $output 'hardware_and_software_versions.csv') -Rows $hardwareRows

$templates = Read-MethodTemplates -Path $ControlConfig
if ($templates.Count -eq 0) {
    throw "No [method_templates.*] sections were found in $ControlConfig"
}

$methodRows = foreach ($name in ($templates.Keys | Sort-Object)) {
    $template = $templates[$name]
    $methodFile = [string]$template['method_file']
    $declaredHash = ([string]$template['sha256']).ToUpperInvariant()
    $exists = -not [string]::IsNullOrWhiteSpace($methodFile) -and
        (Test-Path -LiteralPath $methodFile -PathType Leaf)
    $observedHash = if ($exists) {
        (Get-FileHash -LiteralPath $methodFile -Algorithm SHA256).Hash
    } else {
        ''
    }
    $matches = if ([string]::IsNullOrWhiteSpace($declaredHash)) {
        'not_declared'
    } elseif (-not $exists) {
        'file_missing'
    } elseif ($declaredHash -eq $observedHash) {
        'true'
    } else {
        'false'
    }

    [pscustomobject][ordered]@{
        template = $name
        mode = [string]$template['mode']
        signal_type = [string]$template['signal_type']
        method_file = $methodFile
        file_exists = $exists.ToString().ToLowerInvariant()
        declared_sha256 = $declaredHash
        observed_sha256 = $observedHash
        hash_matches = $matches
    }
}
Write-CsvUtf8NoBom -Path (Join-Path $output 'method_file_hashes.csv') -Rows $methodRows

$failedMethods = @($methodRows | Where-Object { $_.hash_matches -ne 'true' })
if ($failedMethods.Count -gt 0) {
    $failedNames = ($failedMethods | ForEach-Object { "$($_.template)=$($_.hash_matches)" }) -join ', '
    throw "One or more registered method files could not be verified: $failedNames"
}

$repositoryRecord = @"
repository=$remote
version=$projectVersion
commit=$commit
branch=$branch
worktree_dirty=$($isDirty.ToString().ToLowerInvariant())
generated_utc=$generatedUtc
"@
Write-Utf8NoBom -Path (Join-Path $output 'repository_commit.txt') -Content $repositoryRecord

$releaseStatus = if ($isDirty) {
    'DRAFT: the repository contained uncommitted changes when this package was generated. Regenerate from the final clean commit before submission.'
} else {
    'FINAL-CANDIDATE: the repository was clean when this package was generated.'
}

$readme = @"
# UV-Vis instrument configuration and software versions

This package records the hardware, software, repository and LabSolutions method files used by the Shimadzu UV-Vis interface.

Status: **$releaseStatus**

Generated: $generatedUtc

## Files

- hardware_and_software_versions.csv: instrument, LabSolutions, interface-service and runtime versions.
- repository_commit.txt: exact Git revision and worktree state at generation time.
- instrument_control_configuration.toml: copy of the control-computer configuration used to resolve paths, safety gates and registered LabSolutions methods.
- method_file_hashes.csv: declared and independently calculated SHA-256 values for the registered LabSolutions method templates.

The copied control configuration has SHA-256 $configurationHash.

This package does not contain LabSolutions installation files, proprietary method files, product manuals or instrument data. The method files remain on the control computer and are identified here by path and SHA-256 only.
"@
Write-Utf8NoBom -Path (Join-Path $output 'README.md') -Content $readme

if (-not $SkipArchive) {
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
    Compress-Archive -Path (Join-Path $output '*') -DestinationPath $archive -CompressionLevel Optimal
}

Write-Host "Created SI provenance directory: $output"
if (-not $SkipArchive) {
    Write-Host "Created SI provenance archive:   $archive"
}
Write-Host "Repository commit:              $commit"
Write-Host "Repository dirty:               $($isDirty.ToString().ToLowerInvariant())"
Write-Host "Verified method templates:      $($methodRows.Count)"
