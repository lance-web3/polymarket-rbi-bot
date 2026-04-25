# Polymarket RBI Bot — Path to Profitability

Owner: makusho95@gmail.com
Started: 2026-04-20
Goal: build this repo into a durable passive-income stream.

This plan is **staged and gated**. Each phase has a go/no-go decision at the end. We do not advance to the next phase until the current phase's ship criteria are met. We do not throw good capital after bad signals.

Status legend: `[ ]` pending · `[~]` active · `[x]` done · `[-]` abandoned (with reason)

---

## 0. Reality check (read before trading anything)

- "Retirement income" is an ambitious framing for a single bot on a single venue. To net ~$50k/year you need either (a) large capital at a modest edge, or (b) a small capital at an unusually stable high Sharpe — rare, and generally doesn't hold when scaled.
- Polymarket constraints that cap realistic scaling:
  - Thin per-market liquidity (most markets cannot absorb size without moving).
  - Event-driven jumps dominate slow mean-reversion.
  - Informed participants absorb most directional edge in news/legal/political markets.
  - Limit-order-only execution with discrete tick sizes.
- What this means operationally:
  - Treat this bot as **one stream** of a diversified passive-income strategy, not the whole strategy.
  - Pick an edge that compounds on small capital first. Scale only after live proves it.
  - The current README itself says: "This repo is a strong starter, not a production-complete market-making system." Take that seriously.

**Working target (not a promise):** 10–25% annualized net on trading capital, Sharpe > 1.0 over 3+ months of live, low correlation to crypto. We will revise this target after Phase 2.

---

## 1. Edge hypothesis (what we actually bet on)

Ranked by how well this codebase is currently positioned to capture each:

1. **Structural arbitrage / no-arb bundles** — complementary tokens should sum to ~$1. `deploy/scan_structural_arbitrage.py` already scans for this; it is research-only today. This is the most defensible Polymarket edge for a small retail bot because it doesn't require predicting outcomes.
2. **Regime/family filtering** — trading only in market families where short-horizon price dynamics are more predictable (scheduled sports outrights, season awards) and aggressively avoiding news/legal/political/event-resolution markets. `bot/market_filter.py` + the OpenAI classifier pipeline already exist.
3. **Maker-style passive quoting** — capture spread in mature, liquid, long-horizon markets. Requires order book / websocket infrastructure we do **not** have yet.
4. **Directional technical signals (MACD/RSI/CVD/LongEntry)** — the repo's current default. Working hypothesis: these do **not** survive honest transaction costs on event markets. Phase 2 will falsify or confirm this. If they don't survive, we stop running them.

We will commit capital to (1) and (2) first, (3) conditionally, (4) only if Phase 2 shows survival.

---

## 2. Phases and ship criteria

### Phase 0 — Baseline and journaling (target: week 1)

- [x] Decide risk-of-ruin capital: the amount whose **total loss** would not change your life. `RISK_CAPITAL_USD = 500`.
- [x] Decide minimum viable edge (MVE) threshold:
  - **Arb track:** net edge ≥ **30 bps** per round-trip, after spreads paid on all legs + fees. (Convergence is the exit, so spread is paid only on entry.)
  - **Directional track:** net edge ≥ **100 bps** per round-trip, after fees and spread paid on both entry and exit.
- [ ] Start a decision journal: every meaningful change (parameter, code, strategy) logged with date + reason + expected impact. Use the **Progress log** section at the bottom of this file.
- [x] Reconcile `.env` — confirm we are pointed at the real CLOB host and the wallet/funder that will actually trade. Do not skip.
  - Config loads clean: CLOB host + Gamma host correct (mainnet chain 137), `PRIVATE_KEY` / API triple / `FUNDER_ADDRESS` all populated, `has_l2_auth=True`. `.env` gitignored, never committed.
  - `SIGNATURE_TYPE=2` confirmed correct — prior successful place + cancel round-trip on the current wallet is proof the signature type matches the account.

Ship criteria: `RISK_CAPITAL_USD`, MVE, and journal set up.

---

### Phase 1 — Measurement infrastructure (target: weeks 1–3)

We cannot improve what we cannot measure honestly. Every later phase depends on these.

- [~] Stand up a persistent background quote collector for 20–50 candidate markets using `deploy.collect_quotes --use-clob-order-books`. Run it long enough (≥ 3 weeks) to have real per-token order-book history, not Gamma proxies.
  - Watchlist: `data/scan_shortlist.json` (50 condition-ids, ~100 tokens after YES/NO expansion, 47 A-tier from `scan_markets --limit 200 --top 50`).
  - LaunchAgent: `deploy/com.polymarket.rbi-bot.quote-collector.plist`. Install with: `cp deploy/com.polymarket.rbi-bot.quote-collector.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.polymarket.rbi-bot.quote-collector.plist`.
  - Output: `data/quote_collection/run.jsonl` (append-only). Logs: `data/quote_collection/collector.{out,err}.log`.
  - Pre-req for ≥3 weeks of continuous data: prevent laptop sleep — `sudo pmset -a sleep 0 disksleep 0` (or keep it plugged in + lid-open + set Energy Saver "prevent automatic sleeping").
  - Periodic refresh: re-run `python -m deploy.scan_markets --limit 200 --top 50 > data/scan_shortlist.json` every ~7 days so resolved markets get rotated out. LaunchAgent will pick up the new watchlist on next restart (`launchctl kickstart -k gui/$UID/com.polymarket.rbi-bot.quote-collector`).
- [x] Audit `data/paper_trades.jsonl`: every logged decision must contain enough context to replay it (mid, bid, ask, decision_ts, strategy scores, guardrail state). Patch `bot/paper_log.py` if fields are missing.
  - Audit of 28 existing entries: mid, condition_id, token_id, timestamp, status, reason were always present. Gap: `best_bid`/`best_ask` only on blocked entries (flowing through guardrails); missing on dry_run/no_trade. Spread-replay impossible for those.
  - Patched `deploy/run_live.py::log_if_paper` to always emit `best_bid`, `best_ask`, and compute `observed_spread_bps` on every entry. Added `schema_version: 1` so future shape bumps are traceable. `decision_ts` capture already existed; now surfaces consistently when caller passes `--decision-ts`.
  - Verified with a fresh dry-run: new entry carries all replay fields.
- [x] Add an explicit **cost model** to the backtest engine that attributes PnL into: entry-side spread paid, exit-side spread paid, slippage vs mid, fees, adverse selection (fill only when market moved against us). Even a crude attribution is better than a single PnL number.
  - `BacktestEngine`: new `fee_bps` + `adverse_selection_horizon_bars` knobs. Per trade: `spread_cost_bps`, `slippage_cost_bps`, `fee_cost_bps`, `post_fill_move_bps`, `notional`, `mid_at_fill`. Aggregated in `result.metadata.cost_attribution`: gross vs net pnl, USD cost buckets, adverse-fill ratio.
  - `deploy.run_backtest` flags: `--fee-bps`, `--adverse-selection-horizon-bars`. Output JSON now includes a top-level `cost_attribution` block.
  - `deploy.run_experiment_matrix` passes cost_attribution through per-row.
- [ ] Extend the dashboard (`dashboard/server.py`) to show this cost attribution on recent decisions. (Optional; nice-to-have.)

Ship criteria: at least 3 weeks of real CLOB order-book snapshots for ≥ 20 markets, and a cost-attributed backtest report.

---

### Phase 2 — Honest research on existing tooling (target: weeks 2–6)

With real collected data, run the research tools that already exist. Do not build new strategies in this phase.

- [ ] Run `deploy.run_experiment_matrix` across the collected CSVs. Record ranked profiles.
- [ ] Run `deploy.run_walk_forward` on the same data with `--train-bars 12 --test-bars 4 --step-bars 4`. This is the honesty test — out-of-sample only.
- [ ] Run `deploy.export_state_features` to inspect which regimes the bot actually trades in vs. which are profitable.
- [ ] Fit the above against the cost model from Phase 1. The only number that matters is **out-of-sample net expectancy after honest costs**.
- [ ] **Calibration check (new).** For every BUY decision the signal stack would have made, record (model_prob, market_prob_at_entry, resolution). At the end of Phase 2, compute:
  - Brier score of model vs market prob on ≥ 200 decisions.
  - Calibration curve binned by model probability (are bins reliable?).
  - Edge-after-costs per decile of confidence.
  A strategy that is "often right" on direction can still lose money if its probabilities are poorly calibrated and it enters at bad prices. Calibration is the honest test of whether the signal stack's confidence means anything. If Brier(model) ≥ Brier(market-implied), the stack is adding noise — treat that as a no-pass.

**Decision gate — hard:**
- If at least one strict-mode profile is out-of-sample net-positive after costs on fresh data across ≥ 10 markets, continue with that profile as a **secondary** overlay.
- If **no profile** passes, mark directional signal trading `[-]` abandoned and commit to the structural-arb path exclusively. Do not rescue the signal stack with more parameters — that's overfitting.

Ship criteria: documented decision about the signal stack in the Progress log.

---

### Phase 3 — Build the primary edge (target: weeks 4–10)

Primary track: **structural arbitrage as an executable strategy.**

- [ ] Extend `deploy/scan_structural_arbitrage.py` to emit *persistence*: how long does each observed bundle mispricing last in the collected quote history? Only mispricings that persist > latency + execution window are actionable.
- [ ] Model multi-leg execution risk: for a 2–4 leg bundle, cost = sum of spreads paid + race/latency slippage + fees. Only trade bundles whose observed edge exceeds this by ≥ MVE.
- [ ] Add an executable `deploy/run_arb.py` that: monitors selected condition IDs, confirms the current bundle still violates no-arb, sizes each leg to risk cap, places limit orders (respecting `RiskManager`), and auto-cancels stale legs.
- [ ] Paper-run it for ≥ 2 weeks. Compare hypothetical fills to what `deploy.collect_quotes` actually saw in the same window.

Secondary track (if Phase 2 produced a survivor): run the strict-mode signal profile as an overlay in allowed market families only, at half the arb size.

Ship criteria: 2+ weeks of paper arb runs where hypothetical PnL (after costs) clears MVE and paper fill rate is realistic.

---

### Phase 4 — Small live pilot (target: weeks 8–16)

- [ ] Start with capital = min(`RISK_CAPITAL_USD`, $500). Hard cap: do not raise this mid-pilot.
- [ ] Tighten `RiskManager` limits: `MAX_NOTIONAL_PER_ORDER`, `DAILY_LOSS_LIMIT`, `MAX_OPEN_ORDERS_TOTAL` — cut defaults in half for the pilot.
- [ ] Daily review ritual: diff `data/live_state.json` fills vs. `data/paper_trades.jsonl` predictions. Any divergence > 50 bps gets investigated the same day.
- [ ] Weekly review: realized PnL, slippage vs model, cancels, missed arbs.

**Kill criteria (any one trips, pause live):**
- Two consecutive weeks of net-negative after costs.
- Fill quality > 75 bps worse than paper on average.
- Any single unexplained loss > 2× daily loss limit.
- Reconciliation failures > 10% of sessions.

Ship criteria: 8 weeks of live without tripping a kill criterion, net positive after costs.

---

### Phase 5 — Scale (months 4+)

- [ ] Only if Phase 4 cleared: scale capital in 2× steps, no more frequent than monthly. Do not scale complexity.
- [ ] Track capacity: at each step, check whether fill rate or slippage degraded. If they did, you hit venue capacity — stop scaling, don't fight it.
- [ ] Add a second, uncorrelated edge candidate only after the first is stable at target size. Ranked candidates:
  1. **Maker-style passive quoting** in mature high-liquidity sports outrights. Requires websocket/order-book infrastructure (not present today — 2–4 week build).
  2. **LLM-probability engine** (AI-as-updater, not oracle) on narrow niches where information is messy and the consensus is slow. Scope:
     - Build a news/evidence ingestion pipeline (RSS + Gamma market descriptions + scheduled data releases). 2–3 week build.
     - For each selected market, prompt the model for `(p_yes, confidence, top 3 evidence points, "what would flip this")`. Reuse the existing `classify_markets_openai` offline pattern — no synchronous LLM calls inside the trade loop.
     - Trade only when `|model_prob − market_prob| × notional > MVE + 2 × estimated_costs_bps` and Phase 2's calibration check said this niche was calibrated.
     - Honest caveat: LLMs compete poorly on breaking-news markets where insiders and scrapers are faster. Pick niches (earnings-call wording, macro-data-release markets, crypto governance) where messy-text parsing genuinely helps.
  3. **Information overlay** in families you personally track (human in the loop, bot executes).

  Only build one. Picking the LLM track before the maker track commits us to the news infrastructure; do not build both in parallel.

Ship criteria: 3+ months of live at scaled size with PnL consistent with Phase 4 run-rate.

---

### Phase 6 — Operationalize (months 6+)

- [ ] Alerting: any guardrail trip, reconciliation failure, or daily-loss-limit hit pages you.
- [ ] Backups: `data/live_state.json` replicated off the laptop.
- [ ] Multi-wallet support for redundancy and capital segregation.
- [ ] Documented manual override playbook for black-swan events (venue outage, compromised key, stuck order).
- [ ] Tax / accounting hygiene. Prediction-market income has real tax implications — get this right before it becomes a bigger number.

Ship criteria: the bot can run for 2 weeks without you touching it and nothing bad happens.

---

## 3. Off-ramps (when to stop)

- **After Phase 2 with no signal-stack survivor and no actionable arb persistence**: shelf the live trading plan, keep the repo as a research tool only.
- **After Phase 4 with kill criteria tripped twice**: pause live for ≥ 1 month, re-run Phase 2 on fresh data before re-enabling.
- **Personal bankroll stress**: if the bot occupies more emotional bandwidth than the income justifies, it's not passive. Scale down or stop.

Stopping is a valid outcome. This file should be updated with the stop reason if we reach one.

---

## 4. Principles

- **Measure first, trade second.** Phase 1 exists because every Phase 2+ decision is worthless without honest costs.
- **Pick one edge.** A single working strategy at stable size beats five weak ones.
- **Out-of-sample only.** If we tune on the same data we evaluate on, we are lying to ourselves.
- **Scale capital, not complexity.** New code is new risk.
- **Kill criteria are sacred.** If we lower them mid-pilot, we don't have kill criteria.

---

## 5. Progress log

Append entries as we go. Newest at top. Each entry: date, phase, what changed, expected impact, actual outcome (backfilled later).

- 2026-04-23 — Phase 2 — Track B plumbing **shipped**. `polymarket_rbi_bot/calibration.py` (Brier + reliability bins, stdlib-only); `BacktestEngine` accepts `market_filter` + `market_metadata` and short-circuits to a zero-trade result with `skipped_by_family_filter=true` when the family/keyword gates reject; `MarketFilter.evaluate_family_only` runs gates without a history fetch; `data/csv_metadata.py` reads metadata from quote-backtest CSV row-0 (no Gamma round-trip needed — CSVs already carry question/condition_id/market_family/endDate); `run_experiment_matrix` and `run_walk_forward` accept `--family-filter {off,on,both}`, build `MarketFilter` from `BotConfig.from_env()`, classify family via the filter when CSVs lack the column, and emit per-family `by_family` block + per-run `brier_score`. **Smoke** on 10 quote-backtest CSVs × 9 experiments × 2 modes = 180 runs: filter classifies all 10 markets as `sports_outright` (allow-listed → 0 skipped → `filter_on` ≡ `filter_off` for this set, as expected). Brier sanity passes: `[0.5]×1000` against random outcomes → 0.25; sharply aligned 0.9/0.1 → 0.01. Per-CSV Brier ranges from 0.021 (sharp & accurate) to 0.5 (chance-level) on `loose_baseline`. **Implication**: family/keyword filter doesn't bind on this CSV set (all sports outrights), so its real test happens once we run on a mixed-family corpus or once `STRICT_STRATEGY_MODE=true` activates the strict-mode keyword list. Brier is now a first-class headline metric — Phase 2 honest-test gate is wired.
- 2026-04-25 — Phase 3 — **Primary edge falsified on this universe.** Built `deploy/scan_live_arb.py` and ran it against 5 days / 292,316 aligned 60-second buckets across all 50 binaries: after filtering 924 crossed-quote rows (0.08% data corruption from CLOB book races), **zero** Yes+No bundles cleared the 30 bps MVE. Distribution: bundle_ask ∈ [1.0000, 1.1410], bundle_bid ∈ [0.8590, 1.0000], median both 1.001/0.999 — Polymarket maintains tick-perfect $1 invariant on liquid binaries. The Gamma `--mode live` reference scan (500 markets) finds 0 non-balanced because Gamma normalizes outcomePrices to sum to 1 by construction (not a real arb signal). Cross-condition outright probe (sum of all NBA Finals YES asks): 0.962 median across 466 buckets — looks like 380 bps arb but is actually coverage artifact (we track 13 of 30 NBA teams; missing field would close the gap). Implications: (a) single-binary bundle arb is dead at our scale on liquid markets; (b) full-field outright arb is the only remaining structural angle and requires expanding the watchlist to all contenders per championship; (c) before committing more code to Phase 3 we need an honest re-read of edge ranking. **Next step decision pending**: either expand watchlist to full NBA + NHL + MLB outright fields and re-test, or accept that structural arb is gone for our scale and pivot edge ranking. Crossed-quote anomaly also worth filing separately — it's a CLOB book-race data-quality issue the live trader would need to handle.
- 2026-04-23 — Phase 1 — Coverage diagnostic at day 3 (`deploy/analyze_quote_coverage.py`, 830,500 rows / 100 tokens / 73.5h): 99.05% bid+ask coverage, 100% CLOB source, spread_bps median 63 / p75 465 / p95 1818 (heavy right tail), token staleness ratio median 0.995 (mids almost never move between 30s polls). Prune filter (p75 spread < 500 bps AND staleness < 0.95) keeps **12 of 100 tokens** — that's the Phase-2 trading universe candidate. Implications: (a) reinforces arb-first thesis — persistent mispricings, not random walks; (b) **kills directional signals** — nothing for MACD/RSI/CVD to work on when mids are frozen 99% of the time; (c) universe must be pruned hard before Phase 2. Next check-in: **2026-05-11** (Phase 2 open at ~3-week mark). Skipping a 2026-04-27 intermediate look — the structural story is already clear and another weekly re-read re-observes the same thing.
- 2026-04-20 — Plan refinement — Absorbed useful pieces of an "AI-as-probability-engine" framework: (1) added Brier-score/calibration check to Phase 2 (a stack that is often right on direction can still lose money with poorly calibrated probabilities); (2) expanded Phase 5 second-edge candidates to rank maker-quoting above LLM-probability-engine, with explicit scope + niche selection for the LLM track. Declined to restructure PLAN around LLM-as-primary because (a) competitors on news-driven markets are fast and informed, (b) we don't have news/evidence infrastructure, (c) it doesn't solve the liquidity/capacity ceiling. Arb-first stance unchanged.
- 2026-04-20 — Phase 1 — Cost model + paper log schema **shipped**. `BacktestEngine` now emits per-trade cost decomposition (spread / slippage / fee / post-fill-move / notional) and a run-level `cost_attribution` block (gross vs net PnL, USD cost buckets, adverse-fill ratio). `run_backtest` + `run_experiment_matrix` surface it. Paper log now carries `best_bid`/`best_ask`/`observed_spread_bps`/`schema_version=1` on every entry. Validated on `data/ceasefire_yes.csv`: 70 fills, $3.29 total costs split correctly, 82.8% adverse-fill ratio (flags this as a bad regime for the signal stack — exactly the Phase 2 insight we want).
- 2026-04-20 — Phase 0 — `.env` reconciliation: config loads clean, creds populated, not in git history. Open question on `SIGNATURE_TYPE=2` — user to confirm Polymarket account type.
- 2026-04-20 — Phase 1 — Quote collector **running**. LaunchAgent active under venv python `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13` (FDA granted). Collecting 100 tokens @ 30s interval → `data/quote_collection/run.jsonl`. Clock starts now on the ≥3-week accumulation window. Next review: ~2026-05-11.
- 2026-04-20 — Phase 1 — Quote collector plumbing ready. Shortlist built (`scan_markets --limit 200 --top 50` → 50 condition-ids, 47 A-tier). Smoke test: 200 rows, 100% CLOB order-book source, all bid/ask/mid/spread fields present. LaunchAgent plist at `deploy/com.polymarket.rbi-bot.quote-collector.plist`. Initial TCC issue (`~/Desktop` is sandbox-protected) resolved by switching plist off bash wrapper and granting FDA to the venv python binary directly (narrower blast radius than bash FDA).
- 2026-04-20 — Phase 0 — MVE locked: arb ≥ 30 bps / directional ≥ 100 bps per round-trip after costs. Risk capital set at $500.
- 2026-04-20 — Phase 0 — Plan drafted.
