# CLAUDE.md

Wskazówki dla Claude Code (i innych agentów) pracujących w tym repozytorium.
Pełny plan projektu: dokument „Plan: aplikacja do tworzenia CV pod oferty z maila”.

## Czym jest ta aplikacja

Lokalna aplikacja Dominika: na żądanie pobiera oferty pracy z Gmaila, pozwala wybrać
te, pod które aplikuje, i dla każdej generuje dopasowane jednostronicowe CV (PDF) w
stałym szablonie. Nic nie jest hostowane ani nie działa w tle; dane zostają na dysku.

Przepływ: mail → adapter portalu → strona oferty → ekstrakcja → filtr trafności →
generator bloków CV (AI) → walidator reguł → szablon HTML → PDF.

Poza zakresem v1: wysyłanie aplikacji, harmonogram w tle, hosting, wielu użytkowników,
list motywacyjny.

## Stack

- Python 3.12+, zarządzanie przez `uv` (`uv sync`, `uv run ...`), układ `src/cv_app/`.
- FastAPI + uvicorn, panel w karcie przeglądarki; serwer **tylko na 127.0.0.1**
  (walidator w `config.py` odrzuca inne hosty, nie obchodź go).
- Panel: szablony Jinja2 w `src/cv_app/templates/`, CSS w `static/`; bez frameworka JS.
- Baza: SQLite przez SQLModel (`models.py`: `Offer`, `CV`). Klucz deduplikacji oferty
  to para `(source, external_id)`, np. `("pracuj", "1005236889")`.
- PDF: Jinja2 + Playwright (Chromium), szablon w `templates/cv/` (PL i EN).
- AI: **Claude Code w trybie nieinteraktywnym (`claude -p`)** na koncie claude.ai
  Dominika, nie API Anthropic (brak klucza). Całe wywołanie schowane za jedną funkcją
  `run_claude(prompt, schema)`, żeby dało się je później podmienić na API.
  W testach zawsze atrapa, nigdy prawdziwe wywołanie.
- Gmail API z zakresem `gmail.readonly` (OAuth „Desktop app”).

## Komendy

```bash
uv sync                      # instalacja zależności
uv run cv-app                # start panelu (otwiera przeglądarkę)
uv run cv-app --no-browser   # bez otwierania przeglądarki
uv run pytest                # testy
uv run ruff check . && uv run ruff format .
```

Każda zmiana kończy się zielonymi testami i czystym `ruff check` / `ruff format --check`
(to samo sprawdza CI w `.github/workflows/ci.yml`).

## Ustawienia i sekrety

- Ustawienia w `src/cv_app/config.py` (`Settings`, pydantic-settings): zmienne
  `CV_APP_*` i plik `.env` (wzór: `.env.example`). Nowe ustawienie dodawaj tam i do
  `.env.example`, nie twardo w kodzie.
- **Repozytorium jest publiczne.** Nigdy nie commituj ani nie wypisuj w logach:
  `credentials.json`, `token.json`, `client_secret*.json`, `.env`, bazy `*.db`,
  plików `.eml`, folderu `profil/`. Wszystkie są w `.gitignore`; nie usuwaj tych wpisów.
- Aplikacja zna tylko **ścieżki** do sekretów:
  `CV_APP_GMAIL_CREDENTIALS_PATH` (domyślnie `~/.config/cv-app/credentials.json`) i
  `CV_APP_GMAIL_TOKEN_PATH` (domyślnie `~/.local/share/cv-app/token.json`).
  Nie proś Dominika o wklejenie ich treści; panel pokazuje tylko, czy plik istnieje.
- Dane osobowe do AI: wysyłaj tylko potrzebne pola. Telefon, e-mail i adres są
  wstawiane lokalnie przy renderze, nie trafiają do promptu.

## Profil (źródło prawdy)

- Pliki `O mnie.md` i `Projekty.md` w `CV_APP_NOTES_DIR` (domyślnie `./profil`, lokalnie
  u Dominika `~/Projects/cv-design-automate/profil/`). Aplikacja czyta je przy każdym
  uruchomieniu; nie twórz drugiej kopii danych profilu.
- Parsowanie jest deterministyczne (bez AI): `## Nazwa` w `Projekty.md` to projekt,
  pola `**Link:**`, `**Produkcyjnie:**`, `**Na CV jako:**`, `**Stack:**`,
  `**Punkty do CV:**`; tabela „Jak dobierać projekty pod ofertę” to priorytety;
  `### Wariant ...` w `O mnie.md` to warianty podsumowania; `## Umiejętności` to pula
  umiejętności; callouty `> [!important]` to twarde reguły.
- Testy parsera na prawdziwych plikach muszą się pomijać (`pytest.skip`), gdy plików
  nie ma, bo nie ma ich w repozytorium ani w CI. Fikstury testowe twórz jako krótkie,
  zmyślone pliki o tym samym formacie.

## Twarde reguły treści CV

AI tylko **wybiera i przeformułowuje** to, co jest w profilu; nigdy nie dopisuje faktów.
Reguły z calloutów `[!important]` sprawdza walidator w każdym CV:

- Omega Faktury, PapuTrain, web_page_cv: podawać wprost, że powstały z użyciem narzędzi
  AI; nie pisać, że kod jest pisany ręcznie.
- terraform-3tier i AWS-project-01 to zawsze jeden projekt (terraform-3tier).
- Angielski B1, nigdy „płynny” / „fluent”.
- Umiejętności z dopiskiem „podstawy” lub „teoria” zostają z tym dopiskiem
  (np. Ansible, Kubernetes, ITIL).
- Azure: brak doświadczenia, nigdy w CV.
- Pola „DO UZUPEŁNIENIA” i „DO POTWIERDZENIA” są pomijane.
- Każda technologia w CV musi istnieć w profilu; limity długości bloków z szablonu.

Reguły czytaj z plików profilu, gdzie się da, zamiast je duplikować w kodzie.

## CV: szablon i bloki

- Wygląd jak obecne CV „Administrator systemów Linux”: jedna strona A4, ciemny lewy
  pasek (zdjęcie, kontakt, umiejętności jako pigułki, języki), jasna prawa kolumna
  (imię, tytuł, podsumowanie, projekty z tagami, edukacja), klauzula RODO w stopce.
- Bloki CV to JSON niezależny od wyglądu. Limity startowe: tytuł ~35 znaków,
  podsumowanie ~650, do 16 pigułek umiejętności, 3 projekty po 3–4 punkty ~90 znaków.
- Język CV = język oferty (EN oferta → EN CV z angielskimi nagłówkami i klauzulą).
- Dopasowanie do strony: Playwright mierzy przepełnienie, AI skraca, maks. 3 poprawki,
  potem ostrzeżenie w panelu.

## Oferty i portale

- Adapter per portal w osobnym module; start: Pracuj.pl
  (`jobalert@wysylka.pracuj.pl`, mail tylko HTML, kilka ofert w jednym mailu, numer
  oferty z linku `oferta,<numer>`, parametr `sendid` odcinać).
- Pobieranie stron ofert: wolno (1 strona / 2 s), z cache; gdy się nie uda, oferta
  z danymi z maila i flagą `incomplete`.
- Filtr trafności przed pobieraniem stron; kategorie: administrator IT, DevOps,
  helpdesk, backend z Pythonem, AI/ML. Odrzucone trafiają do osobnej sekcji.

## Konwencje

- Kod, nazwy i komentarze techniczne po angielsku lub polsku jak w otoczeniu; teksty
  w panelu i komunikaty dla użytkownika po polsku.
- Testy nie mogą zależeć od sieci, Gmaila, `claude` ani plików z `profil/`.
  Używaj `tmp_path` i fikstur z `tests/conftest.py` (izolują `CV_APP_*` i `.env`).
- Jedno zadanie z planu = jedna gałąź i jeden PR, zakończony testami.

## Zadania (fale)

Zadania z tej samej fali można robić równolegle.

| Nr | Fala | Zadanie | Zależy od |
| --- | --- | --- | --- |
| 1 | 1 | Szkielet: uv, FastAPI, ustawienia, SQLite (Offer, CV), pytest, ruff | — |
| 2 | 2 | Parser profilu (O mnie.md, Projekty.md → Pydantic, reguły `[!important]`) | 1 |
| 3 | 2 | Szablon CV HTML/CSS (Jinja2), schemat bloków, PL/EN, render PDF | 1 |
| 5 | 2 | Klient Claude Code: `run_claude(prompt, schema)`, timeout, ponowienia, atrapa | 1 |
| 9 | 2 | Adapter Pracuj.pl: HTML maila → oferty (13 ofert z maila z 24.11) | 1 |
| 8 | 2 | Gmail: OAuth readonly, pobieranie od ostatniej synchronizacji | 1 |
| 4 | 3 | Pomiar dopasowania do strony (Playwright) | 3 |
| 6 | 3 | Generator bloków: wariant, projekty, prompt z limitami i regułami | 2, 3, 5 |
| 10 | 3 | Strona oferty: pobranie, ekstrakcja, cache, tempo | 5, 9 |
| 11 | 3 | Filtr trafności i deduplikacja | 2, 9 |
| 7 | 4 | Walidator reguł i pętla poprawek | 2, 4, 6 |
| 12 | 4 | Panel Oferty | 8, 10, 11 |
| 13 | 5 | Panel Gotowe CV i Ustawienia, zapis do folderu | 7, 12 |
| 14 | 5 | Test końcowy i README instalacji | wszystkie |
