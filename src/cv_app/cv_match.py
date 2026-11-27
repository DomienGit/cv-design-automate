"""Decyzja, czy do oferty pasuje gotowe CV z biblioteki: użyć, edytować czy zrobić nowe.

Najpierw skrypt (:mod:`cv_app.cv_library`) liczy pokrycie technologii i wybiera kilka
najlepszych CV; z progów wychodzi propozycja decyzji. AI (``run_claude``) dostaje tylko
te kilka CV (tekst bez danych kontaktowych) i potwierdza albo zmienia decyzję. Gdy AI
nie odpowie, zostaje decyzja skryptu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from cv_app.claude_client import ClaudeError, run_claude
from cv_app.config import Settings
from cv_app.cv_library import ACTION_LABELS, Action, Candidate, decide_by_score
from cv_app.generator import _offer_section
from cv_app.models import Offer

CV_TEXT_LIMIT = 3000  # znaków tekstu jednego CV w prompcie


class AIDecision(BaseModel):
    action: Literal["use", "edit", "new"]
    cv: str | None = Field(
        default=None, description="Klucz wybranego CV z listy (dla use i edit), dla new null."
    )
    reason: str = Field(max_length=400, description="Krótko po polsku: dlaczego.")


@dataclass
class Decision:
    action: Action
    candidate: Candidate | None
    reason: str
    by_ai: bool
    script_action: Action
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ACTION_LABELS[self.action]


PROMPT = """\
Decydujesz, czy do oferty pracy pasuje któreś z gotowych CV kandydata. Odpowiadasz
wyłącznie danymi w podanym schemacie JSON.

Możliwości:
- use: CV pasuje do oferty prawie w całości, wysyłamy je bez zmian,
- edit: CV jest dobrą bazą, ale trzeba przesunąć akcenty (tytuł, podsumowanie,
  kolejność umiejętności, inny projekt) albo przetłumaczyć na język oferty,
- new: żadne CV nie pasuje, tworzymy nowe z profilu.

Zasady:
- Liczy się dopasowanie stanowiska i technologii, a nie długość CV.
- CV w innym języku niż oferta nie może być „use” (najwyżej „edit”).
- Brakujące technologie, których kandydat nie zna, nie są powodem do edycji: CV
  i tak ich nie dopisze. Edycja ma sens, gdy w CV brakuje akcentów, które kandydat ma.
- Powód napisz krótko, po polsku, w jednym–dwóch zdaniach.

## Oferta (język: {offer_language})
{offer}

## Propozycja skryptu
Pokrycie technologii liczone słowami kluczowymi; progi: od {use_t}% use, od {edit_t}%
edit, niżej new. Skrypt proponuje: {script_action}{script_cv}.

## Gotowe CV (najlepsze według skryptu)
{candidates}
"""


def _candidate_section(c: Candidate, private: list[str]) -> str:
    from cv_app.cv_library import anonymize

    text = anonymize(c.cv.text, private)
    if len(text) > CV_TEXT_LIMIT:
        text = text[:CV_TEXT_LIMIT] + " […]"
    lines = [
        f"### {c.cv.key}",
        f"Podfolder: {c.cv.folder or '(główny)'}; język CV: {c.cv.language}; "
        f"wynik: {c.score}% (pokrycie {c.coverage}%"
        + ("; podfolder pasuje do kategorii oferty)" if c.category_match else ")"),
        "Pasujące technologie: " + (", ".join(c.matched) or "brak"),
        "Brakujące technologie: " + (", ".join(c.missing) or "brak"),
        "Tekst CV:",
        text,
    ]
    return "\n".join(lines)


def build_prompt(
    offer: Offer,
    candidates: list[Candidate],
    *,
    script_action: Action,
    settings: Settings,
    private: list[str] | None = None,
) -> str:
    top = candidates[0] if candidates else None
    return PROMPT.format(
        offer_language="en" if offer.language == "en" else "pl",
        offer=_offer_section(offer),
        use_t=settings.cv_match_use_threshold,
        edit_t=settings.cv_match_edit_threshold,
        script_action=script_action,
        script_cv=f" ({top.cv.key}, {top.score}%)" if top and script_action != "new" else "",
        candidates="\n\n".join(_candidate_section(c, private or []) for c in candidates),
    )


def _language_ok(candidate: Candidate, offer: Offer) -> bool:
    return candidate.cv.language == ("en" if offer.language == "en" else "pl")


def script_decision(candidates: list[Candidate], offer: Offer, settings: Settings) -> Decision:
    """Decyzja z samych progów (bez AI)."""
    if not candidates:
        return Decision("new", None, "Brak gotowych CV w bibliotece.", False, "new")
    top = candidates[0]
    action = decide_by_score(
        top.score,
        use_threshold=settings.cv_match_use_threshold,
        edit_threshold=settings.cv_match_edit_threshold,
    )
    reason = f"Pokrycie technologii {top.score}% ({top.cv.key})."
    if action == "use" and not _language_ok(top, offer):
        action = "edit"
        reason += " CV jest w innym języku niż oferta, więc do przetłumaczenia."
    return Decision(action, top if action != "new" else None, reason, False, action)


def decide(
    candidates: list[Candidate],
    offer: Offer,
    settings: Settings,
    *,
    private: list[str] | None = None,
) -> Decision:
    """Decyzja skryptu potwierdzona albo zmieniona przez AI (gdy są kandydaci)."""
    base = script_decision(candidates, offer, settings)
    if not candidates:
        return base
    prompt = build_prompt(
        offer, candidates, script_action=base.action, settings=settings, private=private
    )
    try:
        answer = run_claude(prompt, AIDecision, settings=settings)
    except ClaudeError as exc:
        base.notes.append(f"AI nie odpowiedziało ({exc}); zostaje decyzja skryptu.")
        return base

    by_key = {c.cv.key: c for c in candidates}
    action: Action = answer.action
    chosen = by_key.get(answer.cv or "") if action != "new" else None
    notes: list[str] = []
    if action != "new" and chosen is None:
        chosen = candidates[0]
        notes.append(f"AI wskazało nieznane CV „{answer.cv}”; biorę {chosen.cv.key}.")
    if action == "use" and chosen is not None and not _language_ok(chosen, offer):
        action = "edit"
        notes.append("CV jest w innym języku niż oferta: zamiast „bez zmian” będzie edycja.")
    return Decision(action, chosen, answer.reason.strip(), True, base.action, notes)
