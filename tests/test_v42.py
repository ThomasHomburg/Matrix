"""v42 Tests (Fixtures -- kein Netz, kein Volllauf):

Fixture-13F: 2 Quartale, 3 Fonds (aus config smart_money.fonds) -- Aktionen
(neu/erhoeht/reduziert/verkauft/unveraendert) und smart_money_score korrekt,
plus Ueberlappung mit Watchlist-Tickern (live_update.attach_smart_money).
"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from config import load_config
import smart_money
import live_update

# CUSIPs der Fixture
CUSIP = {"AAA": "111111111", "BBB": "222222222", "CCC": "333333333",
         "DDD": "444444444", "EEE": "555555555"}


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    # v42: statisches CUSIP->Ticker-Mapping fuer die Fixture (kein Netz)
    sm = dict(cfg.get("smart_money", {}))
    sm["cusip_map"] = {v: k for k, v in CUSIP.items()}
    sm["max_requests_per_second"] = 100000  # Fixture: Rate-Limit ausbremsen (kein echtes Netz)
    cfg["smart_money"] = sm
    return cfg


# --- Fixture-XML: minimaler infotable.xml ---------------------------------

def _infotable(holdings: list[tuple[str, str, float, float]]) -> bytes:
    """holdings: (cusip, name, shares, value_in_1000_usd)."""
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<shrsOrPrnAmt>{s} sh</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, s, v in holdings)
    return f'<?xml version="1.0"?><informationTable>{rows}</informationTable>'.encode()


def _atom(entries: list[tuple[str, str, str]]) -> bytes:
    """entries: (accession, filing_date, title)."""
    items = "".join(
        f'<entry><title>{t}</title><updated>{d}T00:00:00Z</updated>'
        f'<link href="https://www.sec.gov/Archives/edgar/data/1/{a}/"/></entry>'
        for a, d, t in entries)
    return ('<?xml version="1.0"?>'
            f'<feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>').encode()


def _make_fetcher():
    """Fetcher: simuliert SEC-Endpunkte fuer 3 Fonds x 2 Quartale.

    Berkshire:  Q1: AAA 1000, BBB 500 | Q2: AAA 1500 (erhoeht), CCC 800 (neu), BBB verkauft
    Pershing:   Q1: AAA 300, DDD 200 | Q2: AAA 450 (erhoeht), DDD 200 (unveraendert)
    Duquesne:   Q1: AAA 100 | Q2: AAA 60 (reduziert -40 %), EEE verkauft (war in Q1)
    """
    berkshire = {
        "2025-Q1": [("111111111", "AAA Corp", 1000.0, 100.0), ("222222222", "BBB Inc", 500.0, 50.0)],
        "2025-Q2": [("111111111", "AAA Corp", 1500.0, 150.0), ("333333333", "CCC NV", 800.0, 80.0)],
    }
    pershing = {
        "2025-Q1": [("111111111", "AAA Corp", 300.0, 30.0), ("444444444", "DDD SE", 200.0, 20.0)],
        "2025-Q2": [("111111111", "AAA Corp", 450.0, 45.0), ("444444444", "DDD SE", 200.0, 20.0)],
    }
    duquesne = {
        "2025-Q1": [("111111111", "AAA Corp", 100.0, 10.0), ("555555555", "EEE AG", 50.0, 5.0)],
        "2025-Q2": [("111111111", "AAA Corp", 60.0, 6.0)],
    }
    fonds = {"0001067983": ("Berkshire", berkshire), "0001336528": ("Pershing Square", pershing),
             "0001536411": ("Duquesne", duquesne)}

    def fetcher(url: str) -> bytes:
        if "browse-edgar" in url:
            for cik, (name, qdata) in fonds.items():
                if f"CIK={int(cik)}" in url or f"CIK={cik}" in url:
                    entries = [(f"{int(cik)}0000000{i}", f"2025-0{5 + i}-15", "13F-HR")
                               for i in range(len(qdata))]
                    return _atom(entries)
            return _atom([])
        if url.endswith("index.json"):
            return b'{"directory": {"item": [{"name": "infotable.xml"}, {"name": "primary_doc.xml"}]}}'
        if url.endswith("primary_doc.xml"):
            # Quartal aus der URL: .../data/<cik>/<acc>/primary_doc.xml; acc enthaelt Index
            acc = url.split("/")[-2]
            idx = int(acc[-1]) if acc[-1].isdigit() else 0
            # Quartal: Q1 -> 2025-03-31, Q2 -> 2025-06-30 (idx 0/1)
            rd = ["2025-03-31", "2025-06-30"][min(idx, 1)]
            return (f'<?xml version="1.0"?><doc><periodOfReport>{rd}</periodOfReport></doc>').encode()
        if url.endswith("infotable.xml"):
            # Fonds + Quartal aus der URL rekonstruieren: .../data/<cik_n>/<acc>/infotable.xml
            parts = url.split("/")
            cik_n = parts[-3]
            acc = parts[-2]
            cik = str(int(cik_n)).zfill(10)
            name, qdata = fonds[cik]
            qs = sorted(qdata)
            idx = int(acc[-1]) if acc[-1].isdigit() else 0
            q = qs[min(idx, len(qs) - 1)]
            return _infotable(qdata[q])
        if "company_tickers" in url:
            return (b'{"0": {"cik_str": 1067983, "ticker": "BRK-B"}, '
                    b'"1": {"cik_str": 1, "ticker": "AAA"}}')
        if "openfigi" in url:
            import json as _json
            return _json.dumps([{"ticker": {"111111111": "AAA", "222222222": "BBB",
                                            "333333333": "CCC", "444444444": "DDD",
                                            "555555555": "EEE"}.get(c, "")}
                                for c in ["111111111"]]).encode()
        raise ValueError(f"unbekannte URL im Fixture-Fetcher: {url}")
    return fetcher


@pytest.fixture
def sm_db(tmp_path):
    """DB mit 2 Quartalen x 3 Fonds via Fixture-Fetcher gefuellt."""
    cfg = _cfg()
    db = tmp_path / "sm.db"
    errors = smart_money.fetch_all_fonds(cfg, quarters=2, db_path=db, fetcher=_make_fetcher())
    assert errors == []
    return cfg, db


# --- (1) Aktionen je (Fonds, Ticker, Quartal) -------------------------------

def test_aktionen_und_felder(sm_db):
    cfg, db = sm_db
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {(r["fonds"], r["ticker"], r["quartal"]): dict(r) for r in con.execute(
        "SELECT * FROM smart_money_positions")}
    # Berkshire Q2: AAA erhoeht (+50 %), CCC neu, BBB verkauft
    assert rows[("Berkshire", "AAA", "2025-Q2")]["aktion"] == "erhoeht"
    assert rows[("Berkshire", "AAA", "2025-Q2")]["aenderung_pct"] == pytest.approx(0.50)
    assert rows[("Berkshire", "CCC", "2025-Q2")]["aktion"] == "neu"
    assert rows[("Berkshire", "BBB", "2025-Q2")]["aktion"] == "verkauft"
    assert rows[("Berkshire", "BBB", "2025-Q2")]["aenderung_pct"] == pytest.approx(-1.0)
    # Pershing Q2: AAA erhoeht, DDD unveraendert
    assert rows[("Pershing Square", "AAA", "2025-Q2")]["aktion"] == "erhoeht"
    assert rows[("Pershing Square", "DDD", "2025-Q2")]["aktion"] == "unveraendert"
    # Duquesne Q2: AAA reduziert (-40 %), EEE verkauft
    assert rows[("Duquesne", "AAA", "2025-Q2")]["aktion"] == "reduziert"
    assert rows[("Duquesne", "AAA", "2025-Q2")]["aenderung_pct"] == pytest.approx(-0.40)
    assert rows[("Duquesne", "EEE", "2025-Q2")]["aktion"] == "verkauft"
    # Felder: shares, wert, anteil_portfolio, filing_datum
    r = rows[("Berkshire", "AAA", "2025-Q2")]
    assert r["shares"] == pytest.approx(1500.0)
    assert r["wert"] == pytest.approx(150_000.0)          # 150 (1000 USD) * 1000
    assert r["anteil_portfolio"] == pytest.approx(150_000.0 / 230_000.0)
    assert r["filing_datum"]  # gesetzt


# --- (3) Scores --------------------------------------------------------------

def test_score_1_kein_fonds(sm_db):
    cfg, db = sm_db
    s = smart_money.smart_money_score("ZZZ", cfg, db_path=db)
    assert s["score"] == 1
    assert s["fonds_kaeufer"] == [] and s["fonds_verkaeufer"] == []


def test_score_3_zwei_fonds_erhoeht(sm_db):
    cfg, db = sm_db
    # AAA: Berkshire erhoeht + Pershing erhoeht im selben Quartal -> 2 Fonds -> Score 3
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    # Berkshire + Pershing erhoeht -> Basis-Score 3; Duquesne reduziert -40 % (>= 30 %)
    # -> -1 -> 2
    assert set(s["fonds_kaeufer"]) == {"Berkshire", "Pershing Square"}
    assert s["fonds_verkaeufer"] == ["Duquesne"]
    assert s["score"] == 2  # 3 - 1 (Duquesne reduziert 40 %)


def test_score_anteil_ueber_5_prozent_gibt_3(sm_db):
    cfg, db = sm_db
    # CCC: nur Berkshire (neu) in Q2, aber Anteil 80k/230k = 34,8 % >= 5 % -> Score 3
    s = smart_money.smart_money_score("CCC", cfg, db_path=db)
    assert s["fonds_kaeufer"] == ["Berkshire"]
    assert s["score"] == 3


def test_score_4_rationale_und_zwei_quartale(sm_db):
    cfg, db = sm_db
    con = sqlite3.connect(db)
    con.execute("INSERT INTO smart_money_rationale (ticker, text, quelle_url, datum, typ) "
                "VALUES ('AAA', 'Buffett erhoeht weiter', 'https://example.com/brief', '2025-11-15', 'Brief')")
    con.commit()
    # AAA: Berkshire + Pershing in Q2 erhoeht; in Q1 waren beide neu (keine Vorquartal-Daten
    # -> 'neu') -> 2 Fonds ueber 2 Quartale + Rationale -> Score 4, dann -1 Duquesne -> 3
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["rationale_vorhanden"] is True
    assert s["score"] == 3  # 4 (2 Fonds ueber 2 Quartale + Rationale) - 1 (Duquesne)
    # Ohne Verkaeufer waere es 4: DDD testen (Pershing unveraendert -> kein Kauefer)
    con.execute("INSERT INTO smart_money_rationale (ticker, text, quelle_url, datum, typ) "
                "VALUES ('CCC', 'Pershing neu', 'https://example.com/x', '2025-11-15', 'Brief')")
    con.commit()
    # CCC hat nur Berkshire -> Score bleibt 3 (Anteil >= 5 %), rationale vorhanden aber
    # nur 1 Fonds -> kein 4
    s2 = smart_money.smart_money_score("CCC", cfg, db_path=db)
    assert s2["score"] == 3


def test_score_2_ein_fonds_kleiner_anteil(sm_db):
    cfg, db = sm_db
    # Fixture: EEE nur verkauft; nimm DDD: Pershing unveraendert -> kein Kauefer -> Score 1
    s = smart_money.smart_money_score("DDD", cfg, db_path=db)
    assert s["score"] == 1
    # Score 2 explizit ueber direktes Einspielen: ein Fonds neu mit Anteil < 2 %
    con = sqlite3.connect(db)
    con.execute("DELETE FROM smart_money_positions WHERE ticker='FFF'")
    con.execute("""INSERT INTO smart_money_positions
                   (fonds, ticker, quartal, aktion, shares, wert, anteil_portfolio, aenderung_pct, filing_datum, geladen_am)
                   VALUES ('Berkshire', 'FFF', '2025-Q2', 'neu', 10.0, 1000.0, 0.001, NULL, '2025-08-15', 'x')""")
    con.commit()
    s2 = smart_money.smart_money_score("FFF", cfg, db_path=db)
    assert s2["score"] == 2
    assert s2["fonds_kaeufer"] == ["Berkshire"]


def test_verkauf_minimum_1(sm_db):
    cfg, db = sm_db
    # BBB: Berkshire verkauft -> Verkaeufer; Basis-Score 1 (kein Kauefer) - 1 -> min 1
    s = smart_money.smart_money_score("BBB", cfg, db_path=db)
    assert s["fonds_verkaeufer"] == ["Berkshire"]
    assert s["score"] == 1  # Minimum


# --- (4) Ueberlappung mit Watchlist-Tickern ----------------------------------

def test_attach_smart_money_ueberlappung(sm_db, tmp_path):
    cfg, db = sm_db
    rows = [
        {"ticker": "AAA", "kurs": 100.0, "status_screener": "Kandidat", "status_live": "Kandidat",
         "mos_live_min": 0.4, "mos_live_robust": 0.35, "kaufzone_kurs": 119.0, "distanz_pct": 0.19,
         "timing_status": "", "mom_12_1": float("nan"), "abstand_sma200": float("nan"),
         "revision_90d": float("nan"), "wert_veraltet": 0, "alarm": 0, "naechster_earnings": "",
         "smart_money_score": "n/a", "fonds_kaeufer": "", "fonds_verkaeufer": "",
         "letzte_aktion": "", "filing_datum": "", "rationale_vorhanden": ""},
        {"ticker": "ZZZ", "kurs": 10.0, "status_screener": "nein", "status_live": "nein",
         "mos_live_min": float("nan"), "mos_live_robust": float("nan"), "kaufzone_kurs": float("nan"),
         "distanz_pct": float("nan"), "timing_status": "", "mom_12_1": float("nan"),
         "abstand_sma200": float("nan"), "revision_90d": float("nan"), "wert_veraltet": 0,
         "alarm": 0, "naechster_earnings": "", "smart_money_score": "n/a", "fonds_kaeufer": "",
         "fonds_verkaeufer": "", "letzte_aktion": "", "filing_datum": "", "rationale_vorhanden": ""},
    ]
    live_update.attach_smart_money(rows, cfg, db_path=db)
    assert rows[0]["smart_money_score"] == 2          # AAA: Basis 3 - 1 (Duquesne reduziert 40 %)
    assert rows[0]["fonds_kaeufer"] == "Berkshire,Pershing Square"
    assert rows[0]["fonds_verkaeufer"] == "Duquesne"
    assert rows[0]["letzte_aktion"]  # nicht leer
    assert rows[0]["rationale_vorhanden"] == "nein"
    assert rows[1]["smart_money_score"] == 1          # ZZZ: kein Fonds
    # ohne DB -> bleibt n/a (frische Row, nicht die bereits befuellte Kopie)
    fresh = {"ticker": "ZZZ", "smart_money_score": "n/a", "fonds_kaeufer": "",
             "fonds_verkaeufer": "", "letzte_aktion": "", "filing_datum": "",
             "rationale_vorhanden": ""}
    live_update.attach_smart_money([fresh], cfg, db_path=tmp_path / "fehlt.db")
    assert fresh["smart_money_score"] == "n/a"


def test_watchlist_csv_mit_smart_money_spalten(sm_db, tmp_path):
    cfg, db = sm_db
    out = tmp_path / "watchlist_live.csv"
    rows = [{"ticker": "AAA", "kurs": 100.0, "status_screener": "Kandidat", "status_live": "Kandidat",
             "mos_live_min": 0.4, "mos_live_robust": 0.35, "kaufzone_kurs": 119.0, "distanz_pct": 0.19,
             "timing_status": "positiv", "mom_12_1": 0.2, "abstand_sma200": 0.1, "revision_90d": 0.01,
             "wert_veraltet": 0, "alarm": 0, "naechster_earnings": "2026-02-05",
             "smart_money_score": "n/a", "fonds_kaeufer": "", "fonds_verkaeufer": "",
             "letzte_aktion": "", "filing_datum": "", "rationale_vorhanden": ""}]
    live_update.attach_smart_money(rows, cfg, db_path=db)
    live_update.write_watchlist_csv(rows, str(out))
    import csv as _csv
    with open(out, newline="", encoding="utf-8") as f:
        r = list(_csv.DictReader(f))
    assert r[0]["smart_money_score"] == "2"
    assert r[0]["fonds_kaeufer"] == "Berkshire,Pershing Square"
    assert "smart_money_score" in r[0] and "rationale_vorhanden" in r[0]


# --- (2) Rationale-Tabelle ---------------------------------------------------

def test_rationale_import_und_quelle(sm_db, tmp_path):
    cfg, db = sm_db
    csv_file = tmp_path / "rationale.csv"
    csv_file.write_text(
        "ticker,text,quelle_url,datum,typ\n"
        "AAA,Buffett erhoeht,https://example.com/brief,2025-11-15,Brief\n",
        encoding="utf-8")
    con = smart_money.connect(db)
    import argparse
    smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    con2 = sqlite3.connect(db)
    con2.row_factory = sqlite3.Row
    r = con2.execute("SELECT * FROM smart_money_rationale WHERE ticker='AAA'").fetchone()
    assert r["text"] == "Buffett erhoeht"
    assert r["quelle_url"] == "https://example.com/brief"
    assert r["typ"] == "Brief"
    assert smart_money.rationale_exists(smart_money.connect(db), "AAA") is True
    # Score AAA steigt damit auf 4 - 1 = 3
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["score"] == 3 and s["rationale_vorhanden"] is True


def test_compare_quarters_einheiten():
    prev = {"C1": {"cusip": "C1", "shares": 100.0, "value": 1.0, "name": "X"}}
    cur = {"C1": {"cusip": "C1", "shares": 150.0, "value": 1.5, "name": "X"},
           "C2": {"cusip": "C2", "shares": 10.0, "value": 0.1, "name": "Y"}}
    rows = {r["cusip"]: r for r in smart_money.compare_quarters(prev, cur)}
    assert rows["C1"]["aktion"] == "erhoeht" and rows["C1"]["aenderung_pct"] == pytest.approx(0.5)
    assert rows["C2"]["aktion"] == "neu"
    rows2 = {r["cusip"]: r for r in smart_money.compare_quarters(cur, prev)}
    assert rows2["C2"]["aktion"] == "verkauft"
    assert rows2["C1"]["aktion"] == "reduziert" and rows2["C1"]["aenderung_pct"] == pytest.approx(1/1.5 - 1)


def test_schema_enthaelt_smart_money_tabellen(tmp_path):
    con = smart_money.connect(tmp_path / "s.db")
    namen = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"smart_money_positions", "smart_money_rationale"} <= namen
    con.close()
