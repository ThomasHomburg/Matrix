"""Loader: laedt Fundamentaldaten eines Tickers und extrahiert Kennzahlen.

Jede Kennzahl wird mit Wert, Einheit, Waehrung und Berichtsjahr gespeichert.
Fehlende Rohwerte werden als NaN behandelt, nie als 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .fmp_client import FMPClient

YEARS = 5


@dataclass
class Metric:
    name: str
    value: float  # float('nan') wenn unbekannt
    unit: str
    currency: str
    fiscal_year: int

    def is_na(self) -> bool:
        return math.isnan(self.value)


@dataclass
class TickerMetrics:
    ticker: str
    name: str = ""
    sector: str = ""
    industry: str = ""   # Branchen-Feld (GICS Industry) -- Sektorregeln (Banken/Versicherer/Zykliker/REIT)
    country: str = ""
    source: str = ""  # Datenquelle: fmp | yfinance | demo
    # Zusatzkennzahlen fuer Sektorregeln (Banken/Versicherer: P/B,
    # Dividendenrendite; REITs: FFO-Hinweis). Je Kennzahl eine Metric.
    extras: dict[str, Metric] = field(default_factory=dict)
    market_cap: Metric | None = None
    market_cap_date: str = ""  # Stichtag der Marktkapitalisierung (ISO-Datum, Abrufdatum)
    # naechster Earnings-Termin (ISO-Datum), falls der Provider ihn liefert
    next_earnings_date: str = ""
    years: dict[str, dict[str, Metric]] = field(default_factory=dict)
    # TTM (Quartalsdaten): Flussgroessen = Summe letzte 4 Quartale,
    # Bestandsgoessen = Stichtag letztes Quartal (nur yfinance-Provider).
    # EBITDA unplausibel (EBITDA < EBIT in mind. einem Jahr) -> Flag + ND/EBITDA n/a
    ebitda_unplausible: bool = False
    ttm: object | None = None            # data.ttm.TtmResult
    ttm_plausible: bool = True           # False -> Flag "Quartalsdaten unplausibel"
    ttm_fallback: bool = False           # True -> Rueckfall auf Geschaeftsjahre
    # Exaktes Geschaeftsjahres-Enddatum je Jahr (ISO) -- fuer die TTM-Basis
    # (nicht ueberlappende Geschaeftsjahre vor dem TTM-Fenster).
    fiscal_ends: dict[int, str] = field(default_factory=dict)

    def series(self, metric_name: str) -> list[Metric]:
        """Kennzahl chronologisch (aeltestes Jahr zuerst)."""
        out = []
        for fy in sorted(self.years):
            m = self.years[fy].get(metric_name)
            if m is not None:
                out.append(m)
        return out

    def latest(self, metric_name: str) -> Metric | None:
        series = self.series(metric_name)
        return series[-1] if series else None


_COUNTRY_CODES = {
    "US": "US", "USA": "US", "United States": "US", "United States of America": "US",
    "DE": "DE", "Germany": "DE", "Deutschland": "DE",
    "GB": "GB", "United Kingdom": "GB", "UK": "GB", "Great Britain": "GB",
    "FR": "FR", "France": "FR",
    "NL": "NL", "Netherlands": "NL", "Niederlande": "NL",
    "CH": "CH", "Switzerland": "CH", "Schweiz": "CH",
    "SE": "SE", "Sweden": "SE", "Schweden": "SE",
    "DK": "DK", "Denmark": "DK", "Daenemark": "DK",
    "NO": "NO", "Norway": "NO", "Norwegen": "NO",
    "FI": "FI", "Finland": "FI", "Finnland": "FI",
    "JP": "JP", "Japan": "JP",
    "CN": "CN", "China": "CN",
    "CA": "CA", "Canada": "CA", "Kanada": "CA",
    "AU": "AU", "Australia": "AU",
    "IT": "IT", "Italy": "IT", "Italien": "IT",
    "ES": "ES", "Spain": "ES", "Spanien": "ES",
    "BE": "BE", "Belgium": "BE",
    "IE": "IE", "Ireland": "IE", "Irland": "IE",
    "AT": "AT", "Austria": "AT", "Oesterreich": "AT",
    "KR": "KR", "South Korea": "KR", "Korea": "KR",
    "TW": "TW", "Taiwan": "TW",
    "IN": "IN", "India": "IN",
    "BR": "BR", "Brazil": "BR", "Brasilien": "BR",
    "MX": "MX", "Mexico": "MX",
    "ZA": "ZA", "South Africa": "ZA",
}


def country_code(country: str) -> str:
    """Land als 2-Buchstaben-Kuerzel (US, DE, ...); unbekannt -> Original."""
    if not country:
        return "?"
    c = country.strip()
    return _COUNTRY_CODES.get(c, _COUNTRY_CODES.get(c.upper(), c[:2].upper()))


def _metric(name: str, raw: Any, unit: str, currency: str, fy: int) -> Metric:
    """Baut eine Metric; fehlender Rohwert -> NaN."""
    if raw is None:
        return Metric(name, float("nan"), unit, currency, fy)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return Metric(name, float("nan"), unit, currency, fy)
    return Metric(name, value, unit, currency, fy)


def _by_year(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(r.get("calendarYear") or r.get("date", "0000")[:4]): r for r in rows}


def extract_metrics(
    ticker: str,
    income: list[dict[str, Any]],
    balance: list[dict[str, Any]],
    cashflow: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> TickerMetrics:
    """Extrahiert die CLAUDE.md-Kennzahlen aus FMP-Rohdaten (5 Jahre, falls verfuegbar).

    FCF = OCF - |Capex|; FMP liefert Capex mit negativem Vorzeichen.
    Fehlende Komponente -> NaN.
    """
    inc_y = _by_year(income)
    bal_y = _by_year(balance)
    cf_y = _by_year(cashflow)
    all_years = sorted(set(inc_y) | set(bal_y) | set(cf_y), reverse=True)[:YEARS]

    result = TickerMetrics(ticker=ticker.upper())
    if profile:
        result.name = profile.get("companyName", "")
        result.sector = profile.get("sector", "") or "Unbekannt"
        # Industry separat (FMP: 'industry'; yfinance: uebergeben als industry)
        result.industry = profile.get("industry", "") or ""
        result.country = profile.get("country", "") or ""
        mcap = profile.get("mktCap", profile.get("marketCap"))
        # Zusatzkennzahlen (P/B, Dividendenrendite, FFO, ...) aus dem Profil
        for key in ("price_to_book", "dividend_yield", "ffo_per_share", "ffo"):
            raw = profile.get(key)
            if raw is not None:
                try:
                    result.extras[key] = Metric(key, float(raw), "ratio", "", 0)
                except (TypeError, ValueError):
                    pass
        if mcap is not None:
            # FMP liefert mktCap in absoluten USD (wie die Statements);
            # KEINE Umrechnung auf Mio. -- sonst Misch-Einheiten (Faktor 1e6)
            # gegenueber FCF/EBIT/Nettogewinn aus den Statements.
            # Hinweis: mktCap ist die AKTUELLE Marktkapitalisierung, nicht
            # der Stand zum jeweiligen Berichtsjahr.
            from datetime import date
            result.market_cap = _metric("market_cap", float(mcap), "USD", "USD",
                                        all_years[0] if all_years else 0)
            result.market_cap_date = date.today().isoformat()  # Stichtag: Abrufdatum

    for fy in all_years:
        inc = inc_y.get(fy, {})
        bal = bal_y.get(fy, {})
        cf = cf_y.get(fy, {})
        # Exaktes Geschaeftsjahres-Enddatum (erstes verfuegbares Statement-Datum)
        for row in (inc, bal, cf):
            d = row.get("date")
            if d:
                result.fiscal_ends[fy] = str(d)[:10]
                break
        # FMP-Statements tragen die Waehrung im Feld "reportedCurrency".
        currency = (
            inc.get("reportedCurrency") or inc.get("currency")
            or bal.get("reportedCurrency") or bal.get("currency")
            or cf.get("reportedCurrency") or cf.get("currency")
            or "USD"
        )

        defs = {
            "revenue": (inc.get("revenue"), "currency"),
            "gross_profit": (inc.get("grossProfit"), "currency"),
            "ebit": (inc.get("operatingIncome"), "currency"),
            "ebitda": (inc.get("ebitda") or inc.get("ebitdaLoss"), "currency"),
            "net_income": (inc.get("netIncome"), "currency"),
            "interest_expense": (inc.get("interestExpense"), "currency"),
            "income_tax_expense": (inc.get("incomeTaxExpense"), "currency"),
            "income_before_tax": (inc.get("incomeBeforeTax"), "currency"),
            "shares_diluted": (inc.get("weightedAverageSharesDiluted"), "shares"),
            "equity": (bal.get("totalStockholdersEquity"), "currency"),
            "net_debt": (bal.get("netDebt"), "currency"),
            "_total_debt": (bal.get("totalDebt"), "currency"),
            "_cash": (bal.get("cashAndCashEquivalents"), "currency"),
            "_short_term_investments": (bal.get("shortTermInvestments")
                                        or bal.get("otherShortTermInvestments"), "currency"),
            "customer_deposits": (bal.get("depositLiabilities")
                                  or bal.get("customerDeposits"), "currency"),
            "operating_cashflow": (cf.get("operatingCashFlow"), "currency"),
            "capex": (cf.get("capitalExpenditure"), "currency"),
            "stock_based_compensation": (cf.get("stockBasedCompensation"), "currency"),
            "depreciation_amortization": (cf.get("depreciationAndAmortization")
                                          or cf.get("depreciationAndAmortizationCashFlow"), "currency"),
        }
        year_metrics: dict[str, Metric] = {
            name: _metric(name, raw, unit, currency, fy) for name, (raw, unit) in defs.items()
        }

        # EBITDA = EBIT + Abschreibungen (D&A) aus dem Cashflow-Statement,
        # NICHT das gemeldete Yahoo/FMP-EBITDA-Feld (das weicht teils ab).
        # Fallback nur wenn kein D&A gemeldet: gemeldetes EBITDA.
        ebit_v = year_metrics["ebit"].value
        da_v = year_metrics["depreciation_amortization"].value
        rep_ebitda = year_metrics["ebitda"].value
        if not math.isnan(ebit_v) and not math.isnan(da_v):
            year_metrics["ebitda"] = Metric("ebitda", ebit_v + da_v, currency, currency, fy)
        elif math.isnan(year_metrics["ebitda"].value):
            pass  # gemeldetes EBITDA (ggf. NaN) bleibt
        if (not math.isnan(year_metrics["ebitda"].value) and not math.isnan(ebit_v)
                and year_metrics["ebitda"].value < ebit_v):
            result.ebitda_unplausible = True

        # FCF = OCF - |Capex|; FMP liefert Capex mit negativem Vorzeichen.
        # Fehlende Komponente -> NaN
        ocf = year_metrics["operating_cashflow"].value
        capex = year_metrics["capex"].value
        fcf = ocf + capex if not (math.isnan(ocf) or math.isnan(capex)) else float("nan")
        year_metrics["fcf"] = Metric("fcf", fcf, currency, currency, fy)

        # FCF nach SBC = FCF - Aktienverguetung (SBC ist bei FMP positiv ausgewiesen;
        # fehlende SBC -> NaN, nie 0 -- sonst wuerde der Roh-FCF verwendet)
        sbc = year_metrics["stock_based_compensation"].value
        fcf_sbc = fcf - sbc if not (math.isnan(fcf) or math.isnan(sbc)) else float("nan")
        year_metrics["fcf_sbc"] = Metric("fcf_sbc", fcf_sbc, currency, currency, fy)

        result.years[fy] = year_metrics
    return result


class DataLoader:
    def __init__(self, client: FMPClient, years: int = YEARS) -> None:
        self.client = client
        self.years = years

    def load_ticker(self, ticker: str) -> TickerMetrics:
        """Laedt Rohdaten fuer einen Ticker und extrahiert die Kennzahlen."""
        income = self.client.income_statements(ticker, years=self.years)
        balance = self.client.balance_sheets(ticker, years=self.years)
        cashflow = self.client.cash_flows(ticker, years=self.years)
        profile = self.client.profile(ticker)
        return extract_metrics(ticker, income, balance, cashflow, profile)
