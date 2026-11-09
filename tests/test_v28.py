"""Tests fuer v28:
(1) Bear/Bull je Modus in der Scorecard (compute_dcf je fcf_mode liefert
    scenarios bear/base/bull -- Smoke ueber die Modi).
(2) Ausgeschlossene Ticker in der Row-Liste (evaluate_all -> excluded mit Grund).
(3) SEC-Abgleich (v26) + Quartalstabelle (TtmResult.quarters) -- Smoke.
(4) Sektorregeln ueber Industry: Banken/Versicherer (ROE, P/B, Div-Yield
    statt ROIC/FCF/ND-EBITDA), Zykliker (7-J.-Normalisierung, Flag bei < 7 J.),
    REITs (FFO-Hinweis statt FCF-Yield).
(5) Universumslader: S&P-500 (Wikipedia -> CSV), STOXX 600 (CSV), Batches,
    Wiederaufnahme, Fehlerlog je Ticker (Fehler stoppt Lauf nicht).
(6) Smoke ueber alle Ticker + neue v28-Spalten.
"""

import json

import pytest

from data.loader import Metric, TickerMetrics
from data.demo import demo_companies
from scoring.sectors import (sector_rule, normalized_net_income, price_to_book,
                             dividend_yield)
from scoring.criteria import compute_criteria, active_criteria
from scoring.pipeline import evaluate_all, evaluate_ticker
from valuation.dcf import compute_dcf


def _mk(ticker="T", industry="", sector="Technology", mcap=1e12, shares=2e9,
        ni_by_fy=None, extras=None):
    m = TickerMetrics(ticker=ticker, country="US")
    m.name = ticker
    m.sector = sector
    m.industry = industry
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    rev = 100e9
    for i, fy in enumerate(range(2021, 2026)):
        rev *= 1.08
        ni = (ni_by_fy or {}).get(fy, 0.20 * rev)
        ocf = 0.28 * rev
        capex = -0.05 * rev
        sbc = 0.02 * rev
        fcf = ocf + capex
        m.years[fy] = {
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.30 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", ni, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", ocf, "USD", "USD", fy),
            "capex": Metric("capex", capex, "USD", "USD", fy),
            "fcf": Metric("fcf", fcf, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", sbc, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fcf - sbc, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.5e9, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
        }
    if extras:
        m.extras.update(extras)
    return m


# --- (4) Sektorregeln ueber Industry ------------------------------------------

def test_industry_nicht_sector_pypl_visa_mastercard_keine_banken(cfg):
    """PYPL/V/MA laufen als 'Financial Services' (Sector), aber Industry
    'Credit Services' -> KEINE Banken (Standardregeln)."""
    for ind in ("Credit Services",):
        m = _mk(industry=ind, sector="Financial Services")
        assert sector_rule(m, cfg).group == "standard"

def test_banken_und_versicherer_ueber_industry(cfg):
    """Industry statt Sector. v35: Banks*/Insurance*/Capital Markets sind
    jetzt Sektor-Sonderregel ('special'); v38: Asset Management ist AUCH
    Sonderregel. Diversified Financial Services / Mortgage Finance bleiben
    'financials' (ROE/P-B/Div-Yield)."""
    for ind in ("Diversified Financial Services", "Mortgage Finance"):
        m = _mk(industry=ind, sector="Financial Services")
        assert sector_rule(m, cfg).group == "financials", ind
    # Asset Management -> Sonderregel (wie Banken/Versicherer)
    for ind in ("Asset Management",):
        m = _mk(industry=ind, sector="Financial Services")
        assert sector_rule(m, cfg).group == "special", ind
    # Banken/Versicherer/Capital Markets -> Sonderregel
    for ind in ("Banks - Diversified", "Banks - Regional",
                "Insurance - Property & Casualty", "Insurance - Life",
                "Capital Markets"):
        m = _mk(industry=ind, sector="Financial Services")
        assert sector_rule(m, cfg).group == "special", ind

def test_zykliker_ueber_industry(cfg):
    """Rohstoffe, Chemie, Stahl, Auto -> Zykliker (7-J.-Normalisierung)."""
    for ind in ("Gold", "Copper", "Steel", "Chemicals", "Specialty Chemicals",
                "Auto Manufacturers", "Auto Parts", "Oil & Gas E&P"):
        m = _mk(industry=ind, sector="Basic Materials")
        rule = sector_rule(m, cfg)
        assert rule.group == "cyclicals", ind
        assert rule.normalized_earnings_years == 7

def test_reits_ueber_industry_prefix(cfg):
    """REITs am Industry-Praefix erkannt. v35: REIT - * ist jetzt
    Sektor-Sonderregel ('special') -- DCF/FCF-Yield nicht berechnet."""
    for ind in ("REIT - Industrial", "REIT - Retail", "REIT - Residential",
                "REIT - Specialty", "REIT - Office"):
        m = _mk(industry=ind, sector="Real Estate")
        rule = sector_rule(m, cfg)
        assert rule.group == "special", ind
        assert "Sonderregel" in rule.note

def test_financials_kriterien_aktiv_roic_fcf_nd_na(cfg):
    """Financials (v28, z. B. Diversified Financial Services -- v35/v38-special-
    Gruppen ausgenommen): ROIC, FCF, ND/EBITDA n/a; ROE, P/B, Div-Yield aktiv."""
    m = _mk(industry="Diversified Financial Services", sector="Financial Services",
            extras={"price_to_book": Metric("price_to_book", 1.2, "ratio", "", 0),
                    "dividend_yield": Metric("dividend_yield", 0.03, "ratio", "", 0)})
    crits = compute_criteria(m, cfg)
    assert crits["roic_avg5"].is_na
    assert crits["fcf_positive_years"].is_na
    assert crits["fcf_margin_avg5"].is_na
    assert crits["net_debt_ebitda"].is_na
    assert "roe_avg5_financials" in crits
    assert "price_to_book" in crits and not crits["price_to_book"].is_na
    assert crits["price_to_book"].value == pytest.approx(1.2)
    assert "dividend_yield" in crits and crits["dividend_yield"].value == pytest.approx(0.03)
    # Standardgruppe: Finanz-Kriterien NICHT aktiv
    m2 = _mk(industry="Semiconductors", sector="Technology")
    crits2 = compute_criteria(m2, cfg)
    assert "price_to_book" not in crits2
    assert not crits2["roic_avg5"].is_na

def test_zykliker_normalisierung_7_jahre(cfg):
    """Zykliker: Gewinne ueber bis zu 7 Jahre normalisiert (hier 5 verfuegbar)."""
    ni = {2021: 10e9, 2022: -5e9, 2023: 20e9, 2024: 30e9, 2025: 40e9}
    m = _mk(industry="Steel", sector="Basic Materials", ni_by_fy=ni, shares=1e9)
    rule = sector_rule(m, cfg)
    val, years = normalized_net_income(m, rule)
    assert years == 5                     # nur 5 Jahre verfuegbar
    assert val == pytest.approx(sum(ni.values()) / 5)
    crits = compute_criteria(m, cfg)
    ne = crits["normalized_earnings"]
    assert not ne.is_na
    assert ne.years_available == 5
    assert ne.thin                         # < 7 Jahre -> Flag "Daten duenn"
    assert ne.value == pytest.approx(val / 1e9)  # je Aktie

def test_zykliker_flag_weniger_als_7_jahre(cfg):
    """Zykliker mit < 7 Jahren Historie -> Flag 'zyk:norm<7J' in der Row."""
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    m = _mk(industry="Gold", sector="Basic Materials")
    out = evaluate_ticker(m, cfg_off)
    assert "zyk:norm<7J" in out["row"]["flags"]
    assert out["row"]["sector_group"] == "cyclicals"
    assert out["row"]["normalized_earnings_years"] == 5

def test_reit_ffo_yield_statt_fcf_yield(cfg):
    """REIT - * ist Sektor-Sonderregel -> FCF-Yield/MoS n/a, Status
    'Sonderregel noetig' (FFO-Bewertung erfolgt manuell)."""
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    m = _mk(industry="REIT - Industrial", sector="Real Estate",
            extras={"ffo_per_share": Metric("ffo_per_share", 5.0, "USD", "", 0)})
    out = evaluate_ticker(m, cfg_off)
    row = out["row"]
    assert row["sector_group"] == "special"
    assert row["status"] == "Sonderregel nötig"
    crits = out["criteria"]
    assert crits["fcf_yield_sbc_avg3"].is_na
    assert crits["mos_base"].is_na

def test_reit_ohne_ffo_fcf_yield_na_und_hinweis(cfg):
    """REIT - * -> Sonderregel; FCF-Yield und MoS n/a (nie Roh-FCF)."""
    m = _mk(industry="REIT - Retail", sector="Real Estate")
    rule = sector_rule(m, cfg)
    assert rule.group == "special"
    assert "Sonderregel" in rule.note
    crits = compute_criteria(m, cfg)
    assert crits["fcf_yield_sbc_avg3"].is_na  # Sonderregel -> n/a (nie Roh-FCF)
    assert crits["mos_base"].is_na


# --- (1) Bear/Bull je Modus -----------------------------------------------------

def test_bear_bull_je_modus(cfg):
    """Jeder fcf_mode liefert scenarios bear/base/bull mit Wert je Aktie."""
    m = _mk(industry="Semiconductors")
    for mode in ("avg3", "latest", "ttm", "ttm_only", "auto"):
        dcf = compute_dcf(m, cfg, fcf_mode=mode)
        assert set(dcf.scenarios) == {"bear", "base", "bull"}, mode
        for sc in dcf.scenarios.values():
            assert sc.value_per_share == sc.value_per_share or mode in ("ttm", "ttm_only")

def test_bear_bull_je_modus_in_scorecard_tabelle(cfg):
    """Die Modus-Tabelle in app.py nutzt scenarios bear/bull je Modus --
    Smoke: compute_dcf liefert konsistente MoS je Szenario."""
    m = _mk(industry="Software - Application")
    price = m.market_cap.value / 2e9
    for mode in ("avg3", "latest", "ttm", "ttm_only"):
        dcf = compute_dcf(m, cfg, fcf_mode=mode)
        bear = dcf.scenarios["bear"]
        bull = dcf.scenarios["bull"]
        if bear.value_per_share == bear.value_per_share and bear.value_per_share > 0:
            mos_bear = 1.0 - price / bear.value_per_share
            assert mos_bear <= dcf.margin_of_safety + 1e-9  # Bear <= Base
        if bull.value_per_share == bull.value_per_share and bull.value_per_share > 0:
            mos_bull = 1.0 - price / bull.value_per_share
            assert mos_bull >= dcf.margin_of_safety - 1e-9  # Bull >= Base


# --- (2) Ausgeschlossene Ticker mit Grund ----------------------------------------

def test_ausgeschlossene_ticker_mit_grund(cfg):
    """evaluate_all liefert ausgeschlossene Ticker MIT Grund (Tabelle unter
    der Rangliste, nicht im Expander versteckt)."""
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg_off)
    assert excluded, "Demo-Daten muessen ausgeschlossene Ticker liefern"
    for e in excluded:
        assert e["ticker"] and e["name"] and e["reasons"]
    tickers_ex = {e["ticker"] for e in excluded}
    # DIST (negatives EK) wird nicht mehr wegen EK ausgeschlossen, sondern
    # wegen Value-Trap (sinkender Umsatz); MICR wegen Marktkap.
    assert tickers_ex == {"DIST", "MICR"}
    gruende = {e["ticker"]: "; ".join(e["reasons"]) for e in excluded}
    assert "Durchschnitts-FCF" in gruende["DIST"]  # nicht mehr EK, sondern neg. O-FCF
    assert "Marktkap" in gruende["MICR"]


# --- (5) Universumslader -----------------------------------------------------------

def test_sp500_wikipedia_to_csv(tmp_path, monkeypatch):
    """S&P-500: pandas.read_html auf die Wikipedia-Tabelle -> tickers_sp500.csv."""
    import pandas as pd
    from data import universe as uni

    fake_table = pd.DataFrame({
        "Symbol": ["AAPL", "MSFT", "BRK.B", "BRK/B"],
        "Security": ["Apple", "Microsoft", " Berkshire Hathaway", "Berkshire Hathaway"],
        "GICS Sector": ["IT", "IT", "Financials", "Financials"],
    })

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html><body>fake</body></html>"

    monkeypatch.setattr(uni.urllib.request, "urlopen", lambda req, timeout=30: _Resp())
    monkeypatch.setattr(pd, "read_html", lambda src: [fake_table])
    out = tmp_path / "tickers_sp500.csv"
    tickers = uni.fetch_sp500_tickers("https://example.invalid", out)
    # '.' und '/' werden zu '-'; Duplikate entfernt
    assert tickers == ["AAPL", "BRK-B", "MSFT"]
    assert out.exists()
    content = out.read_text(encoding="utf-8").splitlines()
    assert content[0] == "ticker"
    assert "AAPL" in content and "BRK-B" in content

    # Nochmal laden (aus CSV, kein refresh): kein Netzwerk-Call
    def _no_net(*a, **k):
        raise AssertionError("kein reload ohne refresh")

    monkeypatch.setattr(uni.urllib.request, "urlopen", _no_net)
    assert uni.load_sp500_tickers(out, refresh=False) == tickers


def test_stoxx600_csv_import(tmp_path):
    """STOXX 600: Import per CSV (Spalte 'ticker')."""
    from data.universe import load_stoxx600_tickers
    p = tmp_path / "tickers_stoxx600.csv"
    p.write_text("ticker,name\nSAP.DE,SAP SE\nSIE.DE,Siemens\nNESN.SW,Nestle\n", encoding="utf-8")
    assert load_stoxx600_tickers(p) == ["SAP.DE", "SIE.DE", "NESN.SW"]
    with pytest.raises(FileNotFoundError):
        load_stoxx600_tickers(tmp_path / "fehlt.csv")


def test_universe_batches_resume_fehlerlog(tmp_path, monkeypatch, cfg):
    """Universumslader: Batches, Wiederaufnahme nach Abbruch, Fehlerlog je
    Ticker (ein Fehler stoppt den Lauf nicht)."""
    from data import universe as uni
    from data.providers.base import ProviderError

    calls = []

    def fake_load(ticker, cfg_, providers, years=5):
        calls.append(ticker)
        if ticker == "FEHLER":
            raise ProviderError("yfinance: keine Daten fuer FEHLER")
        m = _mk(ticker=ticker)
        m.source = "yfinance"
        return m

    monkeypatch.setattr(uni, "load_with_provider", fake_load)
    monkeypatch.setattr(uni.time, "sleep", lambda s: None)  # keine Pause im Test

    cfg_u = dict(cfg)
    cfg_u["universe"] = {"batch_size": 2, "batch_pause_seconds": 0.0,
                         "resume": True, "cache_file": str(tmp_path / ".cache.json")}
    cfg_u["sec"] = {"enabled": False}

    tickers = ["AAA", "FEHLER", "BBB", "CCC"]
    progress = []
    companies, errors = uni.load_universe(tickers, cfg_u,
                                          progress_cb=lambda d, t, tk: progress.append((d, t, tk)))
    assert len(companies) == 3
    assert len(errors) == 1 and errors[0]["ticker"] == "FEHLER"
    assert "FEHLER" in errors[0]["error"]
    # Fortschritt je Ticker
    assert len(progress) == 4
    assert progress[-1][0] == 4
    # Cache persistiert
    cache = json.loads((tmp_path / ".cache.json").read_text())
    assert cache["AAA"]["status"] == "ok"
    assert cache["FEHLER"]["status"] == "fehler"

    # Wiederaufnahme: nur FEHLER offen (ok-Ticker werden uebersprungen)
    calls.clear()
    companies2, errors2 = uni.load_universe(tickers, cfg_u)
    assert calls == ["FEHLER"]
    assert len(companies2) == 0 and len(errors2) == 1


# --- (6) Smoke ueber alle Ticker + v28-Spalten --------------------------------------

V28_ROW_COLUMNS = {
    "sector_group", "sector_note", "industry",
    "normalized_earnings", "normalized_earnings_years",
    "price_to_book", "dividend_yield",
}


def test_evaluate_all_smoke_v28_spalten(cfg):
    """evaluate_all ueber alle Demo-Ticker: keine Ausnahme, v28-Spalten da."""
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg_off)
    assert rows
    for row in rows:
        missing = V28_ROW_COLUMNS - set(row)
        assert not missing, f"{row.get('ticker')}: fehlen {sorted(missing)}"
        assert row["sector_group"] in ("standard", "financials", "cyclicals", "reits")


def test_demo_ticker_sectorgruppen(cfg):
    """Demo-Ticker: AAPL/QUAL/LEVR/SIE sind Standard (keine Industry gesetzt)."""
    cfg_off = dict(cfg)
    cfg_off["sec"] = {"enabled": False}
    rows, _ = evaluate_all(demo_companies(), cfg_off)
    for row in rows:
        assert row["sector_group"] == "standard"
        assert row["industry"] == ""


def test_app_row_columns_v28_aktualisiert():
    """app.py zeigt SEC-Spalte; ausgeschlossene Ticker als Tabelle (kein Expander)."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    m = re.search(r"show_cols = \[(.*?)\]", src, re.S)
    assert m
    cols = re.findall(r'"([^"]+)"', m.group(1))
    assert "SEC" in cols
    # Ausgeschlossene als Tabelle mit Grund (nicht mehr nur im Expander)
    assert "Ausgeschlossene Ticker" in src
    assert "Grund" in src
