from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cv_app.app import create_app
from cv_app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Testy nie czytają prawdziwego .env ani zmiennych CV_APP_* z maszyny."""
    import os

    for key in list(os.environ):
        if key.startswith("CV_APP_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        notes_dir=tmp_path / "profil",
        cv_output_dir=tmp_path / "cv",
        gmail_credentials_path=tmp_path / "config" / "credentials.json",
        open_browser=False,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_real_claude():
    """Testy nigdy nie wołają prawdziwego `claude`; potrzebna atrapa → FakeClaude."""
    from cv_app.claude_client import use_runner

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Test próbował wywołać prawdziwego claude; użyj FakeClaude")

    with use_runner(forbidden):
        yield
