"""v43 Tests (Fixtures aus kleinen infotable.xml -- kein Netz, kein Volllauf):

(a) Aktionen neu/erhoeht/reduziert/verkauft/unveraendert aus zwei Quartalen
(b) GOOG+GOOGL je Fonds zusammengefasst; Optionen (PUT/CALL) zaehlen nicht
(c) Score 1..4 inkl. Verkaufsabzug (Minimum 1)
(d) Gewicht 0,5 (Duquesne) zaehlt halb; Aktivist (Gewicht 0) nicht im Score,
    nur Katalysator-Spalte
(e) Urteil Bestaetigung/Divergenz/Warnung/neutral (overlap)
(f) 13F-HR/A ersetzt das Original (neueste filing_date gewinnt)
plus Regression: watchlist_live-Spalten via live_update.attach_smart_money.
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from config import load_config
import smart_money
import live_update

# CUSIPs der Fixture
CUSIP = {"AAA": "111111111", "BBB": "222222222", "CCC": "333333333",
         "GOOG": "38259P508", "GOOGL": "38259P607", "KAT": "999999999"}

CIK = {"Berkshire Hathaway": "0001067983", "Pershing Square Capital Management": "0001336528",
       "Duquesne Family Office": "0001536411", "Starboard Value": "0009999999"}


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    # Fixture: nur 4 Fonds, statisches CUSIP-Mapping, Rate-Limit aus (kein Netz)
    sm["funds"] = [
        {"name": "Berkshire Hathaway", "cik": CIK["Berkshire Hathaway"], "typ": "konzern",
         "gewicht": 1.0, "positions_soll_max": 50},
        {"name": "Pershing Square Capital Management", "cik": CIK["Pershing Square Capital Management"],
         "typ": "konzentriert", "gewicht": 1.0, "positions_soll_max": 20},
        {"name": "Duquesne Family Office", "cik": CIK["Duquesne Family Office"], "typ": "konzern",
         "gewicht": 0.5, "positions_soll_max": 40},
        {"name": "Starboard Value", "cik": "", "typ": "aktivist", "gewicht": 0.0,
         "positions_soll_max": 30},
    ]
    sm["cusip_map"] = {v: k for k, v in CUSIP.items()}
    sm["max_requests_per_second"] = 100000
    cfg["smart_money"] = sm
    return cfg


def _infotable(holdings, options=()) -> bytes:
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<shrsOrPrnAmt>{s} sh</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, s, v in holdings)
    opts = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<putCall>{pc}</putCall><shrsOrPrnAmt>{s} sh</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, pc, s, v in options)
    return f'<?xml version="1.0"?><informationTable>{rows}{opts}</informationTable>'.encode()


def _atom(entries) -> bytes:
    items = "".join(
        f'<entry><title>{t}</title><updated>{d}T00:00:00Z</updated>'
        f'<category term="{form} {rep}"/>'
        f'<link href="https://www.sec.gov/Archives/edgar/data/{cik_n}/{acc}/"/></entry>'
        for acc, d, form, rep, t, cik_n in entries)
    return ('<?xml version="1.0"?>'
            f'<feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>').encode()


# Fixture-Holdings je Fonds/Quartal. Q2 von Berkshire hat zusaetzlich eine
# 13F-HR/A-Nachmeldung (hoeheres filing_date, AAA 1800 statt 1500).
HOLD = {
    "1067983": {  # Berkshire
        0: [("111111111", "AAA Corp", 1000.0, 100.0), ("38259P508", "Alphabet", 100.0, 200.0),
            ("38259P607", "Alphabet", 100.0, 200.0)],
        1: [("111111111", "AAA Corp", 1500.0, 150.0), ("38259P508", "Alphabet", 100.0, 200.0),
            ("38259P607", "Alphabet", 200.0, 400.0)],   # A-Fassung: AAA 1800
    },
    "1067983_A": {1: [("111111111", "AAA Corp", 1800.0, 180.0), ("38259P508", "Alphabet", 100.0, 200.0),
                      ("38259P607", "Alphabet", 200.0, 400.0)]},
    "1336528": {  # Pershing Square
        0: [("111111111", "AAA Corp", 300.0, 30.0), ("222222222", "BBB Inc", 200.0, 20.0)],
        1: [("111111111", "AAA Corp", 450.0, 45.0), ("333333333", "CCC NV", 50.0, 5.0)],
    },
    "1536411": {  # Duquesne (gewicht 0,5)
        0: [("111111111", "AAA Corp", 100.0, 10.0), ("999999999", "KAT AG", 50.0, 5.0)],
        1: [("111111111", "AAA Corp", 60.0, 6.0)],
    },
    "9999999": {  # Starboard (aktivist, gewicht 0)
        0: [("999999999", "KAT AG", 500.0, 50.0)],
        1: [("999999999", "KAT AG", 700.0, 70.0)],
    },
}
OPTIONS = {"1067983": (1, [("111111111", "AAA Corp", "CALL", 100.0, 10.0)])}  # Q2 CALL auf AAA


def _make_fetcher():
    def fetcher(url: str) -> bytes:
        if "efts.sec.gov" in url:
            # EDGAR-Firmensuche: Starboard -> CIK 0009999999
            return json.dumps({"hits": {"hits": [
                {"_source": {"cik": 9999999, "title": "STARBOARD VALUE LP"}}]}}).encode()
        if "browse-edgar" in url and "type=SC+13D" in url:
            return _atom([("999999900000000001", "2025-07-01", "SC 13D", "", "SC 13D", 9999999)])
        if "browse-edgar" in url:
            for cik_n, data in (("1067983", None), ("1336528", None), ("1536411", None), ("9999999", None)):
                if f"CIK={cik_n}" in url:
                    entries = []
                    for idx in sorted(HOLD[cik_n]):
                        entries.append((f"{cik_n}000000000{idx}", f"2025-0{5 + idx}-15",
                                        "13F-HR", ["2025-03-31", "2025-06-30"][idx], "13F-HR", int(cik_n)))
                    if cik_n == "1067983":
                        # 13F-HR/A-Nachmeldung fuer Q2 (filing_date 2025-08-20 > 2025-08-15)
                        entries.append((f"{cik_n}000000001", "2025-08-20", "13F-HR/A", "2025-06-30", "13F-HR/A", int(cik_n)))
                    return _atom(entries)
            return _atom([])
        if url.endswith("index.json"):
            return json.dumps({"directory": {"item": [
                {"name": "infotable.xml"}, {"name": "primary_doc.xml"}]}}).encode()
        if url.endswith("primary_doc.xml"):
            idx = int(url.split("/")[-2][-1])
            rd = ["2025-03-31", "2025-06-30"][min(idx, 1)]
            return f'<?xml version="1.0"?><doc><periodOfReport>{rd}</periodOfReport></doc>'.encode()
        if url.endswith("infotable.xml"):
            cik_n = url.split("/")[-3]
            acc = url.split("/")[-2]
            idx = int(acc[-1])
            if cik_n == "1067983" and acc.startswith("1067983000000001"):
                return _infotable(HOLD["1067983_A"][1], options=OPTIONS["1067983"][1])
            opts = OPTIONS.get(cik_n, (None, ()))
            return _infotable(HOLD[cik_n][idx], options=opts[1] if opts[0] == idx else ())
        if "Archives/edgar/data/9999999" in url and url.endswith(".xml"):
            # 13D-Dokument: CUSIP des Ziels (KAT)
            return b'<?xml version="1.0"?><doc><cusip>999999999</cusip></doc>'
        raise ValueError(f"unbekannte URL im Fixture: {url}")
    return fetcher


@pytest.fixture
def sm_db(tmp_path):
    cfg = _cfg()
    db = tmp_path / "sm.db"
    report = []
    errors = smart_money.fetch_all(cfg, quarters=2, db_path=db, fetcher=_make_fetcher(), report=report)
    assert not [e for e in errors if e.get("fonds")], errors
    return cfg, db, report


# --- (f) 13F-HR/A ersetzt Original -------------------------------------------

def test_hr_a_ersetzt_original(sm_db):
    cfg, db, report = sm_db
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT shares, filing_datum FROM smart_money_positions "
        "WHERE fonds='Berkshire Hathaway' AND ticker='AAA' AND quartal='2025-Q2'").fetchone()
    assert r["shares"] == pytest.approx(1800.0)      # A-Fassung, nicht 1500
    assert r["filing_datum"] == "2025-08-20"          # spaeteres filing_date gewinnt


# --- (a) Aktionen aus zwei Quartalen ------------------------------------------

def test_aktionen_zwei_quartale(sm_db):
    cfg, db, _ = sm_db
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {(r["fonds"], r["ticker"], r["quartal"]): dict(r) for r in con.execute(
        "SELECT * FROM smart_money_positions")}
    b = "Berkshire Hathaway"
    assert rows[(b, "AAA", "2025-Q2")]["aktion"] == "erhoeht"
    assert rows[(b, "AAA", "2025-Q2")]["aenderung_pct"] == pytest.approx(1800 / 1000 - 1)
    p = "Pershing Square Capital Management"
    assert rows[(p, "AAA", "2025-Q2")]["aktion"] == "erhoeht"
    assert rows[(p, "CCC", "2025-Q2")]["aktion"] == "neu"
    assert rows[(p, "BBB", "2025-Q2")]["aktion"] == "verkauft"
    d = "Duquesne Family Office"
    assert rows[(d, "AAA", "2025-Q2")]["aktion"] == "reduziert"
    assert rows[(d, "AAA", "2025-Q2")]["aenderung_pct"] == pytest.approx(-0.40)
    assert rows[(d, "KAT", "2025-Q2")]["aktion"] == "verkauft"
    # Felder: positions_gesamt, rotation_pct, anteil_portfolio
    assert rows[(b, "AAA", "2025-Q2")]["positions_gesamt"] == 2  # AAA + GOOG (GOOGL gemerged)
    assert rows[(b, "AAA", "2025-Q2")]["rotation_pct"] is not None
    assert rows[(b, "AAA", "2025-Q2")]["anteil_portfolio"] == pytest.approx(180_000 / 780_000)


# --- (b) GOOG/GOOGL-Marge + Optionen nicht gezaehlt ---------------------------

def test_googl_zusammengefasst_optionen_getrennt(sm_db):
    cfg, db, _ = sm_db
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # GOOGL existiert nicht als eigene Position -> auf GOOG gemerged
    tickers = {r["ticker"] for r in con.execute(
        "SELECT DISTINCT ticker FROM smart_money_positions WHERE fonds='Berkshire Hathaway'")}
    assert "GOOGL" not in tickers and "GOOG" in tickers
    g = con.execute("SELECT * FROM smart_money_positions WHERE fonds='Berkshire Hathaway' "
                    "AND ticker='GOOG' AND quartal='2025-Q2'").fetchone()
    assert g["shares"] == pytest.approx(300.0)        # 100 GOOG + 200 GOOGL
    assert g["aktion"] == "erhoeht"                    # 200 -> 300
    # Optionen landen in smart_money_options, NICHT in positions
    opts = [dict(r) for r in con.execute(
        "SELECT * FROM smart_money_options WHERE fonds='Berkshire Hathaway'")]
    assert len(opts) == 1 and opts[0]["put_call"] == "CALL" and opts[0]["ticker"] == "AAA"
    pos_aaa = con.execute("SELECT COUNT(*) AS n FROM smart_money_positions "
                          "WHERE ticker='AAA' AND fonds='Berkshire Hathaway'").fetchone()["n"]
    assert pos_aaa == 2  # nur Q1 + Q2, Optionen zaehlen nicht als Position


# --- (c) Score 1..4 + Verkaufsabzug -------------------------------------------

def test_score_1_kein_fonds(sm_db):
    cfg, db, _ = sm_db
    assert smart_money.smart_money_score("ZZZ", cfg, db_path=db)["score"] == 1


def test_score_3_gewicht_summe_2(sm_db):
    cfg, db, _ = sm_db
    # AAA: Berkshire (1.0) + Pershing (1.0) erhoeht -> gew_summe 2.0 -> 3; Duquesne -40 % -> -1 -> 2
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["fonds_kaeufer"] == ["Berkshire Hathaway", "Pershing Square Capital Management"]
    assert s["fonds_verkaeufer"] == ["Duquesne Family Office"]
    assert s["score"] == 2
    assert s["daten_alter_tage"] is not None
    assert s["filing_datum"] == "2025-08-20"


def test_score_2_ein_fonds_kleiner_anteil(sm_db):
    cfg, db, _ = sm_db
    # CCC: nur Pershing neu, Anteil 5k/50k = 10 % ... anteil >= 5 % in gewicht-1.0-Fonds -> 3
    s = smart_money.smart_money_score("CCC", cfg, db_path=db)
    assert s["score"] == 3  # Anteil 10 % >= 5 % bei Gewicht 1,0
    # GOOG: nur Berkshire, Anteil 600k/780k = 76,9 % -> 3 (Anteil >= 5 %, Gewicht 1,0)
    s2 = smart_money.smart_money_score("GOOG", cfg, db_path=db)
    assert s2["score"] == 3


def test_score_4_zwei_quartale_plus_rationale(sm_db, tmp_path):
    cfg, db, _ = sm_db
    con = sqlite3.connect(db)
    con.execute("INSERT INTO smart_money_rationale (ticker, text, quelle_url, datum, typ) "
                "VALUES ('AAA', 'Buffett erhoeht', 'https://example.com/b', '2025-09-01', 'Brief')")
    con.commit()
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    # Q1: Berkshire+Pershing neu (gew 2.0 -> cond3), Q2 erhoeht (cond3) + Rationale -> 4, dann -1 Duquesne -> 3
    assert s["score"] == 3
    assert s["rationale_vorhanden"] is True


def test_verkauf_minimum_1(sm_db):
    cfg, db, _ = sm_db
    # BBB: Pershing verkauft -> -1, Basis 1 -> Minimum 1
    s = smart_money.smart_money_score("BBB", cfg, db_path=db)
    assert s["fonds_verkaeufer"] == ["Pershing Square Capital Management"]
    assert s["score"] == 1


# --- (d) Gewicht 0,5 + Aktivist nur Katalysator --------------------------------

def test_gewicht_halb_und_aktivist_nur_katalysator(sm_db):
    cfg, db, _ = sm_db
    # KAT: nur Starboard (gewicht 0) aktiv + Duquesne verkauft -> Score bleibt 1 (kein Fonds gewicht>0 kauft)
    s = smart_money.smart_money_score("KAT", cfg, db_path=db)
    assert s["score"] == 1
    assert s["fonds_kaeufer"] == []          # Aktivist zaehlt nicht als Kaeufer
    assert "Starboard Value" in s["katalysator_13d"]  # 13D-Katalysator sichtbar
    assert "Duquesne Family Office" in s["fonds_verkaeufer"]
    # Gewicht 0,5 allein reicht nicht fuer Score 3: einzelner Duquesne-Kauf, Anteil < 5 %
    con = sqlite3.connect(db)
    con.execute("""INSERT INTO smart_money_positions
                   (fonds, quartal, filing_datum, ticker, shares, wert, anteil_portfolio, aktion,
                    aenderung_pct, positions_gesamt, rotation_pct, geladen_am)
                   VALUES ('Duquesne Family Office', '2025-Q2', '2025-08-15', 'SOLO', 10.0, 1000.0,
                           0.001, 'neu', NULL, 1, NULL, 'x')""")
    con.commit()
    s2 = smart_money.smart_money_score("SOLO", cfg, db_path=db)
    assert s2["score"] == 2                  # gew_summe 0.5 < 2.0, Anteil 0.1 % < 2 % -> 2
    assert s2["fonds_kaeufer"] == ["Duquesne Family Office"]


# --- (e) Urteil ----------------------------------------------------------------

def test_urteil_overlap():
    sm_kauf = {"fonds_kaeufer": ["Berkshire Hathaway"], "fonds_verkaeufer": []}
    sm_verk = {"fonds_kaeufer": [], "fonds_verkaeufer": ["Berkshire Hathaway"]}
    sm_nix = {"fonds_kaeufer": [], "fonds_verkaeufer": []}
    assert smart_money.urteil({"status_all": "Kandidat", "mos_rule": "0.4"}, sm_kauf) == "Bestaetigung"
    assert smart_money.urteil({"status_all": "Beobachtung", "mos_rule": "0.35"}, sm_kauf) == "Bestaetigung"
    assert smart_money.urteil({"status_all": "nein", "mos_rule": "-0.2"}, sm_verk) == "Bestaetigung"
    assert smart_money.urteil({"status_all": "nein", "mos_rule": "-0.5"}, sm_kauf) == "Divergenz"
    assert smart_money.urteil({"status_all": "nein", "mos_rule": "0.1"}, sm_kauf) == "Divergenz"  # MoS < 0? nein, status nein -> Divergenz
    assert smart_money.urteil({"status_all": "Kandidat", "mos_rule": "0.4"}, sm_verk) == "Warnung"
    assert smart_money.urteil({"status_all": "nein", "mos_rule": "-0.5"}, sm_nix) == "neutral"
    assert smart_money.urteil({"status_all": "Kandidat", "mos_rule": "0.4"}, sm_nix) == "neutral"


# --- Overlap-CSV (Fixture) ------------------------------------------------------

def test_overlap_csv(sm_db, tmp_path):
    cfg, db, report = sm_db
    # ranking-CSV-Fixture
    ranking = tmp_path / "ranking.csv"
    ranking.write_text(
        "ticker,status_all,quality,mos_rule\n"
        "AAA,Kandidat,72,0.41\n"
        "BBB,nein,40,-0.60\n"
        "CCC,Beobachtung,65,0.20\n"
        "KAT,nein,30,-0.90\n", encoding="utf-8")
    watch = tmp_path / "watch.csv"
    watch.write_text("ticker\nAAA\nKAT\n", encoding="utf-8")
    out = tmp_path / "smart_money_overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db, out_path=str(out))
    by = {r["ticker"]: r for r in rows}
    assert set(by) >= {"AAA", "BBB", "CCC", "KAT"}   # Watchlist + aktive Fonds-Ticker
    assert by["AAA"]["smart_money_urteil"] == "Bestaetigung"   # Fonds kauft + Kandidat
    assert by["BBB"]["smart_money_urteil"] == "Bestaetigung"   # Fonds verkauft + nein
    assert by["CCC"]["smart_money_urteil"] == "Bestaetigung"   # Fonds kauft + Beobachtung
    assert by["KAT"]["smart_money_urteil"] == "Bestaetigung"   # Duquesne verkauft + nein
    assert by["AAA"]["smart_money_score"] == 2
    assert by["AAA"]["daten_alter_tage"] != ""
    assert by["KAT"]["katalysator_13d"] != ""
    # CSV geschrieben
    text = out.read_text(encoding="utf-8")
    assert "smart_money_urteil" in text and "Bestaetigung" in text


# --- CIK-Aufloesung + Filer-Status ----------------------------------------------

def test_cik_aufloesung(sm_db):
    cfg, db, report = sm_db
    by = {e["name"]: e for e in report}
    assert by["Berkshire Hathaway"]["filer_status"] == "bekannt"
    assert by["Starboard Value"]["cik"] == "0009999999"      # per EDGAR-Suche aufgeloest
    assert by["Starboard Value"]["filer_status"] == "13F-HR"


# --- Regression: watchlist_live-Spalten ----------------------------------------

def test_attach_smart_money_watchlist(sm_db, tmp_path):
    cfg, db, _ = sm_db
    rows = [{"ticker": "AAA", "smart_money_score": "n/a", "fonds_kaeufer": "",
             "fonds_verkaeufer": "", "letzte_aktion": "", "filing_datum": "",
             "rationale_vorhanden": ""}]
    live_update.attach_smart_money(rows, cfg, db_path=db)
    assert rows[0]["smart_money_score"] == 2
    assert rows[0]["fonds_kaeufer"] == "Berkshire Hathaway,Pershing Square Capital Management"
    fresh = {"ticker": "ZZZ", "smart_money_score": "n/a", "fonds_kaeufer": "",
             "fonds_verkaeufer": "", "letzte_aktion": "", "filing_datum": "",
             "rationale_vorhanden": ""}
    live_update.attach_smart_money([fresh], cfg, db_path=tmp_path / "fehlt.db")
    assert fresh["smart_money_score"] == "n/a"


# --- Rationale-Import: Pflicht quelle_url ---------------------------------------

def test_rationale_import_pflicht_quelle(sm_db, tmp_path, capsys):
    cfg, db, _ = sm_db
    csv_file = tmp_path / "rationale_import.csv"
    csv_file.write_text(
        "ticker,text,quelle_url,datum,typ\n"
        "AAA,Buffett erhoeht,https://example.com/b,2025-09-01,Brief\n"
        "BBB,ohne Quelle,,2025-09-01,Interview\n", encoding="utf-8")
    import argparse
    smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(csv_file)))
    out = capsys.readouterr().out + capsys.readouterr().err
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM smart_money_rationale").fetchone()[0]
    assert n == 1  # BBB ohne quelle_url uebersprungen
    r = con.execute("SELECT quelle_url, typ FROM smart_money_rationale WHERE ticker='AAA'").fetchone()
    assert r[0] == "https://example.com/b" and r[1] == "Brief"


def test_schema_v43_tabellen(tmp_path):
    con = smart_money.connect(tmp_path / "s.db")
    namen = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"smart_money_positions", "smart_money_rationale",
            "smart_money_options", "smart_money_katalysator"} <= namen
    cols = {r[1] for r in con.execute("PRAGMA table_info(smart_money_positions)")}
    assert {"positions_gesamt", "rotation_pct", "filing_datum"} <= cols
    con.close()
