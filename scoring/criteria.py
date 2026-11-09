"""Kriterien-Berechnung fuer den Value-Score (alle Schwellen in config.yaml).

Je Kriterium eine Funktion, die ein CriterionResult zurueckgibt:
- value: Kennzahl (NaN wenn nicht berechenbar)
- is_na: Datenbasis zu duenn (config: data.min_points_*)
- years_available: Jahre mit verwertbaren Eingangsdaten
- thin: True wenn years_available < data.thin_years -> "Daten duenn"

Historienfenster: 5 Jahre (config: data.years). CAGR ueber 5 Punkte
(= Wachstum ueber 4 Jahre innerhalb des 5-J-Fensters).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from data.loader import TickerMetrics
from data.metrics import fcf_yield_sbc, roic, roe
from scoring.sectors import sector_rule, normalized_net_income, price_to_book, dividend_yield


@dataclass
class CriterionResult:
    value: float
    is_na: bool
    years_available: int = 0
    thin: bool = False
    years_total: int = 0  # Fensterlaenge (Basis der Kennzahl), z. B. "4 / 4" bei fcf_positive_years


Calc = Callable[[TickerMetrics, dict, Any], CriterionResult]


def _rule(m: TickerMetrics, cfg) -> object:
    """Sektorregel (Industry-basiert) je Ticker; gecacht auf dem Objekt selbst
    (id()-basierter Cache ist unsicher: id() wird nach Garbage Collection neu
    vergeben und wuerde falsche Regeln liefern)."""
    cached = m.__dict__.get("_sector_rule_cache")
    if cached is None:
        cached = sector_rule(m, cfg)
        m.__dict__["_sector_rule_cache"] = cached
    return cached


def _special(m: TickerMetrics, cfg) -> bool:
    """Sonderregel-Gruppe (Banken/Versicherer/REITs/Versorger als
    Industry-Pattern) -- DCF und FCF-Yield werden nicht berechnet; die
    betroffenen Kriterien sind n/a."""
    return _rule(m, cfg).group == "special"


def _values(metrics: TickerMetrics, name: str) -> list[tuple[int, float]]:
    """[(Jahr, Wert)] chronologisch, ohne NaN."""
    out = []
    for m in metrics.series(name):
        if not m.is_na():
            out.append((m.fiscal_year, m.value))
    return out


def _year_count(metrics: TickerMetrics, *names: str) -> int:
    """Jahre, in denen ALLE genannten Kennzahlen einen Wert haben."""
    counts = [len(_values(metrics, n)) for n in names]
    return min(counts) if counts else 0


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _cagr(values: list[float]) -> float:
    """Jaehrliche Wachstumsrate ueber n-1 Jahre (chronologisch)."""
    if len(values) < 2 or values[0] <= 0:
        return float("nan")
    n = len(values) - 1
    return (values[-1] / values[0]) ** (1 / n) - 1


def _slope(values: list[float]) -> float:
    """Steigung pro Jahr via kleinste Quadrate; Index = Jahr (0, 1, 2, ...)."""
    n = len(values)
    if n < 2:
        return float("nan")
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else float("nan")


def _result(value: float, is_na: bool, years_available: int, thin_years: int) -> CriterionResult:
    return CriterionResult(
        value=value,
        is_na=is_na,
        years_available=years_available,
        thin=years_available < thin_years,
    )


# --- Kriterien ---

def calc_roic_avg5(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    thin = cfg["thin_years"]
    # Banken/Versicherer nutzen ROE statt ROIC
    if _rule(m, cfg).group == "financials":
        return _result(float("nan"), True, 0, thin)
    # Sonderregel (Banken/Versicherer/REITs/Versorger) -> ROIC n/a
    if _special(m, cfg):
        return _result(float("nan"), True, 0, thin)
    years = _year_count(m, "ebit")
    vals = [roic(m, fy) for fy, _ in _values(m, "ebit")]
    vals = [v for v in vals if v == v]
    if len(vals) < cfg["min_points_5y"]:
        return _result(float("nan"), True, years, thin)
    return _result(_avg(vals[-5:]), False, years, thin)


def calc_roe_avg5(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    thin = cfg["thin_years"]
    years = _year_count(m, "net_income")
    # Jahre mit Eigenkapital <= 0 liefern roe()=NaN -> werden nicht als
    # verfuegbar gezaehlt (n/a statt 0 Punkte); EK<=0-Jahre zaehlen nicht zu `years`.
    vals = [roe(m, fy) for fy, _ in _values(m, "net_income")]
    vals = [v for v in vals if v == v]
    years = min(years, len(vals)) if vals else years
    if len(vals) < cfg["min_points_5y"]:
        return _result(float("nan"), True, years, thin)
    return _result(_avg(vals[-5:]), False, years, thin)


def calc_net_debt_ebitda(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    thin = cfg["thin_years"]
    # Banken/Versicherer nutzen P/B statt ND/EBITDA
    if _rule(m, cfg).group == "financials":
        return _result(float("nan"), True, 0, thin)
    # Sonderregel -> ND/EBITDA n/a (DCF/FCF nicht berechnet)
    if _special(m, cfg):
        return _result(float("nan"), True, 0, thin)
    # EBITDA unplausibel (EBITDA < EBIT) -> ND/EBITDA als n/a
    if getattr(m, "ebitda_unplausible", False):
        return _result(float("nan"), True, 0, thin)
    years = _year_count(m, "net_debt", "ebitda")
    nd = [v for _, v in _values(m, "net_debt")]
    eb = [v for _, v in _values(m, "ebitda")]
    if not nd or not eb:
        return _result(float("nan"), True, years, thin)
    return _result(nd[-1] / eb[-1] if eb[-1] else float("nan"), False, years, thin)


def calc_interest_coverage(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    thin = cfg["thin_years"]
    years = _year_count(m, "ebit", "interest_expense")
    ebit = [v for _, v in _values(m, "ebit")]
    interest = [v for _, v in _values(m, "interest_expense")]
    if not ebit or not interest:
        return _result(float("nan"), True, years, thin)
    return _result(ebit[-1] / abs(interest[-1]) if interest[-1] else float("nan"), False, years, thin)


def calc_fcf_positive_years(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Anteil der Jahre mit FCF > 0 an den VERFUEGBAREN Jahren (mindestens 4).

    Weniger als 10 verfuegbare Jahre werden nicht gegen 10 bewertet, sondern
    gegen die tatsaechliche Anzahl (z. B. 4/4 -> 100 %). Anzeige als "4 / 4".
    Volle Punkte bei 100 %, null Punkte bei <= 50 % (Schwellen in config.yaml).
    Banken/Versicherer nutzen Dividendenrendite statt FCF.
    """
    thin = cfg["thin_years"]
    if _rule(m, cfg).group == "financials":
        return _result(float("nan"), True, 0, thin)
    # Sonderregel -> FCF-Kriterien n/a (FCF-Yield nicht berechnet)
    if _special(m, cfg):
        return _result(float("nan"), True, 0, thin)
    years = _year_count(m, "fcf")
    vals = [v for _, v in _values(m, "fcf")]
    if len(vals) < cfg["fcf_pos_min_years"]:
        return _result(float("nan"), True, years, thin)
    share = sum(1 for v in vals if v > 0) / len(vals)
    res = _result(share, False, years, thin)
    res.years_total = len(vals)
    return res


def calc_fcf_margin_avg5(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    thin = cfg["thin_years"]
    # Banken/Versicherer nutzen Dividendenrendite statt FCF-Marge
    if _rule(m, cfg).group == "financials":
        return _result(float("nan"), True, 0, thin)
    # Sonderregel -> FCF-Marge n/a
    if _special(m, cfg):
        return _result(float("nan"), True, 0, thin)
    years = _year_count(m, "fcf", "revenue")
    fcf = dict(_values(m, "fcf"))
    rev = dict(_values(m, "revenue"))
    margins = [v / rev[fy] for fy, v in fcf.items() if fy in rev and rev[fy] != 0]
    if len(margins) < cfg["min_points_5y"]:
        return _result(float("nan"), True, years, thin)
    return _result(_avg(margins[-5:]), False, years, thin)


def calc_gross_margin_trend(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Bruttomarge-Trend: Steigung in Prozentpunkten pro Jahr (Hestoer 5 J.).

    CLAUDE.md: 'steigend' -> volle Punkte, 'stark fallend' -> Null Punkte;
    Schwellen in config.yaml (full: 0.0, zero: -0.5) beziehen sich auf
    Prozentpunkte pro Jahr: -0.5 PP/Jahr = stark fallend.
    """
    thin = cfg["thin_years"]
    years = _year_count(m, "gross_profit", "revenue")
    gp = dict(_values(m, "gross_profit"))
    rev = dict(_values(m, "revenue"))
    margins = [gp[fy] / rev[fy] for fy in sorted(set(gp) & set(rev)) if rev[fy] != 0]
    if len(margins) < cfg["trend_min_points"]:
        return _result(float("nan"), True, years, thin)
    return _result(_slope(margins) * 100, False, years, thin)


def calc_revenue_cagr_5y(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Umsatz-CAGR ueber die verfuegbaren Jahre (mindestens 4 Punkte = 3 Intervalle).

    Konsistent mit der CAGR im DCF (valuation/dcf.py): dort wird ebenfalls die
    CAGR ueber alle verfuegbaren Jahre gebildet. Die tatsaechliche Fensterlaenge
    ist in years_available sichtbar.
    """
    thin = cfg["thin_years"]
    years = _year_count(m, "revenue")
    vals = [v for _, v in _values(m, "revenue")]
    if len(vals) < cfg["cagr_min_points"]:
        return _result(float("nan"), True, years, thin)
    return _result(_cagr(vals), False, years, thin)


def calc_fcf_yield_sbc_avg3(m: TickerMetrics, cfg, fx) -> CriterionResult:
    """FCF-Yield nach SBC im 3-Jahres-Durchschnitt.

    FCF-SBC = FCF - Aktienverguetung (Cashflow-Statement). Loest den Roh-FCF-Yield
    im Value-Score ab; fehlende SBC -> NaN (nie 0) -> Kriterium n/a.
    """
    thin = cfg["thin_years"]
    years = _year_count(m, "fcf_sbc")
    if m.market_cap is None or m.market_cap.is_na():
        return _result(float("nan"), True, years, thin)
    # Sonderregel -> FCF-Yield wird nicht berechnet -> n/a
    if _special(m, cfg):
        return _result(float("nan"), True, years, thin)
    # REITs nutzen FFO-Yield statt FCF-Yield (FFO ist massgeblich)
    if _rule(m, cfg).group == "reits":
        return calc_ffo_yield(m, cfg, fx)
    fcf = _values(m, "fcf_sbc")
    yields = []
    for fy, v in fcf[-3:]:
        y = fcf_yield_sbc(m, fy, fx=fx)
        if y == y:
            yields.append(y)
    if len(yields) < cfg["min_points_3y"]:
        return _result(float("nan"), True, years, thin)
    return _result(_avg(yields), False, years, thin)


def calc_mos_base(m: TickerMetrics, cfg, fx) -> CriterionResult:
    """Margin of Safety (Base) aus dem DCF (valuation/dcf.py).

    MoS (Base) = 1 - Kurs / innerer Wert je Aktie (Base-Szenario).
    Ohne DCF (kein FCF nach SBC, keine Marktkapitalisierung, kein Kurs) -> NaN.
    """
    from valuation.dcf import compute_dcf
    thin = cfg["thin_years"]
    # Sonderregel -> DCF wird nicht berechnet -> MoS n/a
    if _special(m, cfg):
        return _result(float("nan"), True, len(m.years), thin)
    dcf = compute_dcf(m, {"dcf": _dcf_cfg_for_criteria()}, fx=fx)
    mos = dcf.margin_of_safety
    years = len(m.years)
    if mos != mos:
        return _result(float("nan"), True, years, thin)
    return _result(mos, False, years, thin)


def _dcf_cfg_for_criteria() -> dict:
    """Minimal-Config fuer compute_dcf aus der globalen Config laden."""
    try:
        from config import load_config
        return load_config()["dcf"]
    except Exception:
        return {
            "discount_rate": 0.09, "terminal_growth": 0.025,
            "base_growth_cap": 0.06, "bull_growth_multiplier": 1.3,
            "bull_growth_cap": 0.10, "fade_to_growth": 0.025,
        }


def calc_ev_ebit_vs_median(m: TickerMetrics, cfg, fx) -> CriterionResult:
    """Aktuelles EV/EBIT im Verhaeltnis zum eigenen Median (Fenster 5 J.).

    EV = Marktkap. + Nettoverschuldung (Marktkap. in Quote-Waehrung,
    Nettoverschuldung in Berichtswaehrung -> FX-Konvertierung noetig;
    ohne Kurs n/a).
    """
    thin = cfg["thin_years"]
    years = _year_count(m, "ebit", "net_debt")
    if m.market_cap is None or m.market_cap.is_na():
        return _result(float("nan"), True, years, thin)
    ratios = []
    for fy in sorted(m.years, reverse=True):
        ebit = m.years[fy].get("ebit")
        nd = m.years[fy].get("net_debt")
        if ebit is None or nd is None or ebit.is_na() or nd.is_na() or ebit.value <= 0:
            continue
        nd_conv = nd.value if nd.currency == m.market_cap.currency else (
            fx.convert(nd.value, nd.currency, m.market_cap.currency, fy) if fx else float("nan")
        )
        if nd_conv != nd_conv:
            continue
        ev = m.market_cap.value + nd_conv
        ratios.append((fy, ev / ebit.value))
    if len(ratios) < cfg["min_points_5y"]:
        return _result(float("nan"), True, years, thin)
    current_fy = max(fy for fy, _ in ratios)
    current = next(r for fy, r in ratios if fy == current_fy)
    median_vals = sorted(r for _, r in ratios)
    mid = len(median_vals) // 2
    median = median_vals[mid] if len(median_vals) % 2 else (median_vals[mid - 1] + median_vals[mid]) / 2
    if median <= 0:
        return _result(float("nan"), True, years, thin)
    return _result(current / median, False, years, thin)


# --- v28: Sektor-Kriterien (Banken/Versicherer: ROE, P/B, Dividendenrendite;
# Zykliker: normalisierter Gewinn; REITs: FFO-Hinweis) -----------------------

def calc_roe_avg5_financials(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Banken/Versicherer: ROE statt ROIC (ROE ist bei Banken aussagekraeftiger)."""
    return calc_roe_avg5(m, cfg, _fx)


def calc_price_to_book(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Banken/Versicherer: P/B (Kurs / Buchwert je Aktie) statt ND/EBITDA."""
    thin = cfg["thin_years"]
    pkb = price_to_book(m)
    years = _year_count(m, "equity")
    if pkb != pkb:
        return _result(float("nan"), True, years, thin)
    return _result(pkb, False, years, thin)


def calc_dividend_yield(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Banken/Versicherer: Dividendenrendite (Dezimal)."""
    thin = cfg["thin_years"]
    dy = dividend_yield(m)
    years = _year_count(m, "net_income")
    if dy != dy:
        return _result(float("nan"), True, years, thin)
    return _result(dy, False, years, thin)


def calc_normalized_earnings(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """Zykliker: Nettogewinn ueber bis zu 7 Jahre normalisiert (Ø je Aktie).

    Gewinne ueber die verfuegbaren Jahre (max. `sector_rules.groups.cyclicals.
    normalize_years`, Default 7) gleitend gemittelt; reicht die Historie nicht,
    wird mit weniger Jahren gerechnet und das Kriterium als 'Daten duenn'
    markiert (Flag).
    """
    thin = cfg["thin_years"]
    rule = _rule(m, cfg)
    norm_ni, years_used = normalized_net_income(m, rule)
    shares = m.latest("shares_diluted")
    per_share = norm_ni / shares.value if (norm_ni == norm_ni and shares is not None
                                         and not shares.is_na() and shares.value) else float("nan")
    want = max(1, rule.normalized_earnings_years)
    is_thin = years_used < want  # Historie reicht nicht -> Flag
    res = _result(per_share, per_share != per_share, years_used, thin)
    res.thin = is_thin
    return res


def calc_ffo_yield(m: TickerMetrics, cfg, _fx) -> CriterionResult:
    """REITs: FFO-Hinweis statt FCF-Yield (FFO je Aktie / Kurs; NaN wenn kein FFO)."""
    from scoring.sectors import ffo_per_share
    thin = cfg["thin_years"]
    ffo = ffo_per_share(m)
    years = _year_count(m, "fcf")
    if ffo != ffo or m.market_cap is None or m.market_cap.is_na() or m.market_cap.value <= 0:
        return _result(float("nan"), True, years, thin)
    shares = m.latest("shares_diluted")
    if shares is None or shares.is_na() or shares.value <= 0:
        return _result(float("nan"), True, years, thin)
    price = m.market_cap.value / shares.value
    return _result(ffo / price if price else float("nan"), False, years, thin)


# Standard-Kriterien (alle Sektorgruppen)
CRITERIA: dict[str, Calc] = {
    "roic_avg5": calc_roic_avg5,
    "roe_avg5": calc_roe_avg5,
    "net_debt_ebitda": calc_net_debt_ebitda,
    "interest_coverage": calc_interest_coverage,
    "fcf_positive_years": calc_fcf_positive_years,
    "fcf_margin_avg5": calc_fcf_margin_avg5,
    "gross_margin_trend": calc_gross_margin_trend,
    "revenue_cagr_5y": calc_revenue_cagr_5y,
    "fcf_yield_sbc_avg3": calc_fcf_yield_sbc_avg3,
    "mos_base": calc_mos_base,
    "ev_ebit_vs_median": calc_ev_ebit_vs_median,
}

# Sektor-spezifische Kriterien (werden je Gruppe in active_criteria aktiv)
SECTOR_CRITERIA: dict[str, Calc] = {
    "roe_avg5_financials": calc_roe_avg5_financials,   # Banken/Versicherer
    "price_to_book": calc_price_to_book,               # Banken/Versicherer
    "dividend_yield": calc_dividend_yield,             # Banken/Versicherer
    "normalized_earnings": calc_normalized_earnings,   # Zykliker (7-J.-Norm.)
    "ffo_yield": calc_ffo_yield,                       # REITs (FFO-Hinweis)
}


def active_criteria(m: TickerMetrics, cfg: dict) -> dict[str, Calc]:
    """aktive Kriterien je Sektorgruppe.

    Banken/Versicherer: ROE, P/B, Dividendenrendite statt ROIC, FCF, ND/EBITDA
    (diese werden in den Calc-Funktionen per Sektorregel als n/a geliefert;
    zusaetzlich die Finanz-Kriterien aktivieren).
    Zykliker: zusaeztlich normalisierter Gewinn (7-Jahres-Normalisierung).
    REITs: zusaeztlich FFO-Yield (statt FCF-Yield, siehe calc_fcf_yield_sbc_avg3).
    """
    from scoring.sectors import sector_rule
    group = sector_rule(m, cfg).group
    crits = dict(CRITERIA)
    # Sonderregel-Gruppe -> nur Standard-Kriterien; die nicht anwendbaren
    # (ROIC, ND/EBITDA, FCF-*, FCF-Yield, MoS) liefern n/a (siehe Calc-Funktionen)
    if group == "special":
        return crits
    if group == "financials":
        for name in ("roe_avg5_financials", "price_to_book", "dividend_yield"):
            crits[name] = SECTOR_CRITERIA[name]
    elif group == "cyclicals":
        crits["normalized_earnings"] = SECTOR_CRITERIA["normalized_earnings"]
    elif group == "reits":
        crits["ffo_yield"] = SECTOR_CRITERIA["ffo_yield"]
    return crits


def compute_criteria(m: TickerMetrics, cfg: dict, fx=None) -> dict[str, CriterionResult]:
    data_cfg = cfg.get("data", {})
    calc_cfg = {
        "thin_years": data_cfg.get("thin_years", 5),
        "min_points_5y": data_cfg.get("min_points_5y", 3),
        "min_points_3y": data_cfg.get("min_points_3y", 2),
        "min_points_10y": data_cfg.get("min_points_10y", 5),
        "cagr_min_points": data_cfg.get("cagr_min_points", 4),
        "fcf_pos_min_years": data_cfg.get("fcf_pos_min_years", 4),
        "trend_min_points": data_cfg.get("trend_min_points", 4),
        # Sektorregeln (Industry) an die Calc-Funktionen durchreichen
        "sector_rules": cfg.get("sector_rules", {}),
    }
    crits = active_criteria(m, cfg)
    return {name: calc(m, calc_cfg, fx) for name, calc in crits.items()}
