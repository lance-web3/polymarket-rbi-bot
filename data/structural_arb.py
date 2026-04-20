from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from data.market_discovery import parse_jsonish_list


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_market_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("markets"), list):
            return payload["markets"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
    raise ValueError(f"unsupported market fixture format: {path}")


def _load_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _quote_is_sane(bid: float | None, ask: float | None) -> bool:
    if bid is None or ask is None:
        return False
    if bid < 0 or ask < 0 or bid > 1 or ask > 1:
        return False
    return ask >= bid


def _outcome_payloads(market: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = parse_jsonish_list(market.get("outcomes"))
    prices = parse_jsonish_list(market.get("outcomePrices"))
    token_ids = parse_jsonish_list(market.get("clobTokenIds"))
    rows: list[dict[str, Any]] = []
    for index, outcome in enumerate(outcomes):
        rows.append(
            {
                "index": index,
                "outcome": str(outcome),
                "token_id": str(token_ids[index]) if index < len(token_ids) else None,
                "reference_price": _safe_float(prices[index]) if index < len(prices) else None,
            }
        )
    return rows


def analyze_live_bundle_markets(
    markets: list[dict[str, Any]],
    *,
    min_liquidity: float = 0.0,
    underround_buffer: float = 0.01,
    overround_buffer: float = 0.01,
    top: int = 25,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for market in markets:
        liquidity = _safe_float(market.get("liquidity")) or 0.0
        if liquidity < min_liquidity:
            continue

        outcomes = _outcome_payloads(market)
        if len(outcomes) < 2:
            continue

        reference_prices = [row["reference_price"] for row in outcomes if row.get("reference_price") is not None]
        if len(reference_prices) != len(outcomes):
            continue

        implied_sum = sum(reference_prices)
        underround = 1.0 - implied_sum
        overround = implied_sum - 1.0
        category = "balanced"
        if underround > underround_buffer:
            category = "underround"
        elif overround > overround_buffer:
            category = "overround"

        rows.append(
            {
                "question": market.get("question"),
                "condition_id": market.get("conditionId") or market.get("condition_id"),
                "market_slug": market.get("slug"),
                "liquidity": liquidity,
                "volume": _safe_float(market.get("volume")),
                "outcome_count": len(outcomes),
                "pricing_basis": "gamma_outcomePrices_reference_only",
                "category": category,
                "implied_probability_sum": round(implied_sum, 6),
                "underround": round(underround, 6),
                "overround": round(overround, 6),
                "outcomes": outcomes,
                "notes": [
                    "This live scan uses Gamma outcomePrices as a reference snapshot, not guaranteed executable bid/ask across every outcome.",
                    "Use quote-backtest mode for timestamp-aligned executable-style bundle checks when per-token bid/ask snapshots are available.",
                ],
            }
        )

    ranked = sorted(
        rows,
        key=lambda row: (
            1 if row.get("category") == "underround" else 0,
            abs(float(row.get("underround") or 0.0)) if row.get("category") == "underround" else abs(float(row.get("overround") or 0.0)),
            float(row.get("liquidity") or 0.0),
        ),
        reverse=True,
    )
    shortlist = ranked[: max(top, 0)]
    return {
        "mode": "live_reference",
        "summary": {
            "markets_scanned": len(rows),
            "underround_count": sum(1 for row in rows if row.get("category") == "underround"),
            "overround_count": sum(1 for row in rows if row.get("category") == "overround"),
            "balanced_count": sum(1 for row in rows if row.get("category") == "balanced"),
            "top_requested": top,
            "min_liquidity": min_liquidity,
            "underround_buffer": underround_buffer,
            "overround_buffer": overround_buffer,
        },
        "shortlist": shortlist,
        "ranked_markets": ranked,
    }


def analyze_quote_backtest_bundles(
    csv_dir: str | Path,
    *,
    ask_buffer: float = 0.01,
    bid_buffer: float = 0.01,
    min_rows_per_condition: int = 3,
    min_reference_sum: float = 0.95,
    max_reference_sum: float = 1.05,
    top: int = 25,
) -> dict[str, Any]:
    csv_root = Path(csv_dir)
    by_condition: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(csv_root.glob("*.csv")):
        rows = _load_csv_rows(path)
        if not rows:
            continue
        condition_id = str(rows[0].get("condition_id") or "")
        if not condition_id:
            continue
        by_condition[condition_id].append(path)

    opportunities: list[dict[str, Any]] = []
    condition_summaries: list[dict[str, Any]] = []
    total_timestamp_rows = 0

    for condition_id, paths in sorted(by_condition.items()):
        token_rows: list[dict[str, Any]] = []
        for path in paths:
            rows = _load_csv_rows(path)
            if not rows:
                continue
            first = rows[0]
            token_rows.append(
                {
                    "path": path,
                    "rows": rows,
                    "token_id": str(first.get("token_id") or ""),
                    "outcome": first.get("outcome"),
                    "question": first.get("question"),
                }
            )
        if len(token_rows) < 2:
            continue

        by_timestamp: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for token_row in token_rows:
            for row in token_row["rows"]:
                timestamp = str(row.get("timestamp") or "")
                if not timestamp:
                    continue
                by_timestamp[timestamp].append(
                    {
                        "token_id": token_row["token_id"],
                        "outcome": token_row["outcome"],
                        "question": token_row["question"],
                        "best_bid": _safe_float(row.get("best_bid")),
                        "best_ask": _safe_float(row.get("best_ask")),
                        "close": _safe_float(row.get("close")),
                        "mid": _safe_float(row.get("close")),
                    }
                )

        aligned_rows = 0
        executable_buy_count = 0
        executable_sell_count = 0
        invalid_quote_rows = 0
        rejected_reference_rows = 0
        best_buy_edge: float | None = None
        best_sell_edge: float | None = None

        for timestamp, legs in sorted(by_timestamp.items()):
            if len(legs) != len(token_rows):
                continue
            aligned_rows += 1
            total_timestamp_rows += 1
            valid_quotes = [_quote_is_sane(leg.get("best_bid"), leg.get("best_ask")) for leg in legs]
            if not all(valid_quotes):
                invalid_quote_rows += 1
            bundle_ask = sum(leg["best_ask"] for leg in legs if leg.get("best_ask") is not None)
            bundle_bid = sum(leg["best_bid"] for leg in legs if leg.get("best_bid") is not None)
            ref_values = [leg["mid"] for leg in legs if leg.get("mid") is not None]
            ref_sum = sum(ref_values) if len(ref_values) == len(legs) else None
            reference_sane = ref_sum is not None and min_reference_sum <= ref_sum <= max_reference_sum
            if ref_sum is not None and not reference_sane:
                rejected_reference_rows += 1

            buy_edge = 1.0 - bundle_ask if all(valid_quotes) and reference_sane else None
            sell_edge = bundle_bid - 1.0 if all(valid_quotes) and reference_sane else None

            if buy_edge is not None and buy_edge > ask_buffer:
                executable_buy_count += 1
                best_buy_edge = buy_edge if best_buy_edge is None else max(best_buy_edge, buy_edge)
                opportunities.append(
                    {
                        "type": "buy_bundle_under_1",
                        "condition_id": condition_id,
                        "timestamp": timestamp,
                        "question": legs[0].get("question"),
                        "outcome_count": len(legs),
                        "bundle_ask": round(bundle_ask, 6),
                        "edge": round(buy_edge, 6),
                        "reference_sum_close": round(ref_sum, 6) if ref_sum else None,
                        "legs": legs,
                    }
                )

            if sell_edge is not None and sell_edge > bid_buffer:
                executable_sell_count += 1
                best_sell_edge = sell_edge if best_sell_edge is None else max(best_sell_edge, sell_edge)
                opportunities.append(
                    {
                        "type": "sell_bundle_over_1",
                        "condition_id": condition_id,
                        "timestamp": timestamp,
                        "question": legs[0].get("question"),
                        "outcome_count": len(legs),
                        "bundle_bid": round(bundle_bid, 6),
                        "edge": round(sell_edge, 6),
                        "reference_sum_close": round(ref_sum, 6) if ref_sum else None,
                        "legs": legs,
                    }
                )

        if aligned_rows < min_rows_per_condition:
            continue

        condition_summaries.append(
            {
                "condition_id": condition_id,
                "question": token_rows[0].get("question"),
                "outcomes": [row.get("outcome") for row in token_rows],
                "paths": [str(row.get("path")) for row in token_rows],
                "aligned_rows": aligned_rows,
                "buy_bundle_count": executable_buy_count,
                "sell_bundle_count": executable_sell_count,
                "invalid_quote_rows": invalid_quote_rows,
                "rejected_reference_rows": rejected_reference_rows,
                "best_buy_edge": round(best_buy_edge, 6) if best_buy_edge is not None else None,
                "best_sell_edge": round(best_sell_edge, 6) if best_sell_edge is not None else None,
            }
        )

    ranked_conditions = sorted(
        condition_summaries,
        key=lambda row: (
            float(row.get("best_buy_edge") or 0.0),
            float(row.get("best_sell_edge") or 0.0),
            int(row.get("buy_bundle_count") or 0) + int(row.get("sell_bundle_count") or 0),
            int(row.get("aligned_rows") or 0),
        ),
        reverse=True,
    )

    ranked_opportunities = sorted(
        opportunities,
        key=lambda row: (float(row.get("edge") or 0.0), str(row.get("timestamp") or "")),
        reverse=True,
    )

    return {
        "mode": "quote_backtests",
        "summary": {
            "conditions_scanned": len(condition_summaries),
            "timestamp_rows_scanned": total_timestamp_rows,
            "buy_bundle_count": sum(int(row.get("buy_bundle_count") or 0) for row in condition_summaries),
            "sell_bundle_count": sum(int(row.get("sell_bundle_count") or 0) for row in condition_summaries),
            "invalid_quote_rows": sum(int(row.get("invalid_quote_rows") or 0) for row in condition_summaries),
            "rejected_reference_rows": sum(int(row.get("rejected_reference_rows") or 0) for row in condition_summaries),
            "ask_buffer": ask_buffer,
            "bid_buffer": bid_buffer,
            "min_rows_per_condition": min_rows_per_condition,
            "min_reference_sum": min_reference_sum,
            "max_reference_sum": max_reference_sum,
            "top_requested": top,
        },
        "shortlist": ranked_opportunities[: max(top, 0)],
        "ranked_conditions": ranked_conditions[: max(top, 0)],
        "all_condition_summaries": ranked_conditions,
        "notes": [
            "quote-backtest mode requires one CSV per token with shared condition_id and aligned timestamps.",
            "Executable bundle signals are only emitted when every leg has sane quotes (0<=bid<=ask<=1) at that timestamp.",
            "The scanner also requires complementary reference prices to sum near $1 (configurable) to suppress stale or structurally inconsistent quote artifacts.",
            "Edges here are raw bundle sums versus $1 and do not yet subtract fees, inventory, or execution latency.",
        ],
    }


def load_markets_from_json(path: str | Path) -> list[dict[str, Any]]:
    return _load_market_rows(Path(path))
