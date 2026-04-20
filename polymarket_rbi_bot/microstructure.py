from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import MarketSnapshot


@dataclass(slots=True)
class MicrostructureMetrics:
    observed_spread_bps: float | None
    avg_spread_bps: float | None
    quote_availability_ratio: float
    quote_count: int
    wide_spread_rate: float
    lookback_bars: int
    source_mode: str
    real_quote_count: int
    proxy_quote_count: int
    real_quote_availability_ratio: float
    proxy_enabled: bool
    notes: list[str]


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return ((ask - bid) / mid) * 10_000


def _safe_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimate_proxy_spread_bps(snapshot: MarketSnapshot, history_window: list[MarketSnapshot]) -> tuple[float | None, list[str]]:
    meta = snapshot.metadata or {}
    notes: list[str] = []
    close_price = snapshot.candle.close
    liquidity = _safe_float(meta.get("market_liquidity") or meta.get("liquidity"))
    current_market_spread_bps = _safe_float(meta.get("market_current_spread_bps") or meta.get("spread_bps") or meta.get("spread"))
    current_market_bid = _safe_float(meta.get("market_best_bid") or meta.get("bestBid"))
    current_market_ask = _safe_float(meta.get("market_best_ask") or meta.get("bestAsk"))

    if current_market_spread_bps is None:
        current_market_spread_bps = _spread_bps(current_market_bid, current_market_ask)
        if current_market_spread_bps is not None:
            notes.append("proxy seeded from current market bestBid/bestAsk")

    if current_market_spread_bps is None:
        current_market_spread_bps = 350.0
        notes.append("proxy used default baseline spread because no current market spread metadata was available")
    else:
        notes.append("proxy seeded from current market spread metadata, not historical quotes")

    returns: list[float] = []
    for previous, current in zip(history_window, history_window[1:]):
        prev_close = previous.candle.close
        curr_close = current.candle.close
        if prev_close > 0 and curr_close > 0:
            returns.append(abs((curr_close - prev_close) / prev_close))
    realized_vol = sum(returns) / len(returns) if returns else 0.0

    spread_bps = current_market_spread_bps

    if liquidity is not None and liquidity > 0:
        # Coarse liquidity adjustment: tighter if the market is meaningfully more liquid.
        spread_bps -= max(0.0, min(200.0, (math.log10(liquidity) - 4.0) * 90.0))
        notes.append("proxy adjusted for market liquidity")
    else:
        spread_bps += 75.0
        notes.append("proxy penalized for missing liquidity metadata")

    if close_price <= 0.03 or close_price >= 0.97:
        spread_bps += 900.0
        notes.append("proxy penalized extreme tail pricing")
    elif close_price <= 0.08 or close_price >= 0.92:
        spread_bps += 450.0
        notes.append("proxy penalized low-price/high-price tail regime")
    elif close_price <= 0.15 or close_price >= 0.85:
        spread_bps += 180.0
        notes.append("proxy mildly penalized off-center pricing")

    spread_bps += min(900.0, realized_vol * 40_000.0)
    if realized_vol > 0:
        notes.append("proxy widened for realized price volatility")

    if snapshot.candle.volume <= 0:
        spread_bps += 80.0
        notes.append("proxy penalized zero reported bar volume")

    spread_bps = max(75.0, min(3_500.0, spread_bps))
    return spread_bps, notes


def compute_microstructure_metrics(
    history: list[MarketSnapshot],
    *,
    lookback_bars: int = 24,
    wide_spread_bps: float = 500.0,
    proxy_policy: str = "auto",
) -> MicrostructureMetrics:
    if not history:
        return MicrostructureMetrics(
            observed_spread_bps=None,
            avg_spread_bps=None,
            quote_availability_ratio=0.0,
            quote_count=0,
            wide_spread_rate=0.0,
            lookback_bars=0,
            source_mode="none",
            real_quote_count=0,
            proxy_quote_count=0,
            real_quote_availability_ratio=0.0,
            proxy_enabled=proxy_policy != "real-only",
            notes=[],
        )

    window: list[MarketSnapshot] = history[-lookback_bars:] if lookback_bars > 0 else history[:]
    spreads: list[float] = []
    real_quote_count = 0
    proxy_quote_count = 0
    wide_count = 0
    notes: list[str] = []
    observed: float | None = None
    observed_source = "none"

    for idx, snap in enumerate(window):
        real_spread = _spread_bps(snap.best_bid, snap.best_ask)
        spread_to_use = real_spread
        if real_spread is not None:
            real_quote_count += 1
            if idx == len(window) - 1:
                observed = real_spread
                observed_source = "real"
        elif proxy_policy != "real-only":
            proxy_spread, proxy_notes = _estimate_proxy_spread_bps(snap, window[: idx + 1])
            if proxy_spread is not None:
                spread_to_use = proxy_spread
                proxy_quote_count += 1
                notes.extend(proxy_notes)
                if idx == len(window) - 1:
                    observed = proxy_spread
                    observed_source = "proxy"

        if spread_to_use is not None:
            spreads.append(spread_to_use)
            if spread_to_use >= wide_spread_bps:
                wide_count += 1

    if observed is None and window:
        last = window[-1]
        observed = _spread_bps(last.best_bid, last.best_ask)
        if observed is not None:
            observed_source = "real"

    avg_spread = (sum(spreads) / len(spreads)) if spreads else None
    total = max(len(window), 1)
    effective_quote_count = real_quote_count + proxy_quote_count
    quote_avail = effective_quote_count / total
    real_quote_avail = real_quote_count / total
    wide_rate = (wide_count / effective_quote_count) if effective_quote_count else 0.0

    source_mode = "none"
    if real_quote_count and proxy_quote_count:
        source_mode = "mixed"
    elif real_quote_count:
        source_mode = "real"
    elif proxy_quote_count:
        source_mode = "proxy"

    if observed_source == "proxy":
        notes.append("latest spread was proxied because the latest bar had no historical bid/ask")
    if source_mode == "proxy":
        notes.append("all microstructure values in this window are synthetic proxies")
    elif source_mode == "mixed":
        notes.append("window mixes real historical quotes with synthetic proxy estimates")

    deduped_notes = list(dict.fromkeys(notes))
    return MicrostructureMetrics(
        observed_spread_bps=observed,
        avg_spread_bps=avg_spread,
        quote_availability_ratio=quote_avail,
        quote_count=effective_quote_count,
        wide_spread_rate=wide_rate,
        lookback_bars=len(window),
        source_mode=source_mode,
        real_quote_count=real_quote_count,
        proxy_quote_count=proxy_quote_count,
        real_quote_availability_ratio=real_quote_avail,
        proxy_enabled=proxy_policy != "real-only",
        notes=deduped_notes,
    )


def parse_any_timestamp(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        if isinstance(value, (int, float)):
            # assume unix seconds
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        text = str(value).strip()
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        if text.endswith("Z"):
            text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def extract_maturity_datetimes(market_like: dict) -> dict[str, datetime | None]:
    # Try a variety of possible field names used by Gamma or exports
    candidates = {
        "resolution": market_like.get("resolutionTime") or market_like.get("resolution_time") or market_like.get("endDate") or market_like.get("end_date") or market_like.get("end_date_iso") or market_like.get("endTime") or market_like.get("closeTime") or market_like.get("expires_at") or market_like.get("expiryTime"),
        "open": market_like.get("openTime") or market_like.get("open_time") or market_like.get("createdAt") or market_like.get("created_at") or market_like.get("startTime"),
    }
    return {
        "resolution": parse_any_timestamp(candidates["resolution"]),
        "open": parse_any_timestamp(candidates["open"]),
    }


def compute_time_to_resolution_hours(*, now: datetime, market_like: dict | None = None, snapshot: MarketSnapshot | None = None) -> dict[str, float | None]:
    end_dt = None
    open_dt = None
    if market_like is not None:
        times = extract_maturity_datetimes(market_like)
        end_dt = times["resolution"]
        open_dt = times["open"]
    if snapshot is not None:
        meta = snapshot.metadata or {}
        end_dt = end_dt or parse_any_timestamp(meta.get("resolution_ts") or meta.get("end_ts") or meta.get("end_time") or meta.get("endDate"))
        open_dt = open_dt or parse_any_timestamp(meta.get("open_ts") or meta.get("open_time") or meta.get("createdAt"))

    ttr_hours = None
    since_open_hours = None
    if end_dt is not None:
        ttr_hours = (end_dt - now).total_seconds() / 3600.0
    if open_dt is not None:
        since_open_hours = (now - open_dt).total_seconds() / 3600.0

    return {"time_to_resolution_hours": ttr_hours, "time_since_open_hours": since_open_hours}
