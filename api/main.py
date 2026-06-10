import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError

from api.config import settings
from api.database import engine, get_engine
from api.models import Base
from api.routers import meetings, personalization, agent_configs, plans

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Business API starting, creating tables")
    Base.metadata.create_all(bind=engine)
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
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
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
