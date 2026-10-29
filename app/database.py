"""Database engine + session factory.

Uses SQLite for local development (zero config) and PostgreSQL when
``DATABASE_URL`` points at Cloud SQL.  The same SQLAlchemy 2.0 ORM layer
works against both.
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("uvicorn.error")

connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
# Cloud SQL closes idle connections — check each one before use.
engine = create_engine(settings.database_url, connect_args=connect_args, future=True,
                       pool_pre_ping=not settings.is_sqlite)

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
    """Create missing tables, then add columns / enum values that newer code expects."""
    from app import models  # noqa: F401  — register models on Base
    Base.metadata.create_all(bind=engine)
    upgrade_schema()


def upgrade_schema() -> None:
    """Lightweight forward migration for the persistent database.

    ``create_all`` never alters existing tables, so after a release adds a model
    field the production DB would miss it. This adds missing columns (nullable, or
    with a scalar default) and, on PostgreSQL, missing enum labels. It never drops
    or changes anything that exists. Idempotent.
    """
    from sqlalchemy import Enum as SAEnum
    from app import models  # noqa: F401  — register models on Base
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        default = f" DEFAULT {'TRUE' if val else 'FALSE'}" if not settings.is_sqlite else f" DEFAULT {int(val)}"
                    elif isinstance(val, (int, float)):
                        default = f" DEFAULT {val}"
                    elif isinstance(val, str):
                        default = " DEFAULT '" + val.replace("'", "''") + "'"
                if not col.nullable and not default:
                    log.warning("Schema upgrade: can't add NOT NULL column %s.%s without a default", table.name, col.name)
                    continue
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}'))
                log.info("Schema upgrade: added %s.%s", table.name, col.name)
    if engine.dialect.name != "postgresql":
        return
    # Native PostgreSQL enums: add labels introduced by newer code (one statement per label, autocommit).
    seen: set[str] = set()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                t = col.type
                if not isinstance(t, SAEnum) or not t.native_enum or not t.name or t.name in seen:
                    continue
                seen.add(t.name)
                labels = {r[0] for r in conn.execute(text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type ty ON ty.oid = e.enumtypid WHERE ty.typname = :n"),
                    {"n": t.name})}
                if not labels:
                    continue
                for label in t.enums:
                    if label not in labels:
                        conn.execute(text(f"ALTER TYPE \"{t.name}\" ADD VALUE IF NOT EXISTS '{label}'"))
                        log.info("Schema upgrade: enum %s += %s", t.name, label)


def get_db():
    """FastAPI dependency that yields a session and closes it on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
