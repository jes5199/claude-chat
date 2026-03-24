#!/bin/bash
# Kill existing relay processes and start fresh
pkill -f "python.*relay.py" 2>/dev/null
sleep 2
pkill -9 -f "python.*relay.py" 2>/dev/null
sleep 1
nohup /home/jes/claude-chat/.venv/bin/python3 /home/jes/claude-chat/relay.py >> /tmp/claude-chat/relay.log 2>&1 &
echo "$(date): Relay restarted, PID $!" >> /tmp/claude-chat/restart.log
