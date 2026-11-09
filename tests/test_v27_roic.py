"""ROIC-Steuersatz-Begrenzung.

- Effektiver Steuersatz begrenzt auf 0-35 % (config.yaml data.tax_rate_min/max);
  ausserhalb davon und bei Vorsteuerverlust gilt der Normsteuersatz
  (data.tax_rate_norm, 21 %).
- Bei EBIT <= 0 ist NOPAT = EBIT (kein Steuervorteil im Verlustjahr).
- Test: NOPAT <= EBIT, sobald EBIT > 0.
"""

import pytest

from data.loader import Metric, TickerMetrics
from data.metrics import nopat, roic, tax_rate


def _ticker(ebit: float, tax: float, pretax: float, equity: float = 100.0,
            net_debt: float = 0.0) -> TickerMetrics:
    m = TickerMetrics(ticker="T", country="US")
    m.years[2025] = {
        "ebit": Metric("ebit", ebit, "USD", "USD", 2025),
        "income_tax_expense": Metric("income_tax_expense", tax, "USD", "USD", 2025),
        "income_before_tax": Metric("income_before_tax", pretax, "USD", "USD", 2025),
        "equity": Metric("equity", equity, "USD", "USD", 2025),
        "net_debt": Metric("net_debt", net_debt, "USD", "USD", 2025),
    }
    return m


def test_nopat_immer_kleiner_gleich_ebit_bei_positivem_ebit():
    """Kern-Test: NOPAT <= EBIT, sobald EBIT > 0 (egal welcher Steuersatz)."""
    # Steuersatz 0 % (keine Steuer) -> NOPAT == EBIT
    m = _ticker(ebit=100.0, tax=0.0, pretax=100.0)
    assert nopat(m, 2025) == pytest.approx(100.0)
    # Steuersatz 21 % (Norm) -> NOPAT = 79
    m = _ticker(ebit=100.0, tax=21.0, pretax=100.0)
    assert nopat(m, 2025) == pytest.approx(79.0)
    # Steuersatz 35 % (Obergrenze) -> NOPAT = 65
    m = _ticker(ebit=100.0, tax=35.0, pretax=100.0)
    assert nopat(m, 2025) == pytest.approx(65.0)
    # Steuersatz 50 % (> 35 % -> Normsteuersatz 21 %) -> NOPAT = 79
    m = _ticker(ebit=100.0, tax=50.0, pretax=100.0)
    assert tax_rate(m, 2025) == pytest.approx(0.21)
    assert nopat(m, 2025) == pytest.approx(79.0)
    # Negativer Steuersatz (< 0 % -> Normsteuersatz 21 %)
    m = _ticker(ebit=100.0, tax=-10.0, pretax=100.0)
    assert tax_rate(m, 2025) == pytest.approx(0.21)
    assert nopat(m, 2025) == pytest.approx(79.0)
    # Vorsteuerverlust (pretax < 0, Steuersatz negativ) -> Normsteuersatz
    m = _ticker(ebit=100.0, tax=-5.0, pretax=-100.0)
    assert tax_rate(m, 2025) == pytest.approx(0.21)
    assert nopat(m, 2025) == pytest.approx(79.0)
    for mm in (_ticker(ebit=e, tax=t, pretax=p)
               for e, t, p in [(100, 0, 100), (100, 21, 100), (100, 35, 100),
                               (100, 50, 100), (100, -10, 100), (100, -5, -100),
                               (1000, 210, 1000), (1000, 350, 1000)]):
        assert nopat(mm, 2025) <= mm.years[2025]["ebit"].value + 1e-9


def test_nopat_gleich_ebit_bei_ebit_negativ():
    """Bei EBIT <= 0 ist NOPAT = EBIT (kein Steuervorteil im Verlustjahr)."""
    m = _ticker(ebit=-50.0, tax=-10.0, pretax=-50.0)
    assert nopat(m, 2025) == pytest.approx(-50.0)
    m = _ticker(ebit=0.0, tax=0.0, pretax=0.0)
    assert nopat(m, 2025) == pytest.approx(0.0)


def test_tax_rate_clamp_grenzen():
    """Genau an den Grenzen 0 % und 35 % bleibt der effektive Satz erhalten."""
    m = _ticker(ebit=100.0, tax=0.0, pretax=100.0)
    assert tax_rate(m, 2025) == pytest.approx(0.0)
    m = _ticker(ebit=100.0, tax=35.0, pretax=100.0)
    assert tax_rate(m, 2025) == pytest.approx(0.35)
    # Kurz darueber -> Normsteuersatz
    m = _ticker(ebit=100.0, tax=35.01, pretax=100.0)
    assert tax_rate(m, 2025) == pytest.approx(0.21)


def test_roic_mit_geclamptem_steuersatz():
    """ROIC nutzt den geclampten NOPAT: NOPAT/IC mit IC = EK + ND."""
    # EBIT 100, Steuer 50 % -> NOPAT 79; IC = 100 -> ROIC 79 %
    m = _ticker(ebit=100.0, tax=50.0, pretax=100.0, equity=100.0, net_debt=0.0)
    assert roic(m, 2025) == pytest.approx(0.79)
    # Verlustjahr: NOPAT = EBIT = -50; IC = 100 -> ROIC -50 %
    m = _ticker(ebit=-50.0, tax=-10.0, pretax=-50.0, equity=100.0, net_debt=0.0)
    assert roic(m, 2025) == pytest.approx(-0.50)


def test_aapl_nopat_kleiner_gleich_ebit_alle_jahre(aapl_metrics):
    """AAPL (echte Daten): in jedem Jahr NOPAT <= EBIT."""
    for fy in sorted(aapl_metrics.years):
        ebit = aapl_metrics.years[fy]["ebit"].value
        np_ = nopat(aapl_metrics, fy)
        assert np_ == np_
        assert np_ <= ebit + 1e-6, f"FY{fy}: NOPAT {np_} > EBIT {ebit}"
