"""Abgeleitete Kennzahlen mit dokumentierten Formeln.

Formeln (Kapitalbasis jeweils Ende des Geschaeftsjahres):
- NOPAT = EBIT * (1 - Steuersatz); Steuersatz = Steueraufwand / Vorsteuergewinn,
  begrenzt auf 0-35 % (config.yaml `data.tax_rate_min/max`); ausserhalb davon
  und bei Vorsteuerverlust gilt der Normsteuersatz (`data.tax_rate_norm`, 21 %).
  Bei EBIT <= 0 ist NOPAT = EBIT (kein Steuervorteil im Verlustjahr).
- Investiertes Kapital = Eigenkapital + Nettoverschuldung.
- ROIC = NOPAT / investiertes Kapital.
- FCF-Yield = FCF / Marktkapitalisierung.
- ROE = Nettogewinn / Eigenkapital (Banken-Ersatzkennzahl).

Fehlende Eingangsdaten -> NaN, nie 0 (CLAUDE.md).
"""

from __future__ import annotations

import math

from .loader import TickerMetrics

_TAX_CFG = None


def _tax_bounds() -> tuple[float, float, float]:
    """(tax_rate_min, tax_rate_max, tax_rate_norm) aus config.yaml (Block `data`)."""
    global _TAX_CFG
    if _TAX_CFG is None:
        try:
            from config import load_config
            d = load_config().get("data", {})
        except Exception:
            d = {}
        _TAX_CFG = (
            float(d.get("tax_rate_min", 0.0)),
            float(d.get("tax_rate_max", 0.35)),
            float(d.get("tax_rate_norm", 0.21)),
        )
    return _TAX_CFG


class _NoFX:
    """Fallback-Konverter ohne FX: Konvertierung nur bei gleicher Waehrung."""

    def convert(self, value: float, from_cur: str, to_cur: str, year: int) -> float:
        if from_cur.upper() == to_cur.upper():
            return value
        return float("nan")


def _nan(*vals: float) -> bool:
    return any(math.isnan(v) for v in vals)


def tax_rate(m: TickerMetrics, fy: int) -> float:
    """Effektiver Steuersatz = Steueraufwand / Vorsteuergewinn.

    begrenzt auf `data.tax_rate_min`..`data.tax_rate_max` (0-35 %).
    Ausserhalb davon und bei Vorsteuerverlust (pretax <= 0) gilt der
    Normsteuersatz `data.tax_rate_norm` (21 %). Fehlende Daten -> NaN.
    """
    y = m.years.get(fy, {})
    tax = y.get("income_tax_expense")
    pretax = y.get("income_before_tax")
    if tax is None or pretax is None:
        return float("nan")
    t, p = tax.value, pretax.value
    if _nan(t, p) or p == 0:
        return float("nan")
    t_min, t_max, t_norm = _tax_bounds()
    eff = t / p
    if _nan(eff) or eff < t_min or eff > t_max or p <= 0:
        return t_norm
    return eff


def nopat(m: TickerMetrics, fy: int) -> float:
    """NOPAT = EBIT * (1 - Steuersatz).

    Bei EBIT <= 0 ist NOPAT = EBIT (kein Steuervorteil im Verlustjahr),
    damit gilt immer NOPAT <= EBIT fuer EBIT > 0 (Steuersatz >= 0 begrenzt).
    """
    ebit = m.years.get(fy, {}).get("ebit")
    if ebit is None:
        return float("nan")
    if _nan(ebit.value):
        return float("nan")
    if ebit.value <= 0:
        return ebit.value
    t = tax_rate(m, fy)
    if _nan(t):
        return float("nan")
    return ebit.value * (1 - t)


def invested_capital(m: TickerMetrics, fy: int) -> float:
    """Investiertes Kapital = Eigenkapital + Nettoverschuldung (Jahresende)."""
    eq = m.years.get(fy, {}).get("equity")
    nd = m.years.get(fy, {}).get("net_debt")
    if eq is None or nd is None or _nan(eq.value, nd.value):
        return float("nan")
    return eq.value + nd.value


def invested_capital_ek_le0(m: TickerMetrics, fy: int) -> float:
    """Nenner bei Eigenkapital <= 0: investiertes Kapital = Schulden +
    Eigenkapital (statt nur EK + ND, das bei negativem EK klein/negativ waere).
    Auch das <= 0 -> NaN (ROIC dann n/a)."""
    y = m.years.get(fy, {})
    eq = y.get("equity")
    td = y.get("_total_debt")
    nd = y.get("net_debt")
    debt = None
    if td is not None and not td.is_na():
        debt = td.value
    elif nd is not None and not nd.is_na():
        debt = nd.value  # Naeherung: ND ~ Schulden - Cash
    if eq is None or eq.is_na() or debt is None or _nan(eq.value, debt):
        return float("nan")
    ic = debt + eq.value
    if ic <= 0:
        return float("nan")
    return ic


def roic(m: TickerMetrics, fy: int) -> float:
    """ROIC = NOPAT / investiertes Kapital (Jahresende).

    bei Eigenkapital <= 0 ist EK + ND als Nenner verzerrt; dann wird
    Schulden + Eigenkapital verwendet (invested_capital_ek_le0); auch das <= 0
    -> NaN."""
    y = m.years.get(fy, {})
    eq = y.get("equity")
    if eq is not None and not eq.is_na() and eq.value <= 0:
        ic = invested_capital_ek_le0(m, fy)
    else:
        ic = invested_capital(m, fy)
    np_ = nopat(m, fy)
    if _nan(ic, np_) or ic == 0:
        return float("nan")
    return np_ / ic


def fcf_yield(m: TickerMetrics, fy: int, fx=None) -> float:
    """FCF-Yield = FCF / Marktkapitalisierung (Marktkap. zum FX-End).

    Waehrungskonsistent: FMP liefert /profile (mktCap) in USD, Statements
    in reportedCurrency. Bei Mismatch wird der FCF zum historyischen
    Jahresendkurs in die Marktkap.-Waehrung konvertiert. Ohne fx-Converter
    oder ohne Kurs -> NaN.

    Fuer die Anzeige gibt es fcf_yield_raw (zusatzliche Spalte, unkonvertiert).
    """
    fcf = m.years.get(fy, {}).get("fcf")
    if fcf is None or m.market_cap is None or _nan(fcf.value, m.market_cap.value):
        return float("nan")
    if m.market_cap.value == 0:
        return float("nan")
    converter = fx or _NoFX()
    fcf_conv = converter.convert(fcf.value, fcf.currency, m.market_cap.currency, fy)
    if _nan(fcf_conv):
        return float("nan")
    return fcf_conv / m.market_cap.value


def fcf_yield_sbc(m: TickerMetrics, fy: int, fx=None) -> float:
    """FCF-Yield nach SBC = (FCF - Aktienverguetung) / Marktkapitalisierung.

    Wie fcf_yield(), aber auf fcf_sbc (FCF abzuiglich SBC aus dem
    Cashflow-Statement). Fehlende SBC oder fehlender FCF -> NaN, nie 0.
    """
    fcf = m.years.get(fy, {}).get("fcf_sbc")
    if fcf is None or m.market_cap is None or _nan(fcf.value, m.market_cap.value):
        return float("nan")
    if m.market_cap.value == 0:
        return float("nan")
    converter = fx or _NoFX()
    fcf_conv = converter.convert(fcf.value, fcf.currency, m.market_cap.currency, fy)
    if _nan(fcf_conv):
        return float("nan")
    return fcf_conv / m.market_cap.value


def fcf_yield_raw(m: TickerMetrics, fy: int) -> float:
    """FCF-Yield ohne Waehrung, nur fuer Anzeige als zusaetzliche Spalte,
    nicht fuer Scoring verwenden.
    """
    fcf = m.years.get(fy, {}).get("fcf")
    if fcf is None or m.market_cap is None or _nan(fcf.value, m.market_cap.value):
        return float("nan")
    if m.market_cap.value <= 0:
        return float("nan")
    return fcf.value / m.market_cap.value


def roe(m: TickerMetrics, fy: int) -> float:
    """ROE = Nettogewinn / Eigenkapital (Banken-Ersatz fuer ROIC).

    Eigenkapital <= 0 -> NaN (Jahr wird n/a, nicht 0 Punkte)."""
    ni = m.years.get(fy, {}).get("net_income")
    eq = m.years.get(fy, {}).get("equity")
    if ni is None or eq is None or _nan(ni.value, eq.value) or eq.value <= 0:
        return float("nan")
    return ni.value / eq.value
