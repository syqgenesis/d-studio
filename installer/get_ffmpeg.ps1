<#
.SYNOPSIS
  Download the pinned FFmpeg build (installer/pins.json), verify its sha256 and
  put ffmpeg.exe + ffprobe.exe into -Dest. Used by CI for the dev-folder tests
  and by build_windows.ps1 for the bundle.

.EXAMPLE
  ./installer/get_ffmpeg.ps1 -Dest "$env:RUNNER_TEMP\ffmpeg" -CacheDir build-cache
#>
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$CacheDir = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $CacheDir) { $CacheDir = Join-Path (Get-RepoRoot) 'build-cache' }
$CacheDir = [IO.Path]::GetFullPath($CacheDir)
$Dest = [IO.Path]::GetFullPath($Dest)

Write-Step "FFmpeg -> $Dest"
$null = Install-PinnedFfmpeg -Dest $Dest -CacheDir $CacheDir
Write-Host "    ok: $(Join-Path $Dest 'ffmpeg.exe')"
exit 0
