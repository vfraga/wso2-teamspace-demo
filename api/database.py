import logging
import threading

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from api.config import settings

logger = logging.getLogger(__name__)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_sqlite_connection(dbapi_connection) -> bool:
    """True if this DBAPI connection is SQLite, whatever engine produced it.

    Checked by driver module rather than by URL so it holds for engines this
    module didn't create (pysqlite3 and the stdlib sqlite3 both match).
    """
    module = type(dbapi_connection).__module__ or ""
    return "sqlite" in module


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Apply SQLite pragmas on every new connection (2.3.6).

    - ``journal_mode=WAL``   improves concurrent read/write performance.
    - ``busy_timeout=5000``  waits up to 5s on a locked DB instead of
      raising ``OperationalError: database is locked`` immediately.

    Guarded on the *connection*, not on ``settings.DATABASE_URL``: this
    listener is attached to the base ``Engine`` class, so it fires for every
    engine in the process — including ones built independently of the app
    config, such as the test fixtures and Alembic. Against Postgres the PRAGMA
    is a syntax error that aborts the surrounding transaction, so every
    subsequent statement on that connection fails too.
    """
    if not _is_sqlite_connection(dbapi_connection):
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
    except Exception as exc:
        logger.warning("Failed to set SQLite pragmas: %s", exc)
        logger.debug("SQLite pragma setup best-effort failure trace", exc_info=True)


class Database:
    _engine: Engine | None = None
    _session_local: sessionmaker | None = None
    _lock = threading.Lock()

    @classmethod
    def engine(cls) -> Engine:
        if cls._engine is None:
            with cls._lock:
                if cls._engine is None:
                    logger.info(
                        "Initializing SQLAlchemy engine lazily for %s",
                        settings.DATABASE_URL,
                    )
                    # check_same_thread is a SQLite-only DBAPI argument;
                    # passing it to psycopg or MySQLdb is a TypeError at
                    # connect time, which made any non-SQLite DATABASE_URL
                    # fail outright.
                    connect_args = (
                        {"check_same_thread": False}
                        if _is_sqlite(settings.DATABASE_URL)
                        else {}
                    )
                    cls._engine = create_engine(
                        settings.DATABASE_URL,
                        connect_args=connect_args,
                        pool_pre_ping=not _is_sqlite(settings.DATABASE_URL),
                    )
        return cls._engine

    @classmethod
    def sessionmaker(cls) -> sessionmaker:
        if cls._session_local is None:
            with cls._lock:
                if cls._session_local is None:
                    cls._session_local = sessionmaker(
                        autocommit=False, autoflush=False, bind=cls.engine()
                    )
        return cls._session_local


def get_engine() -> Engine:
    return Database.engine()


def get_sessionmaker() -> sessionmaker:
    return Database.sessionmaker()


class EngineProxy:
    def __getattr__(self, name):
        return getattr(get_engine(), name)

    def __repr__(self):
        return repr(get_engine())


class SessionLocalProxy:
    def __call__(self, *args, **kwargs):
        return get_sessionmaker()(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(get_sessionmaker(), name)


engine = EngineProxy()
SessionLocal = SessionLocalProxy()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
