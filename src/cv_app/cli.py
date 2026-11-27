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
    parser.add_argument(
        "--cv-match",
        type=int,
        metavar="ID",
        help="dobierz CV dla oferty: gotowe z biblioteki (bez zmian lub edytowane) albo nowe",
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
    from cv_app.db import init_db, make_engine
    from cv_app.pipeline import PipelineError, fetch_offers

    engine = make_engine(settings)
    init_db(engine)
    try:
        fetch_offers(settings, engine)
    except PipelineError as exc:
        print(exc)
        return 1
    finally:
        engine.dispose()
    return 0


def cv_preview(settings: Settings, offer_id: int) -> int:
    """Generuje bloki CV przez Claude, mierzy dopasowanie i zapisuje podgląd PDF."""
    from cv_app.db import init_db, make_engine
    from cv_app.pipeline import PipelineError, clear_match_report, preview_cv

    engine = make_engine(settings)
    init_db(engine)
    try:
        clear_match_report(settings, offer_id)
        preview_cv(settings, engine, offer_id)
    except PipelineError as exc:
        print(exc)
        return 1
    finally:
        engine.dispose()
    return 0


def cv_match(settings: Settings, offer_id: int) -> int:
    """Biblioteka gotowych CV → wynik skryptu → decyzja AI → podgląd PDF."""
    from cv_app.db import init_db, make_engine
    from cv_app.pipeline import PipelineError, match_cv

    engine = make_engine(settings)
    init_db(engine)
    try:
        match_cv(settings, engine, offer_id)
    except PipelineError as exc:
        print(exc)
        return 1
    finally:
        engine.dispose()
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
    if args.cv_match is not None:
        raise SystemExit(cv_match(settings, args.cv_match))
    app = create_app(settings)

    if settings.open_browser:
        # Otwórz kartę chwilę po starcie serwera.
        threading.Timer(1.0, webbrowser.open, args=(settings.panel_url,)).start()

    print(f"Panel CV: {settings.panel_url}  (Ctrl+C kończy pracę)")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
