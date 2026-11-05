r"""
(v47) Quartalslabel in Log + DB (smart_money_positions.quartal) -- gebildet in
smart_money.py, Funktion quartal_of(), aufgerufen in fetch_all().
Ursache der falschen Label ("06-Q10", "09-Q10", "12-Q11"): quartal_of nahm
YYYY-MM-DD an; bei MM-DD-YYYY ("06-30-2026") wurde der Tag als Monat gelesen
(y=06, m=30 -> Q10). Label wird exklusiv aus dem reportDate des Filings gebildet
(2026-06-30 -> 2026-Q2). 13F-HR + 13F-HR/A derselben Periode -> ein Label,
neueste Fassung ersetzt die aeltere. Perioden ohne Filing -> leer, keine Duplikate.
Migration: bestehende Zeilen werden aus der gespeicherten reportDate neu beschriftet;
fehlt die Spalte (alte DB), werden die Zeilen entfernt und neu geladen.
"""

import json
import sqlite3

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"
CIK10 = "0001067983"
CIK_N = "1067983"


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["max_requests_per_second"] = 100000
    sm["user_agent"] = VALID_UA
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


def _submissions_json(entries, name="BERKSHIRE HATHAWAY INC"):
    """entries: [(accession, filingDate, form, reportDate)]"""
    return json.dumps({
        "name": name,
        "filings": {"recent": {
            "accessionNumber": [e[0] for e in entries],
            "filingDate": [e[1] for e in entries],
            "form": [e[2] for e in entries],
            "reportDate": [e[3] for e in entries],
        }}}).encode()


def _index_json(names):
    return json.dumps({"directory": {"item": [{"name": n} for n in names]}}).encode()


def _infotable_xml(holdings=(("111111111", "AAA Corp", "1000 sh", "100"),)):
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<shrsOrPrnAmt>{s}</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, s, v in holdings)
    return (f'<?xml version="1.0"?><informationTable><periodOfReport>2026-06-30</periodOfReport>'
            f'{rows}</informationTable>').encode()


# --------------------------------------------------------------------------
# Label exklusiv aus reportDate
# --------------------------------------------------------------------------

def test_label_aus_report_date():
    assert smart_money.quartal_of("2026-06-30") == "2026-Q2"
    assert smart_money.quartal_of("2026-03-31") == "2026-Q1"
    assert smart_money.quartal_of("2025-12-31") == "2025-Q4"
    assert smart_money.quartal_of("") == ""


def test_label_us_format_normalisiert_statt_q10_q11():
    """Regression: '06-30-2026' ergab alt '06-Q10', '12-31-2025' -> '12-Q11'."""
    assert smart_money.quartal_of("06-30-2026") == "2026-Q2"
    assert smart_money.quartal_of("09-30-2026") == "2026-Q3"
    assert smart_money.quartal_of("12-31-2025") == "2025-Q4"


# --------------------------------------------------------------------------
# Original + Amendment derselben Periode -> eine Periode, neueste Fassung gewinnt
# --------------------------------------------------------------------------

def test_original_plus_amendment_eine_periode_neueste_fassung():
    entries = [
        ("0001193125-26-111111", "2026-05-15", "13F-HR", "2026-03-31"),
        ("0001193125-26-352199", "2026-08-15", "13F-HR", "2026-06-30"),
        ("0001193125-26-352200", "2026-08-20", "13F-HR/A", "2026-06-30"),
        ("0001193125-26-352201", "2026-08-21", "13F-HR/A", "06-30-2026"),  # gemischtes Format, gleiche Periode
    ]

    def fetcher(url: str) -> bytes:
        if f"submissions/CIK{CIK10}.json" in url:
            return _submissions_json(entries)
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings(CIK10, http)
    assert len(filings) == 2                                   # 2 Perioden, keine Duplikate
    q2 = [f for f in filings if smart_money.quartal_of(f["report_date"]) == "2026-Q2"]
    assert len(q2) == 1                                        # eine Periode trotz 3 Filings
    assert q2[0]["form"] == "13F-HR/A"                         # neueste Fassung gewinnt
    assert q2[0]["accession"] == "0001193125-26-352201"
    assert q2[0]["report_date"] == "06-30-2026"                # Rohwert, Label normalisiert
    assert smart_money.quartal_of(q2[0]["report_date"]) == "2026-Q2"


def test_fetch_schreibt_label_report_date_und_keine_duplikate(tmp_path):
    """fetch_all: 13F-HR + 13F-HR/A derselben Periode -> genau ein Quartal in der DB,
    report_date gespeichert, Label '2026-Q2' (nicht '06-Q10')."""
    cfg = _cfg()
    cfg["smart_money"]["funds"] = [{"name": "Berkshire Hathaway", "cik": CIK10,
                                    "typ": "konzern", "gewicht": 1.0, "positions_soll_max": 50}]
    cfg["smart_money"]["cusip_map"] = {"111111111": "AAA"}
    subs = _submissions_json([
        ("0001193125-26-352199", "2026-08-15", "13F-HR", "2026-06-30"),
        ("0001193125-26-352200", "2026-08-20", "13F-HR/A", "2026-06-30"),
    ])

    def fetcher(url: str) -> bytes:
        if f"submissions/CIK{CIK10}.json" in url:
            return subs
        if url.endswith("index.json"):
            return _index_json(["primary_doc.xml", "infotable.xml"])
        if url.endswith("infotable.xml"):
            return _infotable_xml()
        if url.endswith("primary_doc.xml"):
            return b'<?xml version="1.0"?><doc></doc>'
        raise ValueError(url)

    db = tmp_path / "sm.db"
    errors = smart_money.fetch_all(cfg, quarters=1, db_path=db,
                                   fetcher=fetcher, sleeper=lambda s: None)
    assert not [e for e in errors if e.get("fonds")], errors
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT periode, report_date, accession, filing_datum, cusip, ticker FROM smart_money_positions "
        "WHERE fonds='Berkshire Hathaway'").fetchall()
    # eine Periode, neueste Fassung (13F-HR/A, Accession ...352200), cusip + ticker getrennt
    assert rows == [("2026-Q2", "2026-06-30", "0001193125-26-352200", "2026-08-20", "111111111", "AAA")]


# --------------------------------------------------------------------------
# Migration: bestehende Zeilen aus gespeicherter reportDate neu beschriften
# --------------------------------------------------------------------------

def test_migration_relabelt_aus_report_date(tmp_path):
    db = tmp_path / "sm.db"
    con = smart_money.connect(db)  # legt Tabelle inkl. report_date an (v48: periode/cusip/accession)
    con.execute(
        "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum, ticker,"
        " shares, wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)"
        " VALUES ('Himalaya Capital Management','06-Q10','2026-06-30','2026-08-15','AAA',"
        " 1,1,0.1,'neu',NULL,1,NULL,'t')")
    con.commit()
    con.close()
    con = smart_money.connect(db)  # Migration beim connect
    r = con.execute("SELECT periode FROM smart_money_positions").fetchone()
    assert r[0] == "2026-Q2"      # neu beschriftet aus reportDate


def test_migration_alte_tabelle_ohne_report_date_spalte_wird_neu_geladen(tmp_path):
    """Alte DB ohne report_date-Spalte: Spalte wird per ALTER ergaenzt, Zeilen ohne
    reportDate sind nicht neu beschriftbar -> entfernt (naechster fetch laedt neu)."""
    db = tmp_path / "sm.db"
    raw = sqlite3.connect(db)
    raw.execute(
        """CREATE TABLE smart_money_positions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, fonds TEXT NOT NULL, quartal TEXT NOT NULL,
               filing_datum TEXT, ticker TEXT NOT NULL, shares REAL, wert REAL, anteil_portfolio REAL,
               aktion TEXT NOT NULL, aenderung_pct REAL, positions_gesamt INTEGER, rotation_pct REAL,
               geladen_am TEXT NOT NULL, UNIQUE (fonds, quartal, ticker))""")
    raw.execute(
        "INSERT INTO smart_money_positions (fonds, quartal, filing_datum, ticker, shares, wert,"
        " anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)"
        " VALUES ('Himalaya Capital Management','06-Q10','2026-08-15','AAA',1,1,0.1,'neu',NULL,1,NULL,'t')")
    raw.commit()
    raw.close()
    con = smart_money.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(smart_money_positions)")}
    assert "report_date" in cols                              # Spalte ergaenzt
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions").fetchone()[0] == 0  # neu laden
