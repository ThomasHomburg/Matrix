"""rohwerte_check.py: Rohwerte fuer XOM und DOW (wie die App geladen/evaluiert).

Laedt beide Ticker live ueber load_with_provider (Provider-Routing wie in der
App), wertet sie mit evaluate_ticker aus und schreibt rohwerte_xom_dow.txt
in den Projektordner. Je Ticker:
  - Marktkapitalisierung mit Stichtag, Kurs, verwaesserte Aktien
  - Cashflow je Geschaeftsjahr (OCF, Capex, FCF, SBC)
  - Nettoverschuldung (gemeldet UND Schulden - Cash - kurzfristige Anlagen)
  - Start-FCF je Modus (O3J, juengstes GJ, TTM O, TTM) + Wert je Aktie
    (Bear/Base/Bull) + MoS je Modus
  - EBIT, Steuersatz, NOPAT, Eigenkapital, ROIC je Jahr
  - alle Quality-Kriterien mit Rohwert, Schwelle (full/zero) und Punkten
  - Datenabdeckung
Am Ende 3 Saetze je Ticker: warum MoS < -100 % (XOM) bzw. Quality n/a (DOW).

Aufruf:  python rohwerte_check.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_config
from data.providers.base import build_providers, load_with_provider
from data.metrics import tax_rate, nopat, roic
from scoring.pipeline import evaluate_ticker
from valuation.dcf import compute_dcf

TICKERS = ["XOM", "DOW"]
# Diagnose Ueberlauf bei hohem Wachstum -- optional per CLI:
#   python rohwerte_check.py RDDT STT
if len(sys.argv) > 1:
    TICKERS = [a.upper() for a in sys.argv[1:]]
OUT = Path(__file__).resolve().parent / "rohwerte_xom_dow.txt"


def _f(v: float, nd: int = 2) -> str:
    return "n/a" if v != v else f"{v:,.{nd}f}"


def _mrd(v: float) -> str:
    return "n/a" if v != v else f"{v / 1e9:,.2f} Mrd."


def _pct(v: float) -> str:
    return "n/a" if v != v else f"{v:.1%}"


def fmt_szenario_vps(d) -> tuple[str, str, str]:
    """Bear/Base/Bull als Text fuer die Modus-Tabelle.

    Start-FCF <= 0 (d.fcf_le0) -> alle drei Szenarien "n/a (Start-FCF \u2264 0)"
    statt Zahlen (MoS ist dann nicht definiert)."""
    if getattr(d, "fcf_le0", False):
        na = "n/a (Start-FCF \u2264 0)"
        return na, na, na
    out = []
    for name in ("bear", "base", "bull"):
        sc = d.scenarios.get(name) if hasattr(d, "scenarios") else None
        v = sc.value_per_share if sc is not None else float("nan")
        out.append(_f(v))
    return tuple(out)


def fmt_scenario_growth(d) -> str:
    """tatsaechliche Wachstumsraten J. 1-5 je Szenario aus DcfResult,
    z. B. "Bear -9.7%, Base -6.7%, Bull 0.0%" (CAGR < 0) bzw. "Bear 0.0%, ..."."""
    sg = getattr(d, "scenario_growth", None) or {}
    parts = []
    for name in ("bear", "base", "bull"):
        g = sg.get(name, float("nan"))
        parts.append(f"{name.capitalize()} {_pct(g)}")
    return ", ".join(parts)


def erklaerung_xom(xo: dict) -> list[str]:
    """3 Saetze XOM -- nennt die tatsaechlichen Wachstumsraten je Szenario
    aus der DCF-Funktion (DcfResult.scenario_growth), nicht den Festtext."""
    modes = xo["modes"].values()
    vps = [d.scenarios["base"].value_per_share for d in modes
           if "base" in d.scenarios and d.scenarios["base"].value_per_share == d.scenarios["base"].value_per_share]
    mos_vals = [d.margin_of_safety for d in modes if d.margin_of_safety == d.margin_of_safety]
    fcf_vals = [d.fcf_normalized / 1e9 for d in modes]
    dcf = xo["dcf"]
    lines = [
        "XOM (MoS < -100 %):",
        f"  Der DCF ergibt je nach Startwert-Modus einen inneren Wert von nur {min(vps):,.2f} bis {max(vps):,.2f} USD je Aktie",
        f"  (Start-FCF {min(fcf_vals):,.1f}-{max(fcf_vals):,.1f} Mrd. USD ohne SBC, Nettoverschuldung {dcf.net_debt / 1e9:,.1f} Mrd., "
        f"Umsatz-CAGR {_pct(dcf.revenue_cagr)} -> Szenario-Wachstum J. 1-5: {fmt_scenario_growth(dcf)}),",
        f"  der reale Kurs liegt mit {xo['price']:,.2f} USD beim etwa {xo['price'] / max(vps):.1f}- bis {xo['price'] / min(vps):.1f}-Fachen dieses Werts, "
        f"daher MoS = 1 - Kurs/Wert = {min(mos_vals):.0%} bis {max(mos_vals):.0%}.",
    ]
    return lines


def main() -> None:
    cfg = load_config()
    providers = build_providers("")
    lines: list[str] = []
    w = lines.append
    global _LAST, _LAST_OUT
    _LAST, _LAST_OUT = [], {}

    w(f"rohwerte_xom_dow.txt -- Rohwerte fuer {' und '.join(TICKERS)} (live, wie die App)")
    w(f"Erzeugt von rohwerte_check.py; SEC-Abgleich aktiv wie in config.yaml (sec.enabled={cfg['sec'].get('enabled')})")
    w("=" * 100)

    for ticker in TICKERS:
        m = load_with_provider(ticker, cfg, providers, years=cfg["data"]["years"])
        out = evaluate_ticker(m, cfg)
        _LAST.append(m)
        _LAST_OUT[ticker] = {"dcf": out["dcf"], "price": (m.market_cap.value / out["dcf"].shares
                                 if m.market_cap and out["dcf"].shares == out["dcf"].shares
                                 else float("nan")),
                             "modes": {mode: compute_dcf(m, cfg, fcf_mode=mode)
                                       for mode in ("avg3", "latest", "ttm", "ttm_only")}}
        row, dcf = out["row"], out["dcf"]
        crits = out["criteria"]
        score = out["score"]
        cur = m.market_cap.currency if m.market_cap else ""

        w("")
        w(f"########## {ticker} -- {m.name} | Industry: {m.industry} | Sektor: {m.sector} | Land: {m.country} | Quelle: {m.source} ##########")
        w("")
        # --- Marktkapitalisierung / Kurs / Aktien ---
        mcap = m.market_cap.value if m.market_cap and not m.market_cap.is_na() else float("nan")
        shares = dcf.shares
        price = mcap / shares if mcap == mcap and shares == shares and shares > 0 else float("nan")
        w(f"Marktkapitalisierung: {_mrd(mcap)} {cur} (Stichtag Abrufdatum: {m.market_cap_date})")
        w(f"Verwaesserte Aktien (neuestes FY): {shares / 1e9:,.2f} Mrd.")
        w(f"Kurs (Marktkap. / Aktien): {_f(price)} {cur}")
        w("")
        # --- Cashflow je Geschaeftsjahr ---
        w("Cashflow je Geschaeftsjahr (Mrd. " + cur + "):")
        w(f"  {'FY':<6} {'OCF':>10} {'Capex':>10} {'FCF':>10} {'SBC':>10} {'FCF-SBC':>10}")
        for fy in sorted(m.years):
            ym = m.years[fy]
            def g(k):
                met = ym.get(k)
                return met.value if met is not None and not met.is_na() else float("nan")
            w(f"  {fy:<6} {_mrd(g('operating_cashflow')):>10} {_mrd(g('capex')):>10} "
              f"{_mrd(g('fcf')):>10} {_mrd(g('stock_based_compensation')):>10} {_mrd(g('fcf_sbc')):>10}")
        w("")
        # --- Nettoverschuldung: gemeldet und Schulden - Cash - kurzfr. Anlagen ---
        w("Nettoverschuldung (neuestes Jahr mit Bilanzdaten):")
        for fy in sorted(m.years, reverse=True):
            ym = m.years[fy]
            nd = ym.get("net_debt"); td = ym.get("_total_debt"); cash = ym.get("_cash"); sti = ym.get("_short_term_investments")
            if nd is not None and not nd.is_na():
                w(f"  FY{fy} gemeldet (net_debt): {_mrd(nd.value)} {nd.currency}")
                # Schulden - Cash - kurzfr. Anlagen; Total Debt bei Yahoo teils NaN
                # (XOM: Zeile 'Total Debt' leer) -> Fallback Long+Short Term Debt
                td_v = td.value if td is not None and not td.is_na() else float("nan")
                if td_v != td_v:
                    ltd = ym.get("_long_term_debt"); std = ym.get("_short_term_debt")
                    parts = [x.value for x in (ltd, std)
                             if x is not None and not x.is_na()]
                    td_v = sum(parts) if parts else float("nan")
                    if td_v == td_v:
                        w(f"  FY{fy} Hinweis: 'Total Debt' fehlt bei Yahoo -> Summe Long+Short Term Debt = {_mrd(td_v)}")
                cash_v = cash.value if cash is not None and not cash.is_na() else float("nan")
                sti_v = sti.value if sti is not None and not sti.is_na() else 0.0
                # Yahoo-Fallback: 'Short Term Investments' kann auf dieselbe Zeile wie
                # Cash zeigen (Cash+STI inkl. Cash) -> nur abziehen, wenn verschieden
                if sti_v == cash_v:
                    sti_v = 0.0
                if td_v == td_v and cash_v == cash_v:
                    w(f"  FY{fy} Schulden - Cash - kurzfr. Anlagen: {_mrd(td_v - cash_v - sti_v)} {nd.currency} "
                      f"(Schulden {_mrd(td_v)}, Cash {_mrd(cash_v)}, kurzfr. Anlagen {_mrd(sti_v)})")
                break
        w(f"  DCF verwendet (ohne Kundengelder, {dcf.net_debt_mode}): {_mrd(dcf.net_debt)} -- {dcf.net_debt_source}")
        w("")
        # --- Start-FCF je Modus, Wert je Aktie je Szenario, MoS je Modus ---
        w("DCF je Startwert-Modus (Start-FCF, Wert je Aktie Bear/Base/Bull, MoS Base):")
        w(f"  {'Modus':<10} {'Start-FCF':>12} {'Wert/Aktie Bear':>16} {'Base':>10} {'Bull':>10} {'MoS Base':>10} {'MoS bei 10%':>12}")
        for mode in ("avg3", "latest", "ttm", "ttm_only"):
            d = compute_dcf(m, cfg, fcf_mode=mode)
            # Start-FCF <= 0 -> "n/a (Start-FCF <= 0)" statt Zahlen
            bear_s, base_s, bull_s = fmt_szenario_vps(d)
            w(f"  {mode:<10} {_mrd(d.fcf_normalized):>12} {bear_s:>16} {base_s:>10} {bull_s:>10} {_pct(d.margin_of_safety):>10}"
              f" {_pct(row['mos_robust'] if mode == dcf.fcf_mode else float('nan')):>12}")
        w(f"  aktiver Modus (dcf.fcf_mode={dcf.fcf_mode}): MoS Base {_pct(row['margin_of_safety'])}, MoS bei 10 % (robust) {_pct(row['mos_robust'])}")
        w(f"  Umsatz-CAGR (Basis-Wachstum): {_pct(dcf.revenue_cagr)} | Diskontsatz {_pct(dcf.discount_rate)} | Terminal Growth {_pct(dcf.terminal_growth)}")
        # Szenario-Wachstumsraten J. 1-5 (tatsaechlich, aus DcfResult)
        w(f"  Szenario-Wachstum J. 1-5 (tatsaechlich): {fmt_scenario_growth(dcf)}")
        w("")
        # --- EBIT / Steuersatz / NOPAT / EK / ROIC je Jahr ---
        w("EBIT, Steuersatz, NOPAT, Eigenkapital, ROIC je Jahr:")
        w(f"  {'FY':<6} {'EBIT (Mrd.)':>12} {'Steuer':>8} {'NOPAT (Mrd.)':>13} {'EK (Mrd.)':>12} {'ROIC':>8}")
        for fy in sorted(m.years):
            ym = m.years[fy]
            eb = ym.get("ebit")
            eq = ym.get("equity")
            w(f"  {fy:<6} {_mrd(eb.value if eb is not None else float('nan')):>12} "
              f"{_pct(tax_rate(m, fy)):>8} {_mrd(nopat(m, fy)):>13} "
              f"{_mrd(eq.value if eq is not None else float('nan')):>12} {_pct(roic(m, fy)):>8}")
        w("")
        # --- Quality-Kriterien mit Rohwert, Schwelle, Punkten ---
        w("Quality-/Value-Kriterien (Rohwert, Schwellen full/zero aus config.yaml, Punkte 0-100):")
        w(f"  {'Kriterium':<26} {'Rohwert':>12} {'full':>8} {'zero':>8} {'Punkte':>8} {'n/a':>4} {'Jahre':>5}")
        for name in sorted(crits):
            c = crits[name]
            det = score["details"][name]
            ccfg = cfg["criteria"].get(name, {})
            pts = det["points"]
            w(f"  {name:<26} {_f(det['value'], 4):>12} {_f(ccfg.get('full', float('nan')), 4):>8} "
              f"{_f(ccfg.get('zero', float('nan')), 4):>8} {('n/a' if pts != pts else f'{pts:.1f}'):>8} "
              f"{('ja' if det['na'] else ''):>4} {det['years_available']:>5}")
        w("")
        w(f"Quality={_f(row['quality'], 1)}  Value={_f(row['value'], 1)}  Datenabdeckung={_pct(row['coverage'])}  "
          f"Score={_f(row['score'], 1)}  Status={row['status']}  Gruppe={row['sector_group']}")
        w(f"Flags: {row['flags']}")
        if out.get("data_quality"):
            w("Datenqualitaet:")
            for d in out["data_quality"]:
                w(f"  - {d}")

    # --- Erklaerungen ---
    w("")
    w("=" * 100)
    w(f"ERKLAERUNG (3 Saetze je Ticker: {'/'.join(TICKERS)})")
    w("")
    # XOM-Werte dynamisch aus dem Lauf (statt hartkodiert)
    xom = next((m for m in _LAST if m.ticker == "XOM"), None)
    if xom is not None and "XOM" in _LAST_OUT:
        # Erklaerung nennt die tatsaechlichen Wachstumsraten je Szenario
        # aus DcfResult.scenario_growth (z. B. "Bear -9.7%, Base -6.7%, Bull 0.0%")
        for line in erklaerung_xom(_LAST_OUT["XOM"]):
            w(line)
    w("")
    dow = next((m for m in _LAST if m.ticker == "DOW"), None)
    if dow is not None and "DOW" in _LAST_OUT:
        drow = _LAST_OUT["DOW"]["dcf"]
        dcrits = None
        # Kriterien des DOW-Laufs rekonstruieren (gleiches evaluate_ticker-Ergebnis)
        dres = evaluate_ticker(dow, cfg)
        det = dres["score"]["details"]
        zero_pts = [n for n, d in det.items() if not d["na"] and (d["points"] != d["points"] or d["points"] <= 0.0)]
        na_pts = [n for n, d in det.items() if d["na"]]
        cov = dres["row"]["coverage"]
        w("DOW (Quality n/a):")
        w(f"  {len(zero_pts)} Kriterien mit 0 Punkten und {len(na_pts)} n/a ({', '.join(na_pts)}; u. a. CAGR {dcrits or ''}"
          f"{dres['criteria']['revenue_cagr_5y'].value:.1%}, ND/EBITDA {dres['criteria']['net_debt_ebitda'].value:.2f}, "
          f"Zinsdeckung {dres['criteria']['interest_coverage'].value:.2f}, FCF positiv in {dres['criteria']['fcf_positive_years'].value:.0%} der Jahre),")
        w(f"  sodass die Datenabdeckung mit {cov:.0%} unter der Schwelle min_coverage_quality von 70 % liegt und der "
          f"Quality-Score deshalb als n/a ausgewiesen wird.")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"geschrieben: {OUT} ({OUT.stat().st_size} Bytes, {len(lines)} Zeilen)")


if __name__ == "__main__":
    main()
