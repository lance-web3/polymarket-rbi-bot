# Passive Fill Feasibility Summary (Probes #1 and #2)

## Goal

Assess whether tiny post-only orders can realistically fill in selected tighter Polymarket markets under simple passive execution rules.

## Probe #1

Market:
- San Antonio Spurs 2026 NBA Finals, Yes

Order:
- BUY 20 @ 0.116
- post-only GTC
- about $2.32 notional

Outcome:
- accepted by exchange
- rested on book
- 0 fill
- canceled cleanly

## Probe #2

Market:
- Tampa Bay Lightning 2026 NHL Stanley Cup, Yes

Order:
- BUY 20 @ 0.143
- post-only GTC
- about $2.86 notional
- one tick inside spread while still post-only

Outcome:
- accepted by exchange
- rested on book
- 0 fill
- canceled cleanly

## What we learned

Confirmed working:
- auth path
- live order submission
- live cancellation
- reconciliation/state refresh path

Not confirmed:
- meaningful passive fill probability
- realistic simple maker edge in these test conditions

## Interpretation

The first two live probes suggest:
- passive orders can rest correctly
- but simple tiny passive quoting in these markets may not fill often enough, even with slightly improved queue position

This weakens the short-term thesis that we can rely on easy maker-style fills to rescue strategy economics.

## Best next research question

Not:
- "Can we submit passive orders?"

That has been answered.

Instead:
- "Under what market/spread/activity conditions do passive orders actually fill?"

## Recommended next step

Pause additional live probes for now.

Do research on fill likelihood first, for example:
- compare fillability by spread bucket
- compare markets by activity / quote change frequency
- examine whether our quoted levels were too passive relative to actual queue dynamics
- identify narrower candidate subsets before spending more live attempts
