"""v44 Tests (Fixtures, kein Netz, kein Volllauf):

(1) SEC-Zugriff / User-Agent:
    - ungueltiger UA (fehlt / "example" / "test" / kein "@") -> UserAgentError,
      Abbruch VOR dem ersten Request (Fetcher zaehlt Aufrufe)
    - gueltiger UA aus smart_money.user_agent (Fallback sec.user_agent)
    - HTTP 403 -> 3 Wiederholungen mit Wartezeiten 2, 5, 10 s, danach Fehlerlog
      mit URL + Hinweis "User-Agent pruefen" (Sleeper + urllib-Mock injiziert)
(2) CIK-Aufloesung:
    - Treffer mit mehreren Kandidaten -> volle Liste, KEIN Auto-Pick
    - CIK ohne 13F-HR in submissions -> "kein Filer"
    - CIK aus config -> validiert, EDGAR-Name + letztes 13F-HR im Bericht
"""

import json
from pathlib import Path
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"  # sec.user_agent aus uebernommener config.yaml


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["max_requests_per_second"] = 100000
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


# --------------------------------------------------------------------------
# (1) User-Agent-Validierung
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "research@example.com", "test test@test.de",
                                 "kein-at-zeichen", "Value-Screener research@example.com"])
def test_ungueltiger_user_agent_abbruch_vor_request(bad):
    cfg = _cfg(user_agent=bad)
    cfg["sec"]["user_agent"] = bad
    cfg["sec"]["enabled"] = True   # echter Modus: UA wird validiert
    calls = []

    def fetcher(url):
        calls.append(url)
        return b"{}"
    with pytest.raises(smart_money.UserAgentError) as exc:
        smart_money.fetch_all(cfg, quarters=1, db_path=":memory:", fetcher=fetcher)
    assert "User-Agent" in str(exc.value)
    assert calls == []  # Abbruch VOR dem ersten Request


def test_gueltiger_user_agent_aus_smart_money():
    cfg = _cfg(user_agent=VALID_UA)
    cfg["sec"]["user_agent"] = "fallback@example.com"  # darf nicht greifen/gelb sein
    assert smart_money.user_agent_from_cfg(cfg) == VALID_UA


def test_user_agent_fallback_auf_sec():
    cfg = _cfg()
    cfg["smart_money"]["user_agent"] = ""
    cfg["sec"]["user_agent"] = VALID_UA
    assert smart_money.user_agent_from_cfg(cfg) == VALID_UA


def test_config_ua_eingetragen():
    """config.yaml traegt den echten Kontakt (sec + smart_money).

    Liest die Datei direkt (load_config ist lru_cache -- andere Tests duerfen
    den Cache vorher mit mutiertem Dict gefuellt haben).
    """
    import yaml
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    # user hat die Datei uebernommen: sec.user_agent ist die Quelle
    # (smart_money.user_agent optional; Fallback auf sec.user_agent in user_agent_from_cfg)
    ua_sec = raw["sec"].get("user_agent")
    assert ua_sec and "@" in ua_sec and "example" not in ua_sec.lower() and "test" not in ua_sec.lower()
    assert ua_sec == "Tom Homburg t09homburg@gmail.com"
    # Fallback-Verhalten: ohne smart_money.user_agent greift sec.user_agent
    cfg = load_config()
    cfg2 = dict(cfg)
    cfg2["smart_money"] = {k: v for k, v in cfg["smart_money"].items() if k != "user_agent"}
    assert smart_money.user_agent_from_cfg(cfg2) == "Tom Homburg t09homburg@gmail.com"


# --------------------------------------------------------------------------
# (1) 403 -> Retries 2/5/10 + Fehlerlog (urllib gemockt)
# --------------------------------------------------------------------------

def test_http_403_retries_und_fehlerlog(monkeypatch):
    waits = []
    errors = []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000,
                               retry_waits=(2.0, 5.0, 10.0), retry_tries=3,
                               errors=errors, sleeper=waits.append)

    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        assert req.headers.get("User-agent") == VALID_UA or req.get_header("User-agent") == VALID_UA
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get("https://data.sec.gov/submissions/CIK0001067983.json")
    assert calls["n"] == 3                      # 3 Versuche
    assert waits == [2.0, 5.0]                  # Wartezeiten 2, 5 (vor 2. und 3. Retry)
    assert len(errors) == 1
    assert errors[0]["http_code"] == 403
    assert "data.sec.gov" in errors[0]["url"]
    assert "User-Agent pruefen" in errors[0]["error"]


def test_http_429_retries_und_fehlerlog(monkeypatch):
    waits, errors = [], []
    http = smart_money.SecHttp(VALID_UA, max_rps=100000,
                               retry_waits=(2.0, 5.0, 10.0), retry_tries=3,
                               errors=errors, sleeper=waits.append)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get("https://www.sec.gov/cgi-bin/browse-edgar?x=1")
    assert calls["n"] == 3 and waits == [2.0, 5.0]
    assert errors[0]["http_code"] == 429 and "User-Agent pruefen" in errors[0]["error"]


def test_http_200_kein_retry(monkeypatch):
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, sleeper=lambda s: None)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: BytesIO(b'{"ok": true}'))
    # BytesIO unterstuetzt read() wie ein Response
    class Resp(BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: Resp(b'{"ok": true}'))
    assert json.loads(http.get("https://x").decode()) == {"ok": True}
    assert http.errors == []


# --------------------------------------------------------------------------
# (2) CIK-Aufloesung (Fixtures)
# --------------------------------------------------------------------------

def _submissions(cik10: str, forms, dates, name="X"):
    return json.dumps({"name": name,
                       "filings": {"recent": {"form": forms, "filingDate": dates}}}).encode()


def _company_atom(hits) -> bytes:
    items = "".join(
        f'<entry><title>{t}</title><updated>2025-01-01T00:00:00Z</updated>'
        f'<link href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK={c}"/></entry>'
        for c, t in hits)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>'.encode()


def test_mehrere_treffer_liste_kein_autopick():
    """Firmensuche mit 2 Kandidaten -> beide in der Trefferliste, cik bleibt leer."""
    def fetcher(url: str) -> bytes:
        if "action=getcompany&company=" in url:
            return _company_atom([("0009999999", "STARBOARD VALUE LP"),
                                  ("0008888888", "STARBOARD VALUE &amp; OPP FUND")])
        if "submissions/CIK0009999999" in url:
            return _submissions("0009999999", ["13F-HR", "10-K"], ["2025-08-15", "2025-04-01"],
                                "STARBOARD VALUE LP")
        if "submissions/CIK0008888888" in url:
            return _submissions("0008888888", ["13F-HR"], ["2025-05-15"], "STARBOARD VALUE & OPP FUND")
        raise ValueError(url)
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    cfg = _cfg(name_variants={"Starboard Value": ["Starboard Value"]})
    cfg["smart_money"]["funds"] = [{"name": "Starboard Value", "cik": "", "typ": "aktivist",
                                    "gewicht": 0.0, "positions_soll_max": 30}]
    out = smart_money.resolve_all_ciks(cfg, http)
    e = out[0]
    assert e["cik"] == ""                              # nichts automatisch uebernommen
    assert e["filer_status"] == "manuell eintragen"
    assert {t["cik"] for t in e["treffer"]} == {"0009999999", "0008888888"}
    assert all(t["last_13f_hr"] for t in e["treffer"])


def test_cik_ohne_13f_hr_ist_kein_filer():
    def fetcher(url: str) -> bytes:
        if "submissions/CIK" in url:
            return _submissions("0000000001", ["10-K", "8-K"], ["2025-04-01", "2025-01-01"],
                                "NO FILER INC")
        raise ValueError(url)
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    assert smart_money._validate_cik_13f("1", http) is None
    cfg = _cfg()
    cfg["smart_money"]["funds"] = [{"name": "No Filer", "cik": "0000000001", "typ": "konzern",
                                    "gewicht": 1.0, "positions_soll_max": 10}]
    out = smart_money.resolve_all_ciks(cfg, http)
    assert out[0]["filer_status"] == "kein Filer"
    assert out[0]["cik"] == "0000000001"               # CIK bleibt sichtbar, aber kein Filer


def test_cik_aus_config_validiert_mit_name():
    def fetcher(url: str) -> bytes:
        if "submissions/CIK0001067983" in url:
            return _submissions("0001067983", ["13F-HR", "13F-HR/A", "10-K"],
                                ["2025-08-15", "2025-05-20", "2025-04-01"],
                                "BERKSHIRE HATHAWAY INC")
        raise ValueError(url)
    http = smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher)
    cfg = _cfg()
    cfg["smart_money"]["funds"] = [{"name": "Berkshire Hathaway", "cik": "0001067983",
                                    "typ": "konzern", "gewicht": 1.0, "positions_soll_max": 50}]
    out = smart_money.resolve_all_ciks(cfg, http)
    e = out[0]
    assert e["filer_status"] == "13F-HR (config)"
    assert e["bericht_name"] == "BERKSHIRE HATHAWAY INC"
    assert e["last_13f_hr"] == "2025-08-15"            # 13F-HR/A zaehlt, neuestes Datum gewinnt


def test_baupost_cik_in_config():
    import yaml
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    baupost = next(f for f in raw["smart_money"]["funds"] if "Baupost" in f["name"])
    assert baupost["cik"] == "0001061768"
    assert baupost["name"] == "Baupost Group"  # uebernommene Datei
