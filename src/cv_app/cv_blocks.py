"""Schemat bloków CV: JSON niezależny od wyglądu.

Bloki wypełnia generator (AI wybiera i przeformułowuje treść z profilu), a szablon HTML
tylko je wyświetla. Dane kontaktowe (telefon, e-mail, adres, linki) nie są częścią
bloków: wstawiamy je lokalnie przy renderze, więc nie trafiają do promptu.

Limity mają dwa poziomy: ``TARGET`` to długości, o które prosimy AI, a ``HARD_MAX``
to twarda granica schematu (z zapasem na „~” z planu). Dopasowanie do jednej strony
mierzy osobno Playwright.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Language = Literal["pl", "en"]


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: int
    summary: int
    skills_total: int
    skill_group_name: int
    skill: int
    projects: int
    project_name: int
    tags: int
    tag: int
    points_min: int
    points_max: int
    point: int


TARGET = Limits(
    title=35,
    summary=650,
    skills_total=16,
    skill_group_name=28,
    skill=34,
    projects=3,
    project_name=24,
    tags=4,
    tag=18,
    points_min=3,
    points_max=4,
    point=90,
)

HARD_MAX = Limits(
    title=40,
    summary=720,
    skills_total=16,
    skill_group_name=32,
    skill=40,
    projects=3,
    project_name=30,
    tags=5,
    tag=22,
    points_min=2,  # krótkie projekty (np. terraform-3tier) mają w profilu tylko 2 punkty
    points_max=4,
    point=110,
)


def _strip(value: str) -> str:
    return " ".join(value.split())


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillGroupBlock(_Block):
    name: str = Field(max_length=HARD_MAX.skill_group_name)
    skills: list[str] = Field(min_length=1)

    @field_validator("skills")
    @classmethod
    def _check_skills(cls, value: list[str]) -> list[str]:
        cleaned = [_strip(s) for s in value if s.strip()]
        too_long = [s for s in cleaned if len(s) > HARD_MAX.skill]
        if too_long:
            raise ValueError(f"za długie umiejętności: {too_long}")
        return cleaned


class ProjectBlock(_Block):
    name: str = Field(max_length=HARD_MAX.project_name)
    link: str | None = Field(
        default=None, description="Adres pokazywany przy nazwie, np. ytlottery.pl"
    )
    tags: list[str] = Field(default_factory=list, max_length=HARD_MAX.tags)
    points: list[str] = Field(min_length=HARD_MAX.points_min, max_length=HARD_MAX.points_max)

    @field_validator("points")
    @classmethod
    def _check_points(cls, value: list[str]) -> list[str]:
        cleaned = [_strip(p) for p in value]
        too_long = [p for p in cleaned if len(p) > HARD_MAX.point]
        if too_long:
            raise ValueError(f"za długie punkty (>{HARD_MAX.point} znaków): {too_long}")
        return cleaned

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: list[str]) -> list[str]:
        cleaned = [_strip(t) for t in value]
        if any(len(t) > HARD_MAX.tag for t in cleaned):
            raise ValueError(f"za długi tag (>{HARD_MAX.tag} znaków)")
        return cleaned


class EducationBlock(_Block):
    school: str
    subtitle: str | None = None  # np. "Profil: informatyka (IT)"
    items: list[str] = Field(default_factory=list, max_length=4)


class LanguageBlock(_Block):
    name: str
    level: str


class CVBlocks(_Block):
    language: Language
    title: str = Field(max_length=HARD_MAX.title)
    summary: str = Field(max_length=HARD_MAX.summary)
    skill_groups: list[SkillGroupBlock] = Field(min_length=1, max_length=5)
    projects: list[ProjectBlock] = Field(min_length=1, max_length=HARD_MAX.projects)
    education: list[EducationBlock] = Field(min_length=1, max_length=2)
    languages: list[LanguageBlock] = Field(min_length=1, max_length=4)

    @field_validator("title", "summary")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _strip(value)

    @model_validator(mode="after")
    def _check_totals(self) -> CVBlocks:
        total = sum(len(g.skills) for g in self.skill_groups)
        if total > HARD_MAX.skills_total:
            raise ValueError(f"za dużo umiejętności: {total} (maks. {HARD_MAX.skills_total})")
        return self

    @property
    def skills_count(self) -> int:
        return sum(len(g.skills) for g in self.skill_groups)


def blocks_json_schema() -> dict:
    """Schemat JSON bloków (dla ``run_claude``)."""
    return CVBlocks.model_json_schema()
