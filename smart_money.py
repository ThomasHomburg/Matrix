"""v43: Smart Money -- 13F-HR einer Fonds-Liste aus SEC EDGAR (smart_money.py).

Screener-Logik bleibt unveraendert; Modul ist eigenstaendig lauffaehig
(CSV/SQLite value_tracker.db, Tabellen werden bei Bedarf angelegt).

Aufruf:
    python smart_money.py fetch [--quarters 8] [--db value_tracker.db]
    python smart_money.py score TICKER [TICKER ...] [--db ...]
    python smart_money.py score --ranking ranking.csv [--db ...]
    python smart_money.py overlap --ranking ranking.csv --watch ticker_watch_v40.csv
        [--out smart_money_overlap.csv] [--db ...]
    python smart_money.py rationale-import rationale_import.csv [--db ...]
    python smart_money.py ciks [--db ...]          # CIK-Aufloesung + Filer-Status
    python smart_money.py show TICKER [--db ...]

(1) Fonds-Liste: config smart_money.funds (name, cik, typ, gewicht,
    positions_soll_max). CIKs mit leerem Feld werden per EDGAR-Firmensuche
    (efts.sec.gov full-text search / company search) aufgeloest -- nie raten.
    Fehlt ein Fonds als 13F-HR-Filer -> im Bericht nennen und ueberspringen.

(2) Abruf: letzte N Quartale 13F-HR je Fonds (config quarters, Default 8);
    bei Nachmeldung 13F-HR/A die jeweils neueste Fassung. User-Agent mit
    Kontakt (sec.user_agent), max. 5/s, Wiederholung bei 429 (retry_429).
    infotable.xml parsen. CUSIP -> Ticker: SEC company_tickers.json,
    Rest per OpenFIGI (cusip_map_url); nicht zuordenbare CUSIPs -> Fehlerlog.
    GOOG/GOOGL je Fonds zusammenfassen (config googl_merge); Optionen
    (PUT/CALL) getrennt in smart_money_options, zaehlen NICHT in Score/Anteil.

(3) Tabellen (schema.sql, bei Bedarf automatisch angelegt):
    smart_money_positions (fonds, quartal, filing_datum, ticker, shares, wert,
    anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct),
    smart_money_rationale (nur Import rationale_import.csv, Pflicht quelle_url),
    smart_money_options, smart_money_katalysator (13D/13D-A der Aktivisten).

(4) smart_money_score(ticker) 1..4 (config smart_money.rules):
    1 = kein Fonds mit Gewicht > 0; 2 = ein Fonds neu/erhoeht, Anteil < 2 %;
    3 = Summe Gewicht der Fonds mit neu/erhoeht >= gewicht_summe_min (2,0)
        ODER Anteil >= anteil_gewicht1_min (5 %) in einem Fonds mit Gewicht 1,0;
    4 = Score-3-Bedingung in zwei aufeinanderfolgenden Quartalen + Rationale.
    Verkaeufe: je Fonds verkauft ODER reduziert >= verkauf_schwelle -> -1,
    Minimum verkauf_min_score (1). Zusaetzlich je Ticker: fonds_kaeufer,
    fonds_verkaeufer, letzte_aktion, filing_datum, daten_alter_tage,
    katalysator_13d (aus 13D/13D-A).

(5) overlap: Abgleich mit Screener (ranking-CSV mit status_all/quality/mos_*).
    smart_money_urteil je Ticker: Bestaetigung (Fonds kauft UND Status
    Kandidat/Beobachtung*, oder Fonds verkauft UND Status nein), Divergenz
    (Fonds kauft UND (MoS min < 0 oder Status nein)), Warnung (Fonds
    verkauft/reduziert UND Status Kandidat/Beobachtung), sonst neutral.
    Ausgabe smart_money_overlap.csv: alle Watchlist-Ticker (--watch CSV,
    Default ticker_watch_v40.csv falls vorhanden) + alle Ticker mit
    Fonds-Aktivitaet.

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
# HTTP (SEC-Policy: User-Agent + Rate-Limit + Retry bei 429) -- injizierbar
# --------------------------------------------------------------------------

class UserAgentError(Exception):
    """v44 (1): ungueltiger User-Agent -- Abbruch VOR dem ersten Request."""


def validate_user_agent(ua: str) -> str:
    """v44 (1): SEC-Policy -- User-Agent im Format 'Name email@domain'.

    Fehlt der Wert oder enthaelt er 'example', 'test' oder keinen '@'
    -> UserAgentError mit klarer Meldung (Abbruch vor dem ersten Request).
    """
    ua = (ua or "").strip()
    low = ua.lower()
    if (not ua or "@" not in ua or "example" in low or "test" in low):
        raise UserAgentError(
            "Ungueltiger SEC User-Agent: "
            f"{ua!r}. SEC-Policy verlangt 'Name email@domain' "
            "(config.yaml: smart_money.user_agent, Fallback sec.user_agent). "
            "Bitte echten Kontakt eintragen (z. B. 'Thomas Homburg name@domain.de').")
    return ua


def user_agent_from_cfg(cfg: dict) -> str:
    """v44 (1): smart_money.user_agent, Fallback sec.user_agent; validiert.

    Tests/CI ohne echten Kontakt: sec-Block nur mit {enabled: False} (kein
    user_agent) -> Rueckgabe "" (nicht validiert); fetch_all bricht dann vor
    dem ersten echten Request ab, Fetcher-Fixtures laufen trotzdem.
    """
    sm = cfg.get("smart_money", {})
    sec = cfg.get("sec", {})
    ua = str(sm.get("user_agent") or "").strip() or str(sec.get("user_agent") or "").strip()
    if not ua and sec.get("enabled") is False:
        return ""  # Test-Modus: kein echter Kontakt konfiguriert
    return validate_user_agent(ua)


class SecHttp:
    """urllib-HTTP mit validiertem User-Agent, Rate-Limit und 403/429/503-Retry.

    fetcher: optionale Funktion url -> bytes (Tests ohne Netz). Echte 403/429/503
    (urllib HTTPError) -> bis zu `retry_tries` Wiederholungen mit den
    Wartezeiten `retry_waits` (Default 2, 5, 10 s); danach Fehlerlog mit URL
    und Header-Hinweis ('User-Agent pruefen').
    """

    def __init__(self, user_agent: str, max_rps: float = 5.0,
                 retry_waits=(2.0, 5.0, 10.0), retry_tries: int = 3,
                 fetcher=None, errors: list | None = None, sleeper=time.sleep):
        # Leerer UA nur zulaessig, wenn ein Fixture-Fetcher injiziert ist (Tests)
        self.user_agent = validate_user_agent(user_agent) if (user_agent or fetcher is None) else ""
        self.max_rps = max(0.1, float(max_rps))
        self.retry_waits = [float(w) for w in retry_waits]
        self.retry_tries = max(1, int(retry_tries))
        self._last = 0.0
        self._fetcher = fetcher
        self.errors = errors if errors is not None else []
        self._sleep = sleeper  # injizierbar fuer Tests (429/403 ohne Warten)

    def get(self, url: str, timeout: int = 30) -> bytes:
        """v45 (2): laeuft ueber die zentrale sec_get() (Header User-Agent +
        Accept-Encoding gzip + Host bei jedem Request). 403/429/503 -> Retries
        (503 = SEC rate-limit/Edge-Block, wird wie 403/429 wiederholt)."""
        gap = 1.0 / self.max_rps
        for attempt in range(1, self.retry_tries + 1):
            wait = self._last + gap - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            status, body, _hdrs = sec_get(url, self.user_agent, timeout=timeout,
                                          fetcher=self._fetcher, sleeper=self._sleep)
            if status == 200:
                return body
            if status in (403, 429, 503):  # v45: 503 (SEC Edge) ebenfalls retry
                if attempt < self.retry_tries:
                    w = self.retry_waits[min(attempt - 1, len(self.retry_waits) - 1)]
                    logger.warning("SEC HTTP %d fuer %s -- Retry %d/%d in %.0fs (User-Agent: %r)",
                                   status, url, attempt, self.retry_tries - 1, w, self.user_agent)
                    self._sleep(w)
                    continue
                self.errors.append({
                    "url": url, "http_code": status,
                    "antwort_beginn": body[:200].decode("utf-8", "replace"),
                    "error": (f"HTTP {status} nach {self.retry_tries} Versuchen -- "
                              f"User-Agent pruefen (gesendet: {self.user_agent!r}); "
                              f"Antwortbeginn: {body[:120].decode('utf-8', 'replace')!r}")})
                logger.error("SEC HTTP %d fuer %s -- aufgegeben. User-Agent pruefen "
                             "(config sec.user_agent); Antwortbeginn: %r",
                             status, url, body[:120])
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, None)


def _sec_base() -> str:
    return "https://www.sec.gov"


def sec_get(url: str, user_agent: str, timeout: int = 30,
            fetcher=None, sleeper=time.sleep) -> tuple[int, bytes, dict]:
    """v45 (2): ALLE SEC-Requests laufen ueber diese eine Funktion.

    Sendet immer ein urllib.request.Request-Objekt mit den Headern:
      User-Agent (aus sec.user_agent / smart_money.user_agent),
      Accept-Encoding: gzip,
      Host (aus der URL).
    gzip-Antworten werden entpackt. Rueckgabe: (status, body, headers).
    fetcher: url -> bytes (Tests ohne Netz; Status dann 200).

    WICHTIG: urllib.request.urlopen(url_string) OHNE Request-Objekt wuerde
    den Default-UA "Python-urllib/3.x" senden -- SEC antwortet dann 403.
    (Geprueft in smart_money.py + data/sec.py: alle Aufrufe nutzen Request.)
    """
    import gzip as _gzip
    import urllib.parse as _up
    parsed = _up.urlparse(url)
    host = parsed.netloc
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip",
        "Host": host,
        "Accept": "application/json, text/xml, application/xml, */*",
        "Connection": "close",
    }
    if fetcher is not None:
        return 200, fetcher(url), {"User-Agent": user_agent, "Host": host}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            hdrs = dict(r.headers)
            status = getattr(r, "status", 200)
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return exc.code, body, dict(exc.headers or {})
    if "gzip" in str(hdrs.get("Content-Encoding", "")):
        try:
            body = _gzip.decompress(body)
        except Exception:
            pass
    return status, body, hdrs


# --------------------------------------------------------------------------
# CIK-Aufloesung per EDGAR-Firmensuche (nie raten)
# --------------------------------------------------------------------------

def _validate_cik_13f(cik: str, http: SecHttp) -> dict | None:
    """v44 (2): CIK validieren -- data.sec.gov/submissions/CIK##########.json muss
    mindestens ein 13F-HR enthalten. Rueckgabe: {name, cik, last_13f_hr, n_13f}
    oder None (kein Filer)."""
    cik10 = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    try:
        data = json.loads(http.get(url).decode("utf-8"))
    except Exception as exc:
        logger.warning("submissions-Validierung CIK %s fehlgeschlagen: %s", cik10, exc)
        return None
    rec = data.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    last13f = ""
    for form, d in zip(forms, dates):
        if str(form).startswith("13F-HR"):
            last13f = max(last13f, str(d))
    n = sum(1 for f in forms if str(f).startswith("13F-HR"))
    if n == 0:
        return None  # kein 13F-HR-Filer
    return {"name": data.get("name", ""), "cik": cik10,
            "last_13f_hr": last13f, "n_13f": n}


def search_company_13f(name: str, http: SecHttp) -> list[dict]:
    """v44 (2): EDGAR-Firmensuche -- cgi-bin/browse-edgar?action=getcompany&
    company=<Name>&type=13F-HR&output=atom. Rueckgabe: Trefferliste
    [{cik, name, last_13f_hr}] (validiert via submissions; nur 13F-HR-Filer).
    Mehrere Kandidaten -> volle Liste, KEIN Auto-Pick."""
    import urllib.parse
    q = urllib.parse.quote(name)
    url = (f"{_sec_base()}/cgi-bin/browse-edgar?action=getcompany&company={q}"
           f"&type=13F-HR&dateb=&owner=include&count=40&output=atom")
    try:
        raw = http.get(url)
        root = ET.fromstring(raw)
    except Exception as exc:
        logger.warning("Firmensuche '%s' fehlgeschlagen: %s", name, exc)
        return []
    hits = []
    seen = set()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall("a:entry", ns):
        link = e.find("a:link", ns)
        title = e.find("a:title", ns)
        if link is None:
            continue
        href = link.get("href", "")
        m = None
        import re as _re
        m = _re.search(r"CIK=([^&]+)", href)
        if not m:
            # Alternativ: CIK im Pfad /Archives/edgar/data/<cik>/
            m2 = _re.search(r"/data/(\d+)/", href)
            m = m2
        if not m:
            continue
        cik = str(int(m.group(1))).zfill(10)
        if cik in seen:
            continue
        seen.add(cik)
        name_e = title.text if title is not None else ""
        v = _validate_cik_13f(cik, http)
        if v:  # nur Filer mit 13F-HR
            hits.append({"cik": cik, "name": v["name"] or name_e,
                         "last_13f_hr": v["last_13f_hr"]})
    return hits


def resolve_all_ciks(cfg: dict, http: SecHttp) -> list[dict]:
    """v44 (2): CIK-Aufloesung je Fonds, Reihenfolge:
    (a) cik aus config.yaml (validiert: mindestens ein 13F-HR in submissions),
    (b) EDGAR-Firmensuche mit Namensvarianten (config smart_money.name_variants)
        -> Trefferliste ins Report, NICHTS automatisch uebernehmen,
    (c) sonst 'manuell eintragen'.
    Rueckgabe: [{name, cik, filer_status, treffer: [...]|None}]."""
    sm = cfg.get("smart_money", {})
    variants = sm.get("name_variants", {}) or {}
    out = []
    for f in sm.get("funds", []):
        name = f.get("name", "")
        cik = str(f.get("cik") or "").strip()
        if cik:
            v = _validate_cik_13f(cik, http)
            if v:
                out.append({"name": name, "cik": v["cik"], "filer_status": "13F-HR (config)",
                            "bericht_name": v["name"], "last_13f_hr": v["last_13f_hr"],
                            "treffer": None})
            else:
                out.append({"name": name, "cik": cik.zfill(10), "filer_status": "kein Filer",
                            "bericht_name": "", "last_13f_hr": "", "treffer": None})
            continue
        # (b) Namensvarianten suchen
        treffer: list[dict] = []
        seen = set()
        names = variants.get(name, [name])
        if isinstance(names, str):
            names = [names]
        for nv in names:
            for h in search_company_13f(nv, http):
                if h["cik"] not in seen:
                    seen.add(h["cik"])
                    treffer.append(h)
        if treffer:
            out.append({"name": name, "cik": "", "filer_status": "manuell eintragen",
                        "bericht_name": "", "last_13f_hr": "", "treffer": treffer})
        else:
            out.append({"name": name, "cik": "", "filer_status": "manuell eintragen",
                        "bericht_name": "", "last_13f_hr": "", "treffer": []})
    return out


# --------------------------------------------------------------------------
# CUSIP -> Ticker: SEC company_tickers.json (CIK-basiert), Rest OpenFIGI
# --------------------------------------------------------------------------

def load_company_tickers(http: SecHttp) -> dict[str, str]:
    """SEC company_tickers.json -> {CIK(10): ticker}. Einmalig laden, cachen."""
    try:
        raw = http.get(f"{_sec_base()}/files/company_tickers.json")
        data = json.loads(raw.decode("utf-8"))
        return {str(v.get("cik_str")).zfill(10): str(v.get("ticker", "")).upper()
                for v in data.values()}
    except Exception as exc:
        logger.warning("company_tickers.json nicht ladbar: %s", exc)
        return {}


def cusip_to_tickers(cusips: list[str], http: SecHttp, cfg: dict,
                     errors: list | None = None) -> dict[str, str]:
    """CUSIP -> Ticker. Reihenfolge: statisches cusip_map (Fixtures), dann
    OpenFIGI POST (cusip_map_url), Rest -> Fehlerlog (nicht zuordenbar)."""
    out: dict[str, str] = {}
    cusips = [c for c in dict.fromkeys(cusips) if c]
    static_map = {str(k): str(v).upper()
                  for k, v in (cfg.get("smart_money", {}).get("cusip_map") or {}).items()}
    rest = []
    for c in cusips:
        if c in static_map:
            out[c] = static_map[c]
        else:
            rest.append(c)
    if rest:
        url = str(cfg.get("smart_money", {}).get("cusip_map_url", ""))
        if url:
            try:
                payload = json.dumps(
                    [{"idType": "ID_CUSIP", "idValue": c} for c in rest]).encode()
                gap = 1.0 / http.max_rps
                wait = http._last + gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                http._last = time.monotonic()
                if http._fetcher is not None:
                    raw = http._fetcher(url, payload) if False else http._fetcher(url)
                else:
                    req = urllib.request.Request(
                        url, data=payload,
                        headers={"User-Agent": http.user_agent,
                                 "Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        raw = r.read()
                data = json.loads(raw.decode("utf-8"))
                for item, c in zip(data, rest):
                    t = (item or {}).get("ticker")
                    if t:
                        out[c] = str(t).upper()
            except Exception as exc:
                logger.warning("OpenFIGI-Mapping fehlgeschlagen: %s", exc)
    for c in rest:
        if c not in out:
            if errors is not None:
                errors.append({"cusip": c, "error": "nicht zuordenbar (OpenFIGI/kein Mapping)"})
            out[c] = c  # CUSIP als Schluessel, damit nichts verloren geht
    return out


# --------------------------------------------------------------------------
# 13F laden + parsen
# --------------------------------------------------------------------------

def _parse_infotable(xml_bytes: bytes) -> tuple[list[dict], list[dict]]:
    """Parst infotable.xml: (holdings, options).

    holdings: {cusip, name, shares, value} (Aktien); options: {cusip, name,
    put_call, shares, value}. Wert in 1000 USD -> x1000 auf USD.
    """
    root = ET.fromstring(xml_bytes)
    ns = {"n": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}

    def _t(el, tag):
        child = el.find(f"n:{tag}", ns) if ns else el.find(tag)
        return (child.text or "").strip() if child is not None and child.text else ""

    holdings, options = [], []
    for it in root.iter():
        if it.tag.split("}")[-1] != "infoTable":
            continue
        cusip = _t(it, "cusip")
        name = _t(it, "nameOfIssuer")
        pc = _t(it, "putCall").upper()
        shares_txt = _t(it, "shrsOrPrnAmt").split()[0] if _t(it, "shrsOrPrnAmt") else ""
        try:
            shares = float(shares_txt.replace(",", ""))
        except ValueError:
            shares = float("nan")
        try:
            value = float(_t(it, "value").replace(",", "")) * 1000.0
        except ValueError:
            value = float("nan")
        rec = {"cusip": cusip, "name": name, "shares": shares, "value": value}
        if pc in ("PUT", "CALL"):
            rec["put_call"] = pc
            options.append(rec)
        else:
            holdings.append(rec)
    return holdings, options


def _atom_entries(xml_bytes: bytes) -> list[dict]:
    """Atom-Feed (browse-edgar) -> [{accession, filing_date, form, report_date}]."""
    root = ET.fromstring(xml_bytes)
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
        # category term enthaelt form + period (z. B. "13F-HR 2025-06-30")
        form, report_date = "", ""
        cat = e.find("a:category", ns)
        if cat is not None:
            term = cat.get("term", "")
            # term wie "13F-HR 2025-06-30" oder "SC 13D 2025-07-01":
            # form = alle Tokens bis auf das letzte (Datum), datum = letztes Token
            parts = term.split()
            if len(parts) >= 2 and parts[-1][:4].isdigit():
                form = " ".join(parts[:-1])
                report_date = parts[-1][:10]
            elif parts:
                form = " ".join(parts)
        out.append({"accession": accession,
                    "filing_date": (updated.text or "")[:10] if updated is not None else "",
                    "form": form or (title.text if title is not None else ""),
                    "report_date": report_date})
    return out


def list_13f_filings(cik: str, http: SecHttp, max_filings: int = 12) -> list[dict]:
    """v45 (4): letzte 13F-HR (+13F-HR/A) eines CIK -- OHNE cgi-bin:
    submissions-JSON (data.sec.gov/submissions/CIK##########.json) ->
    Accession + periodOfReport + filingDate -> Archives-Verzeichnis.
    Bei Nachmeldung (A) ersetzt die neuere Fassung das Original zum selben
    report_date (neueste filing_date gewinnt)."""
    cik10 = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    data = json.loads(http.get(url).decode("utf-8"))
    rec = data.get("filings", {}).get("recent", {})
    accessions = rec.get("accessionNumber", [])
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    reports = rec.get("reportDate", [])
    hr = [{"accession": a, "filing_date": d, "form": f, "report_date": r}
          for a, f, d, r in zip(accessions, forms, dates, reports)
          if str(f).startswith("13F-HR")]
    best: dict[str, dict] = {}
    for e in hr:
        rd = e["report_date"]
        if rd not in best or e["filing_date"] > best[rd]["filing_date"]:
            best[rd] = e
    out = sorted(best.values(), key=lambda e: e["report_date"])[-max_filings:]
    return out


def list_13d_filings(cik: str, http: SecHttp, max_filings: int = 20) -> list[dict]:
    """13D/13D-A eines (aktivistischen) Filers: [{accession, filing_date, form, report_date}]."""
    cik_n = str(int(str(cik).lstrip("0") or "0"))
    url = (f"{_sec_base()}/cgi-bin/browse-edgar?action=getcompany&CIK={cik_n}"
           f"&type=SC+13D&dateb=&owner=include&count={max_filings}&output=atom")
    entries = _atom_entries(http.get(url))
    return [e for e in entries if e["form"] in ("SC 13D", "SC 13D/A", "13D", "13D-A")][:max_filings]


def load_13f_quarter(cik: str, accession: str, http: SecHttp,
                     report_date: str = "") -> tuple[str, list[dict], list[dict]]:
    """v45 (4): laedt infotable.xml ueber Archives-Verzeichnis (kein cgi-bin):
    Archives/edgar/data/<cik>/<accession ohne '-'>/infotable.xml.
    report_date kommt aus dem submissions-JSON (Parameter), Fallback: primary_doc.
    Rueckgabe: (report_date, holdings, options)."""
    acc = accession.replace("-", "")
    cik_n = str(int(str(cik).lstrip("0") or "0"))
    base = f"{_sec_base()}/Archives/edgar/data/{cik_n}/{acc}"
    idx = json.loads(http.get(f"{base}/index.json").decode("utf-8"))
    items = idx.get("directory", {}).get("item", [])
    infotable = next((i["name"] for i in items if i.get("name", "").lower() == "infotable.xml"),
                     "infotable.xml")
    if not report_date:
        for item in items:
            n = item.get("name", "")
            if n.endswith(".xml") and "infotable" not in n.lower():
                try:
                    mroot = ET.fromstring(http.get(f"{base}/{n}"))
                    for el in mroot.iter():
                        if el.tag.split("}")[-1] == "periodOfReport" and el.text:
                            report_date = el.text.strip()[:10]
                            break
                    if report_date:
                        break
                except Exception:
                    continue
    holdings, options = _parse_infotable(http.get(f"{base}/{infotable}"))
    return report_date, holdings, options


def load_13d_target(cik: str, accession: str, http: SecHttp) -> tuple[str, str]:
    """13D/13D-A: Ziel-Ticker (CUSIP der issuer) + filing_date.
    Rueckgabe: (cusip, filing_date)."""
    acc = accession.replace("-", "")
    cik_n = str(int(str(cik).lstrip("0") or "0"))
    base = f"{_sec_base()}/Archives/edgar/data/{cik_n}/{acc}"
    idx = json.loads(http.get(f"{base}/index.json").decode("utf-8"))
    items = idx.get("directory", {}).get("item", [])
    doc = next((i["name"] for i in items
                if i.get("name", "").endswith(".xml") and "index" not in i["name"].lower()),
               None)
    cusip = ""
    if doc:
        try:
            root = ET.fromstring(http.get(f"{base}/{doc}"))
            for el in root.iter():
                if el.tag.split("}")[-1].lower() in ("cusip",) and el.text:
                    cusip = el.text.strip()
                    if cusip:
                        break
        except Exception:
            pass
    return cusip, acc


def quartal_of(report_date: str) -> str:
    if not report_date:
        return ""
    y, m, _d = report_date.split("-")[:3]
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def merge_tickers(ticker: str, cfg: dict) -> str:
    """GOOGL -> GOOG je config smart_money.googl_merge (gleiche Aktie je Fonds)."""
    m = cfg.get("smart_money", {}).get("googl_merge", {}) or {}
    return str(m.get(str(ticker).upper(), ticker)).upper()


def compare_quarters(prev: dict[str, dict], cur: dict[str, dict]) -> list[dict]:
    """Vorquartals-Vergleich (cusip -> holding): aktion + aenderung_pct."""
    rows = []
    for cusip, h in cur.items():
        p = prev.get(cusip)
        ps = p.get("shares", float("nan")) if p else float("nan")
        if p is None or ps != ps or ps <= 0:
            aktion, aend = "neu", float("nan")
        elif h["shares"] > ps:
            aktion, aend = "erhoeht", h["shares"] / ps - 1.0
        elif h["shares"] < ps:
            aktion, aend = "reduziert", h["shares"] / ps - 1.0
        else:
            aktion, aend = "unveraendert", 0.0
        rows.append({"cusip": cusip, "aktion": aktion, "aenderung_pct": aend,
                     "shares": h["shares"], "wert": h.get("value", h.get("wert", float("nan"))),
                     "name": h.get("name", "")})
    for cusip, p in prev.items():
        if cusip not in cur:
            rows.append({"cusip": cusip, "aktion": "verkauft", "aenderung_pct": -1.0,
                         "shares": 0.0, "wert": 0.0, "name": p.get("name", "")})
    return rows


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.commit()
    return con


def store_quarter(con: sqlite3.Connection, fonds: str, quartal: str, filing_datum: str,
                  rows: list[dict], cusip2ticker: dict[str, str], portfolio_total: float,
                  options: list[dict], cfg: dict, prev_total: float | None,
                  by_ticker_keys: list | None = None) -> int:
    """Schreibt ein (Fonds, Quartal) in smart_money_positions + smart_money_options."""
    n = 0
    for r in rows:
        ticker = merge_tickers(cusip2ticker.get(r["cusip"], r["cusip"]), cfg)
        anteil = (r["wert"] / portfolio_total) if portfolio_total > 0 and r["wert"] == r["wert"] else None
        con.execute(
            """INSERT INTO smart_money_positions
               (fonds, quartal, filing_datum, ticker, shares, wert, anteil_portfolio,
                aktion, aenderung_pct, positions_gesamt, rotation_pct, geladen_am)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fonds, quartal, ticker) DO UPDATE SET
                 filing_datum=excluded.filing_datum, shares=excluded.shares, wert=excluded.wert,
                 anteil_portfolio=excluded.anteil_portfolio, aktion=excluded.aktion,
                 aenderung_pct=excluded.aenderung_pct, positions_gesamt=excluded.positions_gesamt,
                 rotation_pct=excluded.rotation_pct, geladen_am=excluded.geladen_am""",
            (fonds, quartal, filing_datum, ticker, r["shares"], r["wert"], anteil,
             r["aktion"], r["aenderung_pct"], len(by_ticker_keys), None,
             datetime.now().isoformat(timespec="seconds")))
        n += 1
    # rotation_pct = Wert der veraenderten Positionen / Portfoliowert Vorquartal
    moved = sum(r["wert"] for r in rows if r["aktion"] in ("neu", "verkauft", "erhoeht", "reduziert")
                and r["wert"] == r["wert"])
    rot = (moved / prev_total) if prev_total and prev_total > 0 else None
    if rot is not None:
        con.execute("UPDATE smart_money_positions SET rotation_pct=? WHERE fonds=? AND quartal=?",
                    (rot, fonds, quartal))
    for o in options:
        ticker = merge_tickers(cusip2ticker.get(o["cusip"], o["cusip"]), cfg)
        con.execute(
            """INSERT INTO smart_money_options (fonds, quartal, filing_datum, ticker, put_call, shares, wert, geladen_am)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(fonds, quartal, ticker, put_call) DO UPDATE SET
                 filing_datum=excluded.filing_datum, shares=excluded.shares, wert=excluded.wert,
                 geladen_am=excluded.geladen_am""",
            (fonds, quartal, filing_datum, ticker, o["put_call"], o["shares"], o.get("value", o.get("wert")),
             datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return n


def fetch_all(cfg: dict, quarters: int | None = None, db_path: Path = DB_PATH,
              fetcher=None, report: list | None = None) -> list[dict]:
    """Laedt die letzten `quarters` Quartale je Fonds (13F-HR, A-Fassung gewinnt),
    speichert Positionen/Optionen, sammelt 13D-Katalysatoren der Aktivisten.

    Rueckgabe: Fehlerliste [{fonds, error}]. `report` (optional) bekommt die
    CIK-Aufloesung + Filer-Status [{name, cik, filer_status}].
    """
    sm = cfg.get("smart_money", {})
    nq = int(quarters if quarters is not None else sm.get("quarters", 8))
    r = sm.get("retry_403_429", sm.get("retry_429", {}))
    _waits = r.get("wartezeiten_s") or ([r["warte_s"]] * int(r.get("versuche", 3)) if "warte_s" in r else None)
    errors: list[dict] = []
    # v44 (1): User-Agent aus smart_money.user_agent (Fallback sec.user_agent),
    # validiert -- bei ungueltigem Wert Abbruch VOR dem ersten Request
    http = SecHttp(user_agent_from_cfg(cfg),
                   float(sm.get("max_requests_per_second", 5.0)),
                   tuple(_waits or [2, 5, 10]), int(r.get("versuche", 3)),
                   fetcher=fetcher, errors=errors)
    con = connect(db_path)
    ciks = resolve_all_ciks(cfg, http)
    if report is not None:
        report.extend(ciks)
    funds = {f["name"]: f for f in sm.get("funds", [])}
    for entry in ciks:
        name, cik = entry["name"], entry["cik"]
        if not cik:
            logger.warning("smart_money: %s -> %s -> uebersprungen (CIK manuell in config.yaml smart_money.funds eintragen; Treffer siehe Bericht)",
                           name, entry["filer_status"])
            errors.append({"fonds": name, "error": f"{entry['filer_status']} -- manuell eintragen"})
            continue
        fcfg = funds.get(name, {})
        gew = float(fcfg.get("gewicht", 1.0))
        try:
            filings = list_13f_filings(cik, http, max_filings=nq + 4)
            if not filings:
                logger.warning("smart_money: %s (CIK %s) hat keine 13F-HR -> uebersprungen", name, cik)
                errors.append({"fonds": name, "error": "keine 13F-HR gefunden"})
                continue
            qdata: list[tuple[str, str, list, list]] = []
            for f in filings:
                rd, holdings, options = load_13f_quarter(cik, f["accession"], http,
                                                          report_date=f.get("report_date", ""))
                qdata.append((rd or f["report_date"], f["filing_date"], holdings, options))
            qdata.sort(key=lambda x: x[0])
            qdata = qdata[-nq:]
            # CUSIP->Ticker fuer ALLE Quartale vorab (damit GOOG/GOOGL-Merging
            # VOR dem Vorquartals-Vergleich passiert: gleiche Aktie je Fonds)
            all_cusips = list({h["cusip"] for _rd, _fd, hl, _op in qdata for h in hl}
                              | {o["cusip"] for _rd, _fd, _hl, op in qdata for o in op})
            c2t = cusip_to_tickers(all_cusips, http, cfg, errors=errors)
            prev: dict[str, dict] = {}
            prev_total = None
            for report_date, filing_date, holdings, options in qdata:
                q = quartal_of(report_date)
                # nach Ticker mergen (GOOGL->GOOG): shares + wert summieren
                by_ticker: dict[str, dict] = {}
                for h in holdings:
                    t = merge_tickers(c2t.get(h["cusip"], h["cusip"]), cfg)
                    if t in by_ticker:
                        by_ticker[t]["shares"] += h["shares"]
                        by_ticker[t]["value"] += h["value"]
                    else:
                        by_ticker[t] = {"cusip": h["cusip"], "name": h["name"],
                                        "shares": h["shares"], "value": h["value"]}
                rows = compare_quarters(prev, by_ticker)
                total = sum(h["value"] for h in by_ticker.values() if h["value"] == h["value"])
                n = store_quarter(con, name, q, filing_date, rows, c2t, total, options, cfg, prev_total,
                                  by_ticker_keys=list(by_ticker))
                logger.info("smart_money: %s %s -> %d Positionen (%d Optionen)", name, q, n, len(options))
                prev = by_ticker
                prev_total = total
            # Aktivist: 13D/13D-A als Katalysator sammeln (gewicht 0)
            if fcfg.get("typ") == "aktivist" or float(gew) == 0.0:
                try:
                    for d in list_13d_filings(cik, http):
                        cusip, acc = load_13d_target(cik, d["accession"], http)
                        t = merge_tickers(cusip_to_tickers([cusip], http, cfg).get(cusip, cusip), cfg) if cusip else ""
                        if t:
                            con.execute(
                                """INSERT INTO smart_money_katalysator (ticker, fonds, datum, quelle_url, form)
                                   VALUES (?,?,?,?,?)""",
                                (t, name, d["filing_date"],
                                 f"{_sec_base()}/Archives/edgar/data/{str(int(cik.lstrip('0') or '0'))}/{d['accession'].replace('-', '')}/",
                                 d["form"]))
                    con.commit()
                except Exception as exc:
                    logger.warning("smart_money: 13D-Katalysatoren %s fehlgeschlagen: %s", name, exc)
        except Exception as exc:
            logger.error("smart_money: Fehler bei %s (CIK %s): %s", name, cik, exc)
            errors.append({"fonds": name, "error": str(exc)[:200]})
    return errors


# --------------------------------------------------------------------------
# (4) Score + Zusatzfelder
# --------------------------------------------------------------------------

def _fonds_gewicht(cfg: dict, fonds: str) -> float:
    for f in cfg.get("smart_money", {}).get("funds", []):
        if f.get("name") == fonds:
            return float(f.get("gewicht", 1.0))
    return 1.0


def _rows_for(con: sqlite3.Connection, ticker: str) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT * FROM smart_money_positions WHERE ticker=? ORDER BY quartal, fonds",
        (ticker.upper(),))]


def rationale_exists(con: sqlite3.Connection, ticker: str) -> bool:
    r = con.execute("SELECT COUNT(*) AS n FROM smart_money_rationale WHERE ticker=?",
                    (ticker.upper(),)).fetchone()
    return bool(r and r[0] > 0)


def katalysatoren(con: sqlite3.Connection, ticker: str) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT fonds, datum, quelle_url, form FROM smart_money_katalysator "
        "WHERE ticker=? ORDER BY datum DESC", (ticker.upper(),))]


def smart_money_score(ticker: str, cfg: dict, db_path: Path = DB_PATH,
                      heute: date | None = None) -> dict:
    """Score 1..4 + Zusatzfelder (config smart_money.rules)."""
    rules = cfg.get("smart_money", {}).get("rules", {})
    ein_max = float(rules.get("ein_fonds_anteil_max", 0.02))
    gew_min = float(rules.get("gewicht_summe_min", 2.0))
    anteil_g1_min = float(rules.get("anteil_gewicht1_min", 0.05))
    zwei_q = bool(rules.get("zwei_quartale", True))
    verk_schw = float(rules.get("verkauf_schwelle", 0.30))
    min_score = int(rules.get("verkauf_min_score", 1))
    heute = heute or date.today()

    con = connect(db_path)
    rows = _rows_for(con, ticker)
    empty = {"score": 1, "fonds_kaeufer": [], "fonds_verkaeufer": [],
             "letzte_aktion": "", "filing_datum": "", "daten_alter_tage": None,
             "rationale_vorhanden": False, "katalysator_13d": ""}
    if not rows:
        return empty

    quarters = sorted({r["quartal"] for r in rows})
    last_q = quarters[-1]
    last = [r for r in rows if r["quartal"] == last_q]

    def _gew(fonds: str) -> float:
        return _fonds_gewicht(cfg, fonds)

    # Kaeufer/Verkaeufer im letzten Quartal (nur Gewicht > 0 zaehlt im Score;
    # Aktivisten (gewicht 0) erscheinen nur als Katalysator)
    kaeufer = sorted({r["fonds"] for r in last
                      if r["aktion"] in ("neu", "erhoeht") and _gew(r["fonds"]) > 0})
    verkaeufer = sorted({r["fonds"] for r in last
                         if r["aktion"] in ("verkauft", "reduziert")
                         and (r["aenderung_pct"] is None or r["aenderung_pct"] <= -verk_schw)})
    letzte_aktion = "; ".join(f"{r['fonds']}:{r['aktion']}" for r in last)
    filing_datum = max((r["filing_datum"] or "") for r in rows)
    try:
        daten_alter = (heute - date.fromisoformat(filing_datum[:10])).days if filing_datum else None
    except ValueError:
        daten_alter = None
    rat = rationale_exists(con, ticker)
    kat = katalysatoren(con, ticker)
    kat_str = "; ".join(f"{k['fonds']} {k['form']} {k['datum']}" for k in kat)

    # Score-Basis
    gew_summe = sum(_gew(f) for f in kaeufer)
    _g1 = [r["anteil_portfolio"] or 0.0 for r in last
           if r["fonds"] in kaeufer and _gew(r["fonds"]) == 1.0]
    max_anteil_g1 = max(_g1) if _g1 else 0.0
    cond3 = (gew_summe >= gew_min) or (max_anteil_g1 >= anteil_g1_min)
    _anteile = [r["anteil_portfolio"] or 0.0 for r in last if r["fonds"] in kaeufer]
    if not kaeufer:
        score = 1
    elif len(kaeufer) == 1 and (max(_anteile) if _anteile else 0.0) < ein_max and not cond3:
        score = 2
    elif cond3:
        score = 3
    else:
        score = 2
    # Score 4: cond3 in zwei aufeinanderfolgenden Quartalen + Rationale
    if zwei_q and rat and score >= 3 and len(quarters) >= 2:
        prev_q = quarters[-2]
        prev = [r for r in rows if r["quartal"] == prev_q]
        prev_kaeufer = {r["fonds"] for r in prev
                        if r["aktion"] in ("neu", "erhoeht") and _gew(r["fonds"]) > 0}
        prev_gew = sum(_gew(f) for f in prev_kaeufer)
        _pg1 = [r["anteil_portfolio"] or 0.0 for r in prev
                if r["fonds"] in prev_kaeufer and _gew(r["fonds"]) == 1.0]
        prev_anteil_g1 = max(_pg1) if _pg1 else 0.0
        if (prev_gew >= gew_min or prev_anteil_g1 >= anteil_g1_min):
            score = 4
    score = max(min_score, score - len(verkaeufer))
    return {"score": score, "fonds_kaeufer": kaeufer, "fonds_verkaeufer": verkaeufer,
            "letzte_aktion": letzte_aktion, "filing_datum": filing_datum,
            "daten_alter_tage": daten_alter, "rationale_vorhanden": rat,
            "katalysator_13d": kat_str}


# --------------------------------------------------------------------------
# (5) Overlap mit dem Screener
# --------------------------------------------------------------------------

STATUS_POS = {"Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"}


def _f(row: dict, *keys: str) -> float:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return float("nan")


def urteil(row: dict, sm: dict) -> str:
    """smart_money_urteil: Bestaetigung / Divergenz / Warnung / neutral."""
    status = (row.get("status_all") or row.get("status") or "").strip()
    mos_min = _f(row, "mos_rule", "mos_min", "margin_of_safety")
    kauft = bool(sm["fonds_kaeufer"])
    verkauft = bool(sm["fonds_verkaeufer"])
    pos = status in STATUS_POS
    if kauft and pos:
        return "Bestaetigung"
    if verkauft and status == "nein":
        return "Bestaetigung"
    if kauft and (mos_min != mos_min or mos_min < 0 or status == "nein"):
        return "Divergenz"
    if verkauft and pos:
        return "Warnung"
    return "neutral"


def load_ranking_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("ticker") and not r["ticker"].startswith("#")]


def load_watch_csv(path: str | None) -> list[str]:
    p = Path(path) if path else Path("ticker_watch_v40.csv")
    if not p.exists():
        return []
    from data.tickerlist import load_ticker_file
    return load_ticker_file(str(p))


def overlap(ranking_csv: str, watch_csv: str | None, cfg: dict,
            db_path: Path = DB_PATH, out_path: str = "smart_money_overlap.csv") -> list[dict]:
    """Abgleich Screener x Smart Money; schreibt smart_money_overlap.csv.

    Ticker: alle Watchlist-Ticker + alle Ticker mit Fonds-Aktivitaet.
    """
    rows_in = load_ranking_csv(ranking_csv)
    by_t = {(r.get("ticker") or "").strip().upper(): r for r in rows_in}
    watch = load_watch_csv(watch_csv)
    con = connect(db_path)
    con.row_factory = sqlite3.Row
    aktive = {r["ticker"] for r in con.execute(
        "SELECT DISTINCT ticker FROM smart_money_positions")}
    tickers = list(dict.fromkeys(watch + sorted(aktive)))
    out = []
    for t in tickers:
        row = by_t.get(t, {"ticker": t, "status_all": "", "quality": "", "mos_rule": ""})
        sm = smart_money_score(t, cfg, db_path=db_path)
        out.append({
            "ticker": t,
            "status_all": row.get("status_all") or row.get("status") or "",
            "quality": row.get("quality") or "",
            "mos_rule": row.get("mos_rule") or row.get("margin_of_safety") or "",
            "smart_money_score": sm["score"],
            "fonds_kaeufer": ",".join(sm["fonds_kaeufer"]),
            "fonds_verkaeufer": ",".join(sm["fonds_verkaeufer"]),
            "letzte_aktion": sm["letzte_aktion"],
            "filing_datum": sm["filing_datum"],
            "daten_alter_tage": sm["daten_alter_tage"] if sm["daten_alter_tage"] is not None else "",
            "katalysator_13d": sm["katalysator_13d"],
            "rationale_vorhanden": "ja" if sm["rationale_vorhanden"] else "nein",
            "smart_money_urteil": urteil(row, sm),
        })
    cols = list(out[0].keys()) if out else ["ticker"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in out:
            w.writerow([r.get(c, "") for c in cols])
    logger.info("overlap gespeichert: %s (%d Ticker)", out_path, len(out))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DIAG_URLS = [
    "https://data.sec.gov/submissions/CIK0001067983.json",
    "https://www.sec.gov/Archives/edgar/data/1067983/",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=1067983&type=13F-HR&dateb=&owner=include&count=5&output=atom",
]


def cmd_diagnose(args) -> None:
    """v45 (1): Diagnose -- Config-Pfad (absolut), gesendeter User-Agent und
    je Test-URL HTTP-Status + erste 200 Zeichen der Antwort."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from config import config_path_info
    cfg_path = config_path_info()
    print(f"Config-Pfad (absolut): {cfg_path}")
    cfg = load_config(cfg_path)
    ua = user_agent_from_cfg(cfg)
    print(f"User-Agent (gesendet): {ua!r}")
    print(f"sec.user_agent: {cfg.get('sec', {}).get('user_agent')!r}  "
          f"smart_money.user_agent: {cfg.get('smart_money', {}).get('user_agent')!r}")
    for url in DIAG_URLS:
        try:
            status, body, _h = sec_get(url, ua)
            print(f"  [{status}] {url}")
            print(f"         Antwortbeginn: {body[:200].decode('utf-8', 'replace')!r}")
        except Exception as exc:
            print(f"  [FEHLER] {url}: {exc}")


def cmd_fetch(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from config import config_path_info
    print(f"Config-Pfad (absolut): {config_path_info()}")  # v45 (3): beim Start ausgeben
    cfg = load_config()
    report: list[dict] = []
    errors = fetch_all(cfg, quarters=args.quarters, db_path=Path(args.db), report=report)
    print("\n=== CIK-Aufloesung + Filer-Status (v44: config -> Firmensuche -> manuell) ===")
    for e in report:
        print(f"  {e['name']:<38} cik={e['cik'] or '-':<12} {e['filer_status']}"
              + (f" | EDGAR-Name: {e.get('bericht_name') or '-'}"
                 f" | letztes 13F-HR: {e.get('last_13f_hr') or '-'}" if e.get("cik") else ""))
        for t in (e.get("treffer") or []):
            print(f"      Treffer: cik={t['cik']} name={t['name']!r} letztes 13F-HR={t['last_13f_hr']}")
    if errors:
        print(f"\nFehlerlog ({len(errors)}):")
        for e in errors:
            print(f"  {e.get('fonds') or e.get('cusip')}: {e['error'][:120]}")
    else:
        print("\nKeine Fehler -- alle Fonds aktualisiert.")


def cmd_score(args) -> None:
    cfg = load_config()
    if args.ranking:
        rows = load_ranking_csv(args.ranking)
        tickers = [(r.get("ticker") or "").strip().upper() for r in rows]
    else:
        tickers = args.tickers
    print(f"{'ticker':<8} {'score':>5} {'kaeufer':<30} {'verkaeufer':<20} {'alter_t':>7}  letzte_aktion / filing / katalysator")
    for t in tickers:
        s = smart_money_score(t, cfg, db_path=Path(args.db))
        print(f"{t:<8} {s['score']:>5} {','.join(s['fonds_kaeufer']) or '-':<30} "
              f"{','.join(s['fonds_verkaeufer']) or '-':<20} "
              f"{str(s['daten_alter_tage'] if s['daten_alter_tage'] is not None else '-'):>7}  "
              f"{s['letzte_aktion'] or '-'} | {s['filing_datum'] or '-'} | {s['katalysator_13d'] or '-'}")


def cmd_overlap(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    out = overlap(args.ranking, args.watch, cfg, db_path=Path(args.db), out_path=args.out)
    print(f"\n=== smart_money_overlap ({len(out)} Ticker) -> {args.out} ===")
    print(f"{'ticker':<8} {'status_all':<28} {'score':>5} {'urteil':<14} kaeufer / verkaeufer")
    for r in out:
        print(f"{r['ticker']:<8} {r['status_all'][:28]:<28} {r['smart_money_score']:>5} "
              f"{r['smart_money_urteil']:<14} {r['fonds_kaeufer'] or '-'} / {r['fonds_verkaeufer'] or '-'}")


def cmd_ciks(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    sm = cfg.get("smart_money", {})
    http = SecHttp(user_agent_from_cfg(cfg),
                   float(sm.get("max_requests_per_second", 5.0)))
    for e in resolve_all_ciks(cfg, http):
        print(f"{e['name']:<38} cik={e['cik'] or '-':<12} {e['filer_status']}"
              + (f" | {e.get('bericht_name') or ''} letztes 13F-HR {e.get('last_13f_hr') or ''}"
                 if e.get("cik") else ""))
        for t in (e.get("treffer") or []):
            print(f"    Treffer: cik={t['cik']} name={t['name']!r} letztes 13F-HR={t['last_13f_hr']}")


def cmd_rationale_import(args) -> None:
    cfg = load_config()
    con = connect(Path(args.db))
    n, skipped = 0, 0
    with open(args.csv_file, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip().upper()
            text = (row.get("text") or "").strip()
            quelle = (row.get("quelle_url") or "").strip()
            if not ticker or not text:
                skipped += 1
                continue
            if not quelle:
                print(f"Uebersprungen (Pflichtfeld quelle_url fehlt): {ticker}", file=sys.stderr)
                skipped += 1
                continue
            con.execute(
                """INSERT INTO smart_money_rationale (ticker, fonds, text, quelle_url, datum, typ)
                   VALUES (?,?,?,?,?,?)""",
                (ticker, row.get("fonds") or None, text, quelle,
                 row.get("datum") or None, row.get("typ") or "Sonstiges"))
            n += 1
    con.commit()
    print(f"{n} Rationale importiert, {skipped} uebersprungen (aus {args.csv_file}).")


def cmd_show(args) -> None:
    con = connect(Path(args.db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM smart_money_positions WHERE ticker=? ORDER BY quartal DESC, fonds",
        (args.ticker.upper(),)).fetchall()
    print(f"=== smart_money_positions {args.ticker} ({len(rows)}) ===")
    for r in rows:
        print(f"{r['quartal']} {r['fonds']:<30} {r['aktion']:<14} shares={r['shares'] or 0:>12,.0f} "
              f"anteil={r['anteil_portfolio'] or 0:.2%} aend="
              f"{r['aenderung_pct'] if r['aenderung_pct'] is not None else float('nan'):.1%} filing={r['filing_datum']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smart Money: 13F-HR der Fonds-Liste (SEC EDGAR)")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite-Datei (Default: value_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_f = sub.add_parser("fetch", help="13F-HR laden (letzte N Quartale je Fonds)")
    p_f.add_argument("--quarters", type=int, default=None, help="Default: config smart_money.quarters (8)")
    p_f.set_defaults(func=cmd_fetch)
    p_s = sub.add_parser("score", help="smart_money_score je Ticker (oder --ranking CSV)")
    p_s.add_argument("tickers", nargs="*")
    p_s.add_argument("--ranking", default=None, help="ranking CSV (alle Ticker daraus)")
    p_s.set_defaults(func=cmd_score)
    p_o = sub.add_parser("overlap", help="Abgleich Screener x Smart Money -> smart_money_overlap.csv")
    p_o.add_argument("--ranking", required=True, help="ranking CSV (status_all, quality, mos_*)")
    p_o.add_argument("--watch", default=None, help="Watchlist-CSV (Default: ticker_watch_v40.csv falls vorhanden)")
    p_o.add_argument("--out", default="smart_money_overlap.csv")
    p_o.set_defaults(func=cmd_overlap)
    p_c = sub.add_parser("ciks", help="CIK-Aufloesung + 13F-Filer-Status aller Fonds")
    p_c.set_defaults(func=cmd_ciks)
    p_d = sub.add_parser("diagnose", help="v45: Config-Pfad, User-Agent, HTTP-Status je Test-URL")
    p_d.set_defaults(func=cmd_diagnose)
    p_r = sub.add_parser("rationale-import", help="Rationale aus CSV importieren (Pflicht quelle_url)")
    p_r.add_argument("csv_file", nargs="?", default=None,
                     help="Default: config smart_money.rationale_csv (rationale_import.csv)")
    p_r.set_defaults(func=cmd_rationale_import)
    p_sh = sub.add_parser("show", help="Positionen eines Tickers")
    p_sh.add_argument("ticker")
    p_sh.set_defaults(func=cmd_show)
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    from config import config_path_info
    if getattr(args, "cmd", None) != "diagnose":
        print(f"Config-Pfad (absolut): {config_path_info()}")  # v45 (3)
    if getattr(args, "cmd", None) == "rationale-import" and not getattr(args, "csv_file", None):
        cfg = load_config()
        args.csv_file = cfg.get("smart_money", {}).get("rationale_csv", "rationale_import.csv")
    args.func(args)


if __name__ == "__main__":
    main()
