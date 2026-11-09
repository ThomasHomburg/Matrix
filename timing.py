"""Timing-Kennzahlen aus 2 Jahren Tageskursen je Ticker (timing.py).

Reines Auswertungsmodul -- aendert die Screener-Logik nicht.

Aufruf:
    python timing.py --tickers ticker_watch_v40.csv [--out timing.csv]
        [--period 2y] [--heute YYYY-MM-DD]
    python timing.py TICKER [TICKER ...]          # einzelne Ticker

Kennzahlen aus 2 Jahren Tageskursen (504 Handelstage):
  mom_12_1        Rendite t-252 bis t-21 (12M-Momentum ohne letzten Monat)
  abstand_sma200  Kurs / SMA200 - 1 in %
  abstand_52w_hoch Kurs / 52W-Hoch - 1 in %
  vol_60d         annualisierte Volatilitaet der Tagesrenditen der letzten 60 Tage
  max_drawdown_12m groesster Drawdown der letzten 12 Monate in %
  revision_90d    yfinance eps_trend: Aenderung der Jahresschaetzung in 90 Tagen
                  (current vs. vor 90 Tagen), sonst n/a

timing_status:
  positiv  = mom_12_1 > 0 UND Kurs > SMA200
  neutral  = genau eines von beiden
  negativ  = keines von beiden

Ausgabe: timing.csv + Konsolentabelle. Kursabruf per yfinance im Batch,
Fehlerlog je Ticker (ein Fehler stoppt den Lauf nicht); der Fetcher ist
injizierbar (Tests ohne Netz).
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from data.tickerlist import load_ticker_file

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stderr)
logger = logging.getLogger("value_screener.timing")

OUT_COLS = ["ticker", "kurs", "datum", "mom_12_1", "abstand_sma200", "abstand_52w_hoch",
            "vol_60d", "max_drawdown_12m", "revision_90d", "timing_status"]


# --------------------------------------------------------------------------
# Reine Kennzahlen aus einer Kursreihe (pandas Series, Index = Datum, alt->neu)
# --------------------------------------------------------------------------

def timing_kennzahlen(closes: pd.Series) -> dict:
    """Berechnet alle Timing-Kennzahlen aus einer Schlusskursreihe.

    closes: pandas Series mit Datumsindex, chronologisch aufsteigend.
    Zu wenig Daten (< 260 Handelstage) -> NaN (nie 0).
    """
    nan = float("nan")
    out = {k: nan for k in ("kurs", "datum", "mom_12_1", "abstand_sma200",
                            "abstand_52w_hoch", "vol_60d", "max_drawdown_12m",
                            "revision_90d", "timing_status")}
    closes = closes.dropna()
    if len(closes) < 260:
        return out
    kurs = float(closes.iloc[-1])
    out["kurs"] = kurs
    out["datum"] = closes.index[-1].date().isoformat()

    # mom_12_1: Rendite t-252 bis t-21 (letzte 21 Handelstage ausgespart)
    if len(closes) >= 253:
        mom = closes.iloc[-21] / closes.iloc[-253] - 1.0
        out["mom_12_1"] = float(mom)

    # SMA200 + Abstand
    sma200 = closes.tail(200).mean()
    if sma200 == sma200 and sma200 > 0:
        out["abstand_sma200"] = kurs / sma200 - 1.0

    # 52W-Hoch (252 Handelstage) + Abstand
    hoch52 = closes.tail(252).max()
    if hoch52 == hoch52 and hoch52 > 0:
        out["abstand_52w_hoch"] = kurs / hoch52 - 1.0

    # vol_60d: annualisierte Volatilitaet der Tagesrenditen (sqrt(252))
    rets = closes.pct_change().dropna()
    if len(rets) >= 60:
        out["vol_60d"] = float(rets.tail(60).std(ddof=1) * math.sqrt(252))

    # max_drawdown_12m: groesster Drawdown der letzten 252 Tage
    if len(closes) >= 2:
        window = closes.tail(252)
        roll_max = window.cummax()
        dd = (window / roll_max - 1.0).min()
        out["max_drawdown_12m"] = float(dd)

    out["timing_status"] = timing_status(out["mom_12_1"], out["abstand_sma200"])
    return out


def timing_status(mom_12_1: float, abstand_sma200: float) -> str:
    """positiv = mom_12_1 > 0 UND Kurs > SMA200 (abstand > 0);
    neutral = genau eines; negativ = keines. NaN -> 'n/a'."""
    mom_ok = mom_12_1 == mom_12_1 and mom_12_1 > 0
    sma_ok = abstand_sma200 == abstand_sma200 and abstand_sma200 > 0
    if not mom_ok and not sma_ok:
        return "negativ" if (mom_12_1 == mom_12_1 or abstand_sma200 == abstand_sma200) else "n/a"
    if mom_ok and sma_ok:
        return "positiv"
    return "neutral"


def revision_90d(eps_trend: dict | None) -> float:
    """yfinance eps_trend: Aenderung der Jahresschaetzung in 90 Tagen.

    eps_trend liefert u. a. '0q' (aktuell) und '0q+1y' etc.; hier: current
    Jahreswert vs. Wert vor 90 Tagen -- yfinance bietet '90DaysAgo'-Feld in
    manchen Versionen; wir nutzen current vs. 90DaysAgo wenn vorhanden,
    sonst n/a (nie 0).
    """
    if not eps_trend:
        return float("nan")
    cur = eps_trend.get("current") or eps_trend.get("0q")
    old = eps_trend.get("90DaysAgo")
    try:
        cur_f, old_f = float(cur), float(old)
    except (TypeError, ValueError):
        return float("nan")
    if old_f == 0:
        return float("nan")
    return cur_f / old_f - 1.0


# --------------------------------------------------------------------------
# Kursabruf (yfinance, injizierbar) + Batch mit Fehlerlog
# --------------------------------------------------------------------------

def fetch_history(ticker: str, period: str = "2y", fetcher=None) -> tuple[pd.Series, dict | None]:
    """2 Jahre Tageskurse + eps_trend. fetcher: ticker -> (Series, eps_trend)."""
    if fetcher is None:
        def fetcher(t: str):
            import yfinance as yf
            tk = yf.Ticker(t)
            hist = tk.history(period=period)
            if hist is None or hist.empty:
                raise ValueError("keine Kursdaten")
            eps = None
            try:
                eps = tk.eps_trend
            except Exception:
                eps = None
            return hist["Close"].dropna(), eps
    return fetcher(ticker)


def run_timing(tickers: list[str], period: str = "2y", batch: int = 10, pause: float = 2.0,
               fetcher=None) -> tuple[list[dict], list[dict]]:
    """Timing-Kennzahlen fuer alle Ticker im Batch; Fehler stoppen den Lauf nicht."""
    rows: list[dict] = []
    errors: list[dict] = []
    for start in range(0, len(tickers), max(1, batch)):
        chunk = tickers[start:start + max(1, batch)]
        logger.info("Timing-Batch %d-%d von %d", start + 1, start + len(chunk), len(tickers))
        for t in chunk:
            try:
                closes, eps = fetch_history(t, period=period, fetcher=fetcher)
                r = timing_kennzahlen(closes)
                r["revision_90d"] = revision_90d(eps)
                r["ticker"] = t
                rows.append(r)
            except Exception as exc:
                logger.error("Fehler bei %s: %s", t, exc)
                errors.append({"ticker": t, "error": str(exc)[:200]})
        if start + batch < len(tickers):
            time.sleep(pause)
    return rows, errors


def _fmt(v, spec: str) -> str:
    if v is None or v != v:
        return "n/a"
    return format(v, spec)


def print_table(rows: list[dict]) -> None:
    print(f"\n=== timing ({len(rows)} Ticker) ===")
    header = (f"{'ticker':<8} {'kurs':>8} {'datum':<10} {'mom_12_1':>8} {'sma200%':>8} "
              f"{'52w%':>8} {'vol60d':>7} {'maxdd':>7} {'rev90d':>7}  status")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['ticker']:<8} {_fmt(r['kurs'], '>8.2f')} {r['datum']:<10} "
              f"{_fmt(r['mom_12_1'], '>8.1%')} {_fmt(r['abstand_sma200'], '>8.1%')} "
              f"{_fmt(r['abstand_52w_hoch'], '>8.1%')} {_fmt(r['vol_60d'], '>7.1%')} "
              f"{_fmt(r['max_drawdown_12m'], '>7.1%')} {_fmt(r['revision_90d'], '>7.1%')} "
              f" {r['timing_status']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Timing-Kennzahlen aus 2 Jahren Tageskursen")
    p.add_argument("tickers_pos", nargs="*", help="einzelne Ticker")
    p.add_argument("--tickers", help="CSV mit Spalte 'ticker'")
    p.add_argument("--out", default="timing.csv", help="Ausgabe-CSV (Default: timing.csv)")
    p.add_argument("--period", default="2y", help="yfinance-Periode (Default: 2y)")
    p.add_argument("--batch", type=int, default=10)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--heute", default=None, help="nur Doku (Referenzdatum)")
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    if args.tickers:
        if not Path(args.tickers).exists():
            print(f"Fehler: Ticker-Datei nicht gefunden: {args.tickers}", file=sys.stderr)
            sys.exit(1)
        tickers = load_ticker_file(args.tickers)
    else:
        tickers = args.tickers_pos
    if not tickers:
        print("Fehler: 0 Ticker (--tickers oder Positionaler Argumente).", file=sys.stderr)
        sys.exit(1)
    rows, errors = run_timing(tickers, period=args.period, batch=args.batch, pause=args.pause)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(OUT_COLS)
        for r in rows:
            w.writerow([("" if r.get(c) is None else r.get(c)) for c in OUT_COLS])
    logger.info("timing gespeichert: %s (%d Ticker)", args.out, len(rows))
    print_table(rows)
    if errors:
        print(f"\nFehlerlog ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']}: {e['error'][:120]}")


if __name__ == "__main__":
    main()
