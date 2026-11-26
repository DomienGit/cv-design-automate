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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = build_settings(args)
    if args.gmail_check:
        raise SystemExit(gmail_check(settings))
    app = create_app(settings)

    if settings.open_browser:
        # Otwórz kartę chwilę po starcie serwera.
        threading.Timer(1.0, webbrowser.open, args=(settings.panel_url,)).start()

    print(f"Panel CV: {settings.panel_url}  (Ctrl+C kończy pracę)")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
