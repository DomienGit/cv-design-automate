"""Modele bazy danych: oferta pracy i wygenerowane CV."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class OfferStatus(StrEnum):
    NEW = "new"  # nowa, czeka na decyzję
    REJECTED = "rejected"  # odrzucona przez filtr trafności
    SELECTED = "selected"  # zaznaczona do generatora CV
    HIDDEN = "hidden"  # ukryta przez użytkownika
    APPLIED = "applied"  # już aplikowałem


class CVStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    DRAFT = "draft"
    READY = "ready"
    SENT = "sent"
    ERROR = "error"


class Offer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    # Klucz deduplikacji: źródło + numer oferty (np. "pracuj", "1005236889").
    source: str = Field(index=True)
    external_id: str = Field(index=True)

    # Pola pokazywane w panelu.
    title: str
    locations: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    requirements: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    responsibilities: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )

    # Pola techniczne.
    company: str | None = None
    language: str | None = None  # "pl" / "en"
    url: str | None = None
    mail_message_id: str | None = Field(default=None, index=True)
    mail_date: datetime | None = None
    category: str | None = None  # np. "devops", "helpdesk"
    incomplete: bool = False  # strona oferty się nie pobrała
    status: OfferStatus = Field(default=OfferStatus.NEW, index=True)
    created_at: datetime = Field(default_factory=_now)

    cvs: list["CV"] = Relationship(back_populates="offer")

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_offer_source_id"),)


class CV(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    offer_id: int | None = Field(default=None, foreign_key="offer.id", index=True)

    language: str = "pl"
    status: CVStatus = Field(default=CVStatus.QUEUED, index=True)
    blocks: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    variant: str | None = None  # wariant bazowy, z którego powstało CV
    profile_version: str | None = None
    template_version: str | None = None
    pdf_path: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    offer: Optional["Offer"] = Relationship(back_populates="cvs")


class SyncState(SQLModel, table=True):
    """Stan synchronizacji źródła (np. "gmail"): czas ostatniego udanego pobrania."""

    key: str = Field(primary_key=True)
    last_sync_at: datetime
