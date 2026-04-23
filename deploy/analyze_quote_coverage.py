"""Quote-collector coverage analyzer.

Reads `data/quote_collection/run.jsonl` (or a user-specified JSONL), emits:
  - row/token counts, time window, elapsed hours
  - bid/ask presence, CLOB-source share
  - spread_bps distribution (median/p75/p95)
  - per-token staleness: share of consecutive snapshots where mid didn't change
  - a suggested Phase-2 universe pruning filter (tokens with p75 spread under threshold
    AND staleness ratio under threshold)

Used at every Phase-1 check-in and again when pruning the universe for Phase 2.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def analyze(path: Path, spread_cap_bps: float, staleness_cap: float) -> dict:
    rows_total = 0
    rows_with_bid_ask = 0
    rows_clob = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    per_token_spreads: dict[str, list[float]] = defaultdict(list)
    per_token_mids: dict[str, list[float]] = defaultdict(list)
    per_token_slug: dict[str, str] = {}

    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            rows_total += 1
            tid = rec.get("token_id")
            if not tid:
                continue
            per_token_slug.setdefault(tid, rec.get("market_slug") or "")
            if rec.get("quote_source") == "clob_order_book":
                rows_clob += 1
            bid, ask = rec.get("best_bid"), rec.get("best_ask")
            mid = rec.get("mid")
            spread_bps = rec.get("spread_bps")
            if bid is not None and ask is not None:
                rows_with_bid_ask += 1
            if spread_bps is not None:
                per_token_spreads[tid].append(float(spread_bps))
            if mid is not None:
                per_token_mids[tid].append(float(mid))
            ts = rec.get("timestamp")
            if ts:
                dt = _parse_ts(ts)
                if first_ts is None or dt < first_ts:
                    first_ts = dt
                if last_ts is None or dt > last_ts:
                    last_ts = dt

    elapsed_hours = (
        (last_ts - first_ts).total_seconds() / 3600.0 if first_ts and last_ts else 0.0
    )

    token_stats: list[dict] = []
    for tid, spreads in per_token_spreads.items():
        mids = per_token_mids.get(tid, [])
        unchanged = sum(1 for a, b in zip(mids, mids[1:]) if a == b)
        stale = unchanged / max(len(mids) - 1, 1) if len(mids) > 1 else None
        p75 = statistics.quantiles(spreads, n=4)[2] if len(spreads) >= 4 else None
        token_stats.append(
            {
                "token_id": tid,
                "slug": per_token_slug.get(tid, ""),
                "n_snapshots": len(spreads),
                "median_spread_bps": statistics.median(spreads) if spreads else None,
                "p75_spread_bps": p75,
                "staleness_ratio": stale,
            }
        )

    all_spreads = [s for tid in per_token_spreads for s in per_token_spreads[tid]]
    spread_quartiles = statistics.quantiles(all_spreads, n=20) if len(all_spreads) >= 20 else []
    all_stale = [t["staleness_ratio"] for t in token_stats if t["staleness_ratio"] is not None]

    keep = [
        t
        for t in token_stats
        if t["p75_spread_bps"] is not None
        and t["p75_spread_bps"] < spread_cap_bps
        and t["staleness_ratio"] is not None
        and t["staleness_ratio"] < staleness_cap
    ]

    return {
        "rows_total": rows_total,
        "rows_with_bid_ask": rows_with_bid_ask,
        "rows_clob": rows_clob,
        "clob_source_share": rows_clob / rows_total if rows_total else 0.0,
        "bid_ask_coverage": rows_with_bid_ask / rows_total if rows_total else 0.0,
        "token_count": len(per_token_spreads),
        "first_ts": first_ts.isoformat() if first_ts else None,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "elapsed_hours": elapsed_hours,
        "spread_bps_median": statistics.median(all_spreads) if all_spreads else None,
        "spread_bps_p75": spread_quartiles[14] if spread_quartiles else None,
        "spread_bps_p95": spread_quartiles[18] if spread_quartiles else None,
        "token_staleness_median": statistics.median(all_stale) if all_stale else None,
        "prune_filter": {
            "spread_cap_bps": spread_cap_bps,
            "staleness_cap": staleness_cap,
            "kept_tokens": len(keep),
            "kept_of_total": f"{len(keep)}/{len(token_stats)}",
        },
        "kept_token_slugs": sorted(
            {t["slug"] for t in keep if t["slug"]},
        ),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="data/quote_collection/run.jsonl", type=Path)
    p.add_argument("--spread-cap-bps", default=500.0, type=float)
    p.add_argument("--staleness-cap", default=0.95, type=float)
    p.add_argument("--show-kept-slugs", action="store_true")
    args = p.parse_args()

    report = analyze(args.input, args.spread_cap_bps, args.staleness_cap)
    if not args.show_kept_slugs:
        report.pop("kept_token_slugs", None)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
