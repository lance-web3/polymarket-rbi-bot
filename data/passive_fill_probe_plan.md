# Passive Fill Probe Plan

## Goal

Test **execution feasibility**, not strategy edge.

Primary question:
- Can post-only / passive orders get filled often enough, and cleanly enough, to make maker-style execution plausible on selected Polymarket markets?

This probe is **not** intended to prove profitability.

## Scope

Use tiny size only:
- target notional per probe: **$1 to $10 max**
- one market pair at a time
- one resting order at a time
- no pyramiding
- no overlapping experiments

## Preconditions

Only run if all are true:
- exchange credentials and reconciliation are working cleanly
- decision timestamps are fresh
- selected market spread is relatively tight for this venue
- selected market is liquid enough that resting orders are plausible
- we are explicitly in "execution experiment" mode, not "live strategy" mode

## Market Selection Rules

Prefer markets where:
- quoted spread is among the tighter observed names
- order book appears active / updating
- price is not at an extreme tail if avoidable
- market is not near resolution chaos

Avoid:
- very wide spread names
- stale books
- markets with abrupt jump behavior
- thin / inactive markets where fill inference is meaningless

## Probe Design

### Phase 1: Single passive entry test

Place one **post-only buy** resting near or at best bid.

Track:
- order placed timestamp
- quoted bid/ask at placement
- order price vs best bid
- whether order posts successfully
- whether it fills at all
- time to first fill
- fill size ratio
- whether market moves away immediately after posting
- whether fill occurs just before adverse move

Cancel if not filled within a short window.
Suggested first timeout:
- **5 to 15 minutes max** depending on market activity

### Phase 2: Passive exit test

If entry fills:
- place one **post-only sell** resting near or at best ask
- track the same metrics
- use a bounded timeout
- if exit does not fill, close carefully with explicit manual review or a predefined safe fallback

### Phase 3: Repeat only a few times

Target:
- **3 to 5 total probes**, not dozens

The purpose is to estimate:
- passive entry fill probability
- passive exit fill probability
- typical resting time
- slippage avoided vs taker execution
- whether fills are mostly good fills or toxic fills

## Metrics to Record

For each probe:
- market / token id
- side
- posted price
- best bid / ask when posted
- spread bps when posted
- fill status: none / partial / full
- time to fill seconds
- posted then canceled? yes/no
- markout after fill:
  - 1 minute
  - 5 minutes
  - 15 minutes
- exit behavior
- final realized pnl if round-trip completed

## Success Criteria

A probe is encouraging only if most of these are true:
- passive entry fills happen with reasonable frequency
- passive exits also fill often enough
- average wait time is acceptable
- post-only orders do not constantly miss and chase price
- fills are not systematically followed by adverse markout
- execution improvement vs taker is material, not theoretical

## Failure Criteria

Stop immediately if any of these happen:
- reconciliation breaks
- stale decision / stale quote issues reappear
- post-only orders rarely fill
- fills only happen when price moves against us
- exits get stranded
- operational complexity is too high for tiny improvement

## Hard Safety Rules

- Max one live probe position open at a time
- Max one market under test at a time
- Max total live risk capped to tiny amount only
- No averaging down
- No auto-retry loops
- No hidden order churn
- Manual review before changing probe parameters materially

## Recommended First Live Experiment

1. Pick one relatively tighter-spread market pair
2. Place one tiny passive entry
3. Wait bounded time
4. If filled, attempt one passive exit
5. Log everything
6. Review before doing probe #2

## What We Learn

This experiment should answer:
- Is maker-style execution actually attainable here?
- Is the apparent maker edge from research realistic or fantasy?
- Are we getting benign fills or toxic fills?

## Decision After Probe

After 3 to 5 probes, decide one of:
- maker execution looks viable, continue research
- maker execution is unreliable, stop this line
- strategy may need different markets or different structure
