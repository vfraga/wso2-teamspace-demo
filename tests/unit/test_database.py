import pytest
from api.database import get_db, get_engine, get_sessionmaker, engine, SessionLocal
from api.config import settings

def test_database_lazy_loading():
    # Verify engine and SessionLocal are proxy objects initially
    assert hasattr(engine, "connect")
    assert callable(SessionLocal)

    # Calling get_db should trigger lazy loading and yield a session
    db_gen = get_db()
    db = next(db_gen)
    assert db is not None
    
    # Verify singleton behavior of internal lazy engine/sessionmaker
    e1 = get_engine()
    e2 = get_engine()
    assert e1 is e2

    sm1 = get_sessionmaker()
    sm2 = get_sessionmaker()
    assert sm1 is sm2

    # Clean up
    try:
        next(db_gen)
    except StopIteration:
        pass


# --- `engine` must be a real Engine ---------------------------------------
#
# It used to be a hand-rolled proxy that forwarded attribute access. That made
# `MetaData.create_all(bind=engine)` work while `Session(bind=engine)` silently
# took a different path — SQLAlchemy checks `isinstance(bind, Engine)` — in
# which every statement acquired its own connection. On SQLite the second write
# in a transaction then failed with "database is locked", which showed up as
# environment-dependent E2E flakiness rather than an obvious error.


def test_engine_attribute_is_a_real_sqlalchemy_engine():
    from sqlalchemy.engine import Engine

    from api.database import engine as lazy_engine

    assert isinstance(lazy_engine, Engine), (
        "api.database.engine must be a real Engine — a proxy silently breaks "
        "Session binding"
    )


def test_session_local_attribute_is_a_real_sessionmaker():
    from sqlalchemy.orm import sessionmaker

    from api.database import SessionLocal as lazy_sessionmaker

    assert isinstance(lazy_sessionmaker, sessionmaker)


def test_unknown_module_attribute_still_raises_attribute_error():
    import api.database

    with pytest.raises(AttributeError):
        # Call the PEP 562 hook directly: plain attribute access here trips
        # ruff's useless-expression rule, and getattr() its constant-attr rule.
        api.database.__getattr__("no_such_thing")


def test_multi_row_orm_write_through_the_module_engine(tmp_path):
    """The exact sequence that used to deadlock: create_all then a 2-row seed.

    One row was never enough to trigger it — the failure needs a second write
    in the same transaction.
    """
    from sqlalchemy.orm import Session

    from api.config import settings
    from api.models import Base, OrganizationPlan
    import api.database as database

    db_file = tmp_path / "engine_check.db"
    original_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{db_file}"
    database.Database._engine = None
    database.Database._session_local = None
    try:
        engine = database.get_engine()
        Base.metadata.create_all(bind=engine)
        with Session(engine) as session:
            session.merge(OrganizationPlan(org="org-a", plan="enterprise"))
            session.merge(OrganizationPlan(org="org-b", plan="basic"))
            session.commit()
        with Session(engine) as session:
            assert session.query(OrganizationPlan).count() == 2
    finally:
        settings.DATABASE_URL = original_url
        database.Database._engine = None
        database.Database._session_local = None
