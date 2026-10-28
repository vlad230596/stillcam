<#
.SYNOPSIS
  Writes the official Radxa image to an SD card and verifies it by reading it back.

.DESCRIPTION
  The disk is given by number (Get-Disk) and checked: USB/SD only, not system and not boot, at most 256 GB.
  Without administrator rights the script restarts itself with them (UAC prompt) and waits; output goes to a log whose path is printed.
  The card is wiped completely (Clear-Disk), then flash_sd.py unpacks the .img.xz straight onto it (docs/image.md).

.EXAMPLE
  powershell -File image\radxa-zero3\flash-sd.ps1 -DiskNumber <N> -Image $env:USERPROFILE\Downloads\radxa\radxa-zero3_bookworm_kde_b1.output_512.img.xz
#>
param(
    [Parameter(Mandatory)] [int] $DiskNumber,
    [Parameter(Mandatory)] [string] $Image,
    # sha256 of the .img.xz file; default — rsdk-b1 radxa-zero3_bookworm_kde_b1.output_512.img.xz
    [string] $Sha256 = "47b70ec81377f618505951fc9b4c40784e983c7fdff9706fdf79c13b6cd20730",
    # Do not ask for wipe confirmation.
    [switch] $Force,
    [string] $Log = ""
)
$ErrorActionPreference = "Stop"
$Image = (Resolve-Path $Image).Path
if (-not $Log) { $Log = Join-Path $env:TEMP "flash-sd-$DiskNumber.log" }

$disk = Get-Disk -Number $DiskNumber
$gb = [math]::Round($disk.Size / 1GB, 1)
$what = "disk $DiskNumber '$($disk.FriendlyName)', $($disk.BusType), $gb GB"
if ($disk.BusType -notin "USB", "SD", "MMC") { throw "$what — not USB/SD, leaving it alone" }
if ($disk.IsBoot -or $disk.IsSystem) { throw "$what — system or boot disk, leaving it alone" }
if ($disk.Size -gt 256GB) { throw "$what — larger than 256 GB, does not look like an SD card" }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Administrator rights required — restarting with them (UAC prompt). Log: $Log"
    Remove-Item $Log -ErrorAction SilentlyContinue
    $argv = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-DiskNumber", $DiskNumber,
        "-Image", "`"$Image`"", "-Sha256", $Sha256, "-Log", "`"$Log`"")
    if ($Force) { $argv += "-Force" }
    $p = Start-Process powershell -Verb RunAs -ArgumentList $argv -Wait -PassThru
    if (Test-Path $Log) { Get-Content $Log }
    exit $p.ExitCode
}

function Say([string] $s) { Write-Host $s; Add-Content -Path $Log -Value $s -Encoding UTF8 }
try {
    Say "== $what"
    if (-not $Force) {
        $answer = Read-Host "Everything on the card will be erased. Enter the disk number ($DiskNumber) to continue"
        if ($answer -ne "$DiskNumber") { throw "cancelled" }
    }
    Say "== wiping partitions"
    Clear-Disk -Number $DiskNumber -RemoveData -RemoveOEM -Confirm:$false -ErrorAction SilentlyContinue
    Update-Disk -Number $DiskNumber
    $python = (Get-Command python).Source
    Say "== $python flash_sd.py"
    $ErrorActionPreference = "Continue"
    & $python -u (Join-Path $PSScriptRoot "flash_sd.py") $Image $DiskNumber --sha256 $Sha256 |
        ForEach-Object { Say "$_" }
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    Update-Disk -Number $DiskNumber
    if ($rc -ne 0) { throw "flash_sd.py: code $rc" }
    Say "== done: the card can be removed"
    exit 0
}
catch {
    Say "ERROR: $_"
    if (-not $Force) { Read-Host "Enter — close" | Out-Null }
    exit 1
}
