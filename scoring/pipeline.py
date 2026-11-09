"""Gesamte Auswertungs-Pipeline fuer CLI (run_ranking.py) und Streamlit-UI.

Ablauf je Ticker: harte Filter -> Sanity-Check (Log) -> Kriterien -> Score.
Ausgeschlossene Ticker werden mit Grund geloggt (CLAUDE.md: Logging-Pflicht).
"""

from __future__ import annotations

import logging
import math
from datetime import date

from data.loader import Metric, TickerMetrics
from data.metrics import fcf_yield, fcf_yield_sbc, roic
from data.sanity import check_ticker
from scoring.criteria import compute_criteria, active_criteria
from scoring.sectors import sector_rule, normalized_net_income, price_to_book, dividend_yield
from scoring.filters import check_filters, count_value_years
from scoring.score import score_ticker
from valuation.dcf import compute_dcf, is_buy_candidate

logger = logging.getLogger("value_screener.pipeline")


def _fmt_pct(v: float) -> str:
    return "n/a" if math.isnan(v) else f"{v:.2%}"


def evaluate_ticker(m: TickerMetrics, cfg: dict) -> dict:
    """Wertet einen Ticker aus.

    Rueckgabe: {"row": dict | None, "excluded": [gruende], "violations": [texte],
                "criteria": {...}, "score": {...}}
    row ist None, wenn der Ticker ausgeschlossen wurde.
    """
    out = {"row": None, "excluded": [], "violations": [], "criteria": {}, "score": {}}

    reasons = check_filters(m, cfg["filters"])
    for r in reasons:
        logger.info("%s ausgeschlossen: %s", m.ticker, r)
    out["excluded"] = reasons
    # gezahlte Jahre (mit Werten) fuer die Ausschlusstabelle
    out["history_years"] = count_value_years(m)
    if reasons:
        return out

    out["violations"] = check_ticker(m, cfg["sanity"])
    for v in out["violations"]:
        logger.warning("%s Sanity-Verstoss: %s", m.ticker, v)

    # SBC-Fallback aus SEC-EDGAR -- wenn Yahoo/FMP fuer ein Jahr keine
    # SBC liefert, wird ShareBasedCompensation aus companyfacts uebernommen
    # (einmalig laden, alle betroffenen Jahre ergaenzen). Quelle im Flag.
    sbc_sec_sources: list[str] = []
    if cfg.get("sec", {}).get("enabled", True):
        try:
            from data.sec import sec_sbc_fallback_all
            for fy, sbc_val, quelle in sec_sbc_fallback_all(m, cfg):
                fcf_m = m.years[fy].get("fcf")
                if fcf_m is not None and not fcf_m.is_na():
                    m.years[fy]["stock_based_compensation"] = Metric(
                        "stock_based_compensation", sbc_val, fcf_m.unit, fcf_m.currency, fy)
                    m.years[fy]["fcf_sbc"] = Metric(
                        "fcf_sbc", fcf_m.value - sbc_val, fcf_m.unit, fcf_m.currency, fy)
                    sbc_sec_sources.append(quelle)
                    logger.info("%s: SBC FY%d via %s = %.3g", m.ticker, fy, quelle, sbc_val)
        except Exception as exc:
            logger.warning("%s: SEC-SBC-Fallback fehlgeschlagen: %s", m.ticker, exc)
    out["sbc_sec_sources"] = sbc_sec_sources

    criteria = compute_criteria(m, cfg)
    result = score_ticker(criteria, cfg["criteria"], cfg.get("score_groups"))
    out["criteria"] = criteria
    out["score"] = result

    # Sektorgruppe (Industry-basiert) + Sektor-Kennzahlen in die Row
    rule = sector_rule(m, cfg)
    norm_ni, norm_years = normalized_net_income(m, rule)
    row_sector_group = rule.group
    row_sector_note = rule.note
    row_normalized_earnings = norm_ni
    row_normalized_earnings_years = norm_years
    row_price_to_book = price_to_book(m)
    row_dividend_yield = dividend_yield(m)

    # Sektor-Sonderregel (Banken/Versicherer/REITs/Versorger) --
    # DCF und FCF-Yield werden NICHT berechnet (leeres DcfResult mit NaN)
    special = rule.group == "special"
    if special:
        from valuation.dcf import DcfResult
        dcf = DcfResult(fcf_normalized=float("nan"), fcf_years_used=0,
                        revenue_cagr=float("nan"),
                        discount_rate=float(cfg["dcf"].get("discount_rate", 0.09)),
                        terminal_growth=float(cfg["dcf"].get("terminal_growth", 0.025)),
                        shares=float("nan"), fcf_mode="n/a (Sonderregel)")
        logger.info("%s: Sektor-Sonderregel (%s) -- DCF/FCF-Yield nicht berechnet",
                    m.ticker, m.industry)
    else:
        dcf = compute_dcf(m, cfg)
    out["dcf"] = dcf
    price = float("nan")
    if (m.market_cap is not None and not m.market_cap.is_na()
            and dcf.shares == dcf.shares and dcf.shares > 0):
        price = m.market_cap.value / dcf.shares

    dc = cfg["dcf"]  # vor mos_by_mode/mos_robust benoetigt
    # MoS (Base) je Startwert-Modus (Ø3J / jüngstes GJ / TTM Ø / TTM rein)
    # bei Sektor-Sonderregel kein DCF -> alle MoS NaN ("n/a (Sonderregel)")
    mos_by_mode = {}
    vps_by_mode = {}  # Wert je Aktie (Base) je Startwert-Modus (iv_* im Export)
    for mode in ("avg3", "latest", "ttm", "ttm_only"):
        if special:
            mos_by_mode[mode] = float("nan")
            vps_by_mode[mode] = float("nan")
        else:
            dcf_m = compute_dcf(m, cfg, fcf_mode=mode)
            mos_by_mode[mode] = dcf_m.margin_of_safety
            _b = dcf_m.scenarios.get("base")
            vps_by_mode[mode] = _b.value_per_share if _b is not None else float("nan")
    mos_avg3 = mos_by_mode.get("avg3", float("nan"))
    mos_latest = mos_by_mode.get("latest", float("nan"))
    mos_ttm = mos_by_mode.get("ttm", float("nan"))
    mos_ttm_only = mos_by_mode.get("ttm_only", float("nan"))
    # Modus-Namen fuer die fcf_le0-Begruendung (frueh, auch vor missing[] nutzbar)
    mode_names = {"avg3": "Ø3J", "latest": "jüngstes GJ",
                  "ttm": "TTM Ø", "ttm_only": "TTM"}

    # MoS bei erhoehtem Diskontsatz (Base, aktiver Startwert-Modus) --
    # Robustheit: gleiches Szenario mit dcf.robust_discount_rate (Default 10 %)
    robust_rate = float(dc.get("robust_discount_rate", 0.10))
    mos_robust = float("nan")
    vps_robust = float("nan")  # iv_base_10pct (Base-Szenario bei robust_discount_rate)
    if not special:
        try:
            cfg_r = dict(cfg)
            cfg_r["dcf"] = dict(cfg["dcf"])
            cfg_r["dcf"]["discount_rate"] = robust_rate
            dcf_r = compute_dcf(m, cfg_r, fcf_mode=dcf.fcf_mode
                                if dcf.fcf_mode in ("avg3", "latest", "ttm", "ttm_only") else None)
            mos_robust = dcf_r.margin_of_safety
            _rb = dcf_r.scenarios.get("base")
            vps_robust = _rb.value_per_share if _rb is not None else float("nan")
        except Exception as exc:
            logger.warning("%s: Robust-MoS fehlgeschlagen: %s", m.ticker, exc)

    # Kandidatenregel nutzt das konfigurierte MoS (min/avg3/latest/ttm/ttm_only/auto)
    # (dc bereits oben definiert)
    mos_mode = str(dc.get("mos_min_mode", "min")).lower()
    if mos_mode == "min":
        mos_candidates = [v for v in (mos_avg3, mos_latest, mos_ttm, mos_ttm_only) if v == v]
        mos_rule = min(mos_candidates) if mos_candidates else float("nan")
    elif mos_mode == "auto":
        mos_rule = dcf.margin_of_safety
    else:
        mos_rule = mos_by_mode.get(mos_mode, float("nan"))

    # Kandidatenregel auf Quality (statt Gesamtscore) umgestellt
    dcf.is_buy = is_buy_candidate(result["quality"], dcf, price, cfg, mos_rule=mos_rule)

    # MoS je Szenario + Kandidaten-Begruendung (welche Bedingung fehlt)
    quality_min = float(dc.get("buy_quality_min", 60))
    mos_min = float(dc.get("buy_margin_of_safety_min", 0.30))
    bear_floor = float(dc.get("buy_bear_floor", 0.80))
    watch_min = float(dc.get("watch_margin_of_safety_min", 0.30))
    mos_bear = float("nan")
    mos_bull = float("nan")
    bear_vps = dcf.scenarios.get("bear")
    bull_vps = dcf.scenarios.get("bull")
    if price == price and price > 0:
        if bear_vps is not None and bear_vps.value_per_share == bear_vps.value_per_share \
                and bear_vps.value_per_share > 0:
            mos_bear = 1.0 - price / bear_vps.value_per_share
        if bull_vps is not None and bull_vps.value_per_share == bull_vps.value_per_share \
                and bull_vps.value_per_share > 0:
            mos_bull = 1.0 - price / bull_vps.value_per_share
    missing = []
    if result["quality"] != result["quality"] or result["quality"] < quality_min:
        missing.append(f"Quality {result['quality']:.1f} < {quality_min:.0f}"
                       if result["quality"] == result["quality"] else "Quality n/a")
    if mos_rule != mos_rule or mos_rule < mos_min:
        if mos_rule == mos_rule:
            mos_label = {"min": "MoS min(Ø3J/jüngstes/TTM)",
                         "auto": "MoS Base"}.get(mos_mode, f"MoS {mos_mode}")
            missing.append(f"{mos_label} {mos_rule:.1%} < {mos_min:.0%}")
        else:
            # Start-FCF <= 0 im aktiven/entscheidenden Modus begruenden
            le0_modes = [mode_names.get(k, k) for k, v in mos_by_mode.items()
                         if compute_dcf(m, cfg, fcf_mode=k).fcf_le0] if not special else []
            if dcf.fcf_le0:
                missing.append(f"Start-FCF ≤ 0 im Modus {mode_names.get(dcf.fcf_mode, dcf.fcf_mode)}")
            elif le0_modes:
                missing.append(f"Start-FCF ≤ 0 im Modus {', '.join(le0_modes)}")
            else:
                missing.append("MoS Base n/a")
    if (bear_vps is None or bear_vps.value_per_share != bear_vps.value_per_share
            or bear_vps.value_per_share < bear_floor * (price if price == price else float("nan"))):
        if price == price and bear_vps is not None and bear_vps.value_per_share == bear_vps.value_per_share:
            missing.append(f"Bear {bear_vps.value_per_share / price:.0%} Kurs < {bear_floor:.0%} Kurs")
        else:
            missing.append("Bear n/a")

    # Zwei Stufen -- "Kandidat" und "Beobachtung".
    # Kandidat: Minimum der Modi >= 30 %, Quality >= 60, Bear >= 80 % Kurs
    #   (bei Kandidat: der fuer das Minimum entscheidende Modus wird genannt).
    # Beobachtung: Minimum < 30 %, aber der BESTE Modus >= 30 % und Quality >= 60.
    #   (Bear spielt fuer die Beobachtungsliste keine Rolle.)
    def _status_for(mos_rule_val: float, modes_for_rule: list[tuple[str, float]],
                    is_buy_flag: bool) -> tuple[str, str]:
        """Status (Kandidat/Beobachtung/nein) fuer eine Kandidatenregel (v30:
        dieselbe Logik fuer 'alle Modi' und 'juengste 2 Modi').
        bei 'nein' werden die `missing`-Gruende angehaengt."""
        if is_buy_flag:
            st = "Kandidat"
            detail = ""
            if modes_for_rule:
                min_val = min(v for _, v in modes_for_rule)
                deciding = [mode_names.get(k, k) for k, v in modes_for_rule if v == min_val]
                detail = (f"entscheidend: Minimum {mos_rule_val:.1%} "
                          f"(bei Modus {', '.join(deciding)})"
                          if mos_rule_val == mos_rule_val else "")
            return st, detail
        if (quality_ok_global and mos_rule_val == mos_rule_val and mos_rule_val < mos_min
                and modes_for_rule):
            best_mode, best_val = max(modes_for_rule, key=lambda kv: kv[1])
            if best_val >= watch_min:
                return "Beobachtung", (f"bester Modus {mode_names.get(best_mode, best_mode)} "
                                        f"{best_val:.1%} ≥ {watch_min:.0%}, "
                                        f"aber min {mos_rule_val:.1%} < {mos_min:.0%}")
        # Begruendung aus `missing` anhaengen (z. B. Start-FCF <= 0 im Modus X)
        return "nein", "; ".join(missing)

    # SBC-Zeile fehlt in ALLEN Jahren -> Status "Pruefen (SBC fehlt)" (nie Kandidat);
    # FCF/DCF laufen ohne SBC (Fallback v33), Roh-FCF-Yield fliesst NICHT in den Value-Score.
    # "Pruefen (SBC fehlt)" NUR, wenn der Ticker OHNE SBC-Abzug
    #          Quality >= buy_quality_min UND MoS >= buy_margin_of_safety_min erreicht;
    #          sonst normaler Status (nein/Beobachtung).
    sbc_missing_all = not any(
        "fcf_sbc" in m.years[fy] and not m.years[fy]["fcf_sbc"].is_na()
        for fy in m.years
    ) and not (
        getattr(m, "ttm", None) is not None
        and m.ttm.ttm.get("fcf_sbc") is not None and not m.ttm.ttm["fcf_sbc"].is_na()
    )
    pruefen_sbc = False
    if sbc_missing_all and not special:
        dcf.is_buy = False  # nie Kandidat ohne SBC
        q_ok = (result["quality"] == result["quality"]
                and result["quality"] >= quality_min)
        m_ok = (mos_rule == mos_rule and mos_rule >= mos_min)
        pruefen_sbc = bool(q_ok and m_ok)
    elif sbc_missing_all and special:
        pruefen_sbc = False  # Sonderregel hat eigenen Status

    # Capex-Definitionspruefung -- SEC-Abgleich laeuft unten (sec);
    # hier nur vormerken, Auswertung nach sec_check (deviations verfuegbar)
    pruefen_capex = False
    capex_dev_years: list[dict] = []
    # Captive Finance (config captive_finance.tickers) -- ND inkl.
    # Finanzierungsarm -> Pruefstatus, nie Kandidat (Flag erst nach flag_parts)
    captive = m.ticker.upper() in {str(t).upper() for t in cfg.get("captive_finance", {}).get("tickers", [])}
    if captive:
        dcf.is_buy = False
        logger.warning("%s: Captive Finance (config captive_finance.tickers) -> Status 'Pruefen (Captive Finance)'; "
                       "nie Kandidat; ND inkl. Finanzierungsarm, DCF nicht belastbar", m.ticker)

    mos_vals = [(k, v) for k, v in mos_by_mode.items() if v == v]
    mos_vals_global = mos_vals
    quality_ok = result["quality"] == result["quality"] and result["quality"] >= quality_min
    quality_ok_global = quality_ok
    status, status_detail = _status_for(mos_rule, mos_vals, dcf.is_buy)

    # zweite Regel -- Minimum aus den juengsten 2 Modi (juengstes GJ + TTM rein)
    dc_candidate_mode = str(dc.get("candidate_mode", "min_all")).lower()
    recent2 = [(k, v) for k, v in mos_vals if k in ("latest", "ttm_only")]
    mos_recent2 = min(v for _, v in recent2) if recent2 else float("nan")
    if dc_candidate_mode == "min_recent2":
        # aktive Regel = juengste 2
        status, status_detail = _status_for(
            mos_recent2, recent2,
            is_buy_candidate(result["quality"], dcf, price, cfg, mos_rule=mos_recent2))
        status_all, status_all_detail = _status_for(mos_rule, mos_vals, dcf.is_buy)
        status_recent2, status_recent2_detail = status, status_detail
    else:
        # Default min_all: aktive Regel = alle Modi
        status_all, status_all_detail = status, status_detail
        is_buy_recent2 = (False if sbc_missing_all else
                          is_buy_candidate(result["quality"], dcf, price, cfg, mos_rule=mos_recent2))
        status_recent2, status_recent2_detail = _status_for(mos_recent2, recent2, is_buy_recent2)
    # Quelle pro Ticker loggen (CLAUDE.md: Logging-Pflicht)
    logger.info("%s: Quelle=%s (%s)", m.ticker, m.source or "unbekannt", m.name)

    # Kriterien mit zu wenig Jahren als "Daten duenn" markieren
    thin = result.get("thin_criteria", [])
    out["thin_criteria"] = thin
    for name in thin:
        crit = criteria[name]
        logger.info("%s: Kriterium '%s' = Daten duenn (%d Jahre mit Daten)",
                    m.ticker, name, crit.years_available)

    latest_fy = max(m.years)
    # Flags kompakt und entdoppelt: Block-Flags als "blk:duenn", duenne Kriterien
    # als "duenn", kleine Kapitalbasis als "kl.Kapital", Kaufkandidat als "kauf"
    flag_parts = [f"{b[:4]}:duenn" for b, f in result["block_flags"].items() if f]
    if thin:
        flag_parts.append("duenn")
    # negativer EK (neuestes Jahr) -> Flag "ek_negativ" (kein Ausschluss,
    # wenn filters.exclusion.negative_equity: false)
    latest_eq = m.years[latest_fy].get("equity") if m.years else None
    if latest_eq is not None and not latest_eq.is_na() and latest_eq.value <= 0:
        flag_parts.append("ek_negativ")
        logger.warning("%s: Eigenkapital negativ (FY%d: %.2f Mrd.) -> Flag 'ek_negativ'; "
                       "ROE n/a in EK<=0-Jahren, ROIC mit Schulden+EK als Nenner",
                       m.ticker, latest_fy, latest_eq.value / 1e9)
    # Captive Finance -> Flag (captive wird frueher berechnet)
    if captive:
        flag_parts.append("captive_finance")
    # ROIC > 100 % entsteht typischerweise durch eine kleine/negative Kapitalbasis
    # (z. B. Net Cash wie bei AAPL) -- Anzeige begrenzen und flaggen.
    roic5 = criteria["roic_avg5"].value
    if roic5 == roic5 and roic5 > 1.0:
        flag_parts.append("kl.Kapital")
        logger.info("%s: ROIC 5 J = %.1f%% > 100%% -> Flag 'Kapitalbasis klein' (kleine/negative Kapitalbasis)",
                    m.ticker, roic5 * 100)
    # TTM unplausibel -> Flag + Log (Rueckfall auf Geschaeftsjahre im DCF)
    if getattr(m, "ttm", None) is not None and not getattr(m, "ttm_plausible", True):
        flag_parts.append("ttm:unplausibel")
        logger.warning("%s: Quartalsdaten unplausibel -> Rueckfall auf Geschaeftsjahre (DCF-Basis)",
                       m.ticker)
    # SEC-EDGAR-Abgleich (US-Ticker) — Abweichung > tolerance -> Flag
    # Details (Feld/Jahr/%) werden in der Row als sec_detail mitgefuehrt
    # Capex-Definitionspruefung (sec.capex_check: tolerance + min_years)
    sec = None
    sec_detail = ""
    if cfg.get("sec", {}).get("enabled", True):
        try:
            from data.sec import sec_check, format_sec_detail
            sec = sec_check(m, cfg)
            # SEC-Detail mit OCF je Jahr (Capex-Detail in Mrd. + % des OCF)
            ocf_by_fy = {}
            for fy in m.years:
                ocf_m = m.years[fy].get("operating_cashflow")
                if ocf_m is not None and not ocf_m.is_na():
                    ocf_by_fy[fy] = ocf_m.value
            if sec.flag:
                tol = float(cfg.get("sec", {}).get("tolerance", 0.05))
                devs = [d for d in sec.deviations if d.get("dev", 0) == d.get("dev", 0)
                        and d["dev"] > tol]
                sec_detail = format_sec_detail(devs, ocf_by_fy=ocf_by_fy)
                flag_parts.append("sec:abweichend")
                logger.warning("%s: %s | Detail: %s", m.ticker, sec.note, sec_detail)
            # Capex-Regel -- |Yahoo-Capex - SEC-Capex| > ocf_share * OCF
            # in >= min_years Jahren -> Pruefstatus (nicht mehr rel. Abweichung)
            cap_cfg = cfg.get("sec", {}).get("capex_check", {})
            cap_ocf_share = float(cap_cfg.get("ocf_share", 0.10))
            cap_min_years = int(cap_cfg.get("min_years", 2))
            capex_dev_years = []
            for d in sec.deviations:
                if d.get("key") != "capex" or d.get("dev", 0) != d.get("dev", 0):
                    continue
                fy = d.get("fy")
                ocf = abs(ocf_by_fy.get(fy, 0.0))
                diff = abs(d.get("ours", 0.0) - d.get("sec", 0.0))
                d["diff_ocf_share"] = diff / ocf if ocf else float("nan")
                if ocf and diff > cap_ocf_share * ocf:
                    capex_dev_years.append(d)
            if len(capex_dev_years) >= cap_min_years and not special:
                pruefen_capex = True
                dcf.is_buy = False  # nie Kandidat bei Capex-Definitionszweifel
                flag_parts.append("capex:pruefen")
                logger.warning("%s: Capex-Diff laut SEC in %d Jahren > %.0f%% des OCF -> "
                               "Status 'Pruefen (Capex-Definition)'; nie Kandidat",
                               m.ticker, len(capex_dev_years), cap_ocf_share * 100)
        except Exception as exc:  # SEC nicht erreichbar -> kein Absturz
            logger.warning("%s: SEC-Abgleich fehlgeschlagen: %s", m.ticker, exc)
    # Zykliker mit weniger als normalize_years Jahren -> Status maximal
    # "Beobachtung (nicht normalisiert)" (nie Kandidat)
    zyk_unnorm = (rule.group == "cyclicals" and 0 < norm_years < max(1, rule.normalized_earnings_years))
    # Sektor-Sonderregel -> Status "Sonderregel noetig" (statt nein/Pruefen)
    if special:
        status = status_all = status_recent2 = "Sonderregel nötig"
        status_detail = status_all_detail = status_recent2_detail = (
            f"{m.industry}: DCF/FCF-Yield nicht berechnet (Sonderregel)")
    # Capex-Definition laut SEC in >= min_years Jahren > ocf_share des OCF -> Pruefstatus
    # Captive Finance -> eigener Pruefstatus; bei beiden Gruenden beide nennen
    elif pruefen_capex or captive:
        reasons = []
        if pruefen_capex:
            reasons.append("Prüfen (Capex-Definition)")
        if captive:
            reasons.append("Prüfen (Captive Finance)")
        status = status_all = status_recent2 = "; ".join(reasons)
        det_parts = []
        if pruefen_capex:
            det_parts.append(
                f"Capex-Diff laut SEC in {len(capex_dev_years)} Jahren > "
                f"{float(cfg.get('sec', {}).get('capex_check', {}).get('ocf_share', 0.10)):.0%} des OCF")
        if captive:
            det_parts.append("Nettoverschuldung inkl. Finanzierungsarm, DCF nicht belastbar")
        det_parts.append("manuell pruefen; nie Kandidat")
        status_detail = status_all_detail = status_recent2_detail = "; ".join(det_parts)
    # SBC fehlt UND ohne SBC waere der Ticker Kandidat -> "Pruefen (SBC fehlt)"
    elif pruefen_sbc:
        status = status_all = status_recent2 = "Prüfen (SBC fehlt)"
        status_detail = status_all_detail = status_recent2_detail = (
            "SBC fehlt in allen Jahren; ohne SBC Quality ≥ "
            f"{quality_min:.0f} und MoS ≥ {mos_min:.0%} -> manuell pruefen; nie Kandidat")
    # Zykliker ohne volle Normalisierung -> nie Kandidat, max. Beobachtung
    if zyk_unnorm and status in ("Kandidat", "Beobachtung"):
        status = status_all = status_recent2 = "Beobachtung (nicht normalisiert)"
        status_detail = status_all_detail = status_recent2_detail = (
            f"Zykliker ({m.industry}): nur {norm_years} Jahre Normalisierung "
            f"(< {rule.normalized_earnings_years}) -> max. Beobachtung; nie Kandidat")
        dcf.is_buy = False


    # SBC fehlt -> DCF/FCF ohne SBC (Fallback Roh-FCF) -> Flag;
    # wenn SBC aus SEC-EDGAR uebernommen wurde, Quelle im Flag
    if dcf.sbc_missing:
        if sbc_sec_sources:
            flag_parts.append("SBC n/a (ohne SBC; SEC-Fallback: "
                              + "; ".join(sbc_sec_sources) + ")")
        else:
            flag_parts.append("SBC n/a (ohne SBC)")
        logger.warning("%s: kein FCF-SBC verfuegbar -> FCF und DCF OHNE SBC (Roh-FCF) verwendet",
                       m.ticker)
    # EBITDA unplausibel (EBITDA < EBIT) -> Flag + ND/EBITDA n/a
    if getattr(m, "ebitda_unplausible", False):
        flag_parts.append("ebitda:unplausibel")
        logger.warning("%s: EBITDA < EBIT in mind. einem Jahr -> Flag 'EBITDA unplausibel'; ND/EBITDA n/a",
                       m.ticker)
    # Zykliker mit weniger Jahren als normalize_years -> Flag + Log
    if rule.group == "cyclicals" and 0 < norm_years < max(1, rule.normalized_earnings_years):
        flag_parts.append("zyk:norm<7J")
        logger.info("%s: Zykliker -- Gewinnnormalisierung ueber %d Jahre (< %d) -> Flag",
                    m.ticker, norm_years, rule.normalized_earnings_years)
    if rule.group == "reits":
        logger.info("%s: REIT (%s) -- %s", m.ticker, m.industry, rule.note)
    if dcf.is_buy:
        flag_parts.append("kauf")
        logger.info("%s: Kaufkandidat (Quality %.1f, MoS Base %.1f%%, Bear >= 80%% Kurs)",
                    m.ticker, result["quality"], dcf.margin_of_safety * 100)
    elif status == "Beobachtung":
        flag_parts.append("beobachten")
        logger.info("%s: Beobachtungsliste (%s)", m.ticker, status_detail)
    # Bilanz-Konsistenz -- gemeldete Nettoverschuldung vs. Total Debt - Cash
    # (neuestes Jahr mit allen drei Werten); Abweichung > consistency.net_debt_mcap_share
    # der Marktkapitalisierung -> Flag "ND != Schulden - Cash"
    nd_inconsistent = False
    nd_inconsistent_detail = ""
    if m.market_cap is not None and not m.market_cap.is_na() and m.market_cap.value > 0:
        ccfg = cfg.get("consistency", {})
        nd_share = float(ccfg.get("net_debt_mcap_share", 0.05))
        for fy in sorted(m.years, reverse=True):
            ym = m.years[fy]
            nd = ym.get("net_debt"); td = ym.get("_total_debt"); cash = ym.get("_cash")
            if (nd is None or nd.is_na() or td is None or td.is_na()
                    or cash is None or cash.is_na()):
                continue
            diff = abs(nd.value - (td.value - cash.value))
            if diff > nd_share * m.market_cap.value:
                nd_inconsistent = True
                nd_inconsistent_detail = (f"FY{fy}: ND ({nd.value/1e9:,.1f} Mrd.) weicht von "
                                          f"Schulden-Cash ({(td.value-cash.value)/1e9:,.1f} Mrd.) um "
                                          f"{diff/1e9:,.1f} Mrd. ab (> {nd_share:.0%} Marktkap.)")
            break  # nur neuestes Jahr mit vollstaendigen Werten
    if nd_inconsistent:
        flag_parts.append("ND != Schulden - Cash")
        logger.warning("%s: gemeldete ND weicht um > %.0f%% der Marktkap. von "
                       "Schulden-Cash ab -> Flag 'ND != Schulden - Cash'",
                       m.ticker, float(cfg.get("consistency", {}).get("net_debt_mcap_share", 0.05)) * 100)

    flags = " ".join(dict.fromkeys(flag_parts))  # entdoppeln, Reihenfolge behalten

    # Datenqualitaet je Ticker (fuer testlauf_fehler.txt):
    # Datenluecken (fehlende Kernkennzahlen je Jahr), Abdeckung < 70 %,
    # SEC-Abweichungen, Sektorregel (Sonderregel/Ausschluss)
    dq: list[str] = []
    core_keys = ("revenue", "ebit", "net_income", "operating_cashflow", "equity", "fcf")
    # nur Jahre melden, die der Provider ueberhaupt liefert -- ein Jahr
    # "geliefert" = mindestens eines der drei Statements (GuV/Bilanz/Cashflow)
    # hat eine Spalte fuer dieses Jahr (fiscal_ends gepflegt UND mind. eine
    # Statement-Kennzahl vorhanden). Die leere fuenfte Spalte (kein Statement)
    # wird NICHT als Datenluecke gemeldet.
    for fy in sorted(m.years):
        ym = m.years[fy]
        delivered = bool(m.fiscal_ends.get(fy)) and any(
            k in ym and not ym[k].is_na()
            for k in ("revenue", "equity", "operating_cashflow"))
        if not delivered:
            continue  # Jahr nicht geliefert -> keine Luecke melden
        missing_keys = [k for k in core_keys
                        if k not in ym or ym[k].is_na()]
        if missing_keys:
            dq.append(f"FY{fy} Datenluecke: {', '.join(missing_keys)}")
    min_cov = float(cfg.get("score_groups", {}).get("min_coverage_value", 0.70))
    if result["coverage"] == result["coverage"] and result["coverage"] < min_cov:
        dq.append(f"Datenabdeckung {result['coverage']:.0%} < {min_cov:.0%}")
    if sec is not None and sec.flag and sec_detail:
        dq.append(f"SEC-Abweichung: {sec_detail}")
    if special:
        dq.append(f"Sektor-Sonderregel ({m.industry}): DCF/FCF-Yield n/a")
    if pruefen_sbc:
        dq.append("SBC fehlt in allen Jahren -> Pruefstatus (ohne SBC-Kandidat)")
    if pruefen_capex:
        dq.append(f"Capex-Diff laut SEC in {len(capex_dev_years)} Jahren > "
                  f"{float(cfg.get('sec', {}).get('capex_check', {}).get('ocf_share', 0.10)):.0%} des OCF "
                  f"-> Pruefstatus (Capex-Definition)")
    if getattr(m, "ebitda_unplausible", False):
        dq.append("EBITDA < EBIT (unplausibel)")
    if getattr(m, "ttm", None) is not None and not getattr(m, "ttm_plausible", True):
        dq.append("Quartalsdaten unplausibel (Rueckfall GJ)")
    if nd_inconsistent_detail:
        dq.append(nd_inconsistent_detail)
    out["data_quality"] = dq
    for d in dq:
        logger.info("%s Datenqualitaet: %s", m.ticker, d)

    row = {
        "ticker": m.ticker,
        "name": m.name,
        "sector": m.sector,
        "country": m.country,
        "source": m.source or "unbekannt",
        "score": result["total"],
        "quality": result["quality"],
        "value": result["value"],
        "coverage": result["coverage"],
        "quality_coverage": result.get("quality_coverage", result["coverage"]),  # 
        "fcf_yield": fcf_yield(m, latest_fy),
        "fcf_yield_sbc": fcf_yield_sbc(m, latest_fy),
        "fcf_yield_3y": criteria["fcf_yield_sbc_avg3"].value,  # nach SBC (Value-Score)
        "roic_5y": criteria["roic_avg5"].value,
        "roic_5y_years": criteria["roic_avg5"].years_available,  # Fensterlaenge (Tooltip "ROIC Ø")
        "roic_latest": roic(m, latest_fy),   # ROIC neuestes Geschaeftsjahr (Spalte + Metrik)
        "roic_latest_fy": latest_fy,
        "nd_ebitda": criteria["net_debt_ebitda"].value,
        "ev_ebit_med": criteria["ev_ebit_vs_median"].value,
        # Margin of Safety (Base) aus dem DCF-Modul (Build-Schritt 6)
        "margin_of_safety": dcf.margin_of_safety,
        # MoS je Szenario + Kandidaten-Spalte (ja/nein + fehlende Bedingung)
        "mos_bear": mos_bear,
        "mos_bull": mos_bull,
        # MoS (Base) je Startwert-Modus (Rangliste nebeneinander)
        "mos_avg3": mos_avg3,
        "mos_latest": mos_latest,
        "mos_ttm": mos_ttm,
        "mos_ttm_only": mos_ttm_only,
        "mos_rule": mos_rule,          # fuer Kandidatenregel verwendetes MoS (config mos_min_mode)
        "mos_rule_mode": mos_mode,
        # Zwei Stufen -- Kandidat / Beobachtung (+ Detail, welcher Modus entscheidet)
        "status": status,
        "status_detail": status_detail,
        # Status unter beiden Kandidatenregeln nebeneinander + aktive Regel
        "status_all": status_all,
        "status_all_detail": status_all_detail,
        "status_recent2": status_recent2,
        "status_recent2_detail": status_recent2_detail,
        "mos_recent2": mos_recent2,
        "candidate_mode": dc_candidate_mode,
        # MoS (Base) mit alternativer Nettoverschuldung
        "mos_base_alt_nd": dcf.mos_base_alt,
        "dcf_net_debt_mode": dcf.net_debt_mode,
        "dcf_net_debt_alt": dcf.net_debt_alt,
        "dcf_net_debt_alt_source": dcf.net_debt_alt_source,
        "kandidat": ("ja" if dcf.is_buy else
                     (status if status != "nein" else "nein")
                     + (f" ({status_detail})" if status_detail else "")
                     if dcf.is_buy or status != "nein"
                     else ("nein: " + "; ".join(missing) if missing else "nein")),
        # TTM-Basis des DCF (Scorecard-Anzeige: Stichtag + Basis)
        "dcf_fcf_basis": dcf.fcf_basis,
        "dcf_ttm_quarter_end": dcf.ttm_quarter_end,
        "dcf_fcf_sources": dcf.fcf_sources,
        "dcf_net_debt": dcf.net_debt,
        "dcf_net_debt_source": dcf.net_debt_source,
        "dcf_customer_deposits": dcf.customer_deposits,
        "dcf_fcf_mode": dcf.fcf_mode,
        "ttm_plausible": getattr(m, "ttm_plausible", True),
        "dcf_value_per_share_base": dcf.scenarios.get("base").value_per_share
        if dcf.scenarios.get("base") else float("nan"),
        "dcf_value_per_share_bear": dcf.scenarios.get("bear").value_per_share
        if dcf.scenarios.get("bear") else float("nan"),
        "dcf_value_per_share_bull": dcf.scenarios.get("bull").value_per_share
        if dcf.scenarios.get("bull") else float("nan"),
        "is_buy": dcf.is_buy,
        "flags": flags,
        # Sektorgruppe (Industry) + Sektor-Kennzahlen
        "sector_group": row_sector_group,
        "sector_note": row_sector_note,
        "industry": m.industry,
        "normalized_earnings": row_normalized_earnings,
        "normalized_earnings_years": row_normalized_earnings_years,
        "price_to_book": row_price_to_book,
        "dividend_yield": row_dividend_yield,
        # SEC-EDGAR-Abgleich
        "sec_check_ok": bool(sec and sec.ok),
        "sec_check_flag": bool(sec and sec.flag),
        "sec_check_note": sec.note if sec else "",
        # EBITDA-Plausibilitaet + Historie (Jahre mit Werten)
        "ebitda_unplausible": bool(getattr(m, "ebitda_unplausible", False)),
        "history_years": count_value_years(m),
        # SBC-Fallback (DCF ohne SBC) + Start-FCF <= 0 + Value-n/a-Schwelle
        "dcf_sbc_missing": bool(dcf.sbc_missing),
        "status_pruefen_sbc": pruefen_sbc,
        "dcf_fcf_le0": bool(dcf.fcf_le0),
        "value_na_below_coverage": float(cfg.get("score_groups", {}).get("min_coverage_value", 0.70)),
        # Sektor-Sonderregel + SEC-Detail + SBC-SEC-Quelle + Datenqualitaet
        "sector_special": special,
        "sec_detail": sec_detail,
        "sbc_sec_sources": sbc_sec_sources,
        "data_quality": dq,
        # MoS bei erhoehtem Diskontsatz (robust) + Status-Zusatz,
        #       Capex-Definitionspruefung, Bilanz-Konsistenz, Zykliker-Cap
        "mos_robust": mos_robust,
        "mos_robust_rate": float(dc.get("robust_discount_rate", 0.10)),
        "status_pruefen_capex": pruefen_capex,
        "capex_dev_years": capex_dev_years,
        "nd_inconsistent": nd_inconsistent,
        "zyk_unnorm": zyk_unnorm,
        # Export-Felder fuer Tracker/Live-Update (nur Anzeige/Export,
        # die Screener-Logik bleibt unveraendert)
        "kurs": price,
        "kurs_datum": m.market_cap_date or "",
        "calc_date": date.today().isoformat(),
        "iv_bear": dcf.scenarios.get("bear").value_per_share if dcf.scenarios.get("bear") else float("nan"),
        "iv_base": dcf.scenarios.get("base").value_per_share if dcf.scenarios.get("base") else float("nan"),
        "iv_bull": dcf.scenarios.get("bull").value_per_share if dcf.scenarios.get("bull") else float("nan"),
        "iv_avg3": vps_by_mode.get("avg3", float("nan")),
        "iv_latest": vps_by_mode.get("latest", float("nan")),
        "iv_ttm": vps_by_mode.get("ttm", float("nan")),
        "iv_ttm_only": vps_by_mode.get("ttm_only", float("nan")),
        "iv_base_10pct": vps_robust,
        "naechster_earnings": getattr(m, "next_earnings_date", "") or "",
    }
    out["row"] = row
    out["sec_check"] = sec
    return out


def evaluate_all(companies: list[TickerMetrics], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Wertet alle Ticker aus; gibt (rangliste, ausgeschlossene) zurueck.

    Rangliste: Rows sortiert nach Score absteigend (NaN-Scores zuletzt).
    Ausgeschlossene: [{"ticker", "name", "reasons": [...]}]
    """
    rows, excluded = [], []
    for m in companies:
        res = evaluate_ticker(m, cfg)
        if res["row"] is None:
            excluded.append({"ticker": m.ticker, "name": m.name, "reasons": res["excluded"],
                             "history_years": res.get("history_years", 0)})
        else:
            rows.append(res["row"])
    rows.sort(key=lambda r: (r["score"] if r["score"] == r["score"] else -1), reverse=True)
    return rows, excluded


def rank_rows(rows: list[dict], key: str = "quality") -> dict[str, int | None]:
    """Rang je Ticker nach Kennzahl (1 = bester Wert); Gleichstand -> gleicher Rang.

    NaN-Werte erhalten None (Anzeige "n/a"). Wird fuer die Spalte
    "Quality-Rang" im Universum verwendet (Rangliste + CLI).
    """
    vals = sorted({r[key] for r in rows if r[key] == r[key]}, reverse=True)
    return {
        r["ticker"]: (vals.index(r[key]) + 1 if r[key] == r[key] else None)
        for r in rows
    }


def raw_metric_table(m: "TickerMetrics", names: tuple[str, ...]) -> list[dict]:
    """Rohwerte mit Einheit/Waehrung/Jahr/Quelle fuer eine Kennzahlenliste.

    Wird fuer die Scorecard-Anzeige der Formeln (ROIC 5J, FCF-Yield, ND/EBITDA)
    verwendet.
    """
    out = []
    for fy in sorted(m.years):
        ym = m.years[fy]
        for key in names:
            met = ym.get(key)
            if met is None:
                continue
            out.append({
                "Jahr": fy, "Kennzahl": key,
                "Wert": "n/a" if met.is_na() else f"{met.value:,.0f}",
                "Einheit": met.unit, "Waehrung": met.currency,
            })
    return out
