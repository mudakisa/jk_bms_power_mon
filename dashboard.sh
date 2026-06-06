#!/bin/bash
# Open the PowerMon web dashboard. Starts the web service if it isn't running.
# (Grafana, if you prefer it, lives at http://localhost:3000)
URL="http://localhost:8080"

systemctl --user start powermon-web.service 2>/dev/null
sleep 1
echo "PowerMon dashboard → $URL  (web service: $(systemctl --user is-active powermon-web.service))"

# desktop notification with the address (so it's there to copy/click)
command -v notify-send >/dev/null 2>&1 && \
    notify-send -i battery "PowerMon dashboard" "Running at $URL" 2>/dev/null

if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
else
    echo "xdg-open not found — open $URL in a browser manually."
fi
