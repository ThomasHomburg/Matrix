"""TTM (Trailing Twelve Months) aus Quartalsdaten.

Quartalsdaten (yfinance quarterly_*): Flussgroessen (Umsatz, EBIT, OCF, Capex,
FCF, SBC, Steuern ...) werden als Summe der letzten 4 Quartale gebildet;
Bestandsgroessen (Eigenkapital, Nettoverschuldung, Cash ...) stammen aus dem
neuesten Quartal (Stichtag).

Plausibilitaetspruefung: Ist das letzte Quartal ein Geschaeftsjahresende
(Q4), muss die Summe der letzten 4 Quartale nahe am gemeldeten Jahreswert
liegen (Abweichung < 2 %). Sonst Flag "Quartalsdaten unplausibel" und
Rueckfall auf die Geschaeftsjahre (Jahresdaten). Liegt das letzte Quartal
MITTLERERHALB eines Geschaeftsjahres (Q1-Q3), ist der Jahresvergleich nicht
durchfuehrbar -- dann TTM direkt verwenden (kein Rueckfall), weil die
Quartalsdaten die aktuellste verfuegbare Sicht sind.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from data.loader import Metric, TickerMetrics

# Flussgroessen: Summe letzte 4 Quartale. Bestands- und Hilfsgroessen: Stichtag.
FLOW_KEYS = (
    "revenue", "gross_profit", "ebit", "ebitda", "net_income",
    "interest_expense", "income_tax_expense", "income_before_tax",
    "operating_cashflow", "capex", "stock_based_compensation", "fcf", "fcf_sbc",
)
STOCK_KEYS = ("equity", "net_debt", "_total_debt", "_cash", "_short_term_investments",
              "_restricted_cash", "customer_deposits")

TTM_TOLERANCE = 0.02  # 2 % Abweichung zum Jahreswert


@dataclass
class TtmResult:
    """TTM-Kennzahlen eines Tickers + Plausibilitaet."""

    ttm: dict[str, Metric] = field(default_factory=dict)   # Kennzahl -> TTM-Metric
    quarter_end: str = ""        # Stichtag des letzten Quartals (ISO)
    base_label: str = ""         # z. B. "TTM bis 2026-08"
    plausible: bool = True
    deviation: float = float("nan")  # max. relative Abweichung vs. Jahreswert
    fall_back_to_annual: bool = False
    checks: list[str] = field(default_factory=list)  # pro Kennzahl Abweichung
    # die letzten Quartale einzeln (Scorecard-Anzeige) -- v30: 8 Quartale
    # (4 aktuelle + 4 Vorjahresquartale fuer YoY). quarters[0] = neuestes Quartal;
    # Werte: {"operating_cashflow", "capex", "stock_based_compensation",
    # "fcf_sbc", "yoy"} in Berichtswährung; yoy = FCF-SBC vs. Vorjahresquartal.
    quarters: list = field(default_factory=list)  # [{quarter_end, ocf, capex, sbc, fcf_sbc, yoy}]
    quarters_currency: str = ""


def _sum_last4(frames: dict, row_labels: dict[str, list[str]], key: str) -> tuple[float, list]:
    """Summe der letzten 4 Quartale einer Flussgroesse; (wert, [quartalswerte])."""
    df = frames.get(key)
    if df is None or df.empty:
        return float("nan"), []
    labels = row_labels.get(key, [])
    cols = sorted(df.columns, reverse=True)
    vals, used = [], []
    for c in cols:
        v = float("nan")
        for label in labels:
            if label in df.index:
                cell = df.loc[label, c]
                if cell is not None and not (isinstance(cell, float) and math.isnan(cell)):
                    v = float(cell)
                    break
        if v == v:
            vals.append(v)
            used.append(c)
        if len(vals) >= 4:
            break
    if len(vals) < 4:
        return float("nan"), vals
    return sum(vals), vals


def _stock_value(df, labels: list[str], col) -> float:
    if df is None or df.empty or col not in df.columns:
        return float("nan")
    for label in labels:
        if label in df.index:
            cell = df.loc[label, col]
            if cell is not None and not (isinstance(cell, float) and math.isnan(cell)):
                return float(cell)
    return float("nan")


def compute_ttm(m: TickerMetrics, quarterly: dict) -> TtmResult:
    """Berechnet TTM-Kennzahlen aus Quartalsdaten (yfinance quarterly_*).

    quarterly: {"income": df, "balance": df, "cashflow": df, "row_map": {...}}
    (row_map wie im yfinance-Provider). Fehlende Quartalsdaten -> TTM NaN.
    Plausibilitaet: TTM vs. letzter Jahreswert (Abweichung < 2 % je Kennzahl).
    """
    res = TtmResult()
    frames = {"income": quarterly.get("income"), "balance": quarterly.get("balance"),
              "cashflow": quarterly.get("cashflow")}
    row_map = quarterly.get("row_map", {})
    inc, bal, cf = frames["income"], frames["balance"], frames["cashflow"]
    if inc is None or inc.empty:
        res.plausible = False
        res.fall_back_to_annual = True
        res.checks.append("Quartalsdaten unplausibel: kein Income-Statement")
        return res

    cols = sorted(inc.columns, reverse=True)
    if not cols:
        res.plausible = False
        res.fall_back_to_annual = True
        res.checks.append("Quartalsdaten unplausibel: keine Quartale")
        return res
    last_q = cols[0]
    res.quarter_end = last_q.date().isoformat() if hasattr(last_q, "date") else str(last_q)
    res.base_label = f"TTM bis {res.quarter_end[:7]}"

    currency = m.market_cap.currency if m.market_cap else "USD"
    res.quarters_currency = currency
    fy_ttm = last_q.year

    # --- v26: die letzten Quartale einzeln (Scorecard-Anzeige); v30: 8 Quartale
    # (4 aktuelle + 4 Vorjahresquartale fuer YoY). Neuestes Quartal zuerst;
    # je Quartal OCF, Capex, SBC, FCF-SBC + YoY (FCF-SBC vs. Vorjahresquartal).
    cf_cols = sorted(cf.columns, reverse=True) if cf is not None and not cf.empty else []
    for c in cf_cols[:8]:
        q_end = c.date().isoformat() if hasattr(c, "date") else str(c)
        ocf_q = _stock_value(cf, row_map.get("operating_cashflow", []), c)
        capex_q = _stock_value(cf, row_map.get("capex", []), c)
        sbc_q = _stock_value(cf, row_map.get("stock_based_compensation", []), c)
        fcf_q = _stock_value(cf, row_map.get("fcf", []), c)
        if fcf_q != fcf_q and ocf_q == ocf_q and capex_q == capex_q:
            fcf_q = ocf_q + capex_q  # Capex bei yfinance negativ
        fcf_sbc_q = (fcf_q - sbc_q
                     if (fcf_q == fcf_q and sbc_q == sbc_q) else float("nan"))
        res.quarters.append({
            "quarter_end": q_end,
            "operating_cashflow": ocf_q,
            "capex": capex_q,
            "stock_based_compensation": sbc_q,
            "fcf_sbc": fcf_sbc_q,
            "yoy": float("nan"),  # wird unten gegen das Vorjahresquartal (i+4) gesetzt
        })
    # YoY je Quartal -- quarters[i] vs. quarters[i+4] (Vorjahresquartal)
    for i in range(len(res.quarters)):
        if i + 4 < len(res.quarters):
            cur = res.quarters[i]["fcf_sbc"]
            prev = res.quarters[i + 4]["fcf_sbc"]
            if cur == cur and prev == prev and prev != 0:
                res.quarters[i]["yoy"] = cur / prev - 1.0

    # --- Flussgroessen: Summe letzte 4 Quartale ---
    src_map = {
        "revenue": inc, "gross_profit": inc, "ebit": inc, "ebitda": inc,
        "net_income": inc, "interest_expense": inc, "income_tax_expense": inc,
        "income_before_tax": inc, "shares_diluted": inc,
        "operating_cashflow": cf, "capex": cf, "stock_based_compensation": cf,
    }
    for key in FLOW_KEYS:
        if key in ("fcf", "fcf_sbc"):
            continue  # werden unten abgeleitet (FCF bevorzugt gemeldet, sonst OCF+Capex)
        val, _qvals = _sum_last4({key: src_map.get(key)}, row_map, key)
        unit = "shares" if key == "shares_diluted" else "currency"
        res.ttm[key] = Metric(key, val, unit, currency, fy_ttm)

    # FCF TTM: gemeldeter 'Free Cash Flow' bevorzugt; sonst OCF + Capex (Capex negativ)
    fcf_rep, _ = _sum_last4({"fcf": cf}, row_map, "fcf")
    ocf_v = res.ttm["operating_cashflow"].value
    capex_v = res.ttm["capex"].value
    if fcf_rep == fcf_rep:
        fcf_ttm = fcf_rep
    elif ocf_v == ocf_v and capex_v == capex_v:
        fcf_ttm = ocf_v + capex_v
    else:
        fcf_ttm = float("nan")
    res.ttm["fcf"] = Metric("fcf", fcf_ttm, "currency", currency, fy_ttm)

    # FCF nach SBC = FCF - SBC (falls beide TTM vorhanden)
    fcf_v = res.ttm["fcf"].value
    sbc_v = res.ttm["stock_based_compensation"].value
    fcf_sbc = fcf_v - sbc_v if (fcf_v == fcf_v and sbc_v == sbc_v) else float("nan")
    res.ttm["fcf_sbc"] = Metric("fcf_sbc", fcf_sbc, "currency", currency, fy_ttm)

    # --- Bestandsgroessen: neuestes Quartal ---
    bal_cols = sorted(bal.columns, reverse=True) if bal is not None and not bal.empty else []
    bal_col = bal_cols[0] if bal_cols else None
    for key in STOCK_KEYS:
        labels = row_map.get(key, [])
        # Nettoverschuldung: gemeldet, sonst Total Debt - Cash
        if key == "net_debt":
            v = _stock_value(bal, labels, bal_col)
            if v != v:
                td = _stock_value(bal, row_map.get("_total_debt", []), bal_col)
                cash = _stock_value(bal, row_map.get("_cash", []), bal_col)
                if td == td and cash == cash:
                    v = td - cash
                elif td == td:
                    v = td
            res.ttm[key] = Metric(key, v, "currency", currency, fy_ttm)
        else:
            v = _stock_value(bal, labels, bal_col)
            res.ttm[key] = Metric(key, v, "currency", currency, fy_ttm)

    # Aktienanzahl: verwässert aus dem neuesten Quartal (Stichtag-nahe Näherung)
    sh = _stock_value(inc, row_map.get("shares_diluted", []), last_q)
    res.ttm["shares_diluted"] = Metric("shares_diluted", sh, "shares", currency, fy_ttm)

    # --- Plausibilitaet: nur pruefbar, wenn das letzte Quartal ein
    # Geschaeftsjahresende ist (Q4). Dann Summe letzte 4 Quartale ~ Jahreswert.
    # Bei Q1-Q3 (letztes Quartal mitten im GJ) ist kein Jahresvergleich moeglich
    # -> TTM direkt verwenden (aktuellste Sicht), kein Rueckfall.
    fy_end = m.fiscal_ends.get(fy_ttm)
    is_fy_end_quarter = bool(fy_end) and fy_end == res.quarter_end
    if not is_fy_end_quarter:
        res.checks.append(
            f"Letztes Quartal {res.quarter_end} ist kein Geschaeftsjahresende "
            f"(FY{fy_ttm} endet {fy_end or 'unbekannt'}) -> Jahresvergleich nicht "
            f"durchfuehrbar; TTM wird direkt verwendet (aktuellste Sicht).")
        return res
    annual = m.years.get(fy_ttm)
    max_dev = 0.0
    if annual:
        for key in ("revenue", "ebit", "ebitda", "net_income", "operating_cashflow",
                    "fcf", "fcf_sbc"):
            ttm_v = res.ttm[key].value
            ann = annual.get(key)
            ann_v = ann.value if ann is not None and not ann.is_na() else float("nan")
            if ttm_v == ttm_v and ann_v == ann_v and ann_v != 0:
                dev = abs(ttm_v - ann_v) / abs(ann_v)
                res.checks.append(f"{key}: TTM {ttm_v/1e9:,.2f} vs. FY{fy_ttm} "
                                  f"{ann_v/1e9:,.2f} -> {dev:.1%}")
                max_dev = max(max_dev, dev)
    res.deviation = max_dev
    if max_dev > TTM_TOLERANCE:
        res.plausible = False
        res.fall_back_to_annual = True
        res.checks.append(f"Quartalsdaten unplausibel: Abweichung {max_dev:.1%} > 2 % "
                          f"-> Rueckfall auf Geschaeftsjahre")
    return res


def ttm_metrics_into_years(m: TickerMetrics, ttm: TtmResult) -> TickerMetrics:
    """Schreibt die TTM-Kennzahlen als zusaetzliches 'Jahr' (TTM) in m.years.

    Das TTM-Jahr traegt die Jahresnummer des letzten Quartals und die Quelle
    'ttm:<quarter_end>'. Wird vom DCF als Start-FCF-Basis verwendet
    (Ø aus TTM + den zwei davor liegenden Geschäftsjahren).
    """
    if not ttm.ttm:
        return m
    fy = int(ttm.quarter_end[:4]) if ttm.quarter_end else max(m.years)
    ym = dict(ttm.ttm)
    # Jahres-Metriken, die nicht TTM-relevant sind, bleiben NaN
    for key in ("gross_profit",):
        if key not in ym:
            ym[key] = Metric(key, float("nan"), "currency",
                             m.market_cap.currency if m.market_cap else "USD", fy)
    m.years[fy] = ym
    return m
