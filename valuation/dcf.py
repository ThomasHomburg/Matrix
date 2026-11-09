"""DCF-Modul: innerer Wert je Aktie mit 3 Szenarien (Bear / Base / Bull).

Nach CLAUDE.md (Build-Schritt 6), aber mit normalisiertem FCF nach SBC
(Durchschnitt der letzten 3 Jahre mit verfuegbaren Daten):

- Basis: normalisierter FCF = Ø FCF-SBC der letzten 3 Jahre (fcf_sbc je Jahr).
- Wachstum Jahre 1-5: Bear 0 %, Base = min(Umsatz-CAGR ueber verfuegbare
  Jahre, base_growth_cap), Bull = min(CAGR x bull_growth_multiplier,
  bull_growth_cap).
- Wachstum Jahre 6-10: linear abnehmend von der Jahr-5-Rate auf
  terminal_growth (fade_to_growth).
- Terminal Value = FCF_10 x (1 + g) / (r - g); Diskontsatz r aus Config
  (discount_rate bzw. discount_rate_cyclicals).
- Innerer Wert = Barwert der FCFs (Jahre 1-10) + Barwert Terminal Value.
- Innerer Wert je Aktie = Innerer Wert / verwässerte Aktien (neuestes FY).
- Margin of Safety (Base) = 1 - Kurs / innerer Wert je Aktie (Base).

Alle Parameter kommen aus config.yaml (Block `dcf`), keine Magic Numbers.
Fehlende Daten -> NaN, nie 0 (CLAUDE.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from data.loader import TickerMetrics


@dataclass
class DcfScenario:
    name: str                      # "bear" | "base" | "bull"
    growth_rates: list[float]      # 10 Wachstumsraten (Jahre 1-10)
    fcfs: list[float]              # projizierte FCFs (Jahre 1-10), Berichtswährung
    pv_fcfs: float                 # Barwert der 10 FCFs
    terminal_value: float          # Barwert des Terminal Value (abgezinst)
    enterprise_value: float        # pv_fcfs + terminal_value
    value_per_share: float         # EV / Aktien (NaN wenn keine Aktienanzahl)


@dataclass
class DcfResult:
    fcf_normalized: float          # normalisierter FCF nach SBC (absolut, Berichtswährung)
    fcf_years_used: int            # Anzahl verwendeter Perioden (Jahre bzw. TTM+GJ)
    revenue_cagr: float            # Umsatz-CAGR ueber verfuegbare Jahre (Basis-Wachstum)
    discount_rate: float
    terminal_growth: float
    shares: float                  # verwässerte Aktien, neuestes FY
    scenarios: dict[str, DcfScenario] = field(default_factory=dict)
    margin_of_safety: float = float("nan")   # MoS Base = 1 - Kurs / Wert je Aktie (Base)
    is_buy: bool = False           # Kaufkandidat (Quality/MoS/Bear-Regeln, siehe pipeline)
    fcf_normalized_conv: float = float("nan")  # normalisierter FCF in Marktkap.-Waehrung
    fcf_basis: str = "annual"   # "ttm" (Ø TTM + 2 nicht ueberlappende GJ) oder "annual"
    ttm_quarter_end: str = ""   # Stichtag letztes Quartal (ISO), nur bei fcf_basis="ttm"
    fcf_sources: list = field(default_factory=list)  # verwendete FCF-SBC-Werte [TTM, FY-1, FY-2]
    net_debt: float = float("nan")   # Nettoverschuldung OHNE Kundengelder (Berichtsw., aktuell)
    net_debt_source: str = ""        # "net_debt (gemeldet, exkl. Kundengelder)" | "total_debt - cash" | "n/a"
    customer_deposits: float = float("nan")  # Kundengelder (z. B. PYPL), in Cash enthalten
    fcf_mode: str = "auto"           # Startwert-Modus: "auto" | "avg3" | "latest" | "ttm" | "ttm_only"
    sbc_missing: bool = False        # True wenn kein FCF-SBC verfuegbar -> Roh-FCF ohne SBC verwendet
    fcf_le0: bool = False            # True wenn Start-FCF <= 0 (MoS n/a, Anzeige "Start-FCF <= 0")
    net_debt_mode: str = "reported"  # "reported" (gemeldet) | "incl_sti" (Schulden − (Cash+kurzfr.Anl.))
    net_debt_alt: float = float("nan")  # alternative Nettoverschuldung (anderer Modus, Berichtsw.)
    net_debt_alt_source: str = ""    # Quelle der alternativen Nettoverschuldung
    mos_base_alt: float = float("nan")   # MoS (Base) mit alternativer Nettoverschuldung
    scenario_growth: dict = field(default_factory=dict)  # {"bear": g5, "base": g5, "bull": g5}


def _cagr(values: list[float]) -> float:
    """Jaehrliche Wachstumsrate ueber n-1 Jahre (chronologisch)."""
    if len(values) < 2 or values[0] <= 0:
        return float("nan")
    n = len(values) - 1
    return (values[-1] / values[0]) ** (1 / n) - 1


def _growth_path(g5: float, g_terminal: float, cfg: dict) -> list[float]:
    """10 Wachstumsraten: Jahre 1-5 konstant g5, Jahre 6-10 linear auf g_terminal."""
    fade = float(cfg.get("fade_to_growth", g_terminal))
    rates = [g5] * 5
    for i in range(1, 6):  # Jahre 6-10: linear von g5 auf fade
        t = i / 5.0
        rates.append(g5 + (fade - g5) * t)
    return rates


def _pv(fcf: float, rate: float, year: int) -> float:
    return fcf / (1.0 + rate) ** year


def _dcf_scenario(name: str, g5: float, g_terminal: float, rate: float,
                  fcf_start: float, shares: float, dcfg: dict,
                  net_debt: float = float("nan")) -> DcfScenario:
    """Ein DCF-Szenario: Wachstumspfad, FCFs, Barwerte, Wert je Aktie.

    Wird von compute_dcf (3 Szenarien) und sensitivity_mos_base (Base-Pfad
    je r/g-Kombination) verwendet -- keine duplizierte Logik.

    Unternehmenswert (EV) = Barwert FCFs + Barwert Terminal Value.
    Innerer Wert (Equity) = EV - Nettoverschuldung (ohne Kundengelder).
    Wert je Aktie = Innerer Wert / Aktien. Nettoverschuldung unbekannt
    (NaN) -> Wert je Aktie = EV / Aktien (alte, rueckwaertskompatible
    Behandlung, damit alte Tests ohne Bilanzdaten weiterlaufen).
    """
    nan = float("nan")
    rates = _growth_path(g5, g_terminal, dcfg)
    fcfs, fcf_t = [], fcf_start
    for g in rates:
        fcf_t = fcf_t * (1.0 + g)
        fcfs.append(fcf_t)
    pv_fcfs = sum(_pv(f, rate, i + 1) for i, f in enumerate(fcfs))
    tv = fcfs[-1] * (1.0 + g_terminal) / (rate - g_terminal) if rate > g_terminal else nan
    tv_pv = _pv(tv, rate, 10) if tv == tv else nan
    ev = pv_fcfs + tv_pv if tv_pv == tv_pv else nan
    if net_debt == net_debt:
        equity = ev - net_debt if ev == ev else nan
    else:
        equity = ev  # Nettoverschuldung unbekannt -> EV als Naeherung (alt)
    vps = equity / shares if (equity == equity and shares == shares) else nan
    return DcfScenario(
        name=name, growth_rates=rates, fcfs=fcfs, pv_fcfs=pv_fcfs,
        terminal_value=tv_pv, enterprise_value=ev, value_per_share=vps,
    )


def _ttm_fcf_start(m: TickerMetrics) -> dict | None:
    """TTM-Start-FCF: Ø aus TTM (letzte 4 Quartale) + bis zu 2 davor
    liegenden, NICHT ueberlappenden Geschaeftsjahren.

    Nicht ueberlappend: das Geschaeftsjahr muss vor dem ersten Quartal des
    TTM-Fensters enden (exaktes FY-Enddatum vs. erstes TTM-Quartal).
    Rueckgabe: None wenn TTM nicht verwendbar (kein TTM, unplausibel,
    Fallback aktiv, oder zu wenig nicht ueberlappende GJ), sonst dict mit
    fcf / years_used / quarter_end / sources / currency.
    """
    ttm = getattr(m, "ttm", None)
    if (ttm is None or not getattr(m, "ttm_plausible", True)
            or getattr(m, "ttm_fallback", False)):
        return None
    ttm_fcf = ttm.ttm.get("fcf_sbc")
    sbc_missing = False
    if ttm_fcf is None or ttm_fcf.is_na():
        # TTM ohne SBC -> Fallback auf Roh-FCF TTM
        ttm_fcf = ttm.ttm.get("fcf")
        sbc_missing = True
    if ttm_fcf is None or ttm_fcf.is_na():
        return None
    ttm_end = ttm.quarter_end  # ISO-Stichtag letztes Quartal
    # erstes Quartal des TTM-Fensters = ttm_end - 9 Monate (Naeherung)
    from datetime import date as _date
    y, mo, _d = (int(x) for x in ttm_end.split("-"))
    first_q_month = ((mo - 10) % 12) + 1   # 9 Monate vor Endmonat
    first_q_year = y - (1 if mo <= 9 else 0)
    window_start = _date(first_q_year, first_q_month, 1)
    # Geschaeftsjahre, deren Enddatum STRICT vor dem TTM-Fenster liegt
    prior_fys = []
    for fy in sorted(m.years, reverse=True):
        fe = m.fiscal_ends.get(fy)
        if not fe:
            continue
        if _date.fromisoformat(fe) < window_start:
            prior_fys.append(fy)
    # Vorjahres-GJ ebenfalls auf Roh-FCF fallbacken, wenn deren fcf_sbc fehlt
    fy_vals_fb = []
    for fy in prior_fys[:2]:
        met = m.years[fy].get("fcf_sbc")
        if met is None or met.is_na():
            met = m.years[fy].get("fcf")
            sbc_missing = True
        if met is not None and not met.is_na():
            fy_vals_fb.append((fy, met.value, met.currency))
    if len(fy_vals_fb) < 2:
        return None  # nicht genug nicht-ueberlappende GJ
    vals = [ttm_fcf.value] + [v for _, v, _ in fy_vals_fb]
    return {
        "fcf": sum(vals) / len(vals),
        "years_used": len(vals),
        "quarter_end": ttm_end,
        "sources": ([("TTM", ttm_fcf.value, ttm_fcf.currency)]
                    + [(f"FY{fy}", v, c) for fy, v, c in fy_vals_fb]),
        "currency": ttm_fcf.currency,
        "sbc_missing": sbc_missing or None,
    }


def _ttm_only_fcf_start(m: TickerMetrics) -> dict | None:
    """Reiner TTM-Start-FCF nach SBC (ohne Ø ueber mehrere Perioden).

    Wie _ttm_fcf_start, aber nur das TTM-Fenster (Summe der letzten 4
    Quartale) -- keine Hinzunahme davor liegender Geschaeftsjahre.
    Rueckgabe: None wenn TTM nicht verwendbar (kein TTM, unplausibel,
    Fallback aktiv), sonst dict mit fcf / years_used=1 / quarter_end /
    sources / currency.
    """
    ttm = getattr(m, "ttm", None)
    if (ttm is None or not getattr(m, "ttm_plausible", True)
            or getattr(m, "ttm_fallback", False)):
        return None
    ttm_fcf = ttm.ttm.get("fcf_sbc")
    sbc_missing = False
    if ttm_fcf is None or ttm_fcf.is_na():
        # TTM ohne SBC -> Fallback auf Roh-FCF TTM
        ttm_fcf = ttm.ttm.get("fcf")
        sbc_missing = True
    if ttm_fcf is None or ttm_fcf.is_na():
        return None
    out = {
        "fcf": ttm_fcf.value,
        "years_used": 1,
        "quarter_end": ttm.quarter_end,
        "sources": [("TTM", ttm_fcf.value, ttm_fcf.currency)],
        "currency": ttm_fcf.currency,
    }
    if sbc_missing:
        out["sbc_missing"] = True
    return out


def _avg_fcf_start(m: TickerMetrics, n: int) -> dict | None:
    """Ø FCF-SBC der letzten n Geschaeftsjahre mit Daten (chronologisch). v33: Fallback auf Roh-FCF ohne SBC."""
    sbc_series = [(fy, m.years[fy]["fcf_sbc"]) for fy in sorted(m.years)
                  if "fcf_sbc" in m.years[fy] and not m.years[fy]["fcf_sbc"].is_na()]
    if not sbc_series:
        # Fallback auf Roh-FCF (ohne SBC), sbc_missing=True
        fcf_series = [(fy, m.years[fy]["fcf"]) for fy in sorted(m.years)
                      if "fcf" in m.years[fy] and not m.years[fy]["fcf"].is_na()]
        if not fcf_series:
            return None
        last = fcf_series[-n:]
        return {
            "fcf": sum(x.value for _, x in last) / len(last),
            "years_used": len(last),
            "sources": [(f"FY{fy}", x.value, x.currency) for fy, x in last],
            "currency": last[-1][1].currency,
            "sbc_missing": True,
        }
    last = sbc_series[-n:]
    return {
        "fcf": sum(x.value for _, x in last) / len(last),
        "years_used": len(last),
        "sources": [(f"FY{fy}", x.value, x.currency) for fy, x in last],
        "currency": last[-1][1].currency,
    }


def _net_debt_ex_customer_deposits(m: TickerMetrics, fcf_basis: str,
                                    net_debt_mode: str = "reported"):
    """Nettoverschuldung OHNE Kundengelder (Berichtswährung) + Quelle.

    Kundengelder (z. B. PYPL 'Deposit Liabilities') sind wirtschaftlich
    Fremdmittel, sitzen aber in 'Cash' -- die gemeldete Nettoverschuldung
    (Total Debt - Cash) unterschlaegt die Verschuldung daher um die
    Kundengelder. Sie werden hier hinzuaddiert.

    net_debt_mode (config `dcf.net_debt_mode`):
      "reported"  — gemeldete Nettoverschuldung (+ Kundengelder)
      "incl_sti"  — Finanzschulden − (Cash + kurzfristige Anlagen)
                    (+ Kundengelder); kurzfristige Anlagen gelten als
                    liquid und werden mit abgezogen.

    Prioritaet: TTM-Bestand (Stichtag letztes Quartal) falls fcf_basis="ttm",
    sonst das neueste Geschaeftsjahr mit Bilanzdaten.
    Rueckgabe: (wert, quelle, kundengelder, waehrung).
    """
    nan = float("nan")
    ttm = getattr(m, "ttm", None)
    if fcf_basis == "ttm" and ttm is not None:
        nd_m = ttm.ttm.get("net_debt")
        cd_m = ttm.ttm.get("customer_deposits")
        td_m = ttm.ttm.get("_total_debt")
        cash_m = ttm.ttm.get("_cash")
        sti_m = ttm.ttm.get("_short_term_investments")
        cd = cd_m.value if (cd_m is not None and not cd_m.is_na()) else nan
        cur = (nd_m.currency if nd_m is not None and not nd_m.is_na()
               else (td_m.currency if td_m is not None and not td_m.is_na() else ""))
        if net_debt_mode == "incl_sti" and td_m is not None and not td_m.is_na():
            liquid = 0.0
            if cash_m is not None and not cash_m.is_na():
                liquid += cash_m.value
            if sti_m is not None and not sti_m.is_na():
                liquid += sti_m.value
            if liquid > 0 or (cash_m is not None and not cash_m.is_na()):
                adj = td_m.value - liquid + (cd if cd == cd else 0.0)
                src = (f"TTM (Stichtag {ttm.quarter_end}) "
                       f"Total Debt − (Cash + kurzfr. Anlagen)"
                       + (" + Kundengelder" if cd == cd else ""))
                return adj, src, cd, cur
        if nd_m is not None and not nd_m.is_na():
            adj = nd_m.value + cd if cd == cd else nd_m.value
            src = (f"TTM (Stichtag {ttm.quarter_end})"
                   + (" + Kundengelder" if cd == cd else ""))
            return adj, src, cd, nd_m.currency
    for fy in sorted(m.years, reverse=True):
        y = m.years[fy]
        nd = y.get("net_debt")
        cd = y.get("customer_deposits")
        td = y.get("_total_debt")
        cash = y.get("_cash")
        sti = y.get("_short_term_investments")
        cd_v = cd.value if (cd is not None and not cd.is_na()) else nan
        if net_debt_mode == "incl_sti" and td is not None and not td.is_na():
            liquid = 0.0
            if cash is not None and not cash.is_na():
                liquid += cash.value
            if sti is not None and not sti.is_na():
                liquid += sti.value
            if liquid > 0 or (cash is not None and not cash.is_na()):
                adj = td.value - liquid + (cd_v if cd_v == cd_v else 0.0)
                src = (f"FY{fy} Total Debt − (Cash + kurzfr. Anlagen)"
                       + (" + Kundengelder" if cd_v == cd_v else ""))
                return adj, src, cd_v, td.currency
        if nd is not None and not nd.is_na():
            adj = nd.value + cd_v if cd_v == cd_v else nd.value
            src = f"FY{fy} net_debt" + (" + Kundengelder" if cd_v == cd_v else "")
            return adj, src, cd_v, nd.currency
        if (td is not None and not td.is_na()
                and cash is not None and not cash.is_na()):
            adj = td.value - cash.value + (cd_v if cd_v == cd_v else 0.0)
            src = f"FY{fy} total_debt - cash" + (" + Kundengelder" if cd_v == cd_v else "")
            return adj, src, cd_v, td.currency
    return nan, "n/a", nan, ""


def compute_dcf(m: TickerMetrics, cfg: dict, fx=None, fcf_mode: str | None = None) -> DcfResult:
    """Berechnet den DCF fuer einen Ticker nach Config-Block `cfg['dcf']`.

    fx: optionaler FX-Converter (wie in data/metrics.py) — der normalisierte
    FCF und die Nettoverschuldung werden in die Marktkap.-Waehrung
    konvertiert, damit Kurs und Wert konsistent sind.

    fcf_mode: Startwert des FCF -- "auto" (Config `dcf.fcf_mode`, Default:
    TTM-Basis falls plausibel, sonst Ø letzte 3 GJ), "avg3" (Ø letzte 3 GJ),
    "latest" (juengstes GJ), "ttm" (TTM Ø: Ø aus TTM + 2 nicht
    ueberlappenden GJ, Rueckfall auf Ø3J wenn TTM nicht verwendbar),
    "ttm_only" (v27: reiner TTM-Start-FCF nach SBC, ohne Ø; Rueckfall auf
    Ø3J wenn TTM nicht verwendbar).

    Innerer Wert (Equity) = Unternehmenswert (Barwert FCFs + Barwert
    Terminal Value) - Nettoverschuldung OHNE Kundengelder. Wert je Aktie =
    Innerer Wert / Aktien. Nettoverschuldung unbekannt (NaN) -> EV als
    Naeherung (rueckwaertskompatibel).
    """
    dcfg = cfg["dcf"]
    nan = float("nan")
    mode = (fcf_mode or dcfg.get("fcf_mode", "auto")).lower()
    if mode not in ("auto", "avg3", "latest", "ttm", "ttm_only"):
        mode = "auto"
    result = DcfResult(
        fcf_normalized=nan, fcf_years_used=0, revenue_cagr=nan,
        discount_rate=float(dcfg.get("discount_rate", 0.09)),
        terminal_growth=float(dcfg.get("terminal_growth", 0.025)),
        shares=nan, fcf_mode=mode,
    )

    # --- normalisierter FCF nach SBC (Startwert je fcf_mode) ----------------
    start = None
    if mode in ("auto", "ttm"):
        start = _ttm_fcf_start(m)
    if start is None and mode == "ttm_only":
        start = _ttm_only_fcf_start(m)  # reiner TTM-Start (ohne Ø)
    if start is None and mode == "latest":
        start = _avg_fcf_start(m, 1)
    if start is None and mode != "latest":
        start = _avg_fcf_start(m, int(dcfg.get("fcf_avg_years", 3)))
    if start is None:
        return result  # kein FCF bestimmbar (weder SBC noch Roh-FCF)
    result.sbc_missing = bool(start.get("sbc_missing", False))
    result.fcf_le0 = (start["fcf"] <= 0) if start["fcf"] == start["fcf"] else False
    result.fcf_normalized = start["fcf"]
    result.fcf_years_used = start["years_used"]
    result.fcf_sources = start["sources"]
    if mode in ("auto", "ttm", "ttm_only") and "quarter_end" in start:
        result.fcf_basis = "ttm"
        result.ttm_quarter_end = start["quarter_end"]
    else:
        result.fcf_basis = "annual"

    # --- Umsatz-CAGR ueber verfuegbare Jahre (Basis-Wachstum) ---------------
    rev_series = [m.years[fy]["revenue"] for fy in sorted(m.years)
                  if "revenue" in m.years[fy] and not m.years[fy]["revenue"].is_na()]
    rev_vals = [x.value for x in rev_series if x.value > 0]
    cagr = _cagr(rev_vals) if len(rev_vals) >= 2 else nan
    result.revenue_cagr = cagr

    # --- Aktienanzahl (verwässert, neuestes FY) -----------------------------
    for fy in sorted(m.years, reverse=True):
        sh = m.years[fy].get("shares_diluted")
        if sh is not None and not sh.is_na() and sh.value > 0:
            result.shares = sh.value
            break

    # --- Szenario-Wachstumsraten ---------------------------------------------
    base_cap = float(dcfg.get("base_growth_cap", 0.06))
    bull_mult = float(dcfg.get("bull_growth_multiplier", 1.3))
    bull_cap = float(dcfg.get("bull_growth_cap", 0.10))
    neg_spread = float(dcfg.get("bear_negative_spread", 0.03))  # 
    g_terminal = result.terminal_growth
    if cagr != cagr:  # NaN -> kein Wachstum bestimmbar
        g_bear, g_base, g_bull = 0.0, 0.0, 0.0
    elif cagr < 0:
        # schrumpfendes Unternehmen -- Base = CAGR (negativ),
        # Bear = CAGR - bear_negative_spread, Bull = 0 % (keine Erholung)
        g_bear = cagr - neg_spread
        g_base = cagr
        g_bull = 0.0
    else:
        g_bear = 0.0
        g_base = min(cagr, base_cap)
        g_bull = min(cagr * bull_mult, bull_cap)

    # --- FX: FCF + Nettoverschuldung in Marktkap.-Waehrung -------------------
    mcap = m.market_cap
    if mcap is None or mcap.is_na() or mcap.value <= 0:
        return result
    converter = fx
    if converter is None:
        from data.metrics import _NoFX
        converter = _NoFX()
    latest_fy = max(m.years)
    fcf_conv = converter.convert(result.fcf_normalized, start["currency"],
                                 mcap.currency, latest_fy)
    if math.isnan(fcf_conv):
        return result
    result.fcf_normalized_conv = fcf_conv

    # Nettoverschuldung OHNE Kundengelder (Berichtsw. -> Marktkap.-Waehrung)
    nd_mode = str(dcfg.get("net_debt_mode", "reported")).lower()
    if nd_mode not in ("reported", "incl_sti"):
        nd_mode = "reported"
    result.net_debt_mode = nd_mode
    alt_mode = "incl_sti" if nd_mode == "reported" else "reported"
    nd_rep, nd_src, cd_val, nd_cur = _net_debt_ex_customer_deposits(
        m, result.fcf_basis, net_debt_mode=nd_mode)
    nd_alt, nd_alt_src, _cd_alt, nd_alt_cur = _net_debt_ex_customer_deposits(
        m, result.fcf_basis, net_debt_mode=alt_mode)
    result.net_debt = nd_rep
    result.net_debt_source = nd_src
    result.customer_deposits = cd_val
    result.net_debt_alt = nd_alt
    result.net_debt_alt_source = nd_alt_src
    nd_conv = nan
    if nd_rep == nd_rep and nd_cur:
        nd_conv = converter.convert(nd_rep, nd_cur, mcap.currency, latest_fy)
    nd_alt_conv = nan
    if nd_alt == nd_alt and nd_alt_cur:
        nd_alt_conv = converter.convert(nd_alt, nd_alt_cur, mcap.currency, latest_fy)

    for name, g5 in (("bear", g_bear), ("base", g_base), ("bull", g_bull)):
        if result.fcf_le0:
            # Start-FCF <= 0 -> Wert je Aktie in der BERECHNUNG n/a
            # (nicht nur Anzeige); Szenario bleibt mit NaN gefuellt.
            sc = DcfScenario(name=name, growth_rates=_growth_path(g5, g_terminal, dcfg),
                             fcfs=[], pv_fcfs=nan, terminal_value=nan,
                             enterprise_value=nan, value_per_share=nan)
        else:
            sc = _dcf_scenario(name, g5, g_terminal, result.discount_rate, fcf_conv,
                               result.shares, dcfg, net_debt=nd_conv)
        result.scenarios[name] = sc
        result.scenario_growth[name] = g5  # fuer Erklaertext

    # --- Margin of Safety (Base) ----------------------------------------------
    base = result.scenarios.get("base")
    price = nan
    if result.shares == result.shares and result.shares > 0:
        price = mcap.value / result.shares
    # Start-FCF <= 0 -> MoS in der Berechnung n/a (Base vps ist NaN)
    if (base is not None and not result.fcf_le0
            and base.value_per_share == base.value_per_share and base.value_per_share > 0):
        if price == price and price > 0:
            result.margin_of_safety = 1.0 - price / base.value_per_share

    # --- MoS (Base) mit alternativer Nettoverschuldung (nebengerechnet) ------
    # bei fcf_le0 bleibt mos_base_alt NaN
    if nd_alt_conv == nd_alt_conv and base is not None and not result.fcf_le0:
        ev = base.enterprise_value
        equity_alt = ev - nd_alt_conv if ev == ev else nan
        vps_alt = equity_alt / result.shares if (equity_alt == equity_alt
                                                 and result.shares == result.shares) else nan
        if vps_alt == vps_alt and vps_alt > 0 and price == price and price > 0:
            result.mos_base_alt = 1.0 - price / vps_alt
    return result


def sensitivity_mos_base(m: TickerMetrics, cfg: dict, current_price: float | None = None,
                         fx=None,
                         discount_rates=(0.08, 0.09, 0.10, 0.11),
                         terminal_growths=(0.02, 0.025, 0.03)) -> dict:
    """Sensitivitaet des Base-Szenarios: MoS und innerer Wert je Aktie.

    Eingabe: TickerMetrics, cfg, Kurs (current_price; Default: Kurs aus
    Marktkapitalisierung / Aktien wie in compute_dcf), optional fx.

    Pro Kombination (r, g) aus discount_rates x terminal_growths wird der
    Base-Szenario-Pfad (Wachstum aus der CAGR, identische Logik wie
    compute_dcf via _dcf_scenario) mit r/g neu diskontiert.

    Rueckgabe: {"mos": DataFrame-Rows, "vps": DataFrame-Rows} mit je einer
    Zeile pro (r, g): {discount_rate, terminal_growth, value_per_share, mos}.
    Zwei Tabellen: MoS = 1 - Kurs / Wert je Aktie, und Wert je Aktie.
    """
    import pandas as pd

    dcfg = cfg["dcf"]
    nan = float("nan")
    base_res = compute_dcf(m, cfg, fx=fx)
    if base_res.fcf_normalized != base_res.fcf_normalized:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}
    mcap = m.market_cap
    if mcap is None or mcap.is_na() or mcap.value <= 0:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}
    if base_res.shares != base_res.shares or base_res.shares <= 0:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}

    # Kurs: Parameter, sonst aus Marktkap./Aktien (wie compute_dcf)
    if current_price is not None and current_price == current_price and current_price > 0:
        price = float(current_price)
    else:
        price = mcap.value / base_res.shares
    if price != price or price <= 0:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}

    # Start-FCF + Nettoverschuldung in Marktkap.-Waehrung (identisch zu compute_dcf)
    converter = fx
    if converter is None:
        from data.metrics import _NoFX
        converter = _NoFX()
    latest_fy = max(m.years)
    if base_res.fcf_basis == "ttm" and base_res.fcf_sources:
        fcf_cur = base_res.fcf_sources[0][2]
    else:
        sbc_series = [m.years[fy]["fcf_sbc"] for fy in sorted(m.years)
                      if "fcf_sbc" in m.years[fy] and not m.years[fy]["fcf_sbc"].is_na()]
        fcf_cur = sbc_series[-1].currency
    fcf_conv = converter.convert(base_res.fcf_normalized, fcf_cur,
                                 mcap.currency, latest_fy)
    if math.isnan(fcf_conv):
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}
    # Nettoverschuldung (ohne Kundengelder) wie in compute_dcf -> exakte Uebereinstimmung
    nd_mode = str(dcfg.get("net_debt_mode", "reported")).lower()
    if nd_mode not in ("reported", "incl_sti"):
        nd_mode = "reported"
    nd_rep, _nd_src, _cd, nd_cur = _net_debt_ex_customer_deposits(
        m, base_res.fcf_basis, net_debt_mode=nd_mode)
    nd_conv = float("nan")
    if nd_rep == nd_rep and nd_cur:
        nd_conv = converter.convert(nd_rep, nd_cur, mcap.currency, latest_fy)

    # Base-Wachstum (min(CAGR, base_growth_cap)); identisch zu compute_dcf
    cagr = base_res.revenue_cagr
    base_cap = float(dcfg.get("base_growth_cap", 0.06))
    g5 = 0.0 if cagr != cagr else min(cagr, base_cap)

    mos_rows, vps_rows = [], []
    for r in discount_rates:
        for g in terminal_growths:
            if r <= g:
                mos_rows.append({"discount_rate": r, "terminal_growth": g,
                                 "value_per_share": nan, "mos": nan})
                vps_rows.append({"discount_rate": r, "terminal_growth": g,
                                 "value_per_share": nan})
                continue
            sc = _dcf_scenario("base", g5, g, r, fcf_conv, base_res.shares, dcfg,
                               net_debt=nd_conv)
            vps = sc.value_per_share
            mos = 1.0 - price / vps if (vps == vps and vps > 0) else nan
            mos_rows.append({"discount_rate": r, "terminal_growth": g,
                             "value_per_share": vps, "mos": mos})
            vps_rows.append({"discount_rate": r, "terminal_growth": g,
                             "value_per_share": vps})
    return {"mos": pd.DataFrame(mos_rows), "vps": pd.DataFrame(vps_rows)}


def is_buy_candidate(quality: float, dcf: DcfResult, current_price: float, cfg: dict,
                     mos_rule: float | None = None) -> bool:
    """Kaufkandidat: Quality >= buy_quality_min UND MoS (Base) >=
    buy_margin_of_safety_min UND Bear-Szenario >= Kurs x buy_bear_floor.

    Alle Schwellen aus config.yaml (Block `dcf`). Quality statt Gesamtscore
    (v21): die Bewertungsbedingungen (MoS, Bear) tragen den Value-Anteil.

    mos_rule (v26): das fuer die MoS-Bedingung verwendete MoS — Default
    dcf.margin_of_safety (aktiver fcf_mode). Die Pipeline uebergibt hier
    das nach `dcf.mos_min_mode` aggregierte MoS (min/avg3/latest/ttm/auto).
    """
    dcfg = cfg["dcf"]
    mos = dcf.margin_of_safety if mos_rule is None else mos_rule
    if quality != quality or mos != mos:
        return False
    if current_price != current_price or current_price <= 0:
        return False
    if quality < float(dcfg.get("buy_quality_min", 60)):
        return False
    if mos < float(dcfg.get("buy_margin_of_safety_min", 0.30)):
        return False
    bear = dcf.scenarios.get("bear")
    if bear is None or bear.value_per_share != bear.value_per_share:
        return False
    floor = float(dcfg.get("buy_bear_floor", 0.80))
    return bear.value_per_share >= floor * current_price
