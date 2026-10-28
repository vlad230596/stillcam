<#
.SYNOPSIS
  Grabs a frame from the camera (Radxa ZERO 3E, docs/radxa-zero3.md) and puts
  the JPEG on the workstation.

.DESCRIPTION
  No ffmpeg is needed on the workstation: the frame is pulled from the local RTSP right on
  the board via GStreamer (software decoder avdec_h264 — the hardware mpph264dec must not
  be touched, it pulls in RGA, see "Pitfalls" in docs/radxa-zero3.md), then
  fetched over scp.

  -Still is a different path: a 3280x2464 measurement still from raw frames of the ISP main path
  over TCP, processed on this machine (host/still.py). The live stream is not
  interrupted.

  By policy the stream does not start on its own; if mediamtx is not running, the script starts it.

.EXAMPLE
  pwsh host/snap.ps1 -Out shots/rect.jpg
  pwsh host/snap.ps1 -Stack 120 -Out shots/light.jpg
  pwsh host/snap.ps1 -Exposure 400 -ManualExposure
  pwsh host/snap.ps1 -Still -Out shots/still

.NOTES
  Auto exposure is switched through the service panel on the board: it interrupts
  the stream for a few seconds because the 3A service must not be touched while running. If
  3A is already in the requested state, the panel does nothing and leaves the stream alone.
#>
[CmdletBinding()]
param(
    [string] $Out = "cam-snap.jpg",
    # Camera address: default — the RADXA_HOST environment variable; one of the two is required.
    [string] $CamHost = $env:RADXA_HOST,
    [string] $Key = "$env:USERPROFILE\.ssh\radxa_cam",
    # Which decoded frame to save: the first frames after connecting
    # to RTSP arrive with artefacts (stack.py skips 3). 0 — the old way: pull
    # the stream for the whole -Seconds and take the last frame.
    [int]    $SkipFrames = 8,
    # Upper bound for waiting for a frame, s (before 2026-10-12 it always waited this long).
    [int]    $Seconds = 12,
    # Stack N frames by maximum (lighten). Needed when a moving light spot traces an outline
    # faster than the camera captures a frame: one frame shows only part of the outline.
    [int]    $Stack = 0,
    # Manual exposure: stops rkaiq_3A and sets the value (0..4095).
    [int]    $Exposure = 0,
    [switch] $ManualExposure,
    # Restore auto exposure (start rkaiq_3A) and exit.
    [switch] $AutoExposure,
    # Measurement still (host/still.py): full 3280x2464 frame straight from the
    # ISP (main path), no H.264, averaged over -Frames frames. The live stream runs
    # on the self path and is not interrupted, but keeps the still's exposure/gain.
    # -Out sets the prefix: <Out>.png, <Out>-y16.png, <Out>.txt. -StillArgs —
    # extra still.py arguments, e.g. "--reduce max".
    [switch] $Still,
    [int]    $Frames = 16,
    [string] $StillArgs = "",
    # Workstation Python with numpy — the still is processed with it.
    [string] $Python = "python"
)

$ErrorActionPreference = "Stop"
if (-not $CamHost) { throw "camera address: -CamHost or the RADXA_HOST environment variable" }
# Only the Windows system OpenSSH. Git's ssh (first in PATH when the script is
# started from bash/Python) parses an argument with a pipeline differently: the remote
# side ran a lone "echo", no still was taken, exit code 0 — and scp silently
# fetched the previous still. Verified 2026-09-23.
$openssh = Join-Path $env:SystemRoot "System32\OpenSSH"
$sshExe = Join-Path $openssh "ssh.exe"
$scpExe = Join-Path $openssh "scp.exe"
if (-not (Test-Path $sshExe)) { throw "$sshExe not found — the Windows OpenSSH Client component is required" }
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "radxa@$CamHost")
# The board's sudo password is not stored in the repository: it comes from the environment
# variable RADXA_SUDO_PASS (see docs/radxa-zero3.md, "Access"). Needed only by the branches that
# start mediamtx; -Still passes it on to still.py through the environment.
function Get-SudoPrefix {
    $pass = $env:RADXA_SUDO_PASS
    if ([string]::IsNullOrEmpty($pass)) {
        throw "environment variable RADXA_SUDO_PASS is not set — sudo password of user radxa on the camera (docs/radxa-zero3.md, section Access)"
    }
    $q = "'" + ($pass -replace "'", "'\''") + "'"
    return "SUDO() { printf '%s\n' $q | sudo -S -p '' `"`$@`"; }"
}

function Invoke-Cam([string] $Script) {
    # The script is shipped to the board in base64: otherwise quotes inside it do not survive
    # the double escaping PowerShell -> ssh.exe -> remote bash. bash
    # starts only after the script has been decoded completely — with
    # broken input its first half must not get a chance to run.
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Script -replace "`r`n", "`n")))
    $r = & $sshExe @ssh "f=`$(mktemp) && echo $b64 | base64 -d > `$f && bash `$f; rc=`$?; rm -f `$f; exit `$rc"
    if ($LASTEXITCODE -ne 0) { throw "ssh (code $LASTEXITCODE): $r" }
    return $r
}

# Through the panel, not systemctl directly: the 3A service must not be switched while
# the pipeline is running — the picture turns black-and-white and never
# comes back. The panel interrupts the stream for the switch and restores the
# exposure itself. Details — "Pitfalls", item 3 in docs/radxa-zero3.md.
function Set-AutoExposure([bool] $On) {
    $body = if ($On) { '{"on":true}' } else { '{"on":false}' }
    Invoke-Cam "curl -sS -X POST -H 'Content-Type: application/json' -d '$body' http://127.0.0.1:8080/api/autoexposure"
}

if ($AutoExposure) {
    Set-AutoExposure $true | Out-Null
    Write-Host "Auto exposure enabled."
    return
}

if ($ManualExposure -or $Exposure -gt 0) {
    $e = if ($Exposure -gt 0) { $Exposure } else { 1575 }
    Set-AutoExposure $false | Out-Null
    Invoke-Cam "v4l2-ctl -d /dev/video0 --set-ctrl=exposure=$e" | Out-Null
    Write-Host "Manual exposure: $e"
}

if ($Still) {
    # Everything is done by host/still.py on THIS machine: it goes over ssh to
    # the board itself, the board delivers raw NV12 frames from the ISP main path (/dev/video0) over TCP, and
    # averaging and PNG are computed here (on the board this took ~20 s out of 26).
    # The live stream runs on the self path (/dev/video1) in parallel: mediamtx is not
    # touched. It is stopped only if the stream itself sits on /dev/video0
    # (mode wider than 1920, the self path cannot handle it, see cam-pipeline) — that is also
    # inside still.py.
    # Auto exposure is not needed here and would get in the way — with 3A running a frame cannot be taken
    # without restarting it, so manual mode is required.
    # The still's exposure/gain are NOT restored afterwards — the user
    # asked for this: the live stream shows which parameters the still was taken with.
    $base = $Out -replace '\.(png|jpe?g)$', ''
    foreach ($s in ".png", "-y16.png", ".txt") {
        # so that a failed still does not leave the previous one in place
        Remove-Item -LiteralPath "$base$s" -ErrorAction SilentlyContinue
    }
    $extra = @()
    if ($StillArgs.Trim()) { $extra = $StillArgs.Trim() -split '\s+' }
    $pyArgs = @((Join-Path $PSScriptRoot "still.py"), "-n", $Frames, "--out", $base,
              "--host", $CamHost, "--key", $Key) + $extra
    # still.py writes stdout in UTF-8; without this PowerShell 5.1 reads it in the OEM code page
    $enc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
    try { $r = & $Python @pyArgs } finally { try { [Console]::OutputEncoding = $enc } catch {} }
    $rc = $LASTEXITCODE
    Write-Host ($r -join "`n")
    # still.py prints a line of capture parameters; without it there is no still, whatever
    # the exit code says.
    if ($rc -ne 0 -or -not ($r -match "n=$Frames exposure=")) { throw "still not taken (still.py, code $rc): $r" }
    foreach ($s in ".png", "-y16.png", ".txt") {
        if (-not (Test-Path -LiteralPath "$base$s")) { throw "still.py did not write $base$s" }
    }
    Write-Host "Still: $base.png, $base-y16.png"
    if (($r -join "`n") -match "exposure=(\d+) again=(\d+) dgain=(\d+)") {
        Write-Host ("Live stream left with the still parameters: exposure={0} analogue_gain={1} gain={2}" -f $Matches[1], $Matches[2], $Matches[3])
    }
    return
}

# The stream intentionally does not start at boot — start it when needed.
Invoke-Cam "$(Get-SudoPrefix); systemctl is-active --quiet mediamtx || { SUDO systemctl start mediamtx; sleep 4; }" | Out-Null

if ($Stack -gt 0) {
    # stack.py reads raw RGB from gst-launch and accumulates the maximum; there is no PIL on the board,
    # so encoding back to JPEG is done with the same gstreamer via rawvideoparse.
    $py = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot "../node/stack.py")))
    $grab = @"
echo $py | base64 -d > /tmp/stack.py
rm -f /tmp/acc.rgb /tmp/cam-snap.jpg
python3 /tmp/stack.py $Stack || exit 1
gst-launch-1.0 -q filesrc location=/tmp/acc.rgb \
  ! rawvideoparse width=1920 height=1088 format=rgb framerate=1/1 \
  ! videoconvert ! jpegenc quality=95 ! filesink location=/tmp/cam-snap.jpg >/dev/null 2>&1
test -s /tmp/cam-snap.jpg
"@
} else {
    # Frames are written by number (the last two); as soon as frame -SkipFrames + 1 appears,
    # frame -SkipFrames is complete — take it and stop the stream. Previously it always waited the full -Seconds.
    $want = "{0:D5}" -f $SkipFrames
    $next = "{0:D5}" -f ($SkipFrames + 1)
    $grab = @"
rm -f /tmp/cam-snap.jpg /tmp/cam-snap-*.jpg
timeout $Seconds gst-launch-1.0 -q \
  rtspsrc location=rtsp://127.0.0.1:8554/cam protocols=tcp latency=100 \
  ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert \
  ! jpegenc quality=92 ! multifilesink location=/tmp/cam-snap-%05d.jpg max-files=2 >/dev/null 2>&1 &
pid=`$!
if [ $SkipFrames -gt 0 ]; then
  while kill -0 `$pid 2>/dev/null && [ ! -e /tmp/cam-snap-$next.jpg ]; do sleep 0.1; done
  kill `$pid 2>/dev/null
fi
wait `$pid 2>/dev/null
if [ -e /tmp/cam-snap-$next.jpg ]; then f=/tmp/cam-snap-$want.jpg; else f=`$(ls -1 /tmp/cam-snap-*.jpg 2>/dev/null | tail -n 1); fi
test -n "`$f" && test -s "`$f" && mv "`$f" /tmp/cam-snap.jpg; rm -f /tmp/cam-snap-*.jpg
test -s /tmp/cam-snap.jpg
"@
}
Write-Host (Invoke-Cam $grab)

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
& $scpExe -i $Key -o StrictHostKeyChecking=no "radxa@${CamHost}:/tmp/cam-snap.jpg" $Out
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
Write-Host "Frame: $Out ($((Get-Item $Out).Length) bytes)"
