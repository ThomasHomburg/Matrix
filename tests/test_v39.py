"""Tests fuer v39:
(1) Industry "Financial Data & Stock Exchanges" ist ab v39 in der
    STANDARDgruppe (nicht mehr financials): alle Standardkriterien gelten
    (roic_avg5, fcf_margin_avg5, fcf_positive_years, net_debt_ebitda), die
    Bank-Kriterien (roe_avg5_financials, price_to_book, dividend_yield)
    entfallen. Test: CME, SPGI, ICE, MCO, NDAQ, CBOE, FDS, MSCI, COIN bekommen
    einen Quality-Score (nicht n/a).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.loader import Metric, TickerMetrics
from scoring.criteria import active_criteria, compute_criteria
from scoring.pipeline import evaluate_ticker
from scoring.sectors import sector_rule

EXCHANGES = ["CME", "SPGI", "ICE", "MCO", "NDAQ", "CBOE", "FDS", "MSCI", "COIN"]


def _mk_exchange(ticker: str) -> TickerMetrics:
    """Boersen/Finanzdaten-Firma mit vollstaendigen Standarddaten (5 GJ)."""
    m = TickerMetrics(ticker=ticker, country="US")
    m.name = ticker
    m.sector = "Financial Services"
    m.industry = "Financial Data & Stock Exchanges"
    m.market_cap = Metric("market_cap", 50e9, "USD", "USD", 2025)
    rev = 5e9
    for fy in range(2021, 2026):
        rev *= 1.08
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.75 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.45 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.50 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.35 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.50 * rev, "USD", "USD", fy),
            "capex": Metric("capex", -0.05 * rev, "USD", "USD", fy),
            "fcf": Metric("fcf", 0.45 * rev, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", 0.02 * rev, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", 0.43 * rev, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.005 * rev, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.10 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.44 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 0.1e9, "shares", "USD", fy),
            "equity": Metric("equity", 8e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 2e9, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


def _cfg_off(cfg):
    cfg2 = dict(cfg)
    sec = dict(cfg2.get("sec", {}))
    sec["enabled"] = False
    cfg2["sec"] = sec
    return cfg2


@pytest.mark.parametrize("ticker", EXCHANGES)
def test_exchanges_standardgruppe_kein_financials(cfg, ticker):
    """Gruppe ist 'standard' (nicht financials, nicht special)."""
    m = _mk_exchange(ticker)
    rule = sector_rule(m, _cfg_off(cfg))
    assert rule.group == "standard", f"{ticker}: {rule.group}"


@pytest.mark.parametrize("ticker", EXCHANGES)
def test_exchanges_bank_kriterien_entfallen(cfg, ticker):
    """Bank-Kriterien (roe_avg5_financials, price_to_book,
    dividend_yield) sind NICHT aktiv; Standardkriterien schon."""
    m = _mk_exchange(ticker)
    crits = active_criteria(m, _cfg_off(cfg))
    for bank_crit in ("roe_avg5_financials", "price_to_book", "dividend_yield"):
        assert bank_crit not in crits, f"{ticker}: {bank_crit} darf nicht aktiv sein"
    for std in ("roic_avg5", "fcf_margin_avg5", "fcf_positive_years",
                "net_debt_ebitda", "mos_base"):
        assert std in crits, f"{ticker}: {std} fehlt"


@pytest.mark.parametrize("ticker", EXCHANGES)
def test_exchanges_quality_score_nicht_na(cfg, ticker):
    """CME, SPGI, ICE, MCO, NDAQ, CBOE, FDS, MSCI, COIN bekommen einen
    Quality-Score (Zahl, nicht n/a) -- Standardkriterien inkl. fcf_margin_avg5,
    fcf_positive_years, net_debt_ebitda, roic_avg5 sind alle bewertet."""
    m = _mk_exchange(ticker)
    out = evaluate_ticker(m, _cfg_off(cfg))
    row = out["row"]
    assert row is not None
    crits = out["criteria"]
    for std in ("roic_avg5", "fcf_margin_avg5", "fcf_positive_years", "net_debt_ebitda"):
        assert not crits[std].is_na, f"{ticker}: {std} ist n/a"
    assert row["quality"] == row["quality"], f"{ticker}: Quality ist n/a"
    assert row["quality"] > 0
    assert row["sector_group"] == "standard"


def test_exchanges_in_config_nicht_mehr_financials():
    """config.yaml: 'Financial Data & Stock Exchanges' nicht mehr unter
    sector_rules.groups.financials.industries."""
    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                         .read_text(encoding="utf-8"))
    fin = raw["sector_rules"]["groups"]["financials"]["industries"]
    assert "Financial Data & Stock Exchanges" not in fin
    assert "Diversified Financial Services" in fin  # bleibt financials
