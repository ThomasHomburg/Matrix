r"""
(v49 4a) OpenFIGI-Antwortstruktur in cusip_to_tickers korrekt auswerten (kein Netz,
Fixture-Fetcher fuer die POST-Antwort):

Live gegen https://api.openfigi.com/v3/mapping: die API antwortet je Eingabe mit
{"data": [eintrag, ...]} (Liste von Boersen-Eintraegen mit figi/ticker/exchCode/
securityType) oder {"error": "..."} -- NICHT mit {"ticker": ...}. Der alte Code
las (item or {}).get("ticker") -> immer None -> OpenFIGI mappte nie ein CUSIP.
Zusaetzliche Falle: bei MCO steht der deutsche Listing-Ticker DUT (Stuttgart,
exchCode GR) VOR dem US-Eintrag -- gewaehlt wird daher der Eintrag mit
exchCode == "US" (US-Composite).

(a) echte Struktur {"data": [DUT(GR), MCO(US)]} -> Mapping ergibt MCO, nicht DUT.
(b) {"error": "Invalid idValue format."}-Eintrag -> kein Mapping, kein Abbruch.
(c) AAPL mit US-Eintrag -> AAPL.
(d) gemappte CUSIPs landen mit quelle "openfigi" in cusip_map.
(e) Gegenprobe: cusip_map/config-Treffer fragen OpenFIGI nicht an.
"""

import json

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

CUSIP_MCO = "191216100"
CUSIP_AAPL = "037833100"
CUSIP_ERR = "000000000"


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["user_agent"] = VALID_UA
    sm["max_requests_per_second"] = 100000
    sm["cusip_map"] = {}
    sm["cusip_map_url"] = OPENFIGI_URL
    cfg["smart_money"] = sm
    return cfg


def _http(fetcher):
    return smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher,
                               sleeper=lambda s: None)


def _us(ticker):
    return {"data": [{"figi": "BBG000000001", "ticker": ticker, "exchCode": "US",
                      "securityType": "Common Stock"}]}


# (a)+(b)+(c) eine Anfrage, drei Eingaben: MCO (DUT vor US), error-Eintrag, AAPL
def test_a_b_c_echte_antwortstruktur_us_eintrag_und_error(monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()
    response = [
        {"data": [{"figi": "BBG000DUT000", "ticker": "DUT", "exchCode": "GR",
                   "securityType": "Common Stock"},
                  {"figi": "BBG000MCO000", "ticker": "MCO", "exchCode": "US",
                   "securityType": "Common Stock"}]},
        {"error": "Invalid idValue format."},
        _us("AAPL"),
    ]

    def fetcher(url: str) -> bytes:
        assert "openfigi" in url
        return json.dumps(response).encode()

    out = smart_money.cusip_to_tickers([CUSIP_MCO, CUSIP_ERR, CUSIP_AAPL],
                                       _http(fetcher), cfg)
    assert out[CUSIP_MCO] == "MCO"          # US-Eintrag gewinnt, nicht DUT (GR)
    assert CUSIP_ERR not in out             # error-Eintrag -> kein Mapping, kein Abbruch
    assert out[CUSIP_AAPL] == "AAPL"
    assert len(out) == 2


# (c) nur US-Eintrag, kein auslaendischer davor
def test_c_aapl_us_eintrag(monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()

    def fetcher(url: str) -> bytes:
        return json.dumps([_us("AAPL")]).encode()

    out = smart_money.cusip_to_tickers([CUSIP_AAPL], _http(fetcher), cfg)
    assert out == {CUSIP_AAPL: "AAPL"}


# (d) gemappte CUSIPs landen dauerhaft in cusip_map mit quelle "openfigi"
def test_d_gemappte_cusips_in_cusip_map_quelle_openfigi(monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()

    def fetcher(url: str) -> bytes:
        return json.dumps([_us("MCO")]).encode()

    con = smart_money.connect(":memory:")
    out = smart_money.cusip_to_tickers([CUSIP_MCO], _http(fetcher), cfg, con=con)
    assert out == {CUSIP_MCO: "MCO"}
    assert con.execute("SELECT ticker, quelle FROM cusip_map WHERE cusip=?",
                       (CUSIP_MCO,)).fetchone() == ("MCO", "openfigi")
    con.close()


# (e) Gegenprobe: Treffer aus cusip_map (DB) und statischem config-cusip_map fragen
# OpenFIGI nicht an (Fetcher wuerde Werfen)
def test_e_db_und_config_treffer_ohne_openfigi_anfrage(monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()
    cfg["smart_money"]["cusip_map"] = {"222222222": "BBB"}     # statisch

    def fetcher(url: str) -> bytes:
        raise AssertionError(f"OpenFIGI trotz Treffer angefragt: {url}")

    con = smart_money.connect(":memory:")
    con.execute("INSERT INTO cusip_map (cusip, ticker, quelle, geladen_am) "
                "VALUES (?, 'AAA', 'manuell', 't')", (CUSIP_AAPL,))
    con.commit()
    out = smart_money.cusip_to_tickers([CUSIP_AAPL, "222222222"], _http(fetcher), cfg, con=con)
    assert out == {CUSIP_AAPL: "AAA", "222222222": "BBB"}
    con.close()
