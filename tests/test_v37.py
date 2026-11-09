"""Tests fuer v37:
(1) Szenario-Wachstum: CAGR >= 0 unveraendert; CAGR < 0 -> Base = CAGR,
    Bear = CAGR - bear_negative_spread (3 pp), Bull = 0 %. Test: Wert je Aktie
    Bear <= Base <= Bull fuer CAGR -10 %, 0 %, 4 %, 12 %; scenario_growth
    enthaelt die tatsaechlichen Raten.
(2) Quality-Abdeckung: min_coverage_quality gilt nur fuer die Quality-Bloecke
    -- Value-Luecken (kein Marktkap. -> FCF-Yield/MoS/EV-EBIT n/a) setzen
    Quality NICHT auf n/a. DOW-aehnlicher Ticker liefert Quality-Score.
(3) Capex-Regel: Pruefstatus nur, wenn |Yahoo - SEC| in >= min_years Jahren
    > ocf_share (10 %) des OCF. MA ohne Pruefstatus, CAT und DE mit;
    SEC-Detail enthaelt Yahoo/SEC-Capex in Mrd. USD und Diff in % des OCF.
"""

import math

import pytest

from data.loader import Metric, TickerMetrics
from scoring.criteria import compute_criteria
from scoring.pipeline import evaluate_ticker
from scoring.score import score_ticker
from valuation.dcf import compute_dcf

# wiederverwendete Mocks aus test_
from tests.test_v36 import _cfg, _facts_capex, _mk


def _cfg_v40(cfg, sec_enabled=False):
    """wie _cfg, aber captive_finance.tickers geleert (sonst waere CAT/DE
    zusaetzlich 'Pruefen (Captive Finance)')."""
    cfg2 = _cfg(cfg, sec_enabled=sec_enabled)
    cfg2["captive_finance"] = {"tickers": []}
    return cfg2

DCF_CFG = {"dcf": {
    "discount_rate": 0.10, "terminal_growth": 0.025,
    "base_growth_cap": 0.06, "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10, "fade_to_growth": 0.025,
    "fcf_avg_years": 3, "fcf_mode": "latest", "net_debt_mode": "reported",
    "mos_min_mode": "min", "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30, "buy_bear_floor": 0.80,
    "watch_margin_of_safety_min": 0.30,
    "bear_negative_spread": 0.03,
}}


# ---------- (1) Szenario-Wachstum bei negativem CAGR ----------

def _mk_cagr(cagr: float, fcf: float = 20e9) -> TickerMetrics:
    """Ticker mit 5 GJ, dessen Umsatz exakt den CAGR `cagr` hat (2021->2025)."""
    m = TickerMetrics(ticker="G", country="US")
    m.market_cap = Metric("market_cap", 100e9, "USD", "USD", 2025)
    m.industry = "Technology"
    rev0 = 100e9
    rev = rev0
    for fy in range(2021, 2026):
        if fy > 2021:
            rev = rev0 * (1.0 + cagr) ** (fy - 2021)
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fcf, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "net_debt": Metric("net_debt", 0.0, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


@pytest.mark.parametrize("cagr", [-0.10, 0.0, 0.04, 0.12, 0.30, 0.60, 1.20])
def test_wert_je_aktie_bear_le_base_le_bull(cagr):
    """Wert je Aktie Bear <= Base <= Bull fuer CAGR -10/0/4/12 %.
    erweitert um CAGR 30 %, 60 %, 120 % (hohes Wachstum, Caps greifen)."""
    m = _mk_cagr(cagr)
    res = compute_dcf(m, DCF_CFG, fcf_mode="latest")
    assert res.revenue_cagr == pytest.approx(cagr, abs=1e-9)
    bear = res.scenarios["bear"].value_per_share
    base = res.scenarios["base"].value_per_share
    bull = res.scenarios["bull"].value_per_share
    for v in (bear, base, bull):
        assert v == v and v > 0
    assert bear <= base <= bull


@pytest.mark.parametrize("cagr", [0.30, 0.60, 1.20])
def test_wert_je_aktie_bear_le_base_le_bull_kleiner_fcf(cagr):
    """derselbe Check bei sehr kleinem positivem Start-FCF (1 Mio. USD)
    -- Skalierung des Start-FCF darf die Szenario-Reihenfolge nicht kippen."""
    m = _mk_cagr(cagr, fcf=1e6)
    res = compute_dcf(m, DCF_CFG, fcf_mode="latest")
    assert res.fcf_normalized > 0 and not res.fcf_le0
    bear = res.scenarios["bear"].value_per_share
    base = res.scenarios["base"].value_per_share
    bull = res.scenarios["bull"].value_per_share
    for v in (bear, base, bull):
        assert v == v and v > 0
    assert bear <= base <= bull


@pytest.mark.parametrize("cagr,exp_bear,exp_base,exp_bull", [
    (-0.10, -0.13, -0.10, 0.0),   # CAGR < 0: Bear = CAGR - 3 pp, Base = CAGR, Bull = 0 %
    (0.0, 0.0, 0.0, 0.0),         # CAGR = 0: alles 0 %
    (0.04, 0.0, 0.04, 0.052),     # CAGR >= 0: unveraendert (Bear 0, Bull CAGR*1.3)
    (0.12, 0.0, 0.06, 0.10),      # Caps greifen: Base 6 %, Bull 10 %
])
def test_scenario_growth_je_cagr(cagr, exp_bear, exp_base, exp_bull):
    """scenario_growth enthaelt die tatsaechlichen Wachstumsraten."""
    m = _mk_cagr(cagr)
    res = compute_dcf(m, DCF_CFG, fcf_mode="latest")
    assert res.scenario_growth["bear"] == pytest.approx(exp_bear, abs=1e-9)
    assert res.scenario_growth["base"] == pytest.approx(exp_base, abs=1e-9)
    assert res.scenario_growth["bull"] == pytest.approx(exp_bull, abs=1e-9)


def test_bear_negative_spread_in_config():
    """Schwelle dcf.bear_negative_spread = 0.03 in config.yaml."""
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                         .read_text(encoding="utf-8"))
    assert raw["dcf"]["bear_negative_spread"] == 0.03
    assert raw["score_groups"]["min_coverage_quality"] == 0.70
    assert raw["sec"]["capex_check"]["ocf_share"] == 0.10
    assert raw["sec"]["capex_check"]["min_years"] == 2


# ---------- (2) Quality-Abdeckung nur ueber Quality-Bloecke ----------

def _mk_dow_like() -> TickerMetrics:
    """DOW-aehnlich: Industry Chemicals (Standardgruppe), vollstaendige
    Quality-Daten, aber KEINE Marktkapitalisierung -> alle Value-Kriterien
    (FCF-Yield n. SBC, MoS, EV/EBIT) n/a."""
    m = TickerMetrics(ticker="DOW", country="US")
    m.name = "Dow Inc"
    m.sector = "Basic Materials"
    m.industry = "Chemicals"
    # market_cap absichtlich None -> Value-Block komplett n/a
    rev = 60e9
    for fy in range(2021, 2026):
        rev *= 1.03
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.35 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.12 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.18 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.09 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.14 * rev, "USD", "USD", fy),
            "capex": Metric("capex", -0.06 * rev, "USD", "USD", fy),
            "fcf": Metric("fcf", 0.08 * rev, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", 0.01 * rev, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", 0.07 * rev, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.01 * rev, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.03 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.12 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 0.6e9, "shares", "USD", fy),
            "equity": Metric("equity", 20e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 15e9, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


def test_dow_quality_score_trotz_value_luecken(cfg):
    """DOW-aehnlich -- Value-Block komplett n/a (keine Marktkap.),
    Gesamtabdeckung < 70 %, aber Quality-Abdeckung = 100 % -> Quality-Score
    wird geliefert (in v34 wurde Quality hier fälschlich n/a)."""
    m = _mk_dow_like()
    criteria = compute_criteria(m, cfg)
    # alle Value-Kriterien n/a, alle Quality-Kriterien bewertet
    val_na = [n for n in ("fcf_yield_sbc_avg3", "mos_base", "ev_ebit_vs_median")
              if criteria[n].is_na]
    assert len(val_na) == 3
    res = score_ticker(criteria, cfg["criteria"], cfg.get("score_groups"))
    assert res["quality_coverage"] == pytest.approx(1.0)
    assert res["coverage"] < 0.70            # Gesamtabdeckung niedrig ...
    assert res["value"] != res["value"]       # ... Value n/a ...
    assert res["quality"] == res["quality"]   # ... aber Quality LIEFERT einen Score
    assert res["quality"] > 0


def test_quality_na_wenn_quality_abdeckung_unter_70(cfg):
    """Gegenprobe: Quality-Bloecke selbst lueckig (ROIC, ROE, Zinsdeckung,
    FCF-Marge n/a -> Quality-Abdeckung 70 % < ... bzw. mit ND/EBITDA nur noch
    60 % < 70 %) -> Quality n/a."""
    m = _mk_dow_like()
    for fy in m.years:
        for k in ("income_tax_expense", "interest_expense", "ebitda", "fcf"):
            m.years[fy][k] = Metric(k, float("nan"), "USD", "USD", fy)
    criteria = compute_criteria(m, cfg)
    res = score_ticker(criteria, cfg["criteria"], cfg.get("score_groups"))
    na_names = [n for n, c in criteria.items()
                if c.is_na and cfg["criteria"][n]["block"] in cfg["score_groups"]["quality_blocks"]]
    assert "roic_avg5" in na_names and "interest_coverage" in na_names
    assert res["quality_coverage"] < 0.70
    assert res["quality"] != res["quality"]  # n/a


def test_row_traegt_quality_coverage(cfg):
    """Pipeline-Row enthaelt quality_coverage (nur Quality-Bloecke)."""
    cfg2 = _cfg(cfg)
    m = _mk(ticker="TEST", mcap=2e11)
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert "quality_coverage" in row
    assert row["quality_coverage"] == pytest.approx(1.0)
    assert row["quality"] == row["quality"]


# ---------- (3) Capex-Regel: Diff > 10 % des OCF in >= 2 Jahren ----------

def _run_capex(cfg, monkeypatch, ticker, cik, dev_years):
    import data.sec as secmod
    cfg2 = _cfg_v40(cfg, sec_enabled=True)  # ohne captive_finance
    m = _mk(ticker=ticker, mcap=2e11)
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: cik)
    monkeypatch.setattr(secmod, "_fetch_companyfacts",
                        lambda cik, ua: _facts_capex(dev_years))
    return evaluate_ticker(m, cfg2)


def test_ma_ohne_pruefstatus(cfg, monkeypatch):
    """MA -- SEC-Capex nahe an Yahoo (Diff < 10 % des OCF in allen
    Jahren) -> KEIN Pruefstatus."""
    out = _run_capex(cfg, monkeypatch, "MA", "0000169331",
                     {2025: 0.05, 2024: 0.05, 2023: 0.05})
    row = out["row"]
    assert row["status_pruefen_capex"] is False
    assert row["status"] != "Pr\u00fcfen (Capex-Definition)"
    assert "capex:pruefen" not in row["flags"]


def test_cat_mit_pruefstatus(cfg, monkeypatch):
    """CAT -- Diff > 10 % des OCF in 2 Jahren -> Pruefstatus, nie Kandidat."""
    out = _run_capex(cfg, monkeypatch, "CAT", "0000018230",
                     {2025: 3.0, 2024: 3.0})
    row = out["row"]
    assert row["status_pruefen_capex"] is True
    assert row["status"] == "Pr\u00fcfen (Capex-Definition)"
    assert row["is_buy"] is False
    assert "capex:pruefen" in row["flags"]
    assert len(row["capex_dev_years"]) == 2
    # SEC-Detail mit Mrd. USD und % des OCF je Jahr
    assert "Mrd. USD" in row["sec_detail"]
    assert "des OCF" in row["sec_detail"]
    assert "Capex FY2025" in row["sec_detail"] and "Capex FY2024" in row["sec_detail"]


def test_de_mit_pruefstatus(cfg, monkeypatch):
    """DE -- Diff > 10 % des OCF in 2 Jahren -> Pruefstatus."""
    out = _run_capex(cfg, monkeypatch, "DE", "0000106047",
                     {2025: 3.0, 2023: 3.0})
    row = out["row"]
    assert row["status_pruefen_capex"] is True
    assert row["status"] == "Pr\u00fcfen (Capex-Definition)"
    assert len(row["capex_dev_years"]) == 2


def test_capex_diff_unter_10_prozent_ocf_kein_status(cfg, monkeypatch):
    """2 Jahre mit Abweichung, aber Diff <= 10 % des OCF
    (dev 0.10 -> Diff ~0.45 % Umsatz < 2.8 % Umsatz = 10 % OCF) -> kein Status."""
    out = _run_capex(cfg, monkeypatch, "DE", "0000106047",
                     {2025: 0.10, 2024: 0.10})
    row = out["row"]
    assert row["status_pruefen_capex"] is False
    assert row["capex_dev_years"] == []


def test_format_sec_detail_mit_ocf(cfg):
    """format_sec_detail mit ocf_by_fy liefert Yahoo/SEC-Capex in
    Mrd. USD und Diff in % des OCF je Jahr."""
    from data.sec import format_sec_detail
    devs = [{"fy": 2024, "key": "capex", "ours": -5.0e9, "sec": -1.25e9, "dev": 3.0}]
    ocf_by_fy = {2024: 30.0e9}
    txt = format_sec_detail(devs, ocf_by_fy=ocf_by_fy)
    assert "Capex FY2024" in txt
    assert "Yahoo 5.00 / SEC -1.25 Mrd. USD" in txt
    assert "Diff 3.75 Mrd. = 12.5% des OCF" in txt
    # ohne ocf_by_fy -> alte Kurzform
    txt2 = format_sec_detail(devs)
    assert "Capex FY2024" in txt2 and "Mrd." not in txt2
