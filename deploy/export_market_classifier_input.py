from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.market_filter import MarketFilter
from data.market_discovery import GammaMarketDiscoveryClient, extract_yes_token, parse_jsonish_list
from polymarket_rbi_bot.config import BotConfig


def build_market_payload(market: dict[str, Any], eligibility: dict[str, Any], yes_token: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(eligibility)
    market_family = metrics.get('market_family') or {}
    return {
        'condition_id': str(market.get('conditionId') or market.get('condition_id') or ''),
        'token_id': str(yes_token.get('token_id') or ''),
        'slug': market.get('slug'),
        'question': market.get('question'),
        'description': market.get('description'),
        'category': market.get('category'),
        'subcategory': market.get('subcategory'),
        'end_date_iso': market.get('endDate') or market.get('end_date_iso') or market.get('endDateIso'),
        'outcomes': parse_jsonish_list(market.get('outcomes')),
        'outcome_prices': parse_jsonish_list(market.get('outcomePrices')),
        'yes_outcome': yes_token.get('outcome'),
        'yes_price': yes_token.get('price'),
        'liquidity': metrics.get('liquidity'),
        'best_bid': metrics.get('best_bid'),
        'best_ask': metrics.get('best_ask'),
        'spread_bps': metrics.get('spread_bps'),
        'history_points': metrics.get('history_points'),
        'abs_return_bps_24h': metrics.get('abs_return_bps_24h'),
        'realized_volatility_bps': metrics.get('realized_volatility_bps'),
        'movement_consistency': metrics.get('movement_consistency'),
        'quality_score': metrics.get('quality_score'),
        'quality_tier': metrics.get('quality_tier'),
        'score_breakdown': metrics.get('score_breakdown'),
        'eligibility_reason': eligibility.get('reason'),
        'eligible_now': eligibility.get('eligible'),
        'heuristic_family': market_family.get('heuristic_family'),
        'current_family': market_family.get('family'),
        'current_classifier_decision': market_family.get('decision'),
        'current_classifier_reason': market_family.get('reason'),
        'tags': market.get('tags') or [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Export candidate Polymarket markets for offline/OpenAI classification.')
    parser.add_argument('--limit', type=int, default=25, help='How many live markets to inspect from Gamma.')
    parser.add_argument('--top', type=int, default=25, help='How many ranked markets to export after scoring.')
    parser.add_argument('--output', type=str, default=None, help='Where to write the JSON input artifact.')
    args = parser.parse_args()

    config = BotConfig.from_env()
    output_path = Path(args.output or config.openai_classifier_input_path)
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

    rows: list[dict[str, Any]] = []
    for market in discovery.list_markets(limit=args.limit, closed=False, archived=False):
        yes_token = extract_yes_token(market)
        if not yes_token:
            continue
        result = market_filter.evaluate(market, yes_token['token_id'])
        row = build_market_payload(
            market,
            {
                'eligible': result.eligible,
                'reason': result.reason,
                **result.metrics,
            },
            yes_token,
        )
        rows.append(row)

    ranked = sorted(
        rows,
        key=lambda row: (
            1 if row.get('eligible_now') else 0,
            float(row.get('quality_score') or 0.0),
            float(row.get('liquidity') or 0.0),
            float(row.get('abs_return_bps_24h') or 0.0),
        ),
        reverse=True,
    )
    selected = ranked[: max(args.top, 0)]

    payload = {
        'exported_at': datetime.now(tz=timezone.utc).isoformat(),
        'source': 'deploy.export_market_classifier_input',
        'summary': {
            'markets_scanned': len(rows),
            'markets_exported': len(selected),
            'eligible_exported': sum(1 for row in selected if row.get('eligible_now')),
            'limit_requested': args.limit,
            'top_requested': args.top,
        },
        'classification_guidance': {
            'goal': 'Classify market structure and regime fit for this bot, not predict event truth.',
            'decision_labels': ['allow', 'avoid', 'review'],
            'recommended_focus': [
                'Does this look like a gradual repricing market versus a discrete jump/news market?',
                'Is the market family a good fit for the current bot logic?',
                'Are there structural/risk reasons to avoid or manually review it?',
            ],
        },
        'markets': selected,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print(json.dumps({'output_path': str(output_path), 'markets_exported': len(selected)}, indent=2))


if __name__ == '__main__':
    main()
