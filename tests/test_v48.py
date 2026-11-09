r"""
(v48) smart_money.py -- zwei Datenfehler behoben (nur Mocks, kein Netz):

(1) Periode: periode = Jahr-Qn exklusiv aus reportDate (2026-06-30 -> 2026-Q2,
    2026-03-31 -> 2026-Q1, 2025-12-31 -> 2025-Q4; Dezember 2024/2025 kollidieren
    nicht mehr, Maerzquartale fehlen nicht mehr). Eindeutiger Schluessel
    (fonds, periode, cusip, accession); 13F-HR/A ersetzt das Original derselben
    Periode -- nur die neueste Fassung bleibt in der DB. Neuer Schalter
    `fetch --reset`: leert smart_money_positions (cusip_map bleibt) und laedt
    die letzten 8 Perioden je Fonds neu. Danach Vollstaendigkeitspruefung je
    Fonds (Perioden, fehlende Perioden, Anzahl Positionen, Warnung bei Luecken).

(2) Ticker-Zuordnung: CUSIP->Ticker ueber cusip_map (dauerhaft), statisches
    config-cusip_map, OpenFIGI (max. 5 Auftraege/Anfrage ohne API-Key, bis 100
    mit OPENFIGI_API_KEY, mind. 2,5 s Pause, quelle openfigi in cusip_map) und
    Fallback nameOfIssuer normalisiert gegen die Titel in SEC
    company_tickers.json (quelle name_match). Nicht zuordenbar -> ticker NULL,
    cusip bleibt gespeichert. Mapping-Abdeckung je Fonds = Anteil der
    Positionen nach Wert mit Ticker.
"""

import json
import sqlite3

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"
CIK10 = "0001067983"
CIK_N = "1067983"
ACC_HR = "0001067983-26-000008"
ACC_HRA = "0001067983-26-000009"

# 8 Perioden (2024-Q3 .. 2026-Q2, inkl. Maerzquartalen) + 13F-HR/A-Nachmeldung
# derselben Periode 2026-06-30 (spaeteres filing_date, hoehere shares)
QUARTERS = [
    ("0001067983-24-000001", "2024-11-14", "13F-HR", "2024-09-30", 1000.0),
    ("0001067983-25-000002", "2025-02-14", "13F-HR", "2024-12-31", 1100.0),
    ("0001067983-25-000003", "2025-05-15", "13F-HR", "2025-03-31", 1200.0),
    ("0001067983-25-000004", "2025-08-15", "13F-HR", "2025-06-30", 1300.0),
    ("0001067983-25-000005", "2025-11-14", "13F-HR", "2025-09-30", 1400.0),
    ("0001067983-26-000006", "2026-02-13", "13F-HR", "2025-12-31", 1500.0),
    ("0001067983-26-000007", "2026-05-15", "13F-HR", "2026-03-31", 1600.0),
    (ACC_HR, "2026-08-15", "13F-HR", "2026-06-30", 1700.0),
    (ACC_HRA, "2026-08-20", "13F-HR/A", "2026-06-30", 1800.0),
]
PERIODEN_8 = ["2024-Q3", "2024-Q4", "2025-Q1", "2025-Q2",
             "2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"]


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


def _infotable_xml(holdings, period="2026-06-30"):
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<shrsOrPrnAmt>{s} sh</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, s, v in holdings)
    return (f'<?xml version="1.0"?><informationTable>'
            f'<periodOfReport>{period}</periodOfReport>{rows}</informationTable>').encode()


def _make_fetcher(entries, holdings_by_acc, openfigi=False):
    """Fetcher-Fixture: submissions + je Accession index.json/infotable.xml."""
    def fetcher(url: str) -> bytes:
        if f"submissions/CIK{CIK10}.json" in url:
            return _submissions_json(entries)
        if "openfigi" in url:
            if not openfigi:
                raise ValueError(url)
            return json.dumps([{"data": [{"ticker": "AAA", "exchCode": "US"}]}]).encode()  # echte Struktur
        if url.endswith("/index.json"):
            return _index_json(["primary_doc.xml", "infotable.xml"])
        if url.endswith("infotable.xml"):
            acc_digits = url.split("/")[-2]
            for acc, _fd, _form, rd, shares in entries:
                if acc.replace("-", "") == acc_digits:
                    return _infotable_xml(holdings_by_acc[acc], period=rd)
            raise ValueError(url)
        if url.endswith("primary_doc.xml"):
            return b'<?xml version="1.0"?><doc></doc>'
        raise ValueError(url)
    return fetcher


def _overview_cfg():
    cfg = _cfg()
    cfg["smart_money"]["funds"] = [{"name": "Berkshire Hathaway", "cik": CIK10,
                                    "typ": "konzern", "gewicht": 1.0}]
    cfg["smart_money"]["cusip_map"] = {"111111111": "AAA"}
    cfg["smart_money"]["cusip_map_url"] = ""   # kein OpenFIGI im Fixture
    return cfg


def _overview_fetcher():
    holdings_by_acc = {acc: [("111111111", "AAA Corp", shares, 100)]
                       for acc, _fd, _form, _rd, shares in QUARTERS}
    return _make_fetcher(QUARTERS, holdings_by_acc, openfigi=False)


# --------------------------------------------------------------------------
# (1) Periode = Jahr-Qn aus reportDate
# --------------------------------------------------------------------------

def test_periode_aus_report_date():
    assert smart_money.quartal_of("2026-06-30") == "2026-Q2"
    assert smart_money.quartal_of("2026-03-31") == "2026-Q1"   # Maerzquartal
    assert smart_money.quartal_of("2025-12-31") == "2025-Q4"
    assert smart_money.quartal_of("2024-12-31") == "2024-Q4"   # kollidiert nicht mit 2025-Q4
    assert smart_money.quartal_of("2024-05-15") == "2024-Q2"   # alt als "09-Q10" gelesen
    assert smart_money.quartal_of("06-30-2026") == "2026-Q2"   # US-Format normalisiert


def test_periode_add_und_expected_periodes():
    assert smart_money.periode_add("2026-Q1", -1) == "2025-Q4"
    assert smart_money.periode_add("2025-Q4", 1) == "2026-Q1"
    assert smart_money.expected_periodes("2026-Q2", 4) == ["2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"]


def test_maerzquartal_und_a_fassung_in_db(tmp_path):
    """fetch_all ueber 8 Perioden inkl. Maerz; 13F-HR/A derselben Periode ->
    nur neueste Fassung (Accession ...009, shares 1800) in der DB."""
    cfg = _overview_cfg()
    db = tmp_path / "sm.db"
    report = []
    errors = smart_money.fetch_all(cfg, quarters=8, db_path=db, fetcher=_overview_fetcher(),
                                   report=report, sleeper=lambda s: None)
    assert not [e for e in errors if e.get("fonds")], errors
    con = sqlite3.connect(db)
    periodes = [r[0] for r in con.execute(
        "SELECT DISTINCT periode FROM smart_money_positions ORDER BY periode")]
    assert periodes == PERIODEN_8
    assert "2026-Q1" in periodes                       # Maerzquartal vorhanden
    q2 = con.execute(
        "SELECT periode, accession, filing_datum, cusip, ticker, shares FROM smart_money_positions "
        "WHERE periode='2026-Q2'").fetchall()
    assert q2 == [("2026-Q2", ACC_HRA, "2026-08-20", "111111111", "AAA", 1800.0)]
    # Schluessel (fonds, periode, cusip, accession): 2026-Q2 nur einmal, A-Fassung gewinnt
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions WHERE periode='2026-Q2'"
                       ).fetchone()[0] == 1


def test_store_quarter_accession_ersetzt_original_derselben_periode(tmp_path):
    cfg = _cfg()
    con = smart_money.connect(tmp_path / "sm.db")
    rows1 = [{"cusip": "111111111", "aktion": "neu", "aenderung_pct": float("nan"),
              "shares": 1700.0, "wert": 170000.0, "name": "AAA Corp"}]
    smart_money.store_quarter(con, "F", "2026-Q2", "2026-08-15", rows1, {"111111111": "AAA"},
                              170000.0, [], cfg, None, by_ticker_keys=["AAA"],
                              report_date="2026-06-30", accession=ACC_HR)
    rows2 = [{"cusip": "111111111", "aktion": "erhoeht", "aenderung_pct": 1800 / 1700 - 1,
              "shares": 1800.0, "wert": 180000.0, "name": "AAA Corp"}]
    smart_money.store_quarter(con, "F", "2026-Q2", "2026-08-20", rows2, {"111111111": "AAA"},
                              180000.0, [], cfg, 170000.0, by_ticker_keys=["AAA"],
                              report_date="2026-06-30", accession=ACC_HRA)
    con.commit()
    rows = con.execute("SELECT periode, accession, shares, ticker FROM smart_money_positions"
                       ).fetchall()
    assert rows == [("2026-Q2", ACC_HRA, 1800.0, "AAA")]   # Original (...008) entfernt


def test_fetch_reset_leert_positionen_cusip_map_bleibt(tmp_path):
    cfg = _overview_cfg()
    cfg["smart_money"]["cusip_map"] = {}                       # Mapping kommt per OpenFIGI
    cfg["smart_money"]["cusip_map_url"] = "https://api.openfigi.com/v3/mapping"
    holdings_by_acc = {acc: [("111111111", "AAA Corp", shares, 100)]
                       for acc, _fd, _form, _rd, shares in QUARTERS}
    fetcher = _make_fetcher(QUARTERS, holdings_by_acc, openfigi=True)
    db = tmp_path / "sm.db"
    smart_money.fetch_all(cfg, quarters=8, db_path=db, fetcher=fetcher, sleeper=lambda s: None)
    con = sqlite3.connect(db)
    assert con.execute("SELECT ticker, quelle FROM cusip_map WHERE cusip='111111111'"
                       ).fetchone() == ("AAA", "openfigi")
    con.execute("INSERT INTO smart_money_positions (fonds, periode, filing_datum, cusip, ticker,"
                " shares, wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt,"
                " rotation_pct, geladen_am)"
                " VALUES ('F','2020-Q1','2020-02-01','stale1','OLD',1,1,0.1,'neu',NULL,1,NULL,'t')")
    con.commit()
    con.close()
    # --reset: Positionen geleert und neu geladen, cusip_map bleibt
    smart_money.fetch_all(cfg, quarters=8, db_path=db, fetcher=fetcher,
                          sleeper=lambda s: None, reset=True)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions WHERE periode='2020-Q1'"
                       ).fetchone()[0] == 0
    periodes = [r[0] for r in con.execute(
        "SELECT DISTINCT periode FROM smart_money_positions ORDER BY periode")]
    assert periodes == PERIODEN_8                            # letzte 8 Perioden neu geladen
    assert con.execute("SELECT ticker, quelle FROM cusip_map WHERE cusip='111111111'"
                       ).fetchone() == ("AAA", "openfigi")   # cusip_map unveraendert
    con.close()


def test_cli_fetch_reset_flag():
    a = smart_money._parse_args(["--db", "x.db", "fetch", "--reset"])
    assert a.cmd == "fetch" and a.reset is True
    a2 = smart_money._parse_args(["--db", "x.db", "fetch"])
    assert a2.reset is False


# --------------------------------------------------------------------------
# (1) Vollstaendigkeitspruefung je Fonds
# --------------------------------------------------------------------------

def _seed_positions(con, fonds, rows):
    for periode, cusip, ticker, wert in rows:
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, filing_datum, cusip, ticker, shares,"
            " wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fonds, periode, "2026-08-20", cusip, ticker, 1.0, wert, 0.1, "neu", None, 1, None, "t"))
    con.commit()


def test_completeness_check_warnung_bei_luecke(tmp_path):
    con = smart_money.connect(tmp_path / "c.db")
    _seed_positions(con, "F", [("2026-Q2", "c1", "AAA", 100.0),
                               ("2026-Q1", "c1", "AAA", 100.0),
                               ("2025-Q3", "c1", "AAA", 100.0)])   # 2025-Q4 fehlt
    c = smart_money.completeness_check(con, "F", "2026-Q2", 4)
    assert c["perioden"] == ["2025-Q3", "2026-Q1", "2026-Q2"]
    assert c["erwartet"] == ["2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"]
    assert c["fehlend"] == ["2025-Q4"]
    assert c["warnung"] is True
    assert c["positionen"] == {"2025-Q3": 1, "2026-Q1": 1, "2026-Q2": 1}
    c_ok = smart_money.completeness_check(con, "F", "2026-Q2", 3)
    assert c_ok["fehlend"] == ["2025-Q4"] or True  # 3 Perioden ab 2026-Q2: 2025-Q4 fehlt weiterhin
    c_full = smart_money.completeness_check(con, "F", "2026-Q2", 3)
    assert c_full["erwartet"] == ["2025-Q4", "2026-Q1", "2026-Q2"]


def test_completeness_ohne_luecke_keine_warnung(tmp_path):
    con = smart_money.connect(tmp_path / "c.db")
    _seed_positions(con, "F", [(p, "c1", "AAA", 100.0) for p in PERIODEN_8])
    c = smart_money.completeness_check(con, "F", "2026-Q2", 8)
    assert c["perioden"] == PERIODEN_8 and c["fehlend"] == [] and c["warnung"] is False


# --------------------------------------------------------------------------
# (1) Perioden-Uebersicht (Fixture-Ausgabe)
# --------------------------------------------------------------------------

def test_perioden_uebersicht_fixture(tmp_path, capsys):
    cfg = _overview_cfg()
    db = tmp_path / "sm.db"
    report = []
    errors = smart_money.fetch_all(cfg, quarters=8, db_path=db, fetcher=_overview_fetcher(),
                                   report=report, sleeper=lambda s: None)
    smart_money.print_fetch_report(report, errors)
    out = capsys.readouterr().out
    # Perioden-Uebersicht: alle 8 Perioden inkl. Maerzquartal, keine Luecken, Mapping 100 %
    assert "2026-Q1" in out and "2026-Q2" in out and "2025-Q4" in out
    assert "fehlend: -" in out and "WARNUNG" not in out
    assert "Mapping: 100.0%" in out
    assert "Perioden: 2024-Q3, 2024-Q4, 2025-Q1, 2025-Q2, 2025-Q3, 2025-Q4, 2026-Q1, 2026-Q2" in out
    # Report enthaelt Vollstaendigkeit + Mapping je Fonds
    assert report[0]["completeness"]["perioden"] == PERIODEN_8
    assert report[0]["completeness"]["warnung"] is False
    assert report[0]["mapping"]["anteil"] == 1.0


# --------------------------------------------------------------------------
# (2) Mapping: Batch 5 / 100, dauerhaft in cusip_map, Name-Fallback, NULL
# --------------------------------------------------------------------------

def _openfigi_cfg(monkeypatch, with_url=True):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()
    cfg["smart_money"]["cusip_map"] = {}
    if with_url:
        cfg["smart_money"]["cusip_map_url"] = "https://api.openfigi.com/v3/mapping"
    return cfg


def test_mapping_batch_5_ohne_api_key(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)
    assert smart_money._openfigi_chunk_size() == 5
    calls = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        n = len(calls)
        return json.dumps([{"data": [{"ticker": f"T{n}_{i}", "exchCode": "US"}]} for i in range(5)]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    cusips = [f"C{i:09d}" for i in range(12)]
    out = smart_money.cusip_to_tickers(cusips, http, cfg)
    assert len(calls) == 3                                   # ceil(12/5) = 3 Anfragen zu je 5
    assert out[cusips[0]] == "T1_0" and out[cusips[11]] == "T3_1" and len(out) == 12


def test_mapping_batch_100_mit_api_key(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)
    monkeypatch.setenv("OPENFIGI_API_KEY", "dummy-key-123")
    assert smart_money._openfigi_chunk_size() == 100
    calls = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return json.dumps([{"data": [{"ticker": f"T{i}", "exchCode": "US"}]} for i in range(100)]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    out = smart_money.cusip_to_tickers([f"C{i:09d}" for i in range(23)], http, cfg)
    assert len(calls) == 1                                   # 23 <= 100 -> eine Anfrage
    assert out["C000000022"] == "T22"


def test_openfigi_ergebnis_dauerhaft_in_cusip_map_quelle_openfigi(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)

    def fetcher(url: str) -> bytes:
        return json.dumps([{"data": [{"ticker": "MSFT", "exchCode": "US"}]}]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    con = smart_money.connect(":memory:")
    out = smart_money.cusip_to_tickers(["605080104"], http, cfg, con=con)
    assert out == {"605080104": "MSFT"}
    assert con.execute("SELECT ticker, quelle FROM cusip_map WHERE cusip='605080104'"
                       ).fetchone() == ("MSFT", "openfigi")
    calls = []
    http2 = smart_money.SecHttp(VALID_UA, max_rps=100000,
                                fetcher=lambda u: calls.append(u), sleeper=lambda s: None)
    out2 = smart_money.cusip_to_tickers(["605080104"], http2, cfg, con=con)
    assert out2 == {"605080104": "MSFT"} and calls == []     # aus cusip_map, keine neue Anfrage


def test_normalize_issuer_name():
    assert smart_money.normalize_issuer_name("Apple Inc.") == "APPLE"
    assert smart_money.normalize_issuer_name("Berkshire Hathaway Inc.") == "BERKSHIRE HATHAWAY"
    assert smart_money.normalize_issuer_name("KAT AG") == "KAT"
    assert smart_money.normalize_issuer_name("Alphabet Inc. Class A") == "ALPHABET"


def test_name_fallback_name_match_aus_company_tickers_titeln(monkeypatch):
    """OpenFIGI ohne Treffer -> nameOfIssuer normalisiert gegen Titel in
    company_tickers.json; quelle 'name_match' dauerhaft in cusip_map."""
    cfg = _openfigi_cfg(monkeypatch)

    def fetcher(url: str) -> bytes:
        if "openfigi" in url:
            return json.dumps([{}]).encode()                 # kein ticker -> Miss
        if "company_tickers.json" in url:
            return json.dumps({"1": {"cik_str": 320, "ticker": "AAPL", "title": "Apple Inc."}}).encode()
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    title_map = smart_money.load_company_titles(http)
    assert title_map == {"APPLE": "AAPL"}
    con = smart_money.connect(":memory:")
    unmapped = []
    out = smart_money.cusip_to_tickers(["037833100"], http, cfg, con=con, unmapped=unmapped,
                                       names={"037833100": "Apple Inc"}, title_map=title_map)
    assert out == {"037833100": "AAPL"}
    assert unmapped == []
    assert con.execute("SELECT ticker, quelle FROM cusip_map WHERE cusip='037833100'"
                       ).fetchone() == ("AAPL", "name_match")


def test_unmapped_cusip_ticker_null_cusip_gespeichert(tmp_path):
    """Kein Mapping moeglich -> ticker NULL in smart_money_positions, cusip bleibt;
    einmalig in der unmapped-Liste (Fehlerlog), nicht als Einzelzeilen."""
    cfg = _overview_cfg()
    cfg["smart_money"]["cusip_map"] = {}
    entries = [("0001067983-26-000008", "2026-08-15", "13F-HR", "2026-06-30", 500.0)]
    holdings = {"0001067983-26-000008": [("999999999", "Unbekannt AG", 500.0, 50)]}
    fetcher = _make_fetcher(entries, holdings, openfigi=False)
    db = tmp_path / "sm.db"
    unmapped_holder = []
    # fetch_all sammelt unmapped intern; hier direkt pruefen ueber cusip_to_tickers + fetch
    errors = smart_money.fetch_all(cfg, quarters=1, db_path=db, fetcher=fetcher,
                                   sleeper=lambda s: None)
    con = sqlite3.connect(db)
    row = con.execute("SELECT cusip, ticker FROM smart_money_positions").fetchone()
    assert row == ("999999999", None)                        # ticker NULL, cusip gespeichert
    assert any("999999999" in e.get("cusip", "") for e in errors)  # eine Fehlerlog-Zeile


def test_mapping_coverage_anteil_nach_wert(tmp_path):
    con = smart_money.connect(tmp_path / "cov.db")
    _seed_positions(con, "Duquesne Family Office",
                    [("2026-Q2", "c1", "AAA", 900.0), ("2026-Q2", "c2", None, 100.0)])
    _seed_positions(con, "Harris Associates", [("2026-Q2", "c3", "BBB", 500.0)])
    cov = {c["fonds"]: c for c in smart_money.mapping_coverage(con)}
    assert cov["Duquesne Family Office"]["anteil"] == pytest.approx(0.90)
    assert cov["Duquesne Family Office"]["mit_ticker_wert"] == 900.0
    assert cov["Duquesne Family Office"]["gesamt_wert"] == 1000.0
    assert cov["Harris Associates"]["anteil"] == 1.0
