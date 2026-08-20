#!/bin/bash
set -e

echo "Starting Teamspace demo application..."

./.venv/bin/python3 --version || { echo "Python 3.10+ required"; exit 1; }

# SERVE_MODE=production serves the three services behind gunicorn instead of
# the Flask/uvicorn development servers. The default is unchanged so the README
# quickstart and DEV_MODE=true hot reload behave exactly as before.
SERVE_MODE="${SERVE_MODE:-development}"

if [ "${DEV_MODE:-false}" = "true" ]; then
    RELOAD_FLAG="--reload"
    DEBUG_FLAG="--debug"
    RELOAD_EXCLUDES="--reload-exclude flask_session/* --reload-exclude *.db --reload-exclude *.db-*"
else
    RELOAD_FLAG=""
    DEBUG_FLAG=""
    RELOAD_EXCLUDES=""
fi

if [ "$SERVE_MODE" = "production" ]; then
    API_WORKERS="${API_WORKERS:-4}"
    AGENT_WORKERS="${AGENT_WORKERS:-4}"
    WEBAPP_WORKERS="${WEBAPP_WORKERS:-4}"

    # The agent keeps per-thread OBO state (PKCE verifier, tokens, chat
    # history). Without REDIS_URL that state is per-process, so /authorize and
    # /callback can land on different workers and the OBO flow breaks. Refuse
    # to start multi-worker rather than fail intermittently at consent time.
    if [ -z "${REDIS_URL:-}" ] && [ "$AGENT_WORKERS" -gt 1 ]; then
        echo "ERROR: SERVE_MODE=production with AGENT_WORKERS=$AGENT_WORKERS but REDIS_URL is unset."
        echo "       The agent's OBO state would not be shared between workers, so the"
        echo "       consent callback would intermittently fail."
        echo "       Set REDIS_URL (see 'Agent State Store' in the README), or set"
        echo "       AGENT_WORKERS=1 to accept a single-instance agent."
        exit 1
    fi
    echo "Serving in PRODUCTION mode (gunicorn)."
else
    echo "Serving in DEVELOPMENT mode (flask run + single-process uvicorn)."
    echo "Set SERVE_MODE=production to serve behind gunicorn."
fi

wait_for_health() {
    local url="$1" name="$2"
    for i in $(seq 1 30); do
        if curl --insecure --silent "$url" > /dev/null 2>&1; then
            echo "$name healthy"
            return 0
        fi
        echo "Waiting for $name... ($i/30)"
        sleep 1
    done
    echo "WARNING: $name did not become healthy in 30s"
}

echo "Starting Business API on port 9091..."
if [ "$SERVE_MODE" = "production" ]; then
    ./.venv/bin/python3 -m gunicorn api.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "$API_WORKERS" --bind 0.0.0.0:9091 &
else
    ./.venv/bin/python3 -m uvicorn api.main:app --host 0.0.0.0 --port 9091 --ws none $RELOAD_FLAG $RELOAD_EXCLUDES &
fi
API_PID=$!
echo $API_PID > /tmp/teamspace_api.pid
wait_for_health http://localhost:9091/health "API"

echo "Starting AI Agent Service on port 8000..."
if [ "$SERVE_MODE" = "production" ]; then
    ./.venv/bin/python3 -m gunicorn agent.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "$AGENT_WORKERS" --bind 0.0.0.0:8000 &
else
    ./.venv/bin/python3 -m uvicorn agent.main:app --host 0.0.0.0 --port 8000 --ws none $RELOAD_FLAG $RELOAD_EXCLUDES &
fi
AGENT_PID=$!
echo $AGENT_PID > /tmp/teamspace_agent.pid
wait_for_health http://localhost:8000/health "agent"

echo "Starting Flask Web App on port 5001..."
if [ "$SERVE_MODE" = "production" ]; then
    ./.venv/bin/python3 -m gunicorn "webapp.app:create_app()" \
        --workers "$WEBAPP_WORKERS" --bind 0.0.0.0:5001 &
else
    FLASK_APP=webapp.app:create_app ./.venv/bin/python3 -m flask run --host 0.0.0.0 --port 5001 $DEBUG_FLAG &
fi
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
