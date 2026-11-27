from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from cv_app.config import Settings
from cv_app.cv_blocks import HARD_MAX, CVBlocks, blocks_json_schema
from cv_app.profile import load_profile
from cv_app.render import (
    RenderContext,
    RenderError,
    build_context,
    contact_items,
    render_html,
    render_pdf,
)

FIXTURES = Path(__file__).parent / "fixtures" / "profile_sample"


def make_blocks(**overrides) -> dict:
    data = {
        "language": "pl",
        "title": "Administrator systemów",
        "summary": "Technik informatyk <testowy> & administrator.",
        "skill_groups": [
            {"name": "Linux", "skills": ["Linux: systemd, SSH", "Bash"]},
            {"name": "Kontenery", "skills": ["Docker", "podstawy Kubernetes"]},
        ],
        "projects": [
            {
                "name": "Faktury AI",
                "link": "faktury.example.com",
                "tags": ["Docker Compose", "Caddy"],
                "points": ["Punkt pierwszy", "Punkt drugi", "Punkt trzeci"],
            }
        ],
        "education": [
            {"school": "Technikum Testowe", "subtitle": "Profil: informatyka", "items": ["E.12"]}
        ],
        "languages": [
            {"name": "Polski", "level": "ojczysty"},
            {"name": "Angielski", "level": "B1"},
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture
def profile_settings(settings: Settings) -> Settings:
    shutil.copytree(FIXTURES, settings.notes_dir)
    return settings


def test_blocks_limits():
    CVBlocks.model_validate(make_blocks())
    with pytest.raises(ValidationError):
        CVBlocks.model_validate(make_blocks(title="x" * (HARD_MAX.title + 1)))
    too_many = [{"name": "A", "skills": [f"s{i}" for i in range(HARD_MAX.skills_total + 1)]}]
    with pytest.raises(ValidationError, match="za dużo umiejętności"):
        CVBlocks.model_validate(make_blocks(skill_groups=too_many))
    long_point = make_blocks()
    long_point["projects"][0]["points"][0] = "x" * (HARD_MAX.point + 1)
    with pytest.raises(ValidationError, match="za długie punkty"):
        CVBlocks.model_validate(long_point)
    with pytest.raises(ValidationError):
        CVBlocks.model_validate(make_blocks(language="de"))
    with pytest.raises(ValidationError):
        CVBlocks.model_validate(make_blocks(extra_field=1))


def test_json_schema_is_plain_dict():
    schema = blocks_json_schema()
    assert schema["type"] == "object"
    assert "projects" in schema["required"]


def test_contact_items_hide_postal_code(profile_settings):
    profile = load_profile(profile_settings)
    items = contact_items(profile.contact)
    texts = [i.text for i in items]
    assert "Testowo, Polska" in texts
    assert "github.com/example" in texts
    assert items[0].href == "tel:+48500000000"


def test_render_html_pl(profile_settings):
    profile = load_profile(profile_settings)
    (profile_settings.notes_dir / "zdjecie.png").write_bytes(b"\x89PNG fake")
    ctx = build_context(profile, profile_settings)
    assert ctx.photo_data_uri.startswith("data:image/png;base64,")
    html = render_html(CVBlocks.model_validate(make_blocks()), ctx)
    assert '<html lang="pl">' in html
    assert "Jan Testowy" in html
    assert "Podsumowanie" in html and "RODO" in html
    assert "&lt;testowy&gt; &amp; administrator" in html  # autoescape
    assert "faktury.example.com ↗" in html
    assert "@font-face" in html


def test_render_html_en_uses_english_labels():
    blocks = CVBlocks.model_validate(make_blocks(language="en"))
    html = render_html(blocks, RenderContext(name="Jan Testowy"))
    assert '<html lang="en">' in html
    assert "Projects" in html and "GDPR" in html
    assert "RODO" not in html and "Podsumowanie" not in html


def test_photo_skipped_when_profile_says_no(profile_settings):
    about = profile_settings.notes_dir / "O mnie.md"
    about.write_text(
        about.read_text(encoding="utf-8").replace("Zdjęcie: tak", "Zdjęcie: nie"), "utf-8"
    )
    (profile_settings.notes_dir / "zdjecie.jpg").write_bytes(b"x")
    ctx = build_context(load_profile(profile_settings), profile_settings)
    assert ctx.photo_data_uri is None


def test_render_pdf_one_page(tmp_path, settings, monkeypatch):
    """Prawdziwy Chromium: pomijany, gdy go nie ma (np. w CI bez playwright install).

    Własną przeglądarkę można wskazać zmienną TEST_CHROMIUM_EXECUTABLE.
    """
    if os.environ.get("TEST_CHROMIUM_EXECUTABLE"):
        settings = settings.model_copy(
            update={"chromium_executable": Path(os.environ["TEST_CHROMIUM_EXECUTABLE"])}
        )
    html = render_html(CVBlocks.model_validate(make_blocks()), RenderContext(name="Jan Testowy"))
    out = tmp_path / "cv.pdf"
    try:
        render_pdf(html, out, settings)
    except RenderError as exc:
        pytest.skip(f"brak Chromium: {exc}")
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    assert b"/Count 1" in data  # jedna strona A4
