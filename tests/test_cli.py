from __future__ import annotations

from cv_app import cli


def test_main_runs_on_loopback_without_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("CV_APP_DATA_DIR", str(tmp_path / "data"))
    calls = {}

    def fake_run(app, host, port, **kwargs):
        calls.update(host=host, port=port)

    opened = []
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    cli.main(["--no-browser", "--port", "8899"])

    assert calls == {"host": "127.0.0.1", "port": 8899}
    assert opened == []
