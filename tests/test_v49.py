r"""
(v49) smart_money.py -- Migration + Plausibilitaetsschutz (nur Mocks, kein Netz):

(1) Migration: connect() prueft per PRAGMA table_info die Pflichtspalten
    (periode, accession, report_date); fehlt eine -> Altzeilen werden verworfen
    (Periodenlabel falsch) mit Hinweis im Log, Tabelle wird neu aufgebaut.
    `fetch --reset` legt die Tabelle per DROP + CREATE neu an (cusip_map bleibt).
    Test mit DB im alten Schema: fetch laeuft ohne "no such column".
(2) Basisperiode: aelteste geladene Periode je Fonds -> aktion "basis" (nie "neu");
    neu/erhoeht/reduziert/verkauft nur mit Vorquartal. Score/U Urteil ignorieren "basis".
(3) Vollstaendigkeitsschutz: score/overlap brechen ab, wenn die neueste Periode
    eines Fonds aelter als 140 Tage ist oder in den letzten 4 Perioden eine fehlt
    (--force ueberstimmt); die Ausgabe nennt je Fonds die Periode der Berechnung.
(4) Mapping: Abdeckung je Fonds nach Wert + Top 20 nicht zugeordnete Positionen
    (CUSIP, name, wert, titleOfClass); Anleihen/Optionen/Nicht-US = "nicht
    aktienrelevant", zaehlen nicht als Fehler.
(5) Watchlist: overlap --watch trimmt BOM + Leerzeichen und listet Watch-Ticker
    ohne Fonds-Treffer ausdruecklich auf (CME, MCO, MSCI, DVA).
"""

import argparse
import json
import logging
import sqlite3
from datetime import date

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"
CIK10 = "0001067983"

# 3 Perioden: 2025-Q1 (Basis), 2025-Q2, 2025-Q3 -- AAA steigt jeweils
ENTRIES3 = [
    ("0001067983-25-000001", "2025-05-15", "13F-HR", "2025-03-31"),
    ("0001067983-25-000002", "2025-08-15", "13F-HR", "2025-06-30"),
    ("0001067983-25-000003", "2025-11-14", "13F-HR", "2025-09-30"),
]
HOLD3 = {
    "0001067983-25-000001": [("111111111", "AAA Corp", 1000.0, 100, "COMMON STOCK")],
    "0001067983-25-000002": [("111111111", "AAA Corp", 1500.0, 150, "COMMON STOCK"),
                             ("222222222", "BBB Inc", 200.0, 20, "COMMON STOCK")],
    "0001067983-25-000003": [("111111111", "AAA Corp", 2000.0, 200, "COMMON STOCK"),
                             ("222222222", "BBB Inc", 200.0, 20, "COMMON STOCK")],
}
ENTRIES1 = [("0001067983-26-000008", "2026-08-15", "13F-HR", "2026-06-30")]
HOLD1 = {"0001067983-26-000008": [("111111111", "AAA Corp", 1000.0, 100, "COMMON STOCK")]}


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["max_requests_per_second"] = 100000
    sm["user_agent"] = VALID_UA
    sm["funds"] = [{"name": "Berkshire Hathaway", "cik": CIK10, "typ": "konzern", "gewicht": 1.0}]
    sm["cusip_map"] = {"111111111": "AAA", "222222222": "BBB"}
    sm["cusip_map_url"] = ""
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


def _submissions_json(entries, name="BERKSHIRE HATHAWAY INC"):
    return json.dumps({
        "name": name,
        "filings": {"recent": {
            "accessionNumber": [e[0] for e in entries],
            "filingDate": [e[1] for e in entries],
            "form": [e[2] for e in entries],
            "reportDate": [e[3] for e in entries],
        }}}).encode()


def _infotable_xml(holdings, period="2026-06-30"):
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<titleOfClass>{t}</titleOfClass>"
        f"<shrsOrPrnAmt>{s} sh</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, s, v, t in holdings)
    return (f'<?xml version="1.0"?><informationTable>'
            f'<periodOfReport>{period}</periodOfReport>{rows}</informationTable>').encode()


def _fetcher(entries, hold_by_acc):
    def fetcher(url: str) -> bytes:
        if f"submissions/CIK{CIK10}.json" in url:
            return _submissions_json(entries)
        if url.endswith("/index.json"):
            return json.dumps({"directory": {"item": [{"name": "primary_doc.xml"},
                                                      {"name": "infotable.xml"}]}}).encode()
        if url.endswith("infotable.xml"):
            acc_digits = url.split("/")[-2]
            for acc, _fd, _form, rd in entries:
                if acc.replace("-", "") == acc_digits:
                    return _infotable_xml(hold_by_acc[acc], period=rd)
            raise ValueError(url)
        if url.endswith("primary_doc.xml"):
            return b'<?xml version="1.0"?><doc></doc>'
        raise ValueError(url)
    return fetcher


def _seed(con, fonds, rows):
    """rows: (periode, report_date, cusip, ticker, name, title_of_class, wert)"""
    for periode, rd, cusip, ticker, name, toc, wert in rows:
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum, cusip,"
            " ticker, name, title_of_class, shares, wert, anteil_portfolio, aktion, aenderung_pct,"
            " positions_gesamt, rotation_pct, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fonds, periode, rd, "2026-08-20", cusip, ticker, name, toc, 1.0, wert, 0.1, "neu",
             None, 1, None, "t"))
    con.commit()


# --------------------------------------------------------------------------
# (1) Migration: altes Schema -> fetch ohne "no such column"; --reset DROP+CREATE
# --------------------------------------------------------------------------

def test_migration_alte_schema_fetch_laeuft_ohne_no_such_column(tmp_path, caplog):
    db = tmp_path / "alt.db"
    raw = sqlite3.connect(db)
    raw.execute("""CREATE TABLE smart_money_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fonds TEXT NOT NULL, quartal TEXT NOT NULL,
        filing_datum TEXT, ticker TEXT NOT NULL, shares REAL, wert REAL, anteil_portfolio REAL,
        aktion TEXT NOT NULL, aenderung_pct REAL, positions_gesamt INTEGER, rotation_pct REAL,
        geladen_am TEXT NOT NULL, UNIQUE (fonds, quartal, ticker))""")
    raw.execute("INSERT INTO smart_money_positions (fonds, quartal, filing_datum, ticker, shares,"
                " wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)"
                " VALUES ('F','06-Q10','2026-08-15','AAA',1,1,0.1,'neu',NULL,1,NULL,'t')")
    raw.commit()
    raw.close()
    cfg = _cfg()
    with caplog.at_level(logging.WARNING, logger="value_screener.smart_money"):
        errors = smart_money.fetch_all(cfg, quarters=3, db_path=db,
                                       fetcher=_fetcher(ENTRIES3, HOLD3), sleeper=lambda s: None)
    assert not any("no such column" in str(e.get("error", "")) for e in errors), errors
    assert any("verworfen" in r.getMessage() for r in caplog.records)   # Hinweis im Log
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(smart_money_positions)")}
    assert {"periode", "accession", "report_date", "cusip", "name", "title_of_class"} <= cols
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions WHERE periode='06-Q10'"
                       ).fetchone()[0] == 0          # Altzeilen verworfen
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions").fetchone()[0] == 5


def test_fetch_reset_drop_und_create_cusip_map_bleibt(tmp_path):
    db = tmp_path / "sm.db"
    con = smart_money.connect(db)
    con.execute("INSERT INTO cusip_map (cusip, ticker, quelle, geladen_am) VALUES ('111111111','AAA','manuell','t')")
    con.execute("INSERT INTO smart_money_positions (fonds, periode, filing_datum, cusip, ticker,"
                " shares, wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt,"
                " rotation_pct, geladen_am) VALUES ('F','2020-Q1','2020-02-01','stale','OLD',1,1,0.1,"
                "'neu',NULL,1,NULL,'t')")
    con.commit()
    con.close()
    cfg = _cfg()
    smart_money.fetch_all(cfg, quarters=3, db_path=db, fetcher=_fetcher(ENTRIES3, HOLD3),
                          sleeper=lambda s: None, reset=True)
    con = sqlite3.connect(db)
    assert con.execute("SELECT ticker FROM cusip_map WHERE cusip='111111111'").fetchone() == ("AAA",)
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions WHERE periode='2020-Q1'"
                       ).fetchone()[0] == 0
    periodes = {r[0] for r in con.execute("SELECT DISTINCT periode FROM smart_money_positions")}
    assert periodes == {"2025-Q1", "2025-Q2", "2025-Q3"}   # neu geladen


# --------------------------------------------------------------------------
# (2) Basisperiode
# --------------------------------------------------------------------------

def test_zwei_perioden_aktionen_korrekt_erste_ist_basis(tmp_path):
    cfg = _cfg()
    db = tmp_path / "sm.db"
    entries = ENTRIES3[:2]
    hold = {k: HOLD3[k] for k in entries and [e[0] for e in entries]}
    smart_money.fetch_all(cfg, quarters=2, db_path=db, fetcher=_fetcher(entries, hold),
                          sleeper=lambda s: None)
    con = sqlite3.connect(db)
    q1 = con.execute("SELECT ticker, aktion, aenderung_pct FROM smart_money_positions "
                     "WHERE periode='2025-Q1'").fetchall()
    q2 = con.execute("SELECT ticker, aktion, aenderung_pct FROM smart_money_positions "
                     "WHERE periode='2025-Q2' ORDER BY ticker").fetchall()
    assert all(a == "basis" and ch is None for _t, a, ch in q1)          # aelteste Periode: basis
    assert q2 == [("AAA", "erhoeht", pytest.approx(0.5)), ("BBB", "neu", None)]


def test_eine_periode_alles_basis_und_score_ignoriert_basis(tmp_path):
    cfg = _cfg()
    db = tmp_path / "sm.db"
    smart_money.fetch_all(cfg, quarters=1, db_path=db, fetcher=_fetcher(ENTRIES1, HOLD1),
                          sleeper=lambda s: None)
    con = sqlite3.connect(db)
    rows = con.execute("SELECT ticker, aktion FROM smart_money_positions").fetchall()
    assert rows == [("AAA", "basis")]
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["score"] == 1 and s["fonds_kaeufer"] == [] and s["fonds_verkaeufer"] == []


def test_score_4_mit_drei_perioden_zwei_neueste_nicht_basis(tmp_path):
    cfg = _cfg()
    db = tmp_path / "sm.db"
    smart_money.fetch_all(cfg, quarters=3, db_path=db, fetcher=_fetcher(ENTRIES3, HOLD3),
                          sleeper=lambda s: None)
    con = smart_money.connect(db)
    con.execute("INSERT INTO smart_money_rationale (ticker, text, quelle_url) "
                "VALUES ('AAA','Buffett erhoeht','https://example.com/b')")
    con.commit()
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    # 2025-Q1 basis; 2025-Q2 + 2025-Q3 je cond3 (Anteil 100 % >= 5 % bei Gewicht 1,0) -> 4
    assert s["score"] == 4
    assert s["fonds_kaeufer"] == ["Berkshire Hathaway"]


# --------------------------------------------------------------------------
# (3) Vollstaendigkeitsschutz: 140 Tage / Luecke in letzten 4 Perioden, --force
# --------------------------------------------------------------------------

def _cfg_f():
    cfg = _cfg()
    cfg["smart_money"]["funds"] = [{"name": "F", "cik": "0000000001", "gewicht": 1.0}]
    return cfg


def test_plausibility_report_ok_und_stale(tmp_path):
    con = smart_money.connect(tmp_path / "p.db")
    _seed(con, "F", [(p, rd, "c1", "AAA", "A", "COMMON STOCK", 100.0) for p, rd in
                     (("2025-Q3", "2025-09-30"), ("2025-Q4", "2025-12-31"),
                      ("2026-Q1", "2026-03-31"), ("2026-Q2", "2026-06-30"))])
    rep = smart_money.plausibility_report(con, _cfg_f(), heute=date(2026, 9, 30))
    assert rep[0]["neueste"] == "2026-Q2" and rep[0]["alter_tage"] == 92
    assert rep[0]["fehlend_letzte"] == [] and rep[0]["ok"] is True
    # stale: neueste Periode 2025-Q1 (report 2025-03-31) -> 548 Tage > 140
    con2 = smart_money.connect(tmp_path / "p2.db")
    _seed(con2, "F", [(p, rd, "c1", "AAA", "A", "COMMON STOCK", 100.0) for p, rd in
                      (("2024-Q2", "2024-06-30"), ("2024-Q3", "2024-09-30"),
                       ("2024-Q4", "2024-12-31"), ("2025-Q1", "2025-03-31"))])
    rep2 = smart_money.plausibility_report(con2, _cfg_f(), heute=date(2026, 9, 30))
    assert rep2[0]["neueste"] == "2025-Q1" and rep2[0]["alter_tage"] == 548
    assert rep2[0]["ok"] is False
    # Luecke in den letzten 4 Perioden
    con3 = smart_money.connect(tmp_path / "p3.db")
    _seed(con3, "F", [("2026-Q1", "2026-03-31", "c1", "AAA", "A", "COMMON STOCK", 100.0),
                      ("2026-Q2", "2026-06-30", "c1", "AAA", "A", "COMMON STOCK", 100.0)])
    rep3 = smart_money.plausibility_report(con3, _cfg_f(), heute=date(2026, 9, 30))
    assert rep3[0]["fehlend_letzte"] == ["2025-Q3", "2025-Q4"] and rep3[0]["ok"] is False


def test_score_bricht_bei_stale_ab_force_ueberstimmt(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    con = smart_money.connect(db)
    _seed(con, "F", [(p, rd, "c1", "AAA", "A", "COMMON STOCK", 100.0) for p, rd in
                     (("2024-Q2", "2024-06-30"), ("2024-Q3", "2024-09-30"),
                      ("2024-Q4", "2024-12-31"), ("2025-Q1", "2025-03-31"))])
    con.close()
    cfg = _cfg_f()
    monkeypatch.setattr(smart_money, "load_config", lambda: cfg)
    args = argparse.Namespace(db=str(db), tickers=["AAA"], ranking=None, force=False)
    with pytest.raises(SystemExit, match="Vollstaendigkeitsschutz"):
        smart_money.cmd_score(args)
    args.force = True
    smart_money.cmd_score(args)
    out = capsys.readouterr().out
    assert "neueste Periode 2025-Q1" in out      # nennt die Periode der Berechnung


def test_overlap_bricht_bei_stale_ab_und_force(tmp_path):
    db = tmp_path / "o.db"
    con = smart_money.connect(db)
    _seed(con, "F", [("2026-Q2", "2026-06-30", "c1", "AAA", "A", "COMMON STOCK", 100.0)])
    con.close()
    cfg = _cfg_f()
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nAAA,Kandidat,70,0.4\n", encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nAAA\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Vollstaendigkeitsschutz"):
        smart_money.overlap(str(ranking), str(watch), cfg, db_path=db)
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db,
                               out_path=str(tmp_path / "overlap.csv"), force=True)
    assert [r["ticker"] for r in rows] == ["AAA"]


# --------------------------------------------------------------------------
# (4) Mapping: Abdeckung nach Wert + Top 20, nicht aktienrelevant
# --------------------------------------------------------------------------

def test_nicht_aktienrelevant():
    assert smart_money._nicht_aktienrelevant("023135106", "BOND") is True
    assert smart_money._nicht_aktienrelevant("594918104", "NOTE 4.5%") is True
    assert smart_money._nicht_aktienrelevant("G1234567", "COMMON STOCK") is True   # Nicht-US-CUSIP
    assert smart_money._nicht_aktienrelevant("037833100", "COMMON STOCK") is False
    assert smart_money._nicht_aktienrelevant("037833100", "") is False


def test_mapping_coverage_top20_mit_flags(tmp_path):
    con = smart_money.connect(tmp_path / "m.db")
    _seed(con, "F", [
        ("2026-Q2", "2026-06-30", "111111111", "AAA", "AAA Corp", "COMMON STOCK", 950.0),
        ("2026-Q2", "2026-06-30", "023135106", None, "US TREASURY", "BOND", 30.0),
        ("2026-Q2", "2026-06-30", "G1234567", None, "Foreign AG", "COMMON STOCK", 20.0),
    ])
    rep = smart_money.mapping_coverage(con, fonds="F")[0]
    assert rep["anteil"] == pytest.approx(0.95)          # Ziel >= 95 % nach Wert (US-Aktien)
    assert rep["gesamt_wert"] == 1000.0 and rep["mit_ticker_wert"] == 950.0
    assert len(rep["top_unmapped"]) == 2
    flags = {u["cusip"]: u["nicht_aktienrelevant"] for u in rep["top_unmapped"]}
    assert flags == {"023135106": True, "G1234567": True}
    assert rep["top_unmapped"][0]["title_of_class"] == "BOND"


def test_fetch_anleihe_unmapped_kein_fehler_aber_in_top20(tmp_path):
    entries = [("0001067983-26-000008", "2026-08-15", "13F-HR", "2026-06-30")]
    hold = {"0001067983-26-000008": [
        ("111111111", "AAA Corp", 1000.0, 100, "COMMON STOCK"),
        ("023135106", "US TREASURY BOND", 30.0, 3, "BOND"),
    ]}
    cfg = _cfg()
    db = tmp_path / "sm.db"
    report = []
    errors = smart_money.fetch_all(cfg, quarters=1, db_path=db, fetcher=_fetcher(entries, hold),
                                   report=report, sleeper=lambda s: None)
    assert not any("nicht zuordenbar" in e.get("error", "") for e in errors)  # Anleihe kein Fehler
    top = report[0]["mapping"]["top_unmapped"]
    assert len(top) == 1 and top[0]["cusip"] == "023135106"
    assert top[0]["nicht_aktienrelevant"] is True and top[0]["title_of_class"] == "BOND"
    assert report[0]["mapping"]["anteil"] == pytest.approx(100000.0 / 103000.0)


# --------------------------------------------------------------------------
# (5) Watchlist: BOM + Leerzeichen trimmen, Watch-Ticker ohne Treffer auflisten
# --------------------------------------------------------------------------

def test_load_watch_csv_bom_und_leerzeichen(tmp_path):
    p = tmp_path / "watch.csv"
    p.write_bytes("\ufeffticker\r\n CME \r\nMCO\r\n# Kommentar\r\n\r\nMSCI\r\nDVA\r\n".encode("utf-8"))
    assert smart_money.load_watch_csv(str(p)) == ["CME", "MCO", "MSCI", "DVA"]


def test_overlap_listet_watch_ticker_ohne_treffer(tmp_path, monkeypatch, capsys):
    db = tmp_path / "w.db"
    con = smart_money.connect(db)
    _seed(con, "F", [("2026-Q2", "2026-06-30", "111111111", "AAA", "AAA Corp", "COMMON STOCK", 100.0)])
    con.close()
    cfg = _cfg_f()
    ranking = tmp_path / "ranking.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nCME,Kandidat,70,0.4\n", encoding="utf-8")
    watch = tmp_path / "watch.csv"
    watch.write_bytes("\ufeffticker\r\n CME \r\nMCO\r\nMSCI\r\nDVA\r\n".encode("utf-8"))
    out_csv = tmp_path / "overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db,
                               out_path=str(out_csv), force=True)
    tickers = {r["ticker"] for r in rows}
    assert {"CME", "MCO", "MSCI", "DVA"} <= tickers          # stehen in der Ausgabe
    monkeypatch.setattr(smart_money, "load_config", lambda: cfg)
    args = argparse.Namespace(db=str(db), ranking=str(ranking), watch=str(watch),
                              out=str(out_csv), force=True)
    smart_money.cmd_overlap(args)
    out = capsys.readouterr().out
    assert "Watch-Ticker ohne Fonds-Treffer (4): CME, MCO, MSCI, DVA" in out
