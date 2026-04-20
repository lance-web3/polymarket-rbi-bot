from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.market_filter import MarketFilter
from data.market_discovery import GammaMarketDiscoveryClient, extract_yes_token
from polymarket_rbi_bot.config import BotConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Polymarket markets and rank them by tradability.")
    parser.add_argument("--limit", type=int, default=25, help="How many live markets to inspect from Gamma.")
    parser.add_argument("--top", type=int, default=10, help="How many ranked markets to surface in the shortlist.")
    args = parser.parse_args()

    config = BotConfig.from_env()
    discovery = GammaMarketDiscoveryClient(host=config.gamma_host)
    market_filter = MarketFilter(
        min_liquidity=config.min_market_liquidity,
        min_history_points=config.min_market_history_points,
        min_price=config.min_price,
        max_price=config.max_price,
        min_abs_return_bps_24h=config.min_abs_return_bps_24h,
        excluded_keywords=config.excluded_keywords,
        strict_mode=config.strict_strategy_mode,
        strict_min_price=config.strict_min_price,
        strict_max_price=config.strict_max_price,
        strict_excluded_keywords=config.strict_excluded_keywords,
        market_family_mode=config.market_family_mode,
        allowed_market_families=config.allowed_market_families,
        blocked_market_families=config.blocked_market_families,
        family_allow_keywords=config.family_allow_keywords,
        family_block_keywords=config.family_block_keywords,
        llm_market_classifier_path=config.llm_market_classifier_path,
    )

    rows = []
    for market in discovery.list_markets(limit=args.limit, closed=False, archived=False):
        yes_token = extract_yes_token(market)
        if not yes_token:
            continue
        result = market_filter.evaluate(market, yes_token["token_id"])
        row = {
            "question": market.get("question"),
            "condition_id": market.get("conditionId") or market.get("condition_id"),
            "token_id": yes_token["token_id"],
            "outcome": yes_token["outcome"],
            "eligible": result.eligible,
            "reason": result.reason,
            **result.metrics,
        }
        rows.append(row)

    ranked = sorted(
        rows,
        key=lambda row: (
            1 if row.get("eligible") else 0,
            float(row.get("quality_score") or 0.0),
            float(row.get("liquidity") or 0.0),
            float(row.get("abs_return_bps_24h") or 0.0),
        ),
        reverse=True,
    )

    shortlist = []
    for index, row in enumerate(ranked[: max(args.top, 0)], start=1):
        shortlist.append(
            {
                "rank": index,
                "question": row.get("question"),
                "token_id": row.get("token_id"),
                "condition_id": row.get("condition_id"),
                "eligible": row.get("eligible"),
                "quality_score": row.get("quality_score"),
                "quality_tier": row.get("quality_tier"),
                "liquidity": row.get("liquidity"),
                "current_price": row.get("current_price"),
                "spread_bps": row.get("spread_bps"),
                "abs_return_bps_24h": row.get("abs_return_bps_24h"),
                "history_points": row.get("history_points"),
                "reason": row.get("reason"),
                "best_feature": ((row.get("ranking_summary") or {}).get("best_feature")),
                "weakest_feature": ((row.get("ranking_summary") or {}).get("weakest_feature")),
            }
        )

    payload = {
        "summary": {
            "markets_scanned": len(rows),
            "eligible_count": sum(1 for row in rows if row.get("eligible")),
            "ineligible_count": sum(1 for row in rows if not row.get("eligible")),
            "top_requested": args.top,
        },
        "shortlist": shortlist,
        "ranked_markets": ranked,
    }

    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
