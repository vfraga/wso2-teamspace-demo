#!/bin/bash
set -e

echo "Starting Teamspace demo application..."

./.venv/bin/python3 --version || { echo "Python 3.10+ required"; exit 1; }

if [ "${DEV_MODE:-false}" = "true" ]; then
    RELOAD_FLAG="--reload"
    DEBUG_FLAG="--debug"
    RELOAD_EXCLUDES="--reload-exclude flask_session/* --reload-exclude *.db --reload-exclude *.db-*"
else
    RELOAD_FLAG=""
    DEBUG_FLAG=""
    RELOAD_EXCLUDES=""
fi

echo "Starting Business API on port 9091..."
./.venv/bin/python3 -m uvicorn api.main:app --host 0.0.0.0 --port 9091 --ws none $RELOAD_FLAG $RELOAD_EXCLUDES &
API_PID=$!
echo $API_PID > /tmp/teamspace_api.pid

for i in $(seq 1 30); do
    if curl --insecure --silent http://localhost:9091/health > /dev/null 2>&1; then
        echo "API healthy"
        break
    fi
    echo "Waiting for API... ($i/30)"
    sleep 1
done

echo "Starting AI Agent Service on port 8000..."
./.venv/bin/python3 -m uvicorn agent.main:app --host 0.0.0.0 --port 8000 --ws none $RELOAD_FLAG $RELOAD_EXCLUDES &
AGENT_PID=$!
echo $AGENT_PID > /tmp/teamspace_agent.pid

for i in $(seq 1 30); do
    if curl --insecure --silent http://localhost:8000/health > /dev/null 2>&1; then
        echo "Agent healthy"
        break
    fi
    echo "Waiting for agent... ($i/30)"
    sleep 1
done

echo "Starting Flask Web App on port 5001..."
FLASK_APP=webapp.app:create_app ./.venv/bin/python3 -m flask run --host 0.0.0.0 --port 5001 $DEBUG_FLAG &
WEBAPP_PID=$!
echo $WEBAPP_PID > /tmp/teamspace_webapp.pid

echo ""
echo "All services started:"
echo "  Web App:      http://localhost:5001"
echo "  Business API: http://localhost:9091"
echo "  AI Agent:     http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop all services."

cleanup() {
    for pid in $API_PID $AGENT_PID $WEBAPP_PID; do
        kill $pid 2>/dev/null
    done
    for i in $(seq 1 5); do
        if ! kill -0 $API_PID 2>/dev/null && ! kill -0 $AGENT_PID 2>/dev/null && ! kill -0 $WEBAPP_PID 2>/dev/null; then
            break
        fi
        sleep 1
    done
    for pid in $API_PID $AGENT_PID $WEBAPP_PID; do
        kill -9 $pid 2>/dev/null
    done
    rm -f /tmp/teamspace_*.pid
    exit
}
trap cleanup SIGINT SIGTERM
wait
