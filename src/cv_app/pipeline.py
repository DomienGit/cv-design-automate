"""Wspólne kroki dla CLI i panelu: pobranie ofert z Gmaila i podgląd CV.

Każdy krok raportuje postęp przez ``log`` (w CLI: ``print``, w panelu: log zadania).
Błąd z komunikatem dla użytkownika to :class:`PipelineError`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy import Engine
from sqlmodel import Session

from cv_app.config import Settings
from cv_app.models import Offer
from cv_app.offer_page import OfferPageService
from cv_app.relevance import IngestResult

if TYPE_CHECKING:
    from cv_app.cv_blocks import CVBlocks
    from cv_app.cv_library import Candidate
    from cv_app.fit import FitReport
    from cv_app.generator import GenerationResult
    from cv_app.profile import Profile

Log = Callable[[str], None]


class PipelineError(Exception):
    """Błąd kroku z komunikatem dla użytkownika."""


def preview_path(settings: Settings, offer_id: int) -> Path:
    return settings.data_dir / "preview" / f"cv-oferta-{offer_id}.pdf"


class PreviewReport(BaseModel):
    """Wynik walidacji podglądu CV, zapisany obok PDF (pokazywany w panelu)."""

    ok: bool
    problems: list[str] = Field(default_factory=list)
    fixes: int = 0  # ile poprawek zrobiło AI
    variant: str | None = None
    projects: list[str] = Field(default_factory=list)
    source: str | None = None  # np. "gotowe CV: DevOps/cv-aws" (bez zmian albo edytowane)


def _report_path(settings: Settings, offer_id: int) -> Path:
    return preview_path(settings, offer_id).with_suffix(".json")


def blocks_path(settings: Settings, offer_id: int) -> Path:
    """Bloki CV obok podglądu: źródło, które można skopiować do biblioteki CV obok PDF."""
    return preview_path(settings, offer_id).with_suffix(".blocks.json")


def write_blocks(settings: Settings, offer_id: int, blocks: CVBlocks) -> None:
    path = blocks_path(settings, offer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(blocks.model_dump_json(indent=2), encoding="utf-8")


def write_preview_report(settings: Settings, offer_id: int, report: PreviewReport) -> None:
    path = _report_path(settings, offer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def read_preview_report(settings: Settings, offer_id: int) -> PreviewReport | None:
    path = _report_path(settings, offer_id)
    try:
        return PreviewReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fetch_offers(
    settings: Settings,
    engine: Engine,
    log: Log = print,
    *,
    interactive: bool = True,
) -> IngestResult:
    """Gmail → filtr trafności i deduplikacja → strony ofert → baza."""
    from cv_app import gmail
    from cv_app.portals import offers_from_mail
    from cv_app.profile import ProfileError, load_profile
    from cv_app.relevance import ingest_mail_offers

    try:
        profile = load_profile(settings)
    except ProfileError as exc:
        log(f"Profil: {exc} (filtr działa bez reguł profilu)")
        profile = None

    log(f"Łączę z Gmailem, maile z ostatnich {settings.fetch_days_back} dni...")
    try:
        service = gmail.build_service(gmail.load_credentials(settings, interactive=interactive))
        after = datetime.now(UTC) - timedelta(days=settings.fetch_days_back)
        messages = gmail.fetch_messages(service, settings.senders, after)
    except gmail.GmailError as exc:
        raise PipelineError(f"Gmail: {exc}") from exc
    except Exception as exc:  # błędy sieci / API Google
        raise PipelineError(f"Gmail: nie udało się pobrać maili ({exc})") from exc
    mail_offers = [o for m in messages for o in offers_from_mail(m)]
    log(f"Maile: {len(messages)}, oferty w mailach: {len(mail_offers)}")

    with Session(engine, expire_on_commit=False) as session:
        result = ingest_mail_offers(session, mail_offers, settings, profile)
        log(result.summary)
        for offer in result.rejected:
            log(f"  odrzucona: {offer.title} ({offer.filter_reason})")

        pages = OfferPageService(settings)
        total = len(result.added)
        if total:
            log(f"Pobieram strony {total} ofert (1 strona / {settings.offer_fetch_delay:g} s)...")
        for i, offer in enumerate(result.added, start=1):
            error = enrich_offer(session, pages, offer)
            flag = " [niepełna]" if offer.incomplete else ""
            log(f"  {i}/{total} #{offer.id} {offer.title} | {offer.company or '?'}{flag}")
            if error:
                log(f"    błąd: {error}")
    return result


def enrich_offer(session: Session, pages: OfferPageService, offer: Offer) -> str | None:
    """Pobiera stronę jednej oferty i zapisuje ją od razu. Zwraca komunikat błędu."""
    from cv_app.offer_page import FetchError

    error = None
    try:
        pages.enrich(offer)
    except FetchError as exc:
        error = str(exc)
    session.add(offer)
    session.commit()  # zapis po każdej ofercie: przerwanie nie gubi postępu
    return error


def restore_offer(settings: Settings, engine: Engine, offer_id: int, log: Log = print) -> None:
    """Przywraca ofertę odrzuconą przez filtr i pobiera jej stronę."""
    from cv_app.relevance import restore_rejected

    with Session(engine, expire_on_commit=False) as session:
        offer = session.get(Offer, offer_id)
        if offer is None:
            raise PipelineError(f"Nie ma oferty #{offer_id} w bazie.")
        restore_rejected(session, offer)
        log(f"Przywrócono: {offer.title}. Pobieram stronę oferty...")
        error = enrich_offer(session, OfferPageService(settings), offer)
        if error:
            log(f"  błąd: {error} (zostają dane z maila)")
        else:
            log("  strona odczytana")


def preview_cv(
    settings: Settings,
    engine: Engine,
    offer_id: int,
    log: Log = print,
    *,
    base: CVBlocks | None = None,
    base_text: str | None = None,
    instructions: list[str] | None = None,
    source: str | None = None,
) -> Path:
    """Generuje CV przez Claude, sprawdza reguły i stronę, poprawia i zapisuje podgląd PDF.

    Pętla poprawek: walidator reguł profilu + pomiar strony → uwagi → AI poprawia
    (najwyżej ``settings.cv_fix_attempts`` razy). Gdy uwagi zostają, PDF i tak powstaje,
    a uwagi trafiają do raportu obok PDF (panel pokazuje je jako ostrzeżenie).

    Edycja gotowego CV: ``base`` (bloki z JSON) albo ``base_text`` (tekst z PDF) to
    punkt wyjścia, a ``instructions`` to uwagi do pierwszej wersji (np. czego brakuje
    pod ofertę). ``source`` trafia do raportu (skąd jest CV).
    """
    from cv_app import fit, render
    from cv_app.claude_client import ClaudeError
    from cv_app.generator import GeneratorError, generate_blocks
    from cv_app.profile import ProfileError, load_profile
    from cv_app.validator import validate

    with Session(engine) as session:
        offer = session.get(Offer, offer_id)
    if offer is None:
        raise PipelineError(f"Nie ma oferty #{offer_id} w bazie (najpierw pobierz oferty).")
    max_fixes = settings.cv_fix_attempts
    best: tuple[GenerationResult, str, FitReport, list[str]] | None = None
    feedback: list[str] | None = instructions
    previous = base
    attempts = 0
    try:
        profile = load_profile(settings)
        ctx = render.build_context(profile, settings)
        log(f"Generuję CV dla: {offer.title} ({offer.company or '?'}), to może potrwać...")
        for attempt in range(max_fixes + 1):
            try:
                result = generate_blocks(
                    profile,
                    offer,
                    settings=settings,
                    feedback=feedback,
                    previous=previous,
                    base_text=base_text,
                )
            except (ClaudeError, GeneratorError) as exc:
                if best is None:
                    raise
                log(f"Poprawka {attempt} się nie udała ({exc}); zostaje poprzednia wersja.")
                break
            attempts = attempt
            for warning in result.warnings:
                log(f"  generator: {warning}")
            html = render.render_html(result.blocks, ctx)
            report = fit.measure_fit(html, settings)
            problems = [v.message for v in validate(result.blocks, profile)] + report.problems()
            if best is None or len(problems) <= len(best[3]):
                best = (result, html, report, problems)
            if not problems:
                if attempt:
                    log("Po poprawkach CV spełnia wszystkie reguły.")
                break
            if attempt < max_fixes:
                log(f"Uwagi walidatora ({len(problems)}), poprawka {attempt + 1}/{max_fixes}:")
                for problem in problems:
                    log(f"  - {problem}")
                feedback, previous = problems, result.blocks
        assert best is not None
        result, html, report, problems = best
        out = preview_path(settings, offer_id)
        render.render_pdf(html, out, settings)
    except (ProfileError, ClaudeError, GeneratorError, render.RenderError) as exc:
        raise PipelineError(f"Błąd: {exc}") from exc
    write_blocks(settings, offer_id, result.blocks)

    write_preview_report(
        settings,
        offer_id,
        PreviewReport(
            ok=not problems,
            problems=problems,
            fixes=attempts,
            variant=result.variant,
            projects=result.project_keys,
            source=source,
        ),
    )
    log(f"Wariant: {result.variant}, projekty: {', '.join(result.project_keys)}")
    if problems:
        log(f"UWAGA: po {attempts} poprawkach CV nadal ma {len(problems)} uwag:")
        for problem in problems:
            log(f"  - {problem}")
    else:
        log("CV zgodne z regułami profilu.")
    if report.fits:
        log(f"Mieści się na stronie (wolne miejsce: {report.main.free_mm:.0f} mm).")
    log(f"Podgląd PDF: {out}")
    return out


# --- dobór gotowego CV z biblioteki --------------------------------------------------


class MatchReport(BaseModel):
    """Wynik doboru CV dla oferty, zapisany obok podglądu (pokazywany w panelu)."""

    action: str  # use / edit / new: co ostatecznie zrobiono
    label: str
    cv: str | None = None  # klucz CV z biblioteki, np. "DevOps/cv-aws"
    score: int | None = None
    reason: str
    by_ai: bool = False
    script_action: str | None = None
    notes: list[str] = Field(default_factory=list)
    candidates: list[dict[str, object]] = Field(default_factory=list)


def _match_path(settings: Settings, offer_id: int) -> Path:
    return preview_path(settings, offer_id).with_suffix(".match.json")


def write_match_report(settings: Settings, offer_id: int, report: MatchReport) -> None:
    path = _match_path(settings, offer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def clear_match_report(settings: Settings, offer_id: int) -> None:
    """Usuwa wynik doboru (np. gdy CV wygenerowano od nowa przyciskiem „Podgląd CV”)."""
    _match_path(settings, offer_id).unlink(missing_ok=True)


def read_match_report(settings: Settings, offer_id: int) -> MatchReport | None:
    try:
        return MatchReport.model_validate_json(
            _match_path(settings, offer_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _private_values(profile: Profile) -> list[str]:
    """Dane kontaktowe z profilu, które wycinamy z tekstu CV przed promptem."""
    c = profile.contact
    return [
        v
        for v in (c.name, c.phone, c.email, c.location, c.github, c.gitlab, c.linkedin, c.website)
        if v
    ]


def _use_ready_cv(
    settings: Settings, offer_id: int, candidate: Candidate, profile: Profile, log: Log
) -> list[str]:
    """Gotowe CV bez zmian → podgląd. Zwraca uwagi walidatora (puste = użyte)."""
    import shutil

    from cv_app import fit, render
    from cv_app.validator import validate, validate_text

    cv = candidate.cv
    out = preview_path(settings, offer_id)
    if cv.blocks is not None:
        html = render.render_html(cv.blocks, render.build_context(profile, settings))
        report = fit.measure_fit(html, settings)
        problems = [v.message for v in validate(cv.blocks, profile)] + report.problems()
        if problems:
            return problems
        render.render_pdf(html, out, settings)
        write_blocks(settings, offer_id, cv.blocks)
    else:
        problems = [v.message for v in validate_text(cv.text, profile)]
        if problems:
            return problems
        if cv.pdf is None:  # sam .md/.txt bez PDF: nie ma czego wysłać bez zmian
            return ["Gotowe CV nie ma pliku PDF, trzeba je wyrenderować z profilu."]
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cv.pdf, out)
        blocks_path(settings, offer_id).unlink(missing_ok=True)
    write_preview_report(settings, offer_id, PreviewReport(ok=True, source=f"gotowe CV: {cv.key}"))
    log(f"Użyto gotowego CV bez zmian: {cv.key}. Podgląd PDF: {out}")
    return []


def match_cv(settings: Settings, engine: Engine, offer_id: int, log: Log = print) -> MatchReport:
    """Dobór CV do oferty: biblioteka gotowych CV → wynik skryptu → decyzja AI → PDF.

    - ``use``: gotowe CV przechodzi przez walidator reguł; gdy coś łamie, jest edytowane,
    - ``edit``: generator dostaje gotowe CV jako bazę (bloki z JSON albo tekst z PDF),
    - ``new``: zwykłe generowanie z profilu (:func:`preview_cv`).
    """
    from cv_app.cv_library import ACTION_LABELS, anonymize, rank, scan_library
    from cv_app.cv_match import decide
    from cv_app.generator import offer_category
    from cv_app.profile import ProfileError, load_profile
    from cv_app.validator import forbidden_technologies, known_technologies

    with Session(engine) as session:
        offer = session.get(Offer, offer_id)
    if offer is None:
        raise PipelineError(f"Nie ma oferty #{offer_id} w bazie (najpierw pobierz oferty).")
    try:
        profile = load_profile(settings)
    except ProfileError as exc:
        raise PipelineError(f"Błąd: {exc}") from exc

    root = settings.cv_library_dir
    cvs, warnings = scan_library(root)
    for warning in warnings:
        log(f"  biblioteka: {warning}")
    category = offer_category(offer, settings)
    candidates = rank(
        cvs,
        offer,
        vocabulary=known_technologies(profile),
        category=category,
        exclude=forbidden_technologies(profile),
    )
    log(f"Biblioteka CV ({root}): {len(cvs)} CV; oferta: {offer.title} [{category or '-'}]")
    top = candidates[: settings.cv_match_candidates]
    for c in top:
        missing = f"; brakuje: {', '.join(c.missing[:6])}" if c.missing else ""
        log(f"  {c.score:3d}%  {c.cv.key}{missing}")

    decision = decide(top, offer, settings, private=_private_values(profile))
    who = "AI" if decision.by_ai else "skrypt"
    log(f"Decyzja ({who}): {decision.label}. {decision.reason}")
    for note in decision.notes:
        log(f"  {note}")

    notes = list(decision.notes)
    action = decision.action
    chosen = decision.candidate if action != "new" else None
    instructions: list[str] = []
    if action == "use" and chosen is not None:
        problems = _use_ready_cv(settings, offer_id, chosen, profile, log)
        if problems:
            log(f"Gotowe CV łamie reguły profilu ({len(problems)}), więc je edytuję:")
            for problem in problems:
                log(f"  - {problem}")
            notes.append("Gotowe CV nie przeszło walidatora, więc zostało poprawione.")
            action, instructions = "edit", problems
    if action == "edit" and chosen is not None:
        if not instructions:
            instructions = [
                "Dopasuj to gotowe CV do oferty: tytuł, podsumowanie i kolejność "
                "umiejętności pod jej wymagania; projekty i umiejętności tylko z profilu."
            ]
            # Brakujące technologie, które kandydat ma w profilu (resztę i tak pomijamy).
            known = set(known_technologies(profile))
            addable = [t for t in chosen.missing if t in known]
            if addable:
                instructions.append(
                    "Uwzględnij technologie z oferty, które są w profilu: "
                    + ", ".join(addable[:12])
                    + "."
                )
        if chosen.cv.language != ("en" if offer.language == "en" else "pl"):
            instructions.append("Przetłumacz CV na język oferty.")
        preview_cv(
            settings,
            engine,
            offer_id,
            log,
            base=chosen.cv.blocks,
            base_text=None
            if chosen.cv.blocks is not None
            else anonymize(chosen.cv.text, _private_values(profile)),
            instructions=instructions,
            source=f"edytowane CV: {chosen.cv.key}",
        )
    elif action != "use" or chosen is None:
        action, chosen = "new", None
        preview_cv(settings, engine, offer_id, log)

    report = MatchReport(
        action=action,
        label=ACTION_LABELS[action],
        cv=chosen.cv.key if chosen is not None else None,
        score=chosen.score if chosen is not None else None,
        reason=decision.reason,
        by_ai=decision.by_ai,
        script_action=decision.script_action,
        notes=notes,
        candidates=[c.summary() for c in top],
    )
    write_match_report(settings, offer_id, report)
    return report
