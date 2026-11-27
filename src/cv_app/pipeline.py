"""Wspólne kroki dla CLI i panelu: pobranie ofert z Gmaila i podgląd CV.

Każdy krok raportuje postęp przez ``log`` (w CLI: ``print``, w panelu: log zadania).
Błąd z komunikatem dla użytkownika to :class:`PipelineError`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from cv_app.config import Settings
from cv_app.models import Offer
from cv_app.offer_page import OfferPageService
from cv_app.relevance import IngestResult

Log = Callable[[str], None]


class PipelineError(Exception):
    """Błąd kroku z komunikatem dla użytkownika."""


def preview_path(settings: Settings, offer_id: int) -> Path:
    return settings.data_dir / "preview" / f"cv-oferta-{offer_id}.pdf"


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
    """Generuje bloki CV przez Claude, mierzy dopasowanie i zapisuje podgląd PDF."""
    from cv_app import fit, render
    from cv_app.claude_client import ClaudeError
    from cv_app.generator import GeneratorError, generate_blocks
    from cv_app.profile import ProfileError, load_profile

    with Session(engine) as session:
        offer = session.get(Offer, offer_id)
    if offer is None:
        raise PipelineError(f"Nie ma oferty #{offer_id} w bazie (najpierw pobierz oferty).")
    try:
        profile = load_profile(settings)
        log(f"Generuję CV dla: {offer.title} ({offer.company or '?'}), to może potrwać...")
        result = generate_blocks(profile, offer, settings=settings)
        html = render.render_html(result.blocks, render.build_context(profile, settings))
        log("Mierzę dopasowanie do strony...")
        report = fit.measure_fit(html, settings)
        out = preview_path(settings, offer_id)
        render.render_pdf(html, out, settings)
    except (ProfileError, ClaudeError, GeneratorError, render.RenderError) as exc:
        raise PipelineError(f"Błąd: {exc}") from exc
    log(f"Wariant: {result.variant}, projekty: {', '.join(result.project_keys)}")
    for warning in result.warnings + report.problems():
        log(f"  uwaga: {warning}")
    if report.fits:
        log(f"Mieści się na stronie (wolne miejsce: {report.main.free_mm:.0f} mm).")
    log(f"Podgląd PDF: {out}")
    return out
