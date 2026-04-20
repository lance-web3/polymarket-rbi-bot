from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_discovery import GammaMarketDiscoveryClient
from data.quote_collector import QuoteSnapshotCollector
from polymarket_rbi_bot.config import BotConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll current Polymarket/Gamma quote snapshots into a local JSONL research file."
    )
    parser.add_argument("--token-id", action="append", default=[], help="Target token id to collect. Repeatable.")
    parser.add_argument("--condition-id", action="append", default=[], help="Target condition id to collect. Repeatable; expands to all tokens in the condition.")
    parser.add_argument(
        "--watchlist",
        help="Path to a watchlist file (.json/.jsonl/.csv/.txt). Can also be deploy.scan_markets output with shortlist/ranked_markets.",
    )
    parser.add_argument("--output", default="data/quote_snapshots.jsonl", help="JSONL output path.")
    parser.add_argument("--interval-seconds", type=float, default=30.0, help="Polling interval in seconds.")
    parser.add_argument("--iterations", type=int, default=None, help="Number of polling loops to run. Omit to run indefinitely.")
    parser.add_argument("--lookup-limit", type=int, default=1000, help="How many live Gamma markets to fetch per poll.")
    parser.add_argument("--use-clob-order-books", action="store_true", help="Fetch executable per-token bid/ask from the CLOB public order book when available.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite the output file instead of appending.")
    parser.add_argument("--print-targets", action="store_true", help="Print the resolved targets before collection starts.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.token_id and not args.condition_id and not args.watchlist:
        parser.error("provide at least one --token-id, --condition-id, or --watchlist")

    config = BotConfig.from_env()
    collector = QuoteSnapshotCollector(
        discovery=GammaMarketDiscoveryClient(host=config.gamma_host),
        lookup_limit=args.lookup_limit,
        clob_host=config.host,
        chain_id=config.chain_id,
        use_clob_order_books=args.use_clob_order_books,
    )
    targets = collector.resolve_targets(
        token_ids=args.token_id,
        condition_ids=args.condition_id,
        watchlist_path=args.watchlist,
    )
    if not targets:
        parser.error("no collectable token targets were resolved from the provided ids/watchlist")

    if args.print_targets:
        print(json.dumps({"resolved_targets": [asdict(target) for target in targets]}, indent=2))

    summary = collector.run(
        targets=targets,
        output_path=args.output,
        interval_seconds=args.interval_seconds,
        iterations=args.iterations,
        append=not args.overwrite,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
