#!/bin/bash
# Resume the PowerMon collector after using the phone app (./stop.sh).
systemctl --user start powermon.service
echo "PowerMon collector STARTED — streaming Bank 1 to powermon.db (on SSD)."
systemctl --user is-active powermon.service || true
