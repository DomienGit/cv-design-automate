from __future__ import annotations


def test_panel_renders_tabs(client):
    r = client.get("/")
    assert r.status_code == 200
    for label in ("Oferty", "Gotowe CV", "Ustawienia"):
        assert label in r.text


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["offers"] == 0


def test_settings_endpoint(client, settings):
    data = client.get("/api/settings").json()
    assert data["senders"] == settings.senders
    assert data["gmail_credentials_present"] is False


def test_static_css(client):
    assert client.get("/static/style.css").status_code == 200
