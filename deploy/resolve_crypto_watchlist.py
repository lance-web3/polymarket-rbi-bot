from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

SERIES_SLUGS = {
    "BTC": "btc-up-or-down-5m",
    "ETH": "eth-up-or-down-5m",
    "SOL": "sol-up-or-down-5m",
    "XRP": "xrp-up-or-down-5m",
    "DOGE": "doge-up-or-down-5m",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve crypto event watchlist entries into active token ids and condition ids.")
    parser.add_argument("--watchlist", default="data/crypto_event_watchlist.json")
    parser.add_argument("--out", default="data/crypto_resolved_watchlist.json")
    return parser.parse_args()


def fetch_json(url: str) -> dict[str, Any] | list[Any] | None:
    try:
        response = requests.get(url, timeout=30, headers={"accept": "application/json"})
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def choose_active_event(series: dict[str, Any]) -> dict[str, Any] | None:
    events = series.get("events") or []
    now = datetime.now(timezone.utc)
    scored: list[tuple[int, datetime, dict[str, Any]]] = []
    for event in events:
        active = bool(event.get("active", False))
        closed = bool(event.get("closed", False))
        archived = bool(event.get("archived", False))
        markets = event.get("markets") or []
        accepts = any(bool(m.get("acceptingOrders", False)) for m in markets)
        end_dt = parse_dt(event.get("endDate")) or datetime.max.replace(tzinfo=timezone.utc)
        score = 0
        if active:
            score += 4
        if accepts:
            score += 3
        if not closed:
            score += 2
        if not archived:
            score += 1
        # prefer nearest future event over stale distant data
        if end_dt < now:
            score -= 5
        scored.append((score, end_dt, event))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], -item[1].timestamp()), reverse=True)
    return scored[0][2]


def resolve_entry(entry: dict[str, Any]) -> dict[str, Any]:
    asset = str(entry.get("asset") or "").upper()
    label = entry.get("label")
    series_slug = SERIES_SLUGS.get(asset)
    resolved: dict[str, Any] = {
        "label": label,
        "asset": asset,
        "market_type": entry.get("market_type"),
        "source_url": entry.get("url"),
        "series_slug": series_slug,
        "resolved": False,
    }
    if not series_slug:
        resolved["error"] = "no_series_slug"
        return resolved

    series = fetch_json(f"https://polymarket.com/api/series?slug={series_slug}")
    if not isinstance(series, dict):
        resolved["error"] = "series_lookup_failed"
        return resolved

    event = choose_active_event(series)
    if not isinstance(event, dict):
        resolved["error"] = "no_active_event_found"
        return resolved

    event_slug = event.get("slug")
    event_payload = fetch_json(f"https://polymarket.com/api/event?slug={event_slug}") if event_slug else None
    if not isinstance(event_payload, dict):
        resolved["error"] = "event_lookup_failed"
        resolved["event_slug"] = event_slug
        return resolved

    markets = event_payload.get("markets") or []
    market = next((m for m in markets if not m.get("closed") and not m.get("archived")), markets[0] if markets else None)
    if not isinstance(market, dict):
        resolved["error"] = "no_market_found"
        resolved["event_slug"] = event_slug
        return resolved

    outcomes = market.get("outcomes") or []
    clob_token_ids = market.get("clobTokenIds") or []
    tokens = []
    for idx, token_id in enumerate(clob_token_ids):
        outcome = outcomes[idx] if idx < len(outcomes) else None
        tokens.append({"token_id": token_id, "outcome": outcome})

    resolved.update(
        {
            "resolved": True,
            "series_title": series.get("title"),
            "event_slug": event_slug,
            "event_title": event_payload.get("title") or event_payload.get("question"),
            "question": market.get("question") or event_payload.get("title"),
            "condition_id": market.get("conditionId") or market.get("condition_id"),
            "market_slug": market.get("slug"),
            "accepting_orders": market.get("acceptingOrders"),
            "minimum_tick_size": market.get("minimumTickSize"),
            "minimum_order_size": market.get("minimumOrderSize"),
            "liquidity": market.get("liquidity") or market.get("liquidityClob"),
            "volume": market.get("volume") or market.get("volumeClob"),
            "tokens": tokens,
        }
    )
    return resolved


def main() -> None:
    args = parse_args()
    watchlist = json.loads(Path(args.watchlist).read_text())
    resolved = [resolve_entry(entry) for entry in watchlist]
    Path(args.out).write_text(json.dumps(resolved, indent=2))
    print(json.dumps({"saved": args.out, "resolved_count": sum(1 for row in resolved if row.get('resolved')), "rows": resolved}, indent=2))


if __name__ == "__main__":
    main()
