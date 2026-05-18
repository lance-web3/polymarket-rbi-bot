"""Discover non-sports markets to seed the corpus needed for Phase 2's
honest-test gate.

PLAN gap (recorded 2026-04-25): "before declaring directional Edge #2
falsified globally we need (a) a non-sports CSV corpus and (b) a strict
profile that actually trades. Otherwise Phase 2 result aligns with the
99% mid-staleness prior — there's nothing for directional signals to
predict."

This script pulls live Gamma markets, classifies each by family using
`MarketFilter._heuristic_family`, then picks the top-N most-liquid markets
per non-sports family. Output is a JSON shortlist matching the schema of
`data/scan_shortlist.json`. Output is **not** merged into the existing
watchlist by default — these markets have very different microstructure
than sports outrights and we don't want to dilute the Track A field
coverage check.

Workflow:
    # 1. Discover candidates (dry run, top 5 per family by liquidity).
    python -m deploy.discover_nonsports_corpus

    # 2. Inspect, then write the shortlist.
    python -m deploy.discover_nonsports_corpus --apply \
        --top-per-family 8 --min-liquidity 50000

    # 3. (Future) Either run a separate collector against this shortlist
    #    or merge into the main watchlist for combined coverage.

Default thresholds tilt high-liquidity / wide-coverage to give the Phase 2
re-test the best chance of finding directional structure. Lower
--min-liquidity if you want a broader sample including thin markets.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.market_filter import MarketFilter
from data.market_discovery import GammaMarketDiscoveryClient, extract_yes_token
from polymarket_rbi_bot.config import BotConfig

# Families we want to *include* (non-sports). Sports outrights deliberately
# excluded — that's what we already have in the existing 10 CSVs / 72-condition
# watchlist.
TARGET_FAMILIES = {
    "legal_regulatory",
    "news_breaking",
    "crypto_outright",
    "scheduled_event",
    "event_resolution",
}


def build_filter(config: BotConfig) -> MarketFilter:
    return MarketFilter(
        min_liquidity=0,  # we apply liquidity threshold downstream
        min_history_points=0,
        min_price=0.01,
        max_price=0.99,
        min_abs_return_bps_24h=0,
        excluded_keywords=config.excluded_keywords,
        strict_mode=False,
        market_family_mode="balanced",
        allowed_market_families=set(),
        blocked_market_families=set(),
        family_allow_keywords=set(),
        family_block_keywords=set(),
        llm_market_classifier_path=config.llm_market_classifier_path,
    )


def liquidity_of(market: dict[str, Any]) -> float:
    for key in ("liquidity", "liquidityNum", "marketLiquidity"):
        v = market.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def build_entry(market: dict[str, Any], family: str, family_reason: str) -> dict[str, Any]:
    return {
        "condition_id": market.get("conditionId") or market.get("condition_id"),
        "question": market.get("question"),
        "market_slug": market.get("slug"),
        "liquidity": liquidity_of(market),
        "market_family": family,
        "family_reason": family_reason,
        "added_via": "discover_nonsports_corpus",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=1000,
                        help="Markets to fetch from Gamma (~1000 max per call).")
    parser.add_argument("--top-per-family", type=int, default=8,
                        help="Top-N most-liquid markets to keep per family.")
    parser.add_argument("--min-liquidity", type=float, default=20000.0,
                        help="Skip markets below this liquidity threshold.")
    parser.add_argument("--out", type=Path, default=Path("data/scan_shortlist_nonsports.json"))
    parser.add_argument("--apply", action="store_true",
                        help="Write the shortlist file. Without it, prints summary only.")
    args = parser.parse_args()

    config = BotConfig.from_env()
    discovery = GammaMarketDiscoveryClient(host=config.gamma_host)
    market_filter = build_filter(config)

    print(f"Fetching up to {args.limit} live markets from Gamma...")
    markets = discovery.list_markets(limit=args.limit, closed=False, archived=False)
    print(f"  Got {len(markets)} markets.")

    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_no_yes = 0
    skipped_low_liq = 0
    skipped_sports = 0

    for market in markets:
        yes_token = extract_yes_token(market)
        if not yes_token:
            skipped_no_yes += 1
            continue
        liq = liquidity_of(market)
        if liq < args.min_liquidity:
            skipped_low_liq += 1
            continue
        family, reason = market_filter._heuristic_family(market)
        if family == "sports_outright":
            skipped_sports += 1
            continue
        if family not in TARGET_FAMILIES:
            continue
        entry = build_entry(market, family, reason)
        by_family[family].append(entry)

    chosen: list[dict[str, Any]] = []
    summary_per_family: dict[str, dict[str, Any]] = {}
    for family, candidates in sorted(by_family.items()):
        candidates.sort(key=lambda c: c["liquidity"], reverse=True)
        kept = candidates[: args.top_per_family]
        chosen.extend(kept)
        summary_per_family[family] = {
            "candidates": len(candidates),
            "kept": len(kept),
            "min_liquidity_kept": min((c["liquidity"] for c in kept), default=0),
            "max_liquidity_kept": max((c["liquidity"] for c in kept), default=0),
            "examples": [c["question"] for c in kept[:3]],
        }

    summary = {
        "scanned": len(markets),
        "skipped_no_yes": skipped_no_yes,
        "skipped_below_min_liquidity": skipped_low_liq,
        "skipped_sports_outright": skipped_sports,
        "min_liquidity_threshold": args.min_liquidity,
        "top_per_family": args.top_per_family,
        "by_family": summary_per_family,
        "total_chosen": len(chosen),
        "applied": args.apply,
        "out_path": str(args.out),
    }

    if args.apply and chosen:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": summary,
            "shortlist": chosen,
        }
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {len(chosen)} entries to {args.out}")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
