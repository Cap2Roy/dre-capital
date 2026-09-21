"""Database engine + session factory.

Uses SQLite for local development (zero config) and PostgreSQL when
``DATABASE_URL`` points at Cloud SQL.  The same SQLAlchemy 2.0 ORM layer
works against both.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)

# SQLite WAL = better concurrency for the local single-instance dev server.
if settings.is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create all tables (dev convenience; Cloud Run uses Cloud SQL)."""
    from app import models  # noqa: F401  — register models on Base
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency that yields a session and closes it on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
