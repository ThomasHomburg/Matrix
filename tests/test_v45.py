"""v45 Tests (Mock/Fixtures, kein Netz, kein Volllauf):

(2) Header wird bei JEDEM Request gesetzt (User-Agent aus sec.user_agent,
    Accept-Encoding gzip, Host) -- sec_get ueber gemocktes urllib geprueft.
(2) 403 -> Wiederholungen (2/5/10 s) + Fehlerlog mit URL, Status, Antwortbeginn.
(4) Ablauf submissions-JSON -> Accession -> Archives -> infotable.xml (Fixture).
(3) Config-Suche: cwd-config.yaml hat Vorrang; user_agent mit example.com -> Abbruch.
(1) diagnose: Config-Pfad (absolut) + UA + Status je Test-URL (mit Fixture-Fetcher).
"""

import json
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

import pytest

import smart_money
from config import find_config_path, load_config
import config as config_mod

VALID_UA = "Tom Homburg t09homburg@gmail.com"  # sec.user_agent aus uebernommener config.yaml


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"] = dict(cfg.get("sec", {}))
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["max_requests_per_second"] = 100000
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


# --------------------------------------------------------------------------
# (2) Header bei jedem Request
# --------------------------------------------------------------------------

def test_sec_get_setzt_alle_header(monkeypatch):
    seen = []

    class Resp(BytesIO):
        status = 200
        headers = {"Content-Encoding": "identity"}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        seen.append(req)
        return Resp(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status, body, _ = smart_money.sec_get("https://data.sec.gov/submissions/CIK0001067983.json",
                                          VALID_UA)
    assert status == 200 and body == b"ok"
    req = seen[0]
    assert isinstance(req, urllib.request.Request)          # Request-Objekt, nicht nackter String
    assert req.get_header("User-agent") == VALID_UA         # User-Agent gesetzt
    assert req.get_header("Accept-encoding") == "gzip"      # Accept-Encoding gzip
    assert req.get_header("Host") == "data.sec.gov"          # Host gesetzt


def test_sec_get_gzip_wird_entpackt(monkeypatch):
    import gzip
    payload = gzip.compress(b'{"hello": "world"}')

    class Resp(BytesIO):
        status = 200
        headers = {"Content-Encoding": "gzip"}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: Resp(payload))
    status, body, _ = smart_money.sec_get("https://www.sec.gov/x", VALID_UA)
    assert status == 200
    assert json.loads(body.decode()) == {"hello": "world"}


def test_403_wiederholung_und_fehlerlog_mit_url_status_antwort(monkeypatch):
    """(2) 403 -> 3 Versuche mit Waits 2/5/10, danach Fehlerlog mit URL,
    Statuscode und Antwortbeginn."""
    waits, errors = [], []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000,
                               retry_waits=(2.0, 5.0, 10.0), retry_tries=3,
                               errors=errors, sleeper=waits.append)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden",
                                     {"Content-Type": "text/html"},
                                     BytesIO(b"<html>Blocked: SEC requires a valid User-Agent</html>"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get("https://data.sec.gov/submissions/CIK0001067983.json")
    assert calls["n"] == 3
    assert waits == [2.0, 5.0]  # vor 2. und 3. Retry
    assert len(errors) == 1
    e = errors[0]
    assert e["url"] == "https://data.sec.gov/submissions/CIK0001067983.json"
    assert e["http_code"] == 403
    assert "User-Agent" in e["antwort_beginn"] or "Blocked" in e["antwort_beginn"]
    assert "User-Agent pruefen" in e["error"]


def test_503_wird_ebenfalls_wiederholt(monkeypatch):
    """v45 Nachtrag: 503 (SEC Edge/rate-limit) -> gleiche Retry-Logik wie 403/429."""
    waits, errors = [], []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000,
                               retry_waits=(2.0, 5.0, 10.0), retry_tries=3,
                               errors=errors, sleeper=waits.append)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable",
                                     {"Content-Type": "text/html"},
                                     BytesIO(b"<html>Request blocked (edge)</html>"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as exc:
        http.get("https://www.sec.gov/Archives/edgar/data/1061768/")
    assert exc.value.code == 503
    assert calls["n"] == 3
    assert waits == [2.0, 5.0]
    assert errors[0]["http_code"] == 503
    assert "Archives/edgar/data/1061768" in errors[0]["url"]
    assert "Request blocked" in errors[0]["antwort_beginn"]


# --------------------------------------------------------------------------
# (4) submissions -> accession -> archives -> infotable (Fixture)
# --------------------------------------------------------------------------

def _submissions_json():
    return json.dumps({
        "name": "BERKSHIRE HATHAWAY INC",
        "filings": {"recent": {
            "accessionNumber": ["0001067983-25-000001", "0001067983-25-000002"],
            "form": ["13F-HR", "13F-HR"],
            "filingDate": ["2025-05-15", "2025-08-15"],
            "reportDate": ["2025-03-31", "2025-06-30"],
        }}}).encode()


def _infotable_xml():
    return (b'<?xml version="1.0"?><informationTable>'
            b'<infoTable><nameOfIssuer>AAA Corp</nameOfIssuer><cusip>111111111</cusip>'
            b'<shrsOrPrnAmt>1000 sh</shrsOrPrnAmt><value>100</value></infoTable>'
            b'</informationTable>')


def test_submissions_to_infotable_ablauf():
    """(4) list_13f_filings liest submissions (kein cgi-bin); load_13f_quarter
    laedt Archives/<cik>/<acc>/infotable.xml; report_date aus submissions."""
    seen_urls = []

    def fetcher(url: str) -> bytes:
        seen_urls.append(url)
        if "submissions/CIK0001067983" in url:
            return _submissions_json()
        if url.endswith("index.json"):
            return json.dumps({"directory": {"item": [{"name": "infotable.xml"}]}}).encode()
        if url.endswith("infotable.xml"):
            return _infotable_xml()
        raise ValueError(url)

    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings("0001067983", http)
    assert "cgi-bin" not in " ".join(seen_urls)           # kein cgi-bin mehr
    assert [f["report_date"] for f in filings] == ["2025-03-31", "2025-06-30"]
    assert filings[0]["accession"] == "0001067983-25-000001"
    rd, holdings, options = smart_money.load_13f_quarter(
        "0001067983", filings[0]["accession"], http, report_date=filings[0]["report_date"])
    assert rd == "2025-03-31"                              # aus submissions
    assert "Archives/edgar/data/1067983/000106798325000001/infotable.xml" in seen_urls[-1]
    assert holdings[0]["cusip"] == "111111111" and holdings[0]["shares"] == 1000.0
    assert options == []


def test_hr_a_gewinnt_ueber_submissions():
    """13F-HR/A mit spaeterem filingDate ersetzt Original zum selben reportDate."""
    def fetcher(url: str) -> bytes:
        if "submissions/CIK" in url:
            return json.dumps({"filings": {"recent": {
                "accessionNumber": ["A1", "A2"],
                "form": ["13F-HR", "13F-HR/A"],
                "filingDate": ["2025-08-15", "2025-08-20"],
                "reportDate": ["2025-06-30", "2025-06-30"],
            }}}).encode()
        raise ValueError(url)
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings("0001067983", http)
    assert len(filings) == 1
    assert filings[0]["form"] == "13F-HR/A" and filings[0]["filing_date"] == "2025-08-20"


# --------------------------------------------------------------------------
# (3) Config-Suche: cwd hat Vorrang; example.com -> Abbruch
# --------------------------------------------------------------------------

def test_config_cwd_hat_vorrang(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text('sec: {user_agent: "CWD User cwd@example.org"}\n',
                                           encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    p = find_config_path()
    assert p == (tmp_path / "config.yaml").resolve()
    cfg = load_config(p)
    assert cfg["sec"]["user_agent"] == "CWD User cwd@example.org"


def test_config_example_user_agent_abbruch(tmp_path, monkeypatch):
    """(3) Config mit user_agent example.com -> Abbruch mit klarer Meldung."""
    (tmp_path / "config.yaml").write_text(
        'sec: {user_agent: "research@example.com", enabled: true}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(find_config_path())
    cfg["sec"]["enabled"] = True
    with pytest.raises(smart_money.UserAgentError) as exc:
        smart_money.user_agent_from_cfg(cfg)
    assert "example" in str(exc.value) or "User-Agent" in str(exc.value)


# --------------------------------------------------------------------------
# (1) diagnose (mit Fixture-Fetcher ueber sec_get)
# --------------------------------------------------------------------------

def test_submissions_mit_3_filings_richtige_accessions():
    """(1) submissions-JSON mit 3 Filings (13F-HR, 13F-HR/A, 10-K) -> nur 13F-HR,
    neueste filing_date je report_date gewinnt, richtige Accession."""
    def fetcher(url: str) -> bytes:
        assert "submissions/CIK0001067983" in url
        return json.dumps({
            "name": "BERKSHIRE HATHAWAY INC",
            "filings": {"recent": {
                "accessionNumber": ["0001067983-25-000010", "0001067983-25-000011",
                                    "0001067983-25-000012", "0001067983-25-000013"],
                "form": ["13F-HR", "13F-HR", "10-K", "13F-HR/A"],
                "filingDate": ["2025-05-15", "2025-08-14", "2025-04-01", "2025-08-20"],
                "reportDate": ["2025-03-31", "2025-06-30", "2025-12-31", "2025-06-30"],
            }}}).encode()
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings("0001067983", http)
    assert [f["report_date"] for f in filings] == ["2025-03-31", "2025-06-30"]
    assert filings[0]["accession"] == "0001067983-25-000010"
    # 13F-HR/A (2025-08-20) ersetzt Original (2025-08-14) zum selben report_date
    assert filings[1]["accession"] == "0001067983-25-000013"
    assert filings[1]["form"] == "13F-HR/A"


def test_submissions_filials_files_fallback():
    """(1) filings.files: zusaetzliche JSON-Datei wird nachgeladen."""
    seen = []

    def fetcher(url: str) -> bytes:
        seen.append(url)
        if url.endswith("CIK0001067983.json"):
            return json.dumps({"filings": {
                "recent": {"accessionNumber": ["0001067983-25-000001"], "form": ["13F-HR"],
                           "filingDate": ["2025-05-15"], "reportDate": ["2025-03-31"]},
                "files": [{"name": "CIK0001067983_2024.json"}],
            }}).encode()
        if "CIK0001067983_2024.json" in url:
            return json.dumps({"accessionNumber": ["0001067983-24-000009"], "form": ["13F-HR"],
                                "filingDate": ["2024-05-15"], "reportDate": ["2024-03-31"]}).encode()
        raise ValueError(url)
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    filings = smart_money.list_13f_filings("0001067983", http)
    assert any("CIK0001067983_2024.json" in u for u in seen)
    assert {f["report_date"] for f in filings} == {"2025-03-31", "2024-03-31"}


def test_index_json_fallback_auf_direkten_pfad():
    """(2) index.json schlaegt fehl -> Fallback: infotable.xml direkt laden."""
    seen = []

    def fetcher(url: str) -> bytes:
        seen.append(url)
        if url.endswith("index.json"):
            raise ValueError("404")
        if url.endswith("infotable.xml"):
            return b'<?xml version="1.0"?><informationTable><infoTable><cusip>111111111</cusip>' \
                   b'<nameOfIssuer>AAA</nameOfIssuer><shrsOrPrnAmt>10 sh</shrsOrPrnAmt>' \
                   b'<value>1.0</value></infoTable></informationTable>'
        raise ValueError(url)
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    rd, holdings, options = smart_money.load_13f_quarter("0001067983", "0001067983-25-000001",
                                                         http, report_date="2025-03-31")
    assert rd == "2025-03-31"
    assert holdings and holdings[0]["cusip"] == "111111111"
    assert any(u.endswith("/000106798325000001/infotable.xml") for u in seen)


def test_fehlerlog_enthaelt_retry_after(monkeypatch):
    """(4) 429 mit Retry-After-Header -> Fehlerlog enthaelt retry_after."""
    errors = []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000,
                               retry_waits=(2.0, 5.0, 10.0), retry_tries=3,
                               errors=errors, sleeper=lambda s: None)

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "30", "Content-Type": "text/html"},
                                     BytesIO(b"<html>Rate limited</html>"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get("https://data.sec.gov/submissions/CIK0001067983.json")
    assert errors[0]["http_code"] == 429
    assert errors[0]["retry_after"] == "30"
    assert errors[0]["url"].endswith("CIK0001067983.json")
    assert "Rate limited" in errors[0]["antwort_beginn"]


def test_cik_leer_kein_netz_hinweis_config():
    """(3) resolve_all_ciks: cik leer -> Hinweis, fetcher darf nie aufgerufen werden."""
    def fetcher(url: str) -> bytes:
        raise AssertionError(f"kein Netzabruf: {url}")
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    cfg = {"smart_money": {"funds": [{"name": "X", "cik": "", "typ": "aktivist",
                                      "gewicht": 0.0, "positions_soll_max": 5}]}}
    out = smart_money.resolve_all_ciks(cfg, http)
    assert "config.yaml eintragen" in out[0]["filer_status"]


def test_diagnose_ausgabe(monkeypatch, capsys):
    import argparse
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(req.full_url)
        class Resp(BytesIO):
            status = 200
            headers = {"Content-Encoding": "identity"}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return Resp(b'{"ok": true, "erste 200 zeichen": "..."}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    smart_money.cmd_diagnose(argparse.Namespace())
    out = capsys.readouterr().out
    assert "Config-Pfad (absolut):" in out
    assert str(Path.cwd().resolve()) in out or "config.yaml" in out
    assert f"User-Agent (gesendet): {VALID_UA!r}" in out
    assert out.count("[200]") == 2  # zwei Test-URLs (kein cgi-bin mehr)
    assert len(calls) == 2
    for u in ("data.sec.gov/submissions/CIK0001067983.json",
              "www.sec.gov/Archives/edgar/data/1067983/"):
        assert any(u in c for c in calls)
    assert not any("cgi-bin" in c for c in calls)
