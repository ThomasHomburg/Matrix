"""v41 Tests (Fixtures/Mocks -- kein Netz, kein Volllauf):

(0) Export: kurs/(1-MoS) ergibt iv_base (Toleranz 0,5 %) -- Demo-AAPL ueber
    evaluate_ticker (echte Pipeline, keine Mocks noetig).
(a) live_row: Kurs = Kurs bei calc_date -> status_live = Screener-Status.
(b) live_row: Kurs -20 % -> MoS und Status wechseln wie erwartet
    (Beobachtung -> Kandidat, MoS steigt).
(c) live_row: Kurs = kaufzone_kurs -> mos_live_min = 30,0 %.
(d) decisions: Brier-Score + Ereignis-Logik an einem Beispiel (tmp-SQLite);
    bei n < 30 "unkalibriert, n=...".
(e) timing: timing_status fuer drei synthetische Kursreihen
    (steigend/positiv, fallend/negativ, gemischt/neutral).
"""

import math
import sqlite3
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from config import load_config
from scoring.pipeline import evaluate_ticker
from data.demo import demo_companies
import live_update
import timing
import decisions


def _cfg():
    cfg = load_config()
    sec = dict(cfg.get("sec", {}))
    sec["enabled"] = False
    cfg["sec"] = sec
    return cfg


def _row(**kw) -> dict:
    """Synthetische ranking-Row (v41 Export-Spalten als Strings, wie aus CSV)."""
    base = {
        "ticker": "TEST", "status": "Beobachtung", "quality": "70",
        "kurs": "100", "kurs_datum": "2026-01-02", "calc_date": "2026-01-02",
        "iv_bear": "110", "iv_base": "130", "iv_bull": "160",
        "iv_avg3": "150", "iv_latest": "130", "iv_ttm": "130", "iv_ttm_only": "130",
        "iv_base_10pct": "120", "naechster_earnings": "",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# (0) Export: kurs/(1-MoS) = iv_base
# --------------------------------------------------------------------------

def test_export_kurs_ueber_mos_ergibt_iv_base():
    cfg = _cfg()
    aapl = next(m for m in demo_companies() if m.ticker == "AAPL")
    out = evaluate_ticker(aapl, cfg)
    row = out["row"]
    assert row is not None
    mos = row["margin_of_safety"]
    kurs = row["kurs"]
    iv_base = row["iv_base"]
    assert mos == mos and kurs == kurs and iv_base == iv_base and iv_base > 0
    # MoS = 1 - kurs/iv_base => kurs/(1-MoS) = iv_base (Toleranz 0,5 %)
    rekon = kurs / (1.0 - mos)
    assert abs(rekon / iv_base - 1.0) <= 0.005
    # Export-Felder vorhanden
    for k in ("kurs", "kurs_datum", "calc_date", "iv_bear", "iv_base", "iv_bull",
              "iv_avg3", "iv_latest", "iv_ttm", "iv_ttm_only", "iv_base_10pct",
              "naechster_earnings"):
        assert k in row


# --------------------------------------------------------------------------
# (a) Kurs = Kurs bei calc_date -> status_live = Screener-Status
# --------------------------------------------------------------------------

def test_live_kurs_gleich_calc_date_status_gleich_screener():
    cfg = _cfg()
    row = _row(status="Kandidat", iv_avg3="200", iv_latest="200", iv_ttm="200",
               iv_ttm_only="200", iv_base="200", iv_bear="170", quality="70", kurs="100")
    r = live_update.live_row(row, kurs=100.0, cfg=cfg, heute=date(2026, 1, 2))
    assert r["status_live"] == "Kandidat" == r["status_screener"]
    assert r["mos_live_min"] == pytest.approx(0.50)


# --------------------------------------------------------------------------
# (b) Kurs -20 % -> MoS und Status wechseln (Beobachtung -> Kandidat)
# --------------------------------------------------------------------------

def test_live_kurs_minus_20_prozent_wechsel():
    cfg = _cfg()
    # calc_date: kurs 100, iv min 130 -> mos min 23,1 % (< 30 %), bester Modus
    # iv_avg3=150 -> 33,3 % -> Beobachtung; Bear 110 >= 80 -> ok
    row = _row(status="Beobachtung", kurs="100")
    r_calc = live_update.live_row(row, kurs=100.0, cfg=cfg, heute=date(2026, 1, 2))
    assert r_calc["status_live"] == "Beobachtung"
    assert r_calc["mos_live_min"] == pytest.approx(1.0 - 100.0 / 130.0)
    # -20 %: kurs 80 -> mos min = 1 - 80/130 = 38,5 % >= 30 %, Bear ok -> Kandidat
    r = live_update.live_row(row, kurs=80.0, cfg=cfg, heute=date(2026, 1, 2))
    assert r["status_live"] == "Kandidat"
    assert r["mos_live_min"] == pytest.approx(1.0 - 80.0 / 130.0)
    assert r["mos_live_min"] > r_calc["mos_live_min"]


# --------------------------------------------------------------------------
# (c) Kurs = kaufzone_kurs -> mos_live_min = 30,0 %
# --------------------------------------------------------------------------

def test_live_kurs_gleich_kaufzone_mos_min_30_prozent():
    cfg = _cfg()
    row = _row(iv_bear="500", iv_avg3="130", iv_latest="130", iv_ttm="130",
               iv_ttm_only="130", iv_base="130", quality="70", kurs="100")
    r100 = live_update.live_row(row, kurs=100.0, cfg=cfg, heute=date(2026, 1, 2))
    kz = r100["kaufzone_kurs"]
    assert kz == kz and kz > 0
    # Bear nicht bindend (500 >= 0,8*kz) -> kaufzone = min(iv)*(1-0,30)
    assert kz == pytest.approx(130.0 * 0.70)
    r = live_update.live_row(row, kurs=kz, cfg=cfg, heute=date(2026, 1, 2))
    assert r["mos_live_min"] == pytest.approx(0.30, abs=1e-9)
    assert r["status_live"] == "Kandidat"
    assert r["distanz_pct"] == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------
# (d) Brier-Score + Ereignis-Logik (decisions.py, tmp-SQLite)
# --------------------------------------------------------------------------

def test_erwartungswert_pct_szenariogewichte():
    # kurs 100; iv >= 110: base (120) und bull (150) -> p = 0.50+0.25 = 0.75
    ew = decisions.erwartungswert_pct(90.0, 120.0, 150.0, 100.0,
                                      {"bear": 0.25, "base": 0.50, "bull": 0.25})
    assert ew == pytest.approx(75.0)
    # nur bull -> p = 0.25
    ew2 = decisions.erwartungswert_pct(90.0, 100.0, 150.0, 100.0,
                                       {"bear": 0.25, "base": 0.50, "bull": 0.25})
    assert ew2 == pytest.approx(25.0)


def test_review_brier_und_ereignis_logik(tmp_path, capsys):
    db = tmp_path / "t.db"
    con = decisions.init_db(db)
    # Entscheidung vor 400 Tagen: kurs 100, p = 0.75 (s. o.), Horizont 12
    datum = (date.today() - timedelta(days=400)).isoformat()
    con.execute(
        """INSERT INTO decisions
           (ticker, datum, aktion, kurs, status_screener, iv_base, iv_bear, iv_bull,
            gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate, thesis, break_condition)
           VALUES ('TEST', ?, 'kauf', 100.0, 'Kandidat', 120.0, 90.0, 150.0,
                   0.25, 0.50, 0.25, 75.0, 12, 'fixture', '')""", (datum,))
    # Kurs nach Ablauf: Horizont 12 M = 360 Tage -> wann = datum + 360 = heute - 40
    # +20 % -> E = 1 (>= +10 %), Brier = (0.75 - 1)^2 = 0.0625
    wann = (date.today() - timedelta(days=400 - 360)).isoformat()
    con.execute("INSERT INTO prices (ticker, datum, kurs, quelle, geladen_am) VALUES ('TEST', ?, 120.0, 'fixture', 'x')",
                (wann,))
    con.commit()
    con.close()
    args = Namespace(db=str(db), ranking=None, heute=None, ticker=None)
    decisions.cmd_review(args)
    out = capsys.readouterr().out
    assert "unkalibriert, n=1" in out  # n < 30 -> keine Kennzahl
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM decisions WHERE ticker='TEST'").fetchone()
    assert r["kurs_nachher"] == pytest.approx(120.0)
    assert r["rendite_pct"] == pytest.approx(20.0)
    assert r["ereignis_e"] == 1                      # 20 % >= +10 %
    assert r["brier"] == pytest.approx((0.75 - 1.0) ** 2)


def test_review_ereignis_negativ(tmp_path):
    db = tmp_path / "t.db"
    con = decisions.init_db(db)
    datum = (date.today() - timedelta(days=400)).isoformat()
    con.execute(
        """INSERT INTO decisions
           (ticker, datum, aktion, kurs, status_screener, iv_base, iv_bear, iv_bull,
            gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate, thesis, break_condition)
           VALUES ('TEST', ?, 'watch', 100.0, 'Beobachtung', 120.0, 90.0, 150.0,
                   0.25, 0.50, 0.25, 25.0, 12, 'fixture', '')""", (datum,))
    wann = (date.today() - timedelta(days=400 - 360)).isoformat()  # datum + 360 Tage
    con.execute("INSERT INTO prices (ticker, datum, kurs, quelle, geladen_am) VALUES ('TEST', ?, 105.0, 'fixture', 'x')",
                (wann,))  # +5 % < +10 % -> E = 0, Brier = (0.25-0)^2 = 0.0625
    con.commit()
    con.close()
    decisions.cmd_review(Namespace(db=str(db), ranking=None, heute=None, ticker=None))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM decisions WHERE ticker='TEST'").fetchone()
    assert r["ereignis_e"] == 0
    assert r["brier"] == pytest.approx(0.25 ** 2)


# --------------------------------------------------------------------------
# (e) timing_status fuer drei synthetische Kursreihen
# --------------------------------------------------------------------------

def _serie(werte: list[float]) -> pd.Series:
    idx = pd.date_range(end=date.today(), periods=len(werte), freq="B")
    return pd.Series(werte, index=idx)


def test_timing_status_drei_serien():
    n = 300
    steigend = _serie([100.0 + i for i in range(n)])          # 100 -> 399 linear
    fallend = _serie([400.0 - i for i in range(n)])            # 400 -> 101 linear
    # gemischt: 285 Tage Anstieg 100->300, dann 15 Tage Einbruch auf 180
    # (Kurs danach unter SMA200, mom_12_1 aber noch positiv -> neutral)
    gem = [100.0 + (200.0 / 284) * i for i in range(285)] + [300.0 - (120.0 / 14) * (i + 1) for i in range(15)]
    gemischt = _serie(gem)
    r_up = timing.timing_kennzahlen(steigend)
    r_dn = timing.timing_kennzahlen(fallend)
    r_mx = timing.timing_kennzahlen(gemischt)
    assert r_up["mom_12_1"] > 0 and r_up["abstand_sma200"] > 0
    assert r_up["timing_status"] == "positiv"
    assert r_dn["mom_12_1"] < 0 and r_dn["abstand_sma200"] < 0
    assert r_dn["timing_status"] == "negativ"
    # gemischt: mom_12_1 > 0 (Anstieg), aber Kurs unter SMA200 (starker Einbruch) -> neutral
    assert r_mx["mom_12_1"] > 0 and r_mx["abstand_sma200"] < 0
    assert r_mx["timing_status"] == "neutral"
    # mom_12_1: t-252 bis t-21 -- bei 300 Tagen: idx 47 (~132) bis idx 279 (~291)
    assert r_mx["mom_12_1"] == pytest.approx(291 / 132 - 1, rel=0.05)


def test_timing_status_einheit():
    assert timing.timing_status(0.05, 0.02) == "positiv"
    assert timing.timing_status(0.05, -0.02) == "neutral"
    assert timing.timing_status(-0.05, 0.02) == "neutral"
    assert timing.timing_status(-0.05, -0.02) == "negativ"
    assert timing.timing_status(float("nan"), float("nan")) == "n/a"


# --------------------------------------------------------------------------
# live_update: Watchlist-Auswahl + Flags
# --------------------------------------------------------------------------

def test_watchlist_tickers_status_plus_extra():
    cfg = _cfg()
    rows = [
        {"ticker": "AAA", "status": "Kandidat"},
        {"ticker": "BBB", "status": "Beobachtung (nicht normalisiert)"},
        {"ticker": "CCC", "status": "nein"},
        {"ticker": "DDD", "status": "Prüfen (Captive Finance)"},
    ]
    wl = live_update.watchlist_tickers(rows, cfg)
    assert "AAA" in wl and "BBB" in wl
    assert "CCC" not in wl and "DDD" not in wl
    assert "IT" in wl and "FDS" in wl  # extra_tickers Default


def test_live_flags_veraltet_und_alarm():
    cfg = _cfg()
    heute = date(2026, 6, 1)
    # calc_date vor 200 Tagen -> wert_veraltet; naechster_earnings in Vergangenheit
    row = _row(calc_date="2025-11-13", kurs="100", naechster_earnings="2026-01-15")
    r = live_update.live_row(row, kurs=80.0, cfg=cfg, heute=heute, vorheriger_kurs=100.0)
    assert r["wert_veraltet"] == 1
    assert r["alarm"] == 1  # -20 % seit calc_date UND -20 % Tagesbewegung
    # frischer Lauf, kleiner Move -> keine Flags
    row2 = _row(calc_date="2026-05-30", kurs="100", naechster_earnings="2026-07-30")
    r2 = live_update.live_row(row2, kurs=101.0, cfg=cfg, heute=heute, vorheriger_kurs=100.5)
    assert r2["wert_veraltet"] == 0
    assert r2["alarm"] == 0


def test_fetch_prices_fehler_stoppt_lauf_nicht():
    def fetcher(t):
        if t == "BAD":
            raise ValueError("kaputt")
        return {"kurs": 10.0, "datum": "2026-01-02", "vorheriger": 9.5}
    prices, errors = live_update.fetch_prices(["OK1", "BAD", "OK2"], batch=2, pause=0.0, fetcher=fetcher)
    assert set(prices) == {"OK1", "OK2"}
    assert [e["ticker"] for e in errors] == ["BAD"]


def test_schema_sql_laeuft(tmp_path):
    con = decisions.init_db(tmp_path / "s.db")
    namen = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prices", "valuations", "watchlist_status", "decisions"} <= namen
    con.close()
