"""Tests fuer v32:
(a) run_ranking.py CLI: --tickers/--out/--batch/--pause/--cache/--no-resume
    (Volllauf in Batches mit Pause, Fortschritt, Wiederaufnahme, Fehlerlog,
    Ergebnis-CSV).
(b) SEC-Rate-Limit: max. N Requests pro Sekunde (sec.max_requests_per_second,
    Default 5) -- _rate_limit() sorgt fuer Mindestabstand; User-Agent aus
    config.yaml.
(5) Smoke ueber alle Demo-Ticker (unveraendert gruen).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.demo import demo_companies
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_all
import run_ranking


# ---------- (a) CLI-Parsing ----------

def test_cli_args_defaults():
    args = run_ranking._parse_args([])
    assert args.tickers is None and args.out is None
    assert args.batch == 0 and args.pause == 0.0
    assert args.no_resume is False


def test_cli_args_full_run():
    args = run_ranking._parse_args([
        "--tickers", "tickers_sp500.csv", "--out", "ranking_sp500.csv",
        "--batch", "20", "--pause", "2.0", "--cache", ".ranking_cache.json"])
    assert args.tickers == "tickers_sp500.csv"
    assert args.out == "ranking_sp500.csv"
    assert args.batch == 20 and args.pause == 2.0
    assert args.cache == ".ranking_cache.json"


def test_cli_positional_tickers():
    args = run_ranking._parse_args(["AAPL", "SAP.DE"])
    assert args.tickers_pos == ["AAPL", "SAP.DE"]


# ---------- (a) Volllauf: Batches, Resume, Fehlerlog ----------

def _fake_load(tickers_ok, tickers_err):
    calls = []
    def _load(ticker, cfg, providers, years=5):
        calls.append(ticker)
        if ticker in tickers_err:
            raise RuntimeError(f"boom {ticker}")
        m = TickerMetrics(ticker=ticker, country="US")
        m.name = ticker
        m.market_cap = Metric("market_cap", 1e12, "USD", "USD", 2025)
        for fy in range(2021, 2026):
            rev = 100e9
            m.years[fy] = {
                "revenue": Metric("revenue", rev, "USD", "USD", fy),
                "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
                "ebitda": Metric("ebitda", 0.3 * rev, "USD", "USD", fy),
                "net_income": Metric("net_income", 0.2 * rev, "USD", "USD", fy),
                "operating_cashflow": Metric("operating_cashflow", 0.28 * rev, "USD", "USD", fy),
                "capex": Metric("capex", -0.05 * rev, "USD", "USD", fy),
                "fcf": Metric("fcf", 0.23 * rev, "USD", "USD", fy),
                "stock_based_compensation": Metric("sbc", 0.02 * rev, "USD", "USD", fy),
                "fcf_sbc": Metric("fcf_sbc", 0.21 * rev, "USD", "USD", fy),
                "equity": Metric("equity", 80e9, "USD", "USD", fy),
                "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
            }
        return m
    return _load, calls


def test_batched_run_errors_do_not_stop(tmp_path, monkeypatch, cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    load, calls = _fake_load(set(), {"BAD1", "BAD2"})
    monkeypatch.setattr(run_ranking, "load_with_provider", load)
    sleeps = []
    monkeypatch.setattr(run_ranking.time, "sleep", lambda s: sleeps.append(s))
    args = run_ranking._parse_args(["--tickers", "x.csv", "--batch", "2", "--pause", "0.5",
                                    "--cache", str(tmp_path / "cache.json"), "--no-resume"])
    tickers = ["T1", "BAD1", "T2", "BAD2", "T3"]
    companies, errors = run_ranking._run_batched(tickers, cfg, {}, args)
    assert {c.ticker for c in companies} == {"T1", "T2", "T3"}
    assert {e["ticker"] for e in errors} == {"BAD1", "BAD2"}
    assert len(calls) == 5  # alle versucht, Fehler stoppen nicht
    assert sleeps == [0.5, 0.5]  # Pause zwischen den 3 Batches (nicht nach letztem)
    cache = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert cache["T1"]["status"] == "ok"
    assert cache["BAD1"]["status"] == "fehler" and "boom BAD1" in cache["BAD1"]["error"]


def test_batched_run_resume_skips_ok(tmp_path, monkeypatch, cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"T1": {"status": "ok"}, "BAD1": {"status": "fehler"}}),
                          encoding="utf-8")
    load, calls = _fake_load({"T1"}, {"BAD1"})
    monkeypatch.setattr(run_ranking, "load_with_provider", load)
    monkeypatch.setattr(run_ranking.time, "sleep", lambda s: None)
    args = run_ranking._parse_args(["--tickers", "x.csv", "--cache", str(cache_file)])
    companies, errors = run_ranking._run_batched(["T1", "T2", "BAD1"], cfg, {}, args)
    # T1 ist im Cache ok -> nicht erneut geladen; BAD1 (fehler) wird wiederholt
    assert calls == ["T2", "BAD1"]
    assert {c.ticker for c in companies} == {"T2"}
    assert [e["ticker"] for e in errors] == ["BAD1"]


def test_write_ranking_csv(tmp_path, cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg)
    errors = [{"ticker": "XX", "error": "boom"}]
    out = tmp_path / "ranking_test.csv"
    run_ranking._write_ranking_csv(rows, excluded, errors, str(out))
    text = out.read_text(encoding="utf-8")
    assert text.startswith("ticker,name,sector")
    assert "# ausgeschlossene Ticker" in text
    assert "# Fehler (Provider)" in text
    assert "DIST" in text and "MICR" in text  # ausgeschlossene Demo-Ticker
    assert "XX,boom" in text
    # bewertete Ticker mit v30/v32-Spalten
    for col in ("status_all", "status_recent2", "mos_recent2", "candidate_mode", "history_years"):
        assert col in text.splitlines()[0]


# ---------- (b) SEC-Rate-Limit ----------

def test_sec_rate_limit_config_default(cfg):
    assert cfg["sec"].get("max_requests_per_second", 5) == 5


def test_rate_limit_enforces_min_gap(monkeypatch):
    import data.sec as sec
    times = iter([100.0, 100.1, 100.2, 100.25, 100.4])  # monotonic-Uhr
    slept = []
    monkeypatch.setattr(sec._time if hasattr(sec, "_time") else __import__("time"), "monotonic",
                        lambda: next(times))
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: next(times))
    monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))
    sec._MAX_RPS["v"] = 5.0  # 0,2 s Mindestabstand
    sec._LAST_REQ["t"] = 0.0
    sec._rate_limit()   # t=100.0 -> kein Warten, setzt LAST=100.1 (naechster next)
    sec._rate_limit()   # erwartet Warten
    assert slept, "Rate-Limit muss gewartet haben"
    assert all(s > 0 for s in slept)


def test_sec_user_agent_from_config(cfg, monkeypatch):
    """User-Agent kommt aus config.yaml (sec.user_agent)."""
    import data.sec as sec
    cfg_sec = dict(cfg)
    cfg_sec["sec"] = dict(cfg["sec"])
    cfg_sec["sec"]["enabled"] = True
    captured = {}
    def fake_urlopen(req, timeout=30):
        captured["ua"] = req.headers.get("User-agent") or req.headers.get("User-Agent")
        class R:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    monkeypatch.setattr(sec.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sec, "_cik_for_ticker", lambda t: "0000000001")
    m = TickerMetrics(ticker="AAPL", country="US")
    m.years[2024] = {
        "operating_cashflow": Metric("ocf", 1e9, "USD", "USD", 2024),
        "capex": Metric("capex", -1e8, "USD", "USD", 2024),
        "stock_based_compensation": Metric("sbc", 1e7, "USD", "USD", 2024),
    }
    cfg2 = dict(cfg_sec)
    cfg2["sec"] = dict(cfg_sec["sec"])
    cfg2["sec"]["user_agent"] = "Mein-Test UA test@example.org"
    res = sec.sec_check(m, cfg2)
    assert captured.get("ua") == "Mein-Test UA test@example.org"


# ---------- (5) Smoke ----------

def test_smoke_demo(cfg):
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    rows, excluded = evaluate_all(demo_companies(), cfg)
    # DIST (negatives EK) wird nicht mehr wegen EK ausgeschlossen, sondern
    # wegen Value-Trap -> weiterhin 4 bewertet / 2 ausgeschlossen
    assert len(rows) == 4 and len(excluded) == 2
    assert {e["ticker"] for e in excluded} == {"DIST", "MICR"}
