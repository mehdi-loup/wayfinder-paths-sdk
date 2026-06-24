# Topic scope

A topic is accepted only if it names a **specific signal family**. Before accepting, ask: "could a reader predict the signal formula class from this topic name alone?"

## Accept

- "cross-sectional momentum with 6-12 month formation period"
- "volatility-managed portfolio scaling"
- "realized-variance regime classification for daily equity returns"
- "funding rate mean reversion in cryptocurrency perpetuals"
- "time-series momentum with vol targeting"
- "option-implied skew as a return predictor"

## Reject

- "momentum" — ambiguous, too broad
- "regime detection" — too broad
- "crypto trading signals" — not a family
- "market inefficiencies" — phenomenon, not signal
- "behavioral finance" — domain, not signal
- "high-frequency trading" — execution style, not signal

## Rule

If the topic could produce papers spanning >3 different signal formulas, it is too broad for the **Deep** pipeline. In **Deep mode**, reject and ask the user to narrow, offering 2-3 specific alternatives. In **Scout mode** (`references/scouting.md`), do NOT reject — a broad ask is the expected input; decompose it into a spice-ranked menu of narrow families (each of which must individually pass the Accept test above) and let the user pick interactively.

## Example of the right response to a broad topic

> User: "find papers on momentum signals"
> Assistant: "Too broad — this would pull 100+ papers across cross-sectional, time-series, factor-residual, and intraday momentum. Narrow to one of: (a) cross-sectional momentum with formation + holding windows specified, (b) time-series momentum with vol-targeting, or (c) residual momentum after factor orthogonalization. Which one?"
