"""Rangliste neu berechnen und als Tabelle ausgeben (CLI).

Aufruf:
    python run_ranking.py [TICKER ...]                 # einzelne Ticker / tickers.csv
    python run_ranking.py --tickers tickers_sp500.csv  # v32: Volllauf aus CSV
        [--out ranking_sp500.csv] [--batch 20] [--pause 2.0]
        [--cache .ranking_cache.json] [--no-resume]

Live-Modus ueber Provider-Schnittstelle (data/providers/): US-Ticker -> FMP
(mit FMP_API_KEY), alle anderen (Suffix .DE/.AS/.PA/.CO ...) -> yfinance
(kein Key noetig). Demo-Daten nur als Fallback, wenn kein Live-Ticker geladen
werden kann.

Volllauf (--tickers): Abruf in Batches (Default 20) mit Pause, Fortschritts-
anzeige im Log, Wiederaufnahme nach Abbruch (Cache: Status ok/fehler je
Ticker) und Fehlerlog je Ticker (ein Fehler stoppt den Lauf nicht). Ergebnis
wird als CSV gespeichert (--out, Default ranking.csv). SEC-Abgleich mit
Rate-Limit (sec.max_requests_per_second, Default 5/s) und User-Agent aus
config.yaml.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

from config import load_config
from data.demo import demo_companies
from data.metrics import fcf_yield, fcf_yield_sbc
from data.providers.base import ProviderError, build_providers, load_with_provider
from data.loader import country_code, TickerMetrics
from data.tickerlist import load_ticker_file
from scoring.pipeline import evaluate_all, rank_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("value_screener.ranking")


def _fmt(value: float, spec: str) -> str:
    return "n/a" if math.isnan(value) else format(value, spec)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rangliste berechnen (CLI)")
    p.add_argument("tickers_pos", nargs="*", help="einzelne Ticker (ohne --tickers)")
    p.add_argument("--tickers", help="CSV/Datei mit einer Spalte 'ticker' (Volllauf)")
    p.add_argument("--out", default=None, help="Ergebnis-CSV (Default: ranking.csv; bei --tickers: ranking_<name>.csv)")
    p.add_argument("--batch", type=int, default=0, help="Batch-Groesse fuer --tickers (Default: universe.batch_size)")
    p.add_argument("--pause", type=float, default=0.0, help="Pause zwischen Batches in s (Default: universe.batch_pause_seconds)")
    p.add_argument("--cache", default=None, help="Cache-Datei fuer Wiederaufnahme (Default: .ranking_cache.json)")
    p.add_argument("--no-resume", action="store_true", help="Cache nicht wiederverwenden (frischer Lauf)")
    return p.parse_args(argv)


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_batched(tickers: list[str], cfg: dict, providers: dict, args) -> tuple[list, list]:
    """v32 Volllauf: Batches mit Pause, Fortschritt, Wiederaufnahme (Cache mit
    fertigen Rows), Fehlerlog je Ticker. Rueckgabe: (companies, errors)."""
    ucfg = cfg.get("universe", {})
    batch_size = max(1, args.batch or int(ucfg.get("batch_size", 20)))
    pause = args.pause if args.pause > 0 else float(ucfg.get("batch_pause_seconds", 2.0))
    cache_file = Path(args.cache or ".ranking_cache.json")
    cache = {} if args.no_resume else _load_cache(cache_file)
    errors: list[dict] = []
    companies: list[TickerMetrics] = []
    # Resume: Ticker mit cache[status]=="fehler" erneut versuchen, "ok" ueberspringen
    todo = [t for t in tickers if cache.get(t, {}).get("status") != "ok"]
    logger.info("Volllauf: %d Ticker gesamt, %d noch zu laden (Cache: %s), Batch=%d, Pause=%.1fs",
                len(tickers), len(todo), cache_file.name, batch_size, pause)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        logger.info("Batch %d-%d von %d (%d/%d erledigt)",
                    start + 1, min(start + len(batch), len(todo)), len(todo),
                    len(tickers) - len(todo) + start, len(tickers))
        for ticker in batch:
            try:
                m = load_with_provider(ticker, cfg, providers, years=cfg["data"]["years"])
                companies.append(m)
                cache[ticker] = {"status": "ok"}
            except Exception as exc:  # ProviderError + alles andere: Lauf stoppt nicht
                logger.error("Fehler bei %s: %s", ticker, exc)
                errors.append({"ticker": ticker, "error": str(exc)[:200]})
                cache[ticker] = {"status": "fehler", "error": str(exc)[:200]}
        _save_cache(cache_file, cache)
        if start + batch_size < len(todo):
            time.sleep(pause)
    if errors:
        logger.warning("Fehlerlog (%d): %s", len(errors),
                       ", ".join(e["ticker"] for e in errors))
    return companies, errors


def _write_ranking_csv(rows: list[dict], excluded: list[dict], errors: list[dict], out_path: str) -> None:
    """Ergebnis als CSV speichern (bewertete Rows + ausgeschlossene + Fehler)."""
    cols = ["ticker", "name", "sector", "industry", "sector_group", "country", "source", "score", "quality",
            "value", "coverage", "status", "status_all", "status_recent2", "candidate_mode",
            "margin_of_safety", "mos_rule", "mos_recent2", "mos_avg3", "mos_latest", "mos_ttm",
            "mos_ttm_only", "mos_bear", "mos_bull", "mos_base_alt_nd", "nd_ebitda",
            "roic_5y", "roic_latest", "fcf_yield_3y", "history_years", "flags",
            "sec_check_ok", "sec_check_flag", "sec_check_note", "sec_detail", "kandidat",
            "mos_robust", "status_pruefen_capex", "nd_inconsistent", "zyk_unnorm",
            # Export fuer Tracker/Live-Update
            "kurs", "kurs_datum", "calc_date",
            "iv_bear", "iv_base", "iv_bull",
            "iv_avg3", "iv_latest", "iv_ttm", "iv_ttm_only", "iv_base_10pct",
            "naechster_earnings"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([("" if r.get(c) is None else r.get(c)) for c in cols])
        w.writerow([])
        w.writerow(["# ausgeschlossene Ticker"])
        w.writerow(["ticker", "name", "history_years", "grund"])
        for e in excluded:
            w.writerow([e["ticker"], e["name"], e.get("history_years", ""), "; ".join(e["reasons"])])
        if errors:
            w.writerow([])
            w.writerow(["# Fehler (Provider)"])
            w.writerow(["ticker", "fehler"])
            for e in errors:
                w.writerow([e["ticker"], e["error"]])
    logger.info("Ergebnis gespeichert: %s (%d bewertet, %d ausgeschlossen, %d Fehler)",
                out_path, len(rows), len(excluded), len(errors))


def main() -> None:
    args = _parse_args(sys.argv[1:])
    cfg = load_config()
    api_key = os.environ.get("FMP_API_KEY", "")

    # Provider-Modus: Auswahl per Ticker-Suffix (FMP fuer US, yfinance sonst)
    providers = build_providers(api_key)
    if args.tickers:
        # --tickers-Datei fehlt oder liefert 0 Ticker -> Abbruch mit
        # Fehlermeldung + Exit-Code 1, KEINE leere Ergebnis-CSV schreiben
        if not Path(args.tickers).exists():
            print(f"Fehler: Ticker-Datei nicht gefunden: {args.tickers}", file=sys.stderr)
            sys.exit(1)
        try:
            tickers = load_ticker_file(args.tickers)
        except Exception as exc:
            print(f"Fehler: Ticker-Datei {args.tickers} nicht lesbar ({exc}); "
                  f"Spalte 'ticker' pruefen", file=sys.stderr)
            sys.exit(1)
        if not tickers:
            print(f"Fehler: 0 Ticker gelesen aus {args.tickers}; "
                  f"Spalte 'ticker' pruefen", file=sys.stderr)
            sys.exit(1)
    else:
        tickers = args.tickers_pos or load_ticker_file("tickers.csv") or ["AAPL"]
    logger.info("Live-Modus: %d Ticker ueber Provider (%s)", len(tickers), ", ".join(sorted(providers)))

    companies: list = []
    errors: list[dict] = []
    if args.tickers:
        companies, errors = _run_batched(tickers, cfg, providers, args)
    else:
        for ticker in tickers:
            try:
                companies.append(load_with_provider(ticker, cfg, providers, years=cfg["data"]["years"]))
            except ProviderError as exc:
                logger.error("Provider-Fehler bei %s: %s", ticker, exc)
                errors.append({"ticker": ticker, "error": str(exc)})
    if not companies and not args.tickers:
        logger.info("Kein Live-Ticker geladen -- verwende Demo-Daten.")
        companies = demo_companies()

    rows, excluded = evaluate_all(companies, cfg)
    if args.tickers or args.out:
        if args.out:
            out_path = args.out
        elif args.tickers:
            out_path = f"ranking_{Path(args.tickers).stem}.csv"
        else:
            out_path = "ranking.csv"
        _write_ranking_csv(rows, excluded, errors, out_path)
    q_rank = rank_rows(rows, "quality")
    # Standard-Sortierung: Kaufkandidat zuerst, dann MoS (Base) absteigend
    rows.sort(key=lambda r: (1 if r.get("is_buy") else 0,
                             r["margin_of_safety"] if r["margin_of_safety"] == r["margin_of_safety"] else -1),
              reverse=True)

    print(f"\n=== Rangliste (Quality/Value getrennt, renormalisiert; sortiert: Kandidat, MoS Base) – "
          f"{len(rows)} bewertet, {len(excluded)} ausgeschlossen "
          f"({len(rows) + len(excluded)} Ticker gesamt) ===")
    header = (f"{'Ticker':<10} {'Name':<30} {'Sektor':<16} {'Land':<4} {'Quelle':<8} "
              f"{'Score':>6} {'Quality':>7} {'Q-Rang':>6} {'Value':>6} {'Datenabdeckung':>14} "
              f"{'FCF-Yield':>9} {'FCF-Yld n.SBC':>12} {'FCF-Yld n.SBC 3J':>16} "
              f"{'ROIC Ø':>8} {'ROIC letzte':>11} {'ND/EBITDA':>9} {'MoS Base':>8} {'MoS Bear':>8} {'MoS Bull':>8}  Kandidat / Flags")
    print(header)
    print("-" * len(header))
    for r in rows:
        qr = q_rank.get(r["ticker"])
        print(
            f"{r['ticker']:<10} {r['name'][:30]:<30} {r['sector'][:16]:<16} {country_code(r['country']):<4} {r['source']:<8} "
            f"{_fmt(r['score'], '>6.1f')} "
            f"{_fmt(r['quality'], '>7.1f')} "
            f"{('n/a' if qr is None else str(qr)):>6} "
            f"{_fmt(r['value'], '>6.1f')} "
            f"{_fmt(r['coverage'], '>14.1%')} "
            f"{_fmt(r['fcf_yield'], '>9.1%')} "
            f"{_fmt(r['fcf_yield_sbc'], '>12.1%')} "
            f"{_fmt(r['fcf_yield_3y'], '>16.1%')} "
            f"{_fmt(r['roic_5y'], '>8.1%')} "
            f"{_fmt(r['roic_latest'], '>11.1%')} "
            f"{_fmt(r['nd_ebitda'], '>9.2f')} "
            f"{_fmt(r['margin_of_safety'], '>8.1%')} "
            f"{_fmt(r['mos_bear'], '>8.1%')} "
            f"{_fmt(r['mos_bull'], '>8.1%')}  {r['kandidat']} | {r['flags']}"
        )
    buys = [r["ticker"] for r in rows if r.get("is_buy")]
    if buys:
        print(f"\nKaufkandidaten: {', '.join(buys)}")

    if errors:
        print(f"\nFehlerlog ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']}: {e['error'][:120]}")

    # Vorher/Nachher fuer AAPL zeigen
    aapl = next((m for m in companies if m.ticker == "AAPL"), None)
    if aapl is not None:
        latest_fy = max(aapl.years)
        fixed = fcf_yield(aapl, latest_fy)
        buggy = aapl.years[latest_fy]["fcf"].value / (aapl.market_cap.value / 1e6)
        print(f"\nAAPL FY{latest_fy}: FCF-Yield vorher (Bug, Mio.-Skalierung) = {buggy:.1%}")
        print(f"AAPL FY{latest_fy}: FCF-Yield nachher (Fix, absolute USD)     = {fixed:.1%}")


if __name__ == "__main__":
    main()
