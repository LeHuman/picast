#!/usr/bin/bash
# picast network lockout watchdog.
#
# picast's private wpa_supplicant mode stops NetworkManager. If the station is
# then not usable for >=90s (SSH dead, "lockout mode"), restore the system
# network stack — once per boot, never while a cast is running.
#
# Healthy = station associated (wpa_state=COMPLETED) AND a global IPv4 on wlan0.
# Both are required: picast re-applies the old lease statically, so an IPv4 can
# be present on a dead link, and an association can exist with no IPv4.
#
# State lives outside /run/picast: that is picast.service's RuntimeDirectory and
# is wiped by the `systemctl stop picast` below. Logging goes to the journal.
DIR=/run/picast-watchdog
STATE=$DIR/streak
TRIPPED=$DIR/tripped
mkdir -p "$DIR"

log() { logger -t picast-watchdog -- "$*"; }

# Not in own-supplicant mode: nothing to guard.
if ! pgrep -f 'picast-wpa.conf' >/dev/null 2>&1; then
    rm -f "$STATE"
    exit 0
fi

# Active cast: do not pull the radio out from under a running session.
if pgrep -x 'picast-player' >/dev/null 2>&1; then
    rm -f "$STATE"
    exit 0
fi

# Trip-once per boot: after restoring, do not flap.
[ -e "$TRIPPED" ] && exit 0

ASSOC=no
IPV4=no
wpa_cli -p /run/picast/wpa -i wlan0 status 2>/dev/null | grep -q '^wpa_state=COMPLETED' && ASSOC=yes
ip -4 -o addr show dev wlan0 scope global 2>/dev/null | grep -q 'inet ' && IPV4=yes

if [ "$ASSOC" = yes ] && [ "$IPV4" = yes ]; then
    rm -f "$STATE"
    exit 0
fi

STREAK=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$STREAK" > "$STATE"
log "station unhealthy (associated=$ASSOC ipv4=$IPV4) streak=$STREAK/3"

if [ "$STREAK" -ge 3 ]; then
    log "LOCKOUT detected: restoring system network stack"
    touch "$TRIPPED"
    rm -f "$STATE"
    systemctl stop picast >/dev/null 2>&1
    pkill -f 'picast-wpa.conf' 2>/dev/null
    sleep 2
    for i in $(iw dev 2>/dev/null | grep -oE 'p2p-wlan0-[0-9]+' | sort -u); do
        iw dev "$i" del 2>/dev/null
    done
    systemctl restart wpa_supplicant >/dev/null 2>&1
    sleep 1
    systemctl restart NetworkManager >/dev/null 2>&1
    log "system stack restored; picast left stopped until reboot or manual start"
fi
exit 0
