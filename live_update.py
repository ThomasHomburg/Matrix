"""v41 (3): Live-Bewertung der Watchlist (live_update.py --ranking <csv>).

Screener-Logik bleibt unveraendert -- dieses Modul liest die ranking-CSV
(v41 (0) Export-Spalten: kurs, calc_date, iv_*, naechster_earnings, ...) und
bewertet die Watchlist-Titel mit dem AKTUELLEN Tagesschlusskurs.

Aufruf:
    python live_update.py --ranking ranking_test_v39.csv [--out watchlist_live.csv]
        [--heute YYYY-MM-DD] [--batch 20] [--pause 2.0]

Watchlist = Status Kandidat/Beobachtung/Beobachtung (nicht normalisiert)
(config watchlist.statuses) plus config watchlist.extra_tickers (Default IT, FDS).

Tagesschlusskurse per yfinance im Batch, Fehlerlog je Ticker (ein Fehler
stoppt den Lauf nicht).

Je Ticker:
  mos_live je Modus = 1 - kurs / iv_<modus> (iv_avg3/iv_latest/iv_ttm/iv_ttm_only,
  iv_base = aktiver Modus); mos_live_min = Minimum ueber die Modi (Regel min_all,
  wie dcf.mos_min_mode=min); mos_live_robust = 1 - kurs / iv_base_10pct.
  Bear-Pruefung + Kandidatenregel: bestehende Regelfunktion
  valuation/dcf.py::is_buy_candidate (KEINE Doppelimplementierung) --
  status_live = Kandidat, wenn is_buy_candidate(quality, dcf_live, kurs, cfg,
  mos_rule=mos_live_min) True, sonst Beobachtung/nein analog der zweistufigen Regel.
  kaufzone_kurs = hoechster Kurs, bei dem die Kandidatenregel erfuellt ist:
      kurs <= min(iv_mode) * (1 - buy_margin_of_safety_min)  (MoS-Bedingung)
      kurs <= iv_bear / buy_bear_floor                        (Bear-Bedingung)
  distanz_pct = kaufzone_kurs / kurs - 1.

Flags:
  wert_veraltet: calc_date (oder kurs_datum) aelter als watchlist.wert_veraltet_tage
      (Default 100 Tage) ODER naechster_earnings liegt in der Vergangenheit.
  alarm: Kurs <= -(watchlist.alarm_kurs_fall_pct) seit calc_date (Default -10 %)
      ODER Tagesbewegung >= +/- watchlist.alarm_tagesbewegung_pct (Default 8 %).

Ausgabe: watchlist_live.csv + Konsolentabelle.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from config import load_config
from valuation.dcf import DcfResult, DcfScenario, is_buy_candidate

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stderr)
logger = logging.getLogger("value_screener.live_update")

MODI = ("avg3", "latest", "ttm", "ttm_only")

# Spalten der Ausgabe (watchlist_live.csv + Konsole)
OUT_COLS = ["ticker", "kurs", "status_screener", "status_live",
            "mos_live_min", "mos_live_robust", "kaufzone_kurs", "distanz_pct",
            "timing_status", "mom_12_1", "abstand_sma200", "revision_90d",
            "wert_veraltet", "alarm", "naechster_earnings",
            # v42 (4): Smart Money
            "smart_money_score", "fonds_kaeufer", "fonds_verkaeufer",
            "letzte_aktion", "filing_datum", "rationale_vorhanden"]


# --------------------------------------------------------------------------
# Watchlist aus ranking-CSV
# --------------------------------------------------------------------------

def watchlist_tickers(rows: list[dict], cfg: dict) -> list[str]:
    """Status in watchlist.statuses + watchlist.extra_tickers (dedup, Reihenfolge)."""
    statuses = set(cfg.get("watchlist", {}).get(
        "statuses", ["Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"]))
    extra = [str(t).upper() for t in cfg.get("watchlist", {}).get("extra_tickers", ["IT", "FDS"])]
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        st = (r.get("status") or "").strip()
        t = (r.get("ticker") or "").strip().upper()
        if t and t not in seen and st in statuses:
            seen.add(t)
            out.append(t)
    for t in extra:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_ranking_rows(ranking_csv: str) -> list[dict]:
    """Ranking-CSV laden (v41 Export-Spalten als Strings; numerische Felder via _f)."""
    with open(ranking_csv, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("ticker") and not r["ticker"].startswith("#")]
    return rows


def _f(row: dict, *keys: str) -> float:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return float("nan")


# --------------------------------------------------------------------------
# Kurse: yfinance im Batch (injizierbar fuer Tests), Fehlerlog je Ticker
# --------------------------------------------------------------------------

def fetch_prices(tickers: list[str], batch: int = 20, pause: float = 2.0,
                 fetcher=None) -> tuple[dict[str, dict], list[dict]]:
    """Tagesschlusskurse per yfinance im Batch. Rueckgabe: (prices, errors).

    prices: {ticker: {"kurs": float, "datum": ISO, "vorheriger": float|nan}}
    fetcher: optionale Funktion ticker -> {"kurs":..., "datum":..., "vorheriger":...}
    (Default: yfinance). Fehler je Ticker stoppen den Lauf nicht (errors-Liste).
    """
    if fetcher is None:
        def fetcher(t: str) -> dict:
            import yfinance as yf
            hist = yf.Ticker(t).history(period="5d")
            if hist is None or hist.empty:
                raise ValueError("keine Kursdaten")
            closes = hist["Close"].dropna()
            return {
                "kurs": float(closes.iloc[-1]),
                "datum": closes.index[-1].date().isoformat(),
                "vorheriger": float(closes.iloc[-2]) if len(closes) >= 2 else float("nan"),
            }
    prices: dict[str, dict] = {}
    errors: list[dict] = []
    for start in range(0, len(tickers), max(1, batch)):
        chunk = tickers[start:start + max(1, batch)]
        logger.info("Kurs-Batch %d-%d von %d", start + 1, start + len(chunk), len(tickers))
        for t in chunk:
            try:
                prices[t] = fetcher(t)
            except Exception as exc:  # Fehler loggen, Lauf stoppt nicht
                logger.error("Fehler bei %s: %s", t, exc)
                errors.append({"ticker": t, "error": str(exc)[:200]})
        if start + batch < len(tickers):
            time.sleep(pause)
    if errors:
        logger.warning("Fehlerlog (%d): %s", len(errors), ", ".join(e["ticker"] for e in errors))
    return prices, errors


# --------------------------------------------------------------------------
# Live-Bewertung je Ticker (reine Funktion aus Row + Kurs -- testbar ohne Netz)
# --------------------------------------------------------------------------

def _dcf_live(row: dict, iv_bear: float, iv_base: float, iv_bull: float,
              mos_live_min: float) -> DcfResult:
    """Leichtes DcfResult fuer die bestehende Regelfunktion is_buy_candidate
    (valuation/dcf.py) -- keine Doppelimplementierung der Kandidatenregel."""
    nan = float("nan")
    return DcfResult(
        fcf_normalized=nan, fcf_years_used=0, revenue_cagr=nan,
        discount_rate=nan, terminal_growth=nan, shares=nan,
        scenarios={
            "bear": DcfScenario("bear", [], [], nan, nan, nan, iv_bear),
            "base": DcfScenario("base", [], [], nan, nan, nan, iv_base),
            "bull": DcfScenario("bull", [], [], nan, nan, nan, iv_bull),
        },
        margin_of_safety=mos_live_min,
        fcf_mode="live",
    )


def live_row(row: dict, kurs: float, cfg: dict, heute: date | None = None,
             vorheriger_kurs: float = float("nan")) -> dict:
    """Live-Bewertung einer ranking-Row mit aktuellem Kurs.

    Nutzt fuer die Kandidatenregel die bestehende Funktion
    valuation/dcf.py::is_buy_candidate (mos_rule=mos_live_min, Regel min_all).
    """
    heute = heute or date.today()
    dc = cfg["dcf"]
    wcfg = cfg.get("watchlist", {})
    quality = _f(row, "quality")
    iv = {m: _f(row, f"iv_{m}") for m in MODI}
    iv_base = _f(row, "iv_base")
    iv_bear = _f(row, "iv_bear")
    iv_bull = _f(row, "iv_bull")
    iv_robust = _f(row, "iv_base_10pct")

    # mos_live je Modus = 1 - kurs/iv; mos_live_min = Minimum (Regel min_all)
    mos_live = {m: (1.0 - kurs / v if v == v and v > 0 else float("nan")) for m, v in iv.items()}
    mos_live["base"] = 1.0 - kurs / iv_base if iv_base == iv_base and iv_base > 0 else float("nan")
    vals = [v for v in mos_live.values() if v == v]
    mos_live_min = min(vals) if vals else float("nan")
    mos_live_robust = 1.0 - kurs / iv_robust if iv_robust == iv_robust and iv_robust > 0 else float("nan")

    # status_live ueber die bestehende Regelfunktion (keine Doppelimplementierung)
    dcf_live = _dcf_live(row, iv_bear, iv_base, iv_bull, mos_live_min)
    is_buy = is_buy_candidate(quality, dcf_live, kurs, cfg, mos_rule=mos_live_min)
    quality_min = float(dc.get("buy_quality_min", 60))
    mos_min = float(dc.get("buy_margin_of_safety_min", 0.30))
    watch_min = float(dc.get("watch_margin_of_safety_min", 0.30))
    quality_ok = quality == quality and quality >= quality_min
    if is_buy:
        status_live = "Kandidat"
    elif quality_ok and vals and max(vals) >= watch_min:
        status_live = "Beobachtung"
    else:
        status_live = "nein"

    # kaufzone_kurs: hoechster Kurs, bei dem die Kandidatenregel erfuellt ist
    # MoS-Bedingung: kurs <= min(iv Modi incl. base) * (1 - mos_min)
    # Bear-Bedingung: iv_bear >= floor * kurs -> kurs <= iv_bear / floor
    floor = float(dc.get("buy_bear_floor", 0.80))
    iv_vals = [v for v in list(iv.values()) + [iv_base] if v == v and v > 0]
    kaufzone = float("nan")
    if iv_vals:
        kaufzone = min(iv_vals) * (1.0 - mos_min)
    if iv_bear == iv_bear and iv_bear > 0 and floor > 0:
        kaufzone = min(kaufzone, iv_bear / floor) if kaufzone == kaufzone else iv_bear / floor
    distanz_pct = kaufzone / kurs - 1.0 if kaufzone == kaufzone and kurs > 0 else float("nan")

    # Flags
    calc_date_s = (row.get("calc_date") or row.get("kurs_datum") or "").strip()
    wert_veraltet = False
    if calc_date_s:
        try:
            calc_d = date.fromisoformat(calc_date_s[:10])
            if (heute - calc_d).days > int(wcfg.get("wert_veraltet_tage", 100)):
                wert_veraltet = True
        except ValueError:
            pass
    ne = (row.get("naechster_earnings") or "").strip()
    if ne:
        try:
            if date.fromisoformat(ne[:10]) < heute:
                wert_veraltet = True  # naechster_earnings liegt in der Vergangenheit
        except ValueError:
            pass
    kurs_calc = _f(row, "kurs")
    alarm = False
    if kurs_calc == kurs_calc and kurs_calc > 0:
        if kurs <= kurs_calc * (1.0 - float(wcfg.get("alarm_kurs_fall_pct", 0.10))):
            alarm = True  # Kurs <= -10 % seit calc_date
    if vorheriger_kurs == vorheriger_kurs and vorheriger_kurs > 0:
        if abs(kurs / vorheriger_kurs - 1.0) >= float(wcfg.get("alarm_tagesbewegung_pct", 0.08)):
            alarm = True  # Tagesbewegung >= +/-8 %

    return {
        "ticker": (row.get("ticker") or "").strip().upper(),
        "kurs": kurs,
        "status_screener": (row.get("status") or "").strip(),
        "status_live": status_live,
        "mos_live_min": mos_live_min,
        "mos_live_robust": mos_live_robust,
        "kaufzone_kurs": kaufzone,
        "distanz_pct": distanz_pct,
        "timing_status": "",       # wird von timing.py ergaenzt (--with-timing)
        "mom_12_1": float("nan"),
        "abstand_sma200": float("nan"),
        "revision_90d": float("nan"),
        "wert_veraltet": int(wert_veraltet),
        "alarm": int(alarm),
        "naechster_earnings": ne,
        # v42 (4): Smart-Money-Spalten (DB optional -- ohne DB n/a)
        "smart_money_score": "n/a",
        "fonds_kaeufer": "",
        "fonds_verkaeufer": "",
        "letzte_aktion": "",
        "filing_datum": "",
        "rationale_vorhanden": "",
    }


def attach_smart_money(out_rows: list[dict], cfg: dict, db_path: Path = Path("value_tracker.db")) -> None:
    """v42 (4): fuellt die smart_money-Spalten je Watchlist-Ticker aus der DB
    (smart_money_score). Ohne DB bleiben die Spalten 'n/a'."""
    if not db_path.exists():
        return
    try:
        import sqlite3 as _sq
        con = _sq.connect(db_path)
        tab = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        if "smart_money_positions" not in tab:
            return  # DB ohne smart_money-Tabellen -> Spalten bleiben n/a
        from smart_money import smart_money_score
        for r in out_rows:
            s = smart_money_score(r["ticker"], cfg, db_path=db_path)
            r["smart_money_score"] = s["score"]
            r["fonds_kaeufer"] = ",".join(s["fonds_kaeufer"])
            r["fonds_verkaeufer"] = ",".join(s["fonds_verkaeufer"])
            r["letzte_aktion"] = s["letzte_aktion"]
            r["filing_datum"] = s["filing_datum"]
            r["rationale_vorhanden"] = "ja" if s["rationale_vorhanden"] else "nein"
    except Exception as exc:
        logger.warning("smart_money-Spalten nicht verfuegbar: %s", exc)


def _fmt(v: float, spec: str) -> str:
    return "n/a" if v != v else format(v, spec)


def _fmt_flag(v) -> str:
    return "JA" if v else ""


def write_watchlist_csv(rows: list[dict], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(OUT_COLS)
        for r in rows:
            w.writerow([("" if r.get(c) is None else r.get(c)) for c in OUT_COLS])
    logger.info("watchlist_live gespeichert: %s (%d Ticker)", out_path, len(rows))


def print_table(rows: list[dict]) -> None:
    print(f"\n=== watchlist_live ({len(rows)} Ticker) ===")
    header = (f"{'ticker':<8} {'kurs':>8} {'status_screener':<26} {'status_live':<12} "
              f"{'mos_min':>8} {'mos_rob':>8} {'kaufzone':>9} {'dist%':>7} "
              f"{'timing':<8} {'mom_12_1':>8} {'sma200%':>7} {'rev90d':>7} "
              f"{'veraltet':<8} {'alarm':<6} {'sm':>3} {'kaeufer':<22} {'verkaeufer':<14} "
              f"naechster_earnings")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['ticker']:<8} {_fmt(r['kurs'], '>8.2f')} {r['status_screener'][:26]:<26} "
              f"{r['status_live']:<12} {_fmt(r['mos_live_min'], '>8.1%')} "
              f"{_fmt(r['mos_live_robust'], '>8.1%')} {_fmt(r['kaufzone_kurs'], '>9.2f')} "
              f"{_fmt(r['distanz_pct'], '>7.1%')} {(r['timing_status'] or 'n/a'):<8} "
              f"{_fmt(r['mom_12_1'], '>8.1%')} {_fmt(r['abstand_sma200'], '>7.1%')} "
              f"{_fmt(r['revision_90d'], '>7.1%')} "
              f"{_fmt_flag(r['wert_veraltet']):<8} {_fmt_flag(r['alarm']):<6} "
              f"{str(r.get('smart_money_score', 'n/a')):>3} "
              f"{(r.get('fonds_kaeufer') or '')[:22]:<22} {(r.get('fonds_verkaeufer') or '')[:14]:<14} "
              f"{r['naechster_earnings']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live-Bewertung der Watchlist (--ranking <csv>)")
    p.add_argument("--ranking", required=True, help="ranking CSV aus run_ranking.py (v41 Export-Spalten)")
    p.add_argument("--out", default="watchlist_live.csv", help="Ausgabe-CSV (Default: watchlist_live.csv)")
    p.add_argument("--heute", default=None, help="Referenzdatum YYYY-MM-DD (Default: heute; Tests)")
    p.add_argument("--db", default="value_tracker.db", help="SQLite-DB fuer smart_money-Spalten (v42)")
    p.add_argument("--batch", type=int, default=20, help="Batch-Groesse fuer Kursabruf")
    p.add_argument("--pause", type=float, default=2.0, help="Pause zwischen Batches in s")
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    cfg = load_config()
    heute = date.fromisoformat(args.heute) if args.heute else date.today()
    if not Path(args.ranking).exists():
        print(f"Fehler: Ranking-Datei nicht gefunden: {args.ranking}", file=sys.stderr)
        sys.exit(1)
    rows_in = load_ranking_rows(args.ranking)
    tickers = watchlist_tickers(rows_in, cfg)
    if not tickers:
        print("Fehler: 0 Watchlist-Ticker (watchlist.statuses/extra_tickers pruefen).", file=sys.stderr)
        sys.exit(1)
    logger.info("Watchlist: %d Ticker (%s)", len(tickers), ", ".join(tickers))
    prices, errors = fetch_prices(tickers, batch=args.batch, pause=args.pause)
    rows_by_t = {(r.get("ticker") or "").strip().upper(): r for r in rows_in}
    out_rows = []
    for t in tickers:
        if t not in prices:
            continue  # Fehler bereits geloggt
        row = rows_by_t.get(t) or {"ticker": t, "status": ""}
        out_rows.append(live_row(row, prices[t]["kurs"], cfg, heute=heute,
                                 vorheriger_kurs=prices[t].get("vorheriger", float("nan"))))
    attach_smart_money(out_rows, cfg, db_path=Path(args.db))
    write_watchlist_csv(out_rows, args.out)
    print_table(out_rows)
    if errors:
        print(f"\nFehlerlog ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']}: {e['error'][:120]}")


if __name__ == "__main__":
    main()
