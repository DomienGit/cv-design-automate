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
    from cv_app.fit import FitReport
    from cv_app.generator import GenerationResult

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


def _report_path(settings: Settings, offer_id: int) -> Path:
    return preview_path(settings, offer_id).with_suffix(".json")


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


def preview_cv(settings: Settings, engine: Engine, offer_id: int, log: Log = print) -> Path:
    """Generuje CV przez Claude, sprawdza reguły i stronę, poprawia i zapisuje podgląd PDF.

    Pętla poprawek: walidator reguł profilu + pomiar strony → uwagi → AI poprawia
    (najwyżej ``settings.cv_fix_attempts`` razy). Gdy uwagi zostają, PDF i tak powstaje,
    a uwagi trafiają do raportu obok PDF (panel pokazuje je jako ostrzeżenie).
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
    feedback: list[str] | None = None
    previous = None
    attempts = 0
    try:
        profile = load_profile(settings)
        ctx = render.build_context(profile, settings)
        log(f"Generuję CV dla: {offer.title} ({offer.company or '?'}), to może potrwać...")
        for attempt in range(max_fixes + 1):
            try:
                result = generate_blocks(
                    profile, offer, settings=settings, feedback=feedback, previous=previous
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

    write_preview_report(
        settings,
        offer_id,
        PreviewReport(
            ok=not problems,
            problems=problems,
            fixes=attempts,
            variant=result.variant,
            projects=result.project_keys,
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
