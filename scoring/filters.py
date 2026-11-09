"""Harte Ausschlussfilter vor dem Scoring (CLAUDE.md). Jeder Filter nennt den Grund."""

from __future__ import annotations

import math

from data.loader import TickerMetrics


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


# Kernkennzahlen: ein Jahr zaehlt als "mit Wert", wenn mindestens eine davon
# nicht NaN ist (v29: Historie zaehlt Werte, nicht vorhandene Spalten).
_CORE_KEYS = ("revenue", "ebit", "net_income", "operating_cashflow", "equity")


def count_value_years(m: TickerMetrics) -> int:
    """Jahre mit tatsaechlichen Werten (nicht NaN), nicht Spalten."""
    n = 0
    for fy in m.years:
        ym = m.years[fy]
        if any(k in ym and not ym[k].is_na() for k in _CORE_KEYS):
            n += 1
    return n


def check_filters(m: TickerMetrics, cfg: dict) -> list[str]:
    """Gibt Liste der Ausschlussgruende zurueck; leer = nicht ausgeschlossen.

    Fehlende Daten schliessen NICHT aus (nur der Value-Trap-Filter braucht
    dafuer CAGR; fehlend -> erst im Scoring als n/a markiert).
    """
    reasons = []
    # Marktkapitalisierung ist in absoluten USD (FMP profile.mktCap, kein Mio.-Skalierung);
    # Schwellen in cfg sind daher absolute USD (config: filters.min_market_cap_usd).
    if m.market_cap is not None and not m.market_cap.is_na():
        if m.market_cap.value < cfg["min_market_cap_usd"]:
            reasons.append(
                f"Marktkap. {m.market_cap.value / 1e9:.2f} Mrd. USD "
                f"< {cfg['min_market_cap_usd'] / 1e9:.1f} Mrd. USD"
            )
    else:
        reasons.append("Marktkapitalisierung unbekannt")

    # Jahre mit tatsaechlichen Werten zaehlen, nicht vorhandene Spalten
    n_val_years = count_value_years(m)
    if n_val_years < cfg["min_history_years"]:
        reasons.append(f"Historie {n_val_years} J. mit Werten < {cfg['min_history_years']} J.")

    fcf_vals = [m.years[fy]["fcf"].value for fy in m.years
                if "fcf" in m.years[fy] and not m.years[fy]["fcf"].is_na()]
    fcf_vals = [v for v in fcf_vals if v == v]
    if len(fcf_vals) >= 5 and _avg(fcf_vals[-5:]) < cfg["min_avg_fcf_5y_usd"]:
        reasons.append("Negativer Durchschnitts-FCF (5 J.)")

    # negativer EK schliesst nur aus, wenn exclusion.negative_equity: true
    # (alte Verhalten). Sonst wird der Ticker normal bewertet + Flag "ek_negativ".
    if bool(cfg.get("exclusion", {}).get("negative_equity", False)):
        latest_equity = m.latest("equity")
        if latest_equity is not None and not latest_equity.is_na() and latest_equity.value < cfg["min_equity_usd"]:
            reasons.append(f"Eigenkapital negativ ({latest_equity.value / 1e9:.2f} Mrd.)")

    rev = [m.years[fy]["revenue"].value for fy in sorted(m.years)
           if "revenue" in m.years[fy] and not m.years[fy]["revenue"].is_na()]
    rev = [v for v in rev if v == v]
    if len(rev) >= 6 and rev[-1] > 0:
        cagr = (rev[-1] / rev[0]) ** (1 / (len(rev) - 1)) - 1
        if cagr < cfg["min_revenue_cagr_5y"]:
            reasons.append(f"Umsatz-CAGR {cagr:.1%} < {cfg['min_revenue_cagr_5y']:.0%} (Value-Trap)")

    return reasons
