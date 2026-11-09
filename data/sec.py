"""SEC-EDGAR-Abgleich (kostenlos, https://data.sec.gov/api/xbrl/companyfacts).

Vergleicht die Cashflow-Kennzahlen (OCF, Capex, SBC) aus dem yfinance-Provider
mit den bei der SEC gemeldeten XBRL-Facts (US GAAP, DEI/USD). Abweichung
ueber `sec.tolerance` (Default 5 %) je Jahr -> Flag "Quellen weichen ab".

Wichtig (SEC-Policy): User-Agent MIT E-Mail-Adresse — in config.yaml unter
`sec.user_agent` als Platzhalter hinterlegt ("research@example.com").
Vor Produktivbetrieb eigene E-Mail eintragen.

Nutzung: sec_check(m, cfg) -> {"ok": bool, "deviations": [...], "flag": bool}
"""

from __future__ import annotations

import json
import logging
import math
import urllib.request
from dataclasses import dataclass, field

from data.loader import TickerMetrics

logger = logging.getLogger("value_screener.sec")

# XBRL-Tags (US GAAP) je Kennzahl; erste vorhandene gewinnt.
_XBRL_TAGS = {
    "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities",
                           "NetCashProvidedByUsedInOperatingActivitiesContinuing"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "stock_based_compensation": ["ShareBasedCompensation",
                                 "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensation"],
}

# interner Kennzahl-Name -> SEC-Tag-Key
_KEY_MAP = {
    "operating_cashflow": "operating_cashflow",
    "capex": "capex",
    "stock_based_compensation": "stock_based_compensation",
}


@dataclass
class SecCheckResult:
    ok: bool = False               # Abgleich durchgefuehrt
    flag: bool = False             # True = Abweichung > tolerance in mind. 1 Jahr
    deviations: list = field(default_factory=list)  # [{fy, key, ours, sec, dev}]
    note: str = ""


def _cik_for_ticker(ticker: str) -> str | None:
    """CIK zur Ticker-Symbol via SEC company_tickers.json (einmalig, cached)."""
    _rate_limit()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(
                    "https://www.sec.gov/files/company_tickers.json",
                    headers={"User-Agent": _user_agent_default()}), timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                return str(entry.get("cik_str")).zfill(10)
    except Exception as exc:
        logger.warning("SEC: Ticker->CIK fuer %s fehlgeschlagen: %s", ticker, exc)
    return None


_UA_CACHE = {"ua": None}
# Rate-Limit -- Zeitstempel des letzten SEC-Requests (max. N pro Sekunde)
_LAST_REQ = {"t": 0.0}
_MAX_RPS = {"v": 5.0}


def _user_agent_default() -> str:
    return _UA_CACHE["ua"] or "Value-Screener research@example.com"


def _rate_limit() -> None:
    """wartet, bis der Mindestabstand 1/max_requests_per_second seit dem
    letzten SEC-Request vergangen ist (default 5/s -> 0,2 s)."""
    import time as _time
    min_gap = 1.0 / max(0.1, _MAX_RPS["v"])
    now = _time.monotonic()
    wait = _LAST_REQ["t"] + min_gap - now
    if wait > 0:
        _time.sleep(wait)
    _LAST_REQ["t"] = _time.monotonic()


def _fetch_companyfacts(cik: str, user_agent: str) -> dict | None:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    _rate_limit()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("SEC: companyfacts CIK %s nicht ladbar: %s", cik, exc)
        return None


def _sec_annual_values(facts: dict, key: str) -> dict[int, float]:
    """Jaehrliche SEC-Werte (USD, 10-K/10-K/A, frame=None) je Kennzahl.

    Rueckgabe: {fiscal_year_end_year: value}. Nur abgeschlossene
    Jahresperioden (start + 12 Monate ≈ end, end-Datum im Jahr).
    """
    out = {}
    dei = facts.get("facts", {}).get("dei", {})
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in _XBRL_TAGS[key]:
        fact = usgaap.get(tag)
        if not fact:
            continue
        units = fact.get("units", {}).get("USD", [])
        for u in units:
            if u.get("form") not in ("10-K", "10-K/A"):
                continue
            start, end = u.get("start"), u.get("end")
            if not start or not end:
                continue
            # Jahresperiode: ca. 12 Monate
            try:
                from datetime import date
                d0 = date.fromisoformat(start)
                d1 = date.fromisoformat(end)
            except ValueError:
                continue
            months = (d1.year - d0.year) * 12 + (d1.month - d0.month)
            if not (11 <= months <= 13):
                continue
            fy = d1.year
            val = u.get("val")
            if val is None or fy in out:
                continue
            out[fy] = float(val)
        if out:
            break  # erster vorhandener Tag gewinnt
    return out


def sec_check(m: TickerMetrics, cfg: dict) -> SecCheckResult:
    """Abgleich OCF/Capex/SBC (Jahreswerte) gegen SEC-EDGAR (US-Ticker).

    Rueckgabe: SecCheckResult. ok=False wenn kein Abgleich moeglich
    (kein US-Ticker, kein Netzwerk, CIK nicht gefunden). flag=True wenn
    mind. ein Jahr um mehr als `sec.tolerance` abweicht.
    """
    res = SecCheckResult()
    sec_cfg = cfg.get("sec", {})
    if not sec_cfg.get("enabled", True):
        res.note = "SEC-Abgleich deaktiviert (config sec.enabled)"
        return res
    tol = float(sec_cfg.get("tolerance", 0.05))
    user_agent = str(sec_cfg.get("user_agent") or _user_agent_default())
    _UA_CACHE["ua"] = user_agent
    # Rate-Limit aus config (default 5/s)
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))

    country = (m.country or "").upper()
    if country not in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        res.note = f"kein US-Ticker ({m.country or 'unbekannt'}) — SEC-Abgleich nicht anwendbar"
        return res

    cik = _cik_for_ticker(m.ticker)
    if not cik:
        res.note = "CIK nicht gefunden"
        return res
    facts = _fetch_companyfacts(cik, user_agent)
    if not facts:
        res.note = "companyfacts nicht ladbar"
        return res

    res.ok = True
    worst = 0.0
    for key in ("operating_cashflow", "capex", "stock_based_compensation"):
        sec_vals = _sec_annual_values(facts, key)
        if not sec_vals:
            continue
        for fy in sorted(m.years):
            ours = m.years[fy].get(key)
            if ours is None or ours.is_na():
                continue
            sec_v = sec_vals.get(fy)
            if sec_v is None or sec_v == 0:
                continue
            # Capex: yfinance/FMP negativ, SEC positiv -> Vorzeichen angleichen
            ours_v = ours.value
            if key == "capex" and ours_v < 0:
                ours_v = -ours_v
            dev = abs(ours_v - sec_v) / abs(sec_v)
            res.deviations.append({"fy": fy, "key": key, "ours": ours_v,
                                   "sec": sec_v, "dev": dev})
            worst = max(worst, dev)
    res.flag = worst > tol
    if res.flag:
        res.note = (f"Quellen weichen ab: max. Abweichung {worst:.1%} "
                    f"(> {tol:.0%}) in mind. 1 Jahr")
    else:
        res.note = f"Abgleich ok (max. Abweichung {worst:.1%})"
    logger.info("%s: SEC-Abgleich %s", m.ticker, res.note)
    return res


def sec_sbc_fallback(m: TickerMetrics, cfg: dict) -> tuple[float | None, int | None, str]:
    """SBC-Fallback aus SEC-EDGAR (US-Ticker).

    Wenn der Provider (Yahoo/FMP) fuer ein Geschaeftsjahr keine
    Aktienverguetung (SBC) liefert, wird der XBRL-Tag ShareBasedCompensation
    aus data.sec.gov/api/xbrl/companyfacts als Ersatz verwendet.

    Rueckgabe: (sbc_wert, fiscal_year, quelle) -- sbc_wert None, wenn kein
    SEC-Wert verfuegbar. quelle enthaelt die Herkunft ("SEC EDGAR
    ShareBasedCompensation FY2025").
    """
    sec_cfg = cfg.get("sec", {})
    if not sec_cfg.get("enabled", True):
        return None, None, ""
    country = (m.country or "").upper()
    if country not in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        return None, None, ""
    user_agent = str(sec_cfg.get("user_agent") or _user_agent_default())
    _UA_CACHE["ua"] = user_agent
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))
    cik = _cik_for_ticker(m.ticker)
    if not cik:
        return None, None, ""
    facts = _fetch_companyfacts(cik, user_agent)
    if not facts:
        return None, None, ""
    sec_vals = _sec_annual_values(facts, "stock_based_compensation")
    if not sec_vals:
        return None, None, ""
    # juengstes Jahr, fuer das Yahoo keine SBC geliefert hat
    for fy in sorted(m.years, reverse=True):
        ours = m.years[fy].get("stock_based_compensation")
        if ours is None or ours.is_na():
            if fy in sec_vals and sec_vals[fy] != 0:
                return float(sec_vals[fy]), fy, f"SEC EDGAR ShareBasedCompensation FY{fy}"
    # Fallback: juengstes verfuegbares SEC-Jahr
    fy = max(sec_vals)
    return float(sec_vals[fy]), fy, f"SEC EDGAR ShareBasedCompensation FY{fy}"


def sec_sbc_fallback_all(m: TickerMetrics, cfg: dict) -> list[tuple[int, float, str]]:
    """SBC-Fallback aus SEC-EDGAR fuer ALLE Jahre ohne Yahoo-SBC.

    companyfacts wird nur EINMAL geladen; fuer jedes Geschaeftsjahr, in dem
    der Provider keine SBC (stock_based_compensation / fcf_sbc) liefert,
    wird der SEC-Wert (ShareBasedCompensation) uebernommen.

    Rueckgabe: [(fiscal_year, sbc_wert, quelle), ...] -- quelle z. B.
    "SEC EDGAR ShareBasedCompensation FY2025". Leer, wenn kein US-Ticker,
    SEC deaktiviert oder keine SEC-Werte vorhanden.
    """
    sec_cfg = cfg.get("sec", {})
    if not sec_cfg.get("enabled", True):
        return []
    country = (m.country or "").upper()
    if country not in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        return []
    user_agent = str(sec_cfg.get("user_agent") or _user_agent_default())
    _UA_CACHE["ua"] = user_agent
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))
    cik = _cik_for_ticker(m.ticker)
    if not cik:
        return []
    facts = _fetch_companyfacts(cik, user_agent)
    if not facts:
        return []
    sec_vals = _sec_annual_values(facts, "stock_based_compensation")
    if not sec_vals:
        return []
    out = []
    for fy in sorted(m.years):
        ours = m.years[fy].get("stock_based_compensation")
        fsbc = m.years[fy].get("fcf_sbc")
        if (ours is None or ours.is_na()) and (fsbc is None or fsbc.is_na()):
            if fy in sec_vals and sec_vals[fy] != 0:
                out.append((fy, float(sec_vals[fy]),
                            f"SEC EDGAR ShareBasedCompensation FY{fy}"))
    return out


def format_sec_detail(deviations: list, ocf_by_fy: dict | None = None) -> str:
    """SEC-Abweichungen kompakt als Text: Feld, Jahr, Prozent.

    deviations: [{fy, key, ours, sec, dev}] aus SecCheckResult. Beispiel:
    "OCF FY2024 +6.2%; Capex FY2023 -12.0%". Nur Abweichungen ueber dem
    konfigurierten Toleranzwert (Default 5 %) werden gelistet, aber alle
    uebergebenen Abweichungen formatiert (Aufrufer filtert ggf.).

    ocf_by_fy (v37, optional): {fy: OCF-Betrag} -- dann enthaelt das Capex-Detail
    zusaetzlich Yahoo-Capex, SEC-Capex und Differenz in Mrd. USD und in % des OCF:
    "Capex FY2024: Yahoo 3.2 / SEC 2.0 Mrd., Diff 1.2 Mrd. = 15.6% des OCF".
    """
    key_label = {
        "operating_cashflow": "OCF",
        "capex": "Capex",
        "stock_based_compensation": "SBC",
    }
    parts = []
    for d in sorted(deviations, key=lambda x: (x.get("fy", 0), x.get("key", "")), reverse=True):
        dev = d.get("dev")
        if dev is None or dev != dev:
            continue
        sign = "+" if d.get("ours", 0) >= d.get("sec", 0) else "-"
        label = key_label.get(d.get("key"), d.get("key"))
        fy = d.get("fy")
        if d.get("key") == "capex" and ocf_by_fy and fy in ocf_by_fy and ocf_by_fy[fy]:
            ocf = abs(ocf_by_fy[fy])
            diff = abs(d.get("ours", 0) - d.get("sec", 0))
            parts.append(
                f"{label} FY{fy}: Yahoo {abs(d.get('ours', 0))/1e9:,.2f} / SEC {d.get('sec', 0)/1e9:,.2f} Mrd. USD, "
                f"Diff {diff/1e9:,.2f} Mrd. = {diff/ocf:.1%} des OCF ({sign}{abs(dev):.1%})")
        else:
            parts.append(f"{label} FY{fy} {sign}{abs(dev):.1%}")
    return "; ".join(parts)
