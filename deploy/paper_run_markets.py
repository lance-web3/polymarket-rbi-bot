from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.market_filter import MarketFilter
from data.market_discovery import GammaMarketDiscoveryClient, extract_yes_token
from polymarket_rbi_bot.config import BotConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan eligible Polymarket markets, rank them, and paper-run the strongest candidates through deploy.run_live.")
    parser.add_argument("--limit", type=int, default=10, help="How many live markets to inspect from Gamma.")
    parser.add_argument("--max-runs", type=int, default=3, help="Maximum ranked eligible markets to paper-run.")
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

    repo_root = Path(__file__).resolve().parents[1]
    candidates = []
    runs = []

    for market in discovery.list_markets(limit=args.limit, closed=False, archived=False):
        yes_token = extract_yes_token(market)
        if not yes_token:
            continue
        eligibility = market_filter.evaluate(market, yes_token["token_id"])
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        candidates.append(
            {
                "question": market.get("question"),
                "condition_id": condition_id,
                "token_id": yes_token["token_id"],
                "mid_price": yes_token.get("price"),
                "eligible": eligibility.eligible,
                "reason": eligibility.reason,
                **eligibility.metrics,
            }
        )

    ranked_candidates = sorted(
        candidates,
        key=lambda row: (
            1 if row.get("eligible") else 0,
            float(row.get("quality_score") or 0.0),
            float(row.get("liquidity") or 0.0),
            float(row.get("abs_return_bps_24h") or 0.0),
        ),
        reverse=True,
    )

    for rank, market in enumerate(ranked_candidates, start=1):
        if len(runs) >= args.max_runs:
            break
        if not market.get("eligible"):
            continue
        condition_id = str(market.get("condition_id") or "")
        mid_price = market.get("mid_price")
        if not condition_id or mid_price is None:
            continue

        cmd = [
            sys.executable,
            "-m",
            "deploy.run_live",
            "--condition-id",
            condition_id,
            "--token-id",
            str(market["token_id"]),
            "--mid-price",
            str(mid_price),
        ]
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        payload = {
            "rank": rank,
            "question": market.get("question"),
            "condition_id": condition_id,
            "token_id": market["token_id"],
            "mid_price": mid_price,
            "quality_score": market.get("quality_score"),
            "quality_tier": market.get("quality_tier"),
            "score_breakdown": market.get("score_breakdown"),
            "returncode": proc.returncode,
        }
        if proc.stdout.strip():
            try:
                payload["result"] = json.loads(proc.stdout)
                payload["status"] = payload["result"].get("status")
                payload["reason"] = payload["result"].get("reason")
            except json.JSONDecodeError:
                payload["stdout"] = proc.stdout.strip()
        if proc.stderr.strip():
            payload["stderr"] = proc.stderr.strip()
        runs.append(payload)

    print(
        json.dumps(
            {
                "count": len(runs),
                "ranked_candidates": ranked_candidates[: max(args.max_runs * 3, 10)],
                "runs": runs,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
