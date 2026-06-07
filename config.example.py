"""PowerMon configuration - TEMPLATE.

Copy this file to config.py and edit it for your setup:

    cp config.example.py config.py

config.py is gitignored, so your device addresses and paths stay local.
"""

# SQLite database file. The collector writes here; the dashboards read it.
# An SSD is a good home for it - the frequent small commits stay quiet and cause no
# mechanical wear. (On some spinning HDDs the head clicks audibly on each commit.)
# The file is created automatically.
DB = "powermon.db"

# Battery bank(s) to monitor: a list of (label, BLE MAC address).
#
# How to find your BMS address:
#   1. Close the JK BMS phone app - the BMS allows only ONE BLE client at a time.
#   2. Run:  python scan.py
#   3. Copy the address shown next to your BMS (the name you set in the app)
#      and paste it below, replacing the placeholder.
#
# A single Bluetooth adapter cannot reliably hold two BMS connections at once
# (their discovery sessions collide). For a second bank use a second adapter or
# an ESP32 bridge, then uncomment the second line.
BANKS = [
    ("bank1", "AA:BB:CC:DD:EE:FF"),
    # ("bank2", "11:22:33:44:55:66"),
]
