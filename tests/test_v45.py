r"""
(v45) Filing-Liste primaer aus https://data.sec.gov/submissions/CIK##########.json
     (filings.recent: accessionNumber, form, filingDate, reportDate; 13F-HR/13F-HR/A),
     Atom-Feed nur noch Fallback -- dort Accession per Regex (\d{10}-\d{2}-\d{6}),
     damit das '-index.htm'-Suffix nicht in die URL gelangt.
     Bug alt: .../Archives/edgar/data/1067983/000119312526352200index.htm/index.json
     richtig: .../Archives/edgar/data/1067983/000119312526352200/index.json
(v45) Fehlerlog: HTTP-Fehler (403/429) tragen den Fondsnamen (statt "None").
"""

import json
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"  # sec.user_agent aus uebernommener config.yaml

CIK10 = "0001067983"   # Berkshire Hathaway
CIK_N = "1067983"


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["max_requests_per_second"] = 100000
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


def _submissions_json(entries, name="BERKSHIRE HATHAWAY INC"):
    """entries: [(accession, filingDate, form, reportDate)] -> submissions/CIK##########.json"""
    return json.dumps({
        "name": name,
        "filings": {"recent": {
            "accessionNumber": [e[0] for e in entries],
            "filingDate": [e[1] for e in entries],
            "form": [e[2] for e in entries],
            "reportDate": [e[3] for e in entries],
        }}}).encode()


def _index_json() -> bytes:
    return json.dumps({"directory": {"item": [
        {"name": "infotable.xml"}, {"name": "primary_doc.xml"}]}}).encode()


# 3 Filings: 13F-HR Q1, 13F-HR Q2 (alt), 13F-HR/A Q2 (Nachmeldung, spaeteres filing_date)
THREE_FILINGS = [
    ("0001193125-25-111111", "2025-05-15", "13F-HR", "2025-03-31"),
    ("0001193125-26-352199", "2025-08-15", "13F-HR", "2025-06-30"),
    ("0001193125-26-352200", "2025-08-20", "13F-HR/A", "2025-06-30"),
]


def test_submissions_3_filings_accession_und_url():
    """submissions-JSON mit 3 Filings -> nur 13F-HR/13F-HR/A, neueste Fassung je
    report_date gewinnt; URL-Bau .../000119312526352200/index.json (ohne '-index.htm')."""
    urls = []

    def fetcher(url: str) -> bytes:
        urls.append(url)
        if f"submissions/CIK{CIK10}.json" in url:
            return _submissions_json(THREE_FILINGS)
        if url.endswith("index.json"):
            return _index_json()
        if url.endswith("primary_doc.xml"):
            return b'<?xml version="1.0"?><doc><periodOfReport>2025-06-30</periodOfReport></doc>'
        if url.endswith("infotable.xml"):
            return b'<?xml version="1.0"?><informationTable></informationTable>'
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings(CIK10, http, max_filings=12)
    assert [f["accession"] for f in filings] == ["0001193125-25-111111", "0001193125-26-352200"]
    assert all(f["form"].startswith("13F-HR") for f in filings)  # 10-K/andere fallen raus
    assert filings[-1]["form"] == "13F-HR/A" and filings[-1]["filing_date"] == "2025-08-20"

    rd, holdings, options = smart_money.load_13f_quarter(CIK10, "0001193125-26-352200", http)
    assert rd == "2025-06-30"
    assert f"https://www.sec.gov/Archives/edgar/data/{CIK_N}/000119312526352200/index.json" in urls
    assert f"https://www.sec.gov/Archives/edgar/data/{CIK_N}/000119312526352200/infotable.xml" in urls
    assert not any("index.htm" in u for u in urls)  # kein .../000119312526352200index.htm/index.json


# Echtes Atom-Linkformat: .../<accession-ohne-Bindestriche>/<accession-mit-Bindestrichen>-index.htm
ATOM_REAL = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
    '<entry><title>13F-HR</title><updated>2025-08-20T00:00:00Z</updated>'
    '<category term="13F-HR 2025-06-30"/>'
    f'<link href="https://www.sec.gov/Archives/edgar/data/{CIK_N}/000119312526352200/'
    '0001193125-26-352200-index.htm"/>'
    '</entry></feed>').encode()


def test_atom_fallback_accession_per_regex():
    r"""Atom nur noch Fallback (submissions schlaegt fehl): Accession per Regex
    (\d{10}-\d{2}-\d{6}) aus dem Link -> '0001193125-26-352200' (ohne '-index.htm')."""
    urls = []

    def fetcher(url: str) -> bytes:
        urls.append(url)
        if "submissions/CIK" in url:
            raise ValueError("submissions down -> Fallback Atom")
        if "output=atom" in url:
            return ATOM_REAL
        if url.endswith("index.json"):
            return _index_json()
        if url.endswith("infotable.xml"):
            return b'<?xml version="1.0"?><informationTable></informationTable>'
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings(CIK10, http)
    assert len(filings) == 1
    assert filings[0]["accession"] == "0001193125-26-352200"
    assert filings[0]["form"] == "13F-HR"
    assert filings[0]["report_date"] == "2025-06-30"
    assert filings[0]["filing_date"] == "2025-08-20"

    smart_money.load_13f_quarter(CIK10, filings[0]["accession"], http)
    assert f"https://www.sec.gov/Archives/edgar/data/{CIK_N}/000119312526352200/index.json" in urls
    assert not any("index.htm" in u for u in urls)


def test_accession_digits_schuetzt_vor_index_htm_suffix():
    assert smart_money._accession_digits("0001193125-26-352200") == "000119312526352200"
    assert smart_money._accession_digits("0001193125-26-352200-index.htm") == "000119312526352200"
    assert smart_money._accession_digits("1067983000000000") == "1067983000000000"
    assert smart_money._accession_from_href(
        f"https://www.sec.gov/Archives/edgar/data/{CIK_N}/000119312526352200/"
        "0001193125-26-352200-index.htm") == "0001193125-26-352200"


def test_fehlerlog_zeigt_fondsnamen_statt_none(monkeypatch):
    """HTTP 403 nach Retries -> Fehlerlog-Eintrag traegt den Fondsnamen."""
    errors = []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000,
                               retry_waits=(0.0, 0.0), retry_tries=2,
                               errors=errors, sleeper=lambda s: None)
    http.fonds = "Berkshire Hathaway"  # wird von fetch_all je Fonds gesetzt

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get(f"https://data.sec.gov/submissions/CIK{CIK10}.json")
    assert len(errors) == 1
    assert errors[0].get("fonds") == "Berkshire Hathaway"   # nicht None
    assert errors[0]["http_code"] == 403
