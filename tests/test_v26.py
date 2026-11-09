"""Tests fuer v26:
(1) umschaltbare Nettoverschuldung (reported / incl_sti) + beide MoS
(2) MoS je Startwert-Modus + Kandidatenregel nutzt Minimum (config mos_min_mode)
(3) TTM-Quarters (4 letzte Quartale mit Stichtag)
(4) SEC-EDGAR-Abgleich (User-Agent-Platzhalter, Flag bei > 5 %)
(5) Smoke: evaluate_all ueber alle Demo-Ticker + neue Row-Spalten
"""

import math

import pandas as pd
import pytest

from data.loader import Metric, TickerMetrics
from data.ttm import TtmResult, compute_ttm
from valuation.dcf import compute_dcf, is_buy_candidate


@pytest.fixture
def cfg_sec():
    """Config mit aktiviertem SEC-Abgleich (fuer die data.sec-Tests, die
    _cik_for_ticker/_fetch_companyfacts monkeypatchen)."""
    return {"sec": {"user_agent": "Value-Screener research@example.com",
                    "enabled": True, "tolerance": 0.05}}

DCF_CFG = {"dcf": {
    "discount_rate": 0.10,
    "terminal_growth": 0.025,
    "base_growth_cap": 0.06,
    "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10,
    "fade_to_growth": 0.025,
    "fcf_avg_years": 3,
    "fcf_mode": "auto",
    "net_debt_mode": "reported",
    "mos_min_mode": "min",
    "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30,
    "buy_bear_floor": 0.80,
}, "sec": {"user_agent": "Value-Screener test@example.com",
           "enabled": True, "tolerance": 0.05}}


def _ticker(fcf_sbc_by_fy=None, mcap: float = 1000.0, shares: float = 10.0,
            cagr: float = 0.05, net_debt: float = float("nan"),
            total_debt: float = float("nan"), cash: float = float("nan"),
            sti: float = float("nan"),
            customer_deposits: float = float("nan")) -> TickerMetrics:
    if fcf_sbc_by_fy is None:
        fcf_sbc_by_fy = {2023: 100.0, 2024: 100.0, 2025: 100.0}
    m = TickerMetrics(ticker="TEST", country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev0 = 1000.0
    for i, fy in enumerate(sorted(fcf_sbc_by_fy)):
        y = {
            "fcf_sbc": Metric("fcf_sbc", fcf_sbc_by_fy[fy], "USD", "USD", fy),
            "revenue": Metric("revenue", rev0 * (1 + cagr) ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
        }
        if net_debt == net_debt:
            y["net_debt"] = Metric("net_debt", net_debt, "USD", "USD", fy)
        if total_debt == total_debt:
            y["_total_debt"] = Metric("_total_debt", total_debt, "USD", "USD", fy)
        if cash == cash:
            y["_cash"] = Metric("_cash", cash, "USD", "USD", fy)
        if sti == sti:
            y["_short_term_investments"] = Metric("_short_term_investments",
                                                  sti, "USD", "USD", fy)
        if customer_deposits == customer_deposits:
            y["customer_deposits"] = Metric("customer_deposits",
                                            customer_deposits, "USD", "USD", fy)
        m.years[fy] = y
    return m


# --- (1) umschaltbare Nettoverschuldung ---------------------------------------

def test_net_debt_mode_reported_vs_incl_sti():
    """reported = gemeldetes Net Debt; incl_sti = Total Debt − (Cash + kurzfr. Anlagen)."""
    m = _ticker(net_debt=100.0, total_debt=500.0, cash=200.0, sti=50.0)
    rep = compute_dcf(m, DCF_CFG)  # Default reported
    assert rep.net_debt_mode == "reported"
    assert rep.net_debt == pytest.approx(100.0)
    # Alternative: 500 - (200 + 50) = 250
    assert rep.net_debt_alt == pytest.approx(250.0)
    assert "kurzfr. Anlagen" in rep.net_debt_alt_source

    cfg_sti = {"dcf": dict(DCF_CFG["dcf"], net_debt_mode="incl_sti")}
    sti_res = compute_dcf(m, cfg_sti)
    assert sti_res.net_debt_mode == "incl_sti"
    assert sti_res.net_debt == pytest.approx(250.0)
    assert sti_res.net_debt_alt == pytest.approx(100.0)  # andere Richtung


def test_mos_base_alt_neben_mos():
    """Beide MoS (Base) werden nebeneinander berechnet und unterscheiden sich."""
    m = _ticker(net_debt=100.0, total_debt=500.0, cash=200.0, sti=50.0)
    res = compute_dcf(m, DCF_CFG)
    # reported: ND 100 -> hoeherer Wert -> hoehere MoS
    # incl_sti: ND 250 -> niedrigerer Wert -> niedrigere MoS
    assert res.margin_of_safety == pytest.approx(1 - 100.0 / res.scenarios["base"].value_per_share)
    assert res.mos_base_alt == res.mos_base_alt  # nicht NaN
    assert res.mos_base_alt < res.margin_of_safety


def test_net_debt_mode_invalid_falls_back_to_reported():
    cfg_bad = {"dcf": dict(DCF_CFG["dcf"], net_debt_mode="unsinn")}
    res = compute_dcf(_ticker(net_debt=100.0), cfg_bad)
    assert res.net_debt_mode == "reported"


def test_net_debt_incl_sti_mit_kundengeldnern():
    """incl_sti addiert Kundengelder hinzu (wie gemeldet)."""
    m = _ticker(total_debt=500.0, cash=200.0, sti=50.0,
                customer_deposits=30.0)
    cfg_sti = {"dcf": dict(DCF_CFG["dcf"], net_debt_mode="incl_sti")}
    res = compute_dcf(m, cfg_sti)
    # 500 - (200 + 50) + 30 Kundengelder = 280
    assert res.net_debt == pytest.approx(280.0)


# --- (2) MoS je Startwert + Kandidatenregel Minimum ---------------------------

def test_mos_by_mode_in_row(cfg):
    """Pipeline-Row enthaelt mos_avg3, mos_latest, mos_ttm, mos_rule."""
    from scoring.pipeline import evaluate_ticker
    from data.demo import demo_companies
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    out = evaluate_ticker(qual, cfg)
    row = out["row"]
    for col in ("mos_avg3", "mos_latest", "mos_ttm", "mos_ttm_only",
                "mos_rule", "mos_rule_mode"):
        assert col in row, f"fehlt: {col}"
    assert row["mos_rule_mode"] == cfg["dcf"].get("mos_min_mode", "min")


def test_mos_rule_min_ist_minimum(cfg):
    """mos_rule = Minimum der drei Startwert-MoS (config mos_min_mode=min)."""
    from scoring.pipeline import evaluate_ticker
    from data.demo import demo_companies
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    out = evaluate_ticker(qual, cfg)
    row = out["row"]
    vals = [v for v in (row["mos_avg3"], row["mos_latest"], row["mos_ttm"],
                        row["mos_ttm_only"]) if v == v]
    if vals:
        assert row["mos_rule"] == pytest.approx(min(vals))


def test_is_buy_candidate_mit_mos_rule_parameter():
    """is_buy_candidate nutzt mos_rule statt dcf.margin_of_safety."""
    res = compute_dcf(_ticker(net_debt=200.0), DCF_CFG)
    price = 100.0
    # dcf.margin_of_safety = 1 - 100/136.90 = 26.95 % < 30 %
    assert res.margin_of_safety < 0.30
    assert not is_buy_candidate(75.0, res, price, DCF_CFG)
    # Mit kuenstlich hohem mos_rule (>= 30 %) + Bear ok -> Kandidat
    assert is_buy_candidate(75.0, res, price, DCF_CFG, mos_rule=0.35) is True
    # Mit niedrigem mos_rule -> kein Kandidat (auch wenn dcf.MoS hoch waere)
    assert not is_buy_candidate(75.0, res, price, DCF_CFG, mos_rule=0.10)


# --- (3) TTM-Quarters -----------------------------------------------------------

ROW_MAP = {
    "revenue": ["Total Revenue"],
    "operating_cashflow": ["Operating Cash Flow"],
    "capex": ["Capital Expenditure"],
    "fcf": ["Free Cash Flow"],
    "stock_based_compensation": ["Stock Based Compensation"],
    "net_debt": ["Net Debt"],
    "equity": ["Stockholders Equity"],
}


def _qframes(quarters, ocf_per_q, capex_per_q, sbc_per_q, fcf_per_q=None):
    cols = list(quarters)
    n = len(cols)
    if fcf_per_q is None:
        fcf_per_q = [o + c for o, c in zip(ocf_per_q, capex_per_q)]
    cf = pd.DataFrame({
        "Operating Cash Flow": list(ocf_per_q),
        "Capital Expenditure": list(capex_per_q),
        "Free Cash Flow": list(fcf_per_q),
        "Stock Based Compensation": list(sbc_per_q),
    }, index=cols).T
    inc = pd.DataFrame({"Total Revenue": [1e9] * n}, index=cols).T
    bal = pd.DataFrame({"Net Debt": [0.0] * n, "Stockholders Equity": [1e9] * n},
                       index=cols).T
    return {"income": inc, "balance": bal, "cashflow": cf, "row_map": ROW_MAP}


def test_ttm_quarters_letzte_8_mit_stichtag_und_yoy():
    """TtmResult.quarters: bis zu 8 neueste Quartale (neuestes zuerst),
    inkl. YoY (FCF-SBC vs. Vorjahresquartal, 4 Quartale zurueck)."""
    quarters = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-31"),
                pd.Timestamp("2026-02-28"), pd.Timestamp("2025-11-30"),
                pd.Timestamp("2025-08-31"), pd.Timestamp("2025-05-31"),
                pd.Timestamp("2025-02-28"), pd.Timestamp("2024-11-30"),
                pd.Timestamp("2024-08-31")]  # 9 Quartale -> nur 8 uebernommen
    ocf = [3e9, 3e9, 3e9, 3e9, 99e9, 1e9, 1e9, 1e9, 777e9]
    capex = [-1e9] * 9
    sbc = [0.5e9] * 9
    frames = _qframes(quarters, ocf, capex, sbc)
    res = compute_ttm(TickerMetrics(ticker="T"), frames)
    assert len(res.quarters) == 8
    # neuestes zuerst; das 9. (aelteste, 2024-08) nicht enthalten
    assert res.quarters[0]["quarter_end"] == "2026-08-31"
    assert res.quarters[-1]["quarter_end"] == "2024-11-30"
    q0 = res.quarters[0]
    assert q0["operating_cashflow"] == pytest.approx(3e9)
    assert q0["capex"] == pytest.approx(-1e9)
    assert q0["stock_based_compensation"] == pytest.approx(0.5e9)
    assert q0["fcf_sbc"] == pytest.approx(3e9 - 1e9 - 0.5e9)
    # YoY: quarters[0] vs. quarters[4] -> 1.5e9 / 98.5e9 - 1 (sehr negativ)
    assert q0["yoy"] == pytest.approx(q0["fcf_sbc"] / res.quarters[4]["fcf_sbc"] - 1.0)
    # Vorjahresquartale (i >= 4) haben kein YoY (kein i+4 vorhanden)
    assert res.quarters[4]["yoy"] != res.quarters[4]["yoy"]  # NaN


def test_ttm_quarters_fcf_fallback_ocf_plus_capex():
    """Kein gemeldeter FCF -> FCF = OCF + Capex (Capex negativ)."""
    quarters = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-31"),
                pd.Timestamp("2026-02-28"), pd.Timestamp("2025-11-30"),
                pd.Timestamp("2025-08-31"), pd.Timestamp("2025-05-31"),
                pd.Timestamp("2025-02-28"), pd.Timestamp("2024-11-30")]
    frames = _qframes(quarters, [3e9] * 8, [-1e9] * 8, [0.5e9] * 8,
                      fcf_per_q=[float("nan")] * 8)
    res = compute_ttm(TickerMetrics(ticker="T"), frames)
    assert len(res.quarters) == 8
    assert res.quarters[0]["fcf_sbc"] == pytest.approx(3e9 - 1e9 - 0.5e9)
    # YoY bei gleichem Niveau = 0 %
    assert res.quarters[0]["yoy"] == pytest.approx(0.0)


# --- (4) SEC-EDGAR-Abgleich -----------------------------------------------------

def test_sec_check_deaktiviert(cfg_sec):
    from data.sec import sec_check
    from data.demo import demo_companies
    cfg_off = dict(cfg_sec)
    cfg_off["sec"] = {"enabled": False}
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    res = sec_check(qual, cfg_off)
    assert not res.ok
    assert not res.flag
    assert "deaktiviert" in res.note


def test_sec_check_nicht_us(cfg_sec):
    from data.sec import sec_check
    from data.demo import demo_companies
    sie = next(c for c in demo_companies() if c.ticker == "SIE")  # DE
    res = sec_check(sie, cfg_sec)
    assert not res.ok
    assert not res.flag
    assert "kein US-Ticker" in res.note


def test_sec_check_user_agent_aus_config(cfg_sec, monkeypatch):
    """User-Agent kommt aus config.yaml (Platzhalter mit E-Mail)."""
    from data import sec as sec_mod
    assert cfg_sec["sec"]["user_agent"] == "Value-Screener research@example.com"
    captured = {}

    def fake_fetch(cik, user_agent):
        captured["ua"] = user_agent
        return None

    monkeypatch.setattr(sec_mod, "_fetch_companyfacts", fake_fetch)
    monkeypatch.setattr(sec_mod, "_cik_for_ticker", lambda t: "0000320193")
    from data.demo import demo_companies
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    sec_mod.sec_check(qual, cfg_sec)
    assert captured["ua"] == "Value-Screener research@example.com"


def test_sec_check_flag_bei_abweichung(cfg_sec, monkeypatch):
    """Abweichung > tolerance -> flag=True; sonst False."""
    from data import sec as sec_mod

    # Synthetische companyfacts: OCF fuer FY2025 weicht um 20 % ab
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [
                {"form": "10-K", "start": "2024-09-01", "end": "2025-09-27", "val": 120e9},
            ]},
        },
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "units": {"USD": [
                {"form": "10-K", "start": "2024-09-01", "end": "2025-09-27", "val": 12.715e9},
            ]},
        },
        "ShareBasedCompensation": {
            "units": {"USD": [
                {"form": "10-K", "start": "2024-09-01", "end": "2025-09-27", "val": 11.688e9},
            ]},
        },
    }}}
    monkeypatch.setattr(sec_mod, "_cik_for_ticker", lambda t: "0000320193")
    monkeypatch.setattr(sec_mod, "_fetch_companyfacts", lambda cik, ua: facts)

    from data.demo import AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW, AAPL_PROFILE
    from data.loader import extract_metrics
    m = extract_metrics("AAPL", AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW, AAPL_PROFILE)
    m.country = "US"
    res = sec_mod.sec_check(m, cfg_sec)
    assert res.ok
    # OCF ours 111.48 vs sec 120 -> dev 7.0 % > 5 %
    ocf_dev = next(d for d in res.deviations if d["key"] == "operating_cashflow")
    assert ocf_dev["dev"] > 0.05
    assert res.flag


def test_sec_check_kein_flag_bei_kleiner_abweichung(cfg_sec, monkeypatch):
    """Alle Werte innerhalb tolerance -> flag=False."""
    from data import sec as sec_mod
    from data.demo import AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW, AAPL_PROFILE
    from data.loader import extract_metrics
    m = extract_metrics("AAPL", AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW, AAPL_PROFILE)
    m.country = "US"
    # SEC-Werte = unsere Werte (aus den Jahresdaten)
    sec_vals = {}
    for fy in sorted(m.years):
        y = m.years[fy]
        sec_vals[fy] = {
            "ocf": y["operating_cashflow"].value,
            "capex": -y["capex"].value,  # SEC positiv
            "sbc": y["stock_based_compensation"].value,
        }
    facts = {"facts": {"us-gaap": {}}}
    for key, tag in (("operating_cashflow", "NetCashProvidedByUsedInOperatingActivities"),
                     ("capex", "PaymentsToAcquirePropertyPlantAndEquipment"),
                     ("stock_based_compensation", "ShareBasedCompensation")):
        k = "ocf" if key == "operating_cashflow" else ("capex" if key == "capex" else "sbc")
        facts["facts"]["us-gaap"][tag] = {"units": {"USD": [
            {"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
             "val": sec_vals[fy][k]} for fy in sorted(sec_vals)
        ]}}
    monkeypatch.setattr(sec_mod, "_cik_for_ticker", lambda t: "0000320193")
    monkeypatch.setattr(sec_mod, "_fetch_companyfacts", lambda cik, ua: facts)
    res = sec_mod.sec_check(m, cfg_sec)
    assert res.ok
    assert not res.flag
    assert res.deviations  # verglichen wurde trotzdem


def test_pipeline_flag_sec_abweichend(monkeypatch, cfg):
    """SEC-Abweichung > tolerance -> Flag 'sec:abweichend' in der Row."""
    from data import sec as sec_mod
    from scoring.pipeline import evaluate_ticker
    from data.demo import demo_companies

    class FakeRes:
        ok = True
        flag = True
        note = "Quellen weichen ab: max. 10.6%"
        deviations = []

    monkeypatch.setattr(sec_mod, "sec_check", lambda m, c: FakeRes())
    cfg_on = dict(cfg)
    cfg_on["sec"] = dict(cfg.get("sec", {}), enabled=True)  # Pipeline-Sec-Gate oeffnen
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    qual.country = "US"  # SEC-Abgleich aktivieren
    out = evaluate_ticker(qual, cfg_on)
    assert "sec:abweichend" in out["row"]["flags"]
    assert out["row"]["sec_check_flag"] is True
    assert out["row"]["sec_check_ok"] is True


# --- (5) Smoke: evaluate_all + neue Spalten ------------------------------------

V26_ROW_COLUMNS = {
    "mos_avg3", "mos_latest", "mos_ttm", "mos_rule", "mos_rule_mode",
    "mos_base_alt_nd", "dcf_net_debt_mode", "dcf_net_debt_alt",
    "dcf_net_debt_alt_source", "sec_check_ok", "sec_check_flag", "sec_check_note",
    #
    "mos_ttm_only", "status", "status_detail",
}


def test_evaluate_all_smoke_v26_spalten(cfg):
    """evaluate_all ueber alle Demo-Ticker: keine Ausnahme, neue v26-Spalten da.
    (cfg-Fixture deaktiviert den SEC-Abgleich -- kein Netzwerk im Smoke-Test.)"""
    from data.demo import demo_companies
    from scoring.pipeline import evaluate_all
    rows, excluded = evaluate_all(demo_companies(), cfg)
    assert rows
    for row in rows:
        missing = V26_ROW_COLUMNS - set(row)
        assert not missing, f"{row.get('ticker')}: fehlen {sorted(missing)}"


def test_app_row_columns_v26_aktualisiert():
    """Die in app.py angezeigten MoS-Spalten muessen in der Row vorhanden sein."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    # Spaltennamen aus show_cols extrahieren
    m = re.search(r"show_cols = \[(.*?)\]", src, re.S)
    assert m
    cols = re.findall(r'"([^"]+)"', m.group(1))
    mos_cols = [c for c in cols if c.startswith("MoS")]
    assert "MoS Ø3J" in mos_cols and "MoS jüngstes GJ" in mos_cols
    assert "MoS TTM Ø" in mos_cols and "MoS alt. ND" in mos_cols
