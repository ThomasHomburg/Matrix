"""Tests fuer v33 -- die vier Pruef-Punkte mit je einem eigenen Testnamen:
(1) test_sbc_missing_fcf_without_sbc_and_flag  -- SBC fehlt -> FCF und DCF ohne
    SBC (Fallback Roh-FCF) mit Flag "SBC n/a (ohne SBC)".
(2) test_value_na_below_coverage_70            -- Value = n/a bei Datenabdeckung < 70 %
    (score_groups.min_coverage_value aus config.yaml).
(3) test_mos_cell_start_fcf_le0                -- MoS-Zelle "Start-FCF <= 0" statt n/a
    bei Start-FCF <= 0 (DcfResult.fcf_le0; Anzeige in app.py _fmt_mos + Modus-Tabelle).
(4) test_quality_rang_ist_ganzzahl             -- Quality-Rang als Ganzzahl
    (rank_rows -> int; app.py zeigt str(int)).
Plus Smoke ueber alle Demo-Ticker.
"""

import math

import pytest

from data.demo import demo_companies
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_all, evaluate_ticker, rank_rows
from scoring.score import score_ticker
from scoring.criteria import compute_criteria
from valuation.dcf import compute_dcf


def _mk(ticker="T", mcap=1e12, fcf_sbc_vals="default", fcf_vals=None):
    """5 Jahre; fcf_sbc_vals=None -> fcf_sbc = NaN (SBC fehlt); "default" = fcf - sbc."""
    m = TickerMetrics(ticker=ticker, country="US")
    m.name = ticker
    m.sector = "Technology"
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 100e9
    for i, fy in enumerate(range(2021, 2026)):
        rev *= 1.08
        ocf = 0.28 * rev
        capex = -0.05 * rev
        sbc = 0.02 * rev
        fcf = (fcf_vals or [ocf + capex] * 5)[i]
        if fcf_sbc_vals is None:
            fsbc = float("nan")
        elif fcf_sbc_vals == "default":
            fsbc = fcf - sbc
        else:
            fsbc = fcf_sbc_vals[i]
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.30 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.20 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", ocf, "USD", "USD", fy),
            "capex": Metric("capex", capex, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", sbc, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fsbc, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.5e9, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
        }
    return m


# ---------- (1) SBC fehlt -> FCF/DCF ohne SBC + Flag ----------

def test_sbc_missing_fcf_without_sbc_and_flag(cfg):
    """Kein fcf_sbc (NaN) -> DCF nutzt Roh-FCF (ohne SBC); Row-Flag 'SBC n/a (ohne SBC)'."""
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk(fcf_sbc_vals=None)  # fcf_sbc = NaN ueberall
    dcf = compute_dcf(m, cfg)
    assert dcf.sbc_missing is True
    # Start-FCF = Ø Roh-FCF (nicht NaN, nicht fcf_sbc)
    assert dcf.fcf_normalized == dcf.fcf_normalized
    assert dcf.fcf_normalized != pytest.approx(0)
    out = evaluate_ticker(m, cfg)
    row = out["row"]
    assert "SBC n/a (ohne SBC)" in row["flags"]
    assert row["dcf_sbc_missing"] is True
    # Kontrolle: mit SBC waere der Start-FCF kleiner (fcf - sbc)
    m2 = _mk(fcf_sbc_vals=[1e9] * 5)
    dcf2 = compute_dcf(m2, cfg)
    assert dcf2.sbc_missing is False
    assert dcf.fcf_normalized > dcf2.fcf_normalized  # ohne SBC groesser


def test_sbc_present_no_flag(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk()  # fcf_sbc vorhanden
    out = evaluate_ticker(m, cfg)
    assert "SBC n/a" not in out["row"]["flags"]
    assert out["row"]["dcf_sbc_missing"] is False


# ---------- (2) Value = n/a bei Datenabdeckung < 70 % ----------

def test_value_na_below_coverage_70(cfg):
    """Datenabdeckung < score_groups.min_coverage_value (70 %) -> Value = NaN (n/a)."""
    assert cfg["score_groups"]["min_coverage_value"] == 0.70
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    # Viele Kriterien n/a -> niedrige Coverage: nur revenue/ebit/equity, kein FCF/SBC/NetDebt
    m = TickerMetrics(ticker="THIN", country="US")
    m.name = "THIN"
    m.market_cap = Metric("market_cap", 1e12, "USD", "USD", 2025)
    for fy in range(2021, 2026):
        rev = 100e9
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.3 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.2 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("ocf", float("nan"), "USD", "USD", fy),
            "capex": Metric("capex", float("nan"), "USD", "USD", fy),
            "fcf": Metric("fcf", float("nan"), "USD", "USD", fy),
            "stock_based_compensation": Metric("sbc", float("nan"), "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", float("nan"), "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", float("nan"), "USD", "USD", fy),
            "income_tax_expense": Metric("tax", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("ibt", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 2e9, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", float("nan"), "USD", "USD", fy),
        }
    crits = compute_criteria(m, cfg)
    res = score_ticker(crits, cfg["criteria"], cfg.get("score_groups"))
    assert res["coverage"] < 0.70
    assert math.isnan(res["value"]), f"Value muss n/a sein bei coverage={res['coverage']}"
    # Quality wird ebenfalls n/a, wenn Abdeckung < min_coverage_quality (70 %)
    assert math.isnan(res["quality"]), f"v35: Quality n/a bei coverage={res['coverage']}"


def test_value_present_above_coverage_70(cfg):
    """Hohe Datenabdeckung -> Value bleibt numerisch."""
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk()
    crits = compute_criteria(m, cfg)
    res = score_ticker(crits, cfg["criteria"], cfg.get("score_groups"))
    assert res["coverage"] >= 0.70
    assert res["value"] == res["value"]


def test_app_value_column_na_below_coverage():
    """app.py: Value-Spalte zeigt 'n/a' bei niedriger Coverage (TextColumn + Logik)."""
    import io as _io
    with _io.open("app.py", encoding="utf-8") as f:
        src = f.read()
    assert "min_coverage_value" in src
    assert 'view["Value"]' in src
    assert '"n/a" if (r["value"] != r["value"]' in src


# ---------- (3) MoS-Zelle "Start-FCF <= 0" statt n/a ----------

def test_mos_cell_start_fcf_le0(cfg):
    """Start-FCF <= 0 -> DcfResult.fcf_le0=True; app._fmt_mos liefert 'Start-FCF <= 0'."""
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    # negativer FCF in allen Jahren -> Start-FCF < 0
    m = _mk(fcf_vals=[-10e9] * 5, fcf_sbc_vals=[-12e9] * 5)
    dcf = compute_dcf(m, cfg)
    assert dcf.fcf_le0 is True
    assert dcf.fcf_normalized < 0
    cfg2 = dict(cfg)
    cfg2["filters"] = dict(cfg["filters"])
    cfg2["filters"]["min_avg_fcf_5y_usd"] = -1e18  # Negativ-FCF-Filter aus (wir testen fcf_le0)
    out = evaluate_ticker(m, cfg2)
    assert out["row"] is not None
    assert out["row"]["dcf_fcf_le0"] is True
    # Anzeige-Logik aus app.py
    import app
    assert app._fmt_mos(float("nan"), le0=True) == "Start-FCF \u2264 0"
    assert app._fmt_mos(0.42, le0=False) == "42.0%"
    assert app._fmt_mos(float("nan"), le0=False) == "n/a"
    # Modus-Tabelle in app.py nutzt dcf_m.fcf_le0
    import io as _io
    with _io.open("app.py", encoding="utf-8") as f:
        src = f.read()
    assert src.count('"Start-FCF \u2264 0" if dcf_m.fcf_le0') >= 3  # Base/Bear/Bull in Modus-Tabelle


def test_fcf_le0_false_bei_positivem_fcf(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    m = _mk()
    dcf = compute_dcf(m, cfg)
    assert dcf.fcf_le0 is False


# ---------- (4) Quality-Rang als Ganzzahl ----------

def test_quality_rang_ist_ganzzahl(cfg):
    """rank_rows liefert int (Ganzzahl); app.py zeigt str(int) -- nie 1.0/2.5."""
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    rows, _ = evaluate_all(demo_companies(), cfg)
    q_rank = rank_rows(rows, "quality")
    for t, r in q_rank.items():
        if r is None:
            continue
        assert isinstance(r, int), f"{t}: Rang {r!r} ist keine Ganzzahl"
        assert r >= 1
    # Gleichstand -> gleicher Rang
    rows2 = [{"ticker": "A", "quality": 80.0}, {"ticker": "B", "quality": 80.0},
             {"ticker": "C", "quality": 90.0}]
    r2 = rank_rows(rows2, "quality")
    assert r2 == {"C": 1, "A": 2, "B": 2}
    # app.py: Anzeige als str(int)
    import io as _io
    with _io.open("app.py", encoding="utf-8") as f:
        src = f.read()
    assert 'str(q_rank[t])' in src


# ---------- Smoke ----------

def test_smoke_v33_columns(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg)
    assert rows and excluded
    for r in rows:
        assert "dcf_sbc_missing" in r and "dcf_fcf_le0" in r
        assert "value_na_below_coverage" in r
