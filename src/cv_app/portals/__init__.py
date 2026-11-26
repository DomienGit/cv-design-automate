"""Adaptery portali: mail z ofertami → lista ofert (jeden moduł na portal)."""

from __future__ import annotations

from cv_app.mail import MailMessage
from cv_app.portals.base import MailOffer, PortalAdapter
from cv_app.portals.pracuj import PracujAdapter

ADAPTERS: list[PortalAdapter] = [PracujAdapter()]


def adapter_for(sender: str) -> PortalAdapter | None:
    sender = sender.lower()
    return next((a for a in ADAPTERS if sender in a.senders), None)


def offers_from_mail(message: MailMessage) -> list[MailOffer]:
    """Oferty z maila; pusta lista, gdy nadawca nie ma adaptera."""
    adapter = adapter_for(message.sender)
    return adapter.parse(message) if adapter else []


__all__ = ["ADAPTERS", "MailOffer", "PortalAdapter", "adapter_for", "offers_from_mail"]
