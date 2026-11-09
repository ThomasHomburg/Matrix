# Changelog

Versionshistorie des Value-Screeners. Pro Version ein Absatz: was, warum, Tests.
Aktueller Stand siehe "Status" in CLAUDE.md / README.md.

## v50
Projekt-Wegzug in das frische Repository github.com/ThomasHomburg/Matrix2.0 (ein initialer Commit, ohne alte Historie). Doku aufgeraeumt: Versions-Tags (vNN) aus allen Kommentaren/Docstrings entfernt (fachlicher Inhalt steht), Changelog in diese Datei ausgelagert, CLAUDE.md als kompakte strukturierte Doku neu aufgebaut, README beschreibt den aktuellen Stand. Keine Logik-Aenderung: 508 passed, 1 skipped vor und nach dem Aufraeumen.

## v49
Smart-Money-Haertung und Mapping-Fix. (1) Migration: connect() prueft per PRAGMA table_info die Pflichtspalten (periode, accession, report_date) von smart_money_positions; fehlt eine, werden Altzeilen verworfen und die Tabelle aus schema.sql neu aufgebaut. (1b) Schema-Guard: nach jedem DROP+CREATE wird geprueft, ob die Tabelle das aktuelle Schema wirklich hat -- sonst sofort RuntimeError mit Dateipfad statt "no such column" je Fonds. (1c) Migration/Reset/Guard gelten auch fuer smart_money_options (OPTIONS_PFLICHT_SPALTEN = periode; _drop_wenn_veraltet; --reset droppt beide Tabellen). (2) Basisperiode: aelteste Periode je Fonds = aktion "basis", nie "neu"; Score ignoriert sie. (3) Vollstaendigkeitsschutz: score/overlap brechen ab, wenn die neueste Periode eines Fonds aelter als 140 Tage ist oder in den letzten 4 Perioden eine fehlt; (3b) --force uebersteuert ausschliesslich diesen Abbruch (Abbruchmeldung nennt je Fonds Periode + Grund + --force-Ausweg; mit --force Warnzeile je blockiertem Fonds im Log; Ergebnis-CSV wird erst am Ende geschrieben und bleibt bei Abbruch unberuehrt). (4) Mapping-Report je Fonds (Abdeckung nach Wert + Top 20 ungemappte; Anleihen/Optionen/Nicht-US = nicht aktienrelevant). (4a) OpenFIGI-Antwortstruktur korrigiert: je Eingabe {"data": [...]} bzw. {"error": ...}, US-Composite-Eintrag (exchCode "US") wird gewaehlt (sonst gewinnt z. B. das deutsche Listing DUT vor MCO) -- live verifiziert, unmappte aktienrelevante CUSIPs 212 -> 31. (4b) ETF/Index-Ausblendung: cusip_map.is_etf (OpenFIGI securityType ETP/ETF bzw. marketSector MarketSector/Market; ADRs/REITs bleiben draussen) + statische Liste smart_money.etf_ticker; store_quarter speichert ETF-Positionen weiter, score/overlap werten sie nicht (Score 1, Urteil neutral, CSV-Spalte etf=ja am Ende). (5) rationale-template schreibt Import-Vorlage mit Score-4-Kandidaten; rationale-import validiert alle Zeilen vorab (Abbruch mit Zeilennummer bei fehlendem text/quelle_url, atomar), Duplikate (ticker, fonds, quelle_url) per INSERT OR REPLACE ueberschreiben; neue CSV-Spalte rationale_quelle (quelle_url auf 40 Zeichen). (5b) rationale_import.csv als gepruefter Entwurf vorbefuellt (45/45 Zeilen mit live verifizierten Quellen, [ENTWURF]-Praefix; fehlend-Protokoll in rationale_entwurf_fehlend.txt). Tests: 508 passed, 1 skipped (kein Netz in Tests).

## v48
Periode als eindeutiges Label "periode" = Jahr-Qn exklusiv aus reportDate (2026-06-30 -> 2026-Q2); eindeutiger Schluessel (fonds, periode, cusip, accession); 13F-HR/A ersetzt das Original derselben Periode. Neuer Schalter fetch --reset. Vollstaendigkeitspruefung + Mapping-Abdeckung je Fonds nach dem Fetch. CUSIP->Ticker ueber dauerhafte cusip_map-Tabelle, statisches config-cusip_map, OpenFIGI und nameOfIssuer-Fallback. Tests test_v48.

## v47
Quartalslabel exklusiv aus reportDate, MM-DD-YYYY normalisiert; 13F-HR + 13F-HR/A derselben Periode dedupliziert (neueste Fassung gewinnt); report_date-Spalte + Migration (Relabel aus reportDate). Tests test_v47.

## v46
OpenFIGI-CUSIP-Mapping in Chunks (5 ohne / 100 mit API-Key, mind. 2,5 s Pause, 429-Retry), dauerhaft in cusip_map; 13D/13D-A der Aktivisten mit 503-Retry; unmappable CUSIPs einmalig gesammelt. Tests test_v46.

## v45
HTTP-Fehler (403/429) im Fehlerlog dem jeweiligen Fonds zugeordnet. Tests test_v45.

## v44
User-Agent fuer ALLE SEC-Requests aus config (smart_money.user_agent / sec.user_agent), validiert -- UserAgentError vor dem ersten Request; HTTP 403/429 -> 3 Wiederholungen (2/5/10 s). CIK-Aufloesung: config-CIK via submissions-JSON validiert (mind. ein 13F-HR), sonst EDGAR-Firmensuche mit Namensvarianten (nie raten). Push-Ziel ab hier github.com/ThomasHomburg/Matrix. Tests test_v44 (15).

## v43
Fonds-Liste in config (10 Fonds inkl. Aktivisten mit gewicht 0); CIKs mit leerem Feld per EDGAR-Firmensuche aufgeloest; Fonds ohne 13F-HR werden genannt und uebersprungen. Optionen (PUT/CALL) getrennt in smart_money_options; Katalysatoren (13D/13D-A) in smart_money_katalysator; GOOGL->GOOG je Fonds (googl_merge); Score 1-4 nach config rules; overlap --ranking --watch -> smart_money_overlap.csv mit Urteil Bestaetigung/Divergenz/Warnung/neutral. Tests test_v43 (15).

## v42
Erstes Smart-Money-Modul smart_money.py: 13F-HR je Fonds aus SEC EDGAR (User-Agent, max 5/s, 429-Retry), infotable.xml-Parsing, CUSIP->Ticker (OpenFIGI/company_tickers.json), Vergleich mit Vorquartal -> aktion neu/erhoeht/reduziert/verkauft/unveraendert; Tabellen smart_money_positions + smart_money_rationale (nur mit quelle_url); smart_money_score 1-4; Spalten in watchlist_live.csv. Tests test_v42 (12).

## v41
Tracker + Live-Update (erstes SQLite-Modul): schema.sql-Tabellen prices/valuations/watchlist_status/decisions; decisions.py (add/snapshot/review inkl. Brier-Kalibrierung); live_update.py (Watchlist-Preise, MoS live, Kaufzonen, Flags wert_veraltet/alarm); timing.py; config-Block watchlist + ticker_watch_v40.csv; Loader exportiert Kurs/iv_*/naechster_earnings in die ranking-CSV. Tests test_v41 (13).

## Aeltere Versionen (v26-v40)
Screener-Kern: Provider-Schnittstelle (FMP/yfinance), Kennzahlen-Loader, Scoring mit Renormalisierung und Coverage-Regeln, harte Ausschlussfilter, Sektor-Sonderregeln (Banken/Versicherer/REITs/Versorger/Asset Management/Captive Finance), DCF mit 3 Szenarien und 4 Startwert-Modi (inkl. fcf_le0, robust 10 %, Szenario-Wachstum bei CAGR < 0), SEC-EDGAR-Abgleich (OCF/Capex/SBC inkl. SBC-Fallback und Capex-Pruefstatus), Bilanz-Konsistenz, Volllauf-CLI mit Batches/Resume, Universumslader (S&P 500/STOXX 600), Streamlit-App (Rangliste, Scorecard, Matrix, Import). Details siehe Git-Historie des Vorgaenger-Repos.
