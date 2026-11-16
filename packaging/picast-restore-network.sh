#!/usr/bin/bash
# ExecStopPost for picast.service: runs after the service exits for ANY reason
# (clean stop, crash, SIGKILL, OOM). Guarantees the radio goes back to the
# system network stack even when the Python shutdown path never ran.
#
# --no-block is mandatory: picast.service is ordered After= these units, so a
# blocking start issued while picast is still stopping would deadlock.
PID_FILE="/run/picast/wpa/wpa_supplicant.pid"
if [ -r "$PID_FILE" ]; then
    WPA_PID="$(tr -d '[:space:]' < "$PID_FILE")"
    if [[ "$WPA_PID" =~ ^[0-9]+$ ]] && kill -0 "$WPA_PID" 2>/dev/null; then
        kill "$WPA_PID" 2>/dev/null && sleep 1
        kill -9 "$WPA_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi
for pid in $(pgrep -x wpa_supplicant 2>/dev/null || true); do
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF '/run/picast/wpa/picast-wpa.conf'; then
        kill "$pid" 2>/dev/null && sleep 1 || true
        kill -9 "$pid" 2>/dev/null || true
    fi
done
for unit in wpa_supplicant NetworkManager; do
    systemctl is-active --quiet "$unit" ||
        systemctl --no-block --no-ask-password start "$unit"
done
exit 0
