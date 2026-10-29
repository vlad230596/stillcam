#!/bin/bash
# Turns a freshly flashed Radxa ZERO 3E (official rsdk-b1 image,
# radxa-zero3_bookworm_kde_b1.output_512.img.xz) into a stillcam camera.
# Run ON THE BOARD as root from the bundle that push.ps1 copies to /tmp/cam-image
# (docs/image.md). Safe to run again: every step only brings the board to
# the target state.
#
#   provision.sh [--hostname NAME] [--fallback-ip 169.254.X.Y/16]
#                [--password-stdin] [--no-apt]
#
# Several cameras may share one network, so nothing here is common to all of
# them: the hostname defaults to radxa-cam-<tail of the SoC serial>, and there is
# no fixed address unless --fallback-ip gives one (pick a different one per
# camera). The client application addresses a camera by IP.
#
# --password-stdin reads the new password of user radxa from the first line of
# stdin. Network settings are written but not re-applied: the ssh session that
# runs this script stays up, the new profile takes effect after the reboot.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
NEW_HOSTNAME=
FALLBACK_IP=
READ_PASSWORD=0
APT=1

# Base image the bundle is made for; another one is a warning, not a stop.
EXPECTED_KERNEL=6.1.84-10-rk2410-nocsf
# MediaMTX v1.21.0 - the version that ran on the first camera since 2026-09-18.
MEDIAMTX_URL=https://github.com/bluenviron/mediamtx/releases/download/v1.21.0/mediamtx_v1.21.0_linux_arm64.tar.gz
MEDIAMTX_TAR_SHA256=a8113b5928ba1a934b81557b61b8a07954b76921a4b567d54c7f086f8b39d9a2
MEDIAMTX_BIN_SHA256=e47c58c398adbcaa2cbd545fdd908b9c2e36db4c7a4590fd2e746d3cbf28ae6b
# Kernel, boot loader and the camera/codec stack: an apt upgrade of any of these
# re-opens the RGA and 3A pitfalls (docs/radxa-zero3.md), so they are held.
HOLD_PACKAGES="linux-image-radxa-zero3 linux-image-$EXPECTED_KERNEL linux-headers-radxa-zero3
u-boot-radxa-zero3 u-boot-rk2410 camera-engine-rkaiq librga2 librockchip-mpp1
gstreamer1.0-rockchip1 libv4l-rkmpp radxa-overlays-dkms"
OVERLAYS="radxa-zero3-radxa-camera-8m radxa-zero3-poe-hat"
# Desktop, file sharing, printing, Bluetooth and auto-updates have no business on
# a camera that may live on a network without internet.
DISABLE_UNITS="sddm.service samba-ad-dc.service smbd.service nmbd.service cups.service
cups-browsed.service bluetooth.service ModemManager.service apt-daily.timer
apt-daily-upgrade.timer fwupd-refresh.timer man-db.timer"

while [ $# -gt 0 ]; do
    case "$1" in
        --hostname) NEW_HOSTNAME=$2; shift 2 ;;
        --fallback-ip) FALLBACK_IP=$2; shift 2 ;;
        --password-stdin) READ_PASSWORD=1; shift ;;
        --no-apt) APT=0; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

step() { echo; echo "=== $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
NEW_PASSWORD=
if [ $READ_PASSWORD = 1 ]; then
    IFS= read -r NEW_PASSWORD || true
    [ ${#NEW_PASSWORD} -ge 8 ] || die "new password shorter than 8 characters"
fi

step "bundle"
# CRLF from Windows breaks shebangs and silently corrupts configs (radxa-zero3.md, pitfall 5)
crlf=$(find "$HERE" -type f ! -path "$HERE/reference/*" ! -name mediamtx -exec grep -l $'\r' {} + || true)
[ -z "$crlf" ] || die "CRLF line endings in: $crlf"
for f in cam-panel cam-pipeline cam-still cam-still.service cam-stream.conf; do
    [ -f "$HERE/$f" ] || die "bundle has no $f"
done
python3 -c "import ast,sys; [ast.parse(open(p).read()) for p in sys.argv[1:]]" "$HERE/cam-panel" "$HERE/cam-still"
bash -n "$HERE/cam-pipeline"
echo "ok"

step "board"
model=$(tr -d '\0' < /proc/device-tree/model)
echo "model: $model, kernel: $(uname -r)"
case "$model" in *"ZERO 3"*) ;; *) die "not a Radxa ZERO 3: $model" ;; esac
[ "$(uname -r)" = "$EXPECTED_KERNEL" ] || echo "WARNING: kernel is not $EXPECTED_KERNEL - the RGA workaround was verified on that one only"
serial=$(tr -d '\0 ' < /proc/device-tree/serial-number)
[ -n "$serial" ] || die "no SoC serial in /proc/device-tree/serial-number"
[ -n "$NEW_HOSTNAME" ] || NEW_HOSTNAME="radxa-cam-${serial: -6}"
echo "serial: $serial, hostname: $NEW_HOSTNAME"

step "packages"
# No "cmd | grep -q" under pipefail: grep exits early, cmd dies of SIGPIPE, the test lies.
installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }
holds=
for p in $HOLD_PACKAGES; do installed "$p" && holds="$holds $p"; done
[ -z "$holds" ] || apt-mark hold $holds
if [ $APT = 1 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        gstreamer1.0-tools gstreamer1.0-rtsp gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-libav v4l-utils lm-sensors python3 python3-numpy avahi-daemon curl
fi
for b in gst-launch-1.0 v4l2-ctl media-ctl python3 sensors avahi-daemon; do
    command -v "$b" >/dev/null || die "$b not installed (run without --no-apt)"
done
gst-inspect-1.0 mpph264enc >/dev/null 2>&1 || die "no mpph264enc: gstreamer1.0-rockchip1 missing from the base image"
gst-inspect-1.0 rtspclientsink >/dev/null 2>&1 || die "no rtspclientsink (gstreamer1.0-rtsp)"
gst-inspect-1.0 avdec_h264 >/dev/null 2>&1 || echo "WARNING: no avdec_h264 - host/snap.ps1 (frame from RTSP) will not work"

step "mediamtx"
if [ -f "$HERE/mediamtx" ]; then
    bin="$HERE/mediamtx"
else
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/mediamtx.tar.gz" "$MEDIAMTX_URL"
    echo "$MEDIAMTX_TAR_SHA256  $tmp/mediamtx.tar.gz" | sha256sum -c -
    tar -xzf "$tmp/mediamtx.tar.gz" -C "$tmp" mediamtx
    bin="$tmp/mediamtx"
fi
echo "$MEDIAMTX_BIN_SHA256  $bin" | sha256sum -c -
install -m 755 "$bin" /usr/local/bin/mediamtx

step "camera files"
install -m 755 "$HERE/cam-panel" "$HERE/cam-pipeline" "$HERE/cam-still" /usr/local/bin/
install -m 644 "$HERE/cam-still.service" /etc/systemd/system/cam-still.service
install -m 644 "$HERE/cam-stream.conf" /usr/local/etc/cam-stream.conf
(cd "$HERE/rootfs" && find . -type f) | while read -r rel; do
    case "$rel" in ./usr/local/bin/*) mode=755 ;; *) mode=644 ;; esac
    install -D -m "$mode" "$HERE/rootfs/$rel" "/${rel#./}"
    echo "  /${rel#./}"
done

step "device tree overlays"
for o in $OVERLAYS; do
    if [ -f "/boot/dtbo/$o.dtbo.disabled" ]; then mv "/boot/dtbo/$o.dtbo.disabled" "/boot/dtbo/$o.dtbo"; fi
    [ -f "/boot/dtbo/$o.dtbo" ] || die "overlay $o not found in /boot/dtbo"
done
u-boot-update
for o in $OVERLAYS; do
    grep -q "$o.dtbo" /boot/extlinux/extlinux.conf || die "u-boot-update did not add $o to extlinux.conf"
done
echo "ok: $OVERLAYS"

step "name and network"
hostnamectl set-hostname "$NEW_HOSTNAME"
if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $NEW_HOSTNAME/" /etc/hosts
else
    echo "127.0.1.1 $NEW_HOSTNAME" >> /etc/hosts
fi
# mDNS (finding a camera's IP by its name): answer with IPv4 only. The camera
# services listen on 0.0.0.0, and with an AAAA record Windows first tries IPv6,
# gets refused and loses ~2 s per new connection by name (measured 2026-10-27:
# 2.2 s against 0.08 s by IP).
sed -i 's/^#\?use-ipv6=.*/use-ipv6=no/; s/^#\?publish-aaaa-on-ipv4=.*/publish-aaaa-on-ipv4=no/' /etc/avahi/avahi-daemon.conf
grep -q '^use-ipv6=no' /etc/avahi/avahi-daemon.conf || die "avahi use-ipv6=no not set"
# One wired profile: DHCP, plus a fixed link-local address for a direct cable into
# a PC without DHCP only if asked. Modified in place, not re-created: the profile
# in use keeps carrying this ssh session until the reboot.
con=$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')
if [ -z "$con" ]; then
    nmcli connection add type ethernet con-name cam-eth ifname end1 autoconnect yes
    con=cam-eth
fi
nmcli connection modify "$con" connection.id cam-eth connection.interface-name end1 \
    connection.autoconnect yes ipv4.method auto ipv4.addresses "$FALLBACK_IP" \
    ipv4.may-fail yes ipv4.dhcp-timeout 2147483647 ipv6.method auto
nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet" && $1!="cam-eth"{print "  extra wired profile left as is: " $1}'
nmcli -f connection.id,ipv4.method,ipv4.addresses,ipv4.dhcp-timeout connection show cam-eth

step "user radxa"
if [ -n "$NEW_PASSWORD" ]; then
    printf 'radxa:%s\n' "$NEW_PASSWORD" | chpasswd
    echo "password changed"
fi
if [ -f "$HERE/authorized_keys" ]; then
    install -d -m 700 -o radxa -g radxa /home/radxa/.ssh
    install -m 600 -o radxa -g radxa "$HERE/authorized_keys" /home/radxa/.ssh/authorized_keys
    echo "authorized_keys: $(wc -l < /home/radxa/.ssh/authorized_keys) key(s)"
fi

step "services"
for u in $DISABLE_UNITS; do
    if [ -n "$(systemctl list-unit-files "$u" --no-legend 2>/dev/null)" ]; then
        systemctl disable --now "$u" >/dev/null 2>&1 || true
        echo "  disabled $u"
    fi
done
# No AIC8800 Wi-Fi/BT chip on the ZERO 3E: the reset unit only fails at every boot.
systemctl mask rsetup-aic8800-reset@ttyS1.service >/dev/null 2>&1 || true
systemctl set-default multi-user.target
systemctl daemon-reload
systemctl enable cam-panel.service cam-blackbox.service cam-still.service avahi-daemon.service ssh.service
# The stream does not start at boot: a hang in the camera path must leave the
# next boot alive (docs/radxa-zero3.md). rsetup re-applies this on every boot.
systemctl disable mediamtx.service >/dev/null 2>&1 || true
if [ -f /config/config.txt ]; then
    grep -q '^disable_service mediamtx' /config/config.txt || printf '\ndisable_service mediamtx\n' >> /config/config.txt
else
    echo "WARNING: no /config/config.txt - the boot-time stream policy is only the disabled unit"
fi
systemctl restart avahi-daemon cam-panel cam-blackbox cam-still

step "summary"
sleep 2
for u in cam-panel cam-still cam-blackbox avahi-daemon ssh rkaiq_3A mediamtx; do
    printf '  %-14s %-9s %s\n' "$u" "$(systemctl is-enabled "$u" 2>/dev/null)" "$(systemctl is-active "$u" 2>/dev/null)"
done
sha256sum /usr/local/bin/cam-panel /usr/local/bin/cam-pipeline /usr/local/bin/cam-still /usr/local/bin/mediamtx
echo "rga count (must stay 5): $(dmesg | grep -c rga)"
echo "hostname: $NEW_HOSTNAME  (after reboot: $NEW_HOSTNAME.local; its IP goes into the client configuration)"
echo "done - reboot to apply overlays and network: systemctl reboot"
