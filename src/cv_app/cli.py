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
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.no_browser:
        overrides["open_browser"] = False
    return Settings(**overrides)


def main(argv: list[str] | None = None) -> None:
    settings = build_settings(parse_args(argv))
    app = create_app(settings)

    if settings.open_browser:
        # Otwórz kartę chwilę po starcie serwera.
        threading.Timer(1.0, webbrowser.open, args=(settings.panel_url,)).start()

    print(f"Panel CV: {settings.panel_url}  (Ctrl+C kończy pracę)")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
