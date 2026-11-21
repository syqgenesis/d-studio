# Shared helpers for the D Studio Windows build scripts (dot-source this file).
# Works on Windows PowerShell 5.1 and PowerShell 7+. Keep this file ASCII-only:
# PowerShell 5.1 reads BOM-less scripts in the ANSI code page.

Set-StrictMode -Off
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is 10x slower with the progress bar in 5.1
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Read-Pins {
    $path = Join-Path $PSScriptRoot 'pins.json'
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

# Run a native program without PowerShell turning its stderr into terminating
# errors (Windows PowerShell 5.1 + $ErrorActionPreference = 'Stop' does that).
# Output goes to the host; returns nothing. Throws on a non-zero exit code
# unless -AllowFail is given, in which case $global:LASTEXITCODE tells.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$ArgumentList = @(),
        [switch]$AllowFail
    )
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @ArgumentList 2>&1 | ForEach-Object { Write-Host "    $_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    $global:LASTEXITCODE = $code
    if (-not $AllowFail -and $code -ne 0) {
        throw "$([IO.Path]::GetFileName($Exe)) failed with exit code $code ($($ArgumentList -join ' '))"
    }
}

# Same, but returns stdout+stderr as one string (for parsing).
function Get-NativeOutput {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$ArgumentList = @()
    )
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $Exe @ArgumentList 2>&1 | ForEach-Object { "$_" }
        $global:LASTEXITCODE = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return ($lines -join "`n")
}

# Windows' own bsdtar (handles .tar.gz and .zip). Never use a GNU tar that
# happens to be first on PATH (Git for Windows): it treats "C:\..." as host:path.
function Get-SystemTar {
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (Test-Path -LiteralPath $tar) { return $tar }
    return $null
}

function Expand-ArchiveTo {
    param([Parameter(Mandatory = $true)][string]$Archive, [Parameter(Mandatory = $true)][string]$Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $tar = Get-SystemTar
    if ($tar) {
        Invoke-Native -Exe $tar -ArgumentList @('-xf', $Archive, '-C', $Destination)
        return
    }
    if ($Archive -match '\.zip$') {
        Expand-Archive -LiteralPath $Archive -DestinationPath $Destination -Force
        return
    }
    throw "tar.exe not found (Windows 10 1803+ ships it in System32); cannot extract $Archive"
}

# Download a pinned file into the cache (or reuse a cached copy) and verify sha256.
function Get-PinnedFile {
    param(
        [Parameter(Mandatory = $true)]$Pin,
        [Parameter(Mandatory = $true)][string]$CacheDir
    )
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $dest = Join-Path $CacheDir $Pin.file
    $want = ([string]$Pin.sha256).ToLowerInvariant()
    if (Test-Path -LiteralPath $dest) {
        if ((Get-Sha256 $dest) -eq $want) {
            Write-Host "    cached: $dest"
            return $dest
        }
        Write-Host "    cached copy has the wrong sha256, downloading again"
        Remove-Item -LiteralPath $dest -Force
    }
    $part = "$dest.part"
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    $ok = $false
    for ($attempt = 1; $attempt -le 4 -and -not $ok; $attempt++) {
        if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force }
        Write-Host "    download ($attempt/4): $($Pin.url)"
        try {
            if (Test-Path -LiteralPath $curl) {
                Invoke-Native -Exe $curl -ArgumentList @('-fsSL', '--retry', '3', '--retry-delay', '5',
                    '--connect-timeout', '30', '-o', $part, $Pin.url) -AllowFail
                $ok = ($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath $part)
            } else {
                Invoke-WebRequest -Uri $Pin.url -OutFile $part -UseBasicParsing -TimeoutSec 900
                $ok = Test-Path -LiteralPath $part
            }
        } catch {
            Write-Host "    download error: $($_.Exception.Message)"
            $ok = $false
        }
        if (-not $ok) { Start-Sleep -Seconds (5 * $attempt) }
    }
    if (-not $ok) { throw "could not download $($Pin.url)" }
    $got = Get-Sha256 $part
    if ($got -ne $want) {
        Remove-Item -LiteralPath $part -Force
        throw "sha256 mismatch for $($Pin.file): expected $want, got $got"
    }
    Move-Item -LiteralPath $part -Destination $dest -Force
    Write-Host "    verified sha256 $got"
    return $dest
}

# Unpack the pinned FFmpeg build and copy ffmpeg.exe / ffprobe.exe (+ license)
# into $Dest. Verifies libx264 is present and actually encodes.
function Install-PinnedFfmpeg {
    param(
        [Parameter(Mandatory = $true)][string]$Dest,
        [Parameter(Mandatory = $true)][string]$CacheDir
    )
    $pins = Read-Pins
    $zip = Get-PinnedFile -Pin $pins.ffmpeg -CacheDir $CacheDir
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("dstudio-ffmpeg-" + [Guid]::NewGuid().ToString('N'))
    try {
        Expand-ArchiveTo -Archive $zip -Destination $tmp
        $exe = Get-ChildItem -LiteralPath $tmp -Recurse -File -Filter 'ffmpeg.exe' | Select-Object -First 1
        if (-not $exe) { throw "ffmpeg.exe not found inside $($pins.ffmpeg.file)" }
        $bin = $exe.DirectoryName
        $buildRoot = Split-Path -Parent $bin
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
            $src = Join-Path $bin $name
            if (-not (Test-Path -LiteralPath $src)) { throw "$name missing in the FFmpeg build" }
            Copy-Item -LiteralPath $src -Destination (Join-Path $Dest $name) -Force
        }
        foreach ($doc in @('LICENSE', 'LICENSE.txt', 'README.txt')) {
            $src = Join-Path $buildRoot $doc
            if (Test-Path -LiteralPath $src) {
                $target = if ($doc -eq 'LICENSE') { 'LICENSE.txt' } else { $doc }
                Copy-Item -LiteralPath $src -Destination (Join-Path $Dest $target) -Force
            }
        }
        $info = @(
            "FFmpeg $($pins.ffmpeg.version) - $($pins.ffmpeg.source)",
            "Download : $($pins.ffmpeg.url)",
            "SHA256   : $($pins.ffmpeg.sha256)",
            "FFmpeg is licensed under the GPL v3 (see LICENSE.txt). Source code and build",
            "configuration: https://www.gyan.dev/ffmpeg/builds/ and https://ffmpeg.org/download.html"
        )
        Set-Content -LiteralPath (Join-Path $Dest 'SOURCE.txt') -Value $info -Encoding ASCII
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }

    $ff = Join-Path $Dest 'ffmpeg.exe'
    $encoders = Get-NativeOutput -Exe $ff -ArgumentList @('-hide_banner', '-encoders')
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg -encoders failed: $encoders" }
    $found = @{}
    foreach ($name in @('libx264', 'h264_nvenc', 'h264_amf', 'h264_qsv', 'hevc_nvenc')) {
        $found[$name] = [bool]($encoders -match ("(?m)^\s*\S+\s+" + [regex]::Escape($name) + "\s"))
        Write-Host ("    encoder {0,-11} {1}" -f $name, $(if ($found[$name]) { 'compiled in' } else { 'MISSING' }))
    }
    if (-not $found['libx264']) { throw 'the FFmpeg build has no libx264 (needed for Markov-format CRF 23 clips)' }
    Invoke-Native -Exe $ff -ArgumentList @('-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=320x240:rate=30', '-frames:v', '15', '-c:v', 'libx264', '-preset', 'veryfast',
        '-crf', '23', '-pix_fmt', 'yuv420p', '-f', 'null', '-')
    Invoke-Native -Exe (Join-Path $Dest 'ffprobe.exe') -ArgumentList @('-hide_banner', '-version') -AllowFail
    if ($LASTEXITCODE -ne 0) { throw 'ffprobe.exe does not run' }
    return $found
}
