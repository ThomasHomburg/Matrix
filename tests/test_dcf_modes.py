"""Tests fuer v22: DCF-Startwert-Modi (avg3/latest/ttm), Nettoverschuldung
ohne Kundengelder und Equity Value = EV - Nettoverschuldung (valuation/dcf.py).

Handgerechnetes Beispiel (analog test_dcf.py): FCF 100, Wachstum 5 %,
Diskontsatz 10 %, Terminal Growth 2,5 %, 10 Aktien, Nettoverschuldung 200.

EV (ohne ND-Abzug) = 1569.00 (handgerechnet, siehe test_dcf.py).
Innerer Wert = 1569.00 - 200 = 1369.00; Wert je Aktie = 136.90.
Kurs = Marktkap. 1000 / 10 = 100 -> MoS (Base) = 1 - 100/136.90 = 26.95 %.
"""

import math

import pytest

from data.loader import Metric, TickerMetrics
from data.ttm import TtmResult
from valuation.dcf import compute_dcf, is_buy_candidate, sensitivity_mos_base

DCF_CFG = {"dcf": {
    "discount_rate": 0.10,
    "terminal_growth": 0.025,
    "base_growth_cap": 0.06,
    "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10,
    "fade_to_growth": 0.025,
    "fcf_avg_years": 3,
    "fcf_mode": "auto",
    "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30,
    "buy_bear_floor": 0.80,
}}


def _ticker(fcf_sbc_by_fy=None, mcap: float = 1000.0, shares: float = 10.0,
            cagr: float = 0.05, net_debt: float = float("nan"),
            total_debt: float = float("nan"), cash: float = float("nan"),
            customer_deposits: float = float("nan")) -> TickerMetrics:
    """Synthetischer Ticker: FCF-SBC je FY, Umsatz mit konstantem CAGR."""
    if fcf_sbc_by_fy is None:
        fcf_sbc_by_fy = {2023: 100.0, 2024: 100.0, 2025: 100.0}
    m = TickerMetrics(ticker="TEST")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    revs = sorted(fcf_sbc_by_fy)
    rev0 = 1000.0
    for i, fy in enumerate(revs):
        y = {
            "fcf_sbc": Metric("fcf_sbc", fcf_sbc_by_fy[fy], "USD", "USD", fy),
            "revenue": Metric("revenue", rev0 * (1 + cagr) ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
        }
        if net_debt == net_debt:
            y["net_debt"] = Metric("net_debt", net_debt, "USD", "USD", fy)
        if total_debt == total_debt:
            y["_total_debt"] = Metric("_total_debt", total_debt, "USD", "USD", fy)
        if cash == cash:
            y["_cash"] = Metric("_cash", cash, "USD", "USD", fy)
        if customer_deposits == customer_deposits:
            y["customer_deposits"] = Metric("customer_deposits",
                                            customer_deposits, "USD", "USD", fy)
        m.years[fy] = y
    return m


# --- Nettoverschuldung / Equity Value -----------------------------------------

def test_dcf_equity_value_subtracts_net_debt():
    """Innerer Wert = EV - Nettoverschuldung; Wert je Aktie = (EV-ND)/Aktien."""
    res = compute_dcf(_ticker(net_debt=200.0), DCF_CFG)
    base = res.scenarios["base"]
    assert base.enterprise_value == pytest.approx(1569.00, abs=1e-2)
    # Innerer Wert = 1569.00 - 200 = 1369.00 -> Wert je Aktie = 136.90
    assert base.value_per_share == pytest.approx(136.90, abs=1e-2)
    assert res.net_debt == pytest.approx(200.0)
    assert "FY2025" in res.net_debt_source
    # MoS = 1 - 100/136.90
    assert res.margin_of_safety == pytest.approx(1 - 100.0 / 136.90, abs=1e-3)


def test_dcf_net_debt_ex_customer_deposits():
    """Kundengelder werden zur Nettoverschuldung hinzuaddiert (PYPL-Situation)."""
    res = compute_dcf(_ticker(net_debt=-1000.0, customer_deposits=3000.0), DCF_CFG)
    # Nettoverschuldung ohne Kundengelder = -1000 + 3000 = 2000
    assert res.net_debt == pytest.approx(2000.0)
    assert res.customer_deposits == pytest.approx(3000.0)
    assert "Kundengelder" in res.net_debt_source
    base = res.scenarios["base"]
    assert base.value_per_share == pytest.approx((1569.00 - 2000.0) / 10.0, abs=1e-2)


def test_dcf_net_debt_fallback_total_debt_minus_cash():
    """Kein gemeldetes net_debt -> total_debt - cash (+ Kundengelder)."""
    res = compute_dcf(_ticker(total_debt=500.0, cash=100.0,
                              customer_deposits=50.0), DCF_CFG)
    assert res.net_debt == pytest.approx(500.0 - 100.0 + 50.0)
    assert "total_debt - cash" in res.net_debt_source


def test_dcf_negative_net_debt_increases_value():
    """Net Cash (negative Nettoverschuldung) erhoeht den inneren Wert."""
    res = compute_dcf(_ticker(net_debt=-300.0), DCF_CFG)
    base = res.scenarios["base"]
    assert base.value_per_share == pytest.approx((1569.00 + 300.0) / 10.0, abs=1e-2)


def test_dcf_without_net_debt_keeps_ev_behaviour():
    """Ohne Bilanzdaten: Wert je Aktie = EV / Aktien (rueckwaertskompatibel)."""
    res = compute_dcf(_ticker(), DCF_CFG)
    base = res.scenarios["base"]
    assert math.isnan(res.net_debt)
    assert res.net_debt_source == "n/a"
    assert base.value_per_share == pytest.approx(156.90, abs=1e-2)


def test_dcf_net_debt_from_ttm_when_basis_ttm():
    """TTM-Basis: Nettoverschuldung aus dem letzten Quartal (Stichtag)."""
    m = _ticker(net_debt=200.0, customer_deposits=10.0)
    m.fiscal_ends = {2025: "2025-12-31", 2024: "2024-12-31", 2023: "2023-12-31"}
    ttm = TtmResult(
        ttm={
            "fcf_sbc": Metric("fcf_sbc", 120.0, "USD", "USD", 2026),
            "net_debt": Metric("net_debt", 400.0, "USD", "USD", 2026),
            "customer_deposits": Metric("customer_deposits", 25.0, "USD", "USD", 2026),
        },
        quarter_end="2026-06-30", base_label="TTM bis 2026-06",
        plausible=True,
    )
    m.ttm = ttm
    m.ttm_plausible = True
    m.ttm_fallback = False
    res = compute_dcf(m, DCF_CFG)
    assert res.fcf_basis == "ttm"
    # Nettoverschuldung aus TTM: 400 + 25 Kundengelder = 425
    assert res.net_debt == pytest.approx(425.0)
    assert "TTM" in res.net_debt_source


# --- Startwert-Modi ------------------------------------------------------------

def test_fcf_mode_avg3_uses_last_3_years():
    m = _ticker({2022: 50.0, 2023: 80.0, 2024: 100.0, 2025: 200.0})
    res = compute_dcf(m, DCF_CFG, fcf_mode="avg3")
    assert res.fcf_normalized == pytest.approx((80.0 + 100.0 + 200.0) / 3)
    assert res.fcf_basis == "annual"
    assert res.fcf_mode == "avg3"


def test_fcf_mode_latest_uses_newest_year():
    m = _ticker({2022: 50.0, 2023: 80.0, 2024: 100.0, 2025: 200.0})
    res = compute_dcf(m, DCF_CFG, fcf_mode="latest")
    assert res.fcf_normalized == pytest.approx(200.0)
    assert res.fcf_years_used == 1
    assert res.fcf_sources[0][0] == "FY2025"


def test_fcf_mode_ttm_uses_ttm_when_plausible():
    m = _ticker({2023: 80.0, 2024: 100.0, 2025: 110.0})
    # TTM endet 2026-06-30 -> Fenster startet 2025-10-01; FY2025 endet
    # 2025-12-31 (ueberlappt) -> verwendet: TTM + FY2024 + FY2023.
    m.fiscal_ends = {2025: "2025-12-31", 2024: "2024-12-31", 2023: "2023-12-31"}
    ttm = TtmResult(
        ttm={"fcf_sbc": Metric("fcf_sbc", 150.0, "USD", "USD", 2026)},
        quarter_end="2026-06-30", plausible=True,
    )
    m.ttm = ttm
    m.ttm_plausible = True
    m.ttm_fallback = False
    res = compute_dcf(m, DCF_CFG, fcf_mode="ttm")
    assert res.fcf_basis == "ttm"
    # Ø = (150 + 100 + 80) / 3 (FY2025 ueberlappt das TTM-Fenster!)
    assert res.fcf_normalized == pytest.approx((150.0 + 100.0 + 80.0) / 3)
    names = [n for n, _, _ in res.fcf_sources]
    assert names == ["TTM", "FY2024", "FY2023"]


def test_fcf_mode_ttm_falls_back_to_avg3_when_unplausible():
    m = _ticker({2023: 80.0, 2024: 100.0, 2025: 110.0})
    m.fiscal_ends = {2025: "2025-12-31", 2024: "2024-12-31", 2023: "2023-12-31"}
    ttm = TtmResult(
        ttm={"fcf_sbc": Metric("fcf_sbc", 150.0, "USD", "USD", 2026)},
        quarter_end="2026-06-30", plausible=False, fall_back_to_annual=True,
    )
    m.ttm = ttm
    m.ttm_plausible = False
    m.ttm_fallback = True
    res = compute_dcf(m, DCF_CFG, fcf_mode="ttm")
    assert res.fcf_basis == "annual"
    assert res.fcf_normalized == pytest.approx((80.0 + 100.0 + 110.0) / 3)


def test_fcf_mode_auto_prefers_ttm():
    m = _ticker({2023: 80.0, 2024: 100.0, 2025: 110.0})
    m.fiscal_ends = {2025: "2025-12-31", 2024: "2024-12-31", 2023: "2023-12-31"}
    ttm = TtmResult(
        ttm={"fcf_sbc": Metric("fcf_sbc", 150.0, "USD", "USD", 2026)},
        quarter_end="2026-06-30", plausible=True,
    )
    m.ttm = ttm
    m.ttm_plausible = True
    m.ttm_fallback = False
    res = compute_dcf(m, DCF_CFG)  # Default: auto
    assert res.fcf_basis == "ttm"
    assert res.fcf_mode == "auto"
    assert res.fcf_normalized == pytest.approx((150.0 + 100.0 + 80.0) / 3)


def test_fcf_mode_default_from_config():
    """config.yaml dcf.fcf_mode steuert den Default (hier: 'latest')."""
    cfg = {"dcf": dict(DCF_CFG["dcf"], fcf_mode="latest")}
    m = _ticker({2023: 80.0, 2024: 100.0, 2025: 200.0})
    res = compute_dcf(m, cfg)
    assert res.fcf_normalized == pytest.approx(200.0)


def test_fcf_mode_invalid_falls_back_to_auto():
    m = _ticker()
    res = compute_dcf(m, DCF_CFG, fcf_mode="unsinn")
    assert res.fcf_mode == "auto"
    assert res.fcf_normalized == pytest.approx(100.0)


def test_modes_give_different_mos():
    """MoS (Base) unterscheidet sich je Startwert-Modus."""
    m = _ticker({2023: 80.0, 2024: 100.0, 2025: 200.0})
    mos_avg3 = compute_dcf(m, DCF_CFG, fcf_mode="avg3").margin_of_safety
    mos_latest = compute_dcf(m, DCF_CFG, fcf_mode="latest").margin_of_safety
    assert mos_avg3 != pytest.approx(mos_latest)
    # hoeherer Start-FCF (latest 200 > avg3 126.7) -> hoeherer Wert -> hoehere MoS
    assert mos_latest > mos_avg3


# --- Sensitivitaet beruecksichtigt Nettoverschuldung ---------------------------

def test_sensitivity_center_matches_dcf_base_with_net_debt():
    """Mit Nettoverschuldung: Sensitivitaets-Mitte == compute_dcf Base (exakt)."""
    m = _ticker(net_debt=200.0, customer_deposits=25.0)
    dcf = compute_dcf(m, DCF_CFG)
    sens = sensitivity_mos_base(m, DCF_CFG)
    center = sens["mos"][(sens["mos"]["discount_rate"] == 0.10)
                         & (sens["mos"]["terminal_growth"] == 0.025)].iloc[0]
    assert center["value_per_share"] == pytest.approx(
        dcf.scenarios["base"].value_per_share, rel=1e-9)
    assert center["mos"] == pytest.approx(dcf.margin_of_safety, rel=1e-9)


# --- Kandidatenregel mit Nettoverschuldung --------------------------------------

def test_is_buy_candidate_with_net_debt():
    """Kandidatenregel (Quality/MoS/Bear) funktioniert mit ND-abzug.

    ND 200 senkt den Wert je Aktie -> Wert je Aktie = 136,90,
    MoS Base = 1 - 100/136,90 = 26,95 % (< 30 %) -> kein Kandidat.
    Ohne ND waere MoS = 36,3 % -> Kandidat. Der ND-Abzug kippt die Regel.
    """
    res = compute_dcf(_ticker(net_debt=200.0), DCF_CFG)
    price = 100.0
    assert res.margin_of_safety == pytest.approx(1 - 100.0 / 136.90, abs=1e-3)
    assert not is_buy_candidate(75.0, res, price, DCF_CFG)   # MoS < 30 %
    assert not is_buy_candidate(59.0, res, price, DCF_CFG)   # Quality < 60
    # Ohne ND: MoS 36,3 % >= 30 %, Bear >= 80 % Kurs -> Kandidat
    res_ohne_nd = compute_dcf(_ticker(), DCF_CFG)
    assert is_buy_candidate(75.0, res_ohne_nd, price, DCF_CFG) is True
    # Net Cash erhoeht den Wert -> Kandidat trotz hoeherem Kurs
    res_net_cash = compute_dcf(_ticker(net_debt=-300.0), DCF_CFG)
    assert res_net_cash.margin_of_safety > res_ohne_nd.margin_of_safety
    assert is_buy_candidate(75.0, res_net_cash, price, DCF_CFG) is True
