"""Entscheidungslog (decisions.py) -- SQLite value_tracker.db.

Screener-Logik bleibt unveraendert; dieses Modul ist reine Ablage/Protokoll.

Aufrufe:
    python decisions.py init                                  # DB + Schema anlegen
    python decisions.py add TICKER --aktion watch|kauf|verkauf|nichts \\
        --thesis "..." --break "..." [--kurs 123.4] [--horizont 12|24]
    python decisions.py snapshot [--ranking ranking.csv]      # monatlicher Auto-Eintrag
    python decisions.py review [--ranking ranking.csv] [--heute YYYY-MM-DD]
    python decisions.py list [--ticker TICKER]

add: speichert Datum, Kurs, Screener-Status, iv_* (aus valuations-Snapshot zum
     calc_date bzw. --ranking CSV), Szenariogewichte (Default 25/50/25 aus
     config watchlist.szenario_gewichte), Erwartungswert in % (p*100, p = Summe
     der Gewichte der Szenarien mit iv >= 1,10 x Kurs) und Horizont 12/24 Monate.
snapshot: legt monatlich automatisch einen Eintrag (aktion=auto) fuer alle
     Kandidaten und Beobachtungstitel an (Track-Record ohne manuelle Arbeit).
review: fuellt fuer Eintraege aelter als 12/24 Monate den Kurs nach (aus prices
     oder --ranking CSV), berechnet Rendite und Brier-Score.
     Ereignis E = Rendite nach 12 Monaten >= +10 % (config watchlist.ereignis_rendite_pct).
     Prognosewahrscheinlichkeit p = Summe der Gewichte der Szenarien mit iv >= 1,10 x Kurs.
     Bei n < watchlist.review_min_eintraege (Default 30): "unkalibriert, n=..."
     statt Kennzahl.
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from config import load_config

DB_PATH = Path("value_tracker.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

AKTIONEN = ("watch", "kauf", "verkauf", "nichts")
HORIZONTS = (12, 24)


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Legt value_tracker.db an und fuehrt schema.sql aus (idempotent)."""
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.commit()
    return con


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        return init_db(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _heute() -> date:
    return date.today()


def _parse_date(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


# --------------------------------------------------------------------------
# Snapshot-Helfer: iv_*/status je Ticker aus ranking-CSV oder valuations-Tabelle
# --------------------------------------------------------------------------

def _row_aus_ranking(ranking_csv: str | None, ticker: str) -> dict | None:
    """Zeile eines Tickers aus der ranking-CSV (Export-Spalten)."""
    if not ranking_csv or not Path(ranking_csv).exists():
        return None
    with open(ranking_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("ticker") or "").strip().upper() == ticker.upper():
                return row
    return None


def _latest_valuation(con: sqlite3.Connection, ticker: str) -> dict | None:
    """Neuester valuations-Snapshot je Ticker (falls vorhanden)."""
    cur = con.execute(
        "SELECT * FROM valuations WHERE ticker=? ORDER BY calc_date DESC LIMIT 1",
        (ticker.upper(),))
    r = cur.fetchone()
    return dict(r) if r else None


def _vals(row: dict | None) -> dict:
    """iv_*/status/kurs aus einer ranking-CSV-Zeile oder valuations-Zeile."""
    def _f(*keys) -> float:
        for k in keys:
            v = (row or {}).get(k)
            if v not in (None, ""):
                try:
                    x = float(v)
                    return x
                except (TypeError, ValueError):
                    pass
        return float("nan")
    return {
        "status": (row or {}).get("status") or "",
        "kurs": _f("kurs"),
        "iv_base": _f("iv_base"),
        "iv_bear": _f("iv_bear"),
        "iv_bull": _f("iv_bull"),
    }


def erwartungswert_pct(iv_bear: float, iv_base: float, iv_bull: float, kurs: float,
                       gew: dict[str, float]) -> float:
    """Erwartungswert in %: p = Summe der Gewichte der Szenarien mit
    iv >= 1,10 x Kurs; p*100. NaN-Eingaenge zaehlen nicht mit (Gewichtung verfällt)."""
    if kurs != kurs or kurs <= 0:
        return float("nan")
    p = 0.0
    for iv, g in ((iv_bear, gew["bear"]), (iv_base, gew["base"]), (iv_bull, gew["bull"])):
        if iv == iv and iv >= 1.10 * kurs:
            p += g
    return p * 100.0


def _default_gewichte(cfg: dict) -> dict[str, float]:
    w = cfg.get("watchlist", {}).get("szenario_gewichte", {})
    return {
        "bear": float(w.get("bear", 0.25)),
        "base": float(w.get("base", 0.50)),
        "bull": float(w.get("bull", 0.25)),
    }


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------

def cmd_add(args) -> None:
    cfg = load_config()
    con = _connect(Path(args.db))
    ticker = args.ticker.upper()
    heute = _heute()
    ranking = getattr(args, "ranking", None)
    row = _row_aus_ranking(ranking, ticker) or _latest_valuation(con, ticker) or {}
    v = _vals(row)
    kurs = args.kurs if args.kurs else v["kurs"]
    if kurs != kurs or kurs <= 0:
        print("Fehler: kein Kurs bekannt -- --kurs angeben (oder --ranking mit Export-Spalten).",
              file=sys.stderr)
        sys.exit(1)
    gew = _default_gewichte(cfg)
    if args.gewichte:
        parts = [float(x) for x in args.gewichte.split("/")]
        if len(parts) != 3 or abs(sum(parts) - 1.0) > 1e-6:
            print("Fehler: --gewichte als bear/base/bull mit Summe 1 angeben (z. B. 25/50/25 -> 0.25/0.5/0.25).",
                  file=sys.stderr)
            sys.exit(1)
        gew = {"bear": parts[0], "base": parts[1], "bull": parts[2]}
    ew = erwartungswert_pct(v["iv_bear"], v["iv_base"], v["iv_bull"], kurs, gew)
    con.execute(
        """INSERT INTO decisions
           (ticker, datum, aktion, kurs, status_screener, iv_base, iv_bear, iv_bull,
            gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate,
            thesis, break_condition)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker, heute.isoformat(), args.aktion, kurs, v["status"],
         v["iv_base"], v["iv_bear"], v["iv_bull"],
         gew["bear"], gew["base"], gew["bull"], ew, args.horizont,
         args.thesis, args.break_condition))
    con.commit()
    print(f"Gespeichert: {ticker} {heute.isoformat()} aktion={args.aktion} kurs={kurs:.2f} "
          f"status={v['status'] or 'n/a'} iv_base={v['iv_base']:.2f} "
          f"EW={ew:.1f}% (p={ew/100:.2f}) horizont={args.horizont}M")


# --------------------------------------------------------------------------
# snapshot: monatlich automatisch fuer alle Kandidaten/Beobachtungstitel
# --------------------------------------------------------------------------

def _watchlist_aus_ranking(ranking_csv: str | None, cfg: dict) -> list[dict]:
    """Alle Rows mit Status in watchlist.statuses (Default Kandidat/Beobachtung...)."""
    statuses = set(cfg.get("watchlist", {}).get(
        "statuses", ["Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"]))
    if not ranking_csv or not Path(ranking_csv).exists():
        return []
    out = []
    with open(ranking_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() in statuses:
                out.append(row)
    return out


def cmd_snapshot(args) -> None:
    cfg = load_config()
    con = _connect(Path(args.db))
    heute = _heute()
    # monatlich: nur einmal pro Kalendermonat snapshotten
    cur = con.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE aktion='auto' AND strftime('%Y-%m', datum)=?",
        (heute.strftime("%Y-%m"),))
    if cur.fetchone()["n"] > 0:
        print(f"Snapshot fuer {heute.strftime('%Y-%m')} existiert bereits -- nichts zu tun.")
        return
    rows = _watchlist_aus_ranking(getattr(args, "ranking", None), cfg)
    if not rows:
        print("Keine Watchlist-Titel gefunden -- --ranking <csv> angeben.", file=sys.stderr)
        sys.exit(1)
    gew = _default_gewichte(cfg)
    n = 0
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        v = _vals(row)
        kurs = v["kurs"]
        if kurs != kurs or kurs <= 0:
            continue
        ew = erwartungswert_pct(v["iv_bear"], v["iv_base"], v["iv_bull"], kurs, gew)
        con.execute(
            """INSERT INTO decisions
               (ticker, datum, aktion, kurs, status_screener, iv_base, iv_bear, iv_bull,
                gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate,
                thesis, break_condition)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, heute.isoformat(), "auto", kurs, v["status"],
             v["iv_base"], v["iv_bear"], v["iv_bull"],
             gew["bear"], gew["base"], gew["bull"], ew, 12,
             "auto-Snapshot (Kandidat/Beobachtung)", ""))
        n += 1
    con.commit()
    print(f"Snapshot {heute.isoformat()}: {n} Auto-Eintraege (aktion=auto, Horizont 12M).")


# --------------------------------------------------------------------------
# review: Kurs nachfuellen, Rendite + Brier-Score, Kalibrierung
# --------------------------------------------------------------------------

def _kurs_nach(con: sqlite3.Connection, ticker: str, wann: date) -> float:
    """Kurs zum Datum `wann` aus prices (exakt, sonst naechster frueherer Tag)."""
    cur = con.execute(
        "SELECT kurs FROM prices WHERE ticker=? AND datum<=? ORDER BY datum DESC LIMIT 1",
        (ticker, wann.isoformat()))
    r = cur.fetchone()
    return float(r["kurs"]) if r else float("nan")


def cmd_review(args) -> None:
    cfg = load_config()
    wcfg = cfg.get("watchlist", {})
    con = _connect(Path(args.db))
    heute = _parse_date(args.heute) if args.heute else _heute()
    ereignis_schwelle = float(wcfg.get("ereignis_rendite_pct", 0.10))
    min_n = int(wcfg.get("review_min_eintraege", 30))

    cur = con.execute(
        """SELECT * FROM decisions
           WHERE kurs_nachher IS NULL AND reviewed_at IS NULL
           ORDER BY datum""")
    rows = cur.fetchall()
    n_reviewed = 0
    for r in rows:
        d0 = _parse_date(r["datum"])
        wann = d0 + timedelta(days=30 * r["horizont_monate"])  # 12/24 Monate
        if wann > heute:
            continue  # Horizont noch nicht abgelaufen
        kurs_nach = _kurs_nach(con, r["ticker"], wann)
        if kurs_nach != kurs_nach or kurs_nach <= 0:
            continue  # kein Kurs verfuegbar -> ueberspringen
        rendite = kurs_nach / r["kurs"] - 1.0
        # Ereignis E nur fuer 12-Monats-Horizont definiert (Rendite nach 12 M >= +10 %)
        if r["horizont_monate"] == 12:
            e = 1 if rendite >= ereignis_schwelle else 0
            p = (r["erwartungswert_pct"] / 100.0) if r["erwartungswert_pct"] is not None else float("nan")
            brier = (p - e) ** 2 if p == p else None
        else:
            e, brier = None, None
        con.execute(
            """UPDATE decisions SET kurs_nachher=?, rendite_pct=?, ereignis_e=?,
               brier=?, reviewed_at=? WHERE id=?""",
            (kurs_nach, rendite * 100.0, e, brier, heute.isoformat(), r["id"]))
        n_reviewed += 1
    con.commit()

    # Kalibrierung: Brier-Score ueber alle 12M-Eintraege mit Ereignis
    cur = con.execute(
        "SELECT COUNT(*) AS n, AVG(brier) AS brier FROM decisions "
        "WHERE horizont_monate=12 AND ereignis_e IS NOT NULL AND brier IS NOT NULL")
    agg = cur.fetchone()
    n = agg["n"] or 0
    print(f"Review {heute.isoformat()}: {n_reviewed} Eintraege nachgefuellt.")
    if n < min_n:
        print(f"Kalibrierung: unkalibriert, n={n} (< {min_n}) -- keine Kennzahl.")
    else:
        print(f"Kalibrierung: n={n}, mittlerer Brier-Score = {agg['brier']:.4f} "
              f"(0 = perfekt, 0.25 = Referenz Zufall).")
    cmd_list(args)


def cmd_list(args) -> None:
    con = _connect(Path(args.db))
    q = "SELECT * FROM decisions"
    params: tuple = ()
    if args.ticker:
        q += " WHERE ticker=?"
        params = (args.ticker.upper(),)
    q += " ORDER BY datum DESC, id DESC"
    rows = con.execute(q, params).fetchall()
    print(f"\n=== decisions ({len(rows)} Eintraege) ===")
    header = (f"{'id':>4} {'ticker':<8} {'datum':<10} {'aktion':<8} {'kurs':>8} "
              f"{'status':<22} {'iv_base':>8} {'EW%':>6} {'hor':>4} "
              f"{'kurs_nach':>9} {'rend%':>7} {'E':>2} {'brier':>6}  thesis")
    print(header)
    print("-" * len(header))
    for r in rows:
        def _f(x, spec):
            return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)
        print(f"{r['id']:>4} {r['ticker']:<8} {r['datum']:<10} {r['aktion']:<8} "
              f"{_f(r['kurs'], '>8.2f')} {(r['status_screener'] or '')[:22]:<22} "
              f"{_f(r['iv_base'], '>8.2f')} {_f(r['erwartungswert_pct'], '>6.1f')} "
              f"{r['horizont_monate']:>4} {_f(r['kurs_nachher'], '>9.2f')} "
              f"{_f(r['rendite_pct'], '>7.1f')} "
              f"{('n/a' if r['ereignis_e'] is None else str(r['ereignis_e'])):>2} "
              f"{_f(r['brier'], '>6.3f')}  {(r['thesis'] or '')[:40]}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Entscheidungslog (value_tracker.db)")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite-Datei (Default: value_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="DB + Schema anlegen")
    p_init.set_defaults(func=cmd_init)

    p_add = sub.add_parser("add", help="Entscheidung protokollieren")
    p_add.add_argument("ticker")
    p_add.add_argument("--aktion", required=True, choices=AKTIONEN)
    p_add.add_argument("--thesis", default="", help="Begruendung (why)")
    p_add.add_argument("--break", dest="break_condition", default="",
                       help="Invalidierungsbedingung (break)")
    p_add.add_argument("--kurs", type=float, default=None, help="Kurs (Default: aus ranking/valuations)")
    p_add.add_argument("--horizont", type=int, default=12, choices=HORIZONTS)
    p_add.add_argument("--gewichte", default=None, help="bear/base/bull, Summe 1 (z. B. 0.25/0.5/0.25)")
    p_add.add_argument("--ranking", default=None, help="ranking CSV (v41 Export-Spalten)")
    p_add.set_defaults(func=cmd_add)

    p_snap = sub.add_parser("snapshot", help="monatlicher Auto-Snapshot aller Watchlist-Titel")
    p_snap.add_argument("--ranking", default=None, help="ranking CSV")
    p_snap.set_defaults(func=cmd_snapshot)

    p_rev = sub.add_parser("review", help="Kurs nachfuellen, Rendite + Brier, Kalibrierung")
    p_rev.add_argument("--ranking", default=None, help="ranking CSV (Fallback Kurs)")
    p_rev.add_argument("--heute", default=None, help="Referenzdatum YYYY-MM-DD (Default: heute)")
    p_rev.add_argument("--ticker", default=None, help="nur ein Ticker listen (nur list)")
    p_rev.set_defaults(func=cmd_review)

    p_list = sub.add_parser("list", help="alle Entscheidungen anzeigen")
    p_list.add_argument("--ticker", default=None)
    p_list.set_defaults(func=cmd_list)
    return p.parse_args(argv)


def cmd_init(args) -> None:
    con = init_db(Path(args.db))
    print(f"DB angelegt: {args.db} (Schema aus schema.sql)")
    con.close()


def main() -> None:
    args = _parse_args(sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
