"""Gmail: logowanie OAuth (tylko odczyt) i pobieranie maili z ofertami.

Aplikacja zna tylko ścieżki do sekretów: ``credentials.json`` (klient OAuth typu
„Desktop app” z Google Cloud) i ``token.json`` (zapisywany po pierwszym logowaniu,
z prawami 600). Treści tych plików nigdy nie logujemy.

Pobieranie jest przyrostowe: zapamiętujemy czas ostatniej synchronizacji w bazie
(``SyncState``) i przy kolejnym pobraniu pytamy Gmaila o maile od tego momentu
(z godzinnym zakładem, duplikaty odsiewa klucz oferty). Pierwsze pobranie sięga
``fetch_days_back`` dni wstecz.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from cv_app.config import Settings
from cv_app.mail import MailMessage, parse_raw_message
from cv_app.models import SyncState

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SYNC_KEY = "gmail"
SYNC_OVERLAP = timedelta(hours=1)
MAX_MESSAGES = 500


class GmailError(Exception):
    """Błąd Gmaila z komunikatem dla użytkownika."""


class GmailAuthRequired(GmailError):
    """Potrzebne logowanie w przeglądarce (brak albo nieważny token)."""


# --- OAuth -----------------------------------------------------------------------------


def _save_token(creds: Any, settings: Settings) -> None:
    path = settings.token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.chmod(path, 0o600)


def load_credentials(settings: Settings, *, interactive: bool = False) -> Any:
    """Zwraca ważne poświadczenia. ``interactive`` pozwala otworzyć logowanie w przeglądarce."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if settings.token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(settings.token_path), SCOPES)
        except ValueError:
            creds = None  # uszkodzony token: zaloguj ponownie
    if creds is not None and creds.valid:
        return creds
    if creds is not None and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, settings)
            return creds
        except RefreshError:
            creds = None

    if not interactive:
        raise GmailAuthRequired("Zaloguj się do Gmaila (potrzebny dostęp tylko do odczytu).")
    if not settings.gmail_credentials_path.is_file():
        raise GmailError(
            f"Brak pliku {settings.gmail_credentials_path}. Pobierz z Google Cloud klienta "
            "OAuth typu „Desktop app” i zapisz go pod tą ścieżką "
            "(albo ustaw CV_APP_GMAIL_CREDENTIALS_PATH)."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.gmail_credentials_path), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="127.0.0.1",
        port=0,
        open_browser=True,
        authorization_prompt_message="Otwieram logowanie Google w przeglądarce: {url}",
        success_message="Zalogowano. Możesz zamknąć tę kartę i wrócić do panelu CV.",
    )
    _save_token(creds, settings)
    return creds


def build_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- pobieranie ------------------------------------------------------------------------


def build_query(senders: list[str], after: datetime) -> str:
    """Zapytanie Gmaila: od wybranych nadawców, po danym czasie (sekundy epoki)."""
    if not senders:
        raise GmailError("Lista nadawców jest pusta (CV_APP_SENDERS).")
    who = " OR ".join(senders)
    return f"from:({who}) after:{int(after.timestamp())}"


def list_message_ids(service: Any, query: str, limit: int = MAX_MESSAGES) -> list[str]:
    ids: list[str] = []
    token = None
    while len(ids) < limit:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=token, maxResults=min(100, limit))
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:limit]


def fetch_message(service: Any, message_id: str) -> MailMessage:
    resp = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    raw = base64.urlsafe_b64decode(resp["raw"].encode("ascii"))
    return parse_raw_message(raw, message_id=message_id)


def fetch_messages(service: Any, senders: list[str], after: datetime) -> list[MailMessage]:
    """Maile od nadawców po ``after``, od najstarszego."""
    ids = list_message_ids(service, build_query(senders, after))
    messages = [fetch_message(service, i) for i in ids]
    return sorted(messages, key=lambda m: m.date or datetime.min.replace(tzinfo=UTC))


# --- synchronizacja --------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass
class SyncResult:
    since: datetime
    until: datetime
    messages: list[MailMessage] = field(default_factory=list)


def last_sync(engine: Engine) -> datetime | None:
    with Session(engine) as session:
        state = session.get(SyncState, SYNC_KEY)
        return _as_utc(state.last_sync_at) if state else None


def sync_since(engine: Engine, settings: Settings, now: datetime) -> datetime:
    previous = last_sync(engine)
    if previous is None:
        return now - timedelta(days=settings.fetch_days_back)
    return previous - SYNC_OVERLAP


def sync_mail(
    engine: Engine,
    settings: Settings,
    service: Any,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SyncResult:
    """Pobiera nowe maile i zapisuje czas synchronizacji (tylko gdy wszystko się udało)."""
    started = _as_utc(now())
    since = sync_since(engine, settings, started)
    try:
        messages = fetch_messages(service, settings.senders, since)
    except GmailError:
        raise
    except Exception as exc:  # błędy sieci / API Google
        raise GmailError(f"Nie udało się pobrać maili z Gmaila: {exc}") from exc

    with Session(engine) as session:
        state = session.get(SyncState, SYNC_KEY) or SyncState(key=SYNC_KEY, last_sync_at=started)
        state.last_sync_at = started
        session.add(state)
        session.commit()
    return SyncResult(since=since, until=started, messages=messages)
