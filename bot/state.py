from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from polymarket_rbi_bot.models import Fill, Position, SignalSide


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dt(value: str | int | float | None) -> datetime:
    if value in {None, ""}:
        return _utcnow()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        timestamp = int(text)
        if timestamp > 1_000_000_000_000:
            return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    normalized = text.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


@dataclass(slots=True)
class PositionState:
    token_id: str
    quantity: float = 0.0
    average_price: float = 0.0
    realized_pnl: float = 0.0
    opened_at: str | None = None
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_position(self) -> Position:
        return Position(
            token_id=self.token_id,
            quantity=self.quantity,
            average_price=self.average_price,
            opened_at=_parse_dt(self.opened_at) if self.opened_at else None,
        )


class LiveStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _empty_state(self) -> dict[str, Any]:
        now = _utcnow().isoformat()
        return {
            "version": 1,
            "updated_at": now,
            "realized_pnl": 0.0,
            "positions": {},
            "open_orders": {},
            "fills": [],
            "fill_ids": [],
            "cooldowns": {
                "last_submission_at": None,
                "last_fill_at": None,
            },
            "reconcile": {
                "last_attempt_at": None,
                "last_success_at": None,
                "status": "never",
                "message": None,
            },
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            state = self._empty_state()
            self._write(state)
            return state
        data = json.loads(self.path.read_text())
        merged = self._empty_state()
        merged.update(data)
        merged.setdefault("positions", {})
        merged.setdefault("open_orders", {})
        merged.setdefault("fills", [])
        merged.setdefault("fill_ids", [])
        merged.setdefault("cooldowns", self._empty_state()["cooldowns"])
        merged.setdefault("reconcile", self._empty_state()["reconcile"])
        return merged

    def _write(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = _utcnow().isoformat()
        with NamedTemporaryFile("w", delete=False, dir=str(self.path.parent), encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    def save(self) -> None:
        self._write(self._state)

    @property
    def realized_pnl(self) -> float:
        return float(self._state.get("realized_pnl", 0.0))

    @property
    def positions(self) -> dict[str, Position]:
        return {
            token_id: PositionState(**payload).to_position()
            for token_id, payload in self._state.get("positions", {}).items()
        }

    @property
    def open_orders(self) -> dict[str, dict[str, Any]]:
        return dict(self._state.get("open_orders", {}))

    @property
    def cooldowns(self) -> dict[str, Any]:
        return dict(self._state.get("cooldowns", {}))

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._state))

    def mark_reconcile(self, *, status: str, message: str | None = None, success: bool = False) -> None:
        reconcile = self._state.setdefault("reconcile", {})
        reconcile["last_attempt_at"] = _utcnow().isoformat()
        reconcile["status"] = status
        reconcile["message"] = message
        if success:
            reconcile["last_success_at"] = reconcile["last_attempt_at"]
        self.save()

    def record_submitted_order(self, intent: Any, response: dict[str, Any]) -> str:
        timestamp = _utcnow().isoformat()
        order_id = str(
            response.get("orderID")
            or response.get("id")
            or response.get("order_id")
            or f"local-{intent.token_id}-{int(_utcnow().timestamp())}"
        )
        self._state.setdefault("open_orders", {})[order_id] = {
            "order_id": order_id,
            "token_id": intent.token_id,
            "side": intent.side.value,
            "price": float(intent.price),
            "size": float(intent.size),
            "status": str(response.get("status") or "submitted"),
            "submitted_at": timestamp,
            "raw": response,
        }
        self._state.setdefault("cooldowns", {})["last_submission_at"] = timestamp
        self.save()
        return order_id

    def replace_open_orders(self, orders: list[dict[str, Any]]) -> None:
        next_orders: dict[str, dict[str, Any]] = {}
        for order in orders:
            order_id = str(order.get("id") or order.get("orderID") or order.get("order_id") or "")
            if not order_id:
                continue
            next_orders[order_id] = order
        self._state["open_orders"] = next_orders
        self.save()

    def apply_exchange_fill(self, fill_data: dict[str, Any]) -> bool:
        normalized = self._normalize_fill(fill_data)
        if normalized is None:
            return False
        fill_id = normalized["fill_id"]
        known_ids = set(self._state.setdefault("fill_ids", []))
        if fill_id in known_ids:
            return False

        fill = Fill(
            timestamp=_parse_dt(normalized["timestamp"]),
            token_id=normalized["token_id"],
            side=SignalSide(normalized["side"]),
            price=normalized["price"],
            size=normalized["size"],
            fees=normalized["fees"],
        )
        self._apply_fill(fill)
        known_ids.add(fill_id)
        self._state["fill_ids"] = sorted(known_ids)
        self._state.setdefault("fills", []).append({"fill_id": fill_id, **asdict(fill), "timestamp": fill.timestamp.isoformat(), "raw": fill_data})
        self._state.setdefault("cooldowns", {})["last_fill_at"] = fill.timestamp.isoformat()
        self.save()
        return True

    def _apply_fill(self, fill: Fill) -> None:
        positions = self._state.setdefault("positions", {})
        current = PositionState(**positions.get(fill.token_id, {"token_id": fill.token_id}))

        if fill.side == SignalSide.BUY:
            new_qty = current.quantity + fill.size
            if current.quantity <= 0 and fill.size > 0:
                current.opened_at = fill.timestamp.isoformat()
            if new_qty > 0:
                current.average_price = ((current.quantity * current.average_price) + (fill.size * fill.price) + fill.fees) / new_qty
            current.quantity = new_qty
        else:
            closed_size = min(current.quantity, fill.size)
            if closed_size > 0:
                realized = (fill.price - current.average_price) * closed_size - fill.fees
                current.realized_pnl += realized
                self._state["realized_pnl"] = float(self._state.get("realized_pnl", 0.0)) + realized
            current.quantity = max(current.quantity - fill.size, 0.0)
            if current.quantity == 0:
                current.average_price = 0.0
                current.opened_at = None

        current.updated_at = fill.timestamp.isoformat()
        positions[fill.token_id] = asdict(current)

    def _normalize_fill(self, fill_data: dict[str, Any]) -> dict[str, Any] | None:
        token_id = fill_data.get("asset_id") or fill_data.get("token_id") or fill_data.get("market")
        side = str(fill_data.get("side") or "").upper()
        if side not in {SignalSide.BUY.value, SignalSide.SELL.value}:
            return None
        price_raw = fill_data.get("price") or fill_data.get("matched_price") or fill_data.get("rate")
        size_raw = fill_data.get("size") or fill_data.get("amount") or fill_data.get("matched_amount")
        if token_id is None or price_raw is None or size_raw is None:
            return None
        timestamp = (
            fill_data.get("timestamp")
            or fill_data.get("created_at")
            or fill_data.get("last_update")
            or _utcnow().isoformat()
        )
        fill_id = str(
            fill_data.get("id")
            or fill_data.get("trade_id")
            or fill_data.get("match_id")
            or fill_data.get("transaction_hash")
            or f"{token_id}:{side}:{timestamp}:{price_raw}:{size_raw}"
        )
        return {
            "fill_id": fill_id,
            "timestamp": str(timestamp),
            "token_id": str(token_id),
            "side": side,
            "price": float(price_raw),
            "size": float(size_raw),
            "fees": float(fill_data.get("fee") or fill_data.get("fees") or 0.0),
        }
