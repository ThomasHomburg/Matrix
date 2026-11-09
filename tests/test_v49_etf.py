r"""
(v49 4b) ETF/Index-Erkennung und -Ausblendung in score/overlap (kein Netz, Fixtures):

Erkennung ueber zwei Quellen (Code-Kommentar in smart_money.py): (a) statische
Config-Liste smart_money.etf_ticker (keine Magic Strings im Code); (b) OpenFIGI-
US-Composite-Eintrag mit securityType != 'Common Stock' (z. B. 'ETP'/'ETF') bzw.
marketSector 'MarketSector/Market' -> cusip_map.is_etf=1 (additive ALTER-Migration
in connect()). SEC company_tickers.json enthaelt keine ETFs -> dort kein Kriterium.
store_quarter speichert ETF-Positionen weiterhin; smart_money_score/overlap werten
sie nicht (Score 1, Urteil neutral, CSV-Spalte etf=ja am Ende).

(a) OpenFIGI-Fixture securityType "ETP" -> is_etf=1 (out-Param + cusip_map), Score ignoriert den Ticker.
(b) Common-Stock-Eintrag -> is_etf=0, normale Wertung.
(c) Config-Liste etf_ticker greift ohne OpenFIGI-Anfrage.
(d) ETF-Zeile steht in der overlap-CSV mit etf=ja, Score 1, neutral; Einzeltitel unveraendert.
(e) bestehende Tests unberuehrt gruen (Gesamtlauf pytest).
"""

import json
import sqlite3

import pytest

from config import load_config
import smart_money


VALID_UA = "Tom Homburg t09homburg@gmail.com"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["user_agent"] = VALID_UA
    sm["max_requests_per_second"] = 100000
    sm["cusip_map"] = {}
    sm["cusip_map_url"] = OPENFIGI_URL
    sm["funds"] = [{"name": "F", "cik": "0000000001", "gewicht": 1.0}]
    sm["etf_ticker"] = ["SPY", "IWM", "EEM"]
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


def _http(fetcher):
    return smart_money.SecHttp(VALID_UA, max_rps=100000, fetcher=fetcher,
                               sleeper=lambda s: None)


def _seed(con, fonds, periode, rd, cusip, ticker, aktion="erhoeht", anteil=0.1):
    con.execute(
        "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum, cusip,"
        " ticker, name, title_of_class, shares, wert, anteil_portfolio, aktion, aenderung_pct,"
        " positions_gesamt, rotation_pct, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fonds, periode, rd, "2026-08-20", cusip, ticker, ticker + " Corp", "COMMON STOCK",
         1.0, 100.0, anteil, aktion, 0.1, 1, None, "t"))
    con.commit()


# (a) OpenFIGI securityType "ETP" -> is_etf=1, cusip_map.is_etf=1, Score ignoriert den Ticker
def test_a_openfigi_etp_is_etf_score_ignoriert(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()
    resp = [{"data": [{"figi": "BBG000ETP001", "ticker": "XYZ", "exchCode": "US",
                       "securityType": "ETP", "marketSector": "Equity"}]}]

    def fetcher(url: str) -> bytes:
        assert "openfigi" in url
        return json.dumps(resp).encode()

    db = tmp_path / "a.db"
    con = smart_money.connect(db)
    etf: dict[str, bool] = {}
    out = smart_money.cusip_to_tickers(["999999999"], _http(fetcher), cfg, con=con, etf=etf)
    assert out == {"999999999": "XYZ"}
    assert etf == {"999999999": True}
    assert con.execute("SELECT is_etf FROM cusip_map WHERE cusip='999999999'"
                       ).fetchone()[0] == 1
    # ETF-Position landet in der DB (Transparenz), wird aber nicht gescored
    _seed(con, "F", "2026-Q2", "2026-06-30", "999999999", "XYZ", aktion="neu")
    con.close()
    s = smart_money.smart_money_score("XYZ", cfg, db_path=db)
    assert s["score"] == 1 and s["fonds_kaeufer"] == [] and s["fonds_verkaeufer"] == []
    assert s["letzte_aktion"] == ""
    assert s["filing_datum"] == "2026-08-20"        # bleibt echt (Transparenz)


# (b) Common-Stock-Eintrag -> is_etf=0, normale Wertung; ADR/REIT NICHT als ETF (live verifiziert)
def test_b_common_stock_is_etf_0_normal_score(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg()
    resp = [
        {"data": [{"figi": "BBG000AAA001", "ticker": "AAA", "exchCode": "US",
                   "securityType": "Common Stock", "marketSector": "Equity"}]},
        {"data": [{"figi": "BBG000TSM001", "ticker": "TSM", "exchCode": "US",
                   "securityType": "ADR", "marketSector": "Equity", "securityType2": "Depositary Receipt"}]},
        {"data": [{"figi": "BBG000AMT001", "ticker": "AMT", "exchCode": "US",
                   "securityType": "REIT", "marketSector": "Equity", "securityType2": "REIT"}]},
    ]

    def fetcher(url: str) -> bytes:
        return json.dumps(resp).encode()

    db = tmp_path / "b.db"
    con = smart_money.connect(db)
    etf: dict[str, bool] = {}
    out = smart_money.cusip_to_tickers(["111111111", "874039100", "03027X100"],
                                       _http(fetcher), cfg, con=con, etf=etf)
    assert out == {"111111111": "AAA", "874039100": "TSM", "03027X100": "AMT"}
    assert etf == {"111111111": False, "874039100": False, "03027X100": False}  # ADR/REIT kein ETF
    assert con.execute("SELECT is_etf FROM cusip_map WHERE cusip='111111111'"
                       ).fetchone()[0] == 0
    _seed(con, "F", "2026-Q2", "2026-06-30", "111111111", "AAA", aktion="erhoeht", anteil=0.1)
    con.close()
    s = smart_money.smart_money_score("AAA", cfg, db_path=db)
    assert s["score"] == 3 and s["fonds_kaeufer"] == ["F"]   # Anteil 10 % >= 5 % bei Gewicht 1,0


# (c) Config-Liste etf_ticker greift ohne OpenFIGI-Anfrage
def test_c_config_etf_ticker_ohne_openfigi(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    cfg = _cfg(cusip_map={"111111111": "SPY"}, cusip_map_url="")
    calls = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        raise AssertionError(f"OpenFIGI trotz statischem Mapping angefragt: {url}")

    db = tmp_path / "c.db"
    con = smart_money.connect(db)
    etf: dict[str, bool] = {}
    out = smart_money.cusip_to_tickers(["111111111"], _http(fetcher), cfg, con=con, etf=etf)
    assert out == {"111111111": "SPY"}
    assert etf == {"111111111": True}                 # SPY steht in etf_ticker
    assert calls == []                                 # kein OpenFIGI-Call
    assert smart_money.is_etf_ticker(con, "SPY", cfg) is True
    assert smart_money.is_etf_ticker(con, "AAA", cfg) is False
    _seed(con, "F", "2026-Q2", "2026-06-30", "111111111", "SPY", aktion="neu")
    con.close()
    s = smart_money.smart_money_score("SPY", cfg, db_path=db)
    assert s["score"] == 1 and s["fonds_kaeufer"] == []


# (d) overlap-CSV: ETF-Zeile mit etf=ja, Score 1, neutral; Einzeltitel unveraendert
def test_d_overlap_csv_etf_flag_score1_neutral(tmp_path):
    cfg = _cfg()
    db = tmp_path / "d.db"
    con = smart_money.connect(db)
    _seed(con, "F", "2026-Q2", "2026-06-30", "111111111", "SPY", aktion="neu")
    _seed(con, "F", "2026-Q2", "2026-06-30", "222222222", "AAA", aktion="erhoeht", anteil=0.1)
    con.close()
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nSPY,Kandidat,70,0.4\nAAA,Kandidat,70,0.4\n",
                       encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nSPY\nAAA\n", encoding="utf-8")
    out_csv = tmp_path / "overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db,
                               out_path=str(out_csv), force=True)
    cols = list(rows[0].keys())
    assert cols[-1] == "rationale_quelle"             # letzte Spalte
    assert cols[-2] == "etf"                          # davor
    by_t = {r["ticker"]: r for r in rows}
    assert by_t["SPY"]["etf"] == "ja"
    assert by_t["SPY"]["smart_money_score"] == 1
    assert by_t["SPY"]["smart_money_urteil"] == "neutral"     # nie Divergenz
    assert by_t["SPY"]["fonds_kaeufer"] == ""
    assert by_t["AAA"]["etf"] == ""
    assert by_t["AAA"]["smart_money_score"] == 3               # Einzeltitel unveraendert
    assert by_t["AAA"]["smart_money_urteil"] == "Bestaetigung"
    # ETF-Position bleibt in der DB gespeichert (Transparenz)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM smart_money_positions WHERE ticker='SPY'"
                       ).fetchone()[0] == 1
    con.close()
    # CSV-Header endet mit etf,rationale_quelle (v49 4b/5)
    header = out_csv.read_text(encoding="utf-8").splitlines()[0]
    assert header.endswith(",etf,rationale_quelle")


# (f) alte DB: cusip_map ohne is_etf-Spalte -> connect() migriert additiv per ALTER
def test_f_alte_cusip_map_wird_additiv_migriert(tmp_path):
    db = tmp_path / "alt.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE cusip_map (cusip TEXT PRIMARY KEY, ticker TEXT NOT NULL,"
                " quelle TEXT, geladen_am TEXT NOT NULL)")
    raw.execute("INSERT INTO cusip_map VALUES ('111111111','AAA','manuell','t')")
    raw.commit()
    raw.close()
    con = smart_money.connect(db)          # ALTER TABLE cusip_map ADD COLUMN is_etf ...
    cols = {r[1] for r in con.execute("PRAGMA table_info(cusip_map)")}
    assert "is_etf" in cols
    # Altbestand bleibt, Default 0
    assert con.execute("SELECT ticker, is_etf FROM cusip_map WHERE cusip='111111111'"
                       ).fetchone() == ("AAA", 0)
    con.close()
