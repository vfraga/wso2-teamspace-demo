# Single image serving all three Teamspace services; the compose file selects
# which one each container runs via its command. They share a codebase and
# dependency set, so one image keeps the build cached and the versions aligned.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer, so application edits don't refetch
# them. --frozen fails loudly if uv.lock and pyproject.toml disagree.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra redis

COPY . .
RUN uv sync --frozen --no-dev --extra redis


FROM python:3.12-slim-bookworm AS runtime

# curl is used by the compose healthchecks.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The app writes only to flask_session/ and (with the SQLite default)
# the database file, both of which are chowned below.
RUN groupadd --system --gid 1001 teamspace \
    && useradd --system --uid 1001 --gid teamspace --create-home teamspace

WORKDIR /app
COPY --from=builder --chown=teamspace:teamspace /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_ENV=production \
    SERVE_MODE=production

# Create the writable directories BEFORE dropping privileges, and own them as
# the runtime user. /data matters for more than permissions inside the image: a
# fresh Docker named volume inherits the ownership of the image directory it is
# mounted over, so without this the volume is root-owned and the non-root
# process cannot open the SQLite file ("unable to open database file").
RUN mkdir -p /app/flask_session /data     && chown -R teamspace:teamspace /app/flask_session /data

USER teamspace

# Overridden per service in docker-compose.yml.
EXPOSE 5001 8000 9091
CMD ["gunicorn", "api.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", "--bind", "0.0.0.0:9091"]
