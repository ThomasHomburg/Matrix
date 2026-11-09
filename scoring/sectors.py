"""Sektorregeln ueber das GICS-Induustry-Feld (v28, CLAUDE.md Schritt 7).

Warum Industry statt Sector: PYPL, Visa (V) und Mastercard (MA) laufen als
"Financial Services", sind aber keine Banken -- die Branchenregeln duerfen
deshalb nicht am Sector festgemacht werden.

Gruppen:
- financials (Banken & Versicherer): ROE, P/B, Dividendenrendite statt
  ROIC, FCF, ND/EBITDA. Branchen: Banks*, Insurance*, Capital Markets,
  Asset Management, Credit Services, Financial Data & Stock Exchanges,
  Insurance Brokers, Diversified Financial Services, Mortgage Finance.
  Hinweis: Credit Services (PYPL/V/MA) ist KEINE Bank -- Zahlungsverkehr
  hat andere Kennzahlen als Einlagengeschaeft. Wir behandeln Banken und
  Versicherer als 'financials', Credit Services als Nicht-Bank
  (Standard-Kriterien, aber ohne FCF-Pflicht -- siehe unten).
- cyclicals (Rohstoffe, Chemie, Stahl, Auto): Gewinne ueber bis zu 7 Jahre
  normalisieren (gleitender Durchschnitt), soweit Daten reichen; sonst Flag.
- reits: FFO-Hinweis statt FCF-Yield (FFO ist die massgebliche Kennzahl;
  yfinance liefert FFO i. d. R. nicht -> Hinweis/Fallback auf FCF-Yield).
- special (v35): Industry-Gruppen mit Sonderregel -- DCF und FCF-Yield
  werden NICHT berechnet (MoS-Zellen "n/a (Sonderregel)", Status
  "Sonderregel noetig"). Pattern (fnmatch, case-insensitive) auf Industry:
  Banken (Banks - *, Capital Markets), Versicherer (Insurance - *),
  REITs (REIT - *), Versorger (Utilities - *). PYPL/V/MA (Credit Services)
  sind NICHT enthalten -> Normalmodell.

Config: config.yaml Block `sector_rules`.
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass

from data.loader import TickerMetrics

# GICS-Branchen (Industry), die als Bank/Versicherer gelten.
_FINANCIAL_INDUSTRIES = {
    "banks - diversified", "banks - regional", "banks - money center",
    "insurance - property & casualty", "insurance - life",
    "insurance - diversified", "insurance - reinsurance",
    "insurance - specialty", "insurance - brokers",
    "capital markets", "asset management", "asset management - global",
    # "financial data & stock exchanges" entfernt -> Standardgruppe
    "diversified financial services",
    "mortgage finance", "shell companies",
}
# Branchen, die als Zykliker (Rohstoffe, Chemie, Stahl, Auto) gelten.
_CYCLICAL_INDUSTRIES = {
    # Rohstoffe / Mining
    "gold", "silver", "copper", "aluminum", "steel", "coal",
    "other industrial metals & mining", "other precious metals & mining",
    "uranium", "coking coal", "lumber & wood production",
    "paper & paper products", "packaging & containers",
    # Chemie
    "chemicals", "specialty chemicals", "agricultural inputs",
    "industrial gases", "basic materials - other",
    # Stahl
    "metal fabrication", "steel & iron",
    # Auto
    "auto manufacturers", "auto parts", "auto & truck dealerships",
    "auto parts & equipment", "trucking", "railroads",
    # Energie (zyklisch)
    "oil & gas integrated", "oil & gas e&p", "oil & gas refining & marketing",
    "oil & gas midstream", "oil & gas equipment & services", "oil & gas drilling",
    "thermal coal",
    # Weitere Zykliker
    "building products & equipment", "construction materials",
    "residential construction", "home improvement retail",
    "department stores", "discount stores", "apparel retail",
    "specialty retail", "restaurants", "resorts & casinos", "lodging",
    "travel services", "leisure", "gambling", "footwear & accessories",
    "apparel manufacturing", "luxury goods", "furnishings, fixtures & appliances",
    "consumer electronics", "leisure products", "internet retail",
    "recreational vehicles", "publishing", "broadcasting",
    "entertainment", "movies & entertainment", "advertising agencies",
    "rental & leasing services", "integrated freight & logistics",
    "marine shipping", "airlines", "airports & air services",
    "electrical equipment & parts", "specialty industrial machinery",
    "industrial distribution", "farm & heavy construction machinery",
    "aerospace & defense", "defense", "airlines", "gambling",
}

# Bekannte REIT-Branchenpraefixe.
_REIT_PREFIX = "reit"


@dataclass
class SectorRule:
    group: str          # "financials" | "cyclicals" | "reits" | "special" | "standard"
    normalized_earnings_years: int = 1   # 7 bei Zyklikern, sonst 1
    note: str = ""      # Anzeige-Hinweis (z. B. FFO statt FCF-Yield)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def sector_rule(m: TickerMetrics, cfg: dict | None = None) -> SectorRule:
    """Leitet die Sektorgruppe aus dem Industry-Feld ab (config `sector_rules`)."""
    cfg = cfg or {}
    groups_cfg = cfg.get("sector_rules", {}).get("groups", {})
    fin_cfg = groups_cfg.get("financials", {})
    cyc_cfg = groups_cfg.get("cyclicals", {})
    reit_cfg = groups_cfg.get("reits", {})
    spec_cfg = groups_cfg.get("special", {})
    fin_industries = {_norm(x) for x in fin_cfg.get(
        "industries", sorted(_FINANCIAL_INDUSTRIES))}
    cyc_industries = {_norm(x) for x in cyc_cfg.get(
        "industries", sorted(_CYCLICAL_INDUSTRIES))}
    reit_industries = {_norm(x) for x in reit_cfg.get("industries", [])}
    # Sonderregel-Gruppen (Banken/Versicherer/REITs/Versorger als Liste von
    # Industry-Patternen, z. B. "Banks - *"). fnmatch, case-insensitive.
    spec_patterns = [str(x) for x in spec_cfg.get("industry_patterns", [
        "Banks - *", "Capital Markets", "Insurance - *", "REIT - *", "Utilities - *"])]

    industry = _norm(m.industry)
    sector = _norm(m.sector)

    # Sonderregel hat Vorrang (vor reits/financials, damit REIT/Bank/
    # Versicherer/Versorger NICHT im Normalmodell landen)
    for pat in spec_patterns:
        if fnmatch.fnmatchcase(industry, _norm(pat)):
            return SectorRule(
                group="special", normalized_earnings_years=1,
                note=(f"Sonderregel ({m.industry}): DCF und FCF-Yield werden "
                      f"nicht berechnet -> MoS 'n/a (Sonderregel)', Status "
                      f"'Sonderregel noetig'"))

    is_reit = (industry.startswith(_REIT_PREFIX)
               or industry in reit_industries
               or sector == "real estate" and industry.startswith(_REIT_PREFIX))
    if is_reit:
        ffo = m.extras.get("ffo_per_share") or m.extras.get("ffo")
        if ffo is not None and not ffo.is_na():
            note = "REIT: FFO je Aktie statt FCF-Yield (FFO gemeldet)"
        else:
            note = ("REIT: FFO-Hinweis -- FFO je Aktie ist die massgebliche "
                    "Kennzahl (yfinance liefert i. d. R. kein FFO; "
                    "Fallback auf FCF-Yield)")
        return SectorRule(group="reits", normalized_earnings_years=1, note=note)

    if industry in fin_industries:
        return SectorRule(group="financials", normalized_earnings_years=1,
                          note=("Bank/Versicherer: ROE, P/B, Dividendenrendite "
                                "statt ROIC, FCF, ND/EBITDA"))
    if industry in cyc_industries:
        years = int(cyc_cfg.get("normalize_years", 7))
        return SectorRule(group="cyclicals", normalized_earnings_years=years,
                          note=(f"Zykliker ({m.industry}): Gewinne ueber bis zu "
                                f"{years} Jahre normalisiert"))
    return SectorRule(group="standard", normalized_earnings_years=1, note="")


def normalized_net_income(m: TickerMetrics, rule: SectorRule) -> tuple[float, int]:
    """Gleitender Durchschnitt des Nettogewinns ueber bis zu N Jahre.

    Zykliker: Gewinne ueber bis zu `rule.normalized_earnings_years` Jahre
    normalisieren, soweit Daten reichen; sonst Flag (weniger Jahre).
    Rueckgabe: (Ø Nettogewinn, Anzahl Jahre).
    """
    n = max(1, rule.normalized_earnings_years)
    vals = [met.value for met in m.series("net_income") if not met.is_na()]
    if not vals:
        return float("nan"), 0
    last = vals[-n:]
    return sum(last) / len(last), len(last)


def price_to_book(m: TickerMetrics) -> float:
    """P/B = Kurs / Buchwert je Aktie (aus extras.price_to_book, Provider)."""
    ptb = m.extras.get("price_to_book")
    if ptb is None or ptb.is_na():
        return float("nan")
    return ptb.value


def dividend_yield(m: TickerMetrics) -> float:
    """Dividendenrendite (Dezimal; aus extras.dividend_yield, Provider)."""
    dy = m.extras.get("dividend_yield")
    if dy is None or dy.is_na():
        return float("nan")
    return dy.value


def ffo_per_share(m: TickerMetrics) -> float:
    """FFO je Aktie (REITs; NaN wenn nicht gemeldet)."""
    ffo = m.extras.get("ffo_per_share") or m.extras.get("ffo")
    if ffo is None or ffo.is_na():
        return float("nan")
    return ffo.value
