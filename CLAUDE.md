# Value-Screener (Buffett-Tracker, Ausbau)

## Ziel
Tool, das ein Aktienuniversum automatisch nach Qualitaet und Bewertung scort und unterverteilt bewertete Unternehmen als Rangliste mit Detail-Scorecard ausgibt.

## Stack
- Python 3.11+, pandas, requests, SQLite (Cache), Streamlit (UI), pytest
- Datenquelle primaer: Financial Modeling Prep (FMP) API, Key in `.env` (`FMP_API_KEY`), nie committen
- Fallback: CSV-Import (bestehendes Format des Trackers) und yfinance fuer Prototyping
- Versionshistorie: siehe CHANGELOG.md

## Arbeitsregeln fuer Claude Code
- Immer nur einen Baustein pro Schritt umsetzen, danach Test und kurze Zusammenfassung
- Keine Magic Numbers: alle Schwellenwerte und Gewichte in `config.yaml`
- Jede Kennzahl mit Einheit, Waehrung und Berichtsjahr speichern
- Fehlende Daten als `NaN` behandeln, nie mit 0 fuellen; im Score als "n/a" ausweisen
- Alle API-Antworten in SQLite cachen (TTL 24 h), um Rate Limits zu schonen
- Bei Unsicherheit ueber Berechnung: Formel im Code-Kommentar dokumentieren
- Nach jeder Aenderung `python -m compileall .` und `pytest` ausfuehren und das Ergebnis zeigen, bevor du Schritt als erledigt meldest.
- **Push-Ziel ist github.com/ThomasHomburg/Matrix2.0** (das Vorgaenger-Repo Matrix wird nicht mehr gepusht). Frische Repos/Umzuege: nie blind force-pushen; bei rejected push erst klaeren. Nie neue Repos anlegen.
- **.gitignore-Regeln**: `.env`, `*.db` (inkl. value_tracker.db), `.ranking_cache.json`, `.universe_cache.json`, `__pycache__/`, `.pytest_cache/`, `ranking_*.csv` (Lauf-Artefakte), `smart_money_overlap.csv` bleiben ungetrackt. Niemals Secrets/API-Keys committen (Gegenprobe: `git log --all -p | grep -iE "api_key|secret"`).
- **Keine Emojis im Code.** Icons nur als Material-Shortcodes (`:material/...:`), z. B. `:material/emoji_events:`, `:material/table_view:`, `:material/scatter_plot:`, `:material/upload_file:`, `:material/trending_up:`. Niemals Emojis als `icon=`/`page_icon=`-Wert und keine ASCII-Platzhalter-Tags wie `[RANK]`/`[CHART]` (verursachen StreamlitAPIException). In Fliestexten statt Emojis Text-Tags wie `[LIVE]`, `[DEMO]`, `[!]`, `[KAUF]` verwenden.
- **Dateien immer als UTF-8 lesen und schreiben** (`open(..., encoding="utf-8")`), nie implizites Encoding; vor dem Schreiben sicherstellen, dass der Inhalt UTF-8-kodierbar ist.
- **Dateien fuer Google-Drive-Upload immer ASCII-only** (keine Umlaute, keine Emojis, keine typografischen Anfuehrungszeichen). Vor dem Upload auf Nicht-ASCII-Zeichen pruefen; Umlaute als ae/oe/ue/ss, Durchschnitts-O als O, Multiplikationszeichen als x, Emojis als [OK]/[LIVE]/[NOTE]/[DEMO] ersetzen.
- **app.py wird fuer den Drive-Upload in zwei Dateien gesplittet**: `app_part1.py` (Imports, Laden, Seite 1 Rangliste, Seite 2 Scorecard bis `sbc_detail_section()`) und `app_part2.py` (ab `def _formula_block` bis Ende). Lokal zusammenfuegen mit `cat app_part1.py app_part2.py > app.py` (Linux/Mac) bzw. `copy app_part1.py+app_part2.py app.py` (Windows). Der Split muss byte-exakt sein: `cat part1 part2` == ASCII-Version von app.py, geprueft mit `python -m py_compile`.
- SyntaxError-Diagnose: bei "unterminated string/triple-quoted string" zuerst die gemeldeten Zeilen mit Zeilennummern zeigen, dann die genaue Ursache finden (ungeschlossenes Triple-Quote, vierfaches Anfuehrungszeichen, typografische Quotes), dann `python -m compileall .` und `pytest` ausfuehren und die Ausgabe zeigen.
- **Round-trip-Verifikation nach jedem Drive-Upload**: jede hochgeladene Datei sofort zurueckladen (Download) und gegen die lokale Datei pruefen (`py_compile` fuer .py-Dateien, Byte-/Hash-Vergleich). Erst wenn die zurueckgeladene Datei fehlerfrei kompiliert und byte-identisch ist, gilt der Upload als erledigt. Hintergrund: fruehere Uploads wurden auf dem Transportweg beschaedigt (einzelne Bit-Flips, z. B. `"` -> Leerzeichen, `(` -> `*`), was auf dem Zielrechner zu SyntaxError fuehrt.

## Architektur / Module
- `data/` -- Loader, Cache, Provider-Schnittstelle (`data/providers/`: FMP fuer US-Ticker mit Key, yfinance sonst), `data/sec.py` (SEC-EDGAR-Abgleich: OCF/Capex/SBC vs. companyfacts, SBC-Fallback, Capex-Pruefstatus), `data/metrics.py`, `data/tickerlist.py`
- `scoring/` -- `criteria.py` (Kennzahlen), `filters.py` (harte Ausschlussfilter), `sectors.py` (Sektor-Sonderregeln), `score.py` (Score + Renormalisierung + Coverage), `pipeline.py` (Gesamtlauf je Ticker)
- `valuation/` -- `dcf.py` (DCF, 3 Szenarien Bear/Base/Bull, 4 Startwert-Modi, robust 10 %, fcf_le0)
- `app.py` -- Streamlit-App (Seite 1 Rangliste, Seite 2 Scorecard, Seite 3 Matrix, Seite 4 Import)
- `run_ranking.py` -- Volllauf-CLI (Batches, Pause, Resume via Cache, Fehlerlog, Ergebnis-CSV); `run_universe.py` -- Universumslader (S&P 500 via Wikipedia, STOXX 600 via CSV); `testlauf_run.py`, `rohwerte_check.py` -- Diagnose/Testlaeufe
- `smart_money.py` -- Smart-Money-Pipeline (eigenstaendig, siehe Config-Referenz `smart_money` und README)
- `live_update.py` -- Watchlist Live-Update (Preise, MoS live, Kaufzonen, Alarme, attach_smart_money); `decisions.py` -- Entscheidungslog (add/snapshot/review, Brier-Kalibrierung); `timing.py` -- Timing-Indikatoren
- `schema.sql` -- SQLite-Schema `value_tracker.db` (prices, valuations, watchlist_status, decisions, smart_money_positions, smart_money_options, smart_money_katalysator, smart_money_rationale, cusip_map); bei Bedarf automatisch angelegt, Migration/Guard in smart_money.connect()
- `tests/` -- pytest, kein Netz (Fetcher-Fixtures/Mocks)

## Referenz: Kennzahlen, Scoring, Filter, DCF
- **Kennzahlen** (je Unternehmen, 10 Jahre, falls verfuegbar): Umsatz, EBIT, Nettogewinn, Operating Cashflow, Capex, FCF (= OCF - Capex), Eigenkapital, Nettoverschuldung, EBITDA, Zinsaufwand, Aktienanzahl (verwaessert), investiertes Kapital, Marktkapitalisierung, Enterprise Value
- **Scoring (0-100)**, Bloecke/Gewichte/Kriterien: siehe Tabelle in frueherer Doku bzw. `config.yaml` (`criteria`, `score_groups`); lineare Interpolation zwischen Schwellen; Bloecke mit > 30 % fehlenden Daten markiert; Quality n/a bei Quality-Abdeckung < `score_groups.min_coverage_quality` (0,70), Value n/a bei Abdeckung < `min_coverage_value`
- **Harte Ausschlussfilter**: Marktkapitalisierung < `filters.min_market_cap_usd`, < `filters.min_history_years` Jahre mit echten Werten, negativer O-FCF ueber 5 Jahre, negatives Eigenkapital (per `filters.exclusion.negative_equity` umschaltbar, sonst Flag `ek_negativ`), Umsatz-CAGR 5 J. < Grenzwert
- **Sektor-Sonderregeln** (`sector_rules.groups`): Banken/Versicherer (ROE, P/B, Dividendenrendite, CET1), Rohstoffe/Zykliker (normalisierte Gewinne), REITs (FFO), Utilities und Asset Management sowie Captive Finance (`captive_finance.tickers`) -> Status "Sonderregel noetig"/"Pruefen (Captive Finance)", nie Kandidat
- **DCF**: normalisierter FCF nach SBC als Startwert (Modi umschaltbar: O 3 J. / jungstes GJ / TTM O / TTM rein, `dcf.fcf_mode`); Wachstum J. 1-5 aus Umsatz-CAGR (gicappt), J. 6-10 linear auf `dcf.fade_to_growth`; Terminal Growth und Diskontsatz aus config; Szenario-Wachstum bei CAGR < 0 via `dcf.bear_negative_spread`; Nettoverschuldung umschaltbar (`dcf.net_debt_mode`, ohne Kundengelder); MoS je Startwert-Modus, Kandidatenregel nutzt Minimum (`dcf.mos_min_mode`); Start-FCF <= 0 -> Wert/MoS n/a in der Berechnung (`fcf_le0`); Robustheits-MoS bei `dcf.robust_discount_rate`; SEC-EDGAR-Abgleich je US-Ticker (`sec.tolerance`, Capex-Pruefstatus via `sec.capex_check`), Bilanz-Konsistenz (`consistency.net_debt_mcap_share`)

## Config-Referenz (config.yaml)
- `data`: Steuersatz-Grenzen (`tax_rate_min/max/norm`), Jahre, Punkte-Schwellen fuer CAGR/FCF/Trends
- `providers`: `default`, `force`, `fmp_when_no_suffix`, `suffix_map` (Provider-Routing pro Ticker-Suffix)
- `filters`: `min_market_cap_usd`, `min_history_years`, `min_avg_fcf_5y_usd`, `min_equity_usd`, `min_revenue_cagr_5y`, `exclusion.negative_equity`
- `criteria`: Schwellen je Kriterium (roic_avg5, roe_avg5, net_debt_ebitda, fcf_yield_sbc_avg3, mos_base, ...)
- `sector_rules.groups`: special/financials/reits/cyclicals mit Industry-Patterns
- `score_groups`: Gewichte, Quality/Value-Bloecke, Coverage-Schwellen
- `sanity`: Plausibilitaets-Bereiche je Kennzahl
- `dcf`: Diskontsatz (auch Zykliker), Terminal Growth, Wachstumscaps, `fcf_mode`, `net_debt_mode`, `mos_min_mode`, `candidate_mode`, `robust_discount_rate`, Kauf-/Beobachtungs-Schwellen (`buy_quality_min`, `buy_margin_of_safety_min`, `buy_bear_floor`, `watch_margin_of_safety_min`)
- `sec`: `user_agent` (Pflicht, mit E-Mail), `enabled`, `tolerance`, `max_requests_per_second`, `capex_check`
- `captive_finance.tickers`, `consistency.net_debt_mcap_share`
- `watchlist`: Statuses, extra_tickers, Flags/Alarme, Szenario-Gewichte, Review-Schwellen
- `smart_money`: `funds` (name/cik/typ/gewicht/positions_soll_max), `max_requests_per_second`, `retry_429`, `quarters`, `cusip_map_url` (OpenFIGI), `cusip_map` (statisch), `etf_ticker` (ETF/Index-Liste), `googl_merge`, `rationale_csv`, `rules` (Score 1-4: ein_fonds_anteil_max, gewicht_summe_min, anteil_gewicht1_min, zwei_quartale, verkauf_schwelle, verkauf_min_score)
- `universe`: S&P-500-URL/CSV, STOXX-600-CSV, Batch-Groesse/Pause, Resume, Cache-Datei

## UI (Streamlit)
- **Seite 1 Rangliste**: sortierbare Tabelle (Quality/Value/MoS je Modus/Status/SEC/Flags), ausgeschlossene Ticker als Tabelle mit Grund darunter; Filter nach Sektor/Land/Score
- **Seite 2 Scorecard**: Block-Scores als Balken, Kriterien-Details, DCF-Tabelle (3 Szenarien, 4 Startwert-Modi, Nettoverschuldung umschaltbar), letzte Quartale mit Stichtag, SEC-Abgleich, Charts, Rohdaten mit Quelle
- **Seite 3 Matrix**: Quality (x) vs. MoS (y), Farbe nach Status, Ziel-Quadrant hervorgehoben
- **Seite 4 Import**: CSV-Upload (Spalte `ticker`) oder manuelle Eingabe

## Build-Reihenfolge (Definition of Done je Schritt)
1. Loader fuer einen Ticker; Test: Kennzahlen von 3 Referenzunternehmen manuell gegengeprueft
2. SQLite-Cache plus Loader fuer Ticker-Liste (CSV)
3. Ausschlussfilter und Scoring nach `config.yaml`; Unit-Tests je Kriterium
4. Streamlit-Rangliste
5. Scorecard-Detail
6. DCF-Modul mit Szenarien; Test mit handgerechnetem Beispiel
7. Sektor-Sonderregeln
8. Universum erweitern (S&P 500, STOXX 600), Batch-Lauf mit Fortschrittsanzeige

## Validierung
- Pro Release 3 Unternehmen von Hand nachrechnen (ROIC, FCF-Yield, DCF) und Abweichung < 2 % sicherstellen
- Logging: Datenluecken, API-Fehler, ausgeschlossene Ticker mit Grund
- Look-ahead-Hinweis: Historische Auswertungen nur mit zum Stichtag veroeffentlichten Daten
- Tests: `python -m compileall .` + `python -m pytest` muessen gruen sein (kein Netz in Tests)

## Status (Stand v50)
Der Value-Screener ist vollstaendig umgesetzt: Provider-Schnittstelle (FMP/yfinance), Quality/Value-Scoring mit Renormalisierung, Ausschlussfilter, Sektor-Sonderregeln (Banken/Versicherer/REITs/Versorger/Asset Management), DCF mit 3 Szenarien und 4 Startwert-Modi, SEC-EDGAR-Abgleich mit Capex-Pruefstatus und SBC-Fallback, Robustheits-MoS, Streamlit-App (Rangliste, Scorecard, Matrix, Import), Volllauf-CLI mit Batches/Resume, Universumslader S&P 500/STOXX 600. Darauf aufbauend die Live-Schicht: Watchlist-Tracker mit Preis-/Valuations-Historie und Entscheidungslog (decisions.py inkl. Brier-Kalibrierung), Live-Update mit Kaufzonen und Alarmen, Timing-Modul, Smart-Money-Pipeline (13F-Positionen von 10 Fonds inkl. Aktivisten, CUSIP-Mapping ueber cusip_map/OpenFIGI/Name-Match mit US-Composite-Filter, Options- und Katalysator-Erfassung, Vollstaendigkeitsschutz mit --force, Schema-Guard gegen veraltete DB-Schemata, ETF-Bereinigung, Rationale-Import mit Quellenpflicht fuer Score 4). Alles getestet (508 passed, 1 skipped, kein Netz in Tests); smarter Overlap live: 536 Ticker, Filings bis 2026-Q2. Offene Punkte: Pershing Square meldet zuletzt 2026-Q1 (Luecke, per --force mit Warnung toleriert); rationale_import.csv mit 45 Vorlage-Zeilen wartet auf manuelle Befuellung mit echten Quellen (Score 4).
