# cv-design-automate

Lokalna aplikacja do tworzenia CV dopasowanych do ofert pracy z maila. Działa tylko
na Twoim komputerze i tylko wtedy, gdy ją uruchomisz: panel otwiera się w przeglądarce
pod `http://127.0.0.1:8765/`, a po zamknięciu nic nie działa w tle.

Obecny stan: fala 2 planu. Gotowe są moduły bez panelu: parser profilu, szablon CV
z renderem PDF, klient Claude Code (`claude -p`), adapter maili Pracuj.pl i pobieranie
maili z Gmaila. Panel ma jeszcze puste zakładki (Oferty, Gotowe CV, Ustawienia).

## Uruchomienie

Potrzebny jest [uv](https://docs.astral.sh/uv/). Plik `.python-version` wybiera Pythona 3.13; jeśli go nie masz, uv pobierze go sam. Python musi mieć moduł `sqlite3` (wersje od uv go mają; Python kompilowany ręcznie bez `libsqlite3-dev` nie).

```bash
uv sync
uv run cv-app            # otwiera panel w przeglądarce
uv run cv-app --no-browser --port 9000
uv run cv-app --gmail-check   # logowanie do Gmaila i lista ofert z ostatnich dni
```

Do PDF potrzebny jest Chromium dla Playwrighta (raz, po `uv sync`):

```bash
uv run playwright install chromium
```

Do generowania treści CV potrzebny jest [Claude Code](https://claude.com/claude-code)
zalogowany na Twoje konto claude.ai (komenda `claude` w `PATH`).

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

Pierwsze `uv run cv-app --gmail-check` otworzy logowanie Google w przeglądarce
(zakres tylko do odczytu, `gmail.readonly`). Token trafia do
`~/.local/share/cv-app/token.json` z prawami 600; kolejne uruchomienia go odświeżają.
Jeśli Google pokaże ekran „aplikacja nie jest zweryfikowana”, dodaj swój adres jako
użytkownika testowego w ekranie zgody OAuth projektu w Google Cloud.

Jeśli wolisz inne miejsce, ustaw `CV_APP_GMAIL_CREDENTIALS_PATH` w `.env`. Zakładka
Ustawienia pokazuje, czy aplikacja widzi plik (bez pokazywania jego treści).
`.gitignore` wyklucza `credentials.json`, `token.json`, `client_secret*.json` i `.env`
w każdym folderze projektu.

## Profil

Pliki profilu (`profil/O mnie.md`, `profil/Projekty.md`) zawierają dane osobowe,
dlatego folder `profil/` jest w `.gitignore` i zostaje tylko na Twoim dysku.
Zdjęcie do CV połóż obok jako `profil/zdjecie.jpg` (albo `.png`) lub wskaż je
zmienną `CV_APP_PHOTO_PATH`.

## Biblioteka gotowych CV

W folderze `cv/` (zmienna `CV_APP_CV_LIBRARY_DIR`) trzymaj gotowe CV w podfolderach
per stanowisko, np. `cv/DevOps/`, `cv/Helpdesk/`, `cv/Administrator Linux/`. Folder
jest w `.gitignore`, bo CV mają dane osobowe.

Przycisk „Dobierz CV” przy ofercie (albo `uv run cv-app --cv-match 5`):

1. skrypt (bez AI) czyta CV z biblioteki i liczy pokrycie technologii z oferty;
   podfolder pasujący do kategorii oferty dostaje premię,
2. AI dostaje tylko 3 najlepsze CV (bez telefonu, e-maila i linków) i decyduje:
   gotowe CV bez zmian, edycja albo nowe CV (progi startowe: od 80% bez zmian,
   od 50% edycja),
3. wynik zawsze przechodzi przez walidator reguł profilu; gotowe CV, które łamie
   reguły, jest poprawiane.

Najlepiej trzymać obok PDF źródło o tej samej nazwie: `cv-aws.pdf` + `cv-aws.json`.
Z samego PDF tekst wyciąga skrypt, ale edycja opiera się wtedy tylko na tekście.
Każde CV z aplikacji ma źródło obok podglądu (`cv-oferta-<nr>.blocks.json` w
`~/.local/share/cv-app/preview/`), które można skopiować do biblioteki razem z PDF.

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
