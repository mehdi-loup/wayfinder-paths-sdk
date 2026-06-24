# PRIME Cross-Chain Arbitrage

Cross-chain arbitrage strategy for the $PRIME token. Monitors the price spread between
Ethereum (Uniswap V3 0.3% pool) and Base (Aerodrome CL200 pool). When the spread exceeds
all costs, it simultaneously buys on the cheaper chain and sells on the more expensive chain
using pre-positioned capital floats.

---

## How it works

PRIME trades on two chains simultaneously. Small price differences open and close continuously
as traders and other arb bots act. This strategy holds idle capital on **both chains at all
times** — no bridging happens during execution. When a spread opportunity is detected:

1. **Read** — fetch live prices from both pools via on-chain `slot0` calls
2. **Check** — compare gross spread against the break-even threshold (fees + gas + bridge amort)
3. **Execute** — if spread > threshold: buy PRIME on the cheaper chain, sell on the expensive chain, simultaneously
4. **Wait** — do nothing until the next check interval; do not chase spreads that close before execution

Capital is rebalanced between chains periodically via bridge (BRAP cross-chain swap) when
the float on one side runs low. Rebalancing cost is amortised across trades in the break-even model.

---

## Capital requirements

| Item | Minimum | Recommended |
|---|---|---|
| PRIME float (Ethereum) | $500 | $2,000+ |
| USDC float (Ethereum) | $500 | $2,000+ |
| PRIME float (Base) | $500 | $2,000+ |
| USDC float (Base) | $500 | $2,000+ |
| ETH for gas (Ethereum) | 0.005 ETH | 0.02 ETH |
| ETH for gas (Base) | 0.001 ETH | 0.005 ETH |
| **Total minimum** | **~$2,000 + gas** | **~$8,000 + gas** |

The strategy is **not viable below ~$1,000 per trade side**. At $500 trade size the break-even
spread is ~2.8% — a rare event. At $1,000 it drops to ~1.8%. At $5,000+ it approaches 1.0%.

---

## What the agent does autonomously

The following actions happen **without confirmation** each cycle:

- Read on-chain prices from Uniswap V3 and Aerodrome pools
- Compare spread against the configured threshold
- **Execute swap on Ethereum** (buy or sell PRIME on Uniswap V3) when threshold is met
- **Execute swap on Base** (buy or sell PRIME on Aerodrome) when threshold is met
- Record each trade to the session log

The following actions **require explicit user confirmation**:

- Initial capital deposit to either chain
- Bridge / float rebalancing between chains
- Any change to trade size, threshold, or other parameters
- Withdrawal of funds back to the main wallet

---

## Cost model

Each round-trip trade incurs:

| Cost | Amount | Notes |
|---|---|---|
| Uniswap V3 swap fee | 0.30% | Fixed, taken by the pool |
| Aerodrome CL200 swap fee | 0.30% | Fixed, taken by the pool |
| Slippage (both sides) | ~0.10% each | Estimate; widens in low-liquidity conditions |
| Ethereum gas | ~$5–15 | Highly variable; $8 used as baseline |
| Base gas | ~$0.05 | Near-negligible |
| Bridge amortisation | ~$2/trade | Periodic float rebalancing cost spread across trades |

**Break-even gross spread by trade size:**

| Trade size | Break-even spread |
|---|---|
| $500 | ~2.8% |
| $1,000 | ~1.8% |
| $5,000 | ~1.0% |
| $10,000 | ~0.9% |

---

## Performance context

> These are model-derived estimates, not backtested results from real historical spread data.
> Treat them as directional guidance, not guarantees.

Based on PRIME's 90-day hourly volatility (~1.6%/hr) and a calibrated spread factor (k≈0.78),
the model estimates the spread distribution as approximately normal with σ≈1.2%.

| Trade size | Spread > break-even | Estimated trades/yr | Annualised return est. |
|---|---|---|---|
| $1,000 | ~14% of hours | ~1,200 | ~300% on $2k float |
| $5,000 | ~41% of hours | ~3,600 | ~1,200% on $10k float |

These numbers assume:
- Every opportunity is successfully executed (no failed transactions, no front-running)
- The spread distribution is stationary (same vol regime as the 90-day lookback)
- ETH gas averages $8 per swap

**Returns compress significantly if:**
- ETH gas spikes above $15 (break-even moves up, fewer opportunities clear it)
- PRIME volatility drops materially (fewer and smaller spreads)
- A competing arb bot closes spreads faster than the check interval

---

## Risk factors

### This strategy is NOT suitable if you:

- Cannot afford to have capital locked on both chains simultaneously
- Need liquidity from your PRIME or USDC on short notice
- Are uncomfortable with fully autonomous swap execution (the agent will trade without asking)
- Expect guaranteed returns — spread opportunities are probabilistic and may be scarce in low-vol regimes
- Are in a jurisdiction where automated DeFi trading raises regulatory concerns

### Risks to understand before entering:

**Execution risk** — Spreads are typically open for minutes or less. By the time the agent
detects and executes, the spread may have partially or fully closed, leaving a directional
position instead of a flat arb. The agent does not try to unwind these; they resolve naturally.

**ETH gas volatility** — A spike to $20+ gas completely changes the economics at small trade
sizes. The strategy has a configurable `min_net_spread_pct` parameter that should be raised
when gas is elevated. The agent checks gas cost before each trade and will skip if it exceeds
the configured `gas_limit_eth_usd` ceiling.

**Liquidity risk** — PRIME pools on both chains are moderately liquid but not deep. Large
trade sizes (>$10k) will experience meaningful slippage beyond the modelled 10bps estimate.
Do not size above ~1% of pool TVL per trade.

**Bridge / rebalancing risk** — If one side of the float is depleted faster than the other
(one chain consistently cheaper), the strategy will need periodic rebalancing. BRAP bridge
swaps carry additional slippage and latency. Rebalancing is user-confirmed but delays can
temporarily pause execution.

**Smart contract risk** — Uniswap V3 and Aerodrome are audited and battle-tested. Standard
DeFi risks apply: bugs, oracle manipulation, and pool governance changes are all non-zero.

**PRIME price risk** — The strategy is delta-neutral on a per-trade basis (buy one chain,
sell the other) but not between trades. If PRIME moves sharply between cycles, the float
on one chain will have gained or lost value. This is not a hedge against holding PRIME.

---

## Configuration (inputs/config.yaml)

```yaml
# Pool and token addresses — only change if pool addresses migrate
tokens:
  prime_eth: "0xb23d80f5FefcDDaa212212F028021B41DEd428CF"
  prime_base: "0xfA980cEd6895AC314E7dE34Ef1bFAE90a5AdD21b"
  weth_eth: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
  weth_base: "0x4200000000000000000000000000000000000006"

uniswap_v3:
  pool_address: "0x16588709ca8f7B84829B43cC1c5cb7e84a321b16"  # PRIME/WETH 0.30%

aerodrome:
  pool_address: "0x87cd18069b6547a0e88b6155dd657e71779500ea"  # CL200-WETH/PRIME

execution:
  trade_size_usd: 1000         # Notional per trade per side
  min_gross_spread_pct: 1.9    # Must exceed break-even; set conservatively
  check_interval_seconds: 60   # Price poll frequency
  max_trade_size_pct_tvl: 0.01 # Cap at 1% of pool TVL to limit slippage
  gas_limit_eth_usd: 12.0      # Skip trade if ETH gas estimate exceeds this

rebalancing:
  threshold_pct: 20            # Rebalance when either side float < 20% of target
  min_bridge_usd: 200          # Minimum bridge amount (below this, wait longer)
```

`min_gross_spread_pct` is the most important parameter. Set it **above your actual break-even
by at least 0.3–0.5%** to account for gas variability and execution lag. A value too close to
break-even will generate many marginal trades that net to a loss after gas.

---

## Running and monitoring

```bash
# Check current spread without trading
wayfinder path exec --path . --component main -- --mode check

# Run one cycle (will execute if spread threshold is met)
wayfinder path exec --path . --component main -- --mode once

# Run continuously (production mode)
wayfinder path exec --path . --component main -- --mode loop
```

Or via the agent, simply ask: *"check the PRIME arb spread"* or *"run one arb cycle"*.

---

## Related paths

- **[prime-daily-intel](https://strategies-dev.wayfinder.ai/paths/prime-daily-intel)** —
  Monitor-only companion. Run this first to observe real spread behaviour before committing
  capital. Uses the same on-chain price methodology as this strategy.

## If you need to republish this path

1. `wayfinder path fmt --path examples/paths/prime-arb-strategy`
2. `wayfinder path doctor --check --path examples/paths/prime-arb-strategy`
3. `wayfinder path publish --path examples/paths/prime-arb-strategy --owner-wallet 0x07e8618d1e67ef6efcc9730b54a347ec825ce9a1`
