# Risk Validation

The skeptic agent runs four quantitative checks. A spread must pass all four to be accepted.

## 1. Hidden beta

Regress strategy OOS returns against the equal-weight universe return. R-squared > 0.50 means the strategy is a disguised directional bet. The spread radar's purpose is relative-value — directional beta is not an edge.

## 2. Fee sensitivity

Run the backtest at 0, 4.5, 15, and 30 bps fee rates. If the OOS Sharpe drops by more than 50% between 0bps and 15bps, the edge is too thin for real execution. Most Hyperliquid perps have ~3.5bps taker fees + ~1bp slippage.

## 3. Parameter robustness

Perturb the winning lookback and entry_z by ±20% and re-run. If any neighbor's OOS Sharpe drops below 1.0, the config sits on a narrow peak that won't generalize. The validated combined strategy had 64% of 243 configs achieving OOS Sharpe > 3 — that's the standard.

## 4. Concentration

Decompose OOS P&L by pair. If a single pair contributes >70% of total OOS return, the strategy is a single-pair bet disguised as a basket. This is a warning, not an automatic rejection, but it must be disclosed.

## Null state

Always compare against doing nothing. If the best strategy's OOS Sharpe is below 1.0, the null state (hold cash) is preferred. Never force a trade to avoid returning null.

## Walk-forward protocol

All evaluation uses walk-forward: run the signal on the full sample continuously, measure metrics on the train and test halves separately. Do NOT restart the signal fresh on the test period — that inflates results by giving the signal a warm-up advantage.
