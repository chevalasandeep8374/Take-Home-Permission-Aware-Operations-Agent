from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from app.config import settings

# Engine connected using application credentials (RLS enforced)
engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


@contextmanager
def hospital_transaction(hospital_id: int) -> Generator[Session, None, None]:
    """
    Opens a database session, begins a transaction, and executes
    SET LOCAL app.hospital_id = <hospital_id> for PostgreSQL Row-Level Security.
    Commits on successful completion, rolls back on any exception.
    """
    session: Session = SessionLocal()
    try:
        with session.begin():
            session.execute(
                text("SELECT set_config('app.hospital_id', :hid, true);"),
                {"hid": str(int(hospital_id))},
            )
            yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """Standard database session generator."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
