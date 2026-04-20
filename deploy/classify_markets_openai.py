from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.config import BotConfig

SYSTEM_PROMPT = """You classify Polymarket markets for a trading bot's market-selection layer.
Your job is NOT to forecast whether the event will happen.
Your job IS to judge whether the market's structure/regime is a good fit for a momentum/repricing style Polymarket bot.
Prefer structured market-fit reasoning: scheduled outrights and gradual repricing markets are generally better fits; breaking-news, legal/regulatory shock, binary resolution cliffs, manipulation-prone, illiquid, or ambiguous markets are worse fits.
Return only schema-valid JSON.
"""

JSON_SCHEMA = {
    'name': 'market_classification_batch',
    'schema': {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'results': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'condition_id': {'type': 'string'},
                        'token_id': {'type': 'string'},
                        'question': {'type': 'string'},
                        'decision': {'type': 'string', 'enum': ['allow', 'avoid', 'review']},
                        'family': {'type': 'string'},
                        'regime_labels': {'type': 'array', 'items': {'type': 'string'}},
                        'confidence': {'type': 'number'},
                        'risk_flags': {'type': 'array', 'items': {'type': 'string'}},
                        'rationale': {'type': 'string'},
                    },
                    'required': ['condition_id', 'token_id', 'question', 'decision', 'family', 'regime_labels', 'confidence', 'risk_flags', 'rationale'],
                },
            }
        },
        'required': ['results'],
    },
}


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), max(1, size))]


def build_user_prompt(batch: list[dict[str, Any]]) -> str:
    compact_markets = []
    for market in batch:
        compact_markets.append(
            {
                'condition_id': market.get('condition_id'),
                'token_id': market.get('token_id'),
                'question': market.get('question'),
                'description': market.get('description'),
                'category': market.get('category'),
                'subcategory': market.get('subcategory'),
                'end_date_iso': market.get('end_date_iso'),
                'yes_price': market.get('yes_price'),
                'liquidity': market.get('liquidity'),
                'spread_bps': market.get('spread_bps'),
                'history_points': market.get('history_points'),
                'abs_return_bps_24h': market.get('abs_return_bps_24h'),
                'realized_volatility_bps': market.get('realized_volatility_bps'),
                'quality_score': market.get('quality_score'),
                'heuristic_family': market.get('heuristic_family'),
                'eligible_now': market.get('eligible_now'),
                'eligibility_reason': market.get('eligibility_reason'),
                'tags': market.get('tags'),
            }
        )
    return (
        'Classify each market for bot market-selection. Focus on structure/regime fit, not outcome prediction.\n'
        'Use decision=allow only when the market looks structurally suitable for this bot.\n'
        'Use decision=avoid for obvious bad fits or high structural risk.\n'
        'Use decision=review when uncertain, mixed, or needing manual inspection.\n'
        'Keep rationale short and concrete.\n\n'
        f'Markets:\n{json.dumps(compact_markets, indent=2)}'
    )


def call_openai(*, api_key: str, model: str, batch: list[dict[str, Any]], timeout: int = 120) -> list[dict[str, Any]]:
    response = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': build_user_prompt(batch)},
            ],
            'response_format': {
                'type': 'json_schema',
                'json_schema': JSON_SCHEMA,
            },
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload['choices'][0]['message']['content']
    parsed = json.loads(content)
    results = parsed.get('results') or []
    if not isinstance(results, list):
        raise ValueError('OpenAI response missing results list')
    return results


def build_dry_run_result(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for market in batch:
        heuristic_family = str(market.get('heuristic_family') or market.get('current_family') or 'unknown')
        quality_score = float(market.get('quality_score') or 0.0)
        decision = 'allow' if quality_score >= 65 else 'review' if quality_score >= 45 else 'avoid'
        results.append(
            {
                'condition_id': str(market.get('condition_id') or ''),
                'token_id': str(market.get('token_id') or ''),
                'question': str(market.get('question') or ''),
                'decision': decision,
                'family': heuristic_family,
                'regime_labels': [heuristic_family, 'dry_run'],
                'confidence': 0.35,
                'risk_flags': ['dry_run_placeholder'],
                'rationale': 'Dry-run placeholder classification derived from heuristic family and quality score.',
            }
        )
    return results


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    decision = str(result.get('decision') or 'review').strip().lower()
    if decision not in {'allow', 'avoid', 'review'}:
        decision = 'review'
    confidence = result.get('confidence')
    try:
        confidence_value = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence_value = 0.5
    family = str(result.get('family') or 'unknown').strip() or 'unknown'
    regime_labels = result.get('regime_labels') or []
    risk_flags = result.get('risk_flags') or []
    rationale = str(result.get('rationale') or '').strip() or 'No rationale supplied.'
    score_adjustment = 0.0
    if decision == 'allow':
        score_adjustment = round(min(10.0, 2.0 + confidence_value * 6.0), 2)
    elif decision == 'avoid':
        score_adjustment = round(max(-10.0, -(2.0 + confidence_value * 6.0)), 2)
    return {
        'condition_id': str(result.get('condition_id') or ''),
        'token_id': str(result.get('token_id') or ''),
        'question': str(result.get('question') or ''),
        'decision': decision,
        'family': family,
        'regime_labels': regime_labels if isinstance(regime_labels, list) else [str(regime_labels)],
        'confidence': confidence_value,
        'risk_flags': risk_flags if isinstance(risk_flags, list) else [str(risk_flags)],
        'rationale': rationale,
        'tradable': decision == 'allow',
        'score_adjustment': score_adjustment,
        'reason': rationale,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Classify exported Polymarket markets with OpenAI and write JSON decisions for the bot hook.')
    parser.add_argument('--input', type=str, default=None, help='Path to exported classifier input JSON.')
    parser.add_argument('--output', type=str, default=None, help='Path to write classifier output JSON.')
    parser.add_argument('--model', type=str, default=None, help='OpenAI model name.')
    parser.add_argument('--batch-size', type=int, default=None, help='Markets per API request.')
    parser.add_argument('--pause-seconds', type=float, default=None, help='Delay between API requests.')
    parser.add_argument('--dry-run', action='store_true', help='Do not call OpenAI; emit placeholder review/allow/avoid records.')
    args = parser.parse_args()

    config = BotConfig.from_env()
    input_path = Path(args.input or config.openai_classifier_input_path)
    output_path = Path(args.output or config.openai_classifier_output_path)
    model = args.model or config.openai_model
    batch_size = args.batch_size or config.openai_classifier_batch_size
    pause_seconds = config.openai_classifier_pause_seconds if args.pause_seconds is None else args.pause_seconds

    payload = json.loads(input_path.read_text(encoding='utf-8'))
    markets = payload.get('markets') or []
    if not isinstance(markets, list):
        raise ValueError('Input JSON must contain a markets array')

    api_key = config.openai_api_key
    if not args.dry_run and not api_key:
        raise ValueError('OPENAI_API_KEY is required unless --dry-run is used')

    all_results: list[dict[str, Any]] = []
    requests_made = 0
    for index, batch in enumerate(chunked(markets, batch_size), start=1):
        if args.dry_run:
            raw_results = build_dry_run_result(batch)
        else:
            raw_results = call_openai(api_key=api_key or '', model=model, batch=batch)
            requests_made += 1
            if index * batch_size < len(markets) and pause_seconds > 0:
                time.sleep(pause_seconds)

        normalized = [normalize_result(result) for result in raw_results]
        expected_ids = {str(market.get('condition_id') or '') for market in batch}
        returned_ids = {item['condition_id'] for item in normalized}
        missing_ids = sorted(expected_ids - returned_ids)
        if missing_ids:
            raise ValueError(f'Missing classifications for condition ids: {missing_ids}')
        all_results.extend(normalized)

    index_by_condition_id = {record['condition_id']: record for record in all_results if record.get('condition_id')}
    index_by_token_id = {record['token_id']: record for record in all_results if record.get('token_id')}

    output_payload = {
        'generated_at': datetime.now(tz=timezone.utc).isoformat(),
        'source': 'deploy.classify_markets_openai',
        'input_path': str(input_path),
        'model': 'dry-run-placeholder' if args.dry_run else model,
        'summary': {
            'markets_in_input': len(markets),
            'records_written': len(all_results),
            'api_requests': requests_made,
            'batch_size': batch_size,
            'dry_run': args.dry_run,
            'decision_counts': {
                'allow': sum(1 for row in all_results if row.get('decision') == 'allow'),
                'avoid': sum(1 for row in all_results if row.get('decision') == 'avoid'),
                'review': sum(1 for row in all_results if row.get('decision') == 'review'),
            },
        },
        'records': all_results,
        'index_by_condition_id': index_by_condition_id,
        'index_by_token_id': index_by_token_id,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output_payload, indent=2), encoding='utf-8')
    print(json.dumps({'output_path': str(output_path), 'records_written': len(all_results), 'dry_run': args.dry_run}, indent=2))


if __name__ == '__main__':
    main()
