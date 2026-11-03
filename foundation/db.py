"""Database engine, sessions and the shared declarative Base.

SQLite for local development and tests; Postgres (Neon) in production. Neon's pooled endpoint
runs PgBouncer in transaction mode, so prepared statements are disabled and idle connections
are checked before use. Migrations use the direct (unpooled) URL when one is configured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC so comparisons work on both databases."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, future=True)

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng
    return create_engine(url, future=True, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5,
                         connect_args={"prepare_threshold": None, "connect_timeout": 15})


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@event.listens_for(Session, "before_flush")
def _clamp_strings(session, _ctx, _instances):
    """Postgres enforces VARCHAR lengths (SQLite does not). Over-long text is shortened to the
    column's length instead of failing the whole request."""
    for obj in list(session.new) + list(session.dirty):
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        for col in table.columns:
            length = getattr(col.type, "length", None)
            if length and isinstance(col.type, String):
                value = getattr(obj, col.key, None)
                if isinstance(value, str) and len(value) > length:
                    setattr(obj, col.key, value[:length])


def get_session() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def init_db() -> None:
    """Apply migrations, then create the first administrator if none exists."""
    from . import models  # noqa: F401  (register tables)
    from .migrate import upgrade_to_head

    try:
        import app.models  # noqa: F401  (project tables)
    except ImportError:
        pass
    if settings.database_url_direct and settings.database_url_direct != settings.database_url:
        direct = make_engine(settings.database_url_direct)
        try:
            upgrade_to_head(direct)
        finally:
            direct.dispose()
    else:
        upgrade_to_head(engine)
    from .auth_core import ensure_first_admin

    ensure_first_admin()


def audit(session: Session, action: str, actor_id: int | None = None, target: str = "", **detail: Any) -> None:
    from .models import AuditEvent

    session.add(AuditEvent(actor_id=actor_id, action=action, target=target[:300], detail=detail or None))
