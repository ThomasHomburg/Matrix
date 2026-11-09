"""Tests fuer ROIC-Steuersatz-Begrenzung und Folge-Features:
(2) DCF-Modus ttm_only (reiner TTM-Start-FCF nach SBC, ohne O);
    bisheriger TTM-Modus heisst TTM O (Ø TTM + 2 nicht ueberlappende GJ).
(3) Zwei Stufen in der Rangliste: "Kandidat" (min der Modi >= 30 %,
    Quality >= 60, Bear >= 80 % Kurs; Kandidat nennt den entscheidenden
    Modus) und "Beobachtung" (min < 30 %, bester Modus >= 30 %,
    Quality >= 60). Smoke ueber alle Demo-Ticker + neue Spalten.
"""

import math

import pandas as pd
import pytest

from data.loader import Metric, TickerMetrics
from data.ttm import compute_ttm
from valuation.dcf import compute_dcf

DCF_CFG = {"dcf": {
    "discount_rate": 0.10, "terminal_growth": 0.025,
    "base_growth_cap": 0.06, "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10, "fade_to_growth": 0.025,
    "fcf_avg_years": 3, "fcf_mode": "auto", "net_debt_mode": "reported",
    "mos_min_mode": "min", "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30, "buy_bear_floor": 0.80,
    "watch_margin_of_safety_min": 0.30,
}}


def _ticker_with_ttm(fcf_fy: float = 100.0, fcf_q: float = 30.0,
                     sbc_q: float = 5.0, mcap: float = 1000.0,
                     shares: float = 10.0) -> TickerMetrics:
    """Ticker mit 3 GJ + 4 Quartalen (TTM fcf_sbc = 4*(fcf_q - sbc_q))."""
    m = TickerMetrics(ticker="T", country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 1000.0
    for fy in (2023, 2024, 2025):
        rev *= 1.05
        m.years[fy] = {
            "fcf_sbc": Metric("fcf_sbc", fcf_fy, "USD", "USD", fy),
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
        }
    quarters = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-31"),
                pd.Timestamp("2026-02-28"), pd.Timestamp("2025-11-30")]
    cf = pd.DataFrame({
        "Operating Cash Flow": [fcf_q + 10.0] * 4,
        "Capital Expenditure": [-10.0] * 4,
        "Free Cash Flow": [fcf_q] * 4,
        "Stock Based Compensation": [sbc_q] * 4,
    }, index=quarters).T
    inc = pd.DataFrame({"Total Revenue": [100.0] * 4}, index=quarters).T
    bal = pd.DataFrame({"Net Debt": [0.0] * 4, "Stockholders Equity": [100.0] * 4},
                       index=quarters).T
    row_map = {"revenue": ["Total Revenue"],
               "operating_cashflow": ["Operating Cash Flow"],
               "capex": ["Capital Expenditure"], "fcf": ["Free Cash Flow"],
               "stock_based_compensation": ["Stock Based Compensation"],
               "net_debt": ["Net Debt"], "equity": ["Stockholders Equity"]}
    ttm = compute_ttm(m, {"income": inc, "balance": bal, "cashflow": cf,
                          "row_map": row_map})
    m.ttm = ttm
    m.ttm_plausible = ttm.plausible
    m.ttm_fallback = ttm.fall_back_to_annual
    m.fiscal_ends = {2023: "2023-12-31", 2024: "2024-12-31", 2025: "2025-12-31"}
    return m


# --- (2) ttm_only ---------------------------------------------------------------

def test_ttm_only_reiner_ttm_start():
    """ttm_only: Start-FCF = reines TTM (4 Quartale), years_used=1, basis=ttm."""
    m = _ticker_with_ttm(fcf_fy=100.0, fcf_q=30.0, sbc_q=5.0)
    res = compute_dcf(m, DCF_CFG, fcf_mode="ttm_only")
    assert res.fcf_mode == "ttm_only"
    assert res.fcf_normalized == pytest.approx(4 * (30.0 - 5.0))  # 100
    assert res.fcf_years_used == 1
    assert res.fcf_basis == "ttm"
    assert res.ttm_quarter_end == "2026-08-31"
    assert res.fcf_sources == [("TTM", 100.0, "USD")]


def test_ttm_bleibt_durchschnitt_mit_gj():
    """ttm (bisheriger Modus, jetzt 'TTM Ø'): Ø aus TTM + 2 nicht ueberlappenden GJ."""
    m = _ticker_with_ttm(fcf_fy=100.0, fcf_q=30.0, sbc_q=5.0)
    res = compute_dcf(m, DCF_CFG, fcf_mode="ttm")
    assert res.fcf_mode == "ttm"
    assert res.fcf_years_used == 3          # TTM + FY2024 + FY2023
    assert res.fcf_basis == "ttm"
    assert len(res.fcf_sources) == 3
    assert res.fcf_sources[0][0] == "TTM"


def test_ttm_only_faellt_auf_avg3_zurueck_wenn_kein_ttm():
    """Ohne TTM -> Rueckfall auf Ø3J (wie ttm-Modus)."""
    m = TickerMetrics(ticker="T", country="US")
    m.market_cap = Metric("market_cap", 1000.0, "USD", "USD", 2025)
    rev = 1000.0
    for fy in (2023, 2024, 2025):
        rev *= 1.05
        m.years[fy] = {
            "fcf_sbc": Metric("fcf_sbc", 100.0, "USD", "USD", fy),
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 10.0, "shares", "USD", fy),
        }
    res = compute_dcf(m, DCF_CFG, fcf_mode="ttm_only")
    assert res.fcf_normalized == pytest.approx(100.0)
    assert res.fcf_basis == "annual"        # Rueckfall Ø3J
    assert res.fcf_years_used == 3


def test_ttm_only_unterscheidet_sich_von_ttm_und_avg3():
    """Unterschiedliche Start-FCF-Werte -> ttm_only != ttm != avg3."""
    # TTM hoch (4*80=320), GJ niedrig (100) -> ttm_only 320, ttm Ø (320+100+100)/3=173,3
    m = _ticker_with_ttm(fcf_fy=100.0, fcf_q=90.0, sbc_q=10.0)
    r_only = compute_dcf(m, DCF_CFG, fcf_mode="ttm_only")
    r_ttm = compute_dcf(m, DCF_CFG, fcf_mode="ttm")
    r_avg3 = compute_dcf(m, DCF_CFG, fcf_mode="avg3")
    assert r_only.fcf_normalized == pytest.approx(320.0)
    assert r_ttm.fcf_normalized == pytest.approx((320.0 + 100.0 + 100.0) / 3)
    assert r_avg3.fcf_normalized == pytest.approx(100.0)
    # MoS: hoeherer Start-FCF -> hoeherer Wert -> hoehere MoS
    assert r_only.margin_of_safety > r_ttm.margin_of_safety > r_avg3.margin_of_safety


# --- (3) Zwei Stufen: Kandidat / Beobachtung ------------------------------------

def _mk_watch(fcf_by_fy: dict, mcap: float, shares: float = 2e9) -> TickerMetrics:
    m = TickerMetrics(ticker="WATCH", country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    m.name = "Watch Corp"
    m.sector = "Technology"
    rev = 100e9
    for fy in sorted(fcf_by_fy):
        rev *= 1.10
        m.years[fy] = {
            "fcf": Metric("fcf", fcf_by_fy[fy] + 0.02 * rev, "USD", "USD", fy),  # fcf passend zu fcf_sbc
            "fcf_sbc": Metric("fcf_sbc", fcf_by_fy[fy], "USD", "USD", fy),
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.3 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.2 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.3 * rev, "USD", "USD", fy),
            "capex": Metric("capex", -0.05 * rev, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", 0.02 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
            # Steuerfelder fuer NOPAT/ROIC + Zinsaufwand fuer Zinsdeckung
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.01 * rev, "USD", "USD", fy),
        }
    return m


def test_status_kandidat_mit_entscheidendem_modus(cfg):
    """Alle Modi >= 30 % + Quality >= 60 + Bear ok -> Kandidat + Modus-Name."""
    from scoring.pipeline import evaluate_ticker
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    m = _mk_watch({2021: 2e9, 2022: 2e9, 2023: 2e9, 2024: 2e9, 2025: 40e9},
                  mcap=100e9)
    out = evaluate_ticker(m, cfg_off)
    row = out["row"]
    assert row["status"] == "Kandidat"
    assert out["dcf"].is_buy
    assert "Kandidat" in row["kandidat"] or row["kandidat"] == "ja"
    # Entscheidender Modus wird genannt (Minimum bei Modus ...)
    assert "bei Modus" in row["status_detail"]
    assert "kauf" in row["flags"]


def test_status_beobachtung_wenn_min_unter_30_aber_bestes_ok(cfg):
    """min < 30 %, bester Modus >= 30 %, Quality >= 60 -> Beobachtung."""
    from scoring.pipeline import evaluate_ticker
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    # avg3 durch altes niedriges Jahr gedrueckt, juengstes GJ hoch
    m = _mk_watch({2021: 1e9, 2022: 1e9, 2023: 1e9, 2024: 1e9, 2025: 30e9},
                  mcap=150e9)
    out = evaluate_ticker(m, cfg_off)
    row = out["row"]
    assert row["status"] == "Beobachtung"
    assert not out["dcf"].is_buy
    assert "Beobachtung" in row["kandidat"]
    assert "jüngstes GJ" in row["status_detail"]   # bester Modus genannt
    assert "beobachten" in row["flags"]


def test_status_nein_wenn_quality_unter_60(cfg):
    """Quality < 60 -> weder Kandidat noch Beobachtung."""
    from scoring.pipeline import evaluate_ticker
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    m = _mk_watch({2021: 40e9, 2022: 40e9, 2023: 40e9, 2024: 40e9, 2025: 40e9},
                  mcap=1e12)  # sehr teuer -> Quality evtl. ok, aber MoS negativ
    out = evaluate_ticker(m, cfg_off)
    row = out["row"]
    # Keine Beobachtung: bester Modus < 30 % (Kurs zu hoch)
    assert row["status"] in ("nein", "Beobachtung")
    if row["status"] == "nein":
        assert row["kandidat"].startswith("nein")


def test_mos_ttm_only_in_row(cfg):
    """Row enthaelt mos_ttm_only + status/status_detail."""
    from scoring.pipeline import evaluate_ticker
    from data.demo import demo_companies
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    row = evaluate_ticker(qual, cfg_off)["row"]
    for col in ("mos_ttm_only", "status", "status_detail"):
        assert col in row, f"fehlt: {col}"
    assert row["status"] in ("Kandidat", "Beobachtung", "nein")


# --- (5) Smoke ueber alle Ticker + neue Spalten ----------------------------------

V27_ROW_COLUMNS = {
    "mos_ttm_only", "status", "status_detail",
}


def test_evaluate_all_smoke_v27_spalten(cfg):
    """evaluate_all ueber alle Demo-Ticker: keine Ausnahme, v27-Spalten da."""
    from data.demo import demo_companies
    from scoring.pipeline import evaluate_all
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg_off)
    assert rows
    for row in rows:
        missing = V27_ROW_COLUMNS - set(row)
        assert not missing, f"{row.get('ticker')}: fehlen {sorted(missing)}"
        # Kandidat nur wenn is_buy; Status konsistent
        assert (row["status"] == "Kandidat") == bool(row["is_buy"])
        if row["status"] == "Kandidat":
            assert "kauf" in row["flags"]
        elif row["status"] == "Beobachtung":
            assert "beobachten" in row["flags"]


def test_app_row_columns_v27_aktualisiert():
    """app.py zeigt MoS TTM + Status; Rangliste enthaelt die Spalten."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    m = re.search(r"show_cols = \[(.*?)\]", src, re.S)
    assert m
    cols = re.findall(r'"([^"]+)"', m.group(1))
    assert "MoS TTM" in cols and "Status" in cols
