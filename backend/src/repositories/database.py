"""SQLAlchemy engine and SQLite initialization helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all SQLAlchemy persistence models."""


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///") or database_url == "sqlite:///:memory:":
        return

    database_path = Path(database_url.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_for_settings(settings: Settings | None = None) -> Engine:
    active_settings = settings or get_settings()
    _ensure_sqlite_parent(active_settings.database_url)

    connect_args = {}
    if active_settings.database_url.startswith("sqlite"):
        # FastAPI may access SQLite from worker threads during local development.
        connect_args["check_same_thread"] = False

    return create_engine(active_settings.database_url, connect_args=connect_args, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(settings: Settings | None = None) -> Engine:
    active_settings = settings or get_settings()
    active_settings.cache_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine_for_settings(active_settings)

    # Importing models here registers table metadata without forcing import-time side effects.
    from src.repositories import models as _models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    return engine
