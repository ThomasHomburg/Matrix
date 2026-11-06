r"""
(v49 1b) Schema-Guard smart_money_positions (nur lokale DBs, kein Netz):

Ursache des Fehlers auf der Zielmaschine: `fetch --reset` brach je Fonds mit
sqlite3.OperationalError "no such column: periode" ab -- Migration und --reset
legten die Tabelle per DROP TABLE + executescript(schema.sql) neu an, prueften
aber nicht, ob die neu angelegte Tabelle die Pflichtspalten wirklich hat. Lag
neben smart_money.py eine veraltete schema.sql (v42/v43, Spalte quartal), wurde
die Tabelle erneut ohne periode angelegt; store_quarter scheiterte je Fonds und
der Altbestand (2024-Q3, ~722 Tage alt) blieb stehen.

Fix (nur smart_money.py): _fehlende_pflichtspalten(con) (PRAGMA table_info vs.
MIGRATION_PFLICHT_SPALTEN = periode/accession/report_date) und
_assert_positions_schema(con) -> RuntimeError mit klarer Meldung (kein
Silent-Continue, kein Teil-Lauf). Aufruf an 3 Stellen: _migrate_positions (nach
DROP+executescript+commit vor return), connect (letzte Zeile nach
_migrate_positions), fetch_all --reset (nach DROP+executescript+commit vor dem
INFO-Log).

Tests: (a) frische DB + aktuelles schema.sql -> connect ok, Pflichtspalten da;
(b) alte Tabelle + veraltete schema.sql (Fixture, SCHEMA_PATH per monkeypatch) ->
connect wirft RuntimeError (Meldung enthaelt periode und schema.sql), Altzustand
bleibt abbruchfaehig, fetch_all wirft sofort RuntimeError; (c) alte Tabelle +
aktuelles schema.sql -> Migration laeuft, Altzeilen verworfen, cusip_map bleibt;
(d) --reset-Pfad mit veraltetem Fixture -> RuntimeError statt Einzelfehler je Fonds.
"""

import sqlite3

import pytest

from config import load_config
import smart_money


# veraltetes schema.sql (v42/v43-Format: Spalte quartal, ohne periode/report_date/accession)
STALE_SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_money_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fonds TEXT NOT NULL,
    quartal TEXT NOT NULL,
    filing_datum TEXT,
    ticker TEXT NOT NULL,
    shares REAL, wert REAL, anteil_portfolio REAL,
    aktion TEXT NOT NULL, aenderung_pct REAL,
    positions_gesamt INTEGER, rotation_pct REAL,
    geladen_am TEXT NOT NULL,
    UNIQUE (fonds, quartal, ticker)
);
CREATE INDEX IF NOT EXISTS idx_smp_quartal ON smart_money_positions (quartal);
"""


def _old_table(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE smart_money_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fonds TEXT NOT NULL, quartal TEXT NOT NULL,
        filing_datum TEXT, ticker TEXT NOT NULL, shares REAL, wert REAL, anteil_portfolio REAL,
        aktion TEXT NOT NULL, aenderung_pct REAL, positions_gesamt INTEGER, rotation_pct REAL,
        geladen_am TEXT NOT NULL, UNIQUE (fonds, quartal, ticker))""")
    con.execute("INSERT INTO smart_money_positions (fonds, quartal, filing_datum, ticker, shares,"
                " wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)"
                " VALUES ('F','06-Q10','2026-08-15','AAA',1,1,0.1,'neu',NULL,1,NULL,'t')")
    con.commit()


def _stale_fixture(tmp_path, monkeypatch):
    fixture = tmp_path / "schema_stale.sql"
    fixture.write_text(STALE_SCHEMA, encoding="utf-8")
    monkeypatch.setattr(smart_money, "SCHEMA_PATH", fixture)
    return fixture


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["user_agent"] = "Tom Homburg t09homburg@gmail.com"
    sm["max_requests_per_second"] = 100000
    cfg["smart_money"] = sm
    return cfg


# (a) frische DB + aktuelles schema.sql -> connect ok, Pflichtspalten vorhanden
def test_fresh_db_current_schema_ok(tmp_path):
    db = tmp_path / "neu.db"
    con = smart_money.connect(db)
    assert smart_money._fehlende_pflichtspalten(con) == []
    cols = {r[1] for r in con.execute("PRAGMA table_info(smart_money_positions)")}
    assert {"periode", "accession", "report_date"} <= cols
    smart_money._assert_positions_schema(con)      # wirft nichts


# (b) alte Tabelle + veraltete schema.sql -> RuntimeError, Meldung mit periode + schema.sql
def test_stale_schema_connect_raises_runtimeerror(tmp_path, monkeypatch):
    _stale_fixture(tmp_path, monkeypatch)
    db = tmp_path / "alt.db"
    raw = sqlite3.connect(db)
    _old_table(raw)
    raw.close()
    with pytest.raises(RuntimeError, match="fehlende Spalten: periode, accession, report_date"):
        smart_money.connect(db)
    with pytest.raises(RuntimeError, match="schema.sql ist veraltet oder fehlt"):
        smart_money.connect(db)                     # Altzustand bleibt abbruchfaehig
    with pytest.raises(RuntimeError, match="Altzeilen bleiben unveraendert"):
        smart_money.fetch_all(_cfg(), db_path=db, fetcher=lambda u: b"",
                              sleeper=lambda s: None)   # kein Teil-Lauf, kein Einzelfehler je Fonds
    con = sqlite3.connect(db)
    assert smart_money._fehlende_pflichtspalten(con) == ["periode", "accession", "report_date"]
    with pytest.raises(RuntimeError):
        smart_money._assert_positions_schema(con)


# (c) alte Tabelle + aktuelles schema.sql -> Migration: neu, Pflichtspalten da, Altzeilen weg, cusip_map bleibt
def test_old_table_current_schema_migrates(tmp_path):
    db = tmp_path / "alt2.db"
    raw = sqlite3.connect(db)
    _old_table(raw)
    raw.execute("CREATE TABLE IF NOT EXISTS cusip_map (cusip TEXT PRIMARY KEY, ticker TEXT NOT NULL,"
                " quelle TEXT, geladen_am TEXT NOT NULL)")
    raw.execute("INSERT INTO cusip_map VALUES ('111111111','AAA','manuell','t')")
    raw.commit()
    raw.close()
    con = smart_money.connect(db)                   # aktuelles SCHEMA_PATH (nicht gepatcht)
    cols = {r[1] for r in con.execute("PRAGMA table_info(smart_money_positions)")}
    assert {"periode", "accession", "report_date"} <= cols
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions").fetchone()[0] == 0  # Altzeilen verworfen
    assert con.execute("SELECT ticker FROM cusip_map WHERE cusip='111111111'").fetchone() == ("AAA",)
    assert smart_money._fehlende_pflichtspalten(con) == []
    smart_money._assert_positions_schema(con)       # aktuelles Schema -> wirft nichts


# (d) --reset-Pfad simulieren: DROP + executescript mit veraltetem Fixture -> RuntimeError
def test_reset_path_stale_schema_raises(tmp_path, monkeypatch):
    fixture = _stale_fixture(tmp_path, monkeypatch)
    db = tmp_path / "reset.db"
    con = sqlite3.connect(db)
    con.executescript(STALE_SCHEMA)
    # fetch_all --reset-Zweig: DROP TABLE + executescript(SCHEMA_PATH) + commit + assert
    con.execute("DROP TABLE smart_money_positions")
    con.executescript(fixture.read_text(encoding="utf-8"))
    con.commit()
    with pytest.raises(RuntimeError, match="fehlende Spalten: periode"):
        smart_money._assert_positions_schema(con)
