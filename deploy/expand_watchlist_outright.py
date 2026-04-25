"""Expand the quote-collector watchlist to capture full outright fields.

Single-binary bundle arb (Yes+No=$1) was falsified on the existing 50-token
sports universe. The remaining structural-arb angle is "sum of all Yes asks
across an entire field < $1" (buy the field → guaranteed $1 → profit). To
test that, we need every contender's Yes/No binary in the watchlist, not
just the most-liquid one or two.

This script:
  1. Pulls live Gamma markets,
  2. Matches questions against a config list of championship regexes,
  3. Dedups against the existing watchlist,
  4. Writes back to data/scan_shortlist.json (with --apply).

The collector resolves condition-id-only entries automatically (see
data/quote_collector.py:139-170), so we only need to write condition_id +
metadata fields per new market.
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

from data.market_discovery import GammaMarketDiscoveryClient
from polymarket_rbi_bot.config import BotConfig

DEFAULT_PATTERNS: dict[str, str] = {
    "NBA_FINALS_2026": r"win the 2026 NBA Finals\??$",
    "NHL_CUP_2026": r"win the 2026 NHL Stanley Cup\??$",
    "MLB_WORLD_SERIES_2026": r"win the 2026 (MLB |World Series)",
    "EPL_2025_26": r"win the 2025[-/]26 (English )?Premier League\??$",
    "NCAA_FOOTBALL_2025_26": r"win the 2025[-/]26 (NCAA |college )football national championship",
}


def load_existing(path: Path) -> tuple[dict[str, Any], set[str]]:
    if not path.exists():
        return {"shortlist": []}, set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        payload = {"shortlist": payload}
    existing_cids = {
        str(item.get("condition_id") or item.get("conditionId") or "").strip()
        for item in payload.get("shortlist", [])
        if item
    }
    existing_cids.discard("")
    return payload, existing_cids


def match_markets(
    markets: list[dict[str, Any]], patterns: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    compiled = {name: re.compile(pat, re.IGNORECASE) for name, pat in patterns.items()}
    by_pattern: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        question = (market.get("question") or "").strip()
        if not question:
            continue
        for name, regex in compiled.items():
            if regex.search(question):
                by_pattern[name].append(market)
                break
    return by_pattern


def build_entry(market: dict[str, Any], pattern_name: str) -> dict[str, Any]:
    return {
        "condition_id": market.get("conditionId") or market.get("condition_id"),
        "question": market.get("question"),
        "market_slug": market.get("slug"),
        "liquidity": market.get("liquidity"),
        "added_via": "expand_watchlist_outright",
        "championship_pattern": pattern_name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watchlist",
        default="data/scan_shortlist.json",
        type=Path,
        help="Existing watchlist file to merge into.",
    )
    parser.add_argument(
        "--patterns-file",
        type=Path,
        help="Optional JSON file overriding DEFAULT_PATTERNS (mapping name -> regex).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="How many Gamma markets to scan. Gamma's max per call is around 1000.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the merged watchlist back. Without this flag, only prints a dry-run summary.",
    )
    args = parser.parse_args()

    if args.patterns_file:
        patterns = json.loads(args.patterns_file.read_text(encoding="utf-8"))
    else:
        patterns = DEFAULT_PATTERNS

    config = BotConfig.from_env()
    client = GammaMarketDiscoveryClient(host=config.gamma_host)
    markets = client.list_markets(limit=args.limit, closed=False, archived=False)

    payload, existing_cids = load_existing(args.watchlist)

    by_pattern = match_markets(markets, patterns)

    summary_per_pattern: dict[str, dict[str, int]] = {}
    new_entries: list[dict[str, Any]] = []
    for name, matched in by_pattern.items():
        seen = 0
        added = 0
        for market in matched:
            cid = str(market.get("conditionId") or market.get("condition_id") or "").strip()
            if not cid:
                continue
            seen += 1
            if cid in existing_cids:
                continue
            new_entries.append(build_entry(market, name))
            existing_cids.add(cid)
            added += 1
        summary_per_pattern[name] = {"matched": seen, "added": added}

    summary = {
        "patterns": patterns,
        "markets_scanned": len(markets),
        "existing_count": len(payload.get("shortlist", [])),
        "by_pattern": summary_per_pattern,
        "new_entries": len(new_entries),
        "after_merge_count": len(payload.get("shortlist", [])) + len(new_entries),
        "applied": args.apply,
    }

    if args.apply and new_entries:
        merged_shortlist = list(payload.get("shortlist", [])) + new_entries
        payload["shortlist"] = merged_shortlist
        payload.setdefault("summary", {})
        payload["summary"]["last_outright_expansion"] = {
            "new_entries": len(new_entries),
            "by_pattern": summary_per_pattern,
        }
        args.watchlist.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
