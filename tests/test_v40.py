"""Tests fuer v40:
(1) DCF-Kern: Start-FCF <= 0 im jeweiligen Modus -> Wert je Aktie UND MoS
    (Base/Bear/Bull, robust, alt-ND) in der BERECHNUNG n/a (NaN), nicht nur in
    der Anzeige; gilt auch fuer den aktiven Auto-Modus. Kandidatenregel behandelt
    solche Modi wie andere n/a-Modi; Begruendung nennt "Start-FCF <= 0 im Modus X".
    Test: Start-FCF -0.01 Mrd., Nettokasse positiv, CAGR 49 % -> alle Werte und
    MoS n/a. Start-FCF > 0 unveraendert, Bear <= Base <= Bull.
(2) Captive Finance: config captive_finance.tickers [DE, CAT, PCAR, GM, F] ->
    Flag "captive_finance", Status "Pruefen (Captive Finance)", nie Kandidat,
    Hinweis "Nettoverschuldung inkl. Finanzierungsarm, DCF nicht belastbar";
    Kombination mit "Pruefen (Capex-Definition)" nennt beide Gruende.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_ticker
from valuation.dcf import compute_dcf

DCF_CFG = {"dcf": {
    "discount_rate": 0.10, "terminal_growth": 0.025,
    "base_growth_cap": 0.06, "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10, "fade_to_growth": 0.025,
    "fcf_avg_years": 3, "fcf_mode": "latest", "net_debt_mode": "reported",
    "mos_min_mode": "min", "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30, "buy_bear_floor": 0.80,
    "watch_margin_of_safety_min": 0.30,
    "bear_negative_spread": 0.03,
    "robust_discount_rate": 0.10,
}}


def _mk(cagr: float, fcf: float, net_debt: float = -10e9, mcap: float = 100e9) -> TickerMetrics:
    """Ticker mit 5 GJ; `net_debt` negativ = Nettokasse positiv."""
    m = TickerMetrics(ticker="T", country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    m.industry = "Technology"
    rev0 = 100e9
    for fy in range(2021, 2026):
        rev = rev0 * (1.0 + cagr) ** (fy - 2021)
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fcf, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "net_debt": Metric("net_debt", net_debt, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


def _cfg_off(cfg, captive=None):
    cfg2 = dict(cfg)
    sec = dict(cfg2.get("sec", {}))
    sec["enabled"] = False
    cfg2["sec"] = sec
    cfg2["captive_finance"] = {"tickers": captive if captive is not None else []}
    return cfg2


# ---------- (1) Start-FCF <= 0 -> Werte UND MoS n/a in der Berechnung ----------

def test_fcf_le0_alle_werte_und_mos_na():
    """Start-FCF -0.01 Mrd., Nettokasse positiv (-10 Mrd.), CAGR 49 %
    -> Wert je Aktie Bear/Base/Bull = NaN, margin_of_safety = NaN, mos_base_alt NaN."""
    m = _mk(cagr=0.49, fcf=-0.01e9, net_debt=-10e9)
    res = compute_dcf(m, DCF_CFG, fcf_mode="latest")
    assert res.fcf_le0 is True
    assert res.revenue_cagr == pytest.approx(0.49, abs=1e-9)
    for name in ("bear", "base", "bull"):
        v = res.scenarios[name].value_per_share
        assert v != v, f"{name} vps muss NaN sein, ist {v}"
    assert res.margin_of_safety != res.margin_of_safety  # NaN
    assert res.mos_base_alt != res.mos_base_alt  # NaN
    # robust (10 %) im selben Modus ebenfalls n/a
    cfg_r = {"dcf": dict(DCF_CFG["dcf"], discount_rate=0.10)}
    res_r = compute_dcf(m, cfg_r, fcf_mode="latest")
    assert res_r.margin_of_safety != res_r.margin_of_safety


def test_fcf_le0_auch_im_auto_modus():
    """aktiver Auto-Modus (hier latest, da kein TTM) -> ebenfalls n/a."""
    m = _mk(cagr=0.49, fcf=-0.01e9)
    cfg_auto = {"dcf": dict(DCF_CFG["dcf"], fcf_mode="auto")}
    res = compute_dcf(m, cfg_auto)  # auto -> faellt auf avg3/latest zurueck
    assert res.fcf_le0 is True
    assert res.margin_of_safety != res.margin_of_safety
    assert res.scenarios["base"].value_per_share != res.scenarios["base"].value_per_share


@pytest.mark.parametrize("mode", ["avg3", "latest", "ttm", "ttm_only"])
def test_fcf_le0_jeder_modus_na(mode):
    """jeder Startwert-Modus mit Start-FCF <= 0 liefert n/a (MoS NaN)."""
    m = _mk(cagr=0.49, fcf=-5e9)
    res = compute_dcf(m, DCF_CFG, fcf_mode=mode)
    if res.fcf_le0:
        assert res.margin_of_safety != res.margin_of_safety
        for name in ("bear", "base", "bull"):
            assert res.scenarios[name].value_per_share != res.scenarios[name].value_per_share


def test_fcf_positiv_unveraendert_bear_le_base_le_bull():
    """Gegenprobe: Start-FCF > 0 -> alles berechnet, Bear <= Base <= Bull."""
    m = _mk(cagr=0.49, fcf=20e9)
    res = compute_dcf(m, DCF_CFG, fcf_mode="latest")
    assert res.fcf_le0 is False
    bear = res.scenarios["bear"].value_per_share
    base = res.scenarios["base"].value_per_share
    bull = res.scenarios["bull"].value_per_share
    for v in (bear, base, bull):
        assert v == v and v > 0
    assert bear <= base <= bull
    assert res.margin_of_safety == res.margin_of_safety


def test_pipeline_begruendung_nennt_modus_bei_fcf_le0(cfg):
    """Kandidatenregel behandelt fcf_le0-Modi wie n/a; die Begruendung
    nennt 'Start-FCF <= 0 im Modus X'."""
    m = _mk(cagr=0.49, fcf=-0.01e9, net_debt=-10e9)
    # vollstaendige Qualitaetsdaten, damit nur der FCF fehlt
    rev = 100e9
    for fy in range(2021, 2026):
        rev *= 1.49
        m.years[fy].update({
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.3 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.2 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.3 * rev, "USD", "USD", fy),
            "capex": Metric("capex", -0.05 * rev, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", 0.0, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.0, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
        })
    cfg2 = _cfg_off(cfg)
    # Negativ-FCF-Ausschluss aus, damit der Ticker bewertet wird (wir testen fcf_le0)
    filt = dict(cfg2["filters"])
    filt["min_avg_fcf_5y_usd"] = -1e18
    cfg2["filters"] = filt
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row is not None
    assert row["dcf_fcf_le0"] is True
    assert row["status"] == "nein"
    assert not out["dcf"].is_buy
    assert "Start-FCF \u2264 0 im Modus" in row["status_detail"]
    # MoS je Modus in der Row: alle NaN (Berechnung n/a)
    for col in ("mos_avg3", "mos_latest", "mos_ttm", "mos_ttm_only", "mos_robust", "mos_bear", "mos_bull"):
        v = row[col]
        assert v != v, f"{col} muss NaN sein"


# ---------- (2) Captive Finance ----------

def _mk_captive(ticker="DE", mcap=2e11) -> TickerMetrics:
    from tests.test_v36 import _mk
    m = _mk(ticker=ticker, mcap=mcap)
    return m


@pytest.mark.parametrize("ticker", ["DE", "CAT", "PCAR", "GM", "F"])
def test_captive_finance_flag_status_nie_kandidat(cfg, ticker):
    """captive_finance.tickers -> Flag 'captive_finance', Status
    'Pruefen (Captive Finance)', is_buy False, Hinweis im Detail."""
    cfg2 = _cfg_off(cfg, captive=["DE", "CAT", "PCAR", "GM", "F"])
    m = _mk_captive(ticker)
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row is not None
    assert "captive_finance" in row["flags"]
    assert row["status"] == "Pr\u00fcfen (Captive Finance)"
    assert row["status_all"] == "Pr\u00fcfen (Captive Finance)"
    assert row["status_recent2"] == "Pr\u00fcfen (Captive Finance)"
    assert out["dcf"].is_buy is False
    assert "Nettoverschuldung inkl. Finanzierungsarm" in row["status_detail"]
    assert "DCF nicht belastbar" in row["status_detail"]


def test_captive_finance_nicht_in_liste_kein_flag(cfg):
    """Ticker NICHT in captive_finance.tickers -> kein Flag, normaler Status."""
    cfg2 = _cfg_off(cfg, captive=["DE", "CAT", "PCAR", "GM", "F"])
    m = _mk_captive("AAPL")
    out = evaluate_ticker(m, cfg2)
    assert "captive_finance" not in out["row"]["flags"]
    assert out["row"]["status"] != "Pr\u00fcfen (Captive Finance)"


def test_captive_kombiniert_mit_capex_beide_gruende(cfg, monkeypatch):
    """CAT mit Capex-Pruefstatus UND in captive_finance.tickers ->
    Status nennt BEIDE Gruende."""
    import data.sec as secmod
    from tests.test_v37 import _cfg_v40, _facts_capex
    from tests.test_v36 import _mk
    cfg2 = _cfg_v40(cfg, sec_enabled=True)
    cfg2["captive_finance"] = {"tickers": ["DE", "CAT", "PCAR", "GM", "F"]}
    m = _mk(ticker="CAT", mcap=2e11)
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: "0000018230")
    monkeypatch.setattr(secmod, "_fetch_companyfacts",
                        lambda cik, ua: _facts_capex({2025: 3.0, 2024: 3.0}))
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["status_pruefen_capex"] is True
    assert "Pr\u00fcfen (Capex-Definition)" in row["status"]
    assert "Pr\u00fcfen (Captive Finance)" in row["status"]
    assert "captive_finance" in row["flags"]
    assert "capex:pruefen" in row["flags"]
    assert out["dcf"].is_buy is False
    assert "Finanzierungsarm" in row["status_detail"]


def test_captive_finance_in_config():
    """config.yaml: captive_finance.tickers = [DE, CAT, PCAR, GM, F]."""
    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                         .read_text(encoding="utf-8"))
    assert raw["captive_finance"]["tickers"] == ["DE", "CAT", "PCAR", "GM", "F"]
