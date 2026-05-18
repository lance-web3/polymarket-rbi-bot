# Polymarket RBI Bot — Code Audit

Date: 2026-05-15
Scope: full repo review of `strategies/`, `bot/`, `backtesting/`, `polymarket_rbi_bot/`, plus `deploy/run_live.py` and `data/polymarket_client.py`.
Status of bot: backtest-only, no paper or live trades yet.

---

## TL;DR

The engineering is genuinely good — clean separation of signal/execution/state, real risk
checks, thoughtful live guardrails, atomic state writes, and an unusually honest README.
That is well above the median "I built a trading bot" project.

The problem is **not the code quality. It is that nothing in the repo yet demonstrates the
strategy has edge**, and the backtest you would use to check that is sitting on data that
cannot answer the question. Before adding any more features, the work should turn toward
proving (or disproving) edge on trustworthy data. Everything else is secondary.

Verdict on "can we make it profitable at $1 trades": $1 is the right size for *validation*,
not for profit — the spread alone eats any realistic edge at that size. Treat $1 live as the
final stage of a test, after the data and edge questions below are answered.

---

## What's solid

- **Architecture.** Strategies are side-effect-free signal generators; the trader owns
  execution and state; risk checks are deterministic. This is the right shape and makes the
  system testable and extendable.
- **Live execution guardrails** (`bot/risk_manager.py`). Freshness/staleness, crossed-quote
  detection, spread sanity, price-deviation-from-mid and from-same-side-quote, open-order
  caps, duplicate-token suppression, and submission/fill cooldowns. This is a serious set of
  pre-trade checks and most of it is correct.
- **State store** (`bot/state.py`). Atomic write via temp-file-then-replace; fill
  deduplication by id; average-price and realized-PnL accounting that correctly folds fees
  into cost basis on buys and subtracts on sells.
- **Backtest honesty.** Next-bar fill timing (no same-bar lookahead on entry/exit),
  explicit spread/slippage/fee cost attribution, and a README that repeatedly tells you not
  to trust the output. The `microstructure_run_summary` that labels a run as real/proxy/mixed
  quotes is exactly the right instinct.

---

## Critical issues (these decide whether the bot can ever be profitable)

### 1. The backtest data cannot price your trades
`data/polymarket_client.py` pulls Polymarket's `/prices-history` endpoint, which is
**price-only** — look at lines 56-57, `best_bid` and `best_ask` are returned as empty
strings. So any backtest built from that source has *zero real quotes*.

The synthetic microstructure proxy (`polymarket_rbi_bot/microstructure.py`,
`_estimate_proxy_spread_bps`) fills the gap, but it is seeded from **current** Gamma
metadata (today's spread/liquidity) applied to **past** bars. That is a forward-looking
input on historical data, and it is not a substitute for real historical bid/ask.

Consequence: in a quote-less backtest, `observed_spread_bps` is 0, so the strict edge gate
`required_edge = max(min_expected_edge_bps, round_trip_cost + buffer + observed_spread)`
collapses — the spread term disappears entirely. You are validating a cost-aware strategy
with the cost turned off.

**This is the single biggest threat to profitability.** Until backtests run on real
collected bid/ask, no PnL number from this repo means anything.

### 2. `expected_edge_bps` is a score, not an edge
In `strategies/long_entry_strategy.py` (lines ~247-261), `expected_edge_bps` is a
hand-tuned linear combination of momentum bonuses and pullback/jump penalties. It is
labelled in "bps" and the strict gate compares it against a real cost in bps — but there is
no evidence it predicts actual forward returns. It is an arbitrary confidence score wearing
a basis-points costume.

The strict mode's headline feature ("expected edge must beat cost") is therefore comparing
a made-up number to a real one. **Until `expected_edge_bps` is calibrated against realized
forward returns, the strategy has no demonstrated edge** — and calibrating it is the highest
-value experiment available right now.

### 3. Momentum on probabilities is not momentum on prices
The whole `long_entry` thesis is "buy contracts that have been grinding upward." But on
Polymarket, price ≈ probability, and price drifts mechanically toward 0 or 1 as resolution
approaches regardless of any tradable edge. A naive momentum filter will systematically buy
contracts converging toward 1 — sometimes that's genuine information, sometimes you are just
late and paying up. Nothing in the code separates "informative drift" from "mechanical
convergence." This needs to be tested explicitly, not assumed.

### 4. Backtest execution model and live order type disagree
Live orders are **post-only GTC limit orders** priced at `reference_price - edge`, i.e.
*passive maker* orders sitting below the reference. But the backtest
(`backtesting/engine.py::_resolve_execution`) fills you at the same-side quote (the ask, for
a buy) or `mid + fallback_half_spread_bps`, i.e. it models you as a *taker crossing the
spread*.

These are opposite execution styles. A passive post-only order either (a) doesn't fill, or
(b) fills precisely when the market moves against you — classic adverse selection. The
backtest models neither. Whatever the backtest says about fills and costs, live behavior
will differ, probably for the worse. You need to pick one model and make both sides agree.

### 5. "Daily loss limit" is not daily
`deploy/run_live.py` wires `risk.daily_realized_pnl = state_store.realized_pnl`, but
`state_store.realized_pnl` is **all-time** realized PnL. So the limit is really an all-time
loss limit: a single early loss could permanently lock the bot, and a profitable history
could mask a catastrophic day. Either track PnL per UTC day or rename the control.

---

## Secondary issues

- **No tests at all.** The exact code that most needs them — `RiskManager.validate_order`,
  `RiskManager.evaluate_execution_guards`, `LiveStateStore._apply_fill` (the average-price
  and realized-PnL math) — is untested. This is the cheapest high-value fix in the repo.
- **Walk-forward windows are too short to mean anything.** The README example uses
  `--train-bars 12 --test-bars 4`. A 4-bar out-of-sample window is statistical noise; it
  will produce confident-looking results that don't generalize.
- **Confirmer confidence scaling is arbitrary.** MACD uses `confidence = min(abs(spread) *
  100, 1.0)` — the `* 100` is a magic number, and MACD/RSI parameters (12/26/9, 35/65) are
  equity-market defaults applied to probability series with no re-tuning. Less critical in
  strict mode where they're weak confirmers, but in non-strict mode they're co-equal voters.
- **`$1` trade size vs Polymarket minimums.** Polymarket enforces a minimum order size.
  Confirm the real floor before assuming `$1` notional orders are even accepted; you may
  need to size to the minimum and treat that as your validation unit.
- **Strict mode + tiny size + bad data = false confidence.** Strict mode is designed to
  trade rarely. A backtest that makes 5-10 trades on quote-less data and shows a positive
  number is indistinguishable from noise. Be very disciplined about sample sizes.
- **Stale path** in `README.md` line ~505 still references `/Users/mac/Desktop/...`.

---

## Recommended roadmap (in priority order)

The ordering matters — do not skip ahead. Each stage answers a question that makes the next
stage meaningful.

**Stage 1 — Get trustworthy data.** Run `deploy.collect_quotes --use-clob-order-books`
against a watchlist of candidate markets for a sustained period to build CSVs with *real*
historical bid/ask. Until this exists, stop drawing conclusions from price-history backtests.

**Stage 2 — Calibrate edge.** For every historical `long_entry` signal, log the
`expected_edge_bps` score and the realized forward mid-price move N bars later. Regress one
on the other. If there's no relationship, the strategy has no edge and the honest move is to
go back to research — not to add more gates. This is roughly a day of work and it is the
most important day in the project.

**Stage 3 — Reconcile backtest with reality.** Decide maker vs taker. If maker (post-only),
the backtest must model fill probability and adverse selection. If taker, change the live
order type. Make the two sides agree, then re-run Stage 2's check with realistic costs.

**Stage 4 — Plumbing for safe live.** Add unit tests for the risk manager and state store;
fix the daily-loss-limit semantics; confirm Polymarket's minimum order size and set
`default_order_size` accordingly; dry-run `deploy.run_live` end to end.

**Stage 5 — Paper, then $1 live.** Paper-run on real collected quotes for a few weeks.
Compare each paper decision to what actually happened. Only if Stages 2-3 showed real,
cost-surviving edge *and* paper trading confirms it, move to $1 live — and treat $1 as a
plumbing/behavior test, not a profit attempt.

---

## On the Reddit post and "profitable AI bot"

The linked thread couldn't be retrieved (Reddit is blocked from fetching). General caution
applies regardless: posts claiming a profitable Polymarket bot are almost always
backtest screenshots rather than audited live PnL, survivorship-biased, or soft promotion.
A backtest equity curve — especially one built on quote-less data, as this repo's would be —
is close to worthless as evidence. The only thing that counts is live PnL net of real
spread, over enough trades to be statistically meaningful.

None of this is financial advice — it's a code and process review. Whether to risk real
money is your call.
