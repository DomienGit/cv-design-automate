from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cv_app.claude_client import ClaudeError, FakeClaude, use_runner
from cv_app.cv_blocks import CVBlocks
from cv_app.generator import (
    build_prompt,
    candidate_projects,
    cv_from_result,
    experience_claims,
    filter_tags,
    generate_blocks,
    pick_variant,
    project_link,
    response_model,
    sanitize_source_text,
)
from cv_app.models import CVStatus, Offer
from cv_app.profile import load_profile

FIXTURES = Path(__file__).parent / "fixtures" / "profile_sample"


@pytest.fixture
def profile(settings):
    shutil.copytree(FIXTURES, settings.notes_dir)
    about = settings.notes_dir / "O mnie.md"
    about.write_text(
        about.read_text(encoding="utf-8").replace(
            "Technik informatyk znający Windows i Linuxa.",
            "Technik informatyk znający Windows i Linuxa. Naprawiam komputery, "
            "pomagając współpracownikom i znajomym.",
        ),
        encoding="utf-8",
    )
    return load_profile(settings)


@pytest.fixture
def quick(settings):
    return settings.model_copy(update={"claude_retries": 0})


def offer(**kw) -> Offer:
    data = {
        "source": "pracuj",
        "external_id": "1",
        "title": "Junior DevOps Engineer",
        "company": "Firma",
        "category": "devops",
        "language": "pl",
        "requirements": ["Docker", "Terraform", "Azure"],
        "responsibilities": ["Utrzymanie infrastruktury"],
        "technologies": ["Docker", "AWS"],
    }
    data.update(kw)
    return Offer(**data)


def answer(**kw) -> dict:
    data = {
        "title": "Junior DevOps Engineer",
        "summary": "Programista Python rozwijający się w kierunku DevOps.",
        "skill_groups": [
            {"name": "DevOps", "skills": ["Docker", "AWS (EC2, VPC)", "podstawy Kubernetes"]},
            {"name": "Języki", "skills": ["Python", "Bash"]},
        ],
        "projects": [
            {
                "key": "infra-demo",
                "tags": ["AWS", "Terraform", "Ansible"],
                "points": ["Odtworzyłem architekturę 3-warstwową", "Całość w Terraformie"],
            },
            {
                "key": "Faktury AI",
                "tags": ["Docker Compose", "Python"],
                "points": ["Zbudowałem z narzędziami AI system", "Wdrożenie na VPS", "Flask"],
            },
        ],
        "education": [
            {"school": "Technikum Testowe", "subtitle": "Profil informatyczny", "items": ["E.12"]}
        ],
    }
    data.update(kw)
    return data


def test_pick_variant_by_category(profile):
    assert pick_variant(profile, "devops").name == "DevOps / Cloud"
    assert pick_variant(profile, "helpdesk").name == "Helpdesk / Wsparcie IT"
    assert pick_variant(profile, None).name == "DevOps / Cloud"  # pierwszy wariant


def test_candidate_projects_priority_and_exclusions(profile):
    keys = [c.project.key for c in candidate_projects(profile, "helpdesk")]
    assert keys == ["Faktury AI", "infra-demo"]  # sieciak nie istnieje, reszta bez punktów
    first = candidate_projects(profile, "helpdesk")[0]
    assert first.priority == 1 and first.hint == "monitoring, runbook"
    devops = candidate_projects(profile, "devops")
    assert [c.project.key for c in devops] == ["infra-demo", "Faktury AI"]
    assert all(c.project.key not in ("infra-stare", "sieciak-api") for c in devops)


def test_sanitize_removes_coworkers():
    text = "Naprawiam komputery, pomagając współpracownikom i znajomym."
    assert sanitize_source_text(text) == "Naprawiam komputery, pomagając znajomym."
    assert sanitize_source_text("pomagam znajomym i współpracownikom.") == "pomagam znajomym."
    assert experience_claims("Pomagam współpracownikom") == ["współpracowni"]
    assert experience_claims("I helped my colleagues at work") == ["colleague", "at work"]


def test_prompt_has_rules_and_no_contact_data(profile, settings):
    cands = candidate_projects(profile, "helpdesk")
    prompt = build_prompt(
        profile,
        offer(category="helpdesk", title="Helpdesk"),
        language="pl",
        variant=pick_variant(profile, "helpdesk"),
        candidates=cands,
    )
    for private in ("+48 500", "jan@example.com", "Testowo", "github.com/example", "Jan Testowy"):
        assert private not in prompt
    assert "współpracownik" not in prompt.split("## Profil: tekst bazowy")[1].split("##")[0]
    assert "pomagając znajomym" in prompt
    assert "Azure: nigdy nie wpisywać do CV" in prompt  # reguły z profilu
    assert "AI-asystę podawać wprost" in prompt
    assert "priorytet 1 dla tej oferty; akcent: monitoring, runbook" in prompt
    assert "Liczba użytkowników" not in prompt  # DO UZUPEŁNIENIA pominięte
    assert "dokładnie 2 różne projekty" in prompt
    assert "najwyżej 35 znaków" in prompt and "najwyżej 650 znaków" in prompt


def test_prompt_english_and_feedback(profile):
    prompt = build_prompt(
        profile,
        offer(language="en"),
        language="en",
        variant=pick_variant(profile, "devops"),
        candidates=candidate_projects(profile, "devops"),
        feedback=["Skróć podsumowanie o 100 znaków."],
    )
    assert "całe CV po angielsku" in prompt and "fluent" in prompt
    assert "„basics” / „theory”" in prompt
    assert "Poprawki do poprzedniej wersji" in prompt and "Skróć podsumowanie" in prompt


def test_response_model_restricts_project_keys():
    model = response_model(["a", "b"])
    schema = model.model_json_schema()
    key = schema["$defs"]["GeneratedProject"]["properties"]["key"]
    assert key["enum"] == ["a", "b"]
    with pytest.raises(ValueError):
        model.model_validate(answer(projects=[{"key": "c", "tags": [], "points": ["x", "y"]}]))


def test_filter_tags_only_from_stack(profile):
    faktury = profile.project("Faktury AI")
    assert filter_tags(["Docker Compose", "Python", "Kubernetes"], faktury) == [
        "Docker Compose",
        "Python",
    ]
    assert filter_tags(["Kubernetes"], faktury) == ["Python 3.12", "Flask", "Docker Compose"]
    assert project_link(faktury) == "faktury.example.com"
    assert project_link(profile.project("infra-demo")) == "github.com/example/infra"


def test_generate_blocks_pl(profile, quick):
    fake = FakeClaude([answer()])
    with use_runner(fake):
        result = generate_blocks(profile, offer(), settings=quick)
    blocks = result.blocks
    assert isinstance(blocks, CVBlocks) and blocks.language == "pl"
    assert result.variant == "DevOps / Cloud" and result.category == "devops"
    infra, faktury = blocks.projects
    assert infra.name == "infra-demo" and infra.link == "github.com/example/infra"
    assert infra.tags == ["AWS", "Terraform"]  # Ansible spoza stacku wycięty
    assert faktury.link == "faktury.example.com"
    assert [(lang.name, lang.level) for lang in blocks.languages] == [
        ("Polski", "ojczysty"),
        ("Angielski", "B1"),
    ]
    assert not result.warnings
    prompt, schema = fake.calls[0]
    assert "Junior DevOps Engineer" in prompt
    assert schema["$defs"]["GeneratedProject"]["properties"]["key"]["enum"] == [
        "infra-demo",
        "Faktury AI",
    ]
    cv = cv_from_result(offer(id=7), result, profile)
    assert cv.status == CVStatus.DRAFT and cv.variant == "DevOps / Cloud"
    assert cv.blocks["projects"][0]["name"] == "infra-demo"
    assert cv.profile_version == profile.version


def test_generate_blocks_en_languages(profile, quick):
    with use_runner(FakeClaude([answer()])):
        result = generate_blocks(profile, offer(language="en"), settings=quick)
    assert result.blocks.language == "en"
    assert [(lang.name, lang.level) for lang in result.blocks.languages] == [
        ("Polish", "native"),
        ("English", "B1"),
    ]


def test_generate_blocks_category_from_title(profile, quick):
    fake = FakeClaude([answer()])
    with use_runner(fake):
        result = generate_blocks(
            profile, offer(category=None, title="Specjalista Helpdesk"), settings=quick
        )
    assert result.category == "helpdesk" and result.variant == "Helpdesk / Wsparcie IT"


def test_generate_blocks_warns_about_experience(profile, quick):
    bad = answer(summary="Pomagałem współpracownikom w poprzedniej firmie.")
    with use_runner(FakeClaude([bad])):
        result = generate_blocks(profile, offer(), settings=quick)
    assert result.warnings and "współpracowni" in result.warnings[0]


def test_generate_blocks_duplicate_project(profile, quick):
    dup = answer()
    dup["projects"][1]["key"] = "infra-demo"
    with use_runner(FakeClaude([dup])):
        result = generate_blocks(profile, offer(), settings=quick)
    assert len(result.blocks.projects) == 1 and "powtórzyło" in result.warnings[0]


def test_generate_blocks_invalid_answer_raises(profile, quick):
    with use_runner(FakeClaude([answer(title="x" * 100)])), pytest.raises(ClaudeError):
        generate_blocks(profile, offer(), settings=quick)
