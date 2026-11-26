"""Wiadomość e-mail niezależna od źródła (Gmail API, plik .eml)."""

from __future__ import annotations

import email
from datetime import datetime
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

from pydantic import BaseModel


class MailMessage(BaseModel):
    id: str  # identyfikator wiadomości (Gmail id albo nagłówek Message-ID)
    sender: str  # sam adres, małymi literami
    subject: str = ""
    date: datetime | None = None
    html: str | None = None
    text: str | None = None


def parse_raw_message(raw: bytes, message_id: str | None = None) -> MailMessage:
    """Parsuje surową wiadomość RFC 822 (np. plik .eml albo Gmail ``format=raw``)."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    date = None
    if msg["Date"]:
        try:
            date = parsedate_to_datetime(str(msg["Date"]))
        except (TypeError, ValueError):
            date = None
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    return MailMessage(
        id=message_id or str(msg["Message-ID"] or "").strip("<> ") or "unknown",
        sender=parseaddr(str(msg["From"] or ""))[1].lower(),
        subject=str(msg["Subject"] or ""),
        date=date,
        html=html_part.get_content() if html_part is not None else None,
        text=text_part.get_content() if text_part is not None else None,
    )
