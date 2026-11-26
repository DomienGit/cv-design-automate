"""Połączenie z bazą SQLite."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from cv_app import models  # noqa: F401  (rejestruje tabele w metadanych)
from cv_app.config import Settings


def make_engine(settings: Settings) -> Engine:
    settings.ensure_dirs()
    return create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
