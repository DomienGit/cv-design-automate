"""Punkt wejścia: ``uv run cv-app``."""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from cv_app.app import create_app
from cv_app.config import Settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cv-app", description="Uruchamia lokalny panel do tworzenia CV."
    )
    parser.add_argument("--port", type=int, help="port panelu (domyślnie z ustawień)")
    parser.add_argument(
        "--no-browser", action="store_true", help="nie otwieraj panelu w przeglądarce"
    )
    parser.add_argument(
        "--gmail-check",
        action="store_true",
        help="zaloguj do Gmaila (tylko odczyt) i pokaż oferty z ostatnich dni, bez panelu",
    )
    parser.add_argument(
        "--offers-check",
        action="store_true",
        help="pobierz oferty z Gmaila, odfiltruj, pobierz ich strony i zapisz w bazie",
    )
    parser.add_argument(
        "--cv-preview",
        type=int,
        metavar="ID",
        help="wygeneruj przez Claude CV dla oferty o podanym numerze z bazy (podgląd PDF)",
    )
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.no_browser:
        overrides["open_browser"] = False
    return Settings(**overrides)


def gmail_check(settings: Settings) -> int:
    """Logowanie do Gmaila i podgląd ofert; nie zmienia stanu synchronizacji."""
    from datetime import UTC, datetime, timedelta

    from cv_app.gmail import GmailError, build_service, fetch_messages, load_credentials
    from cv_app.portals import offers_from_mail

    try:
        service = build_service(load_credentials(settings, interactive=True))
        after = datetime.now(UTC) - timedelta(days=settings.fetch_days_back)
        messages = fetch_messages(service, settings.senders, after)
    except GmailError as exc:
        print(f"Gmail: {exc}")
        return 1
    print(
        f"Maile od {', '.join(settings.senders)} z {settings.fetch_days_back} dni: {len(messages)}"
    )
    for message in messages:
        offers = offers_from_mail(message)
        day = message.date.strftime("%Y-%m-%d") if message.date else "?"
        print(f"\n{day}  {message.subject}  ({len(offers)} ofert)")
        for offer in offers:
            where = ", ".join(offer.locations)
            print(f"  - {offer.title} | {offer.company or '?'} | {where}")
    return 0


def offers_check(settings: Settings) -> int:
    """Gmail → filtr trafności i deduplikacja → strony ofert → baza. Bez panelu."""
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session

    from cv_app.db import init_db, make_engine
    from cv_app.gmail import GmailError, build_service, fetch_messages, load_credentials
    from cv_app.offer_page import OfferPageService
    from cv_app.portals import offers_from_mail
    from cv_app.profile import ProfileError, load_profile
    from cv_app.relevance import ingest_mail_offers

    try:
        profile = load_profile(settings)
    except ProfileError as exc:
        print(f"Profil: {exc} (filtr działa bez reguł profilu)")
        profile = None
    try:
        service = build_service(load_credentials(settings, interactive=True))
        after = datetime.now(UTC) - timedelta(days=settings.fetch_days_back)
        messages = fetch_messages(service, settings.senders, after)
    except GmailError as exc:
        print(f"Gmail: {exc}")
        return 1
    mail_offers = [o for m in messages for o in offers_from_mail(m)]

    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        result = ingest_mail_offers(session, mail_offers, settings, profile)
        print(result.summary)
        for offer in result.rejected:
            print(f"  odrzucona: {offer.title} ({offer.filter_reason})")
        if result.added:
            print(f"Pobieram strony {len(result.added)} ofert (1 strona / 2 s)...")
        enriched = OfferPageService(settings).enrich_all(session, result.added)
        for offer in result.added:
            flag = "  [niepełna]" if offer.incomplete else ""
            print(
                f"\n#{offer.id} {offer.title} | {offer.company or '?'} | "
                f"{offer.category} | {offer.language}{flag}"
            )
            for req in offer.requirements[:5]:
                print(f"    - {req}")
            if offer.id in enriched.errors:
                print(f"    błąd: {enriched.errors[offer.id]}")
    engine.dispose()
    return 0


def cv_preview(settings: Settings, offer_id: int) -> int:
    """Generuje bloki CV przez Claude, mierzy dopasowanie i zapisuje podgląd PDF."""
    from sqlmodel import Session

    from cv_app.claude_client import ClaudeError
    from cv_app.db import init_db, make_engine
    from cv_app.fit import measure_fit
    from cv_app.generator import GeneratorError, generate_blocks
    from cv_app.models import Offer
    from cv_app.profile import ProfileError, load_profile
    from cv_app.render import RenderError, build_context, render_html, render_pdf

    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        offer = session.get(Offer, offer_id)
    engine.dispose()
    if offer is None:
        print(f"Nie ma oferty #{offer_id} w bazie (najpierw: uv run cv-app --offers-check).")
        return 1
    try:
        profile = load_profile(settings)
        print(f"Generuję CV dla: {offer.title} ({offer.company or '?'}), to może potrwać...")
        result = generate_blocks(profile, offer, settings=settings)
        html = render_html(result.blocks, build_context(profile, settings))
        report = measure_fit(html, settings)
        out = settings.data_dir / "preview" / f"cv-oferta-{offer_id}.pdf"
        render_pdf(html, out, settings)
    except (ProfileError, ClaudeError, GeneratorError, RenderError) as exc:
        print(f"Błąd: {exc}")
        return 1
    print(f"Wariant: {result.variant}, projekty: {', '.join(result.project_keys)}")
    for warning in result.warnings + report.problems():
        print(f"  uwaga: {warning}")
    if report.fits:
        print(f"Mieści się na stronie (wolne miejsce: {report.main.free_mm:.0f} mm).")
    print(f"Podgląd PDF: {out}")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = build_settings(args)
    if args.gmail_check:
        raise SystemExit(gmail_check(settings))
    if args.offers_check:
        raise SystemExit(offers_check(settings))
    if args.cv_preview is not None:
        raise SystemExit(cv_preview(settings, args.cv_preview))
    app = create_app(settings)

    if settings.open_browser:
        # Otwórz kartę chwilę po starcie serwera.
        threading.Timer(1.0, webbrowser.open, args=(settings.panel_url,)).start()

    print(f"Panel CV: {settings.panel_url}  (Ctrl+C kończy pracę)")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
