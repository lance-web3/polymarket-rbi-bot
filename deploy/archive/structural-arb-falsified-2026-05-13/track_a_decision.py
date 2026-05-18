"""Apply Track A's pre-committed go/no-go criteria to a championship-mode arb scan.

Pinned criteria (from PLAN, 2026-04-25 — DO NOT lower mid-experiment):

  GO (commit Phase 3 executable design): for any single championship,
      - field coverage >= 0.85 (we tracked at least 85% of contenders)
      - >= 1 time-bucket where bundle_ask <= 1 - 0.0030 (i.e., 30 bps net edge)
      - p95 bundle_ask <= 1 - 0.0010 (the *typical* day at p95 still shows arb,
        not a freak one-off)
      - median bundle_ask <= 1.0010 (bundles aren't structurally over-priced)
      - net of measured 158.9 bps round-trip cost (from 2026-04-25 audit), the
        per-leg requirement is >= 80 bps half-spread payment, so 30 bps net = ~190 bps gross
        — this is the strict bar, well above the 80 bps cost prior we used previously

  NO-GO (retire structural arb in PLAN, engage Section 3 off-ramps):
      - all championships fail the GO criteria above
      - OR field coverage < 0.85 on every championship that did show edge
        (signal would be a coverage artifact, not a real arb)

  AMBIGUOUS (re-collect more data, do not commit either way):
      - field coverage 0.70 - 0.85 (partial field, can't trust the sum-Yes math)
      - one championship marginally clears 30 bps but fewer than 5 buckets across
        the full window (single-day fluke)

This script applies those rules deterministically to a `scan_live_arb
--championship-mode` JSON output and prints GO / NO_GO / AMBIGUOUS, plus the
specific failing criterion if not GO.

Usage:
    python -m deploy.track_a_decision \
        --scan-output data/track_a_scan_2026_05_02.json \
        --shortlist data/scan_shortlist.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Inlined from scan_live_arb (both archived together; keeps this script self-contained)
DEFAULT_CHAMPIONSHIP_PATTERNS: dict[str, str] = {
    "NBA_FINALS_2026": r"win the 2026 NBA Finals\??$",
    "NHL_CUP_2026": r"win the 2026 NHL Stanley Cup\??$",
    "EPL_2025_26": r"win the 2025[-/–]26 (English )?Premier League\??$",
}

# Expected field sizes per championship — used to sanity-check coverage.
# Polymarket doesn't always list every theoretical contender; these are the
# typical sizes for *playoff-eligible* fields, which is what Polymarket lists.
EXPECTED_FIELD_SIZE: dict[str, int] = {
    "NBA_FINALS_2026": 16,  # 16 playoff teams
    "NHL_CUP_2026": 16,  # 16 playoff teams
    "EPL_2025_26": 5,  # Polymarket only lists 5 realistic title contenders
    # MLB World Series 2026 + NCAA football not yet listed on Polymarket.
}

MIN_FIELD_COVERAGE_GO = 0.85
MIN_FIELD_COVERAGE_AMBIGUOUS = 0.70
MIN_NET_EDGE_BPS = 30.0
MIN_P95_EDGE_BPS = 10.0
MAX_MEDIAN_BUNDLE = 1.0010
MIN_BUCKETS_FOR_GO = 5
MIN_MEDIAN_EDGE_BPS = (1.0 - MAX_MEDIAN_BUNDLE) * 10_000


def field_coverage(shortlist_path: Path, patterns: dict[str, str]) -> dict[str, dict[str, Any]]:
    data = json.loads(shortlist_path.read_text())
    entries = data.get("shortlist", [])
    compiled = {k: re.compile(v, re.IGNORECASE) for k, v in patterns.items()}
    counts: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        q = (e.get("question") or "").strip()
        for name, pat in compiled.items():
            if pat.search(q):
                counts[name].append(q)
                break
    out: dict[str, dict[str, Any]] = {}
    for name in patterns:
        captured = len(counts.get(name, []))
        expected = EXPECTED_FIELD_SIZE.get(name, captured or 1)
        coverage = captured / expected if expected > 0 else 0.0
        out[name] = {
            "captured": captured,
            "expected": expected,
            "coverage": round(coverage, 3),
            "questions": counts.get(name, []),
        }
    return out


MIN_FIELD_COVERAGE_USABLE = 0.30


def evaluate_championship(scan_row: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    cov = coverage.get("coverage", 0.0)
    bucket_count = int(scan_row.get("bucket_count") or scan_row.get("qualifying_buckets") or 0)
    buckets_above_mve = int(scan_row.get("buckets_above_mve") or scan_row.get("buy_above_mve") or 0)

    if cov < MIN_FIELD_COVERAGE_USABLE:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "failures": [f"coverage {cov:.2f} < {MIN_FIELD_COVERAGE_USABLE} (no data — championship not in current watchlist)"],
            "note": "Excluded from overall verdict.",
        }

    failures: list[str] = []
    if cov < MIN_FIELD_COVERAGE_GO:
        failures.append(f"coverage {cov:.2f} < {MIN_FIELD_COVERAGE_GO} (need ≥{int(EXPECTED_FIELD_SIZE.get(scan_row.get('championship', ''), 0) * MIN_FIELD_COVERAGE_GO)} contenders)")

    if "buy_edge_bps" in scan_row:
        edge = scan_row.get("buy_edge_bps") or {}
        max_edge = edge.get("max")
        p05_edge = edge.get("p05")
        median_edge = edge.get("median")
        if max_edge is None or max_edge < MIN_NET_EDGE_BPS:
            failures.append(f"max_buy_edge_bps {max_edge} < {MIN_NET_EDGE_BPS}bps")
        if p05_edge is None or p05_edge < MIN_P95_EDGE_BPS:
            failures.append(f"p05_buy_edge_bps {p05_edge} < {MIN_P95_EDGE_BPS}bps (not typical-day arb)")
        if median_edge is None or median_edge < MIN_MEDIAN_EDGE_BPS:
            failures.append(f"median_buy_edge_bps {median_edge} < {MIN_MEDIAN_EDGE_BPS:.1f}bps (structurally over-priced)")
    else:
        median_ask = scan_row.get("median_bundle_ask")
        p95_ask = scan_row.get("p95_bundle_ask")
        min_ask = scan_row.get("min_bundle_ask")
        if min_ask is None or min_ask > 1.0 - (MIN_NET_EDGE_BPS / 10000):
            failures.append(f"min_bundle_ask {min_ask} > 1 - {MIN_NET_EDGE_BPS}bps")
        if p95_ask is None or p95_ask > 1.0 - (MIN_P95_EDGE_BPS / 10000):
            failures.append(f"p95_bundle_ask {p95_ask} > 1 - {MIN_P95_EDGE_BPS}bps (not typical-day arb)")
        if median_ask is None or median_ask > MAX_MEDIAN_BUNDLE:
            failures.append(f"median_bundle_ask {median_ask} > {MAX_MEDIAN_BUNDLE} (structurally over-priced)")

    if buckets_above_mve < MIN_BUCKETS_FOR_GO:
        failures.append(f"only {buckets_above_mve} buckets above MVE (< {MIN_BUCKETS_FOR_GO} = single-day fluke)")

    if not failures:
        return {"verdict": "GO", "failures": []}
    if cov < MIN_FIELD_COVERAGE_AMBIGUOUS:
        return {"verdict": "AMBIGUOUS", "failures": failures, "note": "field coverage too low to trust sum-Yes math"}
    return {"verdict": "NO_GO", "failures": failures}


def scan_rows(scan: Any) -> list[dict[str, Any]]:
    if isinstance(scan, list):
        return scan
    if not isinstance(scan, dict):
        return []
    if isinstance(scan.get("championships"), list):
        return scan["championships"]
    per_champ = scan.get("per_championship")
    if isinstance(per_champ, dict):
        rows = []
        for name, stats in per_champ.items():
            row = {"championship": name}
            if isinstance(stats, dict):
                row.update(stats)
            rows.append(row)
        return rows
    return []


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply Track A pre-committed criteria to a championship-mode arb scan output.")
    p.add_argument("--scan-output", type=Path, help="JSON file from `scan_live_arb --championship-mode --json-out`. Optional; if omitted, only field coverage is reported.")
    p.add_argument("--shortlist", type=Path, default=Path("data/scan_shortlist.json"))
    p.add_argument("--patterns-file", type=Path, default=None, help="Optional JSON dict of championship → regex.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    patterns = DEFAULT_CHAMPIONSHIP_PATTERNS
    if args.patterns_file:
        patterns = json.loads(args.patterns_file.read_text())

    coverage = field_coverage(args.shortlist, patterns)

    print("=== FIELD COVERAGE (current watchlist vs expected playoff field) ===")
    for name, info in coverage.items():
        print(f"  {name}: {info['captured']}/{info['expected']} = {info['coverage']:.0%}")
    print()

    if not args.scan_output:
        print("No --scan-output given. Run the scan first:")
        print("  python -m deploy.scan_live_arb --championship-mode --json-out data/track_a_scan.json")
        return

    scan = json.loads(args.scan_output.read_text())
    rows = scan_rows(scan)

    print("=== PER-CHAMPIONSHIP VERDICT ===")
    overall_go = False
    overall_ambiguous = False
    overall_no_go = False
    insufficient_count = 0
    for row in rows:
        name = row.get("championship") or row.get("name") or "UNKNOWN"
        verdict = evaluate_championship(row, coverage.get(name, {"coverage": 0.0}))
        marker = {"GO": "✓", "NO_GO": "✗", "AMBIGUOUS": "?", "INSUFFICIENT_DATA": "·"}[verdict["verdict"]]
        print(f"  [{marker}] {name}: {verdict['verdict']}")
        for f in verdict.get("failures", []):
            print(f"        - {f}")
        if verdict.get("note"):
            print(f"        note: {verdict['note']}")
        if verdict["verdict"] == "GO":
            overall_go = True
        elif verdict["verdict"] == "AMBIGUOUS":
            overall_ambiguous = True
        elif verdict["verdict"] == "NO_GO":
            overall_no_go = True
        elif verdict["verdict"] == "INSUFFICIENT_DATA":
            insufficient_count += 1
    print()

    if overall_go:
        verdict = "GO — at least one championship clears all criteria. Commit Phase 3 executable design."
    elif overall_ambiguous:
        verdict = "AMBIGUOUS — re-collect data; do NOT commit either way."
    elif overall_no_go:
        verdict = "NO_GO — retire structural arb in PLAN; engage Section 3 off-ramps."
    else:
        verdict = "INSUFFICIENT_DATA — every championship in the scan was below the usable coverage floor. Expand watchlist before running again."
    if insufficient_count:
        verdict += f" ({insufficient_count} championship(s) excluded for insufficient coverage.)"
    print(f"=== OVERALL: {verdict}")


if __name__ == "__main__":
    main()
