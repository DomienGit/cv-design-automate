from __future__ import annotations

import base64
import json
import stat
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from cv_app import cli, gmail
from cv_app.db import init_db, make_engine
from cv_app.gmail import (
    GmailAuthRequired,
    GmailError,
    build_query,
    fetch_messages,
    last_sync,
    load_credentials,
    sync_mail,
)

SAMPLE = Path(__file__).parent / "fixtures" / "mail" / "pracuj_sample.html"
NOW = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)


def raw_mail(day: int, sender="jobalert@wysylka.pracuj.pl") -> str:
    msg = EmailMessage()
    msg["From"] = f"Pracuj <{sender}>"
    msg["Subject"] = f"Oferty {day}.11"
    msg["Date"] = f"{day} Nov 2026 02:00:00 +0100"
    msg.set_content(SAMPLE.read_text(encoding="utf-8"), subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeService:
    """Atrapa łańcucha service.users().messages().list/get(...).execute()."""

    def __init__(self, data: dict[str, str], page_size: int = 2, fail: bool = False):
        self.data = data
        self.page_size = page_size
        self.fail = fail
        self.queries: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, pageToken=None, maxResults=100):
        if self.fail:
            raise OSError("sieć leży")
        self.queries.append(q)
        ids = list(self.data)
        start = int(pageToken or 0)
        page = ids[start : start + self.page_size]
        resp = {"messages": [{"id": i} for i in page]}
        if start + self.page_size < len(ids):
            resp["nextPageToken"] = str(start + self.page_size)
        return _Call(resp)

    def get(self, userId, id, format):
        assert format == "raw"
        return _Call({"id": id, "raw": self.data[id]})


@pytest.fixture
def engine(settings):
    eng = make_engine(settings)
    init_db(eng)
    yield eng
    eng.dispose()


def test_build_query():
    after = datetime(2026, 11, 20, tzinfo=UTC)
    q = build_query(["a@x.pl", "b@y.pl"], after)
    assert q == f"from:(a@x.pl OR b@y.pl) after:{int(after.timestamp())}"
    with pytest.raises(GmailError):
        build_query([], after)


def test_fetch_messages_paginates_and_sorts():
    service = FakeService({"m3": raw_mail(25), "m1": raw_mail(23), "m2": raw_mail(24)})
    msgs = fetch_messages(service, ["jobalert@wysylka.pracuj.pl"], NOW - timedelta(days=7))
    assert [m.id for m in msgs] == ["m1", "m2", "m3"]
    assert msgs[0].sender == "jobalert@wysylka.pracuj.pl"
    assert msgs[0].html and "oferta,1000000001" in msgs[0].html


def test_sync_first_time_uses_days_back_then_last_sync(engine, settings):
    service = FakeService({"m1": raw_mail(24)})
    result = sync_mail(engine, settings, service, now=lambda: NOW)
    assert result.since == NOW - timedelta(days=settings.fetch_days_back)
    assert [m.id for m in result.messages] == ["m1"]
    assert last_sync(engine) == NOW

    later = NOW + timedelta(days=1)
    second = sync_mail(engine, settings, service, now=lambda: later)
    assert second.since == NOW - gmail.SYNC_OVERLAP
    assert f"after:{int(second.since.timestamp())}" in service.queries[-1]
    assert last_sync(engine) == later


def test_sync_failure_keeps_previous_state(engine, settings):
    with pytest.raises(GmailError, match="Nie udało się"):
        sync_mail(engine, settings, FakeService({}, fail=True), now=lambda: NOW)
    assert last_sync(engine) is None


def test_credentials_missing_token_requires_login(settings):
    with pytest.raises(GmailAuthRequired):
        load_credentials(settings, interactive=False)


def test_credentials_missing_client_file_has_clear_message(settings):
    with pytest.raises(GmailError, match="credentials.json"):
        load_credentials(settings, interactive=True)


def test_valid_token_is_used_without_browser(settings):
    settings.token_path.parent.mkdir(parents=True, exist_ok=True)
    expiry = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
    settings.token_path.write_text(
        json.dumps(
            {
                "token": "t",
                "refresh_token": "r",
                "client_id": "c",
                "client_secret": "s",
                "scopes": gmail.SCOPES,
                "expiry": expiry.isoformat() + "Z",
            }
        )
    )
    creds = load_credentials(settings, interactive=False)
    assert creds.valid


def test_save_token_is_private(settings):
    class Creds:
        def to_json(self):
            return "{}"

    gmail._save_token(Creds(), settings)
    mode = stat.S_IMODE(settings.token_path.stat().st_mode)
    assert mode == 0o600


def test_cli_gmail_check_prints_offers(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CV_APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(gmail, "load_credentials", lambda s, interactive: object())
    monkeypatch.setattr(gmail, "build_service", lambda creds: FakeService({"m1": raw_mail(24)}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--gmail-check"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "(3 ofert)" in out
    assert "Administrator Linux (K/M/X) | Firma Testowa sp. z o.o. | Katowice, Gliwice" in out
