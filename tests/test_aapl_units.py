"""Einheiten-Regressionstests fuer AAPL (real oeffentliche Daten, absolute USD).

Verhindert Wiederholung des FCF-Yield-Bugs (Faktor 1e6 durch Mio.-Skalierung
der Marktkapitalisierung gegenueber absoluten Statement-Werten).
"""

import math

import pytest

from data.metrics import fcf_yield, fcf_yield_sbc, invested_capital, roic, roe
from data.sanity import check_ticker
from scoring.criteria import compute_criteria
from scoring.filters import check_filters


# --- Rohwerte mit Einheit/Waehrung (Loader-Ebene) ---------------------------

def test_aapl_raw_fcf_absolute_usd(aapl_metrics):
    fcf24 = aapl_metrics.years[2024]["fcf"]
    # OCF 118.254e9 + Capex (-9.447e9) = 108.807e9 USD (absolut, nicht Mio.)
    assert fcf24.value == pytest.approx(108807000000)
    assert fcf24.currency == "USD"
    assert fcf24.unit == "USD"   # Einheit = Waehrungsbetrag (absolut)
    assert fcf24.fiscal_year == 2024


def test_aapl_market_cap_absolute_usd(aapl_metrics):
    mcap = aapl_metrics.market_cap
    # FMP profile.mktCap kommt absolut in USD -- KEINE /1e6-Skalierung,
    # sonst Misch-Einheiten (Faktor 1e6) gegenueber FCF/EBIT/Nettogewinn.
    assert mcap.value == pytest.approx(5.07e12)
    assert mcap.currency == "USD"


def test_aapl_net_debt_and_ev(aapl_metrics):
    nd = aapl_metrics.years[2024]["net_debt"]
    assert nd.value == pytest.approx(-37591000000)  # Net Cash, absolut USD
    # EV = Marktkap. + Nettoverschuldung (beide absolut USD -> konsistent)
    ev = aapl_metrics.market_cap.value + nd.value
    assert ev == pytest.approx(5.07e12 - 37.591e9)


# --- Plausible FCF-Yield (Kern des gemeldeten Bugs) -------------------------

def test_aapl_fcf_yield_plausible_0_to_15_percent(aapl_metrics):
    """FCF-Yield muss im Bereich 0-15 % liegen (real ca. 1,9-2,2 %).

    Regression: mit /1e6-Skalierung der Marktkap ergab sich 1.946.239 %.
    """
    yields = {}
    for fy in (2023, 2024, 2025):
        y = fcf_yield(aapl_metrics, fy)
        assert not math.isnan(y), f"FCF-Yield FY{fy} darf nicht NaN sein"
        assert 0.0 <= y <= 0.15, (
            f"FCF-Yield FY{fy} = {y:.4%} unplausibel (erwartet ca. 2 %)"
        )
        yields[fy] = y
    # Hand-Nachrechnung FY2024: 108.807e9 / 5.07e12 = 2,146 %
    assert yields[2024] == pytest.approx(108807000000 / 5.07e12, rel=1e-9)
    # Ø ueber 3 Jahre im selben Bereich
    avg3 = sum(yields.values()) / 3
    assert 0.0 <= avg3 <= 0.15


def test_aapl_fcf_sbc_absolute_usd(aapl_metrics):
    """FCF-SBC = FCF - Aktienverguetung (FY2024: 108.807e9 - 10.952e9 = 97.855e9)."""
    fcf_sbc24 = aapl_metrics.years[2024]["fcf_sbc"]
    assert fcf_sbc24.value == pytest.approx(108807000000 - 10952000000)
    assert fcf_sbc24.currency == "USD"
    assert fcf_sbc24.fiscal_year == 2024


def test_aapl_fcf_yield_sbc_plausible(aapl_metrics):
    """FCF-Yield nach SBC < Roh-FCF-Yield (SBC wird abgezogen)."""
    y_raw = fcf_yield(aapl_metrics, 2024)
    y_sbc = fcf_yield_sbc(aapl_metrics, 2024)
    assert not math.isnan(y_sbc)
    assert 0.0 <= y_sbc < y_raw
    # Hand-Nachrechnung: 97.855e9 / 5.07e12 = 1,930 %
    assert y_sbc == pytest.approx((108807000000 - 10952000000) / 5.07e12, rel=1e-9)


def test_aapl_fcf_yield_sbc_nan_when_sbc_missing(aapl_metrics):
    """Fehlende SBC in allen Jahren -> fcf_sbc = NaN -> FCF-Yield SBC = NaN
    (nie 0, nie Roh-FCF); nie Kandidat. v35: Status 'Pruefen (SBC fehlt)' NUR,
    wenn ohne SBC Quality >= 60 UND MoS >= 30 % -- AAPL (teuer, MoS < 30 %)
    bekommt den normalen Status."""
    from config import load_config
    from scoring.pipeline import evaluate_ticker
    for fy in aapl_metrics.years:
        aapl_metrics.years[fy]["fcf_sbc"].value = float("nan")
    assert math.isnan(fcf_yield_sbc(aapl_metrics, 2024))
    cfg = load_config()
    cfg["sec"] = {"enabled": False}
    out = evaluate_ticker(aapl_metrics, cfg)
    row = out["row"]
    assert row["is_buy"] is False
    assert "SBC n/a (ohne SBC)" in row["flags"]
    # AAPL ohne SBC hat MoS < 30 % -> normaler Status, kein Pruefstatus
    assert row["status"] != "Kandidat"
    assert row["status_pruefen_sbc"] is (row["status"] == "Prüfen (SBC fehlt)")


def test_aapl_criterion_fcf_yield_sbc_avg3(aapl_metrics, cfg):
    vals = compute_criteria(aapl_metrics, cfg)
    crit = vals["fcf_yield_sbc_avg3"]
    assert not crit.is_na
    assert 0.0 <= crit.value <= 0.15
    assert crit.years_available == 5
    assert not crit.thin


# --- Einheitlich konsistente Kennzahlen -------------------------------------

def test_aapl_roic_consistent(aapl_metrics):
    # ROIC FY2024 = NOPAT / (EK + ND).
    # NOPAT = EBIT × (1 − 29.749/123.485) = 93,58e9
    # IC = 56.950e9 + (-37.591e9) = 19.359e9 -> ROIC = 4,83 (>100 % wg. Net Cash)
    r = roic(aapl_metrics, 2024)
    expected = 123216000000 * (1 - 29749000000 / 123485000000) / (56950000000 - 37591000000)
    assert r == pytest.approx(expected, rel=1e-9)
    assert 0.0 <= r <= 10.0


def test_aapl_invested_capital_equity_plus_net_debt(aapl_metrics):
    ic = invested_capital(aapl_metrics, 2024)
    assert ic == pytest.approx(56950000000 + (-37591000000))


def test_aapl_roe(aapl_metrics):
    r = roe(aapl_metrics, 2024)
    assert r == pytest.approx(93736000000 / 56950000000)
    assert 0.0 <= r <= 2.0


def test_aapl_net_debt_ebitda_negative(aapl_metrics):
    nd = aapl_metrics.years[2024]["net_debt"].value
    eb = aapl_metrics.years[2024]["ebitda"].value
    # EBITDA = EBIT + D&A aus dem Cashflow-Statement (nicht das Yahoo-Feld)
    ebit = aapl_metrics.years[2024]["ebit"].value
    da = aapl_metrics.years[2024]["depreciation_amortization"].value
    assert eb == pytest.approx(ebit + da)
    assert nd / eb == pytest.approx(-37591000000 / (123216000000 + 10801000000))


def test_aapl_interest_coverage_high(aapl_metrics):
    ebit = aapl_metrics.years[2024]["ebit"].value
    interest = aapl_metrics.years[2024]["interest_expense"].value
    assert ebit / abs(interest) == pytest.approx(123216000000 / 2980000000)


def test_aapl_filters_pass(aapl_metrics, cfg):
    reasons = check_filters(aapl_metrics, cfg["filters"])
    assert reasons == [], f"AAPL darf nicht ausgeschlossen werden: {reasons}"


# --- Sanity-Check ------------------------------------------------------------

def test_aapl_sanity_no_violations(aapl_metrics, cfg, caplog):
    with caplog.at_level("WARNING", logger="value_screener.sanity"):
        violations = check_ticker(aapl_metrics, cfg["sanity"])
    assert violations == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_sanity_detects_million_scaling_bug(aapl_metrics, cfg, caplog):
    """Reproduziert den urspruenglichen Bug: mktCap faelschlich /1e6 -> Alarm."""
    buggy = aapl_metrics
    buggy.market_cap.value = buggy.market_cap.value / 1e6  # Mio.-Skalierung (Bug)
    with caplog.at_level("WARNING", logger="value_screener.sanity"):
        violations = check_ticker(buggy, cfg["sanity"])
    assert violations, "Sanity-Check muss den Skalenfehler erkennen"
    assert any("market_cap_over_revenue" in v for v in violations)
    y = fcf_yield(buggy, 2024)
    assert y > 1000.0  # der gemeldete Fehler
