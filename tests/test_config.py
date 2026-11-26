from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cv_app.config import Settings


def test_defaults_are_local_and_outside_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.notes_dir == Path("profil")
    assert s.senders == [
        "jobalert@wysylka.pracuj.pl",
        "no-reply@justjoin.it",
        "notifications@nofluffjobs.com",
        "jobalert@mailing.theprotocol.it",
    ]
    assert s.gmail_credentials_path == tmp_path / ".config/cv-app/credentials.json"
    assert s.token_path == tmp_path / ".local/share/cv-app/token.json"
    assert s.database_path == tmp_path / ".local/share/cv-app/cv-app.db"


def test_env_overrides(monkeypatch, tmp_path):
    creds = tmp_path / "secrets" / "credentials.json"
    monkeypatch.setenv("CV_APP_GMAIL_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv("CV_APP_SENDERS", '[" JobAlert@Wysylka.Pracuj.pl ", "noreply@justjoin.it"]')
    monkeypatch.setenv("CV_APP_PORT", "9000")
    s = Settings()
    assert s.gmail_credentials_path == creds
    assert s.senders == ["jobalert@wysylka.pracuj.pl", "noreply@justjoin.it"]
    assert s.panel_url == "http://127.0.0.1:9000/"


def test_dotenv_file_is_read(tmp_path):
    (tmp_path / ".env").write_text("CV_APP_NOTES_DIR=~/notatki/Rekrutacja\n", encoding="utf-8")
    s = Settings()
    assert s.notes_dir == Path.home() / "notatki/Rekrutacja"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_host_must_be_loopback(host):
    with pytest.raises(ValidationError):
        Settings(host=host)


def test_public_view_has_no_secret_contents(settings):
    creds = settings.gmail_credentials_path
    creds.parent.mkdir(parents=True)
    creds.write_text('{"installed": {"client_secret": "TOP-SECRET"}}', encoding="utf-8")
    view = settings.public_view()
    assert view["gmail_credentials_present"] is True
    assert view["gmail_token_present"] is False
    assert "TOP-SECRET" not in repr(view)


def test_secrets_are_gitignored():
    gitignore = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("credentials.json", "token.json", ".env", "profil/"):
        assert pattern in gitignore.splitlines()
