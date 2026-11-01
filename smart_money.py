"""v42: Smart Money -- 13F-HR der Fonds aus SEC EDGAR (smart_money.py).

Screener-Logik bleibt unveraendert; dieses Modul liest 13F-HR (Holdings
Report) je Fonds aus SEC EDGAR, mappt CUSIP -> Ticker und vergleicht mit dem
Vorquartal. Ergebnisse landen in SQLite (smart_money_positions /
smart_money_rationale, siehe schema.sql) und als Spalten in watchlist_live.csv.

Aufruf:
    python smart_money.py update [--quarters 4] [--db value_tracker.db]
    python smart_money.py score TICKER [TICKER ...] [--db value_tracker.db]
    python smart_money.py rationale add TICKER --text "..." --quelle URL
        --typ Brief --datum YYYY-MM-DD [--fonds NAME]
    python smart_money.py rationale import rationale.csv [--db ...]   # Spalte ticker,text,quelle_url,datum,typ[,fonds]
    python smart_money.py show TICKER [--db ...]

(1) 13F laden: pro Fonds (CIK aus config smart_money.fonds) die letzten
    13F-Filings ueber die SEC submissions API, infotable.xml parsen,
    CUSIP -> Ticker (OpenFIGI, Fallback SEC company_tickers), Vergleich mit
    Vorquartal -> aktion neu/erhoeht/reduziert/verkauft/unveraendert, shares,
    wert, anteil_portfolio, aenderung_pct, filing_datum.
    User-Agent-Header laut SEC-Regeln (config sec.user_agent), max. 5/s
    (config smart_money.max_requests_per_second).

(3) smart_money_score(ticker) 1..4 (Regeln in config smart_money.rules):
    1 = kein Fonds; 2 = ein Fonds (neu/erhoeht) mit anteil < 2 %;
    3 = >= 2 unabhaengige Fonds mit erhoeht/neu ODER anteil >= 5 %;
    4 = >= 2 Fonds mit erhoeht/neu ueber 2 Quartale UND Rationale vorhanden.
    Verkaeufe: Score -1 je Fonds mit verkauft/reduziert >= 30 % (Minimum 1).

Netz-Zugriff ist injizierbar (fetcher-Parameter) -- Tests laufen ohne Netz.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from config import load_config

logger = logging.getLogger("value_screener.smart_money")

DB_PATH = Path("value_tracker.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

AKTIONEN = ("neu", "erhoeht", "reduziert", "verkauft", "unveraendert")


# --------------------------------------------------------------------------
# HTTP-Hilfen (SEC-Policy: User-Agent + Rate-Limit) -- injizierbar fuer Tests
# --------------------------------------------------------------------------

class SecHttp:
    """urllib-HTTP mit User-Agent + Rate-Limit (wie data/sec.py).

    fetcher: optionale Funktion url -> bytes (Tests ohne Netz).
    """

    def __init__(self, user_agent: str, max_rps: float = 5.0, fetcher=None):
        self.user_agent = user_agent
        self.max_rps = max(0.1, float(max_rps))
        self._last = 0.0
        self._fetcher = fetcher

    def get(self, url: str, timeout: int = 30) -> bytes:
        gap = 1.0 / self.max_rps
        wait = self._last + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        if self._fetcher is not None:
            return self._fetcher(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


def _sec_base() -> str:
    return "https://www.sec.gov"


# --------------------------------------------------------------------------
# CUSIP -> Ticker (OpenFIGI, Fallback SEC company_tickers.json)
# --------------------------------------------------------------------------

def cusip_to_tickers(cusips: list[str], http: SecHttp, cfg: dict) -> dict[str, str]:
    """Mappt CUSIPs auf Ticker. Reihenfolge: explizites Mapping config
    smart_money.cusip_map (z. B. Fixtures/lokal), dann OpenFIGI POST
    (config cusip_map_url), Fallback SEC company_tickers.json."""
    out: dict[str, str] = {}
    cusips = [c for c in dict.fromkeys(cusips) if c]
    if not cusips:
        return out
    static_map = {str(k): str(v).upper()
                  for k, v in (cfg.get("smart_money", {}).get("cusip_map") or {}).items()}
    for c in cusips:
        if c in static_map:
            out[c] = static_map[c]
    url = str(cfg.get("smart_money", {}).get("cusip_map_url", ""))
    if url:
        try:
            payload = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in cusips]).encode()
            gap = 1.0 / http.max_rps
            if http._last + gap - time.monotonic() > 0:
                time.sleep(http._last + gap - time.monotonic())
            http._last = time.monotonic()
            if http._fetcher is not None:
                raw = http._fetcher(url)  # Tests liefern JSON-Bytes
            else:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"User-Agent": http.user_agent, "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
            data = json.loads(raw.decode("utf-8"))
            for item, c in zip(data, cusips):
                t = (item or {}).get("ticker") or (item or {}).get("shareClassFigi")
                if item and item.get("ticker"):
                    out[c] = str(item["ticker"]).upper()
        except Exception as exc:
            logger.warning("OpenFIGI CUSIP-Mapping fehlgeschlagen (%s) -- Fallback company_tickers", exc)
    if not out:
        # Fallback: SEC company_tickers.json (liefert ticker+cik, kein CUSIP) --
        # als Rueckfall bleibt der CUSIP als Schluessel (damit nichts verloren geht)
        try:
            raw = http.get(f"{_sec_base()}/files/company_tickers.json")
            data = json.loads(raw.decode("utf-8"))
            cik2t = {str(v.get("cik_str")).zfill(10): v.get("ticker", "").upper()
                     for v in data.values()}
            for c in cusips:
                if c in cik2t:
                    out[c] = cik2t[c]
        except Exception as exc:
            logger.warning("company_tickers Fallback fehlgeschlagen: %s", exc)
    for c in cusips:
        out.setdefault(c, c)  # unbekannt -> CUSIP als Schluessel
    return out


# --------------------------------------------------------------------------
# 13F laden + parsen (infotable.xml)
# --------------------------------------------------------------------------

def _parse_infotable(xml_bytes: bytes) -> list[dict]:
    """Parst infotable.xml eines 13F-HR: Liste von Holdings
    {cusip, name, shares, value, ...}."""
    root = ET.fromstring(xml_bytes)
    ns = {"n": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    def _t(el, tag):
        child = el.find(f"n:{tag}", ns) if ns else el.find(tag)
        return (child.text or "").strip() if child is not None and child.text else ""
    holdings = []
    for it in root.iter():
        if it.tag.split("}")[-1] != "infoTable":
            continue
        cusip = _t(it, "cusip")
        shares_txt = _t(it, "shrsOrPrnAmt").split()[0] if _t(it, "shrsOrPrnAmt") else ""
        value_txt = _t(it, "value")
        try:
            shares = float(shares_txt.replace(",", ""))
        except ValueError:
            shares = float("nan")
        try:
            value = float(value_txt.replace(",", "")) * 1000.0  # 13F: Wert in 1000 USD
        except ValueError:
            value = float("nan")
        holdings.append({"cusip": cusip, "name": _t(it, "nameOfIssuer"),
                         "shares": shares, "value": value})
    return holdings


def _list_13f_filings(cik: str, http: SecHttp, max_filings: int = 8) -> list[dict]:
    """Letzte 13F-Filings eines CIK ueber submissions API:
    [{accession, filing_date, report_date(periodOfReport)}]."""
    cik_n = str(cik).lstrip("0") or "0"
    url = f"{_sec_base()}/cgi-bin/browse-edgar?action=getcompany&CIK={cik_n}&type=13F-HR&dateb=&owner=include&count={max_filings}&output=atom"
    raw = http.get(url)
    # Atom-Feed parsen (ohne externe Deps)
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        title = e.find("a:title", ns)
        updated = e.find("a:updated", ns)
        link = e.find("a:link", ns)
        accession = ""
        if link is not None:
            href = link.get("href", "")
            accession = href.rstrip("/").split("/")[-1] if href else ""
        # periodOfReport steht im filing-summary; hier: updated als filing_datum
        out.append({"accession": accession,
                    "filing_date": (updated.text or "")[:10] if updated is not None else "",
                    "title": title.text if title is not None else ""})
    return out[:max_filings]


def load_13f_quarter(cik: str, accession: str, http: SecHttp) -> tuple[str, str, list[dict]]:
    """Laedt infotable.xml eines 13F (accession ohne Bindestriche in URL).
    Rueckgabe: (report_date, filing_date, holdings)."""
    acc = accession.replace("-", "")
    cik_n = str(cik).lstrip("0") or "0"
    base = f"{_sec_base()}/Archives/edgar/data/{cik_n}/{acc}"
    # filing-index -> infotable.xml suchen
    index_raw = http.get(f"{base}/index.json")
    report_date = ""
    infotable = "infotable.xml"
    try:
        idx = json.loads(index_raw.decode("utf-8"))
        for item in idx.get("directory", {}).get("item", []):
            if item.get("name", "").lower() == "infotable.xml":
                infotable = item["name"]
    except Exception:
        pass
    # reportDate (periodOfReport) steht in der Filing-Metadatei (z. B. primary_doc.xml
    # bzw. submission meta); wir lesen die erste .xml-Dokument-Meta ueber den Index
    for item in (idx.get("directory", {}).get("item", []) if isinstance(idx, dict) else []):
        if item.get("name", "").endswith(".xml") and "infotable" not in item["name"].lower():
            try:
                meta_raw = http.get(f"{base}/{item['name']}")
                mroot = ET.fromstring(meta_raw)
                for el in mroot.iter():
                    if el.tag.split("}")[-1] == "periodOfReport" and el.text:
                        report_date = el.text.strip()[:10]
                        break
                if report_date:
                    break
            except Exception:
                continue
    xml_bytes = http.get(f"{base}/{infotable}")
    holdings = _parse_infotable(xml_bytes)
    return report_date, "", holdings


def quartal_of(report_date: str) -> str:
    """'2025-12-31' -> '2025-Q4'."""
    if not report_date:
        return ""
    y, m, _d = report_date.split("-")[:3]
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def compare_quarters(prev: dict[str, dict], cur: dict[str, dict]) -> list[dict]:
    """Vergleicht zwei Quartale (cusip -> holding). Liefert aktion je CUSIP.

    aktion: neu (nicht im Vorquartal), verkauft (nicht mehr im aktuellen),
    erhoeht/reduziert/unveraendert nach shares; aenderung_pct gg. Vorquartal.
    """
    rows = []
    for cusip, h in cur.items():
        p = prev.get(cusip)
        if p is None:
            aktion, aend = "neu", float("nan")
        else:
            ps = p.get("shares", float("nan"))
            if ps != ps or ps <= 0:
                aktion, aend = "neu", float("nan")
            elif h["shares"] > ps:
                aktion = "erhoeht"; aend = h["shares"] / ps - 1.0
            elif h["shares"] < ps:
                aktion = "reduziert"; aend = h["shares"] / ps - 1.0
            else:
                aktion = "unveraendert"; aend = 0.0
        rows.append({"cusip": cusip, "aktion": aktion, "aenderung_pct": aend,
                     "shares": h["shares"], "wert": h["value"], "name": h.get("name", "")})
    for cusip, p in prev.items():
        if cusip not in cur:
            rows.append({"cusip": cusip, "aktion": "verkauft",
                         "aenderung_pct": -1.0, "shares": 0.0, "wert": 0.0,
                         "name": p.get("name", "")})
    return rows


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        con.commit()
        return con
    return sqlite3.connect(db_path)


def store_positions(con: sqlite3.Connection, fonds: str, quartal: str, filing_datum: str,
                    rows: list[dict], cusip2ticker: dict[str, str], portfolio_total: float) -> int:
    """Schreibt Positionen eines (Fonds, Quartal) in smart_money_positions (UPSERT)."""
    n = 0
    for r in rows:
        ticker = cusip2ticker.get(r["cusip"], r["cusip"]).upper()
        anteil = (r["wert"] / portfolio_total) if portfolio_total and portfolio_total > 0 and r["wert"] == r["wert"] else None
        con.execute(
            """INSERT INTO smart_money_positions
               (fonds, ticker, quartal, aktion, shares, wert, anteil_portfolio,
                aenderung_pct, filing_datum, geladen_am)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fonds, ticker, quartal) DO UPDATE SET
                 aktion=excluded.aktion, shares=excluded.shares, wert=excluded.wert,
                 anteil_portfolio=excluded.anteil_portfolio,
                 aenderung_pct=excluded.aenderung_pct,
                 filing_datum=excluded.filing_datum, geladen_am=excluded.geladen_am""",
            (fonds, ticker, quartal, r["aktion"], r["shares"], r["wert"], anteil,
             r["aenderung_pct"], filing_datum, datetime.now().isoformat(timespec="seconds")))
        n += 1
    con.commit()
    return n


def fetch_all_fonds(cfg: dict, quarters: int = 4, db_path: Path = DB_PATH,
                    fetcher=None) -> list[dict]:
    """Laedt die letzten `quarters` Quartale je Fonds aus config smart_money.fonds,
    vergleicht mit Vorquartal und speichert in die DB. Rueckgabe: Fehlerliste.

    fetcher: optionale Funktion url -> bytes (Tests ohne Netz).
    """
    sm = cfg.get("smart_money", {})
    http = SecHttp(str(cfg.get("sec", {}).get("user_agent", "Value-Screener research@example.com")),
                   float(sm.get("max_requests_per_second", 5.0)), fetcher=fetcher)
    con = connect(db_path)
    errors = []
    for fonds, fcfg in sm.get("fonds", {}).items():
        cik = str(fcfg.get("cik", ""))
        try:
            filings = _list_13f_filings(cik, http, max_filings=quarters + 2)
            # aufsteigend nach Datum -> Vorquartals-Vergleich moeglich
            quarters_data: list[tuple[str, str, list[dict]]] = []
            for f in filings:
                report_date, _fd, holdings = load_13f_quarter(cik, f["accession"], http)
                quarters_data.append((report_date or f["filing_date"], f["filing_date"], holdings))
            quarters_data.sort(key=lambda x: x[0])
            quarters_data = quarters_data[-quarters:]
            prev: dict[str, dict] = {}
            for report_date, filing_date, holdings in quarters_data:
                q = quartal_of(report_date)
                by_cusip = {h["cusip"]: h for h in holdings}
                rows = compare_quarters(prev, by_cusip)
                cusips = [h["cusip"] for h in holdings] + [r["cusip"] for r in rows]
                c2t = cusip_to_tickers(cusips, http, cfg)
                total = sum(h["value"] for h in holdings if h["value"] == h["value"])
                n = store_positions(con, fonds, q, filing_date, rows, c2t, total)
                logger.info("smart_money: %s %s -> %d Positionen gespeichert", fonds, q, n)
                prev = by_cusip
        except Exception as exc:
            logger.error("smart_money: Fehler bei Fonds %s (CIK %s): %s", fonds, cik, exc)
            errors.append({"fonds": fonds, "error": str(exc)[:200]})
    return errors


# --------------------------------------------------------------------------
# (3) Score
# --------------------------------------------------------------------------

def _rows_for(con: sqlite3.Connection, ticker: str) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT * FROM smart_money_positions WHERE ticker=? ORDER BY quartal, fonds", (ticker.upper(),))]


def rationale_exists(con: sqlite3.Connection, ticker: str) -> bool:
    r = con.execute("SELECT COUNT(*) AS n FROM smart_money_rationale WHERE ticker=?",
                    (ticker.upper(),)).fetchone()
    return bool(r and r[0] > 0)


def smart_money_score(ticker: str, cfg: dict, db_path: Path = DB_PATH) -> dict:
    """Score 1..4 je config smart_money.rules (siehe Modul-Docstring).

    Rueckgabe: {"score": int, "fonds_kaeufer": [...], "fonds_verkaeufer": [...],
                "letzte_aktion": str, "filing_datum": str, "rationale_vorhanden": bool}
    """
    rules = cfg.get("smart_money", {}).get("rules", {})
    ein_max = float(rules.get("ein_fonds_anteil_max", 0.02))
    zwei_anteil_min = float(rules.get("zwei_fonds_anteil_min", 0.05))
    zwei_q = bool(rules.get("zwei_quartale", True))
    verkauf_schw = float(rules.get("verkauf_schwelle", 0.30))
    min_score = int(rules.get("verkauf_min_score", 1))

    con = connect(db_path)
    rows = _rows_for(con, ticker)
    if not rows:
        return {"score": 1, "fonds_kaeufer": [], "fonds_verkaeufer": [],
                "letzte_aktion": "", "filing_datum": "", "rationale_vorhanden": False}
    con.row_factory = sqlite3.Row

    # letztes Quartal je Fonds
    last_quarter = max(r["quartal"] for r in rows)
    last = [r for r in rows if r["quartal"] == last_quarter]
    kaeufer = sorted({r["fonds"] for r in last if r["aktion"] in ("neu", "erhoeht")})
    verkaeufer = sorted({r["fonds"] for r in last
                         if r["aktion"] in ("verkauft", "reduziert")
                         and (r["aenderung_pct"] is not None
                              and r["aenderung_pct"] <= -verkauf_schw)})
    letzte_aktion = "; ".join(f"{r['fonds']}:{r['aktion']}" for r in last) or ""
    filing_datum = max((r["filing_datum"] or "") for r in rows)
    rat = rationale_exists(con, ticker)

    # Score-Basis: Kaeufer im letzten Quartal
    max_anteil = max((r["anteil_portfolio"] or 0.0) for r in last if r["fonds"] in kaeufer) \
        if kaeufer else 0.0
    if not kaeufer:
        score = 1
    elif len(kaeufer) == 1 and max_anteil < ein_max:
        score = 2
    elif len(kaeufer) >= 2 or max_anteil >= zwei_anteil_min:
        score = 3
    else:
        score = 2
    # Score 4: >= 2 Fonds mit neu/erhoeht ueber 2 Quartale + Rationale
    if zwei_q and rat and len(kaeufer) >= 2:
        prev_quarter = sorted({r["quartal"] for r in rows})[-2] if len({r["quartal"] for r in rows}) >= 2 else None
        if prev_quarter:
            prev_kaeufer = {r["fonds"] for r in rows
                            if r["quartal"] == prev_quarter and r["aktion"] in ("neu", "erhoeht")}
            if len(set(kaeufer) & prev_kaeufer) >= 2:
                score = 4
    # Verkaeufe: -1 je Fonds mit verkauft/reduziert >= 30 %, Minimum min_score
    score = max(min_score, score - len(verkaeufer))
    return {"score": score, "fonds_kaeufer": kaeufer, "fonds_verkaeufer": verkaeufer,
            "letzte_aktion": letzte_aktion, "filing_datum": filing_datum,
            "rationale_vorhanden": rat}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_update(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    errors = fetch_all_fonds(cfg, quarters=args.quarters, db_path=Path(args.db))
    if errors:
        print(f"Fehlerlog ({len(errors)}):")
        for e in errors:
            print(f"  {e['fonds']}: {e['error'][:120]}")
    else:
        print("Alle Fonds aktualisiert.")


def cmd_score(args) -> None:
    cfg = load_config()
    for t in args.tickers:
        s = smart_money_score(t, cfg, db_path=Path(args.db))
        print(f"{t:<8} score={s['score']} kaeufer={','.join(s['fonds_kaeufer']) or '-'} "
              f"verkaeufer={','.join(s['fonds_verkaeufer']) or '-'} "
              f"letzte_aktion={s['letzte_aktion'] or '-'} filing={s['filing_datum'] or '-'} "
              f"rationale={'ja' if s['rationale_vorhanden'] else 'nein'}")


def cmd_show(args) -> None:
    con = connect(Path(args.db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM smart_money_positions WHERE ticker=? ORDER BY quartal DESC, fonds",
        (args.ticker.upper(),)).fetchall()
    print(f"=== smart_money_positions {args.ticker} ({len(rows)}) ===")
    for r in rows:
        print(f"{r['quartal']} {r['fonds']:<16} {r['aktion']:<14} shares={r['shares']:>12,.0f} "
              f"wert={r['wert'] or 0:>14,.0f} anteil={r['anteil_portfolio'] or 0:.2%} "
              f"aend={r['aenderung_pct'] if r['aenderung_pct'] is not None else float('nan'):.1%} "
              f"filing={r['filing_datum']}")


def cmd_rationale_add(args) -> None:
    con = connect(Path(args.db))
    con.execute(
        """INSERT INTO smart_money_rationale (ticker, fonds, text, quelle_url, datum, typ)
           VALUES (?,?,?,?,?,?)""",
        (args.ticker.upper(), args.fonds or None, args.text, args.quelle, args.datum, args.typ))
    con.commit()
    print(f"Rationale gespeichert: {args.ticker} ({args.typ}, Quelle: {args.quelle or '-'})")


def cmd_rationale_import(args) -> None:
    con = connect(Path(args.db))
    n = 0
    with open(args.csv_file, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (row.get("ticker") or "").strip() or not (row.get("text") or "").strip():
                continue
            con.execute(
                """INSERT INTO smart_money_rationale (ticker, fonds, text, quelle_url, datum, typ)
                   VALUES (?,?,?,?,?,?)""",
                ((row.get("ticker") or "").strip().upper(), row.get("fonds") or None,
                 row["text"], row.get("quelle_url") or None, row.get("datum") or None,
                 row.get("typ") or "sonstiges"))
            n += 1
    con.commit()
    print(f"{n} Rationale-Eintraege importiert aus {args.csv_file}.")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smart Money: 13F-HR der Fonds (SEC EDGAR)")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite-Datei (Default: value_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("update", help="13F-HR laden + mit Vorquartal vergleichen")
    p_up.add_argument("--quarters", type=int, default=4, help="Anzahl Quartale (Default 4)")
    p_up.set_defaults(func=cmd_update)
    p_sc = sub.add_parser("score", help="smart_money_score je Ticker")
    p_sc.add_argument("tickers", nargs="+")
    p_sc.set_defaults(func=cmd_score)
    p_sh = sub.add_parser("show", help="Positionen eines Tickers")
    p_sh.add_argument("ticker")
    p_sh.set_defaults(func=cmd_show)
    p_ra = sub.add_parser("rationale", help="Rationale verwalten (manuell/Import)")
    ras = p_ra.add_subparsers(dest="rcmd", required=True)
    p_radd = ras.add_parser("add", help="einzelne Rationale")
    p_radd.add_argument("ticker")
    p_radd.add_argument("--text", required=True)
    p_radd.add_argument("--quelle", default="", help="Quelle-URL")
    p_radd.add_argument("--datum", default="", help="ISO-Datum der Aeusserung")
    p_radd.add_argument("--typ", default="sonstiges", choices=["Brief", "Interview", "13D", "sonstiges"])
    p_radd.add_argument("--fonds", default=None)
    p_radd.set_defaults(func=cmd_rationale_add)
    p_rimp = ras.add_parser("import", help="Rationale aus CSV importieren (ticker,text,quelle_url,datum,typ[,fonds])")
    p_rimp.add_argument("csv_file")
    p_rimp.set_defaults(func=cmd_rationale_import)
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
