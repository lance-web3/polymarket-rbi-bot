"""Stage A robustness check — event-level Brier deduplication.

Stage A's GO on `awards_entertainment` came from 68 markets that were
essentially 2-3 underlying events (Drake's ICEMAN album with ~42 feature
buckets, Eurovision 2026 brackets with ~26 advance/top-N buckets). Treating
correlated buckets as independent observations inflates the effective sample
size and can mask noise.

This re-aggregates the Stage A calibration JSONL by underlying event:
  1. Group markets by `event_slug` if present, else by question prefix
     ("Will Drake feature X on ICEMAN" → "ICEMAN feature speculation").
  2. For each event, average the model probabilities and outcomes.
  3. Recompute Brier(LLM) vs Brier(market) with one row per event.

Output: console table + `data/llm_calibration_dedup_summary.json`. Gate re-fires
on the deduped data with the same thresholds.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.calibration import brier_score, reference_brier_baselines
from polymarket_rbi_bot.config import BotConfig


def _event_key(row: dict[str, Any]) -> str:
    """Heuristic event-grouping. Slug field is best when present, otherwise
    extract a question prefix that captures the underlying event."""
    slug = row.get("event_slug")
    if slug:
        return f"slug:{slug}"
    q = (row.get("question") or "").lower()
    # Common patterns
    if "iceman" in q:
        return "drake-iceman-album-2026"
    if "eurovision" in q:
        # Eurovision has multiple sub-events (semi 1, semi 2, top-N, winner) — bucket all
        return "eurovision-2026"
    if "cerebras" in q:
        return "cerebras-ipo"
    if "blackstone" in q:
        return "blackstone-ipo"
    if "eaglerock" in q:
        return "eaglerock-ipo"
    if "micware" in q:
        return "micware-ipo"
    # Strip variable elements: dates, numbers, common variable words
    stem = re.sub(r"\b(20\d\d-\d\d-\d\d|\$\d+|\d{2,})\b", "", q)
    stem = re.sub(r"\s+", " ", stem).strip()
    # take first 6 words as the event key
    return f"q:{' '.join(stem.split()[:6])}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=str, default=None)
    parser.add_argument("--output", type=str, default="data/llm_calibration_dedup_summary.json")
    parser.add_argument("--gate-min-n", type=int, default=10,
                        help="Min distinct events per niche for the gate to apply.")
    parser.add_argument("--gate-brier-improvement", type=float, default=0.005)
    parser.add_argument("--gate-brier-absolute", type=float, default=0.22)
    args = parser.parse_args()

    config = BotConfig.from_env()
    path = Path(args.input or config.llm_calibration_output_path)
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") != "ok":
                continue
            if row.get("p_llm") is None or row.get("market_p_t7") is None or row.get("resolved_outcome") is None:
                continue
            rows.append(row)
    print(f"Loaded {len(rows)} ok rows from {path}")

    # Group by event
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[_event_key(r)].append(r)
    print(f"Distinct underlying events: {len(groups)}")

    # Per-event aggregate: mean p_llm, mean market_p_t7, mean outcome (proportion YES)
    # We treat the event as one "observation" with these aggregated values.
    event_rows: list[dict[str, Any]] = []
    for key, lst in groups.items():
        n = len(lst)
        mean_p_llm = statistics.fmean(float(r["p_llm"]) for r in lst)
        mean_p_mkt = statistics.fmean(float(r["market_p_t7"]) for r in lst)
        mean_outcome = statistics.fmean(float(r["resolved_outcome"]) for r in lst)
        # Niche taken from majority within the event
        from collections import Counter
        niche = Counter(r.get("niche_llm") or r.get("niche_heuristic") or "other" for r in lst).most_common(1)[0][0]
        event_rows.append(
            {
                "event_key": key,
                "n_buckets": n,
                "niche": niche,
                "mean_p_llm": round(mean_p_llm, 4),
                "mean_p_market_t7": round(mean_p_mkt, 4),
                "mean_outcome_yes_rate": round(mean_outcome, 4),
                "brier_llm_per_bucket": round(statistics.fmean([(float(r["p_llm"]) - float(r["resolved_outcome"])) ** 2 for r in lst]), 4),
                "brier_market_per_bucket": round(statistics.fmean([(float(r["market_p_t7"]) - float(r["resolved_outcome"])) ** 2 for r in lst]), 4),
            }
        )

    # Two views:
    #   A. Event-level (one obs per event): Brier(mean_p, mean_outcome) — treats event as a single bet
    #   B. Within-event averaged (uses each event's bucket-level Brier means): mean of brier_llm_per_bucket
    event_brier_llm = brier_score(
        [r["mean_p_llm"] for r in event_rows],
        [round(r["mean_outcome_yes_rate"]) for r in event_rows],  # binarize to compare against Brier baseline
    ) if event_rows else None
    event_brier_mkt = brier_score(
        [r["mean_p_market_t7"] for r in event_rows],
        [round(r["mean_outcome_yes_rate"]) for r in event_rows],
    ) if event_rows else None

    # Note: outcome_yes_rate is fractional within events with mixed YES/NO buckets.
    # The event-level Brier as ((mean_p - mean_y)^2) is more honest:
    sq_err_llm = [(r["mean_p_llm"] - r["mean_outcome_yes_rate"]) ** 2 for r in event_rows]
    sq_err_mkt = [(r["mean_p_market_t7"] - r["mean_outcome_yes_rate"]) ** 2 for r in event_rows]
    event_brier_llm_real = statistics.fmean(sq_err_llm) if sq_err_llm else None
    event_brier_mkt_real = statistics.fmean(sq_err_mkt) if sq_err_mkt else None

    # Per-niche dedup
    by_niche: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in event_rows:
        by_niche[e["niche"]].append(e)
    per_niche_summary: dict[str, Any] = {}
    passing: list[str] = []
    for niche, lst in by_niche.items():
        if len(lst) < args.gate_min_n:
            per_niche_summary[niche] = {"n_events": len(lst), "skipped": "below gate_min_n"}
            continue
        sq_llm = [(e["mean_p_llm"] - e["mean_outcome_yes_rate"]) ** 2 for e in lst]
        sq_mkt = [(e["mean_p_market_t7"] - e["mean_outcome_yes_rate"]) ** 2 for e in lst]
        brier_l = statistics.fmean(sq_llm)
        brier_m = statistics.fmean(sq_mkt)
        per_niche_summary[niche] = {
            "n_events": len(lst),
            "brier_llm_event_level": round(brier_l, 4),
            "brier_market_event_level": round(brier_m, 4),
            "delta_llm_minus_market": round(brier_l - brier_m, 4),
        }
        if (
            brier_l <= brier_m - args.gate_brier_improvement
            and brier_l < args.gate_brier_absolute
        ):
            passing.append(niche)

    summary = {
        "n_underlying_events": len(event_rows),
        "n_original_markets": len(rows),
        "event_brier_llm": round(event_brier_llm_real, 4) if event_brier_llm_real is not None else None,
        "event_brier_market_p_t7": round(event_brier_mkt_real, 4) if event_brier_mkt_real is not None else None,
        "delta_llm_minus_market_event_level": (
            round(event_brier_llm_real - event_brier_mkt_real, 4)
            if event_brier_llm_real is not None and event_brier_mkt_real is not None else None
        ),
        "per_niche_event_level": per_niche_summary,
        "passing_niches_event_level": passing,
        "verdict_event_level": "GO" if passing else "NO_GO",
        "events": sorted(event_rows, key=lambda e: e["n_buckets"], reverse=True),
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print("\n=== Stage A event-level dedup ===")
    print(f"  underlying events: {len(event_rows)} (from {len(rows)} original markets)")
    print(f"  Brier(LLM event-level)    = {event_brier_llm_real:.4f}")
    print(f"  Brier(market event-level) = {event_brier_mkt_real:.4f}")
    print(f"  Δ event-level             = {summary['delta_llm_minus_market_event_level']:+.4f}")
    print(f"  Verdict: {summary['verdict_event_level']}")
    print(f"\nPer-niche (event-level):")
    for niche, stats in per_niche_summary.items():
        if "brier_llm_event_level" in stats:
            print(
                f"  {niche:>25s}  n_events={stats['n_events']:>3d}  "
                f"brier_llm={stats['brier_llm_event_level']:.4f}  "
                f"brier_mkt={stats['brier_market_event_level']:.4f}  "
                f"Δ={stats['delta_llm_minus_market']:+.4f}"
            )
        else:
            print(f"  {niche:>25s}  n_events={stats['n_events']:>3d}  (skipped: {stats['skipped']})")
    print(f"\nTop 10 events by bucket count:")
    for e in summary["events"][:10]:
        print(
            f"  n={e['n_buckets']:>3d}  niche={e['niche']:<22s}  "
            f"p_llm={e['mean_p_llm']:.3f}  p_mkt={e['mean_p_market_t7']:.3f}  "
            f"y_rate={e['mean_outcome_yes_rate']:.3f}  | {e['event_key'][:60]}"
        )


if __name__ == "__main__":
    main()
