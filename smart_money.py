"""Smart Money -- 13F-HR einer Fonds-Liste aus SEC EDGAR (smart_money.py).

Beim Start (connect) prueft das Modul per PRAGMA table_info,
    ob smart_money_positions die Spalten periode, accession, report_date hat.
    Fehlt eine -> Tabelle migrieren: Altzeilen werden verworfen (Periodenlabel
    falsch), Hinweis im Log; `fetch --reset` legt die Tabelle per DROP + CREATE
    im aktuellen Schema neu an (cusip_map bleibt).
store_quarter schreibt periode auch in smart_money_options;
    Migration, `fetch --reset` und der Schema-Guard pruefen daher BEIDE Tabellen
    (Optionen: Pflichtspalte periode) -- eine alte Options-Tabelle (v42: quartal)
    wird verworfen und neu aufgebaut statt per CREATE IF NOT EXISTS zu ueberleben.
die aelteste geladene Periode je Fonds hat aktion =
    "basis" (nie "neu"); neu/erhoeht/reduziert/verkauft nur mit Vorquartal.
    Score und Urteil ignorieren "basis".
nach fetch je Fonds Perioden + Positionszahl
    ausgeben; score und overlap brechen mit klarer Meldung ab, wenn die neueste
    Periode eines Fonds aelter als 140 Tage ist oder in den letzten 4 Perioden
    eine fehlt (--force ueberstimmt). Die Ausgabe nennt je Fonds die Periode,
    die in die Berechnung einging.
score/overlap [--force] uebersteuert ausschliesslich diesen Abbruch
    (Schema-Guard/User-Agent-Pruefung schlagen weiterhin durch). Die Abbruchmeldung
    nennt je blockiertem Fonds Periode + Grund und den Ausweg --force; mit --force
    Warnzeile je blockiertem Fonds im Log. Ergebnis-CSV wird erst am Ende geschrieben --
    bricht der Guard ab, bleibt eine vorhandene CSV unveraendert.
Abdeckung je Fonds nach Wert (Anteil mit Ticker) + die 20
    groessten nicht zugeordneten Positionen (CUSIP, name, wert, titleOfClass).
    Anleihen, Optionen und Nicht-US-Titel gelten als "nicht aktienrelevant" und
    zaehlen nicht als Fehler. Ziel: >= 95 % nach Wert bei US-Aktien.
overlap --watch trimmt BOM + Leerzeichen und listet
    Watch-Ticker ohne Fonds-Treffer ausdruecklich auf.

Label "periode" = Jahr-Qn exklusiv aus reportDate

Label "periode" = Jahr-Qn exklusiv aus reportDate
    (2026-06-30 -> 2026-Q2, 2026-03-31 -> 2026-Q1). Eindeutiger Schluessel
    (fonds, periode, cusip, accession); 13F-HR/A ersetzt das Original derselben
    Periode (nur die neueste Fassung bleibt in der DB). Neuer Schalter
    `fetch --reset`: leert smart_money_positions (cusip_map bleibt) und laedt
    die letzten 8 Perioden je Fonds neu. Danach Vollstaendigkeitspruefung je
    Fonds (Perioden, fehlende Perioden, Anzahl Positionen, Warnung bei Luecken)
    + Mapping-Abdeckung je Fonds (Anteil der Positionen nach Wert mit Ticker).
CUSIP->Ticker ueber cusip_map (dauerhaft), statisches
    config-cusip_map, OpenFIGI (max. 5 Auftraege/Anfrage ohne API-Key, bis 100
    mit OPENFIGI_API_KEY, mind. 2,5 s Pause, Ergebnis dauerhaft in cusip_map,
    quelle openfigi) und Fallback nameOfIssuer normalisiert gegen die Titel in
    SEC company_tickers.json (quelle name_match). Nicht zuordenbar -> ticker
    NULL, cusip bleibt gespeichert. Ziel: >= 95 % der Positionen nach Wert
    mit Ticker.

Screener-Logik bleibt unveraendert; Modul ist eigenstaendig lauffaehig
(CSV/SQLite value_tracker.db, Tabellen werden bei Bedarf angelegt).

Aufruf:
    python smart_money.py fetch [--quarters 8] [--reset] [--db value_tracker.db]
    python smart_money.py score TICKER [TICKER ...] [--db ...] [--force]
    python smart_money.py score --ranking ranking.csv [--db ...] [--force]
    python smart_money.py overlap --ranking ranking.csv --watch ticker_watch_v40.csv
        [--out smart_money_overlap.csv] [--db ...] [--force]
    python smart_money.py rationale-import rationale_import.csv [--db ...]
    python smart_money.py rationale-template [--out rationale_import.csv] [--db ...]
        # Vorlage mit Score-4-Kandidaten (Score-3-Bedingung in 2 aufeinanderfolgenden
        # Quartalen); Pflichtfelder text + quelle_url pro Zeile, Import bricht sonst mit
        # Zeilennummer ab; Duplikate (ticker, fonds, quelle_url) werden ueberschrieben
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
    smart_money_positions (fonds, periode, report_date, accession, filing_datum,
    cusip, ticker, shares, wert, anteil_portfolio, aktion, aenderung_pct,
    positions_gesamt, rotation_pct; UNIQUE (fonds, periode, cusip, accession)),
    smart_money_rationale (nur Import rationale_import.csv, Pflicht text + quelle_url;
    rationale-template schreibt die Vorlage, Duplikate (ticker, fonds,
    quelle_url) per INSERT OR REPLACE ueberschreiben; rationale_quelle in der
    overlap-CSV am Ende),
    smart_money_options, smart_money_katalysator (13D/13D-A der Aktivisten),
    cusip_map (dauerhaftes CUSIP->Ticker-Mapping, quelle openfigi|name_match).

(4) smart_money_score(ticker) 1..4 (config smart_money.rules):
    1 = kein Fonds mit Gewicht > 0; 2 = ein Fonds neu/erhoeht, Anteil < 2 %;
    3 = Summe Gewicht der Fonds mit neu/erhoeht >= gewicht_summe_min (2,0)
        ODER Anteil >= anteil_gewicht1_min (5 %) in einem Fonds mit Gewicht 1,0;
    4 = Score-3-Bedingung in zwei aufeinanderfolgenden Quartalen + Rationale.
    Verkaeufe: je Fonds verkauft ODER reduziert >= verkauf_schwelle -> -1,
    Minimum verkauf_min_score (1). Zusaetzlich je Ticker: fonds_kaeufer,
    fonds_verkaeufer, letzte_aktion, filing_datum, daten_alter_tage,
    katalysator_13d (aus 13D/13D-A).
    ETF/Index (SPY, IWM, ...): Erkennung ueber cusip_map.is_etf (OpenFIGI-
    US-Composite-Eintrag mit securityType 'ETP'/'ETF' bzw. marketSector
    'MarketSector/Market'; ADRs/REITs bleiben bewusst draussen) ODER statische
    Liste smart_money.etf_ticker (config.yaml).
    store_quarter speichert ETF-Positionen weiterhin (Transparenz), aber score ignoriert
    sie: Score 1, keine fonds_kaeufer/verkaeufer, keine letzte_aktion.

(5) overlap: Abgleich mit Screener (ranking-CSV mit status_all/quality/mos_*).
    smart_money_urteil je Ticker: Bestaetigung (Fonds kauft UND Status
    Kandidat/Beobachtung*, oder Fonds verkauft UND Status nein), Divergenz
    (Fonds kauft UND (MoS min < 0 oder Status nein)), Warnung (Fonds
    verkauft/reduziert UND Status Kandidat/Beobachtung), sonst neutral.
    ETF/Index-Zeilen stehen weiterhin in der CSV (Abdeckung
    nachvollziehbar), aber mit Score 1, Urteil neutral und Flag etf=ja
    (neue Spalte am Ende).
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
import os
import re
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

AKTIONEN = ("neu", "erhoeht", "reduziert", "verkauft", "unveraendert", "basis")


# --------------------------------------------------------------------------
# HTTP (SEC-Policy: User-Agent + Rate-Limit + Retry bei 429) -- injizierbar
# --------------------------------------------------------------------------

class UserAgentError(Exception):
    """ungueltiger User-Agent -- Abbruch VOR dem ersten Request."""


def validate_user_agent(ua: str) -> str:
    """SEC-Policy -- User-Agent im Format 'Name email@domain'.

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
    """smart_money.user_agent, Fallback sec.user_agent; validiert.

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
    """urllib-HTTP mit validiertem User-Agent, Rate-Limit und 403/429-Retry.

    fetcher: optionale Funktion url -> bytes (Tests ohne Netz). Echte 403/429
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
        self.fonds = ""  # aktueller Fondsname -- landet im Fehlerlog (statt "None")
        self.accession = ""  # aktuelle Accession -- landet im Fehlerlog

    def get(self, url: str, timeout: int = 30) -> bytes:
        gap = 1.0 / self.max_rps
        last_exc = None
        for attempt in range(1, self.retry_tries + 1):
            wait = self._last + gap - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            if self._fetcher is not None:
                return self._fetcher(url)
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in (403, 429) and attempt < self.retry_tries:
                    w = self.retry_waits[min(attempt - 1, len(self.retry_waits) - 1)]
                    logger.warning("SEC HTTP %d fuer %s (fonds=%s accession=%s) -- Retry %d/%d in %.0fs (User-Agent: %r)",
                                   exc.code, url, self.fonds or "-", self.accession or "-",
                                   attempt, self.retry_tries - 1, w, self.user_agent)
                    self._sleep(w)
                    continue
                # JEDER HTTP-Fehler (auch 404) landet mit URL + Fonds + Accession
                # im Fehlerlog -- 403/429 nach allen Retries mit UA-Hinweis
                if exc.code in (403, 429):
                    err = (f"HTTP {exc.code} nach {self.retry_tries} Versuchen -- "
                           f"User-Agent pruefen (gesendet: {self.user_agent!r})")
                else:
                    err = f"HTTP {exc.code}"
                self.errors.append({
                    "fonds": self.fonds or None,  # Fondsnamen statt "None"
                    "accession": self.accession or None,  # v46
                    "url": url, "http_code": exc.code, "error": err})
                logger.error("SEC HTTP %d fuer %s (fonds=%s accession=%s): %s",
                             exc.code, url, self.fonds or "-", self.accession or "-", err)
                raise


def _sec_base() -> str:
    return "https://www.sec.gov"


# --------------------------------------------------------------------------
# CIK-Aufloesung per EDGAR-Firmensuche (nie raten)
# --------------------------------------------------------------------------

def _validate_cik_13f(cik: str, http: SecHttp) -> dict | None:
    """CIK validieren -- data.sec.gov/submissions/CIK##########.json muss
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
    """EDGAR-Firmensuche -- cgi-bin/browse-edgar?action=getcompany&
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
    """CIK-Aufloesung je Fonds, Reihenfolge:
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


# Tokens, die beim Normalisieren von Firmennamen (nameOfIssuer vs. Titel in
# company_tickers.json) ignoriert werden
_ISSUER_TOKENS_IGNORE = frozenset({
    "THE", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
    "LLC", "LLP", "LP", "PLC", "SA", "NV", "AG", "SE", "CLASS", "COMMON",
    "STOCK", "SHARES", "ADR", "TR", "TRUST", "HOLDING", "HOLDINGS", "GROUP",
})


def normalize_issuer_name(name: str) -> str:
    """Firmenname normalisieren fuer den name_match-Fallback:
    Grossbuchstaben, Nicht-Alphanumerisch -> Leerzeichen, Suffix-Tokens
    (INC, CORP, AG, ...) entfernt. 'Apple Inc.' -> 'APPLE'."""
    s = re.sub(r"[^A-Z0-9]+", " ", (name or "").upper())
    return " ".join(t for t in s.split() if len(t) > 1 and t not in _ISSUER_TOKENS_IGNORE)


def load_company_titles(http: SecHttp) -> dict[str, str]:
    """SEC company_tickers.json -> {normalisierter Titel: TICKER}.
    Basis des nameOfIssuer-Fallbacks fuer US-Aktien ohne CUSIP-Mapping."""
    try:
        raw = http.get(f"{_sec_base()}/files/company_tickers.json")
        data = json.loads(raw.decode("utf-8"))
        out: dict[str, str] = {}
        for v in data.values():
            t = normalize_issuer_name(str(v.get("title", "")))
            tk = str(v.get("ticker", "")).upper()
            if t and tk and t not in out:
                out[t] = tk
        return out
    except Exception as exc:
        logger.warning("company_tickers.json (Titel) nicht ladbar: %s", exc)
        return {}


OPENFIGI_MIN_GAP_S = 2.5  # max 25 OpenFIGI-Anfragen/Minute (mindestens 2,5 s Pause)


def _openfigi_chunk_size() -> int:
    """ohne API-Key hoechstens 5 Auftraege je Anfrage; mit Umgebungsvariable
    OPENFIGI_API_KEY bis 100."""
    return 100 if os.environ.get("OPENFIGI_API_KEY", "").strip() else 5


def _openfigi_post(http: SecHttp, url: str, payload: bytes) -> bytes:
    """OpenFIGI POST; bei HTTP 429 Wiederholung (http.retry_tries Versuche)."""
    for attempt in range(1, http.retry_tries + 1):
        try:
            if http._fetcher is not None:
                return http._fetcher(url)
            headers = {"User-Agent": http.user_agent, "Content-Type": "application/json"}
            key = os.environ.get("OPENFIGI_API_KEY", "").strip()
            if key:
                headers["X-OPENFIGI-APIKEY"] = key
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < http.retry_tries:
                w = http.retry_waits[min(attempt - 1, len(http.retry_waits) - 1)]
                logger.warning("OpenFIGI HTTP 429 -- Retry %d/%d in %.0fs",
                               attempt, http.retry_tries - 1, w)
                http._sleep(w)
                continue
            raise


def _etf_ticker_set(cfg: dict) -> set[str]:
    """statische Config-Liste smart_money.etf_ticker (bekannte ETF/Index-Ticker,
    erweiterbar in config.yaml -- keine Magic Strings im Code)."""
    return {str(t).strip().upper() for t in (cfg.get("smart_money", {}).get("etf_ticker") or [])
            if str(t).strip()}


def _is_etf_eintrag(e: dict) -> bool:
    """OpenFIGI-US-Composite-Eintrag als ETF/Index werten, wenn securityType
    'ETP' oder 'ETF' ist (live verifiziert: SPY/IWM/QQQ/XLF -> 'ETP', securityType2
    'Mutual Fund') oder marketSector 'MarketSector/Market'. NICHT einfach
    securityType != 'Common Stock': ADRs sind 'ADR' (TSM, BABA, SAP, DEO) und REITs
    'REIT' (AMT, DLR) -- die duerfen nicht als ETF gelten (live gegen OpenFIGI geprueft).
    (SEC company_tickers.json enthaelt keine ETF-Titel -> dort bewusst KEIN Kriterium.)"""
    st = str(e.get("securityType") or "").strip()
    ms = str(e.get("marketSector") or "").strip()
    return st in ("ETP", "ETF") or ms == "MarketSector/Market"


def is_etf_ticker(con: sqlite3.Connection | None, ticker: str, cfg: dict) -> bool:
    """ETF/Index? Zwei Quellen: (a) statische Config-Liste smart_money.etf_ticker;
    (b) cusip_map.is_etf=1 (aus der OpenFIGI-Antwort gesetzt, siehe _is_etf_eintrag)."""
    t = (ticker or "").strip().upper()
    if not t:
        return False
    if t in _etf_ticker_set(cfg):
        return True
    if con is not None:
        try:
            row = con.execute("SELECT 1 FROM cusip_map WHERE ticker=? AND is_etf=1 LIMIT 1",
                              (t,)).fetchone()
        except sqlite3.OperationalError:
            row = None  # alte DB ohne Spalte -- connect() migriert additiv per ALTER
        if row:
            return True
    return False


def cusip_to_tickers(cusips: list[str], http: SecHttp, cfg: dict,
                     errors: list | None = None, con: sqlite3.Connection | None = None,
                     unmapped: list | None = None,
                     names: dict[str, str] | None = None,
                     title_map: dict[str, str] | None = None,
                     etf: dict[str, bool] | None = None) -> dict[str, str]:
    """CUSIP -> Ticker. Reihenfolge: Tabelle cusip_map (dauerhaft in der DB,
    wird nicht erneut abgefragt), statisches cusip_map (config/Fixtures), dann
    OpenFIGI POST (cusip_map_url) in Chunks -- 5 ohne API-Key, 100 mit
    OPENFIGI_API_KEY; mindestens 2,5 s Pause (max 25/Minute), 429 -> Retry.
    Rest: Fallback ueber nameOfIssuer (normalisiert) gegen die Titel in SEC
    company_tickers.json (`title_map`, siehe load_company_titles). Neu gemappte
    CUSIPs landen dauerhaft in cusip_map mit quelle 'openfigi' bzw.
    'name_match'. Nicht zuordenbare CUSIPs fehlen im Ergebnis (ticker NULL in
    smart_money_positions) und werden einmalig gesammelt (`unmapped`, bzw. eine
    Fehlerlog-Zeile), nicht als Einzelzeilen.
    optionales out-Param `etf` -- fuellt je CUSIP das ETF/Index-Flag
    (aus cusip_map.is_etf, der OpenFIGI-Antwort via _is_etf_eintrag oder der
    Config-Liste etf_ticker); wird IMMER dauerhaft in cusip_map.is_etf gespeichert
    (unabhaengig vom out-Param, damit fetch_all ohne etf-Param mitpersistiert)."""
    out: dict[str, str] = {}
    quelle: dict[str, str] = {}
    etf_map = etf if etf is not None else {}   # immer gefuellt, auch ohne out-Param
    etf_set = _etf_ticker_set(cfg)
    has_etf_col = (con is not None
                   and "is_etf" in {r[1] for r in con.execute("PRAGMA table_info(cusip_map)")})
    cusips = [c for c in dict.fromkeys(cusips) if c]
    if con is not None:
        for c in cusips:
            if has_etf_col:
                row = con.execute("SELECT ticker, quelle, is_etf FROM cusip_map WHERE cusip=?",
                                  (c,)).fetchone()
            else:
                row = con.execute("SELECT ticker, quelle FROM cusip_map WHERE cusip=?",
                                  (c,)).fetchone()
                row = (row + (0,)) if row else None
            if row and row[0]:
                out[c] = str(row[0]).upper()
                quelle[c] = str(row[1] or "manuell")
                etf_map[c] = bool(row[2])
    static_map = {str(k): str(v).upper()
                  for k, v in (cfg.get("smart_money", {}).get("cusip_map") or {}).items()}
    rest = []
    for c in cusips:
        if c in out:
            continue
        if c in static_map:
            out[c] = static_map[c]
            quelle[c] = "manuell"
            etf_map[c] = static_map[c] in etf_set
        else:
            rest.append(c)
            etf_map[c] = False
    if rest:
        url = str(cfg.get("smart_money", {}).get("cusip_map_url", ""))
        if url:
            size = _openfigi_chunk_size()
            last_of = 0.0
            for i in range(0, len(rest), size):
                chunk = rest[i:i + size]
                wait = last_of + OPENFIGI_MIN_GAP_S - time.monotonic()
                if wait > 0:
                    http._sleep(wait)
                last_of = time.monotonic()
                payload = json.dumps(
                    [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]).encode()
                try:
                    raw = _openfigi_post(http, url, payload)
                    data = json.loads(raw.decode("utf-8"))
                    for item, c in zip(data, chunk):
                        # OpenFIGI antwortet je Eingabe mit {"data": [eintrag, ...]}
                        # (Liste von Boersen-Eintraegen mit figi/ticker/exchCode/securityType)
                        # oder {"error": "..."} -- NICHT mit {"ticker": ...}. Gewaehlt wird der
                        # US-Eintrag (exchCode "US" = US-Composite); ohne Filter wuerden z. B.
                        # deutsche/andere Listing-Ticker gewinnen (MCO: DUT, Stuttgart, GR,
                        # steht in data oft VOR dem US-Eintrag).
                        if not isinstance(item, dict) or item.get("error"):
                            continue
                        eintrag = next((e for e in item.get("data") or []
                                        if isinstance(e, dict) and e.get("exchCode") == "US"), None)
                        t = eintrag.get("ticker") if eintrag else None
                        if t:
                            out[c] = str(t).upper()
                            quelle[c] = "openfigi"
                            # ETF-Flag aus dem US-Composite-Eintrag
                            # (securityType != 'Common Stock' / marketSector 'MarketSector/Market')
                            etf_map[c] = _is_etf_eintrag(eintrag) or out[c] in etf_set
                except Exception as exc:
                    logger.warning("OpenFIGI-Mapping fehlgeschlagen (Chunk ab Index %d): %s", i, exc)
    # Fallback nameOfIssuer (normalisiert) gegen Titel aus company_tickers.json
    # (company_tickers.json enthaelt keine ETF-Titel -> dort kein ETF-Kriterium, nur config etf_ticker)
    if title_map:
        for c in rest:
            if c in out:
                continue
            t = title_map.get(normalize_issuer_name((names or {}).get(c, "")))
            if t:
                out[c] = t.upper()
                quelle[c] = "name_match"
                etf_map[c] = out[c] in etf_set
    miss = [c for c in rest if c not in out]
    if con is not None and rest:
        now = datetime.now().isoformat(timespec="seconds")
        for c in rest:
            if c in out:
                if has_etf_col:  # is_etf dauerhaft speichern
                    con.execute(
                        "INSERT OR REPLACE INTO cusip_map (cusip, ticker, quelle, geladen_am, is_etf) "
                        "VALUES (?,?,?,?,?)",
                        (c, out[c], quelle.get(c, "openfigi"), now, int(bool(etf_map.get(c, False)))))
                else:
                    con.execute(
                        "INSERT OR REPLACE INTO cusip_map (cusip, ticker, quelle, geladen_am) "
                        "VALUES (?,?,?,?)", (c, out[c], quelle.get(c, "openfigi"), now))
        con.commit()
    if miss:
        if unmapped is not None:
            unmapped.extend(miss)
        elif errors is not None:
            errors.append({"cusip": ", ".join(miss),
                           "error": f"{len(miss)} CUSIP(s) nicht zuordenbar (OpenFIGI/kein Mapping)"})
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
        rec = {"cusip": cusip, "name": name, "shares": shares, "value": value,
               "title": _t(it, "titleOfClass")}  # titleOfClass (COMMON STOCK, BOND, ...)
        if pc in ("PUT", "CALL"):
            rec["put_call"] = pc
            options.append(rec)
        else:
            holdings.append(rec)
    return holdings, options


_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _normalize_date(s: str) -> str:
    """'2026-06-30' bleibt; US-Format '06-30-2026' (MM-DD-YYYY) -> '2026-06-30'.
    (Alt: quartal_of las '06-30-2026' als y=06, m=30 -> Label '06-Q10'.)"""
    s = (s or "").strip()[:10]
    parts = s.split("-")
    if len(parts) == 3 and len(parts[0]) <= 2:
        return f"{parts[2]}-{parts[0]}-{parts[1]}"
    return s


def _accession_from_href(href: str) -> str:
    r"""Accession aus einem EDGAR-Atom-Link. Echtes Linkformat:
    .../Archives/edgar/data/<cik>/<accession-ohne-Bindestriche>/<accession-mit-Bindestrichen>-index.htm
    -> Regex (\d{10}-\d{2}-\d{6}) gewinnt, damit das '-index.htm'-Suffix nicht
    in die Accession gelangt. Fallback: letztes Pfadsegment ohne Index-Suffix."""
    m = _ACCESSION_RE.search(href or "")
    if m:
        return m.group(1)
    seg = (href or "").rstrip("/").split("/")[-1]
    return re.sub(r"-?index\.html?$", "", seg)


def _accession_digits(accession: str) -> str:
    r"""Accession fuer den Archives-Pfad -- Bindestriche entfernen. Traegt der
    Wert das Muster \d{10}-\d{2}-\d{6}, wird nur dieser Teil verwendet (Schutz gegen
    Suffixe wie '-index.htm': .../000119312526352200/index.json statt
    .../000119312526352200index.htm/index.json)."""
    m = _ACCESSION_RE.search(accession or "")
    if m:
        return m.group(1).replace("-", "")
    return (accession or "").replace("-", "")


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
            accession = _accession_from_href(link.get("href", ""))
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


def _submissions_entries(cik: str, http: SecHttp) -> list[dict]:
    """Primaere Filing-Liste -- https://data.sec.gov/submissions/CIK##########.json,
    filings.recent (accessionNumber, form, filingDate, reportDate) als
    [{accession, filing_date, form, report_date}]. accessionNumber traegt
    Bindestriche (z. B. '0001193125-26-352200')."""
    cik10 = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    data = json.loads(http.get(url).decode("utf-8"))
    rec = data.get("filings", {}).get("recent", {})
    accs = rec.get("accessionNumber", [])
    forms = rec.get("form", [])
    fdates = rec.get("filingDate", [])
    rdates = rec.get("reportDate", [])
    out = []
    for i, acc in enumerate(accs):
        out.append({"accession": str(acc),
                    "filing_date": str(fdates[i]) if i < len(fdates) else "",
                    "form": str(forms[i]) if i < len(forms) else "",
                    "report_date": str(rdates[i]) if i < len(rdates) else ""})
    return out


def _atom_filings(cik: str, http: SecHttp, type_param: str, count: int) -> list[dict]:
    """Fallback Filing-Liste per Atom-Feed (browse-edgar ...&output=atom)."""
    cik_n = str(int(str(cik).lstrip("0") or "0"))
    url = (f"{_sec_base()}/cgi-bin/browse-edgar?action=getcompany&CIK={cik_n}"
           f"&type={type_param}&dateb=&owner=include&count={count}&output=atom")
    return _atom_entries(http.get(url))


def list_13f_filings(cik: str, http: SecHttp, max_filings: int = 12) -> list[dict]:
    """Letzte 13F-HR (+13F-HR/A) eines CIK. Primaer aus
    data.sec.gov/submissions/CIK##########.json (filings.recent), Atom-Feed nur
    noch Fallback. Bei Nachmeldung (A) ersetzt die neuere Fassung das Original
    zum selben report_date (neueste filing_date gewinnt)."""
    try:
        entries = _submissions_entries(cik, http)
        if not entries:
            raise ValueError("submissions ohne filings.recent")
    except Exception as exc:
        logger.warning("submissions-Liste CIK %s fehlgeschlagen (%s) -- Fallback Atom-Feed",
                       cik, exc)
        entries = _atom_filings(cik, http, "13F", max_filings * 3)
    hr = [e for e in entries if e["form"].startswith("13F-HR")]
    # Perioden-Schluessel = normalisiertes report_date (reportDate), Fallback
    # filing_date. 13F-HR + 13F-HR/A derselben Periode (auch gemischte Datumsformate)
    # ergeben EINEN Schluessel -- neueste filing_date gewinnt.
    # Diagnose v46: bei leerem report_date kollabierte die Dedupe und die Sortierung
    # lieferte die aeltesten Filings (2010/2011) statt der neuesten.
    def _key(e: dict) -> str:
        return _normalize_date(e["report_date"]) or e["filing_date"] or ""
    # neueste Fassung je Periode (13F-HR/A mit spaeterem filing_date gewinnt)
    best: dict[str, dict] = {}
    for e in hr:
        k = _key(e)
        if k not in best or e["filing_date"] > best[k]["filing_date"]:
            best[k] = e
    out = sorted(best.values(), key=lambda e: (_key(e), e["filing_date"]))[-max_filings:]
    return out


def list_13d_filings(cik: str, http: SecHttp, max_filings: int = 20) -> list[dict]:
    """13D/13D-A eines (aktivistischen) Filers:
    [{accession, filing_date, form, report_date}]. Primaer submissions-JSON,
    Atom-Feed nur Fallback."""
    try:
        entries = _submissions_entries(cik, http)
        if not entries:
            raise ValueError("submissions ohne filings.recent")
    except Exception as exc:
        logger.warning("submissions-Liste CIK %s fehlgeschlagen (%s) -- Fallback Atom-Feed",
                       cik, exc)
        entries = _atom_filings(cik, http, "SC+13D", max_filings)
    return [e for e in entries if e["form"] in ("SC 13D", "SC 13D/A", "13D", "13D-A")][:max_filings]


def _retry_503(fn, http: SecHttp, retries: int = 3, waits=(5.0, 15.0, 30.0)):
    """13D-Katalysatoren -- bei HTTP 503 dreimal wiederholen (5, 15, 30 s),
    also Erstversuch + 3 Wiederholungen. Ein Fehlschlag nach allen Versuchen wird
    vom Aufrufer abgefangen und blockiert den Lauf nicht."""
    for attempt in range(1, retries + 2):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and attempt <= retries:
                logger.warning("SEC HTTP 503 (fonds=%s) -- Retry %d/%d in %.0fs",
                               http.fonds or "-", attempt, retries, waits[attempt - 1])
                http._sleep(waits[attempt - 1])
                continue
            raise


def load_13f_quarter(cik: str, accession: str, http: SecHttp) -> tuple[str, list[dict], list[dict]]:
    """Laedt die infotable eines 13F. Rueckgabe: (report_date, holdings, options).

    Die Infotabelle wird NICHT ueber einen festen Dateinamen bestimmt (404 bei
    abweichenden Namen wie form13fInfoTable.xml), sondern aus
    <Filing-Verzeichnis>/index.json: alle .xml-Dateien der obersten Ebene ausser
    primary_doc.xml (keine Unterordner wie xslForm13F_X02) werden gelesen; die
    Datei, deren Wurzelelement informationTable ist, gewinnt. report_date kommt
    aus periodOfReport (zuerst in der Infotabelle selbst, dann in den uebrigen
    XML-Dateien inkl. primary_doc.xml)."""
    acc = _accession_digits(accession)
    cik_n = str(int(str(cik).lstrip("0") or "0"))
    base = f"{_sec_base()}/Archives/edgar/data/{cik_n}/{acc}"
    idx = json.loads(http.get(f"{base}/index.json").decode("utf-8"))
    items = idx.get("directory", {}).get("item", [])
    xml_files = [str(i.get("name", "")) for i in items
                 if str(i.get("name", "")).lower().endswith(".xml")
                 and i.get("type") != "directory"
                 and "/" not in str(i.get("name", ""))]
    fetched: dict[str, ET.Element] = {}

    def _root(name: str) -> ET.Element:
        if name not in fetched:
            fetched[name] = ET.fromstring(http.get(f"{base}/{name}"))
        return fetched[name]

    def _period_of(root: ET.Element) -> str:
        for el in root.iter():
            if el.tag.split("}")[-1] == "periodOfReport" and el.text:
                return el.text.strip()[:10]
        return ""

    infotable = None
    for name in xml_files:
        if name.lower() == "primary_doc.xml":
            continue
        try:
            root = _root(name)
        except Exception:
            continue
        if root.tag.split("}")[-1] == "informationTable" and infotable is None:
            infotable = name
    if infotable is None:
        raise ValueError(f"keine informationTable in {base} gefunden (xml-Dateien: {xml_files})")
    info_root = fetched[infotable]
    report_date = _period_of(info_root)
    if not report_date:
        for name in xml_files:
            if name == infotable:
                continue
            try:
                report_date = _period_of(_root(name))
            except Exception:
                continue
            if report_date:
                break
    holdings, options = _parse_infotable(ET.tostring(info_root))
    return report_date, holdings, options


def load_13d_target(cik: str, accession: str, http: SecHttp) -> tuple[str, str]:
    """13D/13D-A: Ziel-Ticker (CUSIP der issuer) + filing_date.
    Rueckgabe: (cusip, filing_date)."""
    acc = _accession_digits(accession)
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
    """Quartalslabel AUSSCHLIESSLICH aus dem reportDate des Filings
    (2026-06-30 -> 2026-Q2). US-Format MM-DD-YYYY wird normalisiert. Leer -> ''."""
    rd = _normalize_date(report_date)
    if not rd:
        return ""
    y, m, _d = rd.split("-")[:3]
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
        # echte CUSIP mitfuehren (Schluessel von cur ist ggf. gemergter Ticker)
        rows.append({"cusip": h.get("cusip") or cusip, "aktion": aktion, "aenderung_pct": aend,
                     "shares": h["shares"], "wert": h.get("value", h.get("wert", float("nan"))),
                     "name": h.get("name", ""), "title": h.get("title", "")})
    for cusip, p in prev.items():
        if cusip not in cur:
            rows.append({"cusip": p.get("cusip") or cusip, "aktion": "verkauft", "aenderung_pct": -1.0,
                         "shares": 0.0, "wert": 0.0, "name": p.get("name", ""),
                         "title": p.get("title", "")})
    return rows


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

# Pflichtspalten der Migration -- fehlt eine, sind die Altzeilen nicht
# zuverlaessig (Periodenlabel falsch) -> Tabelle wird verworfen und neu aufgebaut
MIGRATION_PFLICHT_SPALTEN = ("periode", "accession", "report_date")
# store_quarter schreibt periode auch in smart_money_options; eine alte
# Options-Tabelle (v42: Spalte quartal) ueberlebt sonst jedes CREATE IF NOT EXISTS
OPTIONS_PFLICHT_SPALTEN = ("periode",)


def _fehlende_pflichtspalten(con: sqlite3.Connection, table: str = "smart_money_positions",
                             spalten: tuple[str, ...] = MIGRATION_PFLICHT_SPALTEN) -> list[str]:
    """PRAGMA table_info(<table>) -> fehlende Spalten aus `spalten`
    (Default: smart_money_positions vs. MIGRATION_PFLICHT_SPALTEN =
    periode/accession/report_date)."""
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    return [c for c in spalten if c not in cols]


def _drop_wenn_veraltet(con: sqlite3.Connection, table: str, spalten: tuple[str, ...],
                        hinweis: str) -> None:
    """Tabelle `table` droppen, wenn sie existiert, aber Pflichtspalten fehlen
    (sonst ueberlebt CREATE IF NOT EXISTS die alte Form -- z. B. Options-Tabelle v42
    mit Spalte quartal; store_quarter scheitert dann mit no such column: periode)."""
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return
    fehlend = _fehlende_pflichtspalten(con, table, spalten)
    if fehlend:
        logger.warning("smart_money: Migration %s -- Pflichtspalten %s fehlen; %s",
                       table, ", ".join(fehlend), hinweis)
        con.execute(f"DROP TABLE {table}")


def _assert_positions_schema(con: sqlite3.Connection) -> None:
    """Schema-Guard -- haben die (ggf. soeben neu angelegten) Tabellen
    wirklich das aktuelle Schema (Positionen: periode/accession/report_date;
    Optionen: periode)? Sonst sofort RuntimeError (kein Silent-Continue, kein
    Teil-Lauf): schema.sql ist veraltet oder fehlt -> Lauf abbrechen, Altzeilen
    bleiben unveraendert."""
    for table, spalten in (("smart_money_positions", MIGRATION_PFLICHT_SPALTEN),
                           ("smart_money_options", OPTIONS_PFLICHT_SPALTEN)):
        fehlend = _fehlende_pflichtspalten(con, table, spalten)
        if fehlend:
            raise RuntimeError(
                f"smart_money: Tabelle {table} hat nicht das aktuelle Schema "
                f"(fehlende Spalten: {', '.join(fehlend)}). schema.sql ist veraltet oder fehlt: "
                f"{SCHEMA_PATH} -- schema.sql aktualisieren (siehe CLAUDE.md v48/v49) und den Lauf "
                "wiederholen. Altzeilen bleiben unveraendert.")


def _migrate_positions(con: sqlite3.Connection) -> None:
    """Migration smart_money_positions. Per PRAGMA table_info werden die
    Pflichtspalten (periode, accession, report_date) geprueft; fehlt eine, werden
    die Altzeilen verworfen (Perioden falsch) mit Hinweis im Log und die Tabelle
    aus schema.sql neu aufgebaut -- danach _assert_positions_schema (v49 1b).
    Anschliessend werden optionale Spalten (cusip, name, title_of_class) additiv
    per ALTER ergaenzt und periode aus report_date neu beschriftet."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(smart_money_positions)")}
    if not cols:
        return
    fehlend = _fehlende_pflichtspalten(con)
    if fehlend:
        logger.warning("smart_money: Migration smart_money_positions -- Pflichtspalten %s fehlen; "
                       "Altzeilen werden verworfen (Periodenlabel falsch), Tabelle wird neu aufgebaut "
                       "(cusip_map bleibt; Positionen per fetch/--reset neu laden)", ", ".join(fehlend))
        con.execute("DROP TABLE smart_money_positions")
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        con.commit()
        _assert_positions_schema(con)  # veraltete schema.sql -> RuntimeError statt Teil-Lauf
        return
    for col, decl in (("cusip", "TEXT NOT NULL DEFAULT ''"), ("name", "TEXT"), ("title_of_class", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE smart_money_positions ADD COLUMN {col} {decl}")
    rows = con.execute(
        "SELECT rowid, report_date, periode FROM smart_money_positions "
        "WHERE report_date IS NOT NULL AND report_date != ''").fetchall()
    changed = False
    for rowid, rd, p in rows:
        np = quartal_of(rd)
        if np and np != p:
            try:
                con.execute("UPDATE smart_money_positions SET periode=? WHERE rowid=?", (np, rowid))
                changed = True
            except sqlite3.IntegrityError:
                pass  # Relabel kollidiert mit vorhandener Periode -> Zeile bleibt (Duplikat)
    if changed:
        con.commit()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    # alte Tabellenform (Pflichtspalten fehlen) vor dem Schema-Lauf droppen
    # (sonst ueberlebt CREATE IF NOT EXISTS die alte Form und ein CREATE INDEX schlaegt
    # auf der fehlenden Spalte fehl); Altzeilen werden verworfen -- Hinweis im Log
    # siehe _drop_wenn_veraltet. Gilt fuer BEIDE Tabellen: smart_money_positions
    # (periode/accession/report_date) und smart_money_options (periode)
    _hinweis = ("Altzeilen werden verworfen (Periodenlabel falsch), Tabelle wird neu aufgebaut "
                "(cusip_map bleibt; Positionen/Optionen per fetch/--reset neu laden)")
    _drop_wenn_veraltet(con, "smart_money_positions", MIGRATION_PFLICHT_SPALTEN, _hinweis)
    _drop_wenn_veraltet(con, "smart_money_options", OPTIONS_PFLICHT_SPALTEN, _hinweis)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.commit()
    # additive Migration cusip_map.is_etf (ETF-Flag); frische DBs haben die
    # Spalte schon aus schema.sql, alte DBs bekommen sie per ALTER (Tabelle muss existieren --
    # bei veralteter schema.sql greift sonst der Schema-Guard unten mit RuntimeError)
    cols_cm = {r[1] for r in con.execute("PRAGMA table_info(cusip_map)")}
    if cols_cm and "is_etf" not in cols_cm:
        con.execute("ALTER TABLE cusip_map ADD COLUMN is_etf INTEGER NOT NULL DEFAULT 0")
        con.commit()
    _migrate_positions(con)
    _assert_positions_schema(con)  # Endverifikation nach connect
    return con


def store_quarter(con: sqlite3.Connection, fonds: str, periode: str, filing_datum: str,
                  rows: list[dict], cusip2ticker: dict[str, str], portfolio_total: float,
                  options: list[dict], cfg: dict, prev_total: float | None,
                  by_ticker_keys: list | None = None, report_date: str = "",
                  accession: str = "") -> int:
    """Schreibt ein (Fonds, Periode) in smart_money_positions + smart_money_options.
    eindeutiger Schluessel (fonds, periode, cusip, accession); eine andere
    Accession derselben Periode (z. B. 13F-HR/A nach 13F-HR) wird zuerst
    entfernt -- nur die neueste Fassung zaehlt. ticker bleibt NULL, wenn der
    CUSIP nicht zuordenbar ist (cusip bleibt gespeichert)."""
    n = 0
    con.execute("DELETE FROM smart_money_positions WHERE fonds=? AND periode=? AND accession<>?",
                (fonds, periode, accession))
    for r in rows:
        raw = cusip2ticker.get(r["cusip"])
        ticker = merge_tickers(raw, cfg) if raw else None
        anteil = (r["wert"] / portfolio_total) if portfolio_total > 0 and r["wert"] == r["wert"] else None
        con.execute(
            """INSERT INTO smart_money_positions
               (fonds, periode, report_date, accession, filing_datum, cusip, ticker, name, title_of_class,
                shares, wert, anteil_portfolio, aktion, aenderung_pct, positions_gesamt, rotation_pct,
                geladen_am)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fonds, periode, cusip, accession) DO UPDATE SET
                 report_date=excluded.report_date,
                 filing_datum=excluded.filing_datum, ticker=excluded.ticker, name=excluded.name,
                 title_of_class=excluded.title_of_class, shares=excluded.shares,
                 wert=excluded.wert, anteil_portfolio=excluded.anteil_portfolio, aktion=excluded.aktion,
                 aenderung_pct=excluded.aenderung_pct, positions_gesamt=excluded.positions_gesamt,
                 rotation_pct=excluded.rotation_pct, geladen_am=excluded.geladen_am""",
            (fonds, periode, report_date, accession, filing_datum, r["cusip"], ticker,
             r.get("name"), r.get("title"), r["shares"],
             r["wert"], anteil, r["aktion"], r["aenderung_pct"], len(by_ticker_keys), None,
             datetime.now().isoformat(timespec="seconds")))
        n += 1
    # rotation_pct = Wert der veraenderten Positionen / Portfoliowert Vorquartal
    moved = sum(r["wert"] for r in rows if r["aktion"] in ("neu", "verkauft", "erhoeht", "reduziert")
                and r["wert"] == r["wert"])
    rot = (moved / prev_total) if prev_total and prev_total > 0 else None
    if rot is not None:
        con.execute("UPDATE smart_money_positions SET rotation_pct=? WHERE fonds=? AND periode=?",
                    (rot, fonds, periode))
    con.execute("DELETE FROM smart_money_options WHERE fonds=? AND periode=?", (fonds, periode))
    for o in options:
        raw = cusip2ticker.get(o["cusip"])
        ticker = merge_tickers(raw, cfg) if raw else None
        con.execute(
            """INSERT INTO smart_money_options (fonds, periode, filing_datum, ticker, put_call, shares, wert, geladen_am)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(fonds, periode, ticker, put_call) DO UPDATE SET
                 filing_datum=excluded.filing_datum, shares=excluded.shares, wert=excluded.wert,
                 geladen_am=excluded.geladen_am""",
            (fonds, periode, filing_datum, ticker, o["put_call"], o["shares"], o.get("value", o.get("wert")),
             datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return n


def periode_add(periode: str, k: int) -> str:
    """'2026-Q2' + k Quartale (k auch negativ). '2026-Q1' - 1 -> '2025-Q4'."""
    y, q = periode.split("-Q")
    t = int(y) * 4 + int(q) - 1 + k
    return f"{t // 4}-Q{t % 4 + 1}"


def expected_periodes(newest: str, n: int) -> list[str]:
    """n aufeinanderfolgende Perioden endend bei `newest`, aufsteigend."""
    if not newest:
        return []
    return [periode_add(newest, -i) for i in range(n - 1, -1, -1)]


def completeness_check(con: sqlite3.Connection, fonds: str, newest_periode: str,
                       quarters: int = 8) -> dict:
    """Vollstaendigkeitspruefung je Fonds -- vorhandene Perioden, fehlende
    Perioden (Luecken in den letzten `quarters` Perioden ab newest_periode),
    Anzahl Positionen je Periode, Warnung bei Luecken."""
    rows = con.execute(
        "SELECT periode, COUNT(*) AS n FROM smart_money_positions WHERE fonds=? "
        "GROUP BY periode ORDER BY periode", (fonds,)).fetchall()
    periodes = [r[0] for r in rows]
    counts = {r[0]: r[1] for r in rows}
    erwartet = expected_periodes(newest_periode, quarters)
    fehlend = [p for p in erwartet if p not in periodes]
    return {"fonds": fonds, "perioden": periodes, "positionen": counts,
            "erwartet": erwartet, "fehlend": fehlend, "warnung": bool(fehlend)}


# Vollstaendigkeitsschutz fuer score/overlap
PLAUSIBILITY_MAX_AGE_TAGE = 140   # neueste Periode aelter -> Abbruch
PLAUSIBILITY_LETZTE_PERIODEN = 4  # Luecke in den letzten 4 Perioden -> Abbruch


def plausibility_report(con: sqlite3.Connection, cfg: dict, heute: date | None = None,
                        max_age_tage: int = PLAUSIBILITY_MAX_AGE_TAGE,
                        letzte: int = PLAUSIBILITY_LETZTE_PERIODEN) -> list[dict]:
    """Je Fonds (config smart_money.funds): neueste Periode in der DB, deren
    report_date/Alter, fehlende Perioden in den letzten `letzte` Perioden und ok.
    Die neueste Periode ist die Periode, die in die Berechnung einging."""
    heute = heute or date.today()
    out = []
    for f in cfg.get("smart_money", {}).get("funds", []):
        name = f.get("name", "")
        rows = con.execute(
            "SELECT periode, report_date, filing_datum, COUNT(*) AS n FROM smart_money_positions "
            "WHERE fonds=? GROUP BY periode ORDER BY periode DESC", (name,)).fetchall()
        if not rows:
            out.append({"fonds": name, "neueste": None, "report_date": None, "alter_tage": None,
                        "fehlend_letzte": "alle", "ok": False, "positionen": 0,
                        "grund": "keine Positionen in smart_money_positions"})
            continue
        neueste, rd, fd, _n = rows[0]
        basis_datum = (rd or fd or "")[:10]
        try:
            alter = (heute - date.fromisoformat(basis_datum)).days if basis_datum else None
        except ValueError:
            alter = None
        present = {r[0] for r in rows}
        fehlend = [p for p in expected_periodes(neueste, letzte) if p not in present]
        ok = (alter is None or alter <= max_age_tage) and not fehlend
        gruende = []  # expliziter Grund je Fonds fuer die Abbruchmeldung
        if alter is not None and alter > max_age_tage:
            gruende.append(f"neueste Periode {neueste} ist {alter} Tage alt (> {max_age_tage} Tage)")
        if fehlend:
            gruende.append(f"Periodenluecke in den letzten {letzte} Perioden: {', '.join(fehlend)}")
        out.append({"fonds": name, "neueste": neueste, "report_date": rd, "alter_tage": alter,
                    "fehlend_letzte": fehlend, "ok": ok, "grund": "; ".join(gruende),
                    "positionen": sum(r[3] for r in rows)})
    return out


def plausibility_lines(rep: list[dict]) -> list[str]:
    """Ausgabe je Fonds -- nennt die Periode, die in die Berechnung einging."""
    lines = []
    for r in rep:
        alt = f"{r['alter_tage']} Tage alt" if r["alter_tage"] is not None else "Datum unbekannt"
        fehl = r["fehlend_letzte"]
        fehl_txt = fehl if isinstance(fehl, str) else (", ".join(fehl) or "-")
        lines.append(f"  {r['fonds']}: neueste Periode {r['neueste'] or '-'} "
                     f"(report_date {r['report_date'] or '-'}, {alt}) | fehlend letzte "
                     f"{PLAUSIBILITY_LETZTE_PERIODEN}: {fehl_txt} | "
                     f"{'OK' if r['ok'] else 'PROBLEM'} | positionen={r['positionen']}")
    return lines


def check_plausibility(con: sqlite3.Connection, cfg: dict, force: bool = False,
                       heute: date | None = None) -> list[dict]:
    """score/overlap brechen ab, wenn die neueste Periode eines Fonds aelter
    als 140 Tage ist oder in den letzten 4 Perioden eine fehlt. `--force` ueberstimmt
    ausschliesslich DIESE Pruefung -- Schema-Guard (RuntimeError) und User-Agent-Pruefung
    (UserAgentError) schlagen weiterhin hart durch. Ohne --force: SystemExit, je
    blockiertem Fonds mit Periode + Grund, plus explizitem Ausweg --force (kein
    Silent-Continue; eine vorhandene Ergebnis-CSV wird nicht angefasst -- geschrieben
    wird erst am Ende des Laufs). Mit --force: Lauf normal, aber je blockiertem Fonds
    eine Warnzeile im Log (Fonds, neueste Periode, Alter in Tagen, fehlende Perioden).
    Rueckgabe: Report je Fonds."""
    rep = plausibility_report(con, cfg, heute=heute)
    bad = [r for r in rep if not r["ok"]]
    if bad and force:
        for r in bad:
            fehl = r["fehlend_letzte"]
            fehl_txt = fehl if isinstance(fehl, str) else (", ".join(fehl) or "-")
            alt = r["alter_tage"] if r["alter_tage"] is not None else "?"
            logger.warning("smart_money: --force aktiv -- Fonds %s fliesst mit Luecke ein: "
                           "neueste Periode %s (%s Tage alt), fehlend letzte %d: %s | Grund: %s",
                           r["fonds"], r["neueste"] or "-", alt, PLAUSIBILITY_LETZTE_PERIODEN,
                           fehl_txt, r.get("grund") or "-")
    if bad and not force:
        msg = ["Abbruch Vollstaendigkeitsschutz (v49) -- blockierte Fonds:"]
        for r in bad:
            alt = f"{r['alter_tage']} Tage alt" if r["alter_tage"] is not None else "Datum unbekannt"
            fehl = r["fehlend_letzte"]
            fehl_txt = fehl if isinstance(fehl, str) else (", ".join(fehl) or "-")
            msg.append(f"  {r['fonds']}: neueste Periode {r['neueste'] or '-'} "
                       f"(report_date {r['report_date'] or '-'}, {alt}) | fehlend letzte "
                       f"{PLAUSIBILITY_LETZTE_PERIODEN}: {fehl_txt} | Grund: {r.get('grund') or '-'}")
        msg.append("Ausweg: --force setzt die Vollstaendigkeitspruefung fuer diesen Lauf aus "
                   "(blockierte Fonds fliessen mit Luecke ein, Warnzeile je Fonds im Log). "
                   "Schema-Guard und User-Agent-Pruefung bleiben davon unberuehrt.")
        raise SystemExit("\n".join(msg))
    return rep


def _nicht_aktienrelevant(cusip: str, title_of_class: str) -> bool:
    """Anleihen (BOND/NOTE/DEBENTURE in titleOfClass) und Nicht-US-Titel
    (CUSIP-Laendercode keine Ziffer) sind nicht aktienrelevant -- in der
    Mapping-Uebersicht als solche markiert, zaehlen nicht als Fehler."""
    toc = (title_of_class or "").upper()
    if any(k in toc for k in ("BOND", "NOTE", "DEBENTURE")):
        return True
    if cusip and not cusip[0].isdigit():
        return True
    return False


def mapping_coverage(con: sqlite3.Connection, fonds: str | None = None,
                     top_n: int = 20) -> list[dict]:
    """Mapping-Abdeckung je Fonds -- Anteil der Positionen NACH WERT mit
    Ticker (ticker IS NOT NULL; Ziel >= 95 % bei US-Aktien) plus die `top_n`
    groessten nicht zugeordneten Positionen (CUSIP, name, wert, title_of_class,
    Flag nicht_aktienrelevant fuer Anleihen/Optionen/Nicht-US)."""
    sql = ("SELECT fonds, SUM(wert) AS gesamt, "
           "SUM(CASE WHEN ticker IS NOT NULL THEN wert ELSE 0 END) AS mit_ticker "
           "FROM smart_money_positions")
    args: tuple = ()
    if fonds:
        sql += " WHERE fonds=?"
        args = (fonds,)
    sql += " GROUP BY fonds ORDER BY fonds"
    out = []
    for f, gesamt, mit in con.execute(sql, args).fetchall():
        gesamt = gesamt or 0.0
        mit = mit or 0.0
        top = con.execute(
            "SELECT cusip, name, wert, title_of_class FROM smart_money_positions "
            "WHERE fonds=? AND ticker IS NULL ORDER BY wert DESC LIMIT ?",
            (f, top_n)).fetchall()
        top_unmapped = [{"cusip": c, "name": n or "", "wert": w or 0.0,
                         "title_of_class": t or "",
                         "nicht_aktienrelevant": _nicht_aktienrelevant(c or "", t or "")}
                        for c, n, w, t in top]
        out.append({"fonds": f, "gesamt_wert": gesamt, "mit_ticker_wert": mit,
                    "anteil": (mit / gesamt) if gesamt > 0 else 0.0,
                    "top_unmapped": top_unmapped})
    return out


def fetch_all(cfg: dict, quarters: int | None = None, db_path: Path = DB_PATH,
              fetcher=None, report: list | None = None, sleeper=None,
              reset: bool = False) -> list[dict]:
    """Laedt die letzten `quarters` Perioden je Fonds (13F-HR, A-Fassung gewinnt),
    speichert Positionen/Optionen, sammelt 13D-Katalysatoren der Aktivisten.

    `reset=True` leert smart_money_positions vorab (cusip_map bleibt) und
    laedt die letzten Perioden neu; beide Tabellen (Positionen + Optionen)
    werden per DROP + CREATE neu angelegt. Im `report` je Fonds zusaetzlich
    "completeness" (Vollstaendigkeitspruefung) und "mapping" (Abdeckung nach
    Wert mit Ticker).

    Rueckgabe: Fehlerliste [{fonds, error}]. `report` (optional) bekommt die
    CIK-Aufloesung + Filer-Status [{name, cik, filer_status}] -- v46 ergaenzt um
    "filings" (letzte 8: form, report_date, filing_date, accession je Fonds).
    `sleeper` injizierbar fuer Tests (Retry-Wartezeiten ohne echtes Schlafen).
    """
    sm = cfg.get("smart_money", {})
    nq = int(quarters if quarters is not None else sm.get("quarters", 8))
    r = sm.get("retry_403_429", sm.get("retry_429", {}))
    _waits = r.get("wartezeiten_s") or ([r["warte_s"]] * int(r.get("versuche", 3)) if "warte_s" in r else None)
    errors: list[dict] = []
    # User-Agent aus smart_money.user_agent (Fallback sec.user_agent),
    # validiert -- bei ungueltigem Wert Abbruch VOR dem ersten Request
    http = SecHttp(user_agent_from_cfg(cfg),
                   float(sm.get("max_requests_per_second", 5.0)),
                   tuple(_waits or [2, 5, 10]), int(r.get("versuche", 3)),
                   fetcher=fetcher, errors=errors, sleeper=sleeper or time.sleep)
    con = connect(db_path)
    if reset:
        # fetch --reset -- BEIDE Tabellen per DROP TABLE IF EXISTS + CREATE
        # im aktuellen Schema neu anlegen (cusip_map bleibt erhalten), danach die
        # letzten Perioden neu laden
        con.execute("DROP TABLE IF EXISTS smart_money_positions")
        con.execute("DROP TABLE IF EXISTS smart_money_options")
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        con.commit()
        _assert_positions_schema(con)  # veraltete schema.sql -> RuntimeError vor dem ersten Fonds
        logger.info("smart_money: --reset -- smart_money_positions + smart_money_options per DROP+CREATE "
                    "neu angelegt (cusip_map bleibt)")
    unmapped: list[str] = []  # nicht zuordenbare CUSIPs -- einmal am Ende als Liste
    all_titles: dict[str, str] = {}   # cusip -> titleOfClass ( fuer nicht-aktienrelevant )
    option_cusips: set = set()        # CUSIPs der Optionen (zaehlen nicht als Fehler)
    ciks = resolve_all_ciks(cfg, http)
    if report is not None:
        report.extend(ciks)
    # Titel aus company_tickers.json fuer den nameOfIssuer-Fallback (einmalig)
    title_map = load_company_titles(http)
    funds = {f["name"]: f for f in sm.get("funds", [])}
    for entry in ciks:
        name, cik = entry["name"], entry["cik"]
        http.fonds = name  # HTTP-Fehler (403/429) im Fehlerlog dem Fonds zuordnen
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
            entry["filings"] = filings[-8:]  # letzte 8 Filings je Fonds im Bericht
            qdata: list[tuple[str, str, str, list, list]] = []
            for f in filings:
                http.accession = f["accession"]  # Accession im HTTP-Fehlerlog
                try:
                    rd, holdings, options = load_13f_quarter(cik, f["accession"], http)
                except Exception as exc:
                    logger.error("smart_money: %s (CIK %s) Accession %s: %s", name, cik,
                                 f["accession"], exc)
                    errors.append({"fonds": name, "accession": f["accession"],
                                   "error": str(exc)[:200]})
                    continue
                # Label-Datum AUSSCHLIESSLICH reportDate (submissions bevorzugt,
                # Fallback periodOfReport aus dem XML); filing_date nur fuer Sortierung
                qdata.append((f["report_date"] or rd, f["filing_date"], f["accession"], holdings, options))
            qdata.sort(key=lambda x: x[0] or x[1])
            qdata = qdata[-nq:]
            # CUSIP->Ticker fuer ALLE Perioden vorab (damit GOOG/GOOGL-Merging
            # VOR dem Vorquartals-Vergleich passiert: gleiche Aktie je Fonds)
            all_cusips = list({h["cusip"] for _rd, _fd, _ac, hl, _op in qdata for h in hl}
                              | {o["cusip"] for _rd, _fd, _ac, _hl, op in qdata for o in op})
            all_names = {h["cusip"]: h.get("name", "") for _rd, _fd, _ac, hl, op in qdata
                         for h in list(hl) + list(op)}
            all_titles = {h["cusip"]: h.get("title", "") for _rd, _fd, _ac, hl, op in qdata
                          for h in list(hl) + list(op)}
            option_cusips = {o["cusip"] for _rd, _fd, _ac, _hl, op in qdata for o in op}
            c2t = cusip_to_tickers(all_cusips, http, cfg, errors=errors, con=con, unmapped=unmapped,
                                   names=all_names, title_map=title_map)
            prev: dict[str, dict] = {}
            prev_total = None
            for report_date, filing_date, accession, holdings, options in qdata:
                q = quartal_of(report_date)  # Periode = Jahr-Qn exklusiv aus reportDate
                # nach Ticker mergen (GOOGL->GOOG): shares + wert summieren
                by_ticker: dict[str, dict] = {}
                for h in holdings:
                    t = merge_tickers(c2t.get(h["cusip"], h["cusip"]), cfg)
                    if t in by_ticker:
                        by_ticker[t]["shares"] += h["shares"]
                        by_ticker[t]["value"] += h["value"]
                        if not by_ticker[t].get("title"):
                            by_ticker[t]["title"] = h.get("title", "")
                    else:
                        by_ticker[t] = {"cusip": h["cusip"], "name": h["name"],
                                        "shares": h["shares"], "value": h["value"],
                                        "title": h.get("title", "")}
                rows = compare_quarters(prev, by_ticker)
                if not prev:
                    # aelteste geladene Periode je Fonds -> aktion "basis" (nie "neu");
                    # neu/erhoeht/reduziert/verkauft erst ab vorhandenem Vorquartal
                    for r in rows:
                        r["aktion"] = "basis"
                        r["aenderung_pct"] = None
                total = sum(h["value"] for h in by_ticker.values() if h["value"] == h["value"])
                n = store_quarter(con, name, q, filing_date, rows, c2t, total, options, cfg, prev_total,
                                  by_ticker_keys=list(by_ticker), report_date=report_date,
                                  accession=accession)
                logger.info("smart_money: %s %s -> %d Positionen (%d Optionen)", name, q, n, len(options))
                prev = by_ticker
                prev_total = total
            # Vollstaendigkeitspruefung + Mapping-Abdeckung je Fonds (ab newester Periode)
            newest = max((quartal_of(f["report_date"] or "") for f in filings), default="")
            entry["completeness"] = completeness_check(con, name, newest, nq)
            cov = mapping_coverage(con, fonds=name)
            entry["mapping"] = cov[0] if cov else {"fonds": name, "gesamt_wert": 0.0,
                                                   "mit_ticker_wert": 0.0, "anteil": 0.0}
            # Aktivist: 13D/13D-A als Katalysator sammeln (gewicht 0)
            if fcfg.get("typ") == "aktivist" or float(gew) == 0.0:
                try:
                    # bei HTTP 503 3x Retry (5, 15, 30 s); Fehlschlag blockiert nicht
                    for d in _retry_503(lambda: list_13d_filings(cik, http), http):
                        cusip, acc = _retry_503(lambda: load_13d_target(cik, d["accession"], http), http)
                        t = merge_tickers(cusip_to_tickers([cusip], http, cfg, con=con, unmapped=unmapped).get(cusip, cusip), cfg) if cusip else ""
                        if t:
                            con.execute(
                                """INSERT INTO smart_money_katalysator (ticker, fonds, datum, quelle_url, form)
                                   VALUES (?,?,?,?,?)""",
                                (t, name, d["filing_date"],
                                 f"{_sec_base()}/Archives/edgar/data/{str(int(cik.lstrip('0') or '0'))}/{_accession_digits(d['accession'])}/",
                                 d["form"]))
                    con.commit()
                except Exception as exc:
                    logger.warning("smart_money: 13D-Katalysatoren %s fehlgeschlagen: %s", name, exc)
        except Exception as exc:
            logger.error("smart_money: Fehler bei %s (CIK %s): %s", name, cik, exc)
            errors.append({"fonds": name, "error": str(exc)[:200]})
    # nicht zuordenbare CUSIPs einmal am Ende als Liste; Anleihen, Optionen
    # und Nicht-US-Titel zaehlen nicht als Fehler (nicht aktienrelevant)
    uniq_unmapped = list(dict.fromkeys(unmapped))
    aktien_unmapped = [c for c in uniq_unmapped
                       if c not in option_cusips
                       and not _nicht_aktienrelevant(c, all_titles.get(c, ""))]
    if aktien_unmapped:
        errors.append({"cusip": ", ".join(aktien_unmapped),
                       "error": f"{len(aktien_unmapped)} aktienrelevante CUSIP(s) nicht zuordenbar "
                                "(OpenFIGI/name_match)"})
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
        "SELECT * FROM smart_money_positions WHERE ticker=? ORDER BY periode, fonds",
        (ticker.upper(),))]


def rationale_exists(con: sqlite3.Connection, ticker: str) -> bool:
    r = con.execute("SELECT COUNT(*) AS n FROM smart_money_rationale WHERE ticker=?",
                    (ticker.upper(),)).fetchone()
    return bool(r and r[0] > 0)


def rationale_quelle(con: sqlite3.Connection, ticker: str) -> str:
    """neueste quelle_url des Tickers, auf 40 Zeichen gekuerzt (Anzeige in
    score/overlap als rationale_quelle -- Nachvollziehbarkeit ohne Blick in die DB)."""
    r = con.execute(
        "SELECT quelle_url FROM smart_money_rationale WHERE ticker=? ORDER BY datum DESC, id DESC LIMIT 1",
        (ticker.upper(),)).fetchone()
    return ((r[0] or "")[:40]) if r else ""


def score4_kandidaten(con: sqlite3.Connection, cfg: dict) -> list[dict]:
    """(Ticker, Fonds)-Kombinationen, die aktuell fuer Score 4 in Frage kommen --
    Score-3-Bedingung (config smart_money.rules) in den letzten ZWEI aufeinanderfolgenden
    Quartalen, aus der DB gelesen. Fonds = Kaeufer (neu/erhoeht, Gewicht > 0) des
    neuesten Quartals. Rueckgabe: Zeilen fuer die Import-Vorlage (text/quelle_url leer)."""
    rules = cfg.get("smart_money", {}).get("rules", {})
    gew_min = float(rules.get("gewicht_summe_min", 2.0))
    anteil_g1_min = float(rules.get("anteil_gewicht1_min", 0.05))
    tickers = [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM smart_money_positions WHERE ticker IS NOT NULL ORDER BY ticker")]
    out: list[dict] = []
    for t in tickers:
        rows = _rows_for(con, t)
        quarters = sorted({r["periode"] for r in rows})
        if len(quarters) < 2 or quarters[-2] != periode_add(quarters[-1], -1):
            continue  # letzte zwei Quartale nicht aufeinanderfolgend -> kein Score 4
        last_q, prev_q = quarters[-1], quarters[-2]

        def _cond3(q: str) -> tuple[set, bool]:
            last = [r for r in rows if r["periode"] == q and r["aktion"] != "basis"]
            kaeufer = {r["fonds"] for r in last
                       if r["aktion"] in ("neu", "erhoeht") and _fonds_gewicht(cfg, r["fonds"]) > 0}
            gew = sum(_fonds_gewicht(cfg, f) for f in kaeufer)
            g1 = [r["anteil_portfolio"] or 0.0 for r in last
                  if r["fonds"] in kaeufer and _fonds_gewicht(cfg, r["fonds"]) == 1.0]
            return kaeufer, (gew >= gew_min or (max(g1) if g1 else 0.0) >= anteil_g1_min)

        kaeufer_last, cond_last = _cond3(last_q)
        _prev_kaeufer, cond_prev = _cond3(prev_q)
        if cond_last and cond_prev:
            for f in sorted(kaeufer_last):
                out.append({"ticker": t, "fonds": f, "text": "", "quelle_url": "", "datum": "", "typ": ""})
    return out


def write_rationale_template(con: sqlite3.Connection, cfg: dict, out_path: str) -> int:
    """Import-CSV-Vorlage schreiben -- Spalten ticker,fonds,text,quelle_url,datum,typ,
    je eine Zeile pro Score-4-Kandidat (ticker, fonds). Pflichtfelder (ticker, fonds, text,
    quelle_url) sind in der Vorlage leer markiert (Kommentar-Zeile oben); datum/typ optional
    (Default beim Import: heute, Sonstiges). Rueckgabe: Anzahl Zeilen."""
    rows = score4_kandidaten(con, cfg)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        f.write("# Vorlage -- Pflichtfelder: ticker, fonds, text, quelle_url. "
                "text/quelle_url leer lassen, bis eine echte Quelle (Interview/Brief/13D) eingetragen ist "
                "-- der Import bricht sonst mit Zeilennummer ab. datum/typ optional (Default: heute, Sonstiges).\n")
        w = csv.writer(f)
        w.writerow(["ticker", "fonds", "text", "quelle_url", "datum", "typ"])
        for r in rows:
            w.writerow([r["ticker"], r["fonds"], r["text"], r["quelle_url"], r["datum"], r["typ"]])
    return len(rows)


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
    filing_datum = max((r["filing_datum"] or "") for r in rows) if rows else ""
    try:
        daten_alter = (heute - date.fromisoformat(filing_datum[:10])).days if filing_datum else None
    except ValueError:
        daten_alter = None
    # ETF/Index (config smart_money.etf_ticker oder cusip_map.is_etf) wird
    # NICHT gescored -- Score 1, keine Kaeufer/Verkaeufer, keine letzte_aktion;
    # filing_datum/daten_alter_tage bleiben echt (Transparenz, store_quarter speichert weiter)
    if is_etf_ticker(con, ticker, cfg):
        return {"score": 1, "fonds_kaeufer": [], "fonds_verkaeufer": [],
                "letzte_aktion": "", "filing_datum": filing_datum,
                "daten_alter_tage": daten_alter, "rationale_vorhanden": False,
                "rationale_quelle": "", "katalysator_13d": ""}
    empty = {"score": 1, "fonds_kaeufer": [], "fonds_verkaeufer": [],
             "letzte_aktion": "", "filing_datum": "", "daten_alter_tage": None,
             "rationale_vorhanden": False, "rationale_quelle": "", "katalysator_13d": ""}
    if not rows:
        return empty

    quarters = sorted({r["periode"] for r in rows})
    last_q = quarters[-1]
    last = [r for r in rows if r["periode"] == last_q and r["aktion"] != "basis"]  # basis ignorieren

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
    rat = rationale_exists(con, ticker)
    rat_quelle = rationale_quelle(con, ticker) if rat else ""   # Anzeige in score/overlap
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
        prev = [r for r in rows if r["periode"] == prev_q and r["aktion"] != "basis"]  # v49
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
            "rationale_quelle": rat_quelle, "katalysator_13d": kat_str}


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
    """Watchlist-CSV laden; BOM (utf-8-sig) und Leerzeichen werden getrimmt,
    leere Zeilen und #-Kommentare ignoriert, Ticker uppercased."""
    p = Path(path) if path else Path("ticker_watch_v40.csv")
    if not p.exists():
        return []
    from data.tickerlist import parse_ticker_csv
    raw = p.read_bytes().decode("utf-8-sig")  # BOM entfernen
    out: list[str] = []
    for t in parse_ticker_csv(raw):
        t = (t or "").strip().lstrip("\ufeff").upper()
        if t and not t.startswith("#") and t not in out:
            out.append(t)
    return out


def overlap(ranking_csv: str, watch_csv: str | None, cfg: dict,
            db_path: Path = DB_PATH, out_path: str = "smart_money_overlap.csv",
            force: bool = False, heute: date | None = None) -> list[dict]:
    """Abgleich Screener x Smart Money; schreibt smart_money_overlap.csv.

    Ticker: alle Watchlist-Ticker + alle Ticker mit Fonds-Aktivitaet.
    Vollstaendigkeitsschutz -- Abbruch mit klarer Meldung, wenn die neueste
    Periode eines Fonds aelter als 140 Tage ist oder in den letzten 4 Perioden
    eine fehlt (--force ueberstimmt). Die Ergebnis-CSV wird erst am
    Ende geschrieben -- bricht der Guard ab (ohne --force), bleibt eine vorhandene
    Datei unveraendert; die Spalten bleiben mit --force unveraendert."""
    rows_in = load_ranking_csv(ranking_csv)
    by_t = {(r.get("ticker") or "").strip().upper(): r for r in rows_in}
    watch = load_watch_csv(watch_csv)
    con = connect(db_path)
    check_plausibility(con, cfg, force=force, heute=heute)  # Abbruch bei stale/lueckig
    con.row_factory = sqlite3.Row
    aktive = {r["ticker"] for r in con.execute(
        "SELECT DISTINCT ticker FROM smart_money_positions WHERE ticker IS NOT NULL")}
    tickers = list(dict.fromkeys(watch + sorted(aktive)))
    out = []
    for t in tickers:
        row = by_t.get(t, {"ticker": t, "status_all": "", "quality": "", "mos_rule": ""})
        sm = smart_money_score(t, cfg, db_path=db_path)
        # ETF/Index-Zeile bleibt in der CSV (Abdeckung nachvollziehbar), aber
        # mit Score 1, Urteil neutral und Flag etf=ja (neue Spalte am Ende)
        etf_flag = is_etf_ticker(con, t, cfg)
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
            "smart_money_urteil": "neutral" if etf_flag else urteil(row, sm),
            "etf": "ja" if etf_flag else "",
            # Quelle (erste 40 Zeichen der quelle_url) bei rationale_vorhanden=ja, neue Spalte am Ende
            "rationale_quelle": sm.get("rationale_quelle", "") if sm["rationale_vorhanden"] else "",
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

def print_fetch_report(report: list[dict], errors: list[dict]) -> None:
    """Ausgabe des fetch-Laufs: CIK/Filer-Status, Filings je Fonds, danach
    Vollstaendigkeitspruefung (Perioden, fehlende, Positionen, Warnung) und
    Mapping-Abdeckung je Fonds (Anteil nach Wert mit Ticker), dann Fehlerlog."""
    print("\n=== CIK-Aufloesung + Filer-Status (v44: config -> Firmensuche -> manuell) ===")
    for e in report:
        print(f"  {e['name']:<38} cik={e['cik'] or '-':<12} {e['filer_status']}"
              + (f" | EDGAR-Name: {e.get('bericht_name') or '-'}"
                 f" | letztes 13F-HR: {e.get('last_13f_hr') or '-'}" if e.get("cik") else ""))
        for t in (e.get("treffer") or []):
            print(f"      Treffer: cik={t['cik']} name={t['name']!r} letztes 13F-HR={t['last_13f_hr']}")
        # letzte 8 Filings je Fonds mit Label (aus reportDate), form, reportDate, filingDate, accession
        for f in (e.get("filings") or [])[-8:]:
            label = quartal_of(f["report_date"])  # Periode exklusiv aus reportDate
            print(f"      Filing: {label:<9} {f['form']:<9} report={f['report_date'] or '-'}"
                  f" filing={f['filing_date'] or '-'} acc={f['accession']}")
        # Vollstaendigkeitspruefung je Fonds (Perioden, fehlende, Positionen, Warnung)
        c = e.get("completeness") or {}
        if c:
            pos = ", ".join(f"{p}={n}" for p, n in sorted(c.get("positionen", {}).items()))
            fehl = ", ".join(c.get("fehlend") or []) or "-"
            warn = " WARNUNG: Luecken!" if c.get("warnung") else ""
            print(f"      Perioden: {', '.join(c.get('perioden') or []) or '-'}"
                  f" | fehlend: {fehl} | Positionen: {pos or '-'}{warn}")
        # Mapping-Abdeckung je Fonds (Anteil der Positionen nach Wert mit Ticker)
        # + die 20 groessten nicht zugeordneten Positionen
        m = e.get("mapping") or {}
        if m:
            print(f"      Mapping: {m.get('anteil', 0.0):.1%} nach Wert mit Ticker"
                  f" ({m.get('mit_ticker_wert', 0.0):,.0f} / {m.get('gesamt_wert', 0.0):,.0f} USD)")
            for u in (m.get("top_unmapped") or [])[:20]:
                flag = " [nicht aktienrelevant]" if u.get("nicht_aktienrelevant") else ""
                print(f"        nicht zugeordnet: cusip={u.get('cusip')} name={u.get('name') or '-'} "
                      f"wert={u.get('wert') or 0.0:,.0f} titleOfClass={u.get('title_of_class') or '-'}{flag}")
    if errors:
        print(f"\nFehlerlog ({len(errors)}):")
        for e in errors:
            label = e.get('fonds') or e.get('cusip') or e.get('url') or '-'
            acc = f" acc={e['accession']}" if e.get('accession') else ""
            print(f"  {label}{acc}: {e['error'][:120]}")
    else:
        print("\nKeine Fehler -- alle Fonds aktualisiert.")


def cmd_fetch(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    report: list[dict] = []
    errors = fetch_all(cfg, quarters=args.quarters, db_path=Path(args.db), report=report,
                       reset=bool(getattr(args, "reset", False)))
    print_fetch_report(report, errors)


def cmd_score(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if args.ranking:
        rows = load_ranking_csv(args.ranking)
        tickers = [(r.get("ticker") or "").strip().upper() for r in rows]
    else:
        tickers = args.tickers
    # Vollstaendigkeitsschutz -- nennt je Fonds die Periode, die in die Berechnung einging
    con = connect(Path(args.db))
    rep = check_plausibility(con, cfg, force=bool(getattr(args, "force", False)))
    print("Plausibilitaet je Fonds (neueste Periode geht in die Berechnung ein):")
    for line in plausibility_lines(rep):
        print(line)
    print(f"{'ticker':<8} {'score':>5} {'kaeufer':<30} {'verkaeufer':<20} {'alter_t':>7}  letzte_aktion / filing / katalysator / rationale")
    for t in tickers:
        s = smart_money_score(t, cfg, db_path=Path(args.db))
        rat = f"ja {s['rationale_quelle']}" if s["rationale_vorhanden"] else "nein"  # 
        print(f"{t:<8} {s['score']:>5} {','.join(s['fonds_kaeufer']) or '-':<30} "
              f"{','.join(s['fonds_verkaeufer']) or '-':<20} "
              f"{str(s['daten_alter_tage'] if s['daten_alter_tage'] is not None else '-'):>7}  "
              f"{s['letzte_aktion'] or '-'} | {s['filing_datum'] or '-'} | {s['katalysator_13d'] or '-'} | {rat}")


def cmd_overlap(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    out = overlap(args.ranking, args.watch, cfg, db_path=Path(args.db), out_path=args.out,
                  force=bool(getattr(args, "force", False)))
    con = connect(Path(args.db))
    rep = plausibility_report(con, cfg)
    print("Plausibilitaet je Fonds (neueste Periode geht in die Berechnung ein):")
    for line in plausibility_lines(rep):
        print(line)
    print(f"\n=== smart_money_overlap ({len(out)} Ticker) -> {args.out} ===")
    print(f"{'ticker':<8} {'status_all':<28} {'score':>5} {'urteil':<14} kaeufer / verkaeufer")
    for r in out:
        print(f"{r['ticker']:<8} {r['status_all'][:28]:<28} {r['smart_money_score']:>5} "
              f"{r['smart_money_urteil']:<14} {r['fonds_kaeufer'] or '-'} / {r['fonds_verkaeufer'] or '-'}")
    # Watch-Ticker ohne Fonds-Treffer ausdruecklich auflisten (nicht weglassen)
    aktive = {r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM smart_money_positions WHERE ticker IS NOT NULL")}
    watch = load_watch_csv(args.watch)
    ohne = [t for t in watch if t not in aktive]
    print(f"\nWatch-Ticker ohne Fonds-Treffer ({len(ohne)}): {', '.join(ohne) or '-'}")


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
    # erst ALLE Zeilen validieren -- Pflichtfelder text + quelle_url; bei Fehlern
    # Abbruch mit kompletter Fehlerliste (Zeilennummer = Dateizeile inkl. Header, fehlendes
    # Feld), es wird NICHTS importiert (kein Teil-Import). datum/typ optional
    # (Default: heute, Sonstiges). #-Kommentarzeilen (z. B. aus rationale-template) werden ignoriert.
    rows: list[tuple] = []
    fehler: list[tuple[int, str, list[str]]] = []
    with open(args.csv_file, newline="", encoding="utf-8") as f:
        for zeile, row in enumerate(csv.DictReader(f), start=2):  # Zeile 1 = Header
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker or ticker.startswith("#"):
                continue
            text = (row.get("text") or "").strip()
            quelle = (row.get("quelle_url") or "").strip()
            fehlend = [k for k, v in (("text", text), ("quelle_url", quelle)) if not v]
            if fehlend:
                fehler.append((zeile, ticker, fehlend))
                continue
            rows.append((ticker, (row.get("fonds") or "").strip() or None, text, quelle,
                         (row.get("datum") or "").strip() or date.today().isoformat(),
                         (row.get("typ") or "").strip() or "Sonstiges"))
    if fehler:
        msg = ["Abbruch rationale-import -- Pflichtfeld fehlt (Zeile = Dateizeile inkl. Header):"]
        for zeile, ticker, fehlend in fehler:
            msg.append(f"  Zeile {zeile}: ticker={ticker} -- fehlt: {', '.join(fehlend)}")
        msg.append("Nichts importiert. text und quelle_url in jeder Zeile ausfuellen "
                   "(echte Quelle: Interview/Brief/13D) -- siehe rationale-template.")
        raise SystemExit("\n".join(msg))
    for ticker, fonds, text, quelle, datum, typ in rows:
        # Duplikat (ticker, fonds, quelle_url) ueberschreiben (INSERT OR REPLACE
        # greift ueber idx_smr_unique), nicht doppelt anlegen
        con.execute(
            """INSERT OR REPLACE INTO smart_money_rationale (ticker, fonds, text, quelle_url, datum, typ)
               VALUES (?,?,?,?,?,?)""",
            (ticker, fonds, text, quelle, datum, typ))
    con.commit()
    print(f"{len(rows)} Rationale importiert (aus {args.csv_file}; "
          "Duplikate (ticker, fonds, quelle_url) ueberschrieben).")


def cmd_rationale_template(args) -> None:
    """Import-Vorlage schreiben -- je (Ticker, Fonds)-Kombination, die aktuell
    fuer Score 4 in Frage kommt (Score-3-Bedingung in 2 aufeinanderfolgenden Quartalen)."""
    cfg = load_config()
    con = connect(Path(args.db))
    n = write_rationale_template(con, cfg, args.out)
    con.close()
    print(f"{n} Vorlage-Zeile(n) -> {args.out}")
    print("Score-4-Kandidaten = Score-3-Bedingung in den letzten 2 aufeinanderfolgenden Quartalen. "
          "Pflichtfelder text + quelle_url pro Zeile ausfuellen (echte Quelle), dann: "
          "python smart_money.py rationale-import")


def cmd_show(args) -> None:
    con = connect(Path(args.db))
    con.row_factory = sqlite3.Row
    if args.fonds:
        # Perioden + Anzahl der Positionen je Periode eines Fonds
        rows = con.execute(
            "SELECT periode, COUNT(*) AS n, MAX(filing_datum) AS filing_datum "
            "FROM smart_money_positions WHERE fonds=? GROUP BY periode ORDER BY periode DESC",
            (args.fonds,)).fetchall()
        print(f"=== smart_money_positions fonds={args.fonds} ({len(rows)} Perioden) ===")
        for r in rows:
            print(f"{r['periode']}  positionen={r['n']}  filing={r['filing_datum'] or '-'}")
        return
    if not args.ticker:
        raise SystemExit("show: ticker oder --fonds angeben")
    rows = con.execute(
        "SELECT * FROM smart_money_positions WHERE ticker=? ORDER BY periode DESC, fonds",
        (args.ticker.upper(),)).fetchall()
    print(f"=== smart_money_positions {args.ticker} ({len(rows)}) ===")
    for r in rows:
        print(f"{r['periode']} {r['fonds']:<30} {r['aktion']:<14} shares={r['shares'] or 0:>12,.0f} "
              f"anteil={r['anteil_portfolio'] or 0:.2%} aend="
              f"{r['aenderung_pct'] if r['aenderung_pct'] is not None else float('nan'):.1%} filing={r['filing_datum']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smart Money: 13F-HR der Fonds-Liste (SEC EDGAR)")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite-Datei (Default: value_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_f = sub.add_parser("fetch", help="13F-HR laden (letzte N Perioden je Fonds)")
    p_f.add_argument("--quarters", type=int, default=None, help="Default: config smart_money.quarters (8)")
    p_f.add_argument("--reset", action="store_true",
                     help="v48: smart_money_positions leeren (cusip_map bleibt) und die letzten "
                          "Perioden je Fonds neu laden")
    p_f.set_defaults(func=cmd_fetch)
    p_s = sub.add_parser("score", help="smart_money_score je Ticker (oder --ranking CSV)")
    p_s.add_argument("tickers", nargs="*")
    p_s.add_argument("--ranking", default=None, help="ranking CSV (alle Ticker daraus)")
    p_s.add_argument("--force", action="store_true",
                     help="NUR den Vollstaendigkeitsschutz ueberstimmen (stale/lueckige "
                          "Perioden); Schema-Guard und User-Agent-Pruefung bleiben unberuehrt")
    p_s.set_defaults(func=cmd_score)
    p_o = sub.add_parser("overlap", help="Abgleich Screener x Smart Money -> smart_money_overlap.csv")
    p_o.add_argument("--ranking", required=True, help="ranking CSV (status_all, quality, mos_*)")
    p_o.add_argument("--watch", default=None, help="Watchlist-CSV (Default: ticker_watch_v40.csv falls vorhanden)")
    p_o.add_argument("--out", default="smart_money_overlap.csv")
    p_o.add_argument("--force", action="store_true",
                     help="NUR den Vollstaendigkeitsschutz ueberstimmen (stale/lueckige "
                          "Perioden); Schema-Guard und User-Agent-Pruefung bleiben unberuehrt")
    p_o.set_defaults(func=cmd_overlap)
    p_c = sub.add_parser("ciks", help="CIK-Aufloesung + 13F-Filer-Status aller Fonds")
    p_c.set_defaults(func=cmd_ciks)
    p_r = sub.add_parser("rationale-import", help="Rationale aus CSV importieren (Pflicht text + quelle_url; Abbruch mit Fehlerliste bei Luecken)")
    p_r.add_argument("csv_file", nargs="?", default=None,
                     help="Default: config smart_money.rationale_csv (rationale_import.csv)")
    p_r.set_defaults(func=cmd_rationale_import)
    p_rt = sub.add_parser("rationale-template",
                          help="Import-Vorlage mit Score-4-Kandidaten schreiben")
    p_rt.add_argument("--out", default=None,
                      help="Default: config smart_money.rationale_csv (rationale_import.csv)")
    p_rt.set_defaults(func=cmd_rationale_template)
    p_sh = sub.add_parser("show", help="Positionen eines Tickers oder Perioden je Fonds (--fonds)")
    p_sh.add_argument("ticker", nargs="?", default=None)
    p_sh.add_argument("--fonds", default=None,
                      help="Perioden + Anzahl der Positionen je Quartal dieses Fonds")
    p_sh.set_defaults(func=cmd_show)
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    if getattr(args, "cmd", None) in ("rationale-import", "rationale-template"):
        cfg = load_config()
        if getattr(args, "cmd") == "rationale-import" and not getattr(args, "csv_file", None):
            args.csv_file = cfg.get("smart_money", {}).get("rationale_csv", "rationale_import.csv")
        if getattr(args, "cmd") == "rationale-template" and not getattr(args, "out", None):
            args.out = cfg.get("smart_money", {}).get("rationale_csv", "rationale_import.csv")
    args.func(args)


if __name__ == "__main__":
    main()
