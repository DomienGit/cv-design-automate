from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_render import make_blocks

from cv_app.cv_blocks import CVBlocks
from cv_app.fit import PX_PER_MM, FitMeter, report_from_measurement
from cv_app.render import RenderContext, RenderError, render_html


def test_report_from_measurement_overflow():
    data = {
        "main": {"scroll": 1123 + 20 * PX_PER_MM, "client": 1123, "free": 0},
        "side": {"scroll": 1123, "client": 1123, "free": 30 * PX_PER_MM},
        "blocks": {"summary": 40 * PX_PER_MM},
        "projects": [100, 300, 200],
        "titleLines": 1,
    }
    report = report_from_measurement(data)
    assert not report.fits
    assert report.main.overflow_mm == pytest.approx(20, abs=0.2)
    assert report.side.fits and report.side.free_mm == pytest.approx(30, abs=0.2)
    assert report.blocks_mm["summary"] == pytest.approx(40, abs=0.2)
    problems = report.problems()
    assert len(problems) == 1 and "20 mm" in problems[0] and "nr 2" in problems[0]


def test_report_fits_and_title_wrap():
    ok = {"main": {"scroll": 1000, "client": 1000.5}, "side": {"scroll": 900, "client": 900}}
    assert report_from_measurement(ok).fits
    wrapped = report_from_measurement({**ok, "titleLines": 2})
    assert not wrapped.fits
    assert "Tytuł" in wrapped.problems()[0]


@pytest.fixture
def meter_settings(settings):
    if os.environ.get("TEST_CHROMIUM_EXECUTABLE"):
        return settings.model_copy(
            update={"chromium_executable": Path(os.environ["TEST_CHROMIUM_EXECUTABLE"])}
        )
    return settings


def test_measure_real_browser(meter_settings):
    """Prawdziwy Chromium: pomijany, gdy go nie ma (TEST_CHROMIUM_EXECUTABLE wskazuje własny)."""
    short = render_html(CVBlocks.model_validate(make_blocks()), RenderContext(name="Jan Testowy"))
    huge_html = short.replace("<p>", "<p>" + "Dodatkowy tekst wypełniający. " * 150, 1)
    try:
        with FitMeter(meter_settings) as meter:
            ok = meter.measure(short)
            bad = meter.measure(huge_html)
    except RenderError as exc:
        pytest.skip(f"brak Chromium: {exc}")
    assert ok.fits, ok
    assert ok.main.free_mm > 10
    assert ok.blocks_mm["summary"] > 0 and len(ok.projects_mm) == 1
    assert not bad.fits
    assert bad.main.overflow_mm > 5
