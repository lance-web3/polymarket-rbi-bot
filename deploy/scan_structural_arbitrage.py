from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_discovery import GammaMarketDiscoveryClient
from data.structural_arb import analyze_live_bundle_markets, analyze_quote_backtest_bundles, load_markets_from_json
from polymarket_rbi_bot.config import BotConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan structural bundle/no-arbitrage opportunities for research.")
    parser.add_argument("--mode", choices=["live", "quote-backtests"], default="quote-backtests")
    parser.add_argument("--out", default="data/structural_arb_scan.json", help="JSON output path.")
    parser.add_argument("--top", type=int, default=25, help="How many opportunities/conditions to highlight.")
    parser.add_argument("--input-market-json", help="Optional market fixture JSON for live mode instead of Gamma API.")
    parser.add_argument("--limit", type=int, default=200, help="Live mode: how many Gamma markets to inspect.")
    parser.add_argument("--min-liquidity", type=float, default=0.0, help="Live mode: skip markets below this liquidity.")
    parser.add_argument("--underround-buffer", type=float, default=0.01, help="Live mode: minimum reference underround to flag.")
    parser.add_argument("--overround-buffer", type=float, default=0.01, help="Live mode: minimum reference overround to flag.")
    parser.add_argument("--csv-dir", default="data/quote_backtests", help="Quote-backtests mode: per-token CSV directory.")
    parser.add_argument("--ask-buffer", type=float, default=0.01, help="Quote-backtests mode: require bundle ask < 1-buffer.")
    parser.add_argument("--bid-buffer", type=float, default=0.01, help="Quote-backtests mode: require bundle bid > 1+buffer.")
    parser.add_argument("--min-rows-per-condition", type=int, default=3, help="Quote-backtests mode: minimum aligned rows to include a condition summary.")
    parser.add_argument("--min-reference-sum", type=float, default=0.95, help="Quote-backtests mode: require complementary reference close/mid sum to be at least this value.")
    parser.add_argument("--max-reference-sum", type=float, default=1.05, help="Quote-backtests mode: require complementary reference close/mid sum to be at most this value.")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.mode == "live":
        if args.input_market_json:
            markets = load_markets_from_json(args.input_market_json)
        else:
            config = BotConfig.from_env()
            discovery = GammaMarketDiscoveryClient(host=config.gamma_host)
            markets = discovery.list_markets(limit=args.limit, closed=False, archived=False)
        payload = analyze_live_bundle_markets(
            markets,
            min_liquidity=args.min_liquidity,
            underround_buffer=args.underround_buffer,
            overround_buffer=args.overround_buffer,
            top=args.top,
        )
    else:
        payload = analyze_quote_backtest_bundles(
            args.csv_dir,
            ask_buffer=args.ask_buffer,
            bid_buffer=args.bid_buffer,
            min_rows_per_condition=args.min_rows_per_condition,
            min_reference_sum=args.min_reference_sum,
            max_reference_sum=args.max_reference_sum,
            top=args.top,
        )

    payload["combinatorial_scaffold"] = {
        "status": "not_implemented_yet",
        "idea": "Future step: link logically dependent conditions across separate markets once we have robust dependency mapping and executable pricing checks.",
    }

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
