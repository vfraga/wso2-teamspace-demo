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

# FLASK_PORT is read by webapp/config.py to build PORTAL_URL, and therefore the
# OIDC redirect URI registered by setup_is.py. Binding a different port here
# than the app believes it is on produces "callback.not.match" at login, so the
# one variable drives both.
#
# API_PORT/AGENT_PORT have no such counterpart: nothing derives a bind port from
# BUSINESS_API_URL / AGENT_SERVICE_URL, so changing either of these means
# updating that URL in .env too.
FLASK_HOST="${FLASK_HOST:-localhost}"
FLASK_PORT="${FLASK_PORT:-5001}"
API_PORT="${API_PORT:-9091}"
AGENT_PORT="${AGENT_PORT:-8000}"

# TLS is opt-in: point TLS_CERT_DIR at the generated PKI material (pki/out)
# and every service is served over HTTPS instead of plain HTTP. Left unset,
# the quickstart in the README works with no certificates at all.
#
# Layout is whatever pki/generate.sh produces:
#   $TLS_CERT_DIR/<name>/<name>.fullchain.pem  +  <name>.key
TLS_CERT_DIR="${TLS_CERT_DIR:-}"

tls_cert() { echo "$TLS_CERT_DIR/$1/$1.fullchain.pem"; }
tls_key() { echo "$TLS_CERT_DIR/$1/$1.key"; }

if [ -n "$TLS_CERT_DIR" ]; then
    for _name in businessapi aiagent flaskapp; do
        if [ ! -f "$(tls_cert "$_name")" ] || [ ! -f "$(tls_key "$_name")" ]; then
            echo "ERROR: TLS_CERT_DIR=$TLS_CERT_DIR but no certificate for '$_name'."
            echo "       Expected $(tls_cert "$_name")"
            echo "       Run ./pki/generate.sh first, or unset TLS_CERT_DIR to serve over HTTP."
            exit 1
        fi
    done
    SCHEME="https"
    echo "TLS enabled; serving HTTPS with certificates from $TLS_CERT_DIR."
else
    SCHEME="http"
    echo "TLS disabled (TLS_CERT_DIR unset); serving plain HTTP."
fi

# gunicorn and uvicorn spell the same thing differently.
gunicorn_tls_args() {
    [ -n "$TLS_CERT_DIR" ] || return 0
    echo "--certfile=$(tls_cert "$1") --keyfile=$(tls_key "$1")"
}
uvicorn_tls_args() {
    [ -n "$TLS_CERT_DIR" ] || return 0
    echo "--ssl-certfile $(tls_cert "$1") --ssl-keyfile $(tls_key "$1")"
}
flask_tls_args() {
    [ -n "$TLS_CERT_DIR" ] || return 0
    echo "--cert=$(tls_cert "$1") --key=$(tls_key "$1")"
}

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

echo "Starting Business API on port $API_PORT..."
if [ "$SERVE_MODE" = "production" ]; then
    ./.venv/bin/python3 -m gunicorn api.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "$API_WORKERS" --bind "0.0.0.0:$API_PORT" $(gunicorn_tls_args businessapi) &
else
    ./.venv/bin/python3 -m uvicorn api.main:app --host 0.0.0.0 --port "$API_PORT" --ws none $RELOAD_FLAG $RELOAD_EXCLUDES $(uvicorn_tls_args businessapi) &
fi
API_PID=$!
echo $API_PID > /tmp/teamspace_api.pid
wait_for_health "$SCHEME://localhost:$API_PORT/health" "API"

echo "Starting AI Agent Service on port $AGENT_PORT..."
if [ "$SERVE_MODE" = "production" ]; then
    ./.venv/bin/python3 -m gunicorn agent.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "$AGENT_WORKERS" --bind "0.0.0.0:$AGENT_PORT" $(gunicorn_tls_args aiagent) &
else
    ./.venv/bin/python3 -m uvicorn agent.main:app --host 0.0.0.0 --port "$AGENT_PORT" --ws none $RELOAD_FLAG $RELOAD_EXCLUDES $(uvicorn_tls_args aiagent) &
fi
AGENT_PID=$!
echo $AGENT_PID > /tmp/teamspace_agent.pid
wait_for_health "$SCHEME://localhost:$AGENT_PORT/health" "agent"

echo "Starting Flask Web App on port $FLASK_PORT..."
if [ "$SERVE_MODE" = "production" ]; then
    ./.venv/bin/python3 -m gunicorn "webapp.app:create_app()" \
        --workers "$WEBAPP_WORKERS" --bind "0.0.0.0:$FLASK_PORT" $(gunicorn_tls_args flaskapp) &
else
    FLASK_APP=webapp.app:create_app ./.venv/bin/python3 -m flask run --host 0.0.0.0 --port "$FLASK_PORT" $DEBUG_FLAG $(flask_tls_args flaskapp) &
fi
WEBAPP_PID=$!
echo $WEBAPP_PID > /tmp/teamspace_webapp.pid

echo ""
echo "All services started:"
echo "  Web App:      $SCHEME://$FLASK_HOST:$FLASK_PORT"
echo "  Business API: $SCHEME://localhost:$API_PORT"
echo "  AI Agent:     $SCHEME://localhost:$AGENT_PORT"
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
