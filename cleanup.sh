#!/bin/bash
# Reset Teamspace runtime data for local (dev and SERVE_MODE=production)
# and Docker Compose. Does not touch .env, source, .venv, or WSO2 IS.
#
# Usage:
#   bash cleanup.sh                 # local files + host Redis keys + compose -v
#   bash cleanup.sh --local         # host sqlite / flask_session / pids / Redis
#   bash cleanup.sh --docker        # docker compose down --volumes
#   bash cleanup.sh --redis         # host Redis keys only
#   bash cleanup.sh --stop          # stop.sh first (also implied by default --all)
#   bash cleanup.sh --dry-run
#   bash cleanup.sh --yes           # no prompt (required if stdin is not a TTY)
#   bash cleanup.sh --caches        # also .pytest_cache, .ruff_cache, __pycache__
#   bash cleanup.sh --flush-redis   # FLUSHDB on host Redis (shared-server hazard)
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

DO_LOCAL=0
DO_DOCKER=0
DO_REDIS=0
DO_STOP=0
DO_CACHES=0
DRY_RUN=0
ASSUME_YES=0
FLUSH_REDIS=0
SCOPE_SET=0

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \?//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --all)     DO_LOCAL=1; DO_DOCKER=1; DO_REDIS=1; DO_STOP=1; SCOPE_SET=1 ;;
        --local)   DO_LOCAL=1; DO_REDIS=1; SCOPE_SET=1 ;;
        --docker)  DO_DOCKER=1; SCOPE_SET=1 ;;
        --redis)   DO_REDIS=1; SCOPE_SET=1 ;;
        --stop)    DO_STOP=1 ;;
        --caches)  DO_CACHES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --flush-redis) FLUSH_REDIS=1; DO_REDIS=1 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
    shift
done

if [ "$SCOPE_SET" -eq 0 ]; then
    DO_LOCAL=1
    DO_DOCKER=1
    DO_REDIS=1
    DO_STOP=1
fi

# Prefer already-exported values. Fall back to KEY=value in .env without sourcing it.
env_get() {
    local key="$1"
    local val=""
    if [ -n "${!key:-}" ]; then
        printf '%s' "${!key}"
        return
    fi
    if [ -f "$ROOT/.env" ]; then
        val="$(grep -E "^${key}=" "$ROOT/.env" | tail -n1 | cut -d= -f2- || true)"
        val="${val%\"}"
        val="${val#\"}"
        val="${val%\'}"
        val="${val#\'}"
        printf '%s' "$val"
    fi
}

REDIS_URL_VAL="$(env_get REDIS_URL)"
DATABASE_URL_VAL="$(env_get DATABASE_URL)"
REDIS_KEY_PREFIX_VAL="$(env_get REDIS_KEY_PREFIX)"

compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        echo "docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        echo "docker-compose"
    else
        echo ""
    fi
}

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN: $*"
        return 0
    fi
    eval "$@"
}

# SQLite WAL leaves -wal/-shm; removing only the main file keeps dirty state.
# The well-known list, *.db glob, and DATABASE_URL can all name the same file.
_SQLITE_SEEN=""
remove_sqlite_family() {
    local base="$1"
    local f
    case "$_SQLITE_SEEN" in
        *"::$base::"*) return ;;
    esac
    _SQLITE_SEEN="${_SQLITE_SEEN}::$base::"
    for f in "$base" "$base-wal" "$base-shm" "$base-journal"; do
        if [ -e "$f" ]; then
            echo "  sqlite: $f"
            if [ "$DRY_RUN" -eq 1 ]; then
                echo "DRY-RUN: rm -f $f"
            else
                rm -f "$f"
            fi
        fi
    done
}

# SQLAlchemy: sqlite:///rel.db  vs  sqlite:////abs/path.db
sqlite_path_from_url() {
    local url="$1"
    case "$url" in
        sqlite:////*)
            printf '%s' "${url#sqlite:///}"
            ;;
        sqlite:///*)
            printf '%s' "${url#sqlite:///}"
            ;;
        *)
            printf ''
            ;;
    esac
}

is_under_root() {
    local path="$1"
    case "$path" in
        "$ROOT"|"$ROOT"/*) return 0 ;;
        *) return 1 ;;
    esac
}

# docker-compose.yml uses ${VAR:?...} for CLIENT_ID, CLIENT_SECRET,
# AGENT_STATE_SIGNING_SECRET and FLASK_SECRET_KEY. Compose interpolates the
# whole file for every command, including `down`, so a missing signing secret
# aborts volume cleanup even though those values are never used at teardown.
# The Python apps fall back (signing secret → CLIENT_SECRET, Flask key generated);
# Compose does not. Placeholders satisfy interpolation only for this process.
compose_required_env() {
    local cid csec signing flask
    cid="$(env_get CLIENT_ID)"
    csec="$(env_get CLIENT_SECRET)"
    signing="$(env_get AGENT_STATE_SIGNING_SECRET)"
    flask="$(env_get FLASK_SECRET_KEY)"
    export CLIENT_ID="${cid:-_cleanup}"
    export CLIENT_SECRET="${csec:-_cleanup}"
    export AGENT_STATE_SIGNING_SECRET="${signing:-_cleanup}"
    export FLASK_SECRET_KEY="${flask:-_cleanup}"
}

compose_project_name() {
    # Match Compose's default: the directory name.
    basename "$ROOT"
}

compose_has_state() {
    local project ids
    project="$(env_get COMPOSE_PROJECT_NAME)"
    if [ -z "$project" ]; then
        project="$(compose_project_name)"
    fi
    if docker volume inspect "${project}_teamspace-data" >/dev/null 2>&1; then
        return 0
    fi
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=${project}" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        return 0
    fi
    return 1
}

echo "Teamspace cleanup"
echo "  root:   $ROOT"
echo "  dry:    $DRY_RUN"
echo "  local:  $DO_LOCAL  docker: $DO_DOCKER  redis: $DO_REDIS  stop: $DO_STOP"
echo

if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
    if [ ! -t 0 ]; then
        echo "stdin is not a TTY; pass --yes" >&2
        exit 1
    fi
    printf "This deletes runtime data (sqlite, sessions, compose volume). Continue? [y/N] "
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

# --- stop local processes so WAL/session files are not held open ---
if [ "$DO_STOP" -eq 1 ]; then
    echo "==> Stopping host processes (stop.sh)"
    if [ -x "$ROOT/stop.sh" ] || [ -f "$ROOT/stop.sh" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "DRY-RUN: bash stop.sh"
        else
            bash "$ROOT/stop.sh" || true
        fi
    fi
fi

# --- host sqlite + flask sessions + pid files ---
if [ "$DO_LOCAL" -eq 1 ]; then
    echo "==> Host SQLite"
    # Well-known files in the repo root only — never walk .venv
    for name in teamspace.db test_teamspace.db test_live_teamspace.db test_e2e.db; do
        remove_sqlite_family "$ROOT/$name"
    done

    shopt -s nullglob
    extra_dbs=( "$ROOT"/*.db )
    shopt -u nullglob
    for db in "${extra_dbs[@]:-}"; do
        [ -n "$db" ] || continue
        remove_sqlite_family "$db"
    done

    sqlite_from_env="$(sqlite_path_from_url "$DATABASE_URL_VAL")"
    if [ -n "$sqlite_from_env" ]; then
        case "$sqlite_from_env" in
            /*) abs="$sqlite_from_env" ;;
            *)  abs="$ROOT/$sqlite_from_env" ;;
        esac
        # Compose uses sqlite:////data/teamspace.db — that path is in the volume, not the host.
        if is_under_root "$abs"; then
            remove_sqlite_family "$abs"
        else
            echo "  skip DATABASE_URL sqlite outside repo: $abs"
        fi
    elif [ -n "$DATABASE_URL_VAL" ]; then
        echo "  skip non-SQLite DATABASE_URL (not dropping remote DBs): $DATABASE_URL_VAL"
    fi

    echo "==> Flask sessions (flask_session/)"
    if [ -d "$ROOT/flask_session" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "DRY-RUN: find flask_session -mindepth 1 -delete"
        else
            find "$ROOT/flask_session" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
            echo "  cleared flask_session/"
        fi
    else
        echo "  flask_session/ absent"
    fi

    echo "==> PID files"
    shopt -s nullglob
    pids=( /tmp/teamspace_*.pid )
    shopt -u nullglob
    if [ "${#pids[@]}" -gt 0 ]; then
        for p in "${pids[@]}"; do
            echo "  $p"
            run rm -f "$p"
        done
    else
        echo "  none"
    fi
fi

# --- host Redis (agent OBO + Flask sessions). Never FLUSHDB unless asked. ---
if [ "$DO_REDIS" -eq 1 ]; then
    echo "==> Host Redis"
    # docker-internal hostname is meaningless on the host
    case "$REDIS_URL_VAL" in
        ""|*://redis:*|*://redis/*)
            echo "  REDIS_URL unset or compose-internal; skip host redis-cli"
            ;;
        *)
            if ! command -v redis-cli >/dev/null 2>&1; then
                echo "  redis-cli not found; skip"
            elif [ "$FLUSH_REDIS" -eq 1 ]; then
                echo "  FLUSHDB $REDIS_URL_VAL"
                if [ "$DRY_RUN" -eq 0 ]; then
                    redis-cli -u "$REDIS_URL_VAL" FLUSHDB >/dev/null
                fi
            else
                prefix="$REDIS_KEY_PREFIX_VAL"
                patterns=(
                    "${prefix}teamspace:agent:*"
                    "${prefix}teamspace:session:*"
                    "${prefix}teamspace:*"
                )
                for pat in "${patterns[@]}"; do
                    echo "  SCAN $pat"
                    if [ "$DRY_RUN" -eq 1 ]; then
                        echo "DRY-RUN: redis-cli -u … --scan --pattern $pat | xargs DEL"
                        continue
                    fi
                    # shellcheck disable=SC2046
                    keys="$(redis-cli -u "$REDIS_URL_VAL" --scan --pattern "$pat" || true)"
                    if [ -n "$keys" ]; then
                        printf '%s\n' "$keys" | xargs -r redis-cli -u "$REDIS_URL_VAL" DEL >/dev/null || \
                        printf '%s\n' "$keys" | xargs redis-cli -u "$REDIS_URL_VAL" DEL >/dev/null || true
                    fi
                done
            fi
            ;;
    esac
fi

# --- Compose volume holds /data/teamspace.db; Redis in compose is ephemeral ---
if [ "$DO_DOCKER" -eq 1 ]; then
    echo "==> Docker Compose"
    CC="$(compose_cmd)"
    if [ -z "$CC" ]; then
        echo "  docker compose not available; skip"
    elif [ ! -f "$ROOT/docker-compose.yml" ]; then
        echo "  docker-compose.yml missing; skip"
    elif ! docker info >/dev/null 2>&1; then
        echo "  docker daemon is not running; skip"
    elif ! compose_has_state; then
        echo "  no compose project or teamspace-data volume; skip"
    else
        echo "  $CC down --volumes --remove-orphans"
        if [ "$DRY_RUN" -eq 0 ]; then
            compose_required_env
            # Don't fail the whole cleanup if compose still errors; host files
            # are already gone.
            if ! $CC down --volumes --remove-orphans; then
                echo "  warning: compose down failed; host cleanup already applied" >&2
            fi
        fi
    fi
fi

if [ "$DO_CACHES" -eq 1 ]; then
    echo "==> Caches"
    for d in "$ROOT/.pytest_cache" "$ROOT/.ruff_cache"; do
        if [ -e "$d" ]; then
            echo "  $d"
            run rm -rf "$(printf '%q' "$d")"
        fi
    done
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN: find . -type d -name __pycache__ -prune -exec rm -rf"
    else
        find "$ROOT" \( -path "$ROOT/.venv" -o -path "$ROOT/venv" \) -prune -o \
            -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
        echo "  removed __pycache__ (excluding .venv)"
    fi
fi

echo
echo "Done. .env, WSO2 IS, and source were left alone."
echo "Dev schema will be recreated on next start.sh (DB_AUTO_CREATE)."
echo "Compose needs: docker compose run --rm business-api alembic upgrade head"
