#!/bin/bash
echo "Stopping Teamspace services..."

for svc in api agent webapp; do
    pid_file="/tmp/teamspace_${svc}.pid"
    if [ -f "$pid_file" ]; then
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "${svc} (PID $pid) stopped."
        else
            echo "${svc} was not running."
        fi
        rm -f "$pid_file"
    else
        echo "${svc} PID file not found."
    fi
done

pkill -f "uvicorn api.main:app" 2>/dev/null || true
pkill -f "uvicorn agent.main:app" 2>/dev/null || true
pkill -f "flask run" 2>/dev/null || true

echo "Done."
