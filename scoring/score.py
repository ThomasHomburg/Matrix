"""Score-Berechnung: lineare Interpolation, Block-Scores, Quality/Value-Gruppen.

Eingang: dict[Kriterium, CriterionResult] (value, is_na, years_available, thin).

Regeln:
- Kriterium mit is_na=True wird aus dem Score herausgenommen; die uebrigen
  Gewichte werden renormalisiert (gewichteter Mittelwert der verbleibenden).
- Quality-Score und Value-Score werden getrennt berechnet (Config-Block
  `score_groups`: quality_blocks / value_blocks). Gesamtscore =
  weights.quality * Quality + weights.value * Value (renormalisiert, falls
  eine Gruppe komplett n/a ist).
- `coverage` = Anteil der bewerteten Kriterien-Gewichte am Gesamtgewicht
  ("Datenabdeckung", z. B. 0.80 -> 80 %).
- Block mit > 30 % fehlender Daten: Flag 'duenn'.
- Kriterium mit thin=True (< data.thin_years Jahre) wird als 'Daten duenn'
  markiert.
- Alle Scores werden auf 1 Nachkommastelle gerundet.
"""

from __future__ import annotations

from scoring.criteria import CriterionResult


def interpolate(value: float, direction: str, full: float, zero: float) -> float:
    """Punkte (0-100) linear zwischen Null- und Voll-Punkte-Schwelle.

    direction 'higher': mehr ist besser (Wert >= full -> 100, <= zero -> 0).
    direction 'lower': weniger ist besser (Wert <= full -> 100, >= zero -> 0).
    """
    if value != value:  # NaN
        return float("nan")
    if direction == "higher":
        if value >= full:
            return 100.0
        if value <= zero:
            return 0.0
        return 100.0 * (value - zero) / (full - zero) if full != zero else float("nan")
    if value <= full:
        return 100.0
    if value >= zero:
        return 0.0
    return 100.0 * (zero - value) / (zero - full) if zero != full else float("nan")


def _r1(x: float) -> float:
    """Auf 1 Nachkommastelle runden; NaN bleibt NaN."""
    return round(x, 1) if x == x else float("nan")


def _weighted_mean(entries: list[tuple[float, float]]) -> float:
    """Gewichteteter Mittelwert; leere Liste -> NaN."""
    den = sum(w for w, _ in entries)
    if not den:
        return float("nan")
    return sum(w * p for w, p in entries) / den


def score_ticker(criteria_values: dict[str, CriterionResult], criteria_cfg: dict,
                 groups_cfg: dict | None = None) -> dict:
    """Gesamt-/Block-/Gruppen-Scores mit Renormalisierung bei n/a.

    groups_cfg (optional, config.yaml Block `score_groups`):
        quality_blocks: [profitability, stability, cashflow, moat]
        value_blocks: [valuation]
        weights: {quality: 0.75, value: 0.25}
    Ohne groups_cfg entfaellt die Gruppenebene (abwaertskompatibel).
    """
    result = {
        "total": float("nan"), "blocks": {}, "block_flags": {}, "details": {},
        "thin_criteria": [], "quality": float("nan"), "value": float("nan"),
        "coverage": float("nan"),
        "quality_coverage": float("nan"),  # Abdeckung NUR der Quality-Bloecke
    }
    blocks: dict[str, list[tuple[float, float, bool]]] = {}

    total_weight = 0.0
    covered_weight = 0.0
    # Quality-Abdeckung separat -- nur Kriterien der Quality-Bloecke
    q_blocks = set((groups_cfg or {}).get("quality_blocks", []))
    q_total_weight = 0.0
    q_covered_weight = 0.0

    for name, crit in criteria_values.items():
        cfg = criteria_cfg[name]
        block = cfg["block"]
        weight = cfg["weight"]
        total_weight += weight
        pts = float("nan") if crit.is_na else interpolate(crit.value, cfg["direction"], cfg["full"], cfg["zero"])
        blocks.setdefault(block, []).append((weight, pts, crit.is_na))
        if not crit.is_na and pts == pts:
            covered_weight += weight
        if block in q_blocks:
            q_total_weight += weight
            if not crit.is_na and pts == pts:
                q_covered_weight += weight
        if crit.thin:
            result["thin_criteria"].append(name)
        result["details"][name] = {
            "value": crit.value, "points": pts, "na": crit.is_na,
            "years_available": crit.years_available, "thin": crit.thin,
        }

    # Datenabdeckung = Anteil bewerteter Gewichte
    result["coverage"] = _r1(covered_weight / total_weight) if total_weight else float("nan")
    # Quality-Abdeckung nur ueber Quality-Bloecke (Value-Luecken zaehlen nicht)
    result["quality_coverage"] = _r1(q_covered_weight / q_total_weight) if q_total_weight else float("nan")

    # Block-Scores (renormalisiert auf nicht-n/a Kriterien je Block)
    block_scores: dict[str, float] = {}
    for block, entries in blocks.items():
        na_share = sum(1 for _, _, na in entries if na) / len(entries)
        scored = [(w, p) for w, p, na in entries if not na and p == p]
        bscore = _weighted_mean(scored)
        block_scores[block] = bscore
        result["blocks"][block] = _r1(bscore)
        result["block_flags"][block] = "duenn" if na_share > 0.30 else ""

    # Gruppen-Scores: gewichteter Mittelwert der verfuegbaren Block-Scores
    if groups_cfg:
        q_blocks = groups_cfg.get("quality_blocks", [])
        v_blocks = groups_cfg.get("value_blocks", [])
        gw = groups_cfg.get("weights", {"quality": 0.75, "value": 0.25})
        q_entries = [(1.0, block_scores[b]) for b in q_blocks
                     if b in block_scores and block_scores[b] == block_scores[b]]
        v_entries = [(1.0, block_scores[b]) for b in v_blocks
                     if b in block_scores and block_scores[b] == block_scores[b]]
        result["quality"] = _r1(_weighted_mean(q_entries))
        result["value"] = _r1(_weighted_mean(v_entries))
        # Value als n/a, wenn Datenabdeckung < score_groups.min_coverage_value
        min_cov = 0.70
        if groups_cfg:
            min_cov = float(groups_cfg.get("min_coverage_value", 0.70))
        if (result["coverage"] == result["coverage"] and result["coverage"] < min_cov):
            result["value"] = float("nan")
        # Quality n/a nur, wenn die QUALITY-Abdeckung (nur Quality-Bloecke)
        # < min_coverage_quality -- Value-Luecken beeinflussen Quality nicht mehr
        min_cov_q = 0.70
        if groups_cfg:
            min_cov_q = float(groups_cfg.get("min_coverage_quality", 0.70))
        qc = result["quality_coverage"]
        if (qc == qc and qc < min_cov_q):
            result["quality"] = float("nan")

        # Gesamtscore aus Gruppen, renormalisiert falls eine Gruppe n/a
        g_entries = []
        if result["quality"] == result["quality"]:
            g_entries.append((gw.get("quality", 0.75), result["quality"]))
        if result["value"] == result["value"]:
            g_entries.append((gw.get("value", 0.25), result["value"]))
        result["total"] = _r1(_weighted_mean(g_entries))
    else:
        # Fallback ohne Gruppen-Config: bisherige Gesamtlogik ueber Blockgewichte
        block_weights = {}
        for cfg in criteria_cfg.values():
            block_weights[cfg["block"]] = block_weights.get(cfg["block"], 0) + cfg["weight"]
        entries = [(block_weights[b], block_scores[b]) for b in block_scores
                   if block_scores[b] == block_scores[b]]
        result["total"] = _r1(_weighted_mean(entries))

    return result
