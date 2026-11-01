-- v41 (1): Datenhaltung des Value-Screeners in SQLite (value_tracker.db).
-- Reine Ablage -- die Screener-Logik bleibt unveraendert (neues Modul).
--
-- value_tracker.db anlegen:
--   sqlite3 value_tracker.db < schema.sql
-- (oder decisions.py legt die Tabellen beim ersten Aufruf automatisch an:
--  init_db() fuehrt dieses Schema aus.)

PRAGMA journal_mode = WAL;

-- (a) prices: Tagesschlusskurse je Ticker (Quelle: yfinance, live_update.py)
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT    NOT NULL,
    datum       TEXT    NOT NULL,              -- ISO-Datum (YYYY-MM-DD), Tagesschluss
    kurs        REAL    NOT NULL,              -- Schlusskurs in Quote-Waehrung
    quelle      TEXT    NOT NULL DEFAULT 'yfinance',
    geladen_am  TEXT    NOT NULL,              -- ISO-Zeitpunkt des Abrufs
    PRIMARY KEY (ticker, datum)
);
CREATE INDEX IF NOT EXISTS idx_prices_ticker_datum ON prices (ticker, datum);

-- (b) valuations: Snapshot der Screener-Bewertung je calc_date
--     (eine Zeile pro Ticker und Berechnungstag; kommt aus run_ranking.py /
--      ranking CSV -- Felder siehe Spaltenliste run_ranking._write_ranking_csv)
CREATE TABLE IF NOT EXISTS valuations (
    ticker          TEXT NOT NULL,
    calc_date       TEXT NOT NULL,             -- ISO-Datum der Bewertung
    kurs            REAL,                      -- Kurs bei calc_date (Export v41 (0))
    kurs_datum      TEXT,                      -- Stichtag des Kurses
    status          TEXT,                      -- Kandidat / Beobachtung / nein / Pruefen ...
    quality         REAL,
    value           REAL,
    mos_base        REAL,                      -- margin_of_safety (aktiver Modus)
    mos_min         REAL,                      -- mos_rule (min_all: Minimum der Modi)
    mos_robust      REAL,                      -- MoS bei 10 % (robust)
    iv_bear         REAL,                      -- Wert je Aktie Bear (aktiver Modus)
    iv_base         REAL,                      -- Wert je Aktie Base (aktiver Modus)
    iv_bull         REAL,                      -- Wert je Aktie Bull (aktiver Modus)
    iv_avg3         REAL,                      -- iv_base je Startwert-Modus
    iv_latest       REAL,
    iv_ttm          REAL,
    iv_ttm_only     REAL,
    iv_base_10pct   REAL,                      -- iv_base bei robust_discount_rate
    naechster_earnings TEXT,                   -- naechster Earnings-Termin (ISO)
    flags           TEXT,                      -- Flags des Laufs (Leerzeichen-getrennt)
    quelle_csv      TEXT,                      -- ranking CSV, aus der der Snapshot stammt
    PRIMARY KEY (ticker, calc_date)
);
CREATE INDEX IF NOT EXISTS idx_valuations_calc ON valuations (calc_date);

-- (c) watchlist_status: letzte Live-Aktualisierung je Watchlist-Ticker
--     (live_update.py --ranking <csv> schreibt watchlist_live.csv UND diese Tabelle)
CREATE TABLE IF NOT EXISTS watchlist_status (
    ticker          TEXT NOT NULL,
    updated_at      TEXT NOT NULL,             -- ISO-Zeitpunkt des Live-Laufs
    kurs            REAL,                      -- aktueller Tagesschlusskurs
    status_screener TEXT,                      -- Status aus dem Screener-Lauf (ranking CSV)
    status_live     TEXT,                      -- Status bei aktuellem Kurs (is_buy_candidate)
    mos_live_min    REAL,                      -- MoS live, Minimum ueber die Modi (min_all)
    mos_live_robust REAL,                      -- MoS live bei iv_base_10pct
    kaufzone_kurs   REAL,                      -- hoechster Kurs, der die Kandidatenregel erfuellt
    distanz_pct     REAL,                      -- kaufzone_kurs / kurs - 1
    timing_status   TEXT,                      -- positiv / neutral / negativ (timing.py)
    wert_veraltet   INTEGER NOT NULL DEFAULT 0,  -- 1 = nach naechster_earnings oder > 100 Tage alt
    alarm           INTEGER NOT NULL DEFAULT 0,  -- 1 = Kurs <= -10 % seit calc_date oder Tagesbewegung >= +/-8 %
    PRIMARY KEY (ticker)
);

-- (d) decisions: Entscheidungslog (decisions.py add/snapshot/review)
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    datum           TEXT NOT NULL,             -- ISO-Datum der Entscheidung
    aktion          TEXT NOT NULL,             -- watch | kauf | verkauf | nichts | auto
    kurs            REAL NOT NULL,             -- Kurs bei der Entscheidung
    status_screener TEXT,                      -- Screener-Status zum Zeitpunkt
    iv_base         REAL,                      -- innerer Wert Base zum Zeitpunkt
    iv_bear         REAL,
    iv_bull         REAL,
    gew_bear        REAL NOT NULL DEFAULT 0.25,  -- Szenariogewichte (Default 25/50/25)
    gew_base        REAL NOT NULL DEFAULT 0.50,
    gew_bull        REAL NOT NULL DEFAULT 0.25,
    erwartungswert_pct REAL,                   -- p * 100 (p = Summe Gewichte der Szenarien mit iv >= 1,10 x Kurs)
    horizont_monate INTEGER NOT NULL DEFAULT 12, -- 12 oder 24
    thesis          TEXT,                      -- Begruendung (Pflicht bei manuellen Eintraegen)
    break_condition TEXT,                      -- Invalidierung ("break")
    -- review (decisions.py review): wird nach 12/24 Monaten nachgefuellt
    kurs_nachher    REAL,                      -- Kurs nach Ablauf des Horizonts
    rendite_pct     REAL,                      -- Rendite nach Ablauf des Horizonts in %
    ereignis_e      INTEGER,                   -- 1 = Rendite nach 12 M >= +10 %, sonst 0 (NULL bis review)
    brier           REAL,                      -- (p - E)^2 (NULL bis review)
    reviewed_at     TEXT                       -- ISO-Zeitpunkt des review
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions (ticker);
CREATE INDEX IF NOT EXISTS idx_decisions_datum ON decisions (datum);

-- v42 (2): Smart Money -- 13F-Positionen je (Fonds, Ticker, Quartal)
CREATE TABLE IF NOT EXISTS smart_money_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fonds           TEXT NOT NULL,             -- Fondsname (config smart_money.fonds)
    ticker          TEXT NOT NULL,
    quartal         TEXT NOT NULL,             -- Quartal des 13F (YYYY-Qn, z. B. 2025-Q4)
    aktion          TEXT NOT NULL,             -- neu | erhoeht | reduziert | verkauft | unveraendert
    shares          REAL,                      -- Aktien laut 13F
    wert            REAL,                      -- Marktwert der Position (USD)
    anteil_portfolio REAL,                     -- wert / Gesamtwert des Fonds-Quartals
    aenderung_pct   REAL,                      -- Veraenderung shares gg. Vorquartal
    filing_datum    TEXT,                      -- Datum des 13F-Filings (ISO)
    geladen_am      TEXT NOT NULL,             -- ISO-Zeitpunkt des Abrufs
    UNIQUE (fonds, ticker, quartal)
);
CREATE INDEX IF NOT EXISTS idx_smp_ticker ON smart_money_positions (ticker);
CREATE INDEX IF NOT EXISTS idx_smp_quartal ON smart_money_positions (quartal);

-- v42 (2): Rationale (nur manuell oder per Import, immer mit Quelle)
CREATE TABLE IF NOT EXISTS smart_money_rationale (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    fonds       TEXT,                          -- optional: bezogen auf Fonds
    text        TEXT NOT NULL,                 -- Rationale-Text
    quelle_url  TEXT,                          -- Quelle (Pflicht bei Import)
    datum       TEXT,                          -- ISO-Datum der Aeusserung
    typ         TEXT                           -- Brief | Interview | 13D | sonstiges
);
CREATE INDEX IF NOT EXISTS idx_smr_ticker ON smart_money_rationale (ticker);
