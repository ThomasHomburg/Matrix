"""Unit-Tests fuer das DCF-Modul (valuation/dcf.py, Build-Schritt 6).

Handgerechnetes Beispiel: FCF 100 (normalisiert, nach SBC), Wachstum 5 %,
Diskontsatz 10 %, Terminal Growth 2,5 %, 10 Aktien.

Erwartung (per Hand gerechnet, mit Fade 5 % -> 2,5 % in Jahren 6-10):
- Wachstumsraten: [5,5,5,5,5, 4.5, 4, 3.5, 3, 2.5] %
- FCF_10 = 100 * 1.05^5 * 1.045 * 1.04 * 1.035 * 1.03 * 1.025
- Barwert FCFs = 770.3924; Barwert Terminal = 798.6074
- Innerer Wert (EV) = 1569.00; Wert je Aktie = 156.90
- Kurs = Marktkap. 1000 / 10 Aktien = 100 -> MoS (Base) = 1 - 100/156.90 = 36.27 %
"""

import math

import pytest

from data.loader import Metric, TickerMetrics
from valuation.dcf import compute_dcf, is_buy_candidate

DCF_CFG = {"dcf": {
    "discount_rate": 0.10,
    "terminal_growth": 0.025,
    "base_growth_cap": 0.06,
    "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10,
    "fade_to_growth": 0.025,
    "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30,
    "buy_bear_floor": 0.80,
}}


def _ticker(fcf_sbc: float = 100.0, mcap: float = 1000.0, shares: float = 10.0,
            cagr: float = 0.05) -> TickerMetrics:
    """Synthetischer Ticker: 3 Jahre FCF-SBC, Umsatz mit konstantem CAGR."""
    m = TickerMetrics(ticker="TEST")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    for i, fy in enumerate((2023, 2024, 2025)):
        m.years[fy] = {
            "fcf_sbc": Metric("fcf_sbc", fcf_sbc, "USD", "USD", fy),
            "revenue": Metric("revenue", 1000.0 * (1 + cagr) ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
        }
    return m


def test_dcf_handgerechnet_fcf100_wachstum5_diskont10():
    """Exaktes handgerechnetes Beispiel (CLAUDE.md: Test mit Hand-Beispiel)."""
    res = compute_dcf(_ticker(), DCF_CFG)
    assert res.fcf_normalized == pytest.approx(100.0)
    assert res.fcf_years_used == 3
    assert res.revenue_cagr == pytest.approx(0.05)
    base = res.scenarios["base"]
    # Wachstumsraten: 5 % (J1-5), linear auf 2,5 % (J6-10)
    assert base.growth_rates[:5] == pytest.approx([0.05] * 5)
    assert base.growth_rates[5:] == pytest.approx([0.045, 0.04, 0.035, 0.03, 0.025])
    # Barwerte (handgerechnet)
    assert base.pv_fcfs == pytest.approx(770.3924, abs=1e-3)
    assert base.terminal_value == pytest.approx(798.6074, abs=1e-3)
    assert base.enterprise_value == pytest.approx(1569.00, abs=1e-2)
    assert base.value_per_share == pytest.approx(156.90, abs=1e-2)
    # MoS (Base) = 1 - Kurs / Wert je Aktie; Kurs = 1000 / 10 = 100
    assert res.margin_of_safety == pytest.approx(1 - 100.0 / 156.90, abs=1e-3)


def test_dcf_szenarien_bear_base_bull():
    res = compute_dcf(_ticker(), DCF_CFG)
    bear, base, bull = (res.scenarios["bear"], res.scenarios["base"],
                        res.scenarios["bull"])
    # Bear: 0 % Wachstum -> niedrigster Wert; Bull groesster
    assert bear.value_per_share < base.value_per_share < bull.value_per_share
    # Bear-Wachstum ueberall 0 % bis auf den Fade (startet bei 0)
    assert all(g == 0.0 for g in bear.growth_rates[:5])
    # Base = min(CAGR 5 %, Cap 6 %) = 5 %; Bull = min(5 % x 1,3, 10 %) = 6,5 %
    assert base.growth_rates[0] == pytest.approx(0.05)
    assert bull.growth_rates[0] == pytest.approx(0.065)


def test_dcf_base_cap_begrenzt_cagr():
    # CAGR 20 % -> Base gedeckelt auf 6 %, Bull auf 10 %
    res = compute_dcf(_ticker(cagr=0.20), DCF_CFG)
    assert res.scenarios["base"].growth_rates[0] == pytest.approx(0.06)
    assert res.scenarios["bull"].growth_rates[0] == pytest.approx(0.10)


def test_dcf_nan_ohne_fcf_sbc():
    m = _ticker()
    for fy in m.years:
        m.years[fy]["fcf_sbc"] = Metric("fcf_sbc", float("nan"), "USD", "USD", fy)
    res = compute_dcf(m, DCF_CFG)
    assert math.isnan(res.fcf_normalized)
    assert res.scenarios == {}
    assert math.isnan(res.margin_of_safety)


def test_dcf_nan_ohne_marktkap():
    m = _ticker()
    m.market_cap = None
    res = compute_dcf(m, DCF_CFG)
    assert res.fcf_normalized == pytest.approx(100.0)  # FCF trotzdem bestimmbar
    assert res.scenarios == {}  # aber kein Wert je Aktie ohne Marktkap.
    assert math.isnan(res.margin_of_safety)


def test_is_buy_candidate_regeln():
    """Kandidatenregel = Quality >= 60 (statt Score >= 70) + MoS + Bear."""
    res = compute_dcf(_ticker(), DCF_CFG)   # MoS Base = 36,3 %
    price = 100.0
    # Quality 75, MoS 36 %, Bear-Wert?
    bear_vps = res.scenarios["bear"].value_per_share
    # Bear >= 80 % des Kurses?
    bear_ok = bear_vps >= 0.80 * price
    assert is_buy_candidate(75.0, res, price, DCF_CFG) == bear_ok
    # Quality zu niedrig (59 < 60) -> nie Kaufkandidat, auch bei hohem Score
    assert not is_buy_candidate(59.0, res, price, DCF_CFG)
    # Quality genau an der Schwelle (60) -> erlaubt (andere Bedingungen entscheiden)
    assert is_buy_candidate(60.0, res, price, DCF_CFG) == bear_ok
    # NaN-Werte -> False
    assert not is_buy_candidate(float("nan"), res, price, DCF_CFG)
    assert not is_buy_candidate(75.0, res, float("nan"), DCF_CFG)


def test_pipeline_row_mit_mos_und_buy_flag(aapl_metrics, cfg):
    from scoring.pipeline import evaluate_ticker
    res = evaluate_ticker(aapl_metrics, cfg)
    row = res["row"]
    assert row is not None
    assert "margin_of_safety" in row
    assert "is_buy" in row
    assert res["dcf"].fcf_normalized == pytest.approx(
        (87.079e9 + 97.855e9 + 88.751e9) / 3, rel=1e-3)  # Ø FCF-SBC 2023-2025
    # Flags sind entdoppelt
    flags = row["flags"].split()
    assert len(flags) == len(set(flags))
