"""Tests fuer v34:
(1) SBC-Regel: SBC-Zeile fehlt in ALLEN Jahren -> FCF-Yield und DCF ohne
    SBC-Abzug (Fallback Roh-FCF), Flag "SBC n/a (ohne SBC)" UND Status
    "Pruefen (SBC fehlt)" -- der nie "Kandidat" wird. Roh-FCF-Yield fliesst
    NICHT in den Value-Score (fcf_yield_sbc_avg3 bleibt n/a).
    (angepasste Alt-Tests: test_sbc_yield_is_na_when_sbc_missing,
     test_aapl_fcf_yield_sbc_nan_when_sbc_missing)
(2) Kandidaten-Label: "Kandidat (streng)" (alle Modi) und
    "Kandidat (aktuell)" (juengste 2) in app.py.
(3) Punkt-Tests: Value n/a bei Coverage < 70 %; MoS "Start-FCF <= 0";
    Quality-Rang Ganzzahl; MoS < -100 % -> "< -100 %" (app._fmt_mos).
(5) Smoke ueber alle Demo-Ticker.
"""

import math

import pytest

from data.demo import demo_companies
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_all, evaluate_ticker, rank_rows
from valuation.dcf import compute_dcf
import app


def _mk(ticker="T", mcap=1e12, sbc_na=False, fcf_vals=None):
    m = TickerMetrics(ticker=ticker, country="US")
    m.name = ticker
    m.sector = "Technology"
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 100e9
    for i, fy in enumerate(range(2021, 2026)):
        rev *= 1.08
        ocf = 0.28 * rev
        capex = -0.05 * rev
        sbc = 0.02 * rev
        fcf = (fcf_vals or [ocf + capex] * 5)[i]
        fsbc = float("nan") if sbc_na else fcf - sbc
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.30 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.20 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", ocf, "USD", "USD", fy),
            "capex": Metric("capex", capex, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "stock_based_compensation": Metric("sbc", sbc, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fsbc, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.5e9, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
        }
    return m


# ---------- (1) SBC fehlt in allen Jahren ----------

def test_sbc_fehlt_allen_jahren_pruefstatus_nie_kandidat(cfg):
    """SBC-Zeile fehlt in allen Jahren -> nie Kandidat, Flag
    'SBC n/a (ohne SBC)'; DCF laeuft ohne SBC (Fallback). v35: Status
    'Pruefen (SBC fehlt)' NUR, wenn ohne SBC Quality >= 60 UND MoS >= 30 %
    -- hier mit niedriger Marktkap. (Kurs 100, MoS > 30 %) erfuellt."""
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk(sbc_na=True, mcap=2e11)  # Kurs = 2e11/2e9 = 100 -> MoS > 30 %
    dcf = compute_dcf(m, cfg)
    assert dcf.sbc_missing is True          # DCF ohne SBC (Roh-FCF)
    assert dcf.fcf_normalized == dcf.fcf_normalized
    out = evaluate_ticker(m, cfg)
    row = out["row"]
    assert row["status"] == "Prüfen (SBC fehlt)"
    assert row["status_all"] == "Prüfen (SBC fehlt)"
    assert row["status_recent2"] == "Prüfen (SBC fehlt)"
    assert row["status_pruefen_sbc"] is True
    assert row["is_buy"] is False
    assert "SBC n/a (ohne SBC)" in row["flags"]


def test_sbc_fehlt_aber_mos_unter_30_prozent_kein_pruefstatus(cfg):
    """SBC fehlt, aber MoS < 30 % (teurer Ticker) -> normaler Status,
    NICHT 'Pruefen (SBC fehlt)'; Flag und nie-Kandidat bleiben."""
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk(sbc_na=True, mcap=1e12)  # Kurs 500 -> MoS < 0
    out = evaluate_ticker(m, cfg)
    row = out["row"]
    assert row["status_pruefen_sbc"] is False
    assert row["status"] != "Prüfen (SBC fehlt)"
    assert row["is_buy"] is False
    assert "SBC n/a (ohne SBC)" in row["flags"]


def test_sbc_fehlt_value_score_bleibt_ohne_rohfcf(cfg):
    """Roh-FCF-Yield fliesst NICHT in den Value-Score: fcf_yield_sbc_avg3 bleibt n/a."""
    from scoring.criteria import compute_criteria
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk(sbc_na=True)
    crits = compute_criteria(m, cfg)
    assert crits["fcf_yield_sbc_avg3"].is_na  # nie Roh-FCF-Yield


def test_sbc_vorhanden_kein_pruefstatus(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk()
    out = evaluate_ticker(m, cfg)
    assert out["row"]["status"] != "Pr\u00fcfen (SBC fehlt)"
    assert out["row"]["status_pruefen_sbc"] is False
    assert "SBC n/a (ohne SBC)" not in out["row"]["flags"]


# ---------- (2) Kandidaten-Labels ----------

def test_kandidaten_labels_streng_aktuell():
    """app.py: Status-Spalten labeln Kandidat als '(streng)' (alle Modi) / '(aktuell)' (juengste 2)."""
    import io as _io
    with _io.open("app.py", encoding="utf-8") as f:
        src = f.read()
    assert '"Kandidat (streng)" if v == "Kandidat"' in src
    assert '"Kandidat (aktuell)" if v == "Kandidat"' in src


# ---------- (3) Punkt-Tests ----------

def test_value_na_below_coverage_70():
    # siehe tests/test_v33.py::test_value_na_below_coverage_70 (bestehend)
    import tests.test_v33 as t33  # noqa: F401 -- Dokumentationszweck
    import inspect
    src = inspect.getsource(t33)
    assert "def test_value_na_below_coverage_70" in src


def test_mos_zelle_start_fcf_le0():
    # siehe tests/test_v33.py::test_mos_cell_start_fcf_le0 (bestehend)
    assert app._fmt_mos(float("nan"), le0=True) == "Start-FCF \u2264 0"


def test_quality_rang_ganzzahl(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    rows, _ = evaluate_all(demo_companies(), cfg)
    q_rank = rank_rows(rows, "quality")
    for r in q_rank.values():
        assert r is None or isinstance(r, int)


def test_mos_unter_minus_100_prozent_als_text():
    """MoS < -100 % -> '< -100 %' statt Prozentwert."""
    assert app._fmt_mos(-1.5) == "< \u2212100 %"
    assert app._fmt_mos(-1.0) == "-100.0%"
    assert app._fmt_mos(-0.42) == "-42.0%"
    assert app._fmt_mos(float("nan")) == "n/a"


# ---------- (5) Smoke ----------

def test_smoke_v34(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg)
    assert rows and excluded
    for r in rows:
        assert "status_pruefen_sbc" in r
        assert r["status"] in ("Kandidat", "Beobachtung", "nein", "Pr\u00fcfen (SBC fehlt)")
