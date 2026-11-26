"""Ustawienia aplikacji.

Wartości są czytane (od najważniejszych) ze zmiennych środowiskowych z prefiksem
``CV_APP_``, z pliku ``.env`` w katalogu roboczym i z wartości domyślnych poniżej.
Listy podaje się jako JSON, np. ``CV_APP_SENDERS='["jobalert@wysylka.pracuj.pl"]'``.

Sekrety (plik ``credentials.json`` z Google Cloud i ``token.json`` z OAuth) nigdy nie
trafiają do repozytorium: aplikacja zna tylko ich ścieżki.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _xdg_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / fallback


def default_data_dir() -> Path:
    """Katalog na bazę SQLite, cache i token OAuth (poza repozytorium)."""
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "cv-app"


def default_config_dir() -> Path:
    """Katalog na pliki konfiguracyjne, m.in. credentials.json z Google Cloud."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "cv-app"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CV_APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Serwer: panel działa tylko lokalnie.
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    open_browser: bool = True

    # Ścieżki danych.
    data_dir: Path = Field(default_factory=default_data_dir)
    db_path: Path | None = None  # domyślnie <data_dir>/cv-app.db

    # Profil: folder z plikami O mnie.md i Projekty.md. Domyślnie profil/ w katalogu
    # projektu (folder jest w .gitignore, bo repozytorium jest publiczne).
    notes_dir: Path = Path("profil")
    profile_about_file: str = "O mnie.md"
    profile_projects_file: str = "Projekty.md"

    # Zdjęcie do CV; domyślnie zdjecie.jpg/.png (albo photo.*) w folderze profilu.
    photo_path: Path | None = None

    # Chromium dla Playwrighta; domyślnie przeglądarka z `playwright install chromium`.
    chromium_executable: Path | None = None

    # Claude Code w trybie nieinteraktywnym (`claude -p`) na koncie claude.ai.
    claude_command: str = "claude"
    claude_model: str | None = None  # np. "sonnet"; domyślnie model z konta
    claude_timeout: int = Field(default=300, ge=10)  # sekundy na jedno wywołanie
    claude_retries: int = Field(default=2, ge=0, le=5)  # ponowienia po błędzie

    # Folder, do którego zapisujemy gotowe CV, i wzór nazwy pliku.
    cv_output_dir: Path = Field(default_factory=lambda: Path.home() / "CV")
    cv_filename_pattern: str = "CV_Dominik_{company}_{position}_{date}.pdf"

    # Poczta: nadawcy, od których pobieramy oferty.
    senders: list[str] = Field(default_factory=lambda: ["jobalert@wysylka.pracuj.pl"])
    fetch_days_back: int = Field(default=7, ge=1)

    # Filtr trafności: oferty z tymi słowami w tytule są odrzucane (poziom stanowiska).
    relevance_exclude_keywords: list[str] = Field(
        default_factory=lambda: [
            "senior",
            "sr",
            "starszy",
            "lead",
            "leader",
            "principal",
            "staff",
            "head of",
            "architect",
            "architekt",
            "manager",
            "kierownik",
            "dyrektor",
            "director",
            "ekspert",
            "expert",
        ]
    )

    # Strony ofert: odstęp między pobraniami (s), ważność cache (dni), limit czasu (s).
    offer_fetch_delay: float = Field(default=2.0, ge=0)
    offer_cache_days: int = Field(default=14, ge=0)
    offer_fetch_timeout: float = Field(default=20.0, ge=1)

    # Gmail OAuth: pliki trzymane poza repozytorium.
    gmail_credentials_path: Path = Field(
        default_factory=lambda: default_config_dir() / "credentials.json"
    )
    gmail_token_path: Path | None = None  # domyślnie <data_dir>/token.json

    @field_validator("host")
    @classmethod
    def _host_must_be_loopback(cls, value: str) -> str:
        if value == "localhost":
            return value
        try:
            is_loopback = ipaddress.ip_address(value).is_loopback
        except ValueError as exc:
            raise ValueError(f"host musi być adresem IP lub 'localhost', jest: {value!r}") from exc
        if not is_loopback:
            raise ValueError(
                f"Panel może nasłuchiwać tylko lokalnie (np. 127.0.0.1), a nie na {value!r}"
            )
        return value

    @field_validator("senders")
    @classmethod
    def _normalize_senders(cls, value: list[str]) -> list[str]:
        return [s.strip().lower() for s in value if s.strip()]

    @field_validator(
        "data_dir",
        "notes_dir",
        "cv_output_dir",
        "gmail_credentials_path",
        "gmail_token_path",
        "photo_path",
        "chromium_executable",
    )
    @classmethod
    def _expand_user(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @property
    def database_path(self) -> Path:
        return (self.db_path or self.data_dir / "cv-app.db").expanduser()

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"

    @property
    def token_path(self) -> Path:
        return (self.gmail_token_path or self.data_dir / "token.json").expanduser()

    @property
    def panel_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def ensure_dirs(self) -> None:
        """Tworzy katalogi danych potrzebne przy starcie."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def public_view(self) -> dict[str, object]:
        """Ustawienia do pokazania w panelu: ścieżki i stan, bez treści sekretów."""
        return {
            "notes_dir": str(self.notes_dir),
            "profile_files_present": all(
                (self.notes_dir / name).is_file()
                for name in (self.profile_about_file, self.profile_projects_file)
            ),
            "cv_output_dir": str(self.cv_output_dir),
            "senders": list(self.senders),
            "fetch_days_back": self.fetch_days_back,
            "database_path": str(self.database_path),
            "gmail_credentials_path": str(self.gmail_credentials_path),
            "gmail_credentials_present": self.gmail_credentials_path.is_file(),
            "gmail_token_present": self.token_path.is_file(),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
