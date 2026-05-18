"""calibrate_edge.py - honest edge calibration for Polymarket signals.

This is the single most important experiment in the project. Before tuning any
gate, exit rule, or risk knob, you need to know: does the signal predict
forward returns *after* paying the spread? This script answers that.

It runs on real-quote data - either the CLOB backtest CSVs
(data/quote_backtests_clob_*) or, better, the big collected quote streams
(data/quote_collection/run.jsonl, nonsports_run.jsonl) - and answers three
questions in order:

  Q1. Do these markets have ANY exploitable momentum at all?
      -> raw autocorrelation: past N-bar return vs forward N-bar return.
      If this is ~0, no momentum strategy can work here, full stop.

  Q2. When LongEntry fires BUY, what is the realized EXECUTABLE forward return?
      -> "buy at the ask now, sell at the bid N bars later" = the money you
      could actually keep. Compared against the optimistic mid-to-mid number
      so you can see exactly how much the spread eats.

  Q3. Does the strategy's confidence / expected_edge_bps rank-predict that
      executable return? If a higher score doesn't mean a higher realized
      return, the score is decoration, not edge.

Nothing here is profitable-by-construction. A flat or negative result is a
*useful* result - it tells you to change the signal, not the plumbing.

Usage:
  # big collected quote stream (preferred - lots of data per token)
  python -m deploy.calibrate_edge --input data/quote_collection/run.jsonl
  python -m deploy.calibrate_edge --input data/quote_collection/nonsports_run.jsonl --strict-mode

  # CLOB backtest CSV directory
  python -m deploy.calibrate_edge --input data/quote_backtests_clob_large

  # tuning knobs
  python -m deploy.calibrate_edge --input data/quote_collection/run.jsonl \\
      --horizons 1,4,20,80 --min-bars 120 --exclude-tail --out data/edge_calibration.json

--input accepts a .jsonl file, a .csv file, a directory of CSVs, or a glob.
A .jsonl stream is split into one series per token_id automatically.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.data import load_snapshots_from_csv
from polymarket_rbi_bot.models import Candle, MarketSnapshot, SignalSide
from strategies.long_entry_strategy import LongEntryStrategy


# --------------------------------------------------------------------------
# small stats helpers (stdlib only)
# --------------------------------------------------------------------------
def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _ranks(xs: list[float]) -> list[float]:
    """Average-rank for ties."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return _pearson(_ranks(xs), _ranks(ys))


# --------------------------------------------------------------------------
# input handling - CSV files/dirs/globs and JSONL quote streams
# --------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _parse_ts(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def expand_inputs(raw_inputs: list[str]) -> list[Path]:
    if not raw_inputs:
        raw_inputs = ["data/quote_collection/run.jsonl"]
    paths: list[Path] = []
    for item in raw_inputs:
        candidate = Path(item)
        if any(ch in item for ch in "*?[]"):
            paths.extend(sorted(Path().glob(item)))
        elif candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.csv")))
            paths.extend(sorted(candidate.glob("*.jsonl")))
        elif candidate.exists():
            paths.append(candidate)
    unique: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        r = str(p.resolve())
        if r not in seen:
            seen.add(r)
            unique.append(p)
    return unique


def _snapshot_from_jsonl_record(rec: dict[str, Any]) -> tuple[str, MarketSnapshot] | None:
    """Returns (token_id, snapshot) or None if the record is unusable."""
    token_id = rec.get("token_id")
    ts_raw = rec.get("timestamp")
    if not token_id or not ts_raw:
        return None
    # prefer the executable CLOB book; fall back to gamma best bid/ask
    bid = _to_float(rec.get("clob_best_bid"))
    if bid is None:
        bid = _to_float(rec.get("best_bid"))
    ask = _to_float(rec.get("clob_best_ask"))
    if ask is None:
        ask = _to_float(rec.get("best_ask"))
    mid = _to_float(rec.get("mid"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2
    if mid is None:
        mid = _to_float(rec.get("reference_price")) or _to_float(rec.get("last_price"))
    if mid is None or mid <= 0:
        return None
    try:
        ts = _parse_ts(ts_raw)
    except ValueError:
        return None
    candle = Candle(timestamp=ts, open=mid, high=mid, low=mid, close=mid, volume=0.0)
    meta = {
        "outcome": rec.get("outcome"),
        "market_slug": rec.get("market_slug"),
        "question": rec.get("question"),
        "market_family": rec.get("market_family"),
        "liquidity": rec.get("liquidity"),
        "end_date": rec.get("end_date"),
        "created_at": rec.get("created_at"),
    }
    return str(token_id), MarketSnapshot(candle=candle, best_bid=bid, best_ask=ask, metadata=meta)


def load_markets_from_jsonl(path: Path) -> list[tuple[str, list[MarketSnapshot]]]:
    """Split a quote stream into one chronologically-sorted series per token."""
    by_token: dict[str, list[MarketSnapshot]] = defaultdict(list)
    labels: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = _snapshot_from_jsonl_record(rec)
            if parsed is None:
                continue
            token_id, snap = parsed
            by_token[token_id].append(snap)
            if token_id not in labels:
                slug = rec.get("market_slug") or token_id[:12]
                outcome = rec.get("outcome") or "?"
                labels[token_id] = f"{slug}|{outcome}"
    markets: list[tuple[str, list[MarketSnapshot]]] = []
    for token_id, snaps in by_token.items():
        snaps.sort(key=lambda s: s.candle.timestamp)
        markets.append((labels.get(token_id, token_id), snaps))
    return markets


def iter_markets(paths: list[Path]) -> Iterator[tuple[str, list[MarketSnapshot]]]:
    for path in paths:
        if path.suffix == ".jsonl":
            yield from load_markets_from_jsonl(path)
        elif path.suffix == ".csv":
            try:
                yield path.name, load_snapshots_from_csv(path)
            except Exception as exc:  # noqa: BLE001 - one bad file shouldn't kill the run
                print(f"  ! skipped {path.name}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------
def _executable_buy_return_bps(ask: list[float | None], bid: list[float | None], i: int, h: int) -> float | None:
    """Buy at ask[i], sell at bid[i+h]. The return you could actually keep."""
    j = i + h
    if j >= len(ask):
        return None
    entry, exit_ = ask[i], bid[j]
    if entry is None or exit_ is None or entry <= 0:
        return None
    return ((exit_ - entry) / entry) * 10_000


def _mid_return_bps(mid: list[float], i: int, h: int) -> float | None:
    j = i + h
    if j >= len(mid) or mid[i] <= 0:
        return None
    return ((mid[j] - mid[i]) / mid[i]) * 10_000


def analyze_market(
    name: str,
    snapshots: list[MarketSnapshot],
    *,
    strategy: LongEntryStrategy,
    horizons: list[int],
    momentum_lookback: int,
    exclude_tail: bool,
    min_bars: int,
) -> dict[str, Any] | None:
    required = max(min_bars, momentum_lookback + max(horizons) + 5)
    if len(snapshots) < required:
        return {"csv": name, "skipped": True, "reason": f"too short ({len(snapshots)} < {required} bars)"}

    closes = [s.candle.close for s in snapshots]
    mids = [s.mid_price for s in snapshots]
    bids = [s.best_bid for s in snapshots]
    asks = [s.best_ask for s in snapshots]
    quoted = sum(1 for b, a in zip(bids, asks) if b is not None and a is not None)
    quote_ratio = quoted / len(snapshots)

    last_close = closes[-1]
    price_range = max(closes) - min(closes)
    is_tail = last_close < 0.05 or last_close > 0.95
    is_flat = price_range < 0.005  # never moved more than half a cent -> nothing to predict

    if is_flat:
        return {"csv": name, "skipped": True, "reason": f"flat (range {price_range:.4f})",
                "bars": len(snapshots), "last_close": round(last_close, 4)}
    if exclude_tail and is_tail:
        return {"csv": name, "skipped": True, "reason": f"tail-priced (last {last_close:.3f})",
                "bars": len(snapshots), "last_close": round(last_close, 4)}

    # Q1: raw momentum autocorrelation (does momentum exist as a phenomenon?)
    momentum_pairs: dict[int, tuple[list[float], list[float]]] = {h: ([], []) for h in horizons}
    for i in range(momentum_lookback, len(closes)):
        if closes[i - momentum_lookback] <= 0:
            continue
        past = ((closes[i] - closes[i - momentum_lookback]) / closes[i - momentum_lookback]) * 10_000
        for h in horizons:
            fwd = _mid_return_bps(mids, i, h)
            if fwd is not None:
                momentum_pairs[h][0].append(past)
                momentum_pairs[h][1].append(fwd)
    raw_momentum = {}
    for h in horizons:
        px, fx = momentum_pairs[h]
        corr = _pearson(px, fx)
        raw_momentum[f"h{h}"] = {"n": len(px), "pearson_past_vs_fwd": round(corr, 4) if corr is not None else None}

    # Q2 + Q3: strategy signal -> executable forward return
    buy_rows: list[dict[str, Any]] = []
    signal_sides = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for i in range(len(snapshots)):
        signal = strategy.generate_signal(snapshots[: i + 1])
        signal_sides[signal.side.value] = signal_sides.get(signal.side.value, 0) + 1
        if signal.side != SignalSide.BUY:
            continue
        meta = signal.metadata or {}
        row: dict[str, Any] = {
            "i": i,
            "confidence": signal.confidence,
            "expected_edge_bps": meta.get("expected_edge_bps"),
            "price": closes[i],
        }
        for h in horizons:
            row[f"exec_fwd_h{h}_bps"] = _executable_buy_return_bps(asks, bids, i, h)
            row[f"mid_fwd_h{h}_bps"] = _mid_return_bps(mids, i, h)
        buy_rows.append(row)

    return {
        "csv": name,
        "skipped": False,
        "bars": len(snapshots),
        "quote_ratio": round(quote_ratio, 3),
        "last_close": round(last_close, 4),
        "price_range": round(price_range, 4),
        "is_tail": is_tail,
        "signal_sides": signal_sides,
        "raw_momentum": raw_momentum,
        "buy_rows": buy_rows,
    }


def aggregate(market_results: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    active = [m for m in market_results if not m.get("skipped")]
    skipped = [m for m in market_results if m.get("skipped")]

    all_buy_rows: list[dict[str, Any]] = []
    for m in active:
        all_buy_rows.extend(m["buy_rows"])

    total_signal_sides = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for m in active:
        for k, v in m["signal_sides"].items():
            total_signal_sides[k] = total_signal_sides.get(k, 0) + v

    # Q1 aggregate: pool pearson across markets (weighted by n)
    q1: dict[str, Any] = {}
    for h in horizons:
        weighted_sum, weight = 0.0, 0
        for m in active:
            rm = m["raw_momentum"].get(f"h{h}", {})
            if rm.get("pearson_past_vs_fwd") is not None and rm.get("n"):
                weighted_sum += rm["pearson_past_vs_fwd"] * rm["n"]
                weight += rm["n"]
        q1[f"h{h}"] = {"n": weight,
                       "avg_pearson_past_vs_fwd": round(weighted_sum / weight, 4) if weight else None}

    # Q2 aggregate: executable vs mid forward returns on BUY signals
    q2: dict[str, Any] = {}
    for h in horizons:
        exec_key, mid_key = f"exec_fwd_h{h}_bps", f"mid_fwd_h{h}_bps"
        exec_vals = [r[exec_key] for r in all_buy_rows if r.get(exec_key) is not None]
        mid_vals = [r[mid_key] for r in all_buy_rows if r.get(mid_key) is not None]
        wins = [v for v in exec_vals if v > 0]
        q2[f"h{h}"] = {
            "n": len(exec_vals),
            "mean_executable_bps": round(_mean(exec_vals), 2) if exec_vals else None,
            "median_executable_bps": round(statistics.median(exec_vals), 2) if exec_vals else None,
            "mean_mid_to_mid_bps": round(_mean(mid_vals), 2) if mid_vals else None,
            "spread_drag_bps": round(_mean(mid_vals) - _mean(exec_vals), 2) if exec_vals and mid_vals else None,
            "win_rate_executable": round(len(wins) / len(exec_vals), 4) if exec_vals else None,
        }

    # Q3 aggregate: does score rank-predict executable return?
    q3: dict[str, Any] = {}
    for h in horizons:
        exec_key = f"exec_fwd_h{h}_bps"
        conf_pairs = [(r["confidence"], r[exec_key]) for r in all_buy_rows
                      if r.get(exec_key) is not None and r.get("confidence") is not None]
        edge_pairs = [(r["expected_edge_bps"], r[exec_key]) for r in all_buy_rows
                      if r.get(exec_key) is not None and r.get("expected_edge_bps") is not None]
        q3[f"h{h}"] = {
            "confidence_vs_return_spearman": (
                round(_spearman([c for c, _ in conf_pairs], [e for _, e in conf_pairs]), 4)
                if len(conf_pairs) >= 3 else None),
            "confidence_n": len(conf_pairs),
            "expected_edge_vs_return_spearman": (
                round(_spearman([c for c, _ in edge_pairs], [e for _, e in edge_pairs]), 4)
                if len(edge_pairs) >= 3 else None),
            "expected_edge_n": len(edge_pairs),
        }

    return {
        "markets_total": len(market_results),
        "markets_active": len(active),
        "markets_skipped": len(skipped),
        "skip_reasons": _count_skip_reasons(skipped),
        "total_signal_sides": total_signal_sides,
        "total_buy_signals": len(all_buy_rows),
        "Q1_raw_momentum_exists": q1,
        "Q2_executable_returns_on_buys": q2,
        "Q3_score_predicts_return": q3,
    }


def _count_skip_reasons(skipped: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for m in skipped:
        reason = str(m.get("reason", "unknown"))
        key = reason.split(" (")[0]  # collapse "too short (12 < 109 bars)" -> "too short"
        counts[key] += 1
    return dict(counts)


def verdict(agg: dict[str, Any], horizons: list[int]) -> list[str]:
    lines: list[str] = []
    if agg["markets_active"] == 0:
        lines.append("VERDICT: no usable markets after filtering. "
                     f"Skip reasons: {agg['skip_reasons']}. "
                     "Lower --min-bars, drop --exclude-tail, or collect longer per-token series.")
        return lines
    buys = agg["total_buy_signals"]
    if buys == 0:
        lines.append("VERDICT: the strategy produced ZERO buy signals on "
                     f"{agg['markets_active']} usable markets. It is not 'trading less' - "
                     "it is not trading. Loosen the signal or test non-strict mode first.")
        return lines

    h = horizons[len(horizons) // 2]  # headline on a mid horizon
    q1 = agg["Q1_raw_momentum_exists"].get(f"h{h}", {})
    q2 = agg["Q2_executable_returns_on_buys"].get(f"h{h}", {})
    q3 = agg["Q3_score_predicts_return"].get(f"h{h}", {})

    p = q1.get("avg_pearson_past_vs_fwd")
    if p is None:
        lines.append(f"Q1 (h{h}): not enough data to measure raw momentum.")
    elif abs(p) < 0.03:
        lines.append(f"Q1 (h{h}): raw momentum correlation is {p} - effectively zero. "
                     "These markets show no usable momentum; a momentum signal cannot have "
                     "edge here by construction.")
    else:
        direction = "continues" if p > 0 else "reverses (mean-reversion, not momentum)"
        lines.append(f"Q1 (h{h}): raw momentum correlation is {p} - momentum {direction}. "
                     "There is something to exploit; make sure the signal trades the right sign.")

    me, mm, wr = q2.get("mean_executable_bps"), q2.get("mean_mid_to_mid_bps"), q2.get("win_rate_executable")
    if me is not None:
        sign = "POSITIVE" if me > 0 else "NEGATIVE or flat"
        lines.append(f"Q2 (h{h}): mean executable return on BUY signals is {me} bps ({sign}); "
                     f"mid-to-mid would have looked like {mm} bps. The gap is the spread you pay. "
                     f"Executable win rate {wr} (n={q2.get('n')}).")
        if me <= 0:
            lines.append("       -> after spread, the BUY signal does not make money on this sample. "
                         "Fix the signal, not the exits. Note how win rate can look fine while the "
                         "mean return is negative - that is exactly the Reddit '71% win rate' trap.")

    sp = q3.get("confidence_vs_return_spearman")
    if sp is None:
        lines.append(f"Q3 (h{h}): not enough BUY rows to test whether the score predicts return.")
    elif abs(sp) < 0.05:
        lines.append(f"Q3 (h{h}): confidence vs realized return rank-correlation is {sp} - "
                     "the confidence score is decoration, not edge.")
    else:
        lines.append(f"Q3 (h{h}): confidence vs realized return rank-correlation is {sp} - "
                     "the score carries some signal; worth keeping and sharpening.")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", "--csv", dest="inputs", action="append", default=[],
                        help="A .jsonl quote stream, a .csv, a directory, or a glob. Repeatable. "
                             "Default: data/quote_collection/run.jsonl")
    parser.add_argument("--horizons", default="1,4,20,80",
                        help="Comma-separated forward horizons in bars (default 1,4,20,80). "
                             "Poll bars are ~15-30s, so these are roughly seconds-to-minutes ahead.")
    parser.add_argument("--momentum-lookback", type=int, default=24,
                        help="Lookback in bars for the raw momentum autocorrelation test.")
    parser.add_argument("--strict-mode", action="store_true", help="Run LongEntry in strict mode.")
    parser.add_argument("--signal-version", default="v2", help="LongEntry signal version (v2|legacy).")
    parser.add_argument("--exclude-tail", action="store_true",
                        help="Also skip markets pinned in the tails (<0.05 / >0.95). Flat markets "
                             "(sub-half-cent range) are always skipped regardless.")
    parser.add_argument("--min-bars", type=int, default=120, help="Skip series shorter than this.")
    parser.add_argument("--out", default="data/edge_calibration.json", help="Full JSON output path.")
    args = parser.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    paths = expand_inputs(args.inputs)
    if not paths:
        raise SystemExit("No input files found. Point --input at run.jsonl or a CSV directory.")

    strategy = LongEntryStrategy(strict_mode=args.strict_mode, signal_version=args.signal_version)

    market_results: list[dict[str, Any]] = []
    for name, snapshots in iter_markets(paths):
        try:
            result = analyze_market(
                name, snapshots,
                strategy=strategy, horizons=horizons,
                momentum_lookback=args.momentum_lookback,
                exclude_tail=args.exclude_tail, min_bars=args.min_bars,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"csv": name, "skipped": True, "reason": f"error: {exc}"}
        if result is not None:
            market_results.append(result)

    if not market_results:
        raise SystemExit("No series found in the input at all.")

    agg = aggregate(market_results, horizons)
    verdict_lines = verdict(agg, horizons)

    payload = {
        "config": {
            "inputs": [str(p) for p in paths],
            "horizons_bars": horizons,
            "momentum_lookback_bars": args.momentum_lookback,
            "strict_mode": args.strict_mode,
            "signal_version": args.signal_version,
            "exclude_tail": args.exclude_tail,
            "min_bars": args.min_bars,
        },
        "aggregate": agg,
        "verdict": verdict_lines,
        "per_market": [
            ({k: v for k, v in m.items() if k != "buy_rows"}
             | ({"buy_signal_count": len(m["buy_rows"])} if not m.get("skipped") else {}))
            for m in market_results
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    print("=" * 72)
    print("EDGE CALIBRATION SUMMARY")
    print("=" * 72)
    print(f"markets: {agg['markets_active']} active / {agg['markets_skipped']} skipped "
          f"/ {agg['markets_total']} total")
    if agg["markets_skipped"]:
        print(f"skip reasons: {agg['skip_reasons']}")
    print(f"signals: {agg['total_signal_sides']}  (total BUY rows: {agg['total_buy_signals']})")
    print("-" * 72)
    for line in verdict_lines:
        print(line)
    print("-" * 72)
    print(f"full JSON written to {out_path}")


if __name__ == "__main__":
    main()
