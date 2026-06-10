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
