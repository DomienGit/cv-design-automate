"""Połączenie z bazą SQLite."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, inspect, text
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
    add_missing_columns(engine)


def add_missing_columns(engine: Engine) -> list[str]:
    """Prosta migracja: dodaje do istniejących tabel kolumny dopisane w ``models.py``.

    ``create_all`` tworzy tylko brakujące tabele, a baza u użytkownika już istnieje.
    Nowe kolumny muszą być nullable albo mieć ``server_default``. Zwraca dodane kolumny.
    """
    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                ddl += column.type.compile(dialect=engine.dialect)
                if column.server_default is not None:
                    ddl += f" DEFAULT '{column.server_default.arg}'"
                if not column.nullable and column.server_default is not None:
                    ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
