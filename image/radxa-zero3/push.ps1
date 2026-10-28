<#
.SYNOPSIS
  Sets up a freshly flashed Radxa ZERO 3E as a camera: key, file bundle, provision.sh.

.DESCRIPTION
  The board runs the official rsdk-b1 image (docs/image.md) and is on the same network as this machine.
  Questions in order: fallback address number, current radxa password, new password twice — all before the first
  contact with the board; then ssh itself asks for the password once more (the same current one) while uploading the key, after that everything
  uses the key. Passwords never get into the repository: they are entered here and go to the board inside a base64 script.

.EXAMPLE
  powershell -File image\radxa-zero3\push.ps1
  powershell -File image\radxa-zero3\push.ps1 -CamHost 192.168.1.50 -FallbackOctet 51 -Hostname radxa-cam-2 -Reboot
#>
param(
    # A fresh image is named radxa-zero3; an address from the DHCP lease also works.
    [string] $CamHost = "radxa-zero3.local",
    # Empty — radxa-cam-<tail of the SoC serial>: there may be several cameras on one network.
    [string] $Hostname = "",
    # Fallback address for a direct cable — 169.254.176.<N>/16, unique per camera: first 50, second 51…
    # Not set — asked for.
    [int] $FallbackOctet = 0,
    [string] $Key = "$env:USERPROFILE\.ssh\radxa_cam",
    # Local MediaMTX v1.21.0 linux/arm64 binary, if the board has no internet (provision.sh verifies the hash).
    [string] $MediaMtx = "",
    [switch] $NoApt,
    [switch] $KeepPassword,
    [switch] $Reboot
)
$ErrorActionPreference = "Stop"

# Only the Windows system OpenSSH (like host/snap.ps1).
$openssh = Join-Path $env:SystemRoot "System32\OpenSSH"
$sshExe = Join-Path $openssh "ssh.exe"
$scpExe = Join-Path $openssh "scp.exe"
$camDir = Join-Path $PSScriptRoot "../../node"
if (-not (Test-Path "$Key.pub")) { throw "key $Key.pub not found" }

# A fresh board has new host keys, and the name or address may have belonged to a previous one — a separate known_hosts per run.
$knownHosts = Join-Path $env:TEMP "cam-provision-known_hosts"
Remove-Item $knownHosts -ErrorAction SilentlyContinue
$opts = @("-o", "UserKnownHostsFile=$knownHosts", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")
$target = "radxa@$CamHost"

function Read-Plain([string] $Prompt) {
    $s = Read-Host -AsSecureString $Prompt
    $p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }
}
function Quote([string] $s) { "'" + $s.Replace("'", "'\''") + "'" }
function Invoke-Remote([string[]] $SshArgs, [string] $Script) {
    # LF and base64: PowerShell 5.1 appends CR to whatever it feeds to an external program's stdin.
    # The script goes as a file, not via bash stdin: otherwise the script's stdin would go to the commands inside it.
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script.Replace("`r`n", "`n")))
    & $sshExe @SshArgs "f=`$(mktemp) && echo $b64 | base64 -d > `$f && bash `$f; rc=`$?; rm -f `$f; exit `$rc"
    if ($LASTEXITCODE -ne 0) { throw "error on the board (code $LASTEXITCODE)" }
}

if ($FallbackOctet -eq 0) {
    $FallbackOctet = [int](Read-Host "Fallback address 169.254.176.N — N (first camera 50, second 51, …; unique per camera)")
}
if ($FallbackOctet -lt 1 -or $FallbackOctet -gt 254) { throw "N outside 1…254: $FallbackOctet" }
$FallbackIp = "169.254.176.$FallbackOctet/16"

$current = Read-Plain "Current radxa password on the board (radxa on a fresh image)"
$new = ""
if (-not $KeepPassword) {
    $new = Read-Plain "New radxa password (at least 8 characters)"
    if ($new.Length -lt 8) { throw "new password is shorter than 8 characters" }
    if ($new -ne (Read-Plain "New password again")) { throw "passwords do not match" }
}

# 1. Key — with the password (ssh asks for it itself), after that key only.
Write-Host "== key $Key.pub -> $target"
$pub = (Get-Content "$Key.pub" -Raw).Trim()
Invoke-Remote ($opts + @($target)) "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF $(Quote $pub) ~/.ssh/authorized_keys 2>/dev/null || echo $(Quote $pub) >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
$keyed = $opts + @("-i", $Key, "-o", "BatchMode=yes")

# 2. Bundle: provision.sh, rootfs, service files from node/, key.
$stage = Join-Path $env:TEMP "cam-image"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
Copy-Item (Join-Path $PSScriptRoot "provision.sh") $stage
Copy-Item (Join-Path $PSScriptRoot "rootfs") $stage -Recurse
foreach ($f in "cam-panel", "cam-pipeline", "cam-still", "cam-still.service", "cam-stream.conf") { Copy-Item (Join-Path $camDir $f) $stage }
[IO.File]::WriteAllText((Join-Path $stage "authorized_keys"), "$pub`n")
if ($MediaMtx) { Copy-Item $MediaMtx (Join-Path $stage "mediamtx") }
Write-Host "== bundle -> ${target}:/tmp/cam-image"
Invoke-Remote $keyed "rm -rf /tmp/cam-image"
& $scpExe @keyed -r -q $stage "${target}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "scp (code $LASTEXITCODE)" }

# 3. provision.sh under sudo: the first stdin line is the sudo password, the second the new radxa password.
$flags = ""
if ($Hostname) { $flags += " --hostname $(Quote $Hostname)" }
$flags += " --fallback-ip $(Quote $FallbackIp)"
if ($NoApt) { $flags += " --no-apt" }
$lines = "$(Quote $current)"
if ($new) { $flags += " --password-stdin"; $lines += " $(Quote $new)" }
$run = "printf '%s\n' $lines | sudo -S -p '' bash /tmp/cam-image/provision.sh $flags`nrc=`$?`nrm -rf /tmp/cam-image`n"
if ($Reboot) {
    # The password is already the new one. Reboot via a timer in 3 s, so that ssh has time to close on its own.
    $sudoPass = if ($new) { $new } else { $current }
    $run += "[ `$rc = 0 ] && printf '%s\n' $(Quote $sudoPass) | sudo -S -p '' systemd-run --on-active=3 systemctl reboot && echo 'reboot in 3 s'`n"
}
$run += "exit `$rc`n"
Write-Host "== provision.sh $flags"
Invoke-Remote $keyed $run
Write-Host "Done. The camera name and address are in the provision.sh output above; put the IP into the client configuration."
