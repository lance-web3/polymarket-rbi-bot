# Passive Fill Probe #1 Result

## Summary

Probe intent:
- execution test only
- not strategy validation

Result:
- **order posted successfully**
- **order rested on book**
- **no fill within probe window**
- **order canceled cleanly**
- **no funds were committed**

## Market

Question:
- Will the San Antonio Spurs win the 2026 NBA Finals?

Condition ID:
- `0xb6b3d7a2037b3faa7e1306d741840d453432902d73cc9a146a035e40271eae73`

Token:
- Yes
- `102227184035967850089766981958743064457339118173548431660886438726896222843254`

## Order details

Order ID:
- `0xf8be1b40a53f41ed9ead053bbf67c6af29b197764c1530d3a1437abae434a2ee`

Side:
- BUY

Order type:
- post-only GTC

Posted price:
- `0.116`

Posted size:
- `20`

Posted notional:
- `$2.32`

## Lifecycle

- exchange accepted the post-only order
- order status became live on book
- matched size remained `0 / 20`
- after the intended probe window, order was manually canceled
- final state: **CANCELED**

## Probe takeaway

Positive:
- authenticated trading path is now working
- a live post-only order can be submitted successfully from this setup
- the venue accepted the order and left it resting on the book
- cancellation path worked cleanly

Negative / important:
- this specific tiny passive order did **not** fill within the observation window
- so maker-style feasibility is still unproven
- at least for this price/size/time window, passive fill probability looked weak

## Interpretation

This probe answered infrastructure questions better than edge questions:
- auth path: working
- order submission: working
- cancellation: working
- passive fill likelihood: still uncertain, currently weak from this single observation

## Recommended next step

If continuing live probes:
- do not increase size
- vary only one thing at a time
- test one more passive probe in a similarly tight market or slightly different queue position
- keep bounded timeout and explicit cancellation rule
