r"""
(v49 3b) --force fuer score/overlap (nur lokale Fixture-DBs, kein Netz):

Kontext: der Vollstaendigkeitsschutz brach score/overlap ab, wenn ein Fonds
die Pruefung nicht bestand (z. B. Pershing Square: neueste Periode alt > 140 Tage
und/oder Periodenluecke) -- die alte Ergebnis-CSV blieb dadurch unveraendert stehen.
--force uebersteuert ausschliesslich DIESEN Abbruch.

(a) stale (neueste Periode > 140 Tage alt): score ohne --force -> SystemExit; Meldung
    nennt Fondsname, Periode, Grund und den Ausweg --force.
(b) derselbe Fall mit --force: overlap schreibt die Ergebnis-CSV (Spalten
    unveraendert), Log-Warnung nennt den blockierten Fonds mit Luecke
    (neueste Periode, Alter in Tagen, fehlende Perioden).
(c) Periodenluecke (letzte 4 Perioden nicht vollstaendig) -> analog (a)/(b).
(d) Gegenprobe vollstaendig aktuell: kein Abbruch auch ohne --force; --force aendert
    das Ergebnis nicht (CSV byte-identisch).
(e) --force uebersteuert NICHT andere Guards: Schema-Guard (RuntimeError trotz
    --force) und User-Agent-Pruefung (UserAgentError trotz --force).
(f) Guard-Abbruch fasst eine vorhandene Ergebnis-CSV NICHT an (CSV wird erst am Ende
    geschrieben).
"""

import argparse
import logging
import sqlite3
from datetime import date, timedelta

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"

def _rd(periode: str) -> str:
    """report_date (Quartalsende) einer Periode 'YYYY-Qn'."""
    y, q = periode.split("-Q")
    return f"{y}-" + {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[int(q)]

OVERLAP_COLS = ("ticker,status_all,quality,mos_rule,smart_money_score,fonds_kaeufer,"
                "fonds_verkaeufer,letzte_aktion,filing_datum,daten_alter_tage,"
                "katalysator_13d,rationale_vorhanden,smart_money_urteil,etf,rationale_quelle")  # etf; : rationale_quelle am Ende


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["user_agent"] = VALID_UA
    sm["max_requests_per_second"] = 100000
    sm["funds"] = [{"name": "FondsAktuell", "cik": "0000000001", "gewicht": 1.0},
                   {"name": "Pershing Square", "cik": "0000000002", "gewicht": 1.0}]
    sm["cusip_map"] = {"111111111": "AAA", "222222222": "BBB"}
    cfg["smart_money"] = sm
    return cfg


def _seed(con, fonds, rows):
    """rows: (periode, report_date)"""
    for p, rd in rows:
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum, cusip,"
            " ticker, name, title_of_class, shares, wert, anteil_portfolio, aktion, aenderung_pct,"
            " positions_gesamt, rotation_pct, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fonds, p, rd, "2026-08-20", "111111111", "AAA", "AAA Corp", "COMMON STOCK",
             1.0, 100.0, 0.1, "erhoeht", 0.1, 1, None, "t"))
    con.commit()


def _newest_periode() -> str:
    """Neueste Periode mit report_date vor 45 Tagen (immer <= 140 Tage alt)."""
    return smart_money.quartal_of((date.today() - timedelta(days=45)).isoformat())


def _full_rows():
    """8 vollstaendige Perioden ab newest -> ok."""
    newest = _newest_periode()
    return [(p, _rd(p)) for p in smart_money.expected_periodes(newest, 8)]


def _stale_rows():
    """Neueste Periode mit report_date vor 220 Tagen (> 140) -> stale."""
    stale_rd = (date.today() - timedelta(days=220)).isoformat()
    stale_p = smart_money.quartal_of(stale_rd)
    rows = [(p, _rd(p)) for p in smart_money.expected_periodes(stale_p, 8)[:-1]]
    return rows + [(stale_p, stale_rd)]


def _gap_rows():
    """Neueste Periode frisch, aber Vorperiode fehlt -> Luecke in letzten 4."""
    newest = _newest_periode()
    missing = smart_money.periode_add(newest, -1)
    rows = [(p, _rd(p)) for p in smart_money.expected_periodes(newest, 8) if p != missing]
    return rows, missing


def _ranking_watch(tmp_path):
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nAAA,Kandidat,70,0.4\n", encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nAAA\n", encoding="utf-8")
    return str(ranking), str(watch)


# (a) stale: score ohne --force bricht ab; Meldung nennt Fonds, Periode, Grund, --force
def test_a_stale_abbruch_mit_fonds_periode_grund_und_force(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", _full_rows())
    stale = _stale_rows()
    _seed(con, "Pershing Square", stale)
    con.close()
    cfg = _cfg()
    monkeypatch.setattr(smart_money, "load_config", lambda: cfg)
    args = argparse.Namespace(db=str(db), tickers=["AAA"], ranking=None, force=False)
    with pytest.raises(SystemExit) as ei:
        smart_money.cmd_score(args)
    msg = str(ei.value)
    assert "Vollstaendigkeitsschutz" in msg
    assert "Pershing Square" in msg                 # Fondsname
    assert stale[-1][0] in msg                       # Periode der blockierten Berechnung
    assert f"{stale[-1][1]}" in msg                  # report_date
    assert "Tage alt" in msg and "Grund:" in msg     # Grund (Alter)
    assert "--force" in msg                          # expliziter Ausweg


# (b) mit --force: overlap schreibt die CSV (Spalten unveraendert), Warnung je Fonds
def test_b_force_schreibt_csv_warnung_mit_luecke(tmp_path, caplog):
    db = tmp_path / "b.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", _full_rows())
    stale = _stale_rows()
    _seed(con, "Pershing Square", stale)
    con.close()
    cfg = _cfg()
    ranking, watch = _ranking_watch(tmp_path)
    out_csv = tmp_path / "overlap.csv"
    with caplog.at_level(logging.WARNING, logger="value_screener.smart_money"):
        rows = smart_money.overlap(ranking, watch, cfg, db_path=db,
                                   out_path=str(out_csv), force=True)
    assert out_csv.exists()
    header = out_csv.read_text(encoding="utf-8").splitlines()[0]
    assert header == OVERLAP_COLS                    # Spalten unveraendert
    assert [r["ticker"] for r in rows] == ["AAA"]
    assert "Pershing Square" in caplog.text and "Luecke" in caplog.text
    assert stale[-1][0] in caplog.text               # neueste Periode in der Warnung
    assert "fehlend letzte" in caplog.text


# (c) Periodenluecke: Abbruch ohne --force nennt die fehlende Periode, mit --force Warnung
def test_c_periodenluecke_abbruch_und_force(tmp_path, monkeypatch, caplog):
    db = tmp_path / "c.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", _full_rows())
    gap, missing = _gap_rows()
    _seed(con, "Pershing Square", gap)
    con.close()
    cfg = _cfg()
    monkeypatch.setattr(smart_money, "load_config", lambda: cfg)
    args = argparse.Namespace(db=str(db), tickers=["AAA"], ranking=None, force=False)
    with pytest.raises(SystemExit) as ei:
        smart_money.cmd_score(args)
    msg = str(ei.value)
    assert "Pershing Square" in msg and missing in msg and "--force" in msg
    args.force = True
    with caplog.at_level(logging.WARNING, logger="value_screener.smart_money"):
        smart_money.cmd_score(args)                  # laeuft durch
    assert "Pershing Square" in caplog.text and missing in caplog.text


# (d) vollstaendig aktuell: kein Abbruch ohne --force; --force aendert nichts (CSV identisch)
def test_d_vollstaendig_kein_abbruch_force_identisch(tmp_path):
    db = tmp_path / "d.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", _full_rows())
    _seed(con, "Pershing Square", _full_rows())
    con.close()
    cfg = _cfg()
    ranking, watch = _ranking_watch(tmp_path)
    out1 = tmp_path / "o1.csv"
    out2 = tmp_path / "o2.csv"
    rows1 = smart_money.overlap(ranking, watch, cfg, db_path=db, out_path=str(out1))       # force=False
    rows2 = smart_money.overlap(ranking, watch, cfg, db_path=db, out_path=str(out2), force=True)
    assert out1.exists() and out2.exists()
    assert out1.read_bytes() == out2.read_bytes()    # --force aendert das Ergebnis nicht
    assert [r["ticker"] for r in rows1] == [r["ticker"] for r in rows2] == ["AAA"]


# (e1) --force uebersteuert NICHT den Schema-Guard: RuntimeError schlaegt trotz --force
STALE_SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_money_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fonds TEXT NOT NULL, quartal TEXT NOT NULL, filing_datum TEXT, ticker TEXT NOT NULL,
    shares REAL, wert REAL, anteil_portfolio REAL, aktion TEXT NOT NULL, aenderung_pct REAL,
    positions_gesamt INTEGER, rotation_pct REAL, geladen_am TEXT NOT NULL,
    UNIQUE (fonds, quartal, ticker)
);
"""


def test_e1_schema_guard_schlaegt_trotz_force(tmp_path, monkeypatch):
    fixture = tmp_path / "schema_stale.sql"
    fixture.write_text(STALE_SCHEMA, encoding="utf-8")
    monkeypatch.setattr(smart_money, "SCHEMA_PATH", fixture)
    db = tmp_path / "e1.db"
    raw = sqlite3.connect(db)
    raw.executescript(STALE_SCHEMA)
    raw.commit()
    raw.close()
    cfg = _cfg()
    ranking, watch = _ranking_watch(tmp_path)
    with pytest.raises(RuntimeError, match="aktuelle Schema"):
        smart_money.overlap(ranking, watch, cfg, db_path=db,
                            out_path=str(tmp_path / "o.csv"), force=True)


# (e2) --force uebersteuert NICHT die User-Agent-Pruefung: UserAgentError trotz --force
def test_e2_user_agent_guard_schlaegt_trotz_force(tmp_path, monkeypatch):
    cfg = _cfg()
    cfg["smart_money"]["user_agent"] = "test"       # ungueltig -> UserAgentError
    monkeypatch.setattr(smart_money, "load_config", lambda: cfg)
    args = argparse.Namespace(db=str(tmp_path / "e2.db"), quarters=None, reset=False, force=True)
    with pytest.raises(smart_money.UserAgentError):
        smart_money.cmd_fetch(args)


# (f) Guard-Abbruch fasst eine vorhandene Ergebnis-CSV nicht an (CSV erst am Ende)
def test_f_abbruch_laesst_alte_csv_unveraendert(tmp_path):
    db = tmp_path / "f.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", _full_rows())
    _seed(con, "Pershing Square", _stale_rows())
    con.close()
    cfg = _cfg()
    ranking, watch = _ranking_watch(tmp_path)
    out_csv = tmp_path / "smart_money_overlap.csv"
    out_csv.write_text("SENTINEL\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Vollstaendigkeitsschutz"):
        smart_money.overlap(ranking, watch, cfg, db_path=db, out_path=str(out_csv), force=False)
    assert out_csv.read_text(encoding="utf-8") == "SENTINEL\n"   # unveraendert
