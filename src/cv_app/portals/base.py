"""Wspólne typy adapterów portali."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Protocol

from pydantic import BaseModel, Field

from cv_app.mail import MailMessage


class MailOffer(BaseModel):
    """Oferta odczytana z maila (przed pobraniem strony oferty)."""

    source: str  # np. "pracuj"
    external_id: str  # numer oferty w portalu; z ``source`` tworzy klucz deduplikacji
    title: str
    url: str  # adres bez parametrów śledzących
    company: str | None = None
    locations: list[str] = Field(default_factory=list)
    salary: str | None = None
    mail_message_id: str | None = None
    mail_date: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


class PortalAdapter(Protocol):
    source: ClassVar[str]
    senders: ClassVar[frozenset[str]]

    def parse(self, message: MailMessage) -> list[MailOffer]: ...
