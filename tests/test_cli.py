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


def test_cv_preview_end_to_end(monkeypatch, settings, capsys):
    """Oferta z bazy → generator (atrapa Claude) → pomiar i PDF (atrapy przeglądarki)."""
    import shutil
    from pathlib import Path

    from sqlmodel import Session
    from test_generator import answer

    from cv_app.claude_client import FakeClaude, use_runner
    from cv_app.db import init_db, make_engine
    from cv_app.fit import ColumnFit, FitReport
    from cv_app.models import Offer

    shutil.copytree(Path(__file__).parent / "fixtures" / "profile_sample", settings.notes_dir)
    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        session.add(Offer(source="pracuj", external_id="1", title="Junior DevOps", id=5))
        session.commit()
    engine.dispose()

    report = FitReport(
        main=ColumnFit(overflow_mm=0, free_mm=12), side=ColumnFit(overflow_mm=0, free_mm=3)
    )
    monkeypatch.setattr("cv_app.fit.measure_fit", lambda html, s: report)
    written = []
    monkeypatch.setattr("cv_app.render.render_pdf", lambda html, out, s: written.append(out))

    with use_runner(FakeClaude([answer()])):
        assert cli.cv_preview(settings, 5) == 0
    out = capsys.readouterr().out
    assert "Wariant: DevOps / Cloud" in out and "infra-demo" in out
    assert "wolne miejsce: 12 mm" in out
    assert written and written[0].name == "cv-oferta-5.pdf"

    assert cli.cv_preview(settings, 99) == 1
