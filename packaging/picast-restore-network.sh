#!/usr/bin/bash
# ExecStopPost for picast.service: runs after the service exits for ANY reason
# (clean stop, crash, SIGKILL, OOM). Guarantees the radio goes back to the
# system network stack even when the Python shutdown path never ran.
#
# --no-block is mandatory: picast.service is ordered After= these units, so a
# blocking start issued while picast is still stopping would deadlock.
pkill -f 'picast-wpa.conf' 2>/dev/null && sleep 1
pkill -9 -f 'picast-wpa.conf' 2>/dev/null
for unit in wpa_supplicant NetworkManager; do
    systemctl is-active --quiet "$unit" ||
        systemctl --no-block --no-ask-password start "$unit"
done
exit 0
