<#
.SYNOPSIS
  Build the D Studio Windows deliverables:
    dist\DStudio-Setup-<ver>.exe      per-user installer (Inno Setup 6, no admin)
    dist\DStudio-Portable-<ver>.zip   unzip-and-run folder (recordings in <folder>\data)

.DESCRIPTION
  1. stage a self-contained app folder in dist\_stage\DStudio:
       python\   relocatable CPython 3.12 x64 with tkinter (python-build-standalone, pinned + sha256)
                 + numpy / bettercam / comtypes / pillow (requirements-build.txt, hash-pinned wheels)
       ffmpeg\   ffmpeg.exe + ffprobe.exe (pinned gyan.dev build, sha256)
       recorder\ packager\ viewer\ tools\ schemas\ docs\ dstudio.pyw dstudio.ico README ...
  2. self-test the staged folder (dstudio.pyw --selftest)
  3. zip it (without the INSTALLED marker -> portable layout)
  4. write INSTALLED and compile installer\dstudio.iss with ISCC.

  Runs on GitHub Actions windows-latest (pwsh 7) and on a dev PC (Windows PowerShell 5.1+):
    powershell -ExecutionPolicy Bypass -File installer\build_windows.ps1

.PARAMETER OutDir       where the .exe / .zip go (default <repo>\dist)
.PARAMETER CacheDir     download cache (default <repo>\build-cache)
.PARAMETER SkipInstaller  only stage + zip (no Inno Setup needed)
.PARAMETER SkipZip        do not produce the portable zip
.PARAMETER StrictSelftest fail the build when the staged self-test reports a failure
#>
param(
    [string]$OutDir = '',
    [string]$CacheDir = '',
    [switch]$SkipInstaller,
    [switch]$SkipZip,
    [switch]$StrictSelftest
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$started = Get-Date
$Repo = Get-RepoRoot
if (-not $OutDir) { $OutDir = Join-Path $Repo 'dist' }
if (-not $CacheDir) { $CacheDir = Join-Path $Repo 'build-cache' }
$OutDir = [IO.Path]::GetFullPath($OutDir)
$CacheDir = [IO.Path]::GetFullPath($CacheDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# Keep the build machine's Python environment out of the bundled interpreter.
foreach ($name in @('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE', 'PIP_REQUIRE_VIRTUALENV', 'VIRTUAL_ENV')) {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INPUT = '1'

# ------------------------------------------------------------------ version
$versionLine = (Get-Content -LiteralPath (Join-Path $Repo 'VERSION.txt') -Encoding UTF8 -TotalCount 5) -join "`n"
$m = [regex]::Match($versionLine, '\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?')
if (-not $m.Success) { throw 'VERSION.txt must start with a version such as 0.2.0' }
$Version = $m.Value
$VersionNumeric = [regex]::Match($Version, '^\d+\.\d+\.\d+').Value
Write-Step "D Studio $Version  (repo $Repo)"

$pins = Read-Pins
$stageRoot = Join-Path $OutDir '_stage'
$stage = Join-Path $stageRoot 'DStudio'
if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# ------------------------------------------------------------------ python
Write-Step "Python $($pins.python.version) (python-build-standalone)"
$pyArchive = Get-PinnedFile -Pin $pins.python -CacheDir $CacheDir
Expand-ArchiveTo -Archive $pyArchive -Destination $stage      # -> $stage\python\...
$pyDir = Join-Path $stage 'python'
$py = Join-Path $pyDir 'python.exe'
$pyw = Join-Path $pyDir 'pythonw.exe'
foreach ($f in @($py, $pyw, (Join-Path $pyDir 'DLLs\_tkinter.pyd'), (Join-Path $pyDir 'tcl\tcl8.6\init.tcl'))) {
    if (-not (Test-Path -LiteralPath $f)) { throw "unexpected Python archive layout: missing $f" }
}

Write-Host '    trimming debug symbols / test suites'
Get-ChildItem -LiteralPath $pyDir -Recurse -File -Include '*.pdb' | Remove-Item -Force
foreach ($rel in @('Lib\test', 'Lib\idlelib\idle_test', 'Lib\tkinter\test', 'Lib\lib2to3\tests', 'include', 'libs')) {
    $p = Join-Path $pyDir $rel
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
}
Get-ChildItem -LiteralPath (Join-Path $pyDir 'DLLs') -File -Filter '_test*.pyd' | Remove-Item -Force
$ctTest = Join-Path $pyDir 'DLLs\_ctypes_test.pyd'
if (Test-Path -LiteralPath $ctTest) { Remove-Item -LiteralPath $ctTest -Force }

$verify = Join-Path $PSScriptRoot 'verify_bundle.py'
Invoke-Native -Exe $py -ArgumentList @('-E', '-s', $verify)

# ------------------------------------------------------------------ packages
Write-Step 'Python packages (requirements-build.txt, hash-pinned)'
Invoke-Native -Exe $py -ArgumentList @('-E', '-s', '-m', 'pip', 'install', '--no-cache-dir',
    '--disable-pip-version-check', '--no-warn-script-location', '--only-binary=:all:',
    '--require-hashes', '-r', (Join-Path $Repo 'requirements-build.txt'))
# console-script launchers in Scripts\ hard-code this build path; they are not used
$scripts = Join-Path $pyDir 'Scripts'
if (Test-Path -LiteralPath $scripts) { Get-ChildItem -LiteralPath $scripts -File -Filter '*.exe' | Remove-Item -Force }
# numpy ships its own test suite (~15 MB); the app never imports it
Get-ChildItem -LiteralPath (Join-Path $pyDir 'Lib\site-packages\numpy') -Recurse -Directory -Filter 'tests' |
    Sort-Object { $_.FullName.Length } -Descending | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
Invoke-Native -Exe $py -ArgumentList @('-E', '-s', $verify, '--packages')

# ------------------------------------------------------------------ app code
Write-Step 'App code'
function Copy-Dir([string]$Name) {
    $src = Join-Path $Repo $Name
    if (-not (Test-Path -LiteralPath $src)) { throw "missing source folder $Name" }
    $dst = Join-Path $stage $Name
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & robocopy.exe $src $dst /E /NFL /NDL /NJH /NJS /NP /R:2 /W:1 `
        /XD __pycache__ .git .pytest_cache `
        /XF *.pyc *.pyo *.before-* .DS_Store Thumbs.db *.log | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ge 8) { throw "robocopy $Name failed ($code)" }
    $global:LASTEXITCODE = 0
    Write-Host "    $Name"
}
foreach ($dir in @('recorder', 'packager', 'viewer', 'tools', 'schemas', 'docs')) { Copy-Dir $dir }
# internal build notes are not user documentation
Get-ChildItem -LiteralPath (Join-Path $stage 'docs') -File -Filter '*contracts*.md' | Remove-Item -Force

$rootFiles = @('dstudio.pyw', 'VERSION.txt', 'LICENSE')
$rootFiles += Get-ChildItem -LiteralPath $Repo -File | Where-Object {
    ($_.Extension -ieq '.md' -or $_.Extension -ieq '.txt') -and $_.Name -notlike 'requirements*'
} | ForEach-Object { $_.Name }
$rootFiles += Get-ChildItem -LiteralPath $Repo -File -Filter '*.bat' | Where-Object {
    $_.Name -like 'START-*.bat' -or $_.Name -eq 'SELFTEST.bat'
} | ForEach-Object { $_.Name }
foreach ($name in ($rootFiles | Where-Object { $_ } | Select-Object -Unique)) {
    $src = Join-Path $Repo $name
    if (-not (Test-Path -LiteralPath $src)) { throw "missing $name" }
    Copy-Item -LiteralPath $src -Destination (Join-Path $stage $name) -Force
    Write-Host "    $name"
}

# ------------------------------------------------------------------ ffmpeg
Write-Step "FFmpeg $($pins.ffmpeg.version)"
$encoders = Install-PinnedFfmpeg -Dest (Join-Path $stage 'ffmpeg') -CacheDir $CacheDir

# ------------------------------------------------------------------ icon
Write-Step 'Icon'
$icon = Join-Path $stage 'dstudio.ico'
$repoIcon = Join-Path $PSScriptRoot 'dstudio.ico'
if (Test-Path -LiteralPath $repoIcon) {
    Copy-Item -LiteralPath $repoIcon -Destination $icon -Force
} else {
    Invoke-Native -Exe $py -ArgumentList @('-E', '-s', (Join-Path $PSScriptRoot 'make_icon.py'),
        '--out', $icon, '--png', (Join-Path $stage 'dstudio.png'))
}

# ------------------------------------------------------------------ bytecode
Write-Step 'Pre-compiling bytecode (checked-hash .pyc, so the install dir never needs to be writable)'
Invoke-Native -Exe $py -ArgumentList @('-E', '-s', '-m', 'compileall', '-q', '-f', '-j', '0',
    '--invalidation-mode', 'checked-hash', '-x', '[\\/](test|tests|idle_test|lib2to3)[\\/]', $stage) -AllowFail
if ($LASTEXITCODE -ne 0) { Write-Host "    compileall reported errors (exit $LASTEXITCODE); continuing" -ForegroundColor Yellow }
$global:LASTEXITCODE = 0

# ------------------------------------------------------------------ self-test
Write-Step 'Self-test of the staged folder'
$stTmp = Join-Path ([IO.Path]::GetTempPath()) ('dstudio-selftest-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stTmp | Out-Null
$savedData = $env:DSTUDIO_DATA_DIR; $savedSettings = $env:DSTUDIO_SETTINGS; $savedFf = $env:DSTUDIO_FFMPEG
$env:DSTUDIO_DATA_DIR = Join-Path $stTmp 'data'
$env:DSTUDIO_SETTINGS = Join-Path $stTmp 'settings.json'
Remove-Item Env:DSTUDIO_FFMPEG -ErrorAction SilentlyContinue   # must find the bundled ffmpeg by itself
$selftestJson = Join-Path $OutDir 'selftest-stage.json'
try {
    Invoke-Native -Exe $py -ArgumentList @('-E', '-s', (Join-Path $stage 'dstudio.pyw'), '--selftest',
        '--no-pause', '--no-dialog', '--json', $selftestJson) -AllowFail
    $selftestCode = $LASTEXITCODE
} finally {
    $env:DSTUDIO_DATA_DIR = $savedData; $env:DSTUDIO_SETTINGS = $savedSettings; $env:DSTUDIO_FFMPEG = $savedFf
    if (-not $savedData) { Remove-Item Env:DSTUDIO_DATA_DIR -ErrorAction SilentlyContinue }
    if (-not $savedSettings) { Remove-Item Env:DSTUDIO_SETTINGS -ErrorAction SilentlyContinue }
    if (-not $savedFf) { Remove-Item Env:DSTUDIO_FFMPEG -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $stTmp -Recurse -Force -ErrorAction SilentlyContinue
}
$global:LASTEXITCODE = 0
if ($selftestCode -ne 0) {
    $msg = "staged self-test reported problems (exit $selftestCode), see $selftestJson"
    if ($StrictSelftest) { throw $msg }
    Write-Host "    WARNING: $msg" -ForegroundColor Yellow
}
# the payload must not carry recordings or logs from the self-test
foreach ($junk in @('data', 'logs')) {
    $p = Join-Path $stage $junk
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
}

# ------------------------------------------------------------------ portable zip
$zipPath = Join-Path $OutDir "DStudio-Portable-$Version.zip"
$topName = "DStudio-$Version"
if (-not $SkipZip) {
    Write-Step "Portable zip -> $zipPath"
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $base = (Resolve-Path -LiteralPath $stage).Path.TrimEnd('\') + '\'
    $fs = [IO.File]::Open($zipPath, [IO.FileMode]::CreateNew)
    $zip = New-Object IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Create)
    $count = 0
    try {
        foreach ($file in (Get-ChildItem -LiteralPath $stage -Recurse -File -Force)) {
            $rel = $file.FullName.Substring($base.Length).Replace('\', '/')
            if ($rel -ieq 'INSTALLED') { continue }        # portable: data lives in <folder>\data
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName,
                "$topName/$rel", [IO.Compression.CompressionLevel]::Optimal)
            $count++
        }
    } finally {
        $zip.Dispose()
        $fs.Dispose()
    }
    # verify
    $check = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($check.Entries | ForEach-Object { $_.FullName })
    } finally { $check.Dispose() }
    foreach ($must in @("$topName/dstudio.pyw", "$topName/python/pythonw.exe", "$topName/ffmpeg/ffmpeg.exe",
                        "$topName/recorder/gui.py", "$topName/viewer/index.html")) {
        if ($names -notcontains $must) { throw "portable zip is missing $must" }
    }
    if ($names -contains "$topName/INSTALLED") { throw 'portable zip must not contain INSTALLED' }
    Write-Host ("    {0} files, {1:N1} MB" -f $count, ((Get-Item -LiteralPath $zipPath).Length / 1MB))
}

# ------------------------------------------------------------------ installer
$marker = @(
    "D Studio $Version - installed by DStudio-Setup-$Version.exe.",
    'This marker switches recordings to Documents\DStudio (see recorder/paths.py).',
    'Delete it to make this folder behave like the portable zip (recordings in .\data).'
)
Set-Content -LiteralPath (Join-Path $stage 'INSTALLED') -Value $marker -Encoding ASCII

$setupPath = Join-Path $OutDir "DStudio-Setup-$Version.exe"
$isccPath = $null
if (-not $SkipInstaller) {
    Write-Step 'Inno Setup'
    function Find-Iscc {
        $cands = New-Object System.Collections.Generic.List[string]
        if ($env:ISCC) { $cands.Add($env:ISCC) }
        $cmd = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
        if ($cmd) { $cands.Add($cmd.Source) }
        foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
            if ($base) {
                $cands.Add((Join-Path $base 'Inno Setup 6\ISCC.exe'))
                $cands.Add((Join-Path $base 'Inno Setup 7\ISCC.exe'))
            }
        }
        foreach ($hive in @('HKLM:\SOFTWARE\WOW6432Node', 'HKLM:\SOFTWARE', 'HKCU:\SOFTWARE')) {
            foreach ($key in @('Inno Setup 6_is1', 'Inno Setup 7_is1')) {
                $reg = Join-Path $hive "Microsoft\Windows\CurrentVersion\Uninstall\$key"
                $loc = (Get-ItemProperty -Path $reg -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
                if ($loc) { $cands.Add((Join-Path $loc 'ISCC.exe')) }
            }
        }
        foreach ($c in $cands) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
        return $null
    }
    $isccPath = Find-Iscc
    if (-not $isccPath) {
        $choco = Get-Command 'choco.exe' -ErrorAction SilentlyContinue
        if (-not $choco) {
            throw 'Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) or pass -SkipInstaller.'
        }
        Write-Host "    installing Inno Setup $($pins.innosetup.choco_version) with Chocolatey"
        Invoke-Native -Exe $choco.Source -ArgumentList @('install', 'innosetup', '--version', $pins.innosetup.choco_version,
            '-y', '--no-progress') -AllowFail
        if ($LASTEXITCODE -ne 0) {
            Invoke-Native -Exe $choco.Source -ArgumentList @('install', 'innosetup', '-y', '--no-progress')
        }
        $isccPath = Find-Iscc
        if (-not $isccPath) { throw 'Inno Setup was installed but ISCC.exe cannot be found' }
    }
    Write-Host "    ISCC: $isccPath"
    $zhIsl = Join-Path (Split-Path -Parent $isccPath) 'Languages\ChineseSimplified.isl'
    if (-not (Test-Path -LiteralPath $zhIsl)) { $zhIsl = Join-Path $PSScriptRoot 'lang\ChineseSimplified.isl' }
    Write-Host "    Chinese messages: $zhIsl"
    if (Test-Path -LiteralPath $setupPath) { Remove-Item -LiteralPath $setupPath -Force }
    Invoke-Native -Exe $isccPath -ArgumentList @('/Qp',
        "/DAppVersion=$Version", "/DAppVersionNumeric=$VersionNumeric",
        "/DSourceDir=$stage", "/DOutputDir=$OutDir", "/DZhIsl=$zhIsl",
        (Join-Path $PSScriptRoot 'dstudio.iss'))
    if (-not (Test-Path -LiteralPath $setupPath)) { throw "ISCC did not produce $setupPath" }
    Write-Host ("    {0} ({1:N1} MB)" -f $setupPath, ((Get-Item -LiteralPath $setupPath).Length / 1MB))
}

# ------------------------------------------------------------------ checksums / info
Write-Step 'Checksums'
$sums = @()
foreach ($f in @($setupPath, $zipPath)) {
    if (Test-Path -LiteralPath $f) { $sums += "$(Get-Sha256 $f)  $([IO.Path]::GetFileName($f))" }
}
Set-Content -LiteralPath (Join-Path $OutDir 'SHA256SUMS.txt') -Value $sums -Encoding ASCII
$sums | ForEach-Object { Write-Host "    $_" }

$info = [ordered]@{
    version          = $Version
    built_at         = (Get-Date).ToUniversalTime().ToString('o')
    build_seconds    = [int]((Get-Date) - $started).TotalSeconds
    python           = $pins.python.version
    python_archive   = $pins.python.file
    ffmpeg           = $pins.ffmpeg.version
    ffmpeg_encoders  = $encoders
    iscc             = $isccPath
    selftest_exit    = $selftestCode
    setup            = $(if (Test-Path -LiteralPath $setupPath) { [IO.Path]::GetFileName($setupPath) } else { $null })
    portable_zip     = $(if (Test-Path -LiteralPath $zipPath) { [IO.Path]::GetFileName($zipPath) } else { $null })
    stage_mb         = [math]::Round(((Get-ChildItem -LiteralPath $stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
}
$info | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutDir 'build-info.json') -Encoding UTF8
Write-Step ("done in {0:N0}s" -f ((Get-Date) - $started).TotalSeconds)
exit 0
