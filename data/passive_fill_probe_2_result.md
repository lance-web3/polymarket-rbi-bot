# Passive Fill Probe #2 Result

## Summary

Probe intent:
- execution test only
- not strategy validation
- single controlled change from probe #1: slightly better queue priority while remaining post-only

Result:
- **order posted successfully**
- **order rested on book**
- **no fill within probe window**
- **order canceled cleanly**
- **no funds were committed**

## Market

Question:
- Will the Tampa Bay Lightning win the 2026 NHL Stanley Cup?

Condition ID:
- `0xbdd688664b4f3cf7ec4ec011607934fe8ae720c08353fc14a6e9dfbf6bbcf11a`

Token:
- Yes
- `35573117698117780238142713946749692621043319879346349609080985768472429209643`

## Order details

Order ID:
- `0xf7566a138f9e0db940011dc1e6a46c0315ea865e88be98a51a5a2ba2a0bbdcf0`

Side:
- BUY

Order type:
- post-only GTC

Posted price:
- `0.143`

Posted size:
- `20`

Posted notional:
- `$2.86`

## Lifecycle

- exchange accepted the post-only order
- order status became live on book
- matched size remained `0 / 20`
- after the intended probe window, order was manually canceled
- final state: **CANCELED**

## Probe takeaway

Positive:
- post-only order path worked again on a second market
- this tested the intended queue-priority variation, one tick inside spread while still passive

Negative / important:
- even the more aggressive passive queue position did **not** fill within the observation window
- this weakens the thesis that simple passive quoting at tiny size is easily fillable on these candidate markets

## Interpretation

Combined with probe #1:
- passive orders can be posted and canceled reliably
- passive fill likelihood remains weak in these simple test conditions
- execution feasibility is still not strong enough to support a maker-edge thesis by itself

## Recommended next step

Pause further live probing for now and move back to research focused on fill likelihood:
- which markets actually fill passive orders?
- what spread / activity conditions matter?
- whether passive quoting is only viable in narrower subsets than these first two tests
