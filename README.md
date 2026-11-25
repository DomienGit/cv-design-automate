# cv-design-automate

Lokalna aplikacja do tworzenia CV dopasowanych do ofert pracy z maila. Działa tylko
na Twoim komputerze i tylko wtedy, gdy ją uruchomisz: panel otwiera się w przeglądarce
pod `http://127.0.0.1:8765/`, a po zamknięciu nic nie działa w tle.

Obecny stan: szkielet (fala 1 planu). Panel ma trzy puste zakładki (Oferty, Gotowe CV,
Ustawienia), działa baza SQLite z modelami Oferta i CV oraz ustawienia ze zmiennych
środowiskowych i pliku `.env`.

## Uruchomienie

Potrzebny jest [uv](https://docs.astral.sh/uv/) i Python 3.12 lub nowszy.

```bash
uv sync
uv run cv-app            # otwiera panel w przeglądarce
uv run cv-app --no-browser --port 9000
```

## Ustawienia

Skopiuj `.env.example` do `.env` i odkomentuj to, co chcesz zmienić. Najważniejsze:

| Zmienna | Domyślnie | Co to jest |
| --- | --- | --- |
| `CV_APP_NOTES_DIR` | `./profil` | folder z `O mnie.md` i `Projekty.md` |
| `CV_APP_CV_OUTPUT_DIR` | `~/CV` | gdzie zapisywać gotowe CV |
| `CV_APP_SENDERS` | `["jobalert@wysylka.pracuj.pl"]` | nadawcy maili z ofertami |
| `CV_APP_GMAIL_CREDENTIALS_PATH` | `~/.config/cv-app/credentials.json` | plik OAuth z Google Cloud |
| `CV_APP_GMAIL_TOKEN_PATH` | `~/.local/share/cv-app/token.json` | token zapisywany po zalogowaniu |
| `CV_APP_DATA_DIR` | `~/.local/share/cv-app` | baza SQLite i cache |

Panel nasłuchuje wyłącznie na adresie lokalnym; inny host (np. `0.0.0.0`) jest odrzucany.

## Gmail: credentials.json

Plik `credentials.json` (typ „Desktop app” z Google Cloud) i `token.json` to sekrety.
**Repozytorium jest publiczne**, więc nigdy ich nie commituj ani nigdzie nie wklejaj.

```bash
mkdir -p ~/.config/cv-app
mv ~/Pobrane/credentials.json ~/.config/cv-app/credentials.json
chmod 600 ~/.config/cv-app/credentials.json
```

Jeśli wolisz inne miejsce, ustaw `CV_APP_GMAIL_CREDENTIALS_PATH` w `.env`. Zakładka
Ustawienia pokazuje, czy aplikacja widzi plik (bez pokazywania jego treści).
`.gitignore` wyklucza `credentials.json`, `token.json`, `client_secret*.json` i `.env`
w każdym folderze projektu.

## Profil

Pliki profilu (`profil/O mnie.md`, `profil/Projekty.md`) zawierają dane osobowe,
dlatego folder `profil/` jest w `.gitignore` i zostaje tylko na Twoim dysku.

## Rozwój

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

Struktura:

```
src/cv_app/
  cli.py        # komenda cv-app: start serwera i otwarcie przeglądarki
  app.py        # FastAPI: panel i API (/api/health, /api/settings)
  config.py     # ustawienia (CV_APP_*, .env)
  db.py         # silnik SQLite
  models.py     # Offer, CV
  templates/    # HTML panelu
  static/       # CSS
tests/
```
