# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in Polymarket credentials
```

There is no test suite, linter, or packaging config in the repo — modules are run directly as scripts via `python -m deploy.<name>` or `python deploy/<name>.py`.

## Common entry points (all under `deploy/`)

- `deploy/run_backtest.py` — CSV- or token-driven backtest. Flags: `--strict-mode`, `--long-entry-version v2|legacy`, `--strict-exit-style upgraded|legacy`, `--microstructure-proxy-policy auto|real-only`, slippage/spread knobs.
- `deploy.run_experiment_matrix` — compares predefined strict-mode profiles across many CSVs.
- `deploy.run_walk_forward` — rolling train/test discrete-profile selection.
- `deploy.run_live` — live or dry-run decision for a single token. `--execute` submits an order; without it, the decision is logged to `data/paper_trades.jsonl`.
- `deploy.paper_run_markets` — batched paper-run across eligible markets.
- `deploy.paper_log_summary` — summarize `data/paper_trades.jsonl` (supports `--tail`, `--since`).
- `deploy.collect_quotes` — poll Gamma/CLOB for quote snapshots into JSONL. Use `--use-clob-order-books` for executable per-token bid/ask.
- `deploy.scan_markets` / `deploy.scan_structural_arbitrage` — research scanners.
- `deploy.export_market_classifier_input` → `deploy.classify_markets_openai` — offline LLM market-classification pipeline; output is consumed via `LLM_MARKET_CLASSIFIER_PATH`.
- `dashboard.server` — zero-dependency local HTTP observability dashboard reading `data/live_state.json` + `data/paper_trades.jsonl`. `python -m dashboard.server --dump` for JSON-only smoke test.

The README has extensive runnable examples for each command — consult it for flag combinations rather than inventing new ones.

## Architecture

The codebase is split so execution concerns and signal concerns never mix:

- `strategies/` — **signal generation only**. Each strategy subclasses `BaseStrategy` (`strategies/base.py`) and returns a `StrategySignal` (side/confidence/price/reason) from `generate_signal(history)`. No exchange calls, no state mutation. Strategies: `long_entry_strategy` (the primary lead in strict mode), `macd_strategy`, `rsi_strategy`, `cvd_strategy`.
- `bot/` — **execution, risk, state**. `PolymarketTrader` (`bot/trader.py`) owns the `py-clob-client` connection and turns signals into `OrderIntent`s; `RiskManager` applies deterministic pre-submit checks (notional cap, position cap, daily loss, price sanity, spread sanity, cooldowns, open-order caps); `LiveStateStore` (`bot/state.py`) persists positions/open orders/fills/realized PnL/cooldown timestamps atomically to `data/live_state.json`; `MarketFilter` + `MarketClassifier` apply eligibility/family/keyword/LLM-classifier gating.
- `backtesting/engine.py` — a single `BacktestEngine` that replays `MarketSnapshot` lists through the same strategy classes. It mirrors the strict-mode knobs from `BotConfig` (confidence/edge/hold/cooldown/maturity/microstructure gates) so backtests and live runs behave consistently. When historical quotes are missing it can use a synthetic microstructure proxy seeded from current Gamma metadata.
- `polymarket_rbi_bot/` — shared primitives: `config.py` (`BotConfig.from_env`, the single source of truth for every tunable env var), `models.py` (dataclasses: `Candle`, `MarketSnapshot`, `StrategySignal`, `OrderIntent`, `Position`, `Fill`, `BacktestResult`), `microstructure.py` (spread/TTR/proxy helpers used by both the engine and the live trader), `data.py` (CSV loaders).
- `deploy/` — thin CLI glue: parse args, build `BotConfig`, instantiate strategies + trader/engine, print JSON. Business logic should not live here.
- `data/` — runtime artifacts (`live_state.json`, `paper_trades.jsonl`, classifier I/O, backtest CSVs) **plus** the Polymarket/ccxt downloaders (`data/polymarket_client.py`, `data/downloader.py`). Polymarket history is price-only, so it is best suited to MACD/RSI — not quote-dependent strategies.
- `dashboard/server.py` — stdlib-only HTTP server; reads the same JSON/JSONL files, no exchange connectivity.

### Strict strategy mode (opt-in)

`STRICT_STRATEGY_MODE=true` (live) or `--strict-mode` (backtest) fundamentally changes how entries work:

- BUY is **long-entry-led**: `LongEntry` itself must be BUY and clear `MIN_ENTRY_CONFIDENCE`. MACD/RSI/CVD become weak confirmers (configurable bonus/penalty), not co-equal gatekeepers.
- Expected edge must beat `MIN_EXPECTED_EDGE_BPS` AND `ESTIMATED_ROUND_TRIP_COST_BPS + EDGE_COST_BUFFER_BPS + observed_spread_bps`.
- Maturity gating (`ENABLE_MATURITY_GATING`) uses `endDate`/`resolutionTime` from market metadata.
- Microstructure gating (`ENABLE_MICROSTRUCTURE_GATING`) requires quote availability/spread quality thresholds; backed by synthetic proxy when real historical quotes are sparse.
- Exit tools: `STRICT_MAX_HOLD_BARS`, `STRICT_FAIL_EXIT_*`, `STRICT_TAKE_PROFIT_*`, `STRICT_PROFIT_GIVEBACK_BPS`, `STRICT_EXTENDED_HOLD_*`.
- Market-family filter uses `ALLOWED_MARKET_FAMILIES` / `BLOCKED_MARKET_FAMILIES` + `FAMILY_ALLOW_KEYWORDS` / `FAMILY_BLOCK_KEYWORDS`, optionally overridden by the offline LLM classifier JSON (`LLM_MARKET_CLASSIFIER_PATH`).

Strict mode is designed to trade **less** and miss some winners — that is intentional. Validate on fresh samples before trusting output.

### Live execution guardrails

`deploy.run_live --execute` blocks submission when any of these fail, and surfaces the computed metrics under `guardrails` in the JSON output:

- **Freshness**: `--decision-ts` required (unless `REQUIRE_LIVE_DECISION_TS=false`) and must be within `MAX_DECISION_AGE_SECONDS`.
- **Spread sanity**: derived from `--best-bid`/`--best-ask` vs. `MAX_SPREAD_BPS`.
- **Price/reference sanity**: limit price vs. mid and same-side quote.
- **Open-order caps** + **duplicate-token suppression**.
- **Cooldowns**: `SUBMISSION_COOLDOWN_SECONDS`, `FILL_COOLDOWN_SECONDS` tracked in `LiveStateStore`.
- Before any order, a best-effort reconciliation against exchange open orders + trade history refreshes local state; `--execute` is blocked if reconciliation fails.

## Development rules (from AGENTS.md)

- Keep strategies side-effect-free — no exchange calls inside `generate_signal`.
- Execution stays **limit-order only** via `py-clob-client` unless requirements change explicitly.
- Prefer adding new strategy classes over embedding strategy-specific branches in the trader.
- Risk checks must stay deterministic and testable.
- When behavior depends on external market data or wallet state, document the assumption at the call site.
- Polymarket prices are enforced to be within `(0, 1)`.

## Backtest CSV schema

`deploy.run_backtest --csv` expects: `timestamp, open, high, low, close, volume`, with optional `best_bid`/`best_ask`, maturity metadata (`resolution_ts`/`end_ts`/`endDate`, `open_ts`/`createdAt`), and market metadata (`market_liquidity`, `market_current_spread_bps`, `market_best_bid`/`market_best_ask`, `market_category`, `market_question`). Missing quote columns trigger the synthetic microstructure proxy when `--microstructure-proxy-policy auto` (default).
