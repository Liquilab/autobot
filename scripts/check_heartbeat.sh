#!/bin/bash
# Heartbeat checker for autobot systemd timer
# Checks if the bot's heartbeat is stale (>10 min old) and restarts if needed

HEARTBEAT_FILE="/opt/autobot/data/heartbeat.json"
MAX_AGE_SECONDS=600  # 10 minutes

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "No heartbeat file found, restarting autobot..."
    systemctl restart autobot
    exit 0
fi

# Get timestamp from heartbeat file
HEARTBEAT_TIME=$(python3 -c "
import json, sys
from datetime import datetime, timezone
try:
    with open('$HEARTBEAT_FILE') as f:
        hb = json.load(f)
    ts = datetime.fromisoformat(hb['timestamp'])
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    print(int(age))
except Exception as e:
    print(99999)
")

if [ "$HEARTBEAT_TIME" -gt "$MAX_AGE_SECONDS" ]; then
    echo "Heartbeat stale (${HEARTBEAT_TIME}s old), restarting autobot..."
    systemctl restart autobot
else
    echo "Heartbeat OK (${HEARTBEAT_TIME}s old)"
fi
