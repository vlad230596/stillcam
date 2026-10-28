#!/bin/sh
# Installs the still service on the radxa-cam board. Run ON THE BOARD, as root,
# after copying cam-still, cam-still.service and this script to /tmp
# (docs/cam-still.md, "Deploy"). Touches neither mediamtx, nor rkaiq_3A, nor
# the sensor: the live stream keeps running.
set -e
cd "$(dirname "$0")"
for f in cam-still cam-still.service; do
    # CRLF from Windows breaks the shebang (docs/radxa-zero3.md, pitfall 5)
    if [ "$(tr -d '\r' < "$f" | wc -c)" != "$(wc -c < "$f")" ]; then
        echo "$f has CRLF line endings - refusing" >&2; exit 1
    fi
done
python3 -c "import ast; ast.parse(open('cam-still').read())"
install -m 755 cam-still /usr/local/bin/cam-still
install -m 644 cam-still.service /etc/systemd/system/cam-still.service
systemctl daemon-reload
systemctl enable cam-still
systemctl restart cam-still
sleep 1
systemctl --no-pager --lines=5 status cam-still
echo "rga count (must stay 5): $(dmesg | grep -c rga)"
