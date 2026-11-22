import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from app.config import settings


@pytest.fixture(scope="session")
def db_engine():
    try:
        engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return engine
    except Exception as e:
        pytest.skip(f"PostgreSQL connection to {settings.database_url} unavailable: {e}")


@pytest.fixture
def db_session(db_engine):
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
