from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from data.market_discovery import GammaMarketDiscoveryClient, parse_jsonish_list

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BookParams
except ImportError:  # pragma: no cover - optional at runtime
    ClobClient = None
    BookParams = None


@dataclass(slots=True)
class QuoteCollectorTarget:
    token_id: str
    condition_id: str | None = None
    outcome: str | None = None
    question: str | None = None
    market_slug: str | None = None
    market_family: str | None = None


class QuoteSnapshotCollector:
    """Lightweight polling collector for current Gamma quote snapshots.

    This is intentionally a research data collector, not a production execution feed.
    It polls current market state and appends one JSON row per token snapshot.
    """

    def __init__(
        self,
        *,
        discovery: GammaMarketDiscoveryClient,
        lookup_limit: int = 1000,
        clob_host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        use_clob_order_books: bool = False,
    ) -> None:
        self.discovery = discovery
        self.lookup_limit = lookup_limit
        self.clob_host = clob_host
        self.chain_id = chain_id
        self.use_clob_order_books = use_clob_order_books
        self._public_clob_client: Any = None

    def _connect_public_clob(self) -> Any:
        if ClobClient is None:
            raise RuntimeError("py-clob-client is not installed. Run `pip install -r requirements.txt` first.")
        if self._public_clob_client is None:
            self._public_clob_client = ClobClient(self.clob_host, chain_id=self.chain_id)
        return self._public_clob_client

    def _fetch_clob_books(self, token_ids: Iterable[str]) -> dict[str, dict[str, float | None]]:
        clean_ids = [str(token_id) for token_id in token_ids if token_id and not str(token_id).startswith("condition::")]
        if not clean_ids:
            return {}
        client = self._connect_public_clob()
        books: dict[str, dict[str, float | None]] = {}
        try:
            if BookParams is not None:
                payload = client.get_order_books([BookParams(token_id=token_id) for token_id in clean_ids])
            else:
                payload = [client.get_order_book(token_id) for token_id in clean_ids]
        except Exception:
            payload = []
        for book in payload or []:
            asset_id = str(getattr(book, "asset_id", "") or "")
            if not asset_id:
                continue
            bids = getattr(book, "bids", []) or []
            asks = getattr(book, "asks", []) or []
            bid_prices = [_safe_float(getattr(level, "price", None) if not isinstance(level, dict) else level.get("price")) for level in bids]
            ask_prices = [_safe_float(getattr(level, "price", None) if not isinstance(level, dict) else level.get("price")) for level in asks]
            valid_bid_prices = [price for price in bid_prices if price is not None]
            valid_ask_prices = [price for price in ask_prices if price is not None]
            books[asset_id] = {
                "best_bid": max(valid_bid_prices) if valid_bid_prices else None,
                "best_ask": min(valid_ask_prices) if valid_ask_prices else None,
                "last_trade_price": _safe_float(getattr(book, "last_trade_price", None) if not isinstance(book, dict) else book.get("last_trade_price")),
            }
        return books

    def resolve_targets(
        self,
        *,
        token_ids: Iterable[str] = (),
        condition_ids: Iterable[str] = (),
        watchlist_path: str | Path | None = None,
    ) -> list[QuoteCollectorTarget]:
        requested_targets = list(load_targets_from_sources(
            token_ids=token_ids,
            condition_ids=condition_ids,
            watchlist_path=watchlist_path,
        ))
        requested_token_ids = {item["token_id"] for item in requested_targets if item.get("token_id")}
        requested_condition_ids = {item["condition_id"] for item in requested_targets if item.get("condition_id")}

        markets = self.discovery.list_markets(limit=self.lookup_limit, closed=False, archived=False)
        resolved: dict[str, QuoteCollectorTarget] = {}

        for market in markets:
            condition_id = _as_clean_str(market.get("conditionId") or market.get("condition_id"))
            token_ids_for_market = [str(token_id) for token_id in parse_jsonish_list(market.get("clobTokenIds"))]
            outcomes = [str(item) for item in parse_jsonish_list(market.get("outcomes"))]
            family = _extract_market_family(market)
            for idx, token_id in enumerate(token_ids_for_market):
                if token_id not in requested_token_ids and (not condition_id or condition_id not in requested_condition_ids):
                    continue
                resolved[token_id] = QuoteCollectorTarget(
                    token_id=token_id,
                    condition_id=condition_id,
                    outcome=outcomes[idx] if idx < len(outcomes) else None,
                    question=_as_clean_str(market.get("question")),
                    market_slug=_as_clean_str(market.get("slug")),
                    market_family=family,
                )

        # Preserve explicit/watchlist intent even when the market is not found in the current scan.
        for item in requested_targets:
            token_id = item.get("token_id")
            condition_id = item.get("condition_id")
            if token_id and token_id not in resolved:
                resolved[token_id] = QuoteCollectorTarget(
                    token_id=token_id,
                    condition_id=condition_id,
                    outcome=item.get("outcome"),
                    question=item.get("question"),
                    market_slug=item.get("market_slug"),
                    market_family=item.get("market_family"),
                )
            elif not token_id and condition_id:
                resolved[f"condition::{condition_id}"] = QuoteCollectorTarget(
                    token_id=f"condition::{condition_id}",
                    condition_id=condition_id,
                    outcome=item.get("outcome"),
                    question=item.get("question"),
                    market_slug=item.get("market_slug"),
                    market_family=item.get("market_family"),
                )

        expanded: list[QuoteCollectorTarget] = []
        for target in resolved.values():
            if not target.token_id.startswith("condition::"):
                expanded.append(target)
                continue
            for market in markets:
                condition_id = _as_clean_str(market.get("conditionId") or market.get("condition_id"))
                if condition_id != target.condition_id:
                    continue
                outcomes = [str(item) for item in parse_jsonish_list(market.get("outcomes"))]
                family = _extract_market_family(market)
                for idx, token_id in enumerate(parse_jsonish_list(market.get("clobTokenIds"))):
                    expanded.append(
                        QuoteCollectorTarget(
                            token_id=str(token_id),
                            condition_id=condition_id,
                            outcome=outcomes[idx] if idx < len(outcomes) else target.outcome,
                            question=_as_clean_str(market.get("question")) or target.question,
                            market_slug=_as_clean_str(market.get("slug")) or target.market_slug,
                            market_family=family or target.market_family,
                        )
                    )

        if expanded:
            deduped: dict[str, QuoteCollectorTarget] = {target.token_id: target for target in expanded}
            return list(deduped.values())

        return list(resolved.values())

    def collect_once(self, *, targets: Iterable[QuoteCollectorTarget]) -> list[dict[str, Any]]:
        target_map = {target.token_id: target for target in targets}
        condition_ids = {target.condition_id for target in target_map.values() if target.condition_id}
        markets = self.discovery.list_markets(limit=self.lookup_limit, closed=False, archived=False)

        rows: list[dict[str, Any]] = []
        seen_token_ids: set[str] = set()
        snapshot_ts = datetime.now(timezone.utc).isoformat()
        clob_books = self._fetch_clob_books(target_map.keys()) if self.use_clob_order_books else {}

        for market in markets:
            condition_id = _as_clean_str(market.get("conditionId") or market.get("condition_id"))
            token_ids = [str(token_id) for token_id in parse_jsonish_list(market.get("clobTokenIds"))]
            outcomes = [str(item) for item in parse_jsonish_list(market.get("outcomes"))]
            best_bids = _coerce_float_list(market.get("bestBids"))
            best_asks = _coerce_float_list(market.get("bestAsks"))
            prices = _coerce_float_list(market.get("outcomePrices"))
            family = _extract_market_family(market)

            market_level_bid = _safe_float(market.get("bestBid"))
            market_level_ask = _safe_float(market.get("bestAsk"))
            liquidity = _safe_float(market.get("liquidityNum") or market.get("liquidity"))
            volume = _safe_float(market.get("volumeNum") or market.get("volume"))
            has_per_outcome_quotes = bool(best_bids or best_asks)
            allow_market_level_quote_fallback = len(token_ids) <= 1

            for idx, token_id in enumerate(token_ids):
                target = target_map.get(token_id)
                if not target and condition_id and condition_id in condition_ids:
                    target = next((item for item in target_map.values() if item.condition_id == condition_id and item.token_id == token_id), None)
                if not target:
                    continue

                quote_fallback_used = False
                clob_book = clob_books.get(token_id)
                if clob_book is not None:
                    best_bid = clob_book.get("best_bid")
                    best_ask = clob_book.get("best_ask")
                    quote_source = "clob_order_book"
                elif has_per_outcome_quotes:
                    best_bid = _pick_index(best_bids, idx)
                    best_ask = _pick_index(best_asks, idx)
                    quote_source = "gamma_bestBids_bestAsks"
                elif allow_market_level_quote_fallback:
                    best_bid = market_level_bid
                    best_ask = market_level_ask
                    quote_fallback_used = best_bid is not None or best_ask is not None
                    quote_source = "gamma_market_level_bestBid_bestAsk_fallback"
                else:
                    best_bid = None
                    best_ask = None
                    quote_source = "missing_per_outcome_quotes"
                last_price = _pick_index(prices, idx)
                if clob_book is not None and clob_book.get("last_trade_price") is not None:
                    last_price = clob_book.get("last_trade_price")
                mid = _compute_mid(best_bid, best_ask)
                spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
                spread_bps = None
                if spread is not None and mid and mid > 0:
                    spread_bps = (spread / mid) * 10_000

                rows.append(
                    {
                        "timestamp": snapshot_ts,
                        "token_id": token_id,
                        "condition_id": condition_id or target.condition_id,
                        "outcome": _pick_index(outcomes, idx) or target.outcome,
                        "question": _as_clean_str(market.get("question")) or target.question,
                        "market_slug": _as_clean_str(market.get("slug")) or target.market_slug,
                        "market_family": family or target.market_family,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "mid": mid,
                        "spread": spread,
                        "spread_bps": spread_bps,
                        "last_price": last_price,
                        "liquidity": liquidity,
                        "volume": volume,
                        "closed": bool(market.get("closed", False)),
                        "archived": bool(market.get("archived", False)),
                        "end_date": _as_clean_str(market.get("endDate") or market.get("end_date")),
                        "created_at": _as_clean_str(market.get("createdAt") or market.get("created_at")),
                        "quote_source": quote_source,
                        "has_per_outcome_quotes": has_per_outcome_quotes,
                        "quote_fallback_used": quote_fallback_used,
                        "reference_price": _pick_index(prices, idx),
                        "market_level_best_bid": market_level_bid,
                        "market_level_best_ask": market_level_ask,
                        "clob_best_bid": None if clob_book is None else clob_book.get("best_bid"),
                        "clob_best_ask": None if clob_book is None else clob_book.get("best_ask"),
                        "clob_last_trade_price": None if clob_book is None else clob_book.get("last_trade_price"),
                        "source": "gamma_markets_poll",
                    }
                )
                seen_token_ids.add(token_id)

        for token_id, target in target_map.items():
            if token_id in seen_token_ids:
                continue
            rows.append(
                {
                    "timestamp": snapshot_ts,
                    "token_id": token_id,
                    "condition_id": target.condition_id,
                    "outcome": target.outcome,
                    "question": target.question,
                    "market_slug": target.market_slug,
                    "market_family": target.market_family,
                    "best_bid": None,
                    "best_ask": None,
                    "mid": None,
                    "spread": None,
                    "spread_bps": None,
                    "last_price": None,
                    "liquidity": None,
                    "volume": None,
                    "closed": None,
                    "archived": None,
                    "end_date": None,
                    "created_at": None,
                    "quote_source": "missing_target",
                    "has_per_outcome_quotes": False,
                    "quote_fallback_used": False,
                    "reference_price": None,
                    "market_level_best_bid": None,
                    "market_level_best_ask": None,
                    "clob_best_bid": None,
                    "clob_best_ask": None,
                    "clob_last_trade_price": None,
                    "source": "gamma_markets_poll_missing_target",
                }
            )

        return rows

    def run(
        self,
        *,
        targets: Iterable[QuoteCollectorTarget],
        output_path: str | Path,
        interval_seconds: float,
        iterations: int | None = None,
        append: bool = True,
        sleep_fn: Any = time.sleep,
    ) -> dict[str, Any]:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        target_list = list(targets)

        mode = "a" if append else "w"
        loops_completed = 0
        rows_written = 0

        with output_file.open(mode, encoding="utf-8") as handle:
            while True:
                batch = self.collect_once(targets=target_list)
                for row in batch:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                loops_completed += 1
                rows_written += len(batch)

                if iterations is not None and loops_completed >= iterations:
                    break
                sleep_fn(max(interval_seconds, 0.0))

        return {
            "output_path": str(output_file),
            "targets": len(target_list),
            "iterations": loops_completed,
            "rows_written": rows_written,
            "interval_seconds": interval_seconds,
        }


def load_targets_from_sources(
    *,
    token_ids: Iterable[str] = (),
    condition_ids: Iterable[str] = (),
    watchlist_path: str | Path | None = None,
) -> list[dict[str, str | None]]:
    targets: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()

    def add_target(token_id: str | None, condition_id: str | None = None, **extra: str | None) -> None:
        clean_token = _as_clean_str(token_id)
        clean_condition = _as_clean_str(condition_id)
        if not clean_token and not clean_condition:
            return
        key = (clean_token, clean_condition)
        if key in seen:
            return
        seen.add(key)
        targets.append({"token_id": clean_token, "condition_id": clean_condition, **extra})

    for token_id in token_ids:
        add_target(token_id)
    for condition_id in condition_ids:
        add_target(None, condition_id)

    if watchlist_path:
        for item in _load_watchlist_entries(Path(watchlist_path)):
            add_target(
                item.get("token_id"),
                item.get("condition_id"),
                outcome=item.get("outcome"),
                question=item.get("question"),
                market_slug=item.get("market_slug") or item.get("slug"),
                market_family=item.get("market_family") or item.get("family"),
            )

    return targets


def _load_watchlist_entries(path: Path) -> list[dict[str, str | None]]:
    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        return _load_json_watchlist(path)
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    return _load_text_watchlist(path)


def _load_json_watchlist(path: Path) -> list[dict[str, str | None]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        return [_normalize_watchlist_entry(json.loads(line)) for line in text.splitlines() if line.strip()]

    payload = json.loads(text)
    if isinstance(payload, dict):
        for key in ("shortlist", "ranked_markets", "markets", "targets", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [_normalize_watchlist_entry(item) for item in value]
        return [_normalize_watchlist_entry(payload)]
    if isinstance(payload, list):
        return [_normalize_watchlist_entry(item) for item in payload]
    return []


def _load_text_watchlist(path: Path) -> list[dict[str, str | None]]:
    entries: list[dict[str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [item.strip() for item in stripped.split(",")]
        if len(parts) == 1:
            entries.append({"token_id": parts[0]})
        else:
            entries.append({"token_id": parts[0], "condition_id": parts[1] or None})
    return entries


def _normalize_watchlist_entry(item: Any) -> dict[str, str | None]:
    if isinstance(item, str):
        return {"token_id": item}
    if not isinstance(item, dict):
        return {}
    return {
        "token_id": _as_clean_str(item.get("token_id") or item.get("tokenId")),
        "condition_id": _as_clean_str(item.get("condition_id") or item.get("conditionId")),
        "outcome": _as_clean_str(item.get("outcome")),
        "question": _as_clean_str(item.get("question")),
        "market_slug": _as_clean_str(item.get("market_slug") or item.get("slug")),
        "market_family": _as_clean_str(item.get("market_family") or item.get("family") or item.get("family_label")),
    }


def _coerce_float_list(value: Any) -> list[float | None]:
    raw_items = parse_jsonish_list(value)
    return [_safe_float(item) for item in raw_items]


def _pick_index(values: list[Any], idx: int, fallback: Any = None) -> Any:
    if idx < len(values):
        return values[idx]
    return fallback


def _compute_mid(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2


def _extract_market_family(market: dict[str, Any]) -> str | None:
    for key in ("family", "marketFamily", "family_label", "category"):
        value = _as_clean_str(market.get(key))
        if value:
            return value
    return None


def _as_clean_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
