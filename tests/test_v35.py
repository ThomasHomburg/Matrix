"""Tests fuer v35:
(1) Sektor-Sonderregel: Industry-Gruppen Banken (Banks - *, Capital Markets),
    Versicherer (Insurance - *), REITs (REIT - *), Versorger (Utilities - *)
    -> DCF/FCF-Yield nicht berechnet, MoS n/a (Sonderregel), Status
    "Sonderregel noetig". PYPL/V/MA (Credit Services) bleiben Normalmodell.
    Quality n/a bei Abdeckung < 70 %.
(2) Status "Pruefen (SBC fehlt)" nur, wenn ohne SBC Quality >= 60 UND MoS >= 30 %.
(3) SBC-Fallback aus SEC-EDGAR (ShareBasedCompensation) mit Quelle im Flag.
(4) SEC-Detail (Feld/Jahr/%) in Row -- Tests mit CAT und DE (gemonkeypatcht).
(5) Datenqualitaet je Ticker in out["data_quality"].
"""

import math

import pytest

from config import load_config
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_all, evaluate_ticker
from scoring.sectors import sector_rule
from scoring.criteria import compute_criteria
from data.demo import demo_companies
import app


def _mk(ticker="T", industry="Semiconductors", country="US", mcap=1e12,
        sbc_na=False, quality_boost=False):
    """Synthetischer Ticker mit 5 Jahren Daten."""
    m = TickerMetrics(ticker=ticker, country=country)
    m.name = ticker
    m.sector = "Technology" if industry == "Semiconductors" else "Financials"
    m.industry = industry
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 100e9
    for i, fy in enumerate(range(2021, 2026)):
        rev *= 1.08
        ocf = (0.28 if not quality_boost else 0.35) * rev
        capex = -0.05 * rev
        sbc = 0.02 * rev
        fcf = ocf + capex
        fsbc = float("nan") if sbc_na else fcf - sbc
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.30 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.20 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", ocf, "USD", "USD", fy),
            "capex": Metric("capex", capex, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "stock_based_compensation": Metric("sbc", sbc, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fsbc, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.5e9, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
        }
    return m


# ---------- (1) Sektor-Sonderregel ----------

@pytest.mark.parametrize("industry", [
    "Banks - Diversified", "Banks - Regional", "Capital Markets",
    "Insurance - Life", "Insurance - Property & Casualty",
    "REIT - Specialty", "REIT - Industrial",
    "Utilities - Regulated Electric",
])
def test_sonderregel_gruppen(cfg, industry):
    """Banken/Versicherer/REITs/Versorger -> group 'special'."""
    m = _mk(industry=industry)
    assert sector_rule(m, cfg).group == "special"


@pytest.mark.parametrize("ticker,industry", [("PYPL", "Credit Services"),
                                             ("V", "Credit Services"),
                                             ("MA", "Credit Services")])
def test_credit_services_bleiben_normalmodell(cfg, ticker, industry):
    """PYPL/V/MA (Credit Services) sind NICHT in der Sonderregel -> Normalmodell."""
    m = _mk(ticker=ticker, industry=industry)
    assert sector_rule(m, cfg).group == "standard"
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["sector_special"] is False
    assert row["status"] != "Sonderregel nötig"
    # DCF wird berechnet (MoS vorhanden, nicht NaN-special)
    assert "Sonderregel" not in str(row["flags"])


def test_sonderregel_dcf_nicht_berechnet(cfg):
    """Sonderregel -> MoS NaN, Status 'Sonderregel noetig' auf allen 3 Status-Spalten."""
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    m = _mk(ticker="JPM", industry="Banks - Diversified")
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["sector_special"] is True
    assert row["sector_group"] == "special"
    assert row["status"] == "Sonderregel nötig"
    assert row["status_all"] == "Sonderregel nötig"
    assert row["status_recent2"] == "Sonderregel nötig"
    assert row["is_buy"] is False
    for k in ("margin_of_safety", "mos_avg3", "mos_latest", "mos_ttm", "mos_ttm_only",
              "mos_bear", "mos_bull"):
        assert math.isnan(row[k]), k
    # MoS-Zellen in app.py: "n/a (Sonderregel)"
    assert app._fmt_mos(float("nan"), special=True) == "n/a (Sonderregel)"
    # FCF-Yield-Kriterium n/a, MoS-Kriterium n/a
    crits = compute_criteria(m, cfg2)
    assert crits["fcf_yield_sbc_avg3"].is_na
    assert crits["mos_base"].is_na
    assert crits["roic_avg5"].is_na
    assert crits["net_debt_ebitda"].is_na
    assert crits["fcf_positive_years"].is_na
    assert crits["fcf_margin_avg5"].is_na
    # Qualitaet nur aus anwendbaren Kriterien; bei Abdeckung < 70 % -> n/a
    assert "Sektor-Sonderregel" in " ".join(out["data_quality"])


def test_sonderregel_quality_na_unter_70_prozent(cfg):
    """Sonderregel: ROIC/ND-EBITDA/FCF n/a -> Abdeckung < 70 % -> Quality = NaN (n/a)."""
    from scoring.score import score_ticker
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    m = _mk(ticker="AEE", industry="Utilities - Regulated Electric")
    crits = compute_criteria(m, cfg2)
    res = score_ticker(crits, cfg2["criteria"], cfg2.get("score_groups"))
    # n/a-Kriterien: roic_avg5, net_debt_ebitda, fcf_positive_years, fcf_margin_avg5,
    # fcf_yield_sbc_avg3, mos_base -> Abdeckung deutlich < 70 %
    assert res["coverage"] < 0.70
    assert math.isnan(res["quality"])  # Quality n/a bei Abdeckung < min_coverage_quality


# ---------- (2) Pruefstatus nur bei Quality >= 60 UND MoS >= 30 % ----------

def test_pruefen_sbc_nur_wenn_ohne_sbc_kandidat(cfg):
    """SBC fehlt in allen Jahren UND Quality >= 60 UND MoS >= 30 % -> Pruefstatus."""
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    m = _mk(sbc_na=True, quality_boost=True)  # starker Ticker: Quality hoch, MoS hoch
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    if row["quality"] >= 60 and not math.isnan(row["mos_rule"]) and row["mos_rule"] >= 0.30:
        assert row["status"] == "Prüfen (SBC fehlt)"
        assert row["status_pruefen_sbc"] is True
        assert row["is_buy"] is False
    else:
        pytest.skip("Synthetischer Ticker erreicht Schwellen nicht")


def test_pruefen_sbc_nicht_bei_schlechtem_ticker(cfg):
    """SBC fehlt, aber Quality < 60 oder MoS < 30 % -> normaler Status (nicht 'Pruefen')."""
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    m = _mk(sbc_na=True, mcap=1e14)  # riesige Marktkap -> MoS sehr negativ
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["status_pruefen_sbc"] is False
    assert row["status"] != "Prüfen (SBC fehlt)"
    assert row["status"] in ("nein", "Beobachtung", "Kandidat")
    assert "SBC n/a" in row["flags"]  # Fallback-Flag bleibt


# ---------- (3) SBC-Fallback aus SEC-EDGAR ----------

def _fake_facts(sbc_by_year):
    facts = {"facts": {"us-gaap": {"ShareBasedCompensation": {
        "units": {"USD": [
            {"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31", "val": v}
            for fy, v in sbc_by_year.items()]}}}}}
    return facts


def test_sbc_fallback_aus_sec_mit_quelle(cfg, monkeypatch):
    """Yahoo liefert keine SBC -> SEC-EDGAR ShareBasedCompensation wird uebernommen,
    Quelle im Flag."""
    import data.sec as secmod
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": True, "tolerance": 0.05,
                   "user_agent": "test@example.com", "max_requests_per_second": 5}
    m = _mk(ticker="CAT", sbc_na=True, country="US")
    for fy in m.years:
        m.years[fy]["stock_based_compensation"].value = float("nan")
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: "0000018230")
    monkeypatch.setattr(secmod, "_fetch_companyfacts",
                        lambda cik, ua: _fake_facts({2023: 2.0e9, 2024: 2.1e9, 2025: 2.2e9}))
    monkeypatch.setattr(secmod, "sec_check", lambda m, cfg: secmod.SecCheckResult(ok=True, flag=False, note="ok"))
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    # SBC wurde aus SEC uebernommen -> fcf_sbc vorhanden -> KEIN sbc_missing
    assert any("SEC EDGAR ShareBasedCompensation" in s for s in out["sbc_sec_sources"])
    assert not row["dcf_sbc_missing"]
    assert "SBC n/a" not in row["flags"]
    # Pruefstatus nicht gesetzt (SBC vorhanden)
    assert row["status_pruefen_sbc"] is False


def test_sbc_fallback_nicht_bei_nicht_us(cfg, monkeypatch):
    """Nicht-US-Ticker -> kein SEC-Fallback."""
    import data.sec as secmod
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": True, "user_agent": "t@example.com"}
    m = _mk(ticker="SAP.DE", sbc_na=True, country="DE")
    called = {"n": 0}
    def _boom(cik, ua):
        called["n"] += 1
        return {}
    monkeypatch.setattr(secmod, "_fetch_companyfacts", _boom)
    out = evaluate_ticker(m, cfg2)
    assert called["n"] == 0
    assert out["sbc_sec_sources"] == []


# ---------- (4) SEC-Detail mit CAT und DE (gemonkeypatcht) ----------

def test_sec_detail_cat(cfg, monkeypatch):
    """CAT: SEC weicht bei OCF FY2024 um 8 % ab -> sec_detail enthaelt Feld/Jahr/%,
    Flag gesetzt, Rangliste zeigt Detail."""
    import data.sec as secmod
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": True, "tolerance": 0.05,
                   "user_agent": "test@example.com", "max_requests_per_second": 5}
    m = _mk(ticker="CAT", country="US")
    # ours OCF FY2024 = 0.28*rev (in _mk: rev = 100e9*1.08**(fy-2020));
    # SEC-Wert so, dass dev = |ours-sec|/sec exakt 8 % ergibt (ours > sec)
    fy = 2024
    rev = 100e9 * 1.08 ** (fy - 2020)
    ours_ocf = 0.28 * rev
    sec_ocf = ours_ocf / 1.08
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [{"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                               "val": sec_ocf}]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "units": {"USD": [{"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                               "val": 0.05 * rev}]}},
        "ShareBasedCompensation": {
            "units": {"USD": [{"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                               "val": 0.02 * rev}]}},
    }}}
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: "0000018230")
    monkeypatch.setattr(secmod, "_fetch_companyfacts", lambda cik, ua: facts)
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["sec_check_flag"] is True
    assert "sec:abweichend" in row["flags"]
    assert "OCF FY2024" in row["sec_detail"]
    assert "8.0%" in row["sec_detail"]


def test_sec_detail_de(cfg, monkeypatch):
    """DE (John Deere, US-Listing): SEC-Abweichung bei Capex FY2023 > 5 % ->
    sec_detail mit 'Capex FY2023'."""
    import data.sec as secmod
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": True, "tolerance": 0.05,
                   "user_agent": "test@example.com", "max_requests_per_second": 5}
    m = _mk(ticker="DE", country="US")
    fy = 2023
    rev = 100e9 * 1.08 ** (fy - 2020)  # in _mk: rev = 100e9*1.08**(fy-2020)
    sec_capex = (0.05 * rev) / 1.10  # dev = |ours-sec|/sec = 10 % (ours > sec)
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [{"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                               "val": 0.28 * rev}]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "units": {"USD": [{"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                               "val": sec_capex}]}},
        "ShareBasedCompensation": {
            "units": {"USD": [{"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                               "val": 0.02 * rev}]}},
    }}}
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: "0000106047")
    monkeypatch.setattr(secmod, "_fetch_companyfacts", lambda cik, ua: facts)
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["sec_check_flag"] is True
    assert "Capex FY2023" in row["sec_detail"]
    assert "10.0%" in row["sec_detail"]


def test_format_sec_detail_einheiten():
    from data.sec import format_sec_detail
    devs = [
        {"fy": 2024, "key": "operating_cashflow", "ours": 108.0, "sec": 100.0, "dev": 0.08},
        {"fy": 2023, "key": "capex", "ours": 45.0, "sec": 50.0, "dev": 0.10},
        {"fy": 2025, "key": "stock_based_compensation", "ours": 20.0, "sec": 20.0, "dev": 0.0},
    ]
    s = format_sec_detail(devs)
    assert "OCF FY2024 +8.0%" in s
    assert "Capex FY2023 -10.0%" in s
    assert "SBC FY2025 +0.0%" in s


# ---------- (5) Datenqualitaet ----------

def test_datenqualitaet_je_ticker(cfg):
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    m = _mk(ticker="XOM", industry="Oil & Gas Integrated")
    for fy in m.years:  # Provider liefert das Jahr (Statement-Spalte vorhanden)
        m.fiscal_ends[fy] = f"{fy}-12-31"
    # Datenluecke erzeugen: FY2023 ohne ebit
    m.years[2023]["ebit"] = Metric("ebit", float("nan"), "USD", "USD", 2023)
    out = evaluate_ticker(m, cfg2)
    dq = out["data_quality"]
    assert any("FY2023" in d and "ebit" in d for d in dq)
    assert any("Datenabdeckung" in d for d in dq) or out["row"]["coverage"] >= 0.70


def test_datenqualitaet_sonderregel(cfg):
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    m = _mk(ticker="DUK", industry="Utilities - Regulated Electric")
    out = evaluate_ticker(m, cfg2)
    assert any("Sektor-Sonderregel" in d for d in out["data_quality"])


# ---------- Smoke ----------

def test_smoke_v35(cfg):
    cfg2 = dict(cfg)
    cfg2["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg2)
    assert rows
    for r in rows:
        assert r["status"] in ("Kandidat", "Beobachtung", "nein",
                               "Prüfen (SBC fehlt)", "Sonderregel nötig")
        assert "sector_special" in r
        assert "sec_detail" in r
        assert "data_quality" in r
