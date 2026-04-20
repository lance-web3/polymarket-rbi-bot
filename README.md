# Polymarket RBI Bot

Python trading bot starter for Polymarket built around `py-clob-client`, with limit-order-only execution, simple technical strategies, and a lightweight backtesting flow.

## Project Structure

- `strategies/`: long-entry, MACD, RSI, and CVD strategies.
- `backtesting/`: Backtest engine for replaying snapshots.
- `bot/`: Live trader and risk manager.
- `deploy/`: CLI entry points for backtesting and live trading.
- `polymarket_rbi_bot/`: Shared config, models, and CSV loaders.
- `data/`: Downloaders and storage helpers for backtests.

## Features

- Limit-order-only order creation through `py-clob-client`.
- Strategy ensemble support for MACD, RSI, and cumulative volume delta.
- Risk checks for per-order notional, position limits, and daily loss limits.
- Persistent local live state for positions, open orders, fills, realized PnL, and live pacing timestamps.
- Best-effort exchange reconciliation before live order decisions.
- Extra live execution guardrails for stale data, spread sanity, price/reference sanity, open-order caps, duplicate suppression, and cooldown pacing.
- CSV-driven backtest entry point for fast iteration.
- More honest backtest execution using bid/ask when present, configurable slippage, and optional skip/partial-fill behavior for missing or wide quotes.
- Optional **strict strategy mode** that trades less by requiring stronger multi-signal confirmation, explicit edge-vs-cost margin, tighter price-zone filters, and anti-churn holding/cooldown rules.
- Backtest summaries now include win rate, expectancy, average win/loss, simple exposure/inventory stats, and strict-mode block counts.

## Getting Started

1. Create a virtualenv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in your Polymarket credentials.

3. Run a backtest from a Polymarket token directly:

```bash
python deploy/run_backtest.py \
  --token-id YOUR_TOKEN_ID \
  --interval max \
  --fidelity 60 \
  --save-csv data/polymarket_history.csv
```

4. Run a backtest from a CSV:

```bash
python deploy/run_backtest.py --csv data/sample_snapshots.csv
```

You can make the execution model stricter when quotes are sparse or wide:

```bash
python deploy/run_backtest.py \
  --csv data/polymarket_history.csv \
  --slippage-bps 35 \
  --max-spread-bps 250 \
  --missing-quote-fill-ratio 0 \
  --wide-spread-fill-ratio 0.5
```

You can also run the stricter strategy profile explicitly:

```bash
python deploy/run_backtest.py \
  --csv data/polymarket_history.csv \
  --strict-mode \
  --min-entry-confidence 0.60 \
  --min-buy-score 1.8 \
  --min-buy-signal-count 2 \
  --min-expected-edge-bps 140 \
  --estimated-round-trip-cost-bps 90 \
  --min-hold-bars 4 \
  --cooldown-bars-after-exit 3
```

The above CLI example is intentionally stricter than the built-in defaults; the defaults are a middle-ground profile listed below.

The JSON output now separates cash, risk, metrics, execution-model assumptions, run-level microstructure source/proxy usage, and strict-mode filters so per-market runs are easier to compare.

You can also run a lightweight ablation matrix across many CSVs to compare which strict-mode pieces appear helpful:

```bash
python -m deploy.run_experiment_matrix \
  --csv data/*.csv \
  --out data/experiment_matrix.json
```

By default this compares:
- `loose_baseline`
- `strict_full`
- `strict_no_long_entry_led`
- `strict_long_entry_legacy`
- `strict_exit_legacy`
- `strict_no_maturity_gate`
- `strict_no_micro_gate`

The output includes per-run metrics plus an experiment-level ranked summary with average net return, expectancy, drawdown, trade count, win rate, time-in-market, and dominant blocked-entry reasons.

For a more honest rolling out-of-sample check, you can also run the first-pass walk-forward evaluator:

```bash
python -m deploy.run_walk_forward \
  --csv data/quote_backtests_clob_fresh/*.csv \
  --train-bars 12 \
  --test-bars 4 \
  --step-bars 4 \
  --out data/walk_forward.json
```

What it does:
- evaluates the predefined experiment profiles on each training window
- picks the best train-window profile by score
- tests that selected profile on the immediately following out-of-sample window
- rolls forward and repeats

This is still a simple discrete-profile selection test, not a full continuous parameter optimizer, but it is much more honest than choosing one profile on the full history and admiring the curve.

To export per-bar activity/state features for regime research:

```bash
python -m deploy.export_state_features \
  --csv data/quote_backtests_clob_fresh \
  --lookback-bars 5 \
  --out data/state_features.csv
```

The exporter emits features like:
- `spread_bps`, `abs_move_bps`, `lookback_move_bps`
- `quote_changed`, `quote_change_count`, `quote_change_ratio`
- `price_bucket`, `spread_bucket`, `movement_bucket`, `activity_bucket`
- `hours_to_resolution`, `resolution_bucket`
- `state_label` (combined spread/movement/activity regime)

This is meant for regime/state research first, so you can test whether certain market states are more tradable before building more complex models.

5. Dry-run a live decision:

```bash
python -m deploy.run_live \
  --condition-id YOUR_CONDITION_ID \
  --token-id YOUR_TOKEN_ID \
  --mid-price 0.51 \
  --best-bid 0.50 \
  --best-ask 0.52 \
  --decision-ts 2026-04-06T03:15:00Z
```

Dry-runs, no-trades, and blocked runs are logged to:
- `data/paper_trades.jsonl`

6. Paper-run a batch of eligible markets:

```bash
python -m deploy.paper_run_markets --limit 10 --max-runs 3
```

7. Summarize the paper log:

```bash
python -m deploy.paper_log_summary
python -m deploy.paper_log_summary --tail 20
python -m deploy.paper_log_summary --since 2026-03-28
```

8. Collect live quote snapshots for research:

```bash
python -m deploy.collect_quotes \
  --token-id YOUR_TOKEN_ID \
  --interval-seconds 30 \
  --output data/quote_snapshots.jsonl

# preferred for executable quote research: pull per-token quotes from the CLOB order book
python -m deploy.collect_quotes \
  --token-id YOUR_TOKEN_ID \
  --interval-seconds 30 \
  --use-clob-order-books \
  --output data/quote_snapshots.jsonl
```

You can also point it at a watchlist file or a saved `deploy.scan_markets` output:

```bash
python -m deploy.scan_markets --limit 50 --top 10 > data/scan_shortlist.json

python -m deploy.collect_quotes \
  --watchlist data/scan_shortlist.json \
  --interval-seconds 20 \
  --iterations 30 \
  --output data/quote_snapshots.jsonl
```

Or collect by condition id (this expands to all tokens under that condition):

```bash
python -m deploy.collect_quotes \
  --condition-id YOUR_CONDITION_ID \
  --interval-seconds 15 \
  --iterations 40
```

What gets stored:
- one JSON object per polled token snapshot (`.jsonl`)
- `timestamp`, `token_id`, `condition_id`, `outcome`
- `best_bid`, `best_ask`, `mid`, `spread`, `spread_bps`, `last_price`
- `quote_source`, `reference_price`, `market_level_best_bid`, `market_level_best_ask`
- when `--use-clob-order-books` is enabled: `clob_best_bid`, `clob_best_ask`, `clob_last_trade_price`
- helpful context when Gamma exposes it: `question`, `market_slug`, `market_family`, `liquidity`, `volume`, `end_date`, `created_at`
- `source` to distinguish normal polls from missing-target rows

This collector is intentionally lightweight and honest:
- it polls current Gamma market state on an interval
- it is for research data collection and later backtesting/analysis
- it is **not** a production execution feed or historical order book replay system
- quote quality depends on the selected source. Gamma-only mode is lightweight but may lack executable per-outcome quotes. `--use-clob-order-books` is preferred when you need real per-token best bid/ask.
- missed intra-interval changes are not captured

Quick inspection ideas after collection:

```bash
# peek at the latest rows
 tail -n 5 data/quote_snapshots.jsonl

# extract a single token's rows into a flat file for analysis
 python - <<'PY'
import json
from pathlib import Path
rows = []
for line in Path('data/quote_snapshots.jsonl').read_text().splitlines():
    row = json.loads(line)
    if row['token_id'] == 'YOUR_TOKEN_ID':
        rows.append(row)
print(rows[:3])
print(f"rows={len(rows)}")
PY
```

9. Scan structural bundle / no-arbitrage opportunities for research:

```bash
# executable-style scan using timestamp-aligned per-token quote CSVs
python -m deploy.scan_structural_arbitrage \
  --mode quote-backtests \
  --csv-dir data/quote_backtests \
  --ask-buffer 0.01 \
  --bid-buffer 0.01 \
  --min-reference-sum 0.95 \
  --max-reference-sum 1.05 \
  --out data/structural_arb_scan.json

# live reference scan from Gamma outcomePrices (research-only, not executable)
python -m deploy.scan_structural_arbitrage \
  --mode live \
  --limit 200 \
  --min-liquidity 1000 \
  --out data/structural_arb_live.json

# deterministic smoke test using the bundled fixture
python -m deploy.scan_structural_arbitrage \
  --mode live \
  --input-market-json data/structural_arb_fixture.json
```

Output notes:
- `quote-backtests` mode is the more actionable path when you already collected per-token bid/ask snapshots.
- It groups CSVs by `condition_id`, aligns timestamps, and flags:
  - `buy_bundle_under_1`: total best ask across all outcomes is below $1 by more than the buffer
  - `sell_bundle_over_1`: total best bid across all outcomes is above $1 by more than the buffer
- To suppress fake bundle signals from stale or inconsistent quotes, quote-backtest mode also requires the complementary reference close/mid values to sum near $1 (configurable with `--min-reference-sum` and `--max-reference-sum`).
- `live` mode currently uses Gamma `outcomePrices` as a reference snapshot to surface possible underround/overround markets. Treat this as idea generation, not executable truth.

The live runner keeps durable state at `data/live_state.json` by default. Override with `LIVE_STATE_PATH=/path/to/state.json`.

Before a live order, `deploy.run_live` attempts a best-effort reconciliation using exchange open orders and trade history, then refreshes the local state file. If reconciliation fails, `--execute` is blocked by default. You can inspect the reconciliation result in the CLI JSON output.

8. Send a real order:

```bash
python -m deploy.run_live \
  --condition-id YOUR_CONDITION_ID \
  --token-id YOUR_TOKEN_ID \
  --mid-price 0.51 \
  --best-bid 0.50 \
  --best-ask 0.52 \
  --decision-ts 2026-04-06T03:15:00Z \
  --execute
```

## Strict strategy mode

This repo now has an opt-in stricter strategy path for the exact problem case where the loose stack overtrades weak news/jump markets.

When `STRICT_STRATEGY_MODE=true` in live trading, or `--strict-mode` in backtests:

- **Entries are now long-entry-led**: in strict mode, a BUY starts with `long_entry` itself being `BUY` and clearing its own quality filters plus `MIN_ENTRY_CONFIDENCE`. MACD/RSI/CVD no longer act as co-equal gatekeepers.
- **Confirmers are optional weak modifiers**: non-`long_entry` BUY signals can add a small boost to a strict entry score, while SELL confirmers subtract a small penalty. If you want harder agreement, enable `STRICT_REQUIRE_CONFIRMERS` and set `STRICT_MIN_CONFIRMERS`.
- **Expected edge must clear cost**: the long-entry strategy now exposes a transparent `expected_edge_bps` estimate. A strict BUY is blocked unless that long-entry edge beats both `MIN_EXPECTED_EDGE_BPS` and `ESTIMATED_ROUND_TRIP_COST_BPS + EDGE_COST_BUFFER_BPS + observed_spread_bps`.
- **LongEntry v2 is smoother by design**: strict mode now prefers multi-window momentum alignment, persistence above a moving baseline, better breakout quality, and smoother trend efficiency rather than raw jumpiness. It also rejects one-bar shock moves and short-term volatility bursts.
- **Price regime is tighter**: strict mode still narrows the acceptable entry zone with `STRICT_MIN_PRICE` / `STRICT_MAX_PRICE`, but the lead signal now specifically wants gradual repricing instead of noisy spikes.
- **Bad-fit markets can be skipped earlier**: eligibility now layers family-level filtering on top of keywords, so sports/season-outright style markets can be favored while news/legal/discrete-jump markets are blocked more aggressively.
- **Churn is reduced without making exits sticky**: strict mode still respects `MIN_HOLD_BARS` and `COOLDOWN_BARS_AFTER_EXIT`, but now also supports transparent exit tools like `STRICT_MAX_HOLD_BARS`, `STRICT_FAIL_EXIT_BARS`/`STRICT_FAIL_EXIT_PNL_BPS`, pullback-based profit protection via `STRICT_TAKE_PROFIT_*`, and a softer `STRICT_EXTENDED_HOLD_EXIT_GAP` after long holds.

Default middle-ground strict profile:
- `MIN_ENTRY_CONFIDENCE=0.50`
- `MIN_BUY_SCORE=1.1` (legacy aggregate knob; not the primary strict BUY gate anymore)
- `MIN_BUY_SELL_SCORE_GAP=0.35` (still used for stricter exits)
- `MIN_BUY_SIGNAL_COUNT=1` (legacy aggregate knob; mainly kept for older comparisons)
- `STRICT_REQUIRE_CONFIRMERS=false`
- `STRICT_MIN_CONFIRMERS=0`
- `STRICT_CONFIRMER_BUY_BONUS=0.08`
- `STRICT_CONFIRMER_SELL_PENALTY=0.12`
- `STRICT_MIN_ENTRY_SCORE=0.55`
- `MIN_HOLD_BARS=3`
- `COOLDOWN_BARS_AFTER_EXIT=2`
- `STRICT_MAX_HOLD_BARS=12`
- `STRICT_FAIL_EXIT_BARS=6`
- `STRICT_FAIL_EXIT_PNL_BPS=-35`
- `STRICT_TAKE_PROFIT_BARS=4`
- `STRICT_TAKE_PROFIT_PNL_BPS=80`
- `STRICT_PROFIT_GIVEBACK_BPS=45`
- `STRICT_EXTENDED_HOLD_BARS=8`
- `STRICT_EXTENDED_HOLD_EXIT_GAP=0.15`
- `ESTIMATED_ROUND_TRIP_COST_BPS=80`
- `MIN_EXPECTED_EDGE_BPS=120`
- `EDGE_COST_BUFFER_BPS=30`
- `STRICT_MIN_PRICE=0.18`
- `STRICT_MAX_PRICE=0.68`
- `STRICT_EXCLUDED_KEYWORDS=election,war,ceasefire,attack,assassination,indictment,sentenced,convicted,supreme court,sec,etf approval,fed rate,tariff,sanction`
- `MARKET_FAMILY_MODE=balanced`
- `ALLOWED_MARKET_FAMILIES=sports_outright,crypto_outright,award_outright,entertainment_outright,scheduled_event`
- `BLOCKED_MARKET_FAMILIES=news_breaking,legal_regulatory,war_geopolitics,disaster,assassination,discrete_binary,event_resolution`
- `FAMILY_ALLOW_KEYWORDS=qualify,win the,advance to,reach the playoffs,champion,...`
- `FAMILY_BLOCK_KEYWORDS=sentenced,indicted,convicted,arrested,supreme court,appeal,ceasefire,attack,...`
- `LLM_MARKET_CLASSIFIER_PATH=data/market_classifier_output.json`
- `OPENAI_API_KEY=...`
- `OPENAI_MODEL=gpt-5.4`
- `OPENAI_CLASSIFIER_INPUT_PATH=data/market_classifier_input.json`
- `OPENAI_CLASSIFIER_OUTPUT_PATH=data/market_classifier_output.json`
- `OPENAI_CLASSIFIER_BATCH_SIZE=8`
- `OPENAI_CLASSIFIER_PAUSE_SECONDS=1.5`

Useful env knobs:
- `STRICT_STRATEGY_MODE`
- `MIN_ENTRY_CONFIDENCE`
- `MIN_BUY_SCORE`
- `MIN_BUY_SELL_SCORE_GAP`
- `MIN_BUY_SIGNAL_COUNT`
- `STRICT_REQUIRE_CONFIRMERS`
- `STRICT_MIN_CONFIRMERS`
- `STRICT_CONFIRMER_BUY_BONUS`
- `STRICT_CONFIRMER_SELL_PENALTY`
- `STRICT_MIN_ENTRY_SCORE`
- `MIN_HOLD_BARS`
- `COOLDOWN_BARS_AFTER_EXIT`
- `STRICT_MAX_HOLD_BARS`
- `STRICT_FAIL_EXIT_BARS`
- `STRICT_FAIL_EXIT_PNL_BPS`
- `STRICT_TAKE_PROFIT_BARS`
- `STRICT_TAKE_PROFIT_PNL_BPS`
- `STRICT_PROFIT_GIVEBACK_BPS`
- `STRICT_EXTENDED_HOLD_BARS`
- `STRICT_EXTENDED_HOLD_EXIT_GAP`
- `ESTIMATED_ROUND_TRIP_COST_BPS`
- `MIN_EXPECTED_EDGE_BPS`
- `EDGE_COST_BUFFER_BPS`
- `STRICT_MIN_PRICE`
- `STRICT_MAX_PRICE`
- `STRICT_EXCLUDED_KEYWORDS`
- `MARKET_FAMILY_MODE`
- `ALLOWED_MARKET_FAMILIES`
- `BLOCKED_MARKET_FAMILIES`
- `FAMILY_ALLOW_KEYWORDS`
- `FAMILY_BLOCK_KEYWORDS`
- `LLM_MARKET_CLASSIFIER_PATH`

### Maturity and microstructure gating (strict mode)

The bot now includes optional gating to avoid bad temporal and liquidity regimes before signals matter. The defaults are intentionally conservative-but-usable for sparse Polymarket quote series, not idealized order books.

- Maturity awareness uses market metadata (`endDate`, `resolutionTime`, `end_date_iso`, etc.) when available. Configure a sweet-spot window via:
  - `ENABLE_MATURITY_GATING` (default: true)
  - `STRICT_MIN_TTR_HOURS`, `STRICT_MAX_TTR_HOURS` (time to resolution)
  - `STRICT_MIN_SINCE_OPEN_HOURS` (avoid brand-new listings)

- Microstructure quality prefers real historical best bid/ask snapshots when present.
- When historical quotes are missing, backtests can now use an explicit synthetic proxy layer (`--microstructure-proxy-policy auto`, the default) instead of silently treating the series as quote-blind.
  - The proxy is seeded from current Gamma market metadata when available (`market_current_spread_bps`, `market_liquidity`, current `bestBid`/`bestAsk`) plus coarse penalties for tail pricing, realized volatility, and zero-volume bars.
  - This is intentionally a conservative liquidity/spread proxy, not fake historical order-book reconstruction.
  - Use `--microstructure-proxy-policy real-only` if you want the old behavior and only trust actual historical bid/ask.
- Relevant knobs:
  - `ENABLE_MICROSTRUCTURE_GATING` (default: true)
  - `STRICT_QUOTE_LOOKBACK_BARS` (default: 24)
  - `STRICT_MIN_QUOTE_OBSERVATIONS` (default: 3; the gate only activates once at least this many quoted-or-proxied bars exist)
  - `STRICT_WIDE_SPREAD_BPS` (default: 700)
  - `STRICT_MIN_QUOTE_AVAIL_RATIO` (default: 0.25)
  - `STRICT_MAX_AVG_SPREAD_BPS` (default: 450)
  - `STRICT_MAX_CURRENT_SPREAD_BPS` (default: 450)
  - `STRICT_MAX_WIDE_SPREAD_RATE` (default: 0.65)

Live dry-runs and backtests surface `signal_summary.maturity` and `signal_summary.microstructure`, and backtests also emit a run-level `microstructure` block so you can see whether a run used real quotes, proxy estimates, or a mix.

Optional LLM hook: if you point `LLM_MARKET_CLASSIFIER_PATH` at a JSON file of per-market decisions, `MarketFilter` will merge that classifier output into the heuristic family filter. This stays offline-ish: you precompute a JSON artifact, then the bot only reads the file during scans/live eligibility checks. No synchronous OpenAI call is made while trading.

### OpenAI-assisted market classification workflow

The repo now includes a lightweight export → classify → consume loop:

1. **Export candidate markets**

```bash
python -m deploy.export_market_classifier_input --limit 40 --top 25
```

This writes `data/market_classifier_input.json` by default (override with `OPENAI_CLASSIFIER_INPUT_PATH` or `--output`).
The export contains market IDs, question text, category/description, liquidity/quote/history metrics, heuristic family, and current eligibility context so the LLM can judge market *structure* rather than make vague predictions.

2. **Classify offline with OpenAI**

```bash
python -m deploy.classify_markets_openai
```

By default this reads `OPENAI_CLASSIFIER_INPUT_PATH`, sends markets in batches (`OPENAI_CLASSIFIER_BATCH_SIZE`, default `8`), pauses between requests (`OPENAI_CLASSIFIER_PAUSE_SECONDS`, default `1.5`), and writes `data/market_classifier_output.json`.

Useful options:

```bash
python -m deploy.classify_markets_openai --dry-run
python -m deploy.classify_markets_openai --input data/custom_input.json --output data/custom_output.json --batch-size 5
```

`--dry-run` is handy for validating the pipeline without real API credentials.

3. **Let the bot consume the output**

Set:

```bash
LLM_MARKET_CLASSIFIER_PATH=data/market_classifier_output.json
```

Then run the usual tools (`deploy.scan_markets`, `deploy.paper_run_markets`, `deploy.run_live`, `deploy.research_loop`). They will load the precomputed JSON if present.

### Output format

The classifier output is a JSON artifact with a `records` array plus indexes by `condition_id` and `token_id`. Each record includes fields like:

- `condition_id`
- `token_id`
- `question`
- `decision`: `allow | avoid | review`
- `family`
- `regime_labels`
- `confidence`
- `risk_flags`
- `rationale`
- `tradable`
- `score_adjustment`

### How classifier output influences filtering

- `decision=allow` → market remains tradable and gets a small positive `score_adjustment`.
- `decision=avoid` → market is marked non-tradable and is blocked during eligibility checks.
- `decision=review` → market is also marked non-tradable by default, so it will not auto-trade until you change the record manually.
- `family`, `risk_flags`, `confidence`, and `rationale` are carried through into `market_family.classifier_metadata` so scan/live outputs explain *why* a market was accepted or rejected.
- If no matching record exists, the bot falls back to the existing heuristic family logic.

Prompting is intentionally focused on market regime fit (scheduled outrights vs breaking-news/discrete-resolution risk, liquidity/spread concerns, ambiguity, etc.), not on asking the model to predict event outcomes or place trades.

Tradeoff: this mode should produce fewer trades and lower churn. It will also miss some winners. That is intentional.

For single-market comparisons, `deploy.run_backtest` now also exposes:
- `--long-entry-version v2|legacy`
- `--strict-exit-style upgraded|legacy`
- `--strict-long-entry-led` / `--no-strict-long-entry-led`

That makes it easier to sanity-check one market before running the full matrix.

## Live execution guardrails

When `--execute` is used, the bot blocks submission if any of these checks fail:

- **Trusted freshness**: by default, live execution requires `--decision-ts`. If the timestamp is missing or older than `MAX_DECISION_AGE_SECONDS`, the order is blocked.
- **Spread sanity**: if `--best-bid` and `--best-ask` are supplied, the bot computes spread bps and blocks when it exceeds `MAX_SPREAD_BPS`.
- **Price/reference sanity**: the intended limit price is compared against the observed mid and the same-side quote (`best_bid` for buys, `best_ask` for sells). Excessive deviation is blocked.
- **Open-order caps**: configurable max open orders globally and per token. Duplicate-token suppression remains enabled by default.
- **Cooldown pacing**: recent submissions and recent fills are tracked in live state, and new live orders are blocked until `SUBMISSION_COOLDOWN_SECONDS` / `FILL_COOLDOWN_SECONDS` expire.

All of the computed guard metrics are returned under `guardrails` in the CLI JSON so you can inspect exactly why something was blocked.

## Data Download

For exchange OHLCV data, use `ccxt` through [`data/downloader.py`](/Users/mac/Desktop/polymarket-rbi-bot/data/downloader.py). This is useful for external proxy signals like BTC, ETH, or SOL, but it is not the primary backtest source for a Polymarket execution bot.

For Polymarket token history, use [`data/polymarket_client.py`](/Users/mac/Desktop/polymarket-rbi-bot/data/polymarket_client.py), which calls the CLOB price-history endpoint and reshapes the result into backtest-friendly rows. It now also attaches current Gamma market metadata (liquidity/category/current spread snapshot) to each exported row so quote-poor backtests have an inspectable proxy context. This should be your default source for MACD and RSI backtests on a Polymarket bot.

## Data Assumptions

`deploy.run_backtest` expects a CSV with:

- `timestamp`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `best_bid` optional
- `best_ask` optional
- optional maturity metadata columns if you want time-aware backtests: `resolution_ts` / `end_ts` / `end_time` / `endDate` and `open_ts` / `open_time` / `createdAt`
- optional market metadata columns for inspectable proxying: `market_liquidity`, `market_current_spread_bps`, `market_best_bid`, `market_best_ask`, `market_category`, `market_question`, `market_metadata_quote_note`

The Polymarket price-history endpoint is still price-only in this project, so it is best suited to MACD and RSI. We are not claiming to have true historical quote history here. The new backtest path is: use real historical `best_bid`/`best_ask` when you have them, otherwise optionally fall back to an explicit synthetic microstructure proxy. CVD should only be enabled when your snapshots contain real trade-flow data.

## Local Dashboard (MVP)

A lightweight, zero-dependency HTTP dashboard is included for local observability of positions, orders/fills, PnL, health, and recent paper/live decisions.

- Entry point: `dashboard/server.py`
- Data sources it reads (no network required):
  - `data/live_state.json` (positions, open orders, fills, realized PnL, cooldowns, reconcile status)
  - `data/paper_trades.jsonl` (recent decisions: dry-run approvals, blocks, no-trade reasons, intents, signal summaries)
  - `data/market_classifier_output.json` (optional; summarized count only)

How to run:

```bash
python -m dashboard.server --host 127.0.0.1 --port 8008
# then open http://127.0.0.1:8008 in a browser
```

Quick JSON-only smoke test (no server):

```bash
python -m dashboard.server --dump | head -200
```

What it shows:
- Positions/exposure: token, qty, avg price, latest mid/bid/ask from recent paper runs, unrealized PnL (and bps), time held, time-to-resolution (when present in recent decisions).
- Orders/fills: open orders from local state, and the most recent fills recorded.
- PnL/metrics: realized, unrealized, and total from local state plus simple mark-to-mid.
- Bot health/guardrails: reconcile status/message, last submission/fill timestamps, last paper-log recency, strict/buy-only flags, and classifier path if configured.
- Decision/classifier context: most recent `signal_summary` (scores, edge, spread, maturity/microstructure, and per-strategy reasons) from paper runs.

Limitations (MVP):
- Quotes and maturity data come from the latest paper/logged decisions, not live websockets; unrealized PnL is approximate.
- Daily PnL and time-weighted exposure are not reconstructed from logs.
- Research artifacts are not visualized; only a classifier-output count is shown if present.
- Open orders and fills depend on `data/live_state.json` being refreshed by `deploy.run_live` reconciliation.

## Notes

- This repo is a strong starter, not a production-complete market-making system.
- The new stricter mode is meant to be honest and conservative, not magically profitable. Validate it on fresh samples before trusting it.
- The bot persists positions/orders/fills locally, but it still does not stream websocket updates or reconcile cancels/partial fills beyond the fields exposed by current REST responses.
- The freshness guard assumes the caller provides a trustworthy `--decision-ts` for the actual quote snapshot used to make the decision.
- Polymarket prices should remain between `0` and `1`, and this project enforces that constraint.
