"""yfinance-Provider: alle Nicht-US-Ticker (und US als Fallback).

Liefert das normalisierte Schema (TickerMetrics) mit Quelle 'yfinance'.
yfinance-Row-Labels -> interne Kennzahl-Felder (Mapping siehe _ROW_MAP).
Waehrung = Quote-Waehrung des Tickers (SAP.DE -> EUR, NOVO-B.CO -> DKK).
Alle Betraege absolut (nicht Mio.). Fehlende Werte -> NaN, nie 0.
"""

from __future__ import annotations

import logging
import math

from data.loader import Metric, TickerMetrics
from data.providers.base import ProviderError

logger = logging.getLogger("value_screener.providers.yfinance")

# yfinance-Row-Label -> internes Kennzahl-Feld.
# Jede Kennzahl hat mehrere Label-Fallbacks: yfinance nennt die Zeilen je nach
# Quelle/Sprache unterschiedlich ("Income Tax Expense" vs. "Tax Provision",
# "Diluted Average Shares" vs. "Diluted Average Shares"); erstes gefundenes
# Label gewinnt.
_ROW_MAP = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "gross_profit": ["Gross Profit"],
    "ebit": ["Operating Income", "EBIT"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "net_income": ["Net Income", "Net Income Common Stockholders"],
    "interest_expense": ["Interest Expense", "Net Interest Income", "Interest Expense Non Operating"],
    "income_tax_expense": ["Income Tax Expense", "Tax Provision"],
    "income_before_tax": ["Pretax Income", "Income Before Tax"],
    "shares_diluted": ["Diluted Average Shares", "Diluted Average Shares"],
    "equity": ["Stockholders Equity", "Total Equity Gross Minority Interest"],
    "_total_debt": ["Total Debt"],
    # Fallback-Zeilen fuer Schulden, wenn 'Total Debt' fehlt (z. B. XOM)
    "_long_term_debt": ["Long Term Debt"],
    "_short_term_debt": ["Short Term Debt", "Short Long Term Debt"],
    "net_debt": ["Net Debt"],
    "_cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "_short_term_investments": ["Short Term Investments",
                                "Other Short Term Investments",
                                "Cash Cash Equivalents And Short Term Investments"],
    "_restricted_cash": ["Restricted Cash"],
    "customer_deposits": ["Deposit Liabilities", "Customer Deposits"],
    "operating_cashflow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure", "Purchase Of PPE"],
    "stock_based_compensation": ["Stock Based Compensation", "Stock Based Compensation"],
    "_fcf_reported": ["Free Cash Flow"],
    # EBITDA = EBIT + D&A aus dem Cashflow-Statement (nicht das Yahoo-Feld)
    "depreciation_amortization": ["Depreciation And Amortization",
                                  "Depreciation & Amortization",
                                  "Depreciation Amortization And Accretion",
                                  "Depreciation And Amortization Cash Flow"],
}


def _nan() -> float:
    return float("nan")


def _pick(df, labels: list[str], col) -> float:
    """Erstes vorhandenes Fallback-Label; fehlend -> NaN."""
    for label in labels:
        v = _cell(df, label, col)
        if not math.isnan(v):
            return v
    return _nan()


def _cell(df, row_label: str, col) -> float:
    """Wert einer yfinance-Zelle/Spalte; fehlend -> NaN."""
    try:
        if row_label in df.index and col in df.columns:
            v = df.loc[row_label, col]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return _nan()
            return float(v)
    except (KeyError, TypeError, ValueError):
        pass
    return _nan()


class YfinanceProvider:
    """Provider fuer yfinance (Yahoo Finance); deckt alle Laender ab."""

    name = "yfinance"

    def __init__(self) -> None:
        self._yf = None  # lazy import, damit Tests ohne yfinance laufen

    def _module(self):
        if self._yf is None:
            try:
                import yfinance as yf
            except ImportError as exc:
                raise ProviderError("yfinance nicht installiert (pip install yfinance)") from exc
            self._yf = yf
        return self._yf

    def supports(self, ticker: str) -> bool:
        return True

    def load(self, ticker: str, years: int = 5) -> TickerMetrics:
        yf = self._module()
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        financials = tk.financials
        balance = tk.balance_sheet
        cashflow = tk.cashflow

        if financials is None or financials.empty:
            raise ProviderError(f"yfinance: keine Daten fuer {ticker}")

        currency = (info.get("currency") or info.get("financialCurrency") or "USD").upper()
        mcap = info.get("marketCap")

        result = TickerMetrics(ticker=ticker.upper())
        result.name = info.get("longName") or info.get("shortName") or ""
        result.sector = info.get("sector") or "Unbekannt"
        # Industry separat (GICS Industry) -- Sektorregeln (Banken/
        # Versicherer/Zykliker/REIT) laufen ueber Industry, nicht Sector
        # (PYPL/V/MA laufen als 'Financial Services', sind aber keine Banken).
        result.industry = info.get("industry") or ""
        result.country = info.get("country") or ""
        result.source = self.name
        # Zusatzkennzahlen fuer Sektorregeln (Banken/Versicherer: P/B,
        # Dividendenrendite; REITs: FFO-Hinweis falls gemeldet).
        ptb = info.get("priceToBook")
        if ptb is not None:
            result.extras["price_to_book"] = Metric("price_to_book", float(ptb), "ratio", "", 0)
        dy = info.get("dividendYield")
        if dy is not None:
            # yfinance liefert dividendYield teils als Prozent (2.5), teils als
            # Dezimalzahl (0.025) -- heuristisch auf Dezimal normieren.
            dy_val = float(dy)
            if dy_val > 1.0:
                dy_val = dy_val / 100.0
            result.extras["dividend_yield"] = Metric("dividend_yield", dy_val, "ratio", "", 0)
        if mcap is not None:
            # marketCap in Quote-Waehrung, absolut (wie FMP mktCap)
            result.market_cap = Metric("market_cap", float(mcap), currency, currency,
                                       int(financials.columns[0].year))
            from datetime import date
            result.market_cap_date = date.today().isoformat()  # Stichtag: Abrufdatum
        # naechster Earnings-Termin, falls im info-Feld vorhanden
        ned = info.get("nextEarningsDate") or info.get("earningsDate")
        if ned is not None:
            try:
                result.next_earnings_date = str(ned)[:10]
            except Exception:
                result.next_earnings_date = ""

        # Spalten sind Timestamps (Jahresende); auf int-Jahr mappen, juengste zuerst
        cols = sorted(financials.columns, reverse=True)[:years]
        all_cols = {
            "inc": sorted(getattr(financials, "columns", []), reverse=True)[:years],
            "bal": sorted(getattr(balance, "columns", []), reverse=True)[:years] if balance is not None and not balance.empty else [],
            "cf": sorted(getattr(cashflow, "columns", []), reverse=True)[:years] if cashflow is not None and not cashflow.empty else [],
        }

        for fy in sorted({int(c.year) for c in cols} | {int(c.year) for c in all_cols["bal"]} | {int(c.year) for c in all_cols["cf"]}):
            year_metrics: dict[str, Metric] = {}

            inc_col = next((c for c in all_cols["inc"] if int(c.year) == fy), None)
            bal_col = next((c for c in all_cols["bal"] if int(c.year) == fy), None)
            cf_col = next((c for c in all_cols["cf"] if int(c.year) == fy), None)
            # Exaktes Geschaeftsjahres-Enddatum ( fuer TTM-Nicht-Ueberlappung)
            for c in (inc_col, bal_col, cf_col):
                if c is not None:
                    result.fiscal_ends[fy] = c.date().isoformat()
                    break

            if inc_col is not None:
                for key in ("revenue", "gross_profit", "ebit", "ebitda", "net_income",
                            "interest_expense", "income_tax_expense", "income_before_tax",
                            "shares_diluted"):
                    year_metrics[key] = Metric(key, _pick(financials, _ROW_MAP[key], inc_col),
                                               "currency" if key != "shares_diluted" else "shares",
                                               currency, fy)
            if bal_col is not None:
                for key in ("equity", "_total_debt", "net_debt", "_cash", "_short_term_investments",
                            "_restricted_cash", "customer_deposits",
                            "_long_term_debt", "_short_term_debt"):  # Debt-Fallbacks
                    year_metrics[key] = Metric(key, _pick(balance, _ROW_MAP[key], bal_col),
                                               "currency", currency, fy)
            if cf_col is not None:
                for key in ("operating_cashflow", "capex", "stock_based_compensation", "_fcf_reported",
                            "depreciation_amortization"):
                    year_metrics[key] = Metric(key, _pick(cashflow, _ROW_MAP[key], cf_col),
                                               "currency", currency, fy)

            # EBITDA = EBIT + D&A aus dem Cashflow-Statement (nicht das
            # Yahoo-EBITDA-Feld); Fallback: gemeldetes EBITDA wenn D&A fehlt.
            ebit_m = year_metrics.get("ebit")
            da_m = year_metrics.get("depreciation_amortization")
            if (ebit_m is not None and da_m is not None
                    and not math.isnan(ebit_m.value) and not math.isnan(da_m.value)):
                year_metrics["ebitda"] = Metric("ebitda", ebit_m.value + da_m.value,
                                                "currency", currency, fy)
            eb_m = year_metrics.get("ebitda")
            if (eb_m is not None and ebit_m is not None and not eb_m.is_na()
                    and not math.isnan(ebit_m.value) and eb_m.value < ebit_m.value):
                result.ebitda_unplausible = True

            # Nettoverschuldung: yfinance 'Net Debt' bevorzugt; sonst Total Debt - Cash
            nd = year_metrics.get("net_debt")
            if nd is not None and math.isnan(nd.value):
                td = year_metrics.get("_total_debt")
                cash = year_metrics.get("_cash")
                if td is not None and cash is not None and not math.isnan(td.value) and not math.isnan(cash.value):
                    nd.value = td.value - cash.value
                elif td is not None and not math.isnan(td.value) and (cash is None or math.isnan(cash.value)):
                    nd.value = td.value
            if nd is not None:
                nd.name = "net_debt"

            # FCF: yfinance 'Free Cash Flow' bevorzugt; sonst OCF + Capex (Capex i. d. R. negativ)
            fcf_m = year_metrics.get("_fcf_reported")
            ocf = year_metrics.get("operating_cashflow")
            capex = year_metrics.get("capex")
            if fcf_m is not None and not math.isnan(fcf_m.value):
                fcf_val = fcf_m.value
            elif ocf is not None and capex is not None and not math.isnan(ocf.value) and not math.isnan(capex.value):
                fcf_val = ocf.value + capex.value  # Capex bei yfinance negativ
            else:
                fcf_val = _nan()
            year_metrics["fcf"] = Metric("fcf", fcf_val, currency, currency, fy)

            # FCF nach SBC = FCF - Aktienverguetung; fehlende SBC -> NaN, nie 0
            sbc = year_metrics.get("stock_based_compensation")
            sbc_val = sbc.value if sbc is not None else _nan()
            fcf_sbc = fcf_val - sbc_val if not (math.isnan(fcf_val) or math.isnan(sbc_val)) else _nan()
            year_metrics["fcf_sbc"] = Metric("fcf_sbc", fcf_sbc, currency, currency, fy)

            # Interest Expense: yfinance berichtet teils negativ -> Betrag
            ie = year_metrics.get("interest_expense")
            if ie is not None and not math.isnan(ie.value):
                ie.value = abs(ie.value)

            result.years[fy] = year_metrics

        logger.info("%s: %d Jahre von yfinance geladen (Waehrung %s)", ticker, len(result.years), currency)

        # --- TTM aus Quartalsdaten (Flussgroessen Summe letzte 4 Quartale,
        # Bestandsgoessen Stichtag letztes Quartal) ---
        ttm = self._load_ttm(tk, result, currency, fy_hint=int(financials.columns[0].year))
        if ttm is not None:
            result.ttm = ttm
            result.ttm_plausible = ttm.plausible
            result.ttm_fallback = ttm.fall_back_to_annual
            if not ttm.plausible:
                logger.warning("%s: %s", ticker, "; ".join(ttm.checks))
        return result

    def _load_ttm(self, tk, result: TickerMetrics, currency: str, fy_hint: int):
        """Laedt Quartalsdaten und berechnet TTM (data/ttm.py).

        Rueckgabe: TtmResult oder None, wenn keine Quartalsdaten verfuegbar.
        """
        try:
            q_inc = tk.quarterly_financials
            q_bal = tk.quarterly_balance_sheet
            q_cf = tk.quarterly_cashflow
        except Exception as exc:
            logger.warning("TTM: Quartalsdaten nicht ladbar: %s", exc)
            return None
        if q_inc is None or q_inc.empty:
            return None
        from data.ttm import compute_ttm
        # result traegt bereits die Jahresdaten -> Vergleich TTM vs. Jahreswert
        return compute_ttm(result, {
            "income": q_inc, "balance": q_bal, "cashflow": q_cf, "row_map": _ROW_MAP,
        })
