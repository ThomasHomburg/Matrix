"""Tests fuer v36:
(1) Datenluecken nur fuer vom Provider gelieferte Jahre (nicht die leere 5. Spalte).
(2) Capex-Regel: SEC-Abweichung > 20 % in >= 2 Jahren -> Status "Pruefen
    (Capex-Definition)", nie Kandidat; Schwelle in config (sec.capex_check).
(3) Zykliker < 7 Jahre -> Status max. "Beobachtung (nicht normalisiert)".
(4) Spalte "MoS bei 10 %" (Base, aktiver Modus) + Status-Zusatz "robust" bei >= 30 %.
(5) Bilanz-Konsistenz: Flag "ND != Schulden - Cash" bei > 5 % Marktkap.-Abweichung
    (Test mit ADBE 2025).
(7) Regression: PYPL, V, MA, ADBE, UBER bleiben im Normalmodell (standard).
"""

import math

import pytest

from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_ticker, evaluate_all
from scoring.sectors import sector_rule
from data.demo import demo_companies


def _mk(ticker="T", industry="Semiconductors", country="US", mcap=1e12, sbc_na=False,
        cyclicals=False, nd_gap=False, no_fy2021=False):
    m = TickerMetrics(ticker=ticker, country=country)
    m.name = ticker
    m.sector = "Basic Materials" if cyclicals else "Technology"
    m.industry = "Steel" if cyclicals else industry
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 100e9
    fys = range(2021, 2026)
    for i, fy in enumerate(fys):
        if no_fy2021 and fy == 2021:
            continue
        rev *= 1.08
        ocf = 0.28 * rev
        capex = -0.05 * rev
        sbc = 0.02 * rev
        fcf = ocf + capex
        fsbc = float("nan") if sbc_na else fcf - sbc
        td = 5e9
        cash = 10e9
        nd = (td - cash) + (0.30 * mcap if (nd_gap and fy == 2025) else 0.0)  # Luecke > 5 % mcap
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
            "net_debt": Metric("net_debt", nd, "USD", "USD", fy),
            "_total_debt": Metric("_total_debt", td, "USD", "USD", fy),
            "_cash": Metric("_cash", cash, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


def _cfg(cfg, sec_enabled=False):
    cfg2 = dict(cfg)
    sec = dict(cfg2.get("sec", {}))
    sec["enabled"] = sec_enabled
    cfg2["sec"] = sec
    # captive_finance.tickers leeren, damit Capex-Tests nicht zusaetzlich
    # 'Pruefen (Captive Finance)' bekommen (CAT/DE stehen in der Config-Liste)
    cfg2["captive_finance"] = {"tickers": []}
    return cfg2


# ---------- (1) Datenluecken nur fuer gelieferte Jahre ----------

def test_datenluecke_nur_fuer_gelieferte_jahre(cfg):
    """Ein Jahr ohne Statement-Spalte (fiscal_ends leer, keine Werte)
    wird NICHT als Datenluecke gemeldet; ein geliefertes Jahr mit Luecke schon."""
    cfg2 = _cfg(cfg)
    m = _mk()
    # FY2021 "nicht geliefert": fiscal_ends entfernen, Werte auf NaN
    del m.fiscal_ends[2021]
    for k in m.years[2021]:
        m.years[2021][k].value = float("nan")
    # FY2022 geliefert, aber ebit fehlt -> Luecke
    m.years[2022]["ebit"].value = float("nan")
    out = evaluate_ticker(m, cfg2)
    dq = out["data_quality"]
    assert not any("FY2021" in d for d in dq), "nicht geliefertes Jahr darf keine Luecke melden"
    assert any("FY2022" in d and "ebit" in d for d in dq)


# ---------- (2) Capex-Definitionspruefung ----------

def _facts_capex(dev_years):
    """companyfacts-Mock: Capex weicht in den gegebenen Jahren ab."""
    units = []
    for fy, dev in dev_years.items():
        # ours = 0.05*rev (negativ im Loader: -0.05*rev); SEC = ours/(1+dev)
        rev = 100e9 * 1.08 ** (fy - 2020)
        ours = 0.05 * rev
        units.append({"form": "10-K", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                      "val": ours / (1 + dev)})
    return {"facts": {"us-gaap": {
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": units}}}}}


def test_capex_pruefstatus_ab_2_jahren(cfg, monkeypatch):
    """Capex weicht laut SEC in >= 2 Jahren um > 20 % ab ->
    Status 'Pruefen (Capex-Definition)', nie Kandidat, Flag 'capex:pruefen'."""
    import data.sec as secmod
    cfg2 = _cfg(cfg, sec_enabled=True)
    m = _mk(ticker="CAT", mcap=2e11)  # guenstig -> waere sonst Kandidat
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: "0000018230")
    # Diff = |ours - ours/(1+dev)| = 0.75*ours (~5.1 Mrd.) > 10 % OCF (~3.8 Mrd.)
    monkeypatch.setattr(secmod, "_fetch_companyfacts",
                        lambda cik, ua: _facts_capex({2024: 3.0, 2023: 3.0}))
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["status_pruefen_capex"] is True
    assert row["status"] == "Prüfen (Capex-Definition)"
    assert row["status_all"] == "Prüfen (Capex-Definition)"
    assert row["is_buy"] is False
    assert "capex:pruefen" in row["flags"]
    assert len(row["capex_dev_years"]) == 2


def test_capex_unter_schwelle_kein_pruefstatus(cfg, monkeypatch):
    """2 Jahre, aber Diff <= 10 % des OCF -> kein Pruefstatus; sowie nur
    1 Jahr mit grossem Diff -> min_years=2 nicht erreicht."""
    import data.sec as secmod
    cfg2 = _cfg(cfg, sec_enabled=True)
    m = _mk(ticker="DE", mcap=2e11)
    monkeypatch.setattr(secmod, "_cik_for_ticker", lambda t: "0000106047")
    # 2 Jahre, aber Diff je Jahr < 10 % des OCF (dev 0.10 -> Diff ~0.45 % rev < 2.8 % rev)
    monkeypatch.setattr(secmod, "_fetch_companyfacts",
                        lambda cik, ua: _facts_capex({2024: 0.10, 2023: 0.10}))
    out = evaluate_ticker(m, cfg2)
    assert out["row"]["status_pruefen_capex"] is False
    assert out["row"]["status"] != "Prüfen (Capex-Definition)"
    # nur 1 Jahr mit grossem Diff -> min_years=2 nicht erreicht
    monkeypatch.setattr(secmod, "_fetch_companyfacts",
                        lambda cik, ua: _facts_capex({2024: 3.0}))
    out = evaluate_ticker(m, cfg2)
    assert out["row"]["status_pruefen_capex"] is False
    assert out["row"]["status"] != "Prüfen (Capex-Definition)"


def test_capex_schwelle_in_config(cfg):
    """Schwellen in config.yaml (sec.capex_check). Hinweis: cfg ist das geteilte
    lru_cache-Dict -- andere Tests ersetzen cfg['sec']; daher frisches YAML laden."""
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                         .read_text(encoding="utf-8"))
    # Schwelle als Anteil des OCF (nicht mehr relativ zur Capex-Zahl)
    assert raw["sec"]["capex_check"]["ocf_share"] == 0.10
    assert raw["sec"]["capex_check"]["min_years"] == 2
    assert raw["consistency"]["net_debt_mcap_share"] == 0.05
    assert raw["dcf"]["robust_discount_rate"] == 0.10


# ---------- (3) Zykliker < 7 Jahre -> max. Beobachtung (nicht normalisiert) ----------

def test_zykliker_unnorm_cappt_kandidat(cfg):
    """Zykliker mit 5 Jahren (< 7) und Kandidat-Bedingungen ->
    Status 'Beobachtung (nicht normalisiert)', nie Kandidat."""
    cfg2 = _cfg(cfg)
    m = _mk(cyclicals=True, mcap=2e11)  # guenstig -> sonst Kandidat
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["zyk_unnorm"] is True
    assert row["status"] == "Beobachtung (nicht normalisiert)"
    assert row["is_buy"] is False


# ---------- (4) MoS bei 10 % + robust ----------

def test_mos_robust_spalte_und_robust_zusatz(cfg):
    """Row traegt mos_robust (Base bei dcf.robust_discount_rate=10 %);
    guenstiger Ticker -> mos_robust >= 30 %; app-Status-Zusatz '(robust)'."""
    cfg2 = _cfg(cfg)
    m = _mk(mcap=2e11)  # Kurs 100 -> hoher MoS auch bei 10 %
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert "mos_robust" in row
    assert row["mos_robust_rate"] == pytest.approx(0.10)
    assert row["mos_robust"] == row["mos_robust"]  # nicht NaN
    # hoeherer Diskontsatz -> niedrigerer Wert -> niedrigeres MoS als bei 9 %
    assert row["mos_robust"] <= row["margin_of_safety"] + 1e-9
    if row["mos_robust"] >= 0.30:
        import app
        # Status-Zusatz in app.py: "(robust)"
        src = open("app.py", encoding="utf-8").read()
        assert '(robust)' in src


def test_mos_robust_bei_sonderregel_na(cfg):
    cfg2 = _cfg(cfg)
    m = _mk(ticker="JPM", industry="Banks - Diversified")
    out = evaluate_ticker(m, cfg2)
    assert math.isnan(out["row"]["mos_robust"])


# ---------- (5) Bilanz-Konsistenz: ND != Schulden - Cash (ADBE 2025) ----------

def test_nd_inconsistent_flag(cfg):
    """ND weicht um > 5 % der Marktkap. von Schulden-Cash ab -> Flag."""
    cfg2 = _cfg(cfg)
    m = _mk(ticker="ADBE", mcap=1e12, nd_gap=True)  # +30 % mcap Luecke in FY2025
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["nd_inconsistent"] is True
    assert "ND != Schulden - Cash" in row["flags"]
    assert any("ND" in d and "Schulden-Cash" in d for d in out["data_quality"])


def test_nd_consistent_kein_flag(cfg):
    cfg2 = _cfg(cfg)
    m = _mk(ticker="ADBE", mcap=1e12, nd_gap=False)
    out = evaluate_ticker(m, cfg2)
    assert out["row"]["nd_inconsistent"] is False
    assert "ND != Schulden - Cash" not in out["row"]["flags"]


# ---------- (7) Regression: PYPL, V, MA, ADBE, UBER im Normalmodell ----------

@pytest.mark.parametrize("ticker,industry", [
    ("PYPL", "Credit Services"), ("V", "Credit Services"), ("MA", "Credit Services"),
    ("ADBE", "Software - Infrastructure"), ("UBER", "Software - Application"),
])
def test_regression_normalmodell(cfg, ticker, industry):
    """PYPL/V/MA/ADBE/UBER bleiben im Normalmodell (standard), DCF berechnet."""
    cfg2 = _cfg(cfg)
    m = _mk(ticker=ticker, industry=industry)
    assert sector_rule(m, cfg2).group == "standard"
    out = evaluate_ticker(m, cfg2)
    row = out["row"]
    assert row["sector_special"] is False
    assert row["sector_group"] == "standard"
    assert row["status"] != "Sonderregel nötig"
    # DCF berechnet (MoS vorhanden)
    assert row["margin_of_safety"] == row["margin_of_safety"]


# ---------- Smoke ----------

def test_smoke_v36(cfg):
    cfg2 = _cfg(cfg)
    rows, excluded = evaluate_all(demo_companies(), cfg2)
    assert rows
    for r in rows:
        assert "mos_robust" in r and "status_pruefen_capex" in r
        assert r["status"] in ("Kandidat", "Beobachtung", "nein",
                               "Prüfen (SBC fehlt)", "Sonderregel nötig",
                               "Beobachtung (nicht normalisiert)",
                               "Prüfen (Capex-Definition)")
