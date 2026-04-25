"""Scan live quote-collector output for complementary-bundle arb opportunities.

For each binary condition (Yes + No legs), finds time-aligned snapshots where
  bundle_ask = ask_yes + ask_no < 1   (buy both → guaranteed $1 payout → profit)
  bundle_bid = bid_yes + bid_no > 1   (sell both → guaranteed $1 liability → profit)

Applies the MVE gate: net edge (after fees) must clear `--min-net-edge-bps`.

Edges here assume taker fills at current best ask/bid. They do NOT model:
  - partial fills across the two legs (leg risk)
  - price drift between the two leg submissions (execution latency)
  - depth beyond top-of-book

Think of the output as an upper bound on opportunity density. Live execution
will realize less of it.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CHAMPIONSHIP_PATTERNS: dict[str, str] = {
    "NBA_FINALS_2026": r"win the 2026 NBA Finals\??$",
    "NHL_CUP_2026": r"win the 2026 NHL Stanley Cup\??$",
    "MLB_WORLD_SERIES_2026": r"win the 2026 (MLB |World Series)",
    "EPL_2025_26": r"win the 2025[-/]26 (English )?Premier League\??$",
}


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _bucket(ts: datetime, seconds: int) -> int:
    epoch = int(ts.timestamp())
    return epoch - (epoch % seconds)


def _load(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    out: list[dict[str, Any]] = []
    rejected = {"missing_quote": 0, "crossed_quote": 0, "out_of_band": 0}
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            bid_raw, ask_raw = r.get("best_bid"), r.get("best_ask")
            if bid_raw is None or ask_raw is None:
                rejected["missing_quote"] += 1
                continue
            bid, ask = float(bid_raw), float(ask_raw)
            if bid < 0 or ask < 0 or bid > 1 or ask > 1:
                rejected["out_of_band"] += 1
                continue
            if bid > ask:
                rejected["crossed_quote"] += 1
                continue
            ts_raw = r.get("timestamp")
            if not ts_raw:
                continue
            out.append(
                {
                    "condition_id": r.get("condition_id"),
                    "outcome": r.get("outcome"),
                    "slug": r.get("market_slug") or "",
                    "question": r.get("question") or "",
                    "bid": bid,
                    "ask": ask,
                    "ts": _parse_ts(ts_raw),
                }
            )
    return out, rejected


def scan(
    records: list[dict[str, Any]],
    bucket_seconds: int,
    fee_bps: float,
    min_net_edge_bps: float,
) -> dict[str, Any]:
    by_cond_bucket: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in records:
        cid = r["condition_id"]
        out = r["outcome"]
        if out not in {"Yes", "No"}:
            continue
        key = (cid, _bucket(r["ts"], bucket_seconds))
        existing = by_cond_bucket[key].get(out)
        if existing is None or r["ts"] < existing["ts"]:
            by_cond_bucket[key][out] = r

    opportunities: list[dict[str, Any]] = []
    per_cond: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "aligned_buckets": 0,
            "buy_above_mve": 0,
            "sell_above_mve": 0,
            "best_buy_edge_bps": None,
            "best_sell_edge_bps": None,
            "slug": "",
            "question": "",
        }
    )
    total_aligned = 0

    for (cid, bucket), legs in by_cond_bucket.items():
        if "Yes" not in legs or "No" not in legs:
            continue
        yes, no = legs["Yes"], legs["No"]
        total_aligned += 1
        stat = per_cond[cid]
        stat["aligned_buckets"] += 1
        stat["slug"] = yes["slug"] or no["slug"]
        stat["question"] = yes["question"] or no["question"]

        bundle_ask = yes["ask"] + no["ask"]
        bundle_bid = yes["bid"] + no["bid"]
        buy_edge_bps = (1.0 - bundle_ask) * 10_000 - fee_bps
        sell_edge_bps = (bundle_bid - 1.0) * 10_000 - fee_bps

        if buy_edge_bps > (stat["best_buy_edge_bps"] or -1e18):
            stat["best_buy_edge_bps"] = buy_edge_bps
        if sell_edge_bps > (stat["best_sell_edge_bps"] or -1e18):
            stat["best_sell_edge_bps"] = sell_edge_bps

        if buy_edge_bps >= min_net_edge_bps:
            stat["buy_above_mve"] += 1
            opportunities.append(
                {
                    "type": "buy_bundle",
                    "condition_id": cid,
                    "slug": stat["slug"],
                    "bucket_ts": datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat(),
                    "bundle_ask": round(bundle_ask, 6),
                    "net_edge_bps": round(buy_edge_bps, 2),
                    "yes_ask": yes["ask"],
                    "no_ask": no["ask"],
                }
            )
        if sell_edge_bps >= min_net_edge_bps:
            stat["sell_above_mve"] += 1
            opportunities.append(
                {
                    "type": "sell_bundle",
                    "condition_id": cid,
                    "slug": stat["slug"],
                    "bucket_ts": datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat(),
                    "bundle_bid": round(bundle_bid, 6),
                    "net_edge_bps": round(sell_edge_bps, 2),
                    "yes_bid": yes["bid"],
                    "no_bid": no["bid"],
                }
            )

    for stat in per_cond.values():
        for k in ("best_buy_edge_bps", "best_sell_edge_bps"):
            if stat[k] is not None:
                stat[k] = round(stat[k], 2)

    all_buy = [s["best_buy_edge_bps"] for s in per_cond.values() if s["best_buy_edge_bps"] is not None]
    all_sell = [s["best_sell_edge_bps"] for s in per_cond.values() if s["best_sell_edge_bps"] is not None]

    return {
        "params": {
            "bucket_seconds": bucket_seconds,
            "fee_bps": fee_bps,
            "min_net_edge_bps": min_net_edge_bps,
        },
        "summary": {
            "conditions_with_both_legs": len(per_cond),
            "total_aligned_buckets": total_aligned,
            "opportunities_above_mve": len(opportunities),
            "conditions_with_any_mve_hit": sum(
                1 for s in per_cond.values() if s["buy_above_mve"] or s["sell_above_mve"]
            ),
            "best_buy_edge_median_bps": statistics.median(all_buy) if all_buy else None,
            "best_sell_edge_median_bps": statistics.median(all_sell) if all_sell else None,
            "best_buy_edge_max_bps": max(all_buy) if all_buy else None,
            "best_sell_edge_max_bps": max(all_sell) if all_sell else None,
        },
        "per_condition": sorted(
            [{"condition_id": c, **s} for c, s in per_cond.items()],
            key=lambda r: max(r["best_buy_edge_bps"] or -1e9, r["best_sell_edge_bps"] or -1e9),
            reverse=True,
        ),
    }, opportunities


def _classify_championship(question: str, compiled: dict[str, re.Pattern[str]]) -> str | None:
    if not question:
        return None
    for name, regex in compiled.items():
        if regex.search(question):
            return name
    return None


def scan_championship(
    records: list[dict[str, Any]],
    bucket_seconds: int,
    fee_bps: float,
    min_net_edge_bps: float,
    patterns: dict[str, str],
    min_field_fraction: float,
) -> dict[str, Any]:
    """Cross-condition outright bundle scanner.

    Hypothesis: across all "Will TEAM_X win the championship?" binaries for one
    championship, the sum of YES asks (resp. bids) should be close to $1 because
    exactly one team wins. If sum_yes_ask < 1 there is a buy-the-field arb;
    if sum_yes_bid > 1 there is a sell-the-field arb.
    """
    compiled = {name: re.compile(pat, re.IGNORECASE) for name, pat in patterns.items()}

    by_champ_bucket: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    field_counts: dict[str, set[str]] = defaultdict(set)

    for r in records:
        if r.get("outcome") != "Yes":
            continue
        champ = _classify_championship(r.get("question") or "", compiled)
        if not champ:
            continue
        cid = r.get("condition_id")
        if not cid:
            continue
        field_counts[champ].add(cid)
        key = (champ, _bucket(r["ts"], bucket_seconds))
        ex = by_champ_bucket[key].get(cid)
        if ex is None or r["ts"] < ex["ts"]:
            by_champ_bucket[key][cid] = r

    per_champ: dict[str, dict[str, Any]] = {}
    opportunities: list[dict[str, Any]] = []

    for champ, expected in field_counts.items():
        expected_n = len(expected)
        min_legs = max(1, int(round(expected_n * min_field_fraction)))
        ask_edges_bps: list[float] = []
        bid_edges_bps: list[float] = []
        buy_above = 0
        sell_above = 0
        partial_buckets = 0
        full_buckets = 0
        best_buy_bucket: dict[str, Any] | None = None
        best_sell_bucket: dict[str, Any] | None = None

        for (c, bucket), legs in by_champ_bucket.items():
            if c != champ:
                continue
            n_legs = len(legs)
            if n_legs < min_legs:
                partial_buckets += 1
                continue
            if n_legs == expected_n:
                full_buckets += 1
            sum_ask = sum(leg["ask"] for leg in legs.values())
            sum_bid = sum(leg["bid"] for leg in legs.values())
            buy_edge_bps = (1.0 - sum_ask) * 10_000 - fee_bps
            sell_edge_bps = (sum_bid - 1.0) * 10_000 - fee_bps
            ask_edges_bps.append(buy_edge_bps)
            bid_edges_bps.append(sell_edge_bps)
            ts_iso = datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat()
            if buy_edge_bps >= min_net_edge_bps:
                buy_above += 1
                opp = {
                    "type": "buy_field",
                    "championship": champ,
                    "bucket_ts": ts_iso,
                    "n_legs": n_legs,
                    "expected_n_legs": expected_n,
                    "sum_yes_ask": round(sum_ask, 6),
                    "net_edge_bps": round(buy_edge_bps, 2),
                }
                opportunities.append(opp)
                if best_buy_bucket is None or buy_edge_bps > best_buy_bucket["net_edge_bps"]:
                    best_buy_bucket = opp
            if sell_edge_bps >= min_net_edge_bps:
                sell_above += 1
                opp = {
                    "type": "sell_field",
                    "championship": champ,
                    "bucket_ts": ts_iso,
                    "n_legs": n_legs,
                    "expected_n_legs": expected_n,
                    "sum_yes_bid": round(sum_bid, 6),
                    "net_edge_bps": round(sell_edge_bps, 2),
                }
                opportunities.append(opp)
                if best_sell_bucket is None or sell_edge_bps > best_sell_bucket["net_edge_bps"]:
                    best_sell_bucket = opp

        per_champ[champ] = {
            "expected_field_size": expected_n,
            "min_legs_required": min_legs,
            "full_buckets": full_buckets,
            "qualifying_buckets": len(ask_edges_bps),
            "skipped_partial_buckets": partial_buckets,
            "buy_above_mve": buy_above,
            "sell_above_mve": sell_above,
            "buy_edge_bps": _quantile_summary(ask_edges_bps),
            "sell_edge_bps": _quantile_summary(bid_edges_bps),
            "best_buy_bucket": best_buy_bucket,
            "best_sell_bucket": best_sell_bucket,
        }

    return {
        "params": {
            "bucket_seconds": bucket_seconds,
            "fee_bps": fee_bps,
            "min_net_edge_bps": min_net_edge_bps,
            "min_field_fraction": min_field_fraction,
            "patterns": patterns,
        },
        "summary": {
            "championships_scanned": len(per_champ),
            "total_opportunities_above_mve": len(opportunities),
            "any_mve_hit": any(
                v["buy_above_mve"] or v["sell_above_mve"] for v in per_champ.values()
            ),
        },
        "per_championship": per_champ,
    }, opportunities


def _quantile_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "median": None, "max": None, "min": None, "p95": None}
    p95 = statistics.quantiles(values, n=20)[18] if len(values) >= 20 else None
    return {
        "count": len(values),
        "median": round(statistics.median(values), 2),
        "max": round(max(values), 2),
        "min": round(min(values), 2),
        "p95": round(p95, 2) if p95 is not None else None,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="data/quote_collection/run.jsonl", type=Path)
    p.add_argument("--bucket-seconds", default=60, type=int)
    p.add_argument("--fee-bps", default=0.0, type=float)
    p.add_argument("--min-net-edge-bps", default=30.0, type=float)
    p.add_argument("--top", default=20, type=int)
    p.add_argument("--dump-opportunities", action="store_true")
    p.add_argument(
        "--championship-mode",
        action="store_true",
        help="Run cross-condition outright (sum-of-yes-across-field) scan instead of single-condition Yes+No bundle scan.",
    )
    p.add_argument(
        "--championship-patterns-file",
        type=Path,
        help="Optional JSON file overriding DEFAULT_CHAMPIONSHIP_PATTERNS.",
    )
    p.add_argument(
        "--min-field-fraction",
        type=float,
        default=0.85,
        help="Championship mode: require this fraction of the expected field present in a bucket.",
    )
    args = p.parse_args()

    records, rejected = _load(args.input)

    if args.championship_mode:
        if args.championship_patterns_file:
            patterns = json.loads(args.championship_patterns_file.read_text(encoding="utf-8"))
        else:
            patterns = DEFAULT_CHAMPIONSHIP_PATTERNS
        report, opportunities = scan_championship(
            records,
            args.bucket_seconds,
            args.fee_bps,
            args.min_net_edge_bps,
            patterns,
            args.min_field_fraction,
        )
    else:
        report, opportunities = scan(
            records, args.bucket_seconds, args.fee_bps, args.min_net_edge_bps
        )

    report["data_quality"] = {
        "rows_kept": len(records),
        "rows_rejected": rejected,
    }
    report["top_opportunities"] = sorted(
        opportunities, key=lambda r: r["net_edge_bps"], reverse=True
    )[: max(args.top, 0)]
    if args.dump_opportunities:
        report["all_opportunities"] = opportunities
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
