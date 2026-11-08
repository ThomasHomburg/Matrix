r"""
(v49 5) Rationale-Template + schaerfere Import-Validierung + rationale_quelle in der CSV
(kein Netz, Fixture-DB):

Kontext: smart_money_rationale ist leer -> rationale_vorhanden=nein ueberall -> Score 4
(Score-3-Bedingung in 2 aufeinanderfolgenden Quartalen + Rationale) wird nie erreicht.

(a) rationale-template enthaelt nur Score-4-Kandidaten (Score-3-Bedingung in 2
    aufeinanderfolgenden Quartalen, aus der DB gelesen), Pflichtfelder markiert.
(b) Import ohne quelle_url -> Abbruch mit Zeilennummer + fehlendem Feld, nichts importiert.
(c) gueltiger Import -> rationale_vorhanden=ja, Score steigt auf 4 (zwei Quartale erfuellt),
    rationale_quelle = quelle_url (40 Zeichen).
(d) Duplikat-Import (ticker, fonds, quelle_url) ueberschreibt statt zu verdoppeln.
(e) rationale_quelle-Spalte am Ende der overlap-CSV (bei rationale_vorhanden=ja gefuellt).
"""

import argparse
import sqlite3

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["user_agent"] = VALID_UA
    sm["funds"] = [{"name": "F", "cik": "0000000001", "gewicht": 1.0}]
    cfg["smart_money"] = sm
    return cfg


def _seed(con, fonds, periode, rd, cusip, ticker, aktion="erhoeht", anteil=0.1):
    con.execute(
        "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum, cusip,"
        " ticker, name, title_of_class, shares, wert, anteil_portfolio, aktion, aenderung_pct,"
        " positions_gesamt, rotation_pct, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fonds, periode, rd, "2026-08-20", cusip, ticker, ticker + " Corp", "COMMON STOCK",
         1.0, 100.0, anteil, aktion, 0.1, 1, None, "t"))
    con.commit()


def _db_zwei_quartale(tmp_path):
    """F kauft AAA in 2026-Q1 und 2026-Q2 (Anteil 10 % >= 5 % bei Gewicht 1,0 -> cond3 in
    beiden Quartalen); BBB nur in 2026-Q2 (kein Score-4-Kandidat)."""
    db = tmp_path / "r.db"
    con = smart_money.connect(db)
    _seed(con, "F", "2026-Q1", "2026-03-31", "111111111", "AAA")
    _seed(con, "F", "2026-Q2", "2026-06-30", "111111111", "AAA")
    _seed(con, "F", "2026-Q2", "2026-06-30", "222222222", "BBB")
    con.close()
    return db


# (a) Template enthaelt nur Score-4-Kandidaten
def test_a_template_nur_score4_kandidaten(tmp_path):
    db = _db_zwei_quartale(tmp_path)
    cfg = _cfg()
    con = smart_money.connect(db)
    rows = smart_money.score4_kandidaten(con, cfg)
    assert rows == [{"ticker": "AAA", "fonds": "F", "text": "", "quelle_url": "", "datum": "", "typ": ""}]
    con.close()
    out = tmp_path / "rationale_import.csv"
    n = smart_money.write_rationale_template(smart_money.connect(db), cfg, str(out))
    assert n == 1
    text = out.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("#") and "quelle_url" in lines[0]     # Pflichtfelder markiert
    assert lines[1] == "ticker,fonds,text,quelle_url,datum,typ"
    assert lines[2] == "AAA,F,,,,"                                      # Vorlage-Zeile, Pflichtfelder leer
    # CLI-Aufruf schreibt ebenfalls
    smart_money.cmd_rationale_template(argparse.Namespace(db=str(db), out=str(tmp_path / "t2.csv")))
    assert (tmp_path / "t2.csv").read_text(encoding="utf-8") == text


# (b) Import ohne quelle_url -> Abbruch mit Zeilennummer, nichts importiert
def test_b_import_ohne_quelle_url_abbruch_zeilennummer(tmp_path):
    db = _db_zwei_quartale(tmp_path)
    csv_file = tmp_path / "imp.csv"
    csv_file.write_text(
        "ticker,fonds,text,quelle_url,datum,typ\n"
        "AAA,F,Buffett erhoeht,,2026-08-20,Brief\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    msg = str(ei.value)
    assert "Zeile 2" in msg and "quelle_url" in msg and "Nichts importiert" in msg
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM smart_money_rationale").fetchone()[0] == 0
    con.close()


# (c) gueltiger Import -> rationale_vorhanden=ja, Score 4, rationale_quelle gesetzt
def test_c_gueltiger_import_score4(tmp_path):
    db = _db_zwei_quartale(tmp_path)
    cfg = _cfg()
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["score"] == 3 and s["rationale_vorhanden"] is False   # noch keine Rationale
    csv_file = tmp_path / "imp.csv"
    url = "https://example.com/buffett-brief-2026-q2-interview-sehr-lang"
    csv_file.write_text(
        "ticker,fonds,text,quelle_url,datum,typ\n"
        f"AAA,F,Buffett erhoeht deutlich,{url},2026-08-20,Brief\n",
        encoding="utf-8")
    smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["rationale_vorhanden"] is True
    assert s["score"] == 4                                          # 2 Quartale cond3 + Rationale
    assert s["rationale_quelle"] == url[:40]                        # auf 40 Zeichen gekuerzt
    # BBB hat nur ein Quartal -> kein Score 4
    assert smart_money.smart_money_score("BBB", cfg, db_path=db)["score"] != 4


# (d) Duplikat-Import ueberschreibt statt zu verdoppeln
def test_d_duplikat_import_ueberschreibt(tmp_path):
    db = _db_zwei_quartale(tmp_path)
    csv_file = tmp_path / "imp.csv"
    csv_file.write_text(
        "ticker,fonds,text,quelle_url,datum,typ\n"
        "AAA,F,Text alt,https://example.com/b,2026-08-20,Brief\n", encoding="utf-8")
    smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    csv_file.write_text(
        "ticker,fonds,text,quelle_url,datum,typ\n"
        "AAA,F,Text neu,https://example.com/b,2026-08-21,Interview\n", encoding="utf-8")
    smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    con = sqlite3.connect(db)
    rows = con.execute("SELECT text, datum, typ FROM smart_money_rationale WHERE ticker='AAA'").fetchall()
    assert rows == [("Text neu", "2026-08-21", "Interview")]        # ueberschrieben, nicht verdoppelt
    con.close()


# (e) rationale_quelle-Spalte am Ende der overlap-CSV
def test_e_overlap_csv_rationale_quelle(tmp_path):
    db = _db_zwei_quartale(tmp_path)
    cfg = _cfg()
    csv_file = tmp_path / "imp.csv"
    url = "https://example.com/sehr-lange-url-mit-vielen-zeichen-xyz"
    csv_file.write_text(
        f"ticker,fonds,text,quelle_url,datum,typ\nAAA,F,Text,{url},2026-08-20,Brief\n", encoding="utf-8")
    smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nAAA,Kandidat,70,0.4\nBBB,Kandidat,70,0.4\n",
                       encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nAAA\nBBB\n", encoding="utf-8")
    out_csv = tmp_path / "overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db,
                               out_path=str(out_csv), force=True)
    cols = list(rows[0].keys())
    assert cols[-1] == "rationale_quelle"                            # neue Spalte am Ende
    by_t = {r["ticker"]: r for r in rows}
    assert by_t["AAA"]["rationale_vorhanden"] == "ja"
    assert by_t["AAA"]["rationale_quelle"] == url[:40]
    assert by_t["AAA"]["smart_money_score"] == 4
    assert by_t["BBB"]["rationale_vorhanden"] == "nein"
    assert by_t["BBB"]["rationale_quelle"] == ""
    header = out_csv.read_text(encoding="utf-8").splitlines()[0]
    assert header.endswith(",rationale_quelle")
