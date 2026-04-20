from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from polymarket_rbi_bot.models import Candle, MarketSnapshot, TradeTick


CORE_SNAPSHOT_FIELDS = {"timestamp", "open", "high", "low", "close", "volume", "best_bid", "best_ask"}


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.isdigit():
        return datetime.fromtimestamp(int(normalized), tz=timezone.utc)
    if normalized.endswith("Z"):
        normalized = normalized.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snapshot_metadata_from_row(row: dict[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key, value in row.items():
        if key in CORE_SNAPSHOT_FIELDS:
            continue
        if value not in {None, ""}:
            metadata[key] = value
    return metadata


def load_snapshots_from_csv(path: str | Path) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            candle = Candle(
                timestamp=_parse_timestamp(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0) or 0.0),
            )
            snapshots.append(
                MarketSnapshot(
                    candle=candle,
                    best_bid=float(row["best_bid"]) if row.get("best_bid") else None,
                    best_ask=float(row["best_ask"]) if row.get("best_ask") else None,
                    metadata=_snapshot_metadata_from_row(row),
                )
            )
    return snapshots


def rows_to_snapshots(rows: list[dict[str, object]]) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    for row in rows:
        candle = Candle(
            timestamp=_parse_timestamp(str(row["timestamp"])),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0) or 0.0),
        )
        snapshots.append(
            MarketSnapshot(
                candle=candle,
                best_bid=float(row["best_bid"]) if row.get("best_bid") else None,
                best_ask=float(row["best_ask"]) if row.get("best_ask") else None,
                metadata=_snapshot_metadata_from_row(row),
            )
        )
    return snapshots


def load_trades_from_csv(path: str | Path) -> list[TradeTick]:
    trades: list[TradeTick] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            trades.append(
                TradeTick(
                    timestamp=_parse_timestamp(row["timestamp"]),
                    price=float(row["price"]),
                    size=float(row["size"]),
                    side=row["side"].upper(),
                )
            )
    return trades
