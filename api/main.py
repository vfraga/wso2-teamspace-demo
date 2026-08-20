import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError

from common.logging_setup import configure_logging, is_production
from common.m2m_auth import SERVICE_AUTH_HEADER
from api.config import settings
from api.database import engine, get_engine
from api.models import Base
from api.routers import meetings, personalization, agent_configs, plans

configure_logging("Business API")

logger = logging.getLogger(__name__)


def _auto_create_enabled() -> bool:
    """Whether this process should create missing tables at startup.

    On by default so the demo boots against an empty database with no extra
    step. Off by default in production, where schema changes belong to Alembic
    (`alembic upgrade head`) — an app that silently creates its own schema has
    no way to apply a migration. `DB_AUTO_CREATE` overrides either way.
    """
    raw = os.getenv("DB_AUTO_CREATE", "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no")
    return not is_production()


def _ensure_schema() -> None:
    """Create any missing tables, tolerating a concurrent worker doing the same.

    `create_all(checkfirst=True)` inspects then creates, and those two steps are
    not atomic across processes. Under gunicorn every worker boots at once, so
    the losers of that race see "table already exists" — which used to abort the
    worker and take the whole master down with it.
    """
    try:
        Base.metadata.create_all(bind=engine)
    except OperationalError as exc:
        if "already exists" not in str(exc).lower():
            raise
        logger.info("Schema was created concurrently by another worker; continuing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _auto_create_enabled():
        logger.info("Business API starting, ensuring tables exist")
        _ensure_schema()
    else:
        logger.info(
            "Business API starting; DB_AUTO_CREATE is off. Apply schema with "
            "`alembic upgrade head` before serving traffic."
        )
    logger.info("Business API ready, CORS origins=%s", settings.ALLOWED_ORIGINS)
    yield


app = FastAPI(title="Teamspace Business API", version="1.0.0", lifespan=lifespan)


from common.fastapi_errors import handle_validation_error, handle_global_error


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return await handle_validation_error(request, exc, logger, "Business API")


@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    return await handle_global_error(request, exc, logger, "Business API")


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    # Explicit method list rather than "*"; the headers were already explicit.
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", SERVICE_AUTH_HEADER],
)


@app.get("/health")
def health():
    try:
        conn = get_engine().connect()
        conn.close()
        return {"status": "healthy"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unreachable") from None


app.include_router(meetings.router, prefix="/meetings", tags=["Meetings"])
app.include_router(personalization.router, prefix="/personalization", tags=["Personalization"])
app.include_router(agent_configs.router, prefix="/agent-config", tags=["Agent Config"])
app.include_router(plans.router, prefix="/plans", tags=["Plans"])
