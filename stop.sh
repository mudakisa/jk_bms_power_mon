#!/bin/bash
# Pause the PowerMon collector so the JK phone app can connect to a bank
# (JK-BMS allows only ONE BLE client at a time). Resume with ./start.sh
systemctl --user stop powermon.service
# The collector handles SIGTERM and disconnects cleanly, so no stale BLE link is
# left behind (it used to need a forced bluetoothctl disconnect here).
echo "PowerMon collector STOPPED - BLE link freed, JK phone app can connect now."
systemctl --user is-active powermon.service || true
