"""Adaptery portali: mail z ofertami → lista ofert (jeden moduł na portal)."""

from __future__ import annotations

from cv_app.mail import MailMessage
from cv_app.portals.base import MailOffer, PortalAdapter
from cv_app.portals.justjoin import JustJoinAdapter
from cv_app.portals.nofluff import NoFluffAdapter
from cv_app.portals.pracuj import PracujAdapter
from cv_app.portals.theprotocol import TheProtocolAdapter

ADAPTERS: list[PortalAdapter] = [
    PracujAdapter(),
    JustJoinAdapter(),
    NoFluffAdapter(),
    TheProtocolAdapter(),
]

# Nazwy portali w panelu (klucz to ``Offer.source``).
SOURCE_LABELS: dict[str, str] = {
    "pracuj": "Pracuj.pl",
    "justjoin": "JustJoin.it",
    "nofluff": "No Fluff Jobs",
    "theprotocol": "theprotocol.it",
}


def adapter_for(sender: str) -> PortalAdapter | None:
    sender = sender.lower()
    return next((a for a in ADAPTERS if sender in a.senders), None)


def offers_from_mail(message: MailMessage) -> list[MailOffer]:
    """Oferty z maila; pusta lista, gdy nadawca nie ma adaptera."""
    adapter = adapter_for(message.sender)
    return adapter.parse(message) if adapter else []


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def all_senders() -> list[str]:
    """Adresy nadawców obsługiwanych przez adaptery (domyślna lista do Gmaila)."""
    return sorted({s for a in ADAPTERS for s in a.senders})


__all__ = [
    "ADAPTERS",
    "SOURCE_LABELS",
    "MailOffer",
    "PortalAdapter",
    "adapter_for",
    "all_senders",
    "offers_from_mail",
    "source_label",
]
