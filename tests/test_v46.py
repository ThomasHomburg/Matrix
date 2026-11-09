r"""
(v46) smart_money.py -- Fixes + Diagnose (nur Mocks, kein Netz):
(1) Jeder HTTP-Fehler (auch 404) loggt URL + Fondsname + Accession. Die infotable-Datei
    wird nicht ueber einen festen Namen bestimmt, sondern aus <Filing-Verzeichnis>/index.json:
    alle .xml der obersten Ebene ausser primary_doc.xml (keine Unterordner wie
    xslForm13F_X02) lesen, Datei mit Wurzelelement informationTable waehlen.
(2) OpenFIGI: Chunks (5 ohne API-Key, 100 mit OPENFIGI_API_KEY), mind. 2,5 s Pause
    (max 25/Min), 429 -> Retry; Mapping dauerhaft in Tabelle cusip_map, wird nicht erneut
    abgefragt; nicht zuordenbare CUSIPs einmalig als Liste.
(3) Quartalslabel aus reportDate (2026-06-30 -> 2026-Q2); 13F-HR/A ersetzt das Original
    derselben Periode. Diagnose 2010/2011: Atom-Fallback ohne Datum im category term darf
    nicht die aeltesten Filings liefern (Perioden-Key = report_date || filing_date).
(4) 13D-Katalysatoren: HTTP 503 -> 3x Wiederholung (5, 15, 30 s); Fehlschlag blockiert nicht.
(5) show --fonds <Name>: Perioden + Anzahl Positionen je Quartal.
"""

import argparse
import json
import sqlite3
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"
CIK10 = "0001067983"
CIK_N = "1067983"
ACC = "0001193125-26-352200"
ACC_DIGITS = "000119312526352200"
BASE = f"https://www.sec.gov/Archives/edgar/data/{CIK_N}/{ACC_DIGITS}"


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


def _infotable_xml(with_period=True, holdings=(("111111111", "AAA Corp", "1000 sh", "100"),)):
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<shrsOrPrnAmt>{s}</shrsOrPrnAmt><value>{v}</value></infoTable>"
        for c, n, s, v in holdings)
    period = "<periodOfReport>2026-06-30</periodOfReport>" if with_period else ""
    return f'<?xml version="1.0"?><informationTable>{period}{rows}</informationTable>'.encode()


def _primary_doc_xml(period="2026-06-30"):
    inner = f"<periodOfReport>{period}</periodOfReport>" if period else ""
    return f'<?xml version="1.0"?><doc>{inner}</doc>'.encode()


# --------------------------------------------------------------------------
# (1) infotable-Datei wird aus index.json erkannt (nicht fester Name)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("info_name", ["infotable.xml", "form13fInfoTable.xml", "a1b2c3.xml"])
def test_infotable_datei_wird_erkannt(info_name):
    urls = []

    def fetcher(url: str) -> bytes:
        urls.append(url)
        if url.endswith("index.json"):
            return _index_json(["primary_doc.xml", "xslForm13F_X02", info_name, "index.json"])
        if url.endswith("primary_doc.xml"):
            return _primary_doc_xml("2026-06-30")
        if url.endswith(info_name):
            return _infotable_xml(with_period=False)
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    rd, holdings, options = smart_money.load_13f_quarter(CIK10, ACC, http)
    assert f"{BASE}/{info_name}" in urls            # erkannte Datei wurde gelesen
    assert not any("xslForm13F_X02" in u for u in urls)  # Unterordner nicht angefasst
    assert len(holdings) == 1 and holdings[0]["cusip"] == "111111111"
    assert rd == "2026-06-30"                        # report_date aus primary_doc.xml


def test_infotable_period_of_report_in_der_tabelle_selbst():
    """periodOfReport steht im informationTable selbst -> report_date daraus."""
    def fetcher(url: str) -> bytes:
        if url.endswith("index.json"):
            return _index_json(["primary_doc.xml", "infotable.xml"])
        if url.endswith("primary_doc.xml"):
            return _primary_doc_xml(period="")
        if url.endswith("infotable.xml"):
            return _infotable_xml(with_period=True)
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    rd, holdings, _ = smart_money.load_13f_quarter(CIK10, ACC, http)
    assert rd == "2026-06-30"
    assert len(holdings) == 1


def test_keine_informationTable_wirft_fehler():
    def fetcher(url: str) -> bytes:
        if url.endswith("index.json"):
            return _index_json(["primary_doc.xml", "foo.xml"])
        if url.endswith(".xml"):
            return b'<?xml version="1.0"?><doc><x/></doc>'
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    with pytest.raises(ValueError, match="keine informationTable"):
        smart_money.load_13f_quarter(CIK10, ACC, http)


def test_http_fehler_loggt_url_fonds_accession_auch_404(monkeypatch):
    errors = []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, retry_tries=1,
                               errors=errors, sleeper=lambda s: None)
    http.fonds = "Berkshire Hathaway"
    http.accession = ACC

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get(f"{BASE}/index.json")
    assert errors[0]["http_code"] == 404
    assert errors[0]["url"] == f"{BASE}/index.json"
    assert errors[0]["fonds"] == "Berkshire Hathaway"
    assert errors[0]["accession"] == ACC


# --------------------------------------------------------------------------
# (2) OpenFIGI: Chunks, Pacing, 429-Retry, cusip_map-Persistenz, unmapped-Liste
# --------------------------------------------------------------------------

def _openfigi_cfg(monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()
    cfg["smart_money"]["cusip_map"] = {}
    cfg["smart_money"]["cusip_map_url"] = "https://api.openfigi.com/v3/mapping"
    return cfg


def test_openfigi_23_cusips_5_anfragen_ohne_key_und_dauerhaft(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)
    calls = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        n = len(calls)
        # echte OpenFIGI-Struktur {"data": [{... US-Eintrag ...}]} je Eingabe
        return json.dumps([{"data": [{"ticker": f"T{n}_{i}", "exchCode": "US"}]} for i in range(5)]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    con = smart_money.connect(":memory:")
    cusips = [f"C{i:09d}" for i in range(23)]
    out = smart_money.cusip_to_tickers(cusips, http, cfg, con=con)
    assert len([u for u in calls if "openfigi" in u]) == 5   # ceil(23/5) = 5 Anfragen
    assert out[cusips[0]] == "T1_0"
    assert con.execute("SELECT COUNT(*) FROM cusip_map").fetchone()[0] == 23

    calls.clear()
    out2 = smart_money.cusip_to_tickers(cusips, http, cfg, con=con)
    assert [u for u in calls if "openfigi" in u] == []       # aus cusip_map, keine neue Anfrage
    assert out2 == out


def test_openfigi_mit_api_key_bis_100_pro_anfrage(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)
    monkeypatch.setenv("OPENFIGI_API_KEY", "dummy-key-123")
    calls = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return json.dumps([{"data": [{"ticker": f"T{i}", "exchCode": "US"}]} for i in range(100)]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    cusips = [f"C{i:09d}" for i in range(23)]
    out = smart_money.cusip_to_tickers(cusips, http, cfg)
    assert len(calls) == 1                                    # 23 <= 100 -> eine Anfrage
    assert out[cusips[22]] == "T22"


def test_openfigi_pacing_mindestens_2_5_s(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)
    sleeps = []

    def fetcher(url: str) -> bytes:
        return json.dumps([{"data": [{"ticker": f"T{i}", "exchCode": "US"}]} for i in range(5)]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=sleeps.append)
    cusips = [f"C{i:09d}" for i in range(12)]                 # 3 Chunks -> 2 Pausen
    smart_money.cusip_to_tickers(cusips, http, cfg)
    gaps = [s for s in sleeps if s >= 2.0]
    assert len(gaps) == 2 and all(g >= 2.4 for g in gaps)  # Pause ~2,5 s -> max 25/Minute


def test_openfigi_429_retry(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)
    sleeps = []
    state = {"n": 0}

    def fetcher(url: str) -> bytes:
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, BytesIO(b""))
        return json.dumps([{"data": [{"ticker": "AAA", "exchCode": "US"}]}]).encode()

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, retry_tries=3,
                               retry_waits=(2.0, 5.0, 10.0), fetcher=fetcher,
                               errors=[], sleeper=sleeps.append)
    out = smart_money.cusip_to_tickers(["111111111"], http, cfg)
    assert out["111111111"] == "AAA"
    assert state["n"] == 2 and sleeps == [2.0]


def test_unmapped_einmalig_als_liste(monkeypatch):
    cfg = _openfigi_cfg(monkeypatch)

    def fetcher(url: str) -> bytes:
        return json.dumps([{"data": [{"ticker": "AAA", "exchCode": "US"}]}, {}]).encode()  # 2. CUSIP ohne data

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher, sleeper=lambda s: None)
    errors, unmapped = [], []
    out = smart_money.cusip_to_tickers(["111111111", "222222222"], http, cfg,
                                       errors=errors, unmapped=unmapped)
    assert unmapped == ["222222222"]
    assert errors == []                                        # nicht als Einzelzeilen
    assert "222222222" not in out                              # Miss -> nicht im Ergebnis (ticker NULL in DB)


# --------------------------------------------------------------------------
# (3) Quartalslabel aus reportDate + 13F-HR/A + Diagnose 2010/2011
# --------------------------------------------------------------------------

def test_quartalslabel_aus_report_date():
    assert smart_money.quartal_of("2026-06-30") == "2026-Q2"
    assert smart_money.quartal_of("2026-09-30") == "2026-Q3"
    assert smart_money.quartal_of("2026-12-31") == "2026-Q4"
    assert smart_money.quartal_of("") == ""


def test_13f_hr_a_ersetzt_original_label_aus_report_date():
    entries = [
        ("0001193125-26-111111", "2026-05-15", "13F-HR", "2026-03-31"),
        ("0001193125-26-352199", "2026-08-15", "13F-HR", "2026-06-30"),
        ("0001193125-26-352200", "2026-08-20", "13F-HR/A", "2026-06-30"),
    ]

    def fetcher(url: str) -> bytes:
        if f"submissions/CIK{CIK10}.json" in url:
            return _submissions_json(entries)
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings(CIK10, http)
    assert len(filings) == 2
    q2 = [f for f in filings if smart_money.quartal_of(f["report_date"]) == "2026-Q2"]
    assert len(q2) == 1
    assert q2[0]["form"] == "13F-HR/A" and q2[0]["accession"] == "0001193125-26-352200"
    assert q2[0]["report_date"] == "2026-06-30"


def test_atom_fallback_ohne_datum_im_term_liefert_neueste_nicht_2010_2011():
    """Diagnose: category term ohne Datum -> report_date '' -- alt kollabierte die
    Dedupe und die Sortierung lieferte die aeltesten Filings (2010/2011)."""
    dates = ["2010-08-15", "2011-08-15"] + [f"2026-0{m}-15" for m in range(1, 7)]
    items = "".join(
        f'<entry><title>13F-HR</title><updated>{d}T00:00:00Z</updated>'
        f'<category term="13F-HR"/>'   # KEIN Datum im term
        f'<link href="https://www.sec.gov/Archives/edgar/data/{CIK_N}/0001193125260000{i:02d}/'
        f'0001193125-26-0000{i:02d}-index.htm"/></entry>'
        for i, d in enumerate(dates))
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            + items + '</feed>').encode()

    def fetcher(url: str) -> bytes:
        if "submissions/CIK" in url:
            raise ValueError("submissions down -> Fallback Atom")
        if "output=atom" in url:
            return atom
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings(CIK10, http, max_filings=4)
    assert len(filings) == 4
    assert all(f["filing_date"] >= "2026-01-01" for f in filings)   # neueste, nicht 2010/2011
    assert filings[-1]["filing_date"] == "2026-06-15"


# --------------------------------------------------------------------------
# (4) 13D: HTTP 503 -> 3x Retry (5, 15, 30 s), blockiert den Lauf nicht
# --------------------------------------------------------------------------

def test_retry_503_dreimal_5_15_30_dann_erfolg():
    sleeps = []
    state = {"n": 0}
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=lambda u: b"{}",
                               sleeper=sleeps.append)

    def fn():
        state["n"] += 1
        if state["n"] <= 2:
            raise urllib.error.HTTPError("https://x", 503, "Service Unavailable", {}, BytesIO(b""))
        return "ok"

    assert smart_money._retry_503(fn, http) == "ok"
    assert state["n"] == 3 and sleeps == [5.0, 15.0]


def test_retry_503_nach_3_wiederholungen_weiterhin_fehler():
    sleeps = []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=lambda u: b"{}",
                               sleeper=sleeps.append)

    def fn():
        raise urllib.error.HTTPError("https://x", 503, "Service Unavailable", {}, BytesIO(b""))

    with pytest.raises(urllib.error.HTTPError):
        smart_money._retry_503(fn, http)
    assert sleeps == [5.0, 15.0, 30.0]   # 3 Wiederholungen


def test_13d_503_blockiert_lauf_nicht(tmp_path):
    cfg = _cfg()
    cfg["smart_money"]["funds"] = [{"name": "Starboard Value", "cik": "0001517137",
                                    "typ": "aktivist", "gewicht": 0.0, "positions_soll_max": 30}]
    cfg["smart_money"]["cusip_map"] = {"999999999": "KAT"}
    atom13f = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
               '<entry><title>13F-HR</title><updated>2026-08-15T00:00:00Z</updated>'
               '<category term="13F-HR 2026-06-30"/>'
               '<link href="https://www.sec.gov/Archives/edgar/data/1517137/000151713726000001/'
               '0001517137-26-000001-index.htm"/></entry></feed>').encode()

    def fetcher(url: str) -> bytes:
        if "submissions/CIK" in url:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, BytesIO(b""))
        if "output=atom" in url and "SC+13D" in url:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, BytesIO(b""))
        if "output=atom" in url:
            return atom13f
        if url.endswith("index.json"):
            return _index_json(["primary_doc.xml", "infotable.xml"])
        if url.endswith("infotable.xml"):
            return _infotable_xml(with_period=True, holdings=(("999999999", "KAT AG", "500 sh", "50"),))
        if url.endswith("primary_doc.xml"):
            return _primary_doc_xml("2026-06-30")
        raise ValueError(url)

    sleeps = []
    db = tmp_path / "sm.db"
    report = []
    errors = smart_money.fetch_all(cfg, quarters=1, db_path=db, fetcher=fetcher,
                                   sleeper=sleeps.append, report=report)
    assert [s for s in sleeps if s >= 1.0] == [5.0, 15.0, 30.0]   # 503-Retries 5/15/30 s
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM smart_money_positions WHERE fonds='Starboard Value'"
                    ).fetchone()[0]
    assert n == 1                     # Lauf nicht blockiert, Position gespeichert
    assert con.execute("SELECT COUNT(*) FROM smart_money_katalysator").fetchone()[0] == 0
    assert not [e for e in errors if e.get("fonds") == "Starboard Value"]
    # Filing-Liste (letzte 8) je Fonds im Bericht
    assert report and report[0].get("filings")
    f0 = report[0]["filings"][-1]
    assert f0["form"] == "13F-HR" and f0["accession"] == "0001517137-26-000001"
    assert f0["report_date"] == "2026-06-30" and f0["filing_date"] == "2026-08-15"


# --------------------------------------------------------------------------
# (5) show --fonds: Perioden + Anzahl Positionen je Quartal
# --------------------------------------------------------------------------

def test_show_fonds(tmp_path, capsys):
    db = tmp_path / "sm.db"
    con = smart_money.connect(db)
    for q, tickers in (("2026-Q2", [("AAA", "111111111"), ("BBB", "222222222")]),
                       ("2026-Q1", [("AAA", "111111111")])):
        for t, cusip in tickers:
            con.execute(
                "INSERT INTO smart_money_positions (fonds, periode, filing_datum, cusip, ticker, shares,"
                " wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("Starboard Value", q, "2026-08-20", cusip, t, 100.0, 1000.0, 0.01, "neu", None, 2,
                 None, "2026-08-20T00:00:00"))
    con.commit()
    smart_money.cmd_show(argparse.Namespace(db=str(db), ticker=None, fonds="Starboard Value"))
    out = capsys.readouterr().out
    assert "fonds=Starboard Value" in out and "2 Perioden" in out
    assert "2026-Q2" in out and "positionen=2" in out
    assert "2026-Q1" in out and "positionen=1" in out
