"""Tests fuer v29:
(1) Harter Filter "Historie": Jahre mit tatsaechlichen Werten (nicht Spalten),
    Schwelle 4 aus config.yaml; eine leere fuenfte Spalte aendert das Ergebnis
    nicht. Ausschlusstabelle traegt die gezaehlten Jahre (history_years).
(2) EBITDA = EBIT + D&A aus dem Cashflow-Statement (nicht das Yahoo-Feld);
    EBITDA < EBIT -> Flag "ebitda:unplausibel" und ND/EBITDA n/a.
(3) Quartalstabelle: Summenzeile der 4 Quartale = TTM (app.py-Anzeige: Summe
    ueber ttm.quarters entspricht ttm.ttm-Werten).
(4) SEC-Status in der Rangliste vorhanden (sec_check_* in der Row; Spalte SEC
    in app.py show_cols).
(5) Smoke ueber alle Demo-Ticker + v29-Spalten.
"""

import math

import pytest

from data.demo import demo_companies, AAPL_BALANCE, AAPL_CASHFLOW, AAPL_INCOME, AAPL_PROFILE
from data.loader import Metric, TickerMetrics, extract_metrics
from scoring.criteria import compute_criteria
from scoring.filters import check_filters, count_value_years
from scoring.pipeline import evaluate_all, evaluate_ticker


def _mk(ticker="T", mcap=1e12, with_fy2020=True):
    """Synthetischer Ticker mit 4 oder 5 Jahren; fy2020 optional als LEERE Spalte."""
    m = TickerMetrics(ticker=ticker, country="US")
    m.name = ticker
    m.sector = "Technology"
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 100e9
    for fy in ([2020] if with_fy2020 else []) + list(range(2021, 2025)):
        if fy == 2020:
            # leere fuenfte Spalte: Jahr vorhanden, aber alle Werte NaN
            m.years[fy] = {k: Metric(k, float("nan"), "USD", "USD", fy)
                           for k in ("revenue", "ebit", "net_income",
                                     "operating_cashflow", "equity")}
            continue
        rev *= 1.08
        ocf = 0.28 * rev
        capex = -0.05 * rev
        sbc = 0.02 * rev
        fcf = ocf + capex
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.30 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.20 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", ocf, "USD", "USD", fy),
            "capex": Metric("capex", capex, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", sbc, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fcf - sbc, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.5e9, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
        }
    return m


# ---------- (1) Historie-Filter: Werte, nicht Spalten ----------

def test_history_counts_values_not_columns(cfg):
    m = _mk(with_fy2020=True)   # 5 Spalten, aber nur 4 mit Werten
    assert len(m.years) == 5
    assert count_value_years(m) == 4
    reasons = check_filters(m, cfg["filters"])
    assert not any("Historie" in r for r in reasons), reasons  # 4 >= 4 -> kein Ausschluss


def test_empty_fifth_column_does_not_change_result(cfg):
    """Eine leere fuenfte Spalte aendert das Ergebnis nicht (Werte zaehlen)."""
    m_full = _mk(with_fy2020=False)   # 4 Jahre mit Werten
    m_empty = _mk(with_fy2020=True)   # 5 Spalten, 4 mit Werten
    r_full = check_filters(m_full, cfg["filters"])
    r_empty = check_filters(m_empty, cfg["filters"])
    assert r_full == r_empty
    assert count_value_years(m_full) == count_value_years(m_empty) == 4


def test_history_below_threshold_excludes(cfg):
    m = _mk()
    # 2024 leer machen -> nur 3 Jahre mit Werten (2021-2023)
    m.years[2024] = {k: Metric(k, float("nan"), "USD", "USD", 2024) for k in m.years[2024]}
    assert count_value_years(m) == 3
    reasons = check_filters(m, cfg["filters"])
    assert any("Historie 3 J. mit Werten < 4 J." in r for r in reasons), reasons


def test_excluded_table_carries_history_years(cfg):
    rows, excluded = evaluate_all(demo_companies(), cfg)
    assert excluded, "Demo-Daten muessen ausgeschlossene Ticker liefern"
    for e in excluded:
        assert "history_years" in e
        assert isinstance(e["history_years"], int)
    gruende = {e["ticker"]: e for e in excluded}
    # DIST und MICR haben 5 Jahre mit Werten -> anderer Grund
    # DIST wegen Value-Trap (negatives EK schliesst nicht mehr aus)
    assert gruende["DIST"]["history_years"] == 5
    assert any("Durchschnitts-FCF" in r for r in gruende["DIST"]["reasons"])  # v38
    assert gruende["MICR"]["history_years"] == 5
    assert any("Marktkap" in r for r in gruende["MICR"]["reasons"])


def test_threshold_from_config(cfg):
    """Schwelle kommt aus config.yaml (filters.min_history_years = 4)."""
    assert cfg["filters"]["min_history_years"] == 4
    m = _mk()
    # 2024 leer -> 3 Jahre mit Werten
    m.years[2024] = {k: Metric(k, float("nan"), "USD", "USD", 2024) for k in m.years[2024]}
    assert count_value_years(m) == 3
    cfg2 = dict(cfg)
    cfg2["filters"] = dict(cfg["filters"])
    cfg2["filters"]["min_history_years"] = 3
    reasons = check_filters(m, cfg2["filters"])
    assert not any("Historie" in r for r in reasons)
    reasons4 = check_filters(m, cfg["filters"])
    assert any("Historie" in r for r in reasons4)


# ---------- (2) EBITDA = EBIT + D&A ----------

def test_ebitda_is_ebit_plus_da_fmp(aapl_metrics):
    """FMP-Pfad: EBITDA = EBIT + D&A aus dem Cashflow-Statement."""
    for fy in aapl_metrics.years:
        ebit = aapl_metrics.years[fy]["ebit"].value
        da = aapl_metrics.years[fy]["depreciation_amortization"].value
        ebitda = aapl_metrics.years[fy]["ebitda"].value
        assert ebitda == pytest.approx(ebit + da)
        assert ebitda >= ebit  # Plausibilitaet
    assert not aapl_metrics.ebitda_unplausible


def test_ebitda_ignores_reported_field():
    """Gemeldetes EBITDA-Feld wird ignoriert, wenn D&A vorhanden ist."""
    income = [{"date": "2025-12-31", "calendarYear": "2025", "reportedCurrency": "USD",
               "revenue": 100e9, "grossProfit": 60e9, "operatingIncome": 25e9,
               "ebitda": 999e9,  # bewusst falsch gemeldet
               "netIncome": 20e9, "interestExpense": -0.5e9,
               "incomeTaxExpense": 5e9, "incomeBeforeTax": 25e9,
               "weightedAverageSharesDiluted": 2e9}]
    balance = [{"date": "2025-12-31", "calendarYear": "2025", "reportedCurrency": "USD",
                "totalStockholdersEquity": 80e9, "netDebt": 5e9}]
    cashflow = [{"date": "2025-12-31", "calendarYear": "2025", "reportedCurrency": "USD",
                 "operatingCashFlow": 28e9, "capitalExpenditure": -5e9,
                 "stockBasedCompensation": 2e9, "depreciationAndAmortization": 4e9}]
    m = extract_metrics("T", income, balance, cashflow, {"symbol": "T", "companyName": "T",
                                                          "sector": "Technology", "country": "US",
                                                          "mktCap": 1e12})
    assert m.years[2025]["ebitda"].value == pytest.approx(25e9 + 4e9)


def test_ebitda_unplausible_flag_and_nd_ebitda_na(cfg):
    """EBITDA < EBIT -> Flag 'ebitda:unplausibel' in der Row, ND/EBITDA n/a."""
    m = _mk()
    # EBITDA kleiner als EBIT im neuesten Jahr erzwingen
    fy_last = max(m.years)
    m.years[fy_last]["ebitda"] = Metric("ebitda", m.years[fy_last]["ebit"].value - 1e9, "USD", "USD", fy_last)
    m.ebitda_unplausible = True
    res = evaluate_ticker(m, cfg)
    assert res["row"] is not None
    assert "ebitda:unplausibel" in res["row"]["flags"]
    assert math.isnan(res["row"]["nd_ebitda"])
    assert res["row"]["ebitda_unplausible"] is True
    crits = compute_criteria(m, cfg)
    assert crits["net_debt_ebitda"].is_na and math.isnan(crits["net_debt_ebitda"].value)


def test_ebitda_fallback_to_reported_when_no_da():
    """Kein D&A gemeldet -> gemeldetes EBITDA bleibt (Fallback)."""
    income = [{"date": "2025-12-31", "calendarYear": "2025", "reportedCurrency": "USD",
               "revenue": 100e9, "grossProfit": 60e9, "operatingIncome": 25e9,
               "ebitda": 30e9, "netIncome": 20e9, "interestExpense": -0.5e9,
               "incomeTaxExpense": 5e9, "incomeBeforeTax": 25e9,
               "weightedAverageSharesDiluted": 2e9}]
    balance = [{"date": "2025-12-31", "calendarYear": "2025", "reportedCurrency": "USD",
                "totalStockholdersEquity": 80e9, "netDebt": 5e9}]
    cashflow = [{"date": "2025-12-31", "calendarYear": "2025", "reportedCurrency": "USD",
                 "operatingCashFlow": 28e9, "capitalExpenditure": -5e9,
                 "stockBasedCompensation": 2e9}]  # kein D&A
    m = extract_metrics("T", income, balance, cashflow, {"symbol": "T", "mktCap": 1e12})
    assert m.years[2025]["ebitda"].value == pytest.approx(30e9)


# ---------- (3) Quartalstabelle: Summenzeile = TTM ----------

def test_quarter_sum_equals_ttm():
    """Summe der 4 Quartale (app.py-Summenzeile) entspricht dem TTM-Wert."""
    from data.ttm import TtmResult
    ttm = TtmResult()
    ttm.quarters = [
        {"quarter_end": "2025-09-30", "operating_cashflow": 30e9, "capex": -3e9,
         "stock_based_compensation": 2e9, "fcf_sbc": 25e9},
        {"quarter_end": "2025-06-30", "operating_cashflow": 28e9, "capex": -2.5e9,
         "stock_based_compensation": 1.8e9, "fcf_sbc": 23.7e9},
        {"quarter_end": "2025-03-31", "operating_cashflow": 27e9, "capex": -2e9,
         "stock_based_compensation": 1.7e9, "fcf_sbc": 23.3e9},
        {"quarter_end": "2024-12-31", "operating_cashflow": 29e9, "capex": -2.2e9,
         "stock_based_compensation": 1.9e9, "fcf_sbc": 24.9e9},
    ]
    ttm.ttm["operating_cashflow"] = Metric("operating_cashflow", 114e9, "currency", "USD", 2025)
    ttm.ttm["capex"] = Metric("capex", -9.7e9, "currency", "USD", 2025)
    ttm.ttm["stock_based_compensation"] = Metric("stock_based_compensation", 7.4e9, "currency", "USD", 2025)
    ttm.ttm["fcf_sbc"] = Metric("fcf_sbc", 96.9e9, "currency", "USD", 2025)
    assert sum(q["operating_cashflow"] for q in ttm.quarters) == pytest.approx(ttm.ttm["operating_cashflow"].value)
    assert sum(q["capex"] for q in ttm.quarters) == pytest.approx(ttm.ttm["capex"].value)
    assert sum(q["stock_based_compensation"] for q in ttm.quarters) == pytest.approx(ttm.ttm["stock_based_compensation"].value)
    assert sum(q["fcf_sbc"] for q in ttm.quarters) == pytest.approx(ttm.ttm["fcf_sbc"].value)


# ---------- (4) SEC-Status in der Rangliste ----------

def test_sec_columns_in_row_and_app(cfg):
    rows, _ = evaluate_all(demo_companies(), cfg)
    for r in rows:
        assert "sec_check_ok" in r and "sec_check_flag" in r and "sec_check_note" in r
    # app.py zeigt die SEC-Spalte
    import io as _io
    with _io.open("app.py", encoding="utf-8") as f:
        src = f.read()
    assert '"SEC"' in src and "show_cols" in src
    assert '"SEC", "Flags"]' in src


# ---------- (5) Smoke ----------

def test_smoke_v29_columns(cfg):
    rows, excluded = evaluate_all(demo_companies(), cfg)
    assert rows, "mindestens ein Ticker muss bewertet werden"
    for r in rows:
        assert "history_years" in r and "ebitda_unplausible" in r
        assert r["history_years"] >= cfg["filters"]["min_history_years"]
    assert excluded
    for e in excluded:
        assert "history_years" in e
