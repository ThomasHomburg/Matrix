"""Smoke-Test: evaluate_all mit Demo-Daten fuer ALLE Ticker.

Regression: AttributeError "'DcfResult' object has no attribute 'fcf_basis'"
in scoring/pipeline.py (aufgerufen aus evaluate_ticker) trat erst zur
LAUFZEIT auf -- `import app` allein reicht nicht. Dieser Test fuehrt die
gesamte Pipeline aus und prueft, dass keine Ausnahme auftritt und jede
Zeile alle Spalten enthaelt, die app.py anzeigt.
"""

import math

from config import load_config
from data.demo import demo_companies
from scoring.pipeline import evaluate_all, evaluate_ticker

# Spalten, die app.py (Seite 1 Rangliste) aus jeder Row liest
APP_ROW_COLUMNS = {
    "ticker", "name", "sector", "country", "source",
    "score", "quality", "value", "coverage",
    "fcf_yield", "fcf_yield_sbc", "fcf_yield_3y",
    "roic_5y", "roic_5y_years", "roic_latest", "roic_latest_fy",
    "nd_ebitda", "ev_ebit_med",
    "margin_of_safety", "mos_bear", "mos_bull", "kandidat",
    "dcf_fcf_basis", "dcf_ttm_quarter_end", "dcf_fcf_sources",
    "dcf_net_debt", "dcf_net_debt_source", "dcf_customer_deposits",
    "dcf_fcf_mode", "ttm_plausible",
    "dcf_value_per_share_base", "dcf_value_per_share_bear",
    "dcf_value_per_share_bull",
    "is_buy", "flags",
    # MoS je Startwert-Modus + umschaltbare Nettoverschuldung
    "mos_avg3", "mos_latest", "mos_ttm", "mos_rule", "mos_rule_mode",
    "mos_base_alt_nd", "dcf_net_debt_mode", "dcf_net_debt_alt",
    "dcf_net_debt_alt_source",
    # SEC-EDGAR-Abgleich
    "sec_check_ok", "sec_check_flag", "sec_check_note",
    # reiner TTM-Start (ttm_only) + Zwei Stufen Kandidat/Beobachtung
    "mos_ttm_only", "status", "status_detail",
    # Sektorgruppe (Industry) + Sektor-Kennzahlen
    "sector_group", "sector_note", "industry",
    "normalized_earnings", "normalized_earnings_years",
    "price_to_book", "dividend_yield",
    # EBITDA-Plausibilitaet + Historie (Jahre mit Werten)
    "ebitda_unplausible", "history_years",
    # Status unter beiden Kandidatenregeln + aktive Regel
    "status_all", "status_all_detail", "status_recent2", "status_recent2_detail",
    "mos_recent2", "candidate_mode",
    # SBC-Pruefstatus + Start-FCF <= 0
    "dcf_sbc_missing", "status_pruefen_sbc", "dcf_fcf_le0", "value_na_below_coverage",
    # Sektor-Sonderregel + SEC-Detail + SBC-SEC-Quelle
    "sector_special", "sec_detail", "sbc_sec_sources", "data_quality",
    # MoS bei 10 % (robust), Capex-Pruefstatus, Bilanz-Konsistenz, Zykliker-Cap
    "mos_robust", "mos_robust_rate", "status_pruefen_capex", "capex_dev_years",
    "nd_inconsistent", "zyk_unnorm",
    # Quality-Abdeckung nur ueber Quality-Bloecke
    "quality_coverage",
    # Export-Felder fuer Tracker/Live-Update
    "kurs", "kurs_datum", "calc_date",
    "iv_bear", "iv_base", "iv_bull",
    "iv_avg3", "iv_latest", "iv_ttm", "iv_ttm_only", "iv_base_10pct",
    "naechster_earnings",
}

# Felder, die scoring/pipeline.py und app.py aus DcfResult lesen
DCF_RESULT_FIELDS = {
    "fcf_normalized", "fcf_years_used", "revenue_cagr",
    "discount_rate", "terminal_growth", "shares", "scenarios",
    "margin_of_safety", "is_buy", "fcf_normalized_conv",
    "fcf_basis", "ttm_quarter_end", "fcf_sources",
    "net_debt", "net_debt_source", "customer_deposits", "fcf_mode",
    # umschaltbare Nettoverschuldung
    "net_debt_mode", "net_debt_alt", "net_debt_alt_source", "mos_base_alt",
    # tatsaechliche Wachstumsraten je Szenario
    "scenario_growth",
}


# SEC-Abgleich wird zentral in tests/conftest.py deaktiviert (autouse),
# damit dieser Smoke-Test keine echten HTTP-Calls gegen data.sec.gov macht.


def test_dcf_result_hat_alle_felder():
    """DcfResult deklariert alle Felder, die Pipeline/UI lesen (kein
    Laufzeit-AttributeError wie 'fcf_basis')."""
    from valuation.dcf import DcfResult
    fields = set(DcfResult.__dataclass_fields__)
    missing = DCF_RESULT_FIELDS - fields
    assert not missing, f"DcfResult fehlen Felder: {sorted(missing)}"


def test_dcf_result_felder_werden_tatsaechlich_gelesen():
    """Jedes Attribut, das pipeline.py oder app.py von DcfResult liest,
    muss in der Klasse DEFINIERT sein -- dynamisch aus dem Quelltext
    ermittelt (statt handgepflegter Liste). Fängt Mischstaende wie
    'pipeline.py aus TTM-Branch, dcf.py aus aelterem Stand'."""
    import re
    from pathlib import Path

    from valuation.dcf import DcfResult

    root = Path(__file__).resolve().parents[1]
    fields = set(DcfResult.__dataclass_fields__)
    used = set()
    for fname in ("scoring/pipeline.py", "app.py"):
        src = (root / fname).read_text(encoding="utf-8")
        # dcf.<attr> und dcf_m.<attr> (app.py: Startwert-Modi-Schleife)
        used |= set(re.findall(r"\bdcf(?:_m)?\.(\w+)", src))
    # 'scenarios' ist ein dict -- .get(...) darauf ist ok; Rest muessen Felder sein
    # dc.get("robust_discount_rate") ist Config-Zugriff (cfg["dcf"]), kein DcfResult-Attribut
    missing = used - fields - {"scenarios", "robust_discount_rate"}
    assert not missing, (
        f"pipeline.py/app.py lesen DcfResult-Attribute, die nicht definiert "
        f"sind: {sorted(missing)} (Mischstand Branch/aelterer dcf.py?)"
    )


def test_evaluate_all_demo_alle_ticker_ohne_ausnahme(cfg):
    """evaluate_all ueber alle Demo-Ticker: keine Ausnahme, Rows vollstaendig."""
    companies = demo_companies()
    assert len(companies) >= 5  # AAPL, QUAL, LEVR, DIST, MICR, SIE, ...
    rows, excluded = evaluate_all(companies, cfg)
    assert rows, "keine Rows erzeugt"
    for row in rows:
        missing = APP_ROW_COLUMNS - set(row)
        assert not missing, f"{row.get('ticker')}: Row fehlen Spalten {sorted(missing)}"


def test_evaluate_ticker_pro_ticker_alle_dcf_felder(cfg):
    """Jeder nicht ausgeschlossene Demo-Ticker: evaluate_ticker liefert
    out['dcf'] mit allen DcfResult-Feldern, die Pipeline/app lesen
    (fcf_basis, ttm_quarter_end, net_debt, customer_deposits, fcf_mode, ...).
    Ausgeschlossene Ticker (harte Filter) haben kein 'dcf' -- erwartet."""
    from valuation.dcf import DcfResult
    fields = set(DcfResult.__dataclass_fields__)
    bewertet = 0
    for m in demo_companies():
        out = evaluate_ticker(m, cfg)
        if out["excluded"]:
            assert "dcf" not in out and out["row"] is None
            continue  # ausgeschlossen -> kein DCF, kein Row
        bewertet += 1
        dcf = out["dcf"]
        assert isinstance(dcf, DcfResult)
        for f in DCF_RESULT_FIELDS:
            assert f in fields, f"DcfResult deklariert '{f}' nicht"
            assert hasattr(dcf, f), f"{m.ticker}: dcf.{f} fehlt (AttributeError)"
        # Konsistenz: fcf_basis gesetzt und ttm_quarter_end nur bei ttm
        assert dcf.fcf_basis in ("annual", "ttm")
        if dcf.fcf_basis != "ttm":
            assert dcf.ttm_quarter_end == ""
        # Row traegt die DCF-Basis durch (Scorecard-Anzeige)
        assert out["row"]["dcf_fcf_basis"] == dcf.fcf_basis
    assert bewertet >= 4  # mindestens AAPL, QUAL, ... bewertet


def test_evaluate_all_rows_haben_kandidat_und_mos(cfg):
    """MoS je Szenario + Kandidaten-Spalte sind in jeder Row gefuellt."""
    rows, _ = evaluate_all(demo_companies(), cfg)
    for row in rows:
        for col in ("margin_of_safety", "mos_bear", "mos_bull", "kandidat"):
            assert col in row
        assert row["kandidat"].startswith(("ja", "nein"))
        for col in ("margin_of_safety", "mos_bear", "mos_bull"):
            v = row[col]
            assert v == v or math.isnan(v)  # NaN erlaubt, fehlende Spalte nicht
