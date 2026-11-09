"""Tests fuer v38:
(1) Negatives Eigenkapital: kein Ausschluss mehr (exclusion.negative_equity:
    false), Flag "ek_negativ"; roe_avg5 n/a in EK<=0-Jahren; roic/roic_latest
    nutzen bei EK<=0 Schulden+EK als Nenner (auch <=0 -> n/a). Test mit
    synthetischem EK-negativ-Ticker + Schalter true stellt altes Verhalten her.
(3) Asset Management -> Sektor-Sonderregel (special); Financial Data & Stock
    Exchanges bleibt Normalmodell.
"""

import pytest

from data.loader import Metric, TickerMetrics
from data.metrics import roe, roic, invested_capital_ek_le0
from scoring.criteria import compute_criteria
from scoring.filters import check_filters
from scoring.pipeline import evaluate_ticker
from scoring.sectors import sector_rule


def _mk_ek_negativ(ticker="EKNEG", equity=-5e9, total_debt=20e9, mcap=100e9) -> TickerMetrics:
    """5 GJ, letztes Jahr EK < 0 (wie MCD/PM/SBUX/ORLY/LOW), sonst solide Daten."""
    m = TickerMetrics(ticker=ticker, country="US")
    m.name = ticker
    m.sector = "Consumer Defensive"
    m.industry = "Restaurants"
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 30e9
    for fy in range(2021, 2026):
        rev *= 1.05
        eq = equity if fy == 2025 else 10e9
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.3 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.2 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.3 * rev, "USD", "USD", fy),
            "capex": Metric("capex", -0.05 * rev, "USD", "USD", fy),
            "fcf": Metric("fcf", 0.25 * rev, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", 0.02 * rev, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", 0.23 * rev, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.01 * rev, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.24 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 1e9, "shares", "USD", fy),
            "equity": Metric("equity", eq, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 15e9, "USD", "USD", fy),
            "_total_debt": Metric("_total_debt", total_debt, "USD", "USD", fy),
            "_cash": Metric("_cash", 5e9, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


def _cfg_off(cfg, neg_equity_excl=False):
    cfg2 = dict(cfg)
    filt = dict(cfg2.get("filters", {}))
    excl = dict(filt.get("exclusion", {}))
    excl["negative_equity"] = neg_equity_excl
    filt["exclusion"] = excl
    cfg2["filters"] = filt
    sec = dict(cfg2.get("sec", {}))
    sec["enabled"] = False
    cfg2["sec"] = sec
    return cfg2


# ---------- (1) negatives Eigenkapital ----------

def test_ek_negativ_kein_ausschluss_flag_und_roe_na(cfg):
    """EK < 0 -> kein Ausschluss, Flag 'ek_negativ', roe_avg5 nutzt nur
    EK>0-Jahre (FY2025 n/a), roic_avg5 rechnet mit Schulden+EK."""
    m = _mk_ek_negativ()
    cfg2 = _cfg_off(cfg)
    assert check_filters(m, cfg2["filters"]) == []  # nicht ausgeschlossen
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row is not None
    assert "ek_negativ" in row["flags"]
    crits = out["criteria"]
    # ROE: FY2025 (EK -5e9) ist n/a -> roe(2025) NaN, roe(2024) normal
    assert roe(m, 2025) != roe(m, 2025)  # NaN
    assert roe(m, 2024) == pytest.approx(0.2 * (30e9 * 1.05 ** 4) / 10e9)
    assert not crits["roe_avg5"].is_na  # 4 EK>0-Jahre reichen (min_points_5y=3)
    # ROIC FY2025: Nenner = Schulden + EK = 20e9 - 5e9 = 15e9 (nicht EK+ND=10e9)
    ic = invested_capital_ek_le0(m, 2025)
    assert ic == pytest.approx(15e9)
    from data.metrics import nopat
    r2025 = roic(m, 2025)
    assert r2025 == r2025  # kein NaN
    assert r2025 == pytest.approx(nopat(m, 2025) / 15e9)  # Nenner Schulden+EK = 15e9
    # roic_latest in der Row nutzt dieselbe Logik
    assert row["roic_latest"] == pytest.approx(r2025)
    assert not crits["roic_avg5"].is_na


def test_roic_ek_le0_und_ic_le0_na(cfg):
    """EK <= 0 UND Schulden + EK <= 0 -> ROIC n/a (NaN, nicht 0)."""
    m = _mk_ek_negativ(equity=-30e9, total_debt=20e9)  # 20e9 + (-30e9) = -10e9 <= 0
    assert invested_capital_ek_le0(m, 2025) != invested_capital_ek_le0(m, 2025)  # NaN
    assert roic(m, 2025) != roic(m, 2025)  # NaN


def test_schalter_truestellt_altes_verhalten_her(cfg):
    """exclusion.negative_equity: true -> wieder Ausschluss 'Eigenkapital negativ'."""
    m = _mk_ek_negativ()
    cfg2 = _cfg_off(cfg, neg_equity_excl=True)
    reasons = check_filters(m, cfg2["filters"])
    assert any("Eigenkapital negativ" in r for r in reasons)
    out = evaluate_ticker(m, cfg2)
    assert out["row"] is None and out["excluded"]


def test_schalter_in_config(cfg):
    """config.yaml: filters.exclusion.negative_equity: false (Default v38)."""
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                         .read_text(encoding="utf-8"))
    assert raw["filters"]["exclusion"]["negative_equity"] is False
    # Asset Management in den Sonderregel-Patterns
    pats = raw["sector_rules"]["groups"]["special"]["industry_patterns"]
    assert "Asset Management" in pats
    assert "Financial Data & Stock Exchanges" not in pats


# ---------- (3) Asset Management -> Sonderregel ----------

def _mk_ind(ticker, industry, sector="Financial Services"):
    m = TickerMetrics(ticker=ticker, country="US")
    m.sector = sector
    m.industry = industry
    m.market_cap = Metric("market_cap", 50e9, "USD", "USD", 2025)
    return m


@pytest.mark.parametrize("ticker,industry", [
    ("NTRS", "Asset Management"), ("STT", "Asset Management"), ("AMP", "Asset Management"),
    ("BLK", "Asset Management"), ("BX", "Asset Management"), ("KKR", "Asset Management"),
])
def test_asset_management_sonderregel(cfg, ticker, industry):
    """NTRS, STT, AMP, BLK, BX, KKR -> Gruppe 'special' -> Status
    'Sonderregel noetig', MoS n/a (Sonderregel)."""
    cfg2 = _cfg_off(cfg)
    m = _mk_ind(ticker, industry)
    assert sector_rule(m, cfg2).group == "special"
    # minimale Jahre fuers evaluate_ticker (sonst Historie-Ausschluss)
    rev = 10e9
    for fy in range(2021, 2026):
        rev *= 1.05
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "fcf": Metric("fcf", 0.2 * rev, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", 0.18 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.15 * rev, "USD", "USD", fy),
            "equity": Metric("equity", 20e9, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 0.5e9, "shares", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["sector_special"] is True
    assert row["status"] == "Sonderregel n\u00f6tig"
    assert row["sector_group"] == "special"


@pytest.mark.parametrize("ticker", ["CME", "ICE", "SPGI", "MCO", "NDAQ", "CBOE", "FDS", "COIN"])
def test_financial_data_exchanges_normalmodell(cfg, ticker):
    """'Financial Data & Stock Exchanges' bleibt im Normalmodell
    (Standard/financials, NICHT special)."""
    cfg2 = _cfg_off(cfg)
    m = _mk_ind(ticker, "Financial Data & Stock Exchanges")
    rule = sector_rule(m, cfg2)
    assert rule.group != "special", f"{ticker} darf nicht special sein"
