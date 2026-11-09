"""Tests fuer Pipeline-Flags und Quality/Value-Scores."""

import math

from config import load_config
from scoring.pipeline import evaluate_ticker
from scoring.score import score_ticker


def test_aapl_roic_flag_kapitalbasis_klein(aapl_metrics):
    """AAPL hat Net Cash -> ROIC > 100 % -> kompaktes Flag in der Ranglisten-Zeile."""
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    row = res["row"]
    assert row is not None
    assert row["roic_5y"] > 1.0
    assert "kl.Kapital" in row["flags"]


def test_normal_roic_has_no_kapitalbasis_flag():
    from data.demo import demo_companies

    cfg = load_config()
    qual = next(c for c in demo_companies() if c.ticker == "QUAL")
    res = evaluate_ticker(qual, cfg)
    assert res["row"] is not None
    assert res["row"]["roic_5y"] < 1.0
    assert "kl.Kapital" not in res["row"]["flags"]


def test_quality_value_scores_present(aapl_metrics):
    """Quality und Value werden getrennt berechnet; Gesamtscore = gewichtete Summe."""
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    s = res["score"]
    assert s["quality"] == s["quality"]  # nicht NaN
    assert s["value"] == s["value"]
    gw = cfg["score_groups"]["weights"]
    expected = gw["quality"] * s["quality"] + gw["value"] * s["value"]
    assert abs(s["total"] - expected) < 0.2  # Rundung auf 1 Nachkommastelle


def test_coverage_is_fraction(aapl_metrics):
    """Datenabdeckung liegt zwischen 0 und 1 (hier: alle Kriterien bewertet -> 100 %)."""
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    cov = res["score"]["coverage"]
    assert 0.0 <= cov <= 1.0
    # Standardgruppe (AAPL) -- alle aktiven Kriterien haben Daten -> 100 %
    assert cov == 1.0


def test_na_criteria_excluded_and_renormalized(aapl_metrics):
    """n/a-Kriterien werden herausgenommen; uebrige Gewichte renormalisieren sich."""
    from scoring.criteria import compute_criteria
    cfg = load_config()
    crits = compute_criteria(aapl_metrics, cfg)
    # EBITDA loeschen -> net_debt_ebitda wird n/a
    for fy in aapl_metrics.years:
        aapl_metrics.years[fy]["ebitda"].value = float("nan")
    crits = compute_criteria(aapl_metrics, cfg)
    res = score_ticker(crits, cfg["criteria"], cfg.get("score_groups"))
    assert crits["net_debt_ebitda"].is_na
    # Deckung sinkt unter 100 % (Gewicht 0.10 fehlt; v28: Sektor-Kriterien
    # sind fuer AAPL (Standardgruppe) nicht aktiv, Basis-Gewicht 1.0)
    assert res["coverage"] < 1.0
    assert res["coverage"] == round(0.90, 1)
    # Quality-Score trotzdem definiert (renormalisiert auf verbleibende Bloecke)
    assert res["quality"] == res["quality"]


def test_sbc_yield_is_na_when_sbc_missing(aapl_metrics):
    """Ohne SBC in ALLEN Jahren -> fcf_yield_sbc_avg3 n/a (Roh-FCF-Yield fliesst
    NICHT in den Value-Score), nie Kandidat. v35: Status 'Pruefen (SBC fehlt)' NUR,
    wenn ohne SBC Quality >= 60 UND MoS >= 30 % -- AAPL (MoS < 30 %) -> normaler Status."""
    from scoring.criteria import compute_criteria
    from scoring.pipeline import evaluate_ticker
    cfg = load_config()
    cfg["sec"] = {"enabled": False}
    for fy in aapl_metrics.years:
        aapl_metrics.years[fy]["fcf_sbc"].value = float("nan")
    crits = compute_criteria(aapl_metrics, cfg)
    # Roh-FCF-Yield fliesst nicht in den Value-Score ein:
    assert crits["fcf_yield_sbc_avg3"].is_na
    out = evaluate_ticker(aapl_metrics, cfg)
    row = out["row"]
    assert row is not None
    assert "SBC n/a (ohne SBC)" in row["flags"]
    assert row["is_buy"] is False  # nie Kandidat
    # Pruefstatus nur bei Quality >= 60 UND MoS >= 30 % ohne SBC
    assert row["status_pruefen_sbc"] is (row["status"] == "Prüfen (SBC fehlt)")
    if row["status_pruefen_sbc"]:
        assert row["status"] == row["status_all"] == row["status_recent2"] == "Prüfen (SBC fehlt)"


def test_row_has_both_yield_columns(aapl_metrics):
    """Ranglisten-Zeile enthaelt Roh-FCF-Yield UND FCF-Yield nach SBC."""
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    row = res["row"]
    assert "fcf_yield" in row and "fcf_yield_sbc" in row
    assert row["fcf_yield_sbc"] < row["fcf_yield"]  # SBC abgezogen
    # 3J-Spalte nutzt den SBC-Wert (Value-Score)
    assert row["fcf_yield_3y"] == res["score"]["details"]["fcf_yield_sbc_avg3"]["value"]


def test_scores_rounded_to_1_decimal(aapl_metrics):
    """Alle Scores sind auf 1 Nachkommastelle gerundet."""
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    s = res["score"]
    for key in ("total", "quality", "value", "coverage"):
        v = s[key]
        if v == v:
            assert v == round(v, 1), f"{key}={v} nicht auf 1 Nachkommastelle gerundet"


def test_row_has_roic_latest_and_window_years(aapl_metrics):
    """Row enthaelt ROIC letztes GJ (neuestes FY) + Fensterlaenge des ROIC Ø."""
    from data.metrics import roic
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    row = res["row"]
    latest_fy = max(aapl_metrics.years)
    assert row["roic_latest"] == roic(aapl_metrics, latest_fy)
    assert row["roic_latest_fy"] == latest_fy
    assert row["roic_5y_years"] == res["criteria"]["roic_avg5"].years_available


def test_rank_rows_ties_and_nan():
    """Quality-Rang: 1 = bester Wert; Gleichstand -> gleicher Rang; NaN -> None."""
    from scoring.pipeline import rank_rows
    rows = [
        {"ticker": "A", "quality": 100.0},
        {"ticker": "B", "quality": 100.0},
        {"ticker": "C", "quality": 94.3},
        {"ticker": "D", "quality": float("nan")},
    ]
    ranks = rank_rows(rows, "quality")
    assert ranks["A"] == 1 and ranks["B"] == 1   # Gleichstand
    assert ranks["C"] == 2                        # nächster freier Rang
    assert ranks["D"] is None                     # n/a


def test_row_has_mos_scenarios_and_kandidat(aapl_metrics):
    """Row enthaelt MoS Bear/Bull + Kandidaten-Spalte mit Begruendung."""
    cfg = load_config()
    res = evaluate_ticker(aapl_metrics, cfg)
    row = res["row"]
    assert "mos_bear" in row and "mos_bull" in row and "kandidat" in row
    assert row["kandidat"].startswith(("ja", "nein"))
    # AAPL ist kein Kaufkandidat -> Begruendung nennt fehlende Bedingungen
    if not row["is_buy"]:
        assert "nein:" in row["kandidat"]


def test_kandidat_uses_quality_rule_not_score(aapl_metrics, cfg, monkeypatch):
    """Kandidatenregel = Quality >= 60 (config buy_quality_min).

    Auch bei Gesamtscore >= 70 darf die Begruendung NICHT 'Score < 70' nennen,
    sondern muss die Quality-Schwelle referenzieren.
    """
    import scoring.pipeline as pl

    orig_score = pl.score_ticker

    def fake_score(criteria, crit_cfg, groups=None):
        res = orig_score(criteria, crit_cfg, groups)
        res["quality"] = 55.0   # unter der Schwelle 60
        return res

    monkeypatch.setattr(pl, "score_ticker", fake_score)
    res = evaluate_ticker(aapl_metrics, cfg)
    row = res["row"]
    assert row["quality"] == 55.0
    # Gesamtscore hoch (0,75*55 + 0,25*Value = 68,2 bei Value 84,6), Quality niedrig
    assert row["score"] >= 65
    assert not row["is_buy"]
    assert "Quality 55.0 < 60" in row["kandidat"]
    assert "Score" not in row["kandidat"]


def test_mos_base_criterion_uses_dcf(aapl_metrics, cfg):
    """mos_base-Kriterium liefert die MoS (Base) aus dem DCF."""
    from scoring.criteria import compute_criteria
    from valuation.dcf import compute_dcf
    crits = compute_criteria(aapl_metrics, cfg)
    dcf = compute_dcf(aapl_metrics, cfg)
    mos = crits["mos_base"]
    assert not mos.is_na
    assert mos.value == dcf.margin_of_safety


def test_mos_base_interpolation_points(aapl_metrics, cfg):
    """MoS >= 40 % -> 100 Punkte; MoS <= 0 % -> 0 Punkte (config: mos_base)."""
    from scoring.criteria import compute_criteria
    from scoring.score import interpolate, score_ticker
    cc = cfg["criteria"]["mos_base"]
    assert cc["full"] == 0.40 and cc["zero"] == 0.0
    assert interpolate(0.45, cc["direction"], cc["full"], cc["zero"]) == 100.0
    assert interpolate(0.0, cc["direction"], cc["full"], cc["zero"]) == 0.0
    assert interpolate(0.20, cc["direction"], cc["full"], cc["zero"]) == 50.0
    # AAPL: MoS Base negativ -> mos_base 0 Punkte, Value sinkt
    crits = compute_criteria(aapl_metrics, cfg)
    res = score_ticker(crits, cfg["criteria"], cfg.get("score_groups"))
    assert res["details"]["mos_base"]["points"] == 0.0


def test_value_weights_sum_to_one(cfg):
    """Value-Block (Standard): FCF-Yield 30 % + MoS Base 40 % + EV/EBIT 30 % = 100 %.

    Zusaetzlich ffo_yield (REITs) im Valuation-Block -- die Summe der
    Standard-Kriterien bleibt 1.0; Sektor-Kriterien werden je Gruppe
    aktiviert (siehe test_v28.py)."""
    w = {n: c["weight"] for n, c in cfg["criteria"].items()
         if c["block"] == "valuation" and n != "ffo_yield"}
    assert w == {"fcf_yield_sbc_avg3": 0.30, "mos_base": 0.40, "ev_ebit_vs_median": 0.30}
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_quality_thresholds_sharpened(cfg):
    """Verschaerfte Quality-Schwellen: ROIC 35 %, CAGR 15 %, FCF-Marge 30 %."""
    c = cfg["criteria"]
    assert c["roic_avg5"]["full"] == 0.35
    assert c["revenue_cagr_5y"]["full"] == 0.15
    assert c["fcf_margin_avg5"]["full"] == 0.30
