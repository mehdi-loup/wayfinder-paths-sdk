# Echelon LP Program

Manages PRIME/WETH Uniswap V3 1% liquidity positions for the Echelon Prime LP Rewards Program. Opens qualifying positions, monitors their in-range status and token ratio, and collects accumulated swap fees.

---

## How the Echelon LP program works

The Echelon program rewards wallets that provide liquidity for PRIME/ETH on Uniswap V3, but only to positions that continuously meet three hard requirements:

1. **Correct pair and pool** — PRIME/WETH on Ethereum, 1% fee tier (pool fee = 10000)
2. **Valid range width** — the combined span of your position must be between **20% and 80%**:
   - Lower bound pct + upper bound pct must sum to 20–80
   - Example: a range of -22% to +28% has a combined width of 50% ✓
3. **Qualifying ratio** — your position must hold **40%–60% PRIME** (by USD value) at any given moment to earn points. 50:50 is optimal and earns the maximum modifier (200). Positions outside 40/60 earn a reduced modifier.

Out-of-range positions (price has moved outside your tick boundaries) earn **zero points** until the position is back in range. The dashboard shows a position modifier between ~20 and 200, where 200 is the maximum.

**Points** accumulate continuously while your position is in-range and ratio-qualifying.

---

## What the agent does autonomously

The following happen **without confirmation** each cycle:

- Read on-chain PRIME/WETH 1% positions via the Uniswap V3 Non-Fungible Position Manager
- Fetch live PRIME and ETH prices from Delta Lab
- Compute the current ratio (PRIME % of position value) and in-range status
- Report compliance against all three Echelon requirements

The following **require explicit user confirmation** (or the `--dry-run` flag to preview without executing):

- **Opening a new LP position** (`--mode setup`) — this transfers PRIME and WETH into the pool
- **Collecting fees** (`--mode collect`) — this sends accrued swap fees to your wallet
- Any change to position size, range, or parameters

---

## Capital and wallet requirements

| Requirement | Detail |
|---|---|
| PRIME on Ethereum | The amount you want to deposit to the LP |
| WETH on Ethereum | Wrapped ETH — NOT native ETH — required by Uniswap V3 |
| ETH for gas | ~0.005–0.02 ETH for mint + approvals; ~0.002 ETH for collect |
| Minimum viable deposit | ~$200+ per side for meaningful points accrual |

**WETH note:** Uniswap V3 requires WETH, not native ETH. If you only have ETH, you must wrap it first (use the WETH contract `deposit()` or swap ETH → WETH on Uniswap before running setup).

---

## Range design

For a position to qualify under Echelon rules, the combined range must be 20–80%. Here is how to think about range width:

| Combined width | Lower / Upper split | Rebalance frequency |
|---|---|---|
| 20% (narrow) | -10% / +10% | Very frequent — price easily escapes |
| 50% (balanced) | -25% / +25% | Moderate — reasonable for PRIME volatility |
| 80% (wide) | -40% / +40% | Infrequent — but lower fee yield per unit of capital |

**Recommendation:** Start with -25% / +25% (50% combined). PRIME's typical daily volatility is ~5–10%, so a ±25% range will stay in-range for days to weeks under normal conditions.

A symmetric range (equal lower and upper pct) produces a ~50:50 ratio at mint and keeps the modifier near 200 as long as the price stays near the center.

---

## Ratio and position modifier

The Echelon modifier measures how close your position ratio is to the ideal 50:50:

| Position ratio (PRIME %) | Modifier | Qualifying? |
|---|---|---|
| 50% | 200 (maximum) | Yes |
| 45–55% | ~160–200 | Yes (Optimal) |
| 40–60% | ~80–160 | Yes (Qualifying) |
| < 40% or > 60% | ~20–80 | No — modifier too low |
| Out of range | 0 | No |

The ratio drifts as PRIME price moves relative to ETH. A price increase makes the position more WETH-heavy (lower PRIME %). A price decrease makes it more PRIME-heavy. If the ratio drifts outside 40/60, consider closing and reopening at the current price center.

---

## Risk factors

**Re-read this before depositing.**

**Impermanent loss** — LP positions lose value relative to simply holding when the price moves significantly in one direction. This is the fundamental tradeoff of LP. At a ±25% range, a one-sided move to the boundary results in roughly 5–8% IL versus holding.

**Out-of-range risk** — PRIME is a volatile asset. Sudden price moves can push your position fully out of range in minutes. An out-of-range position earns zero Echelon points and zero swap fees, but still holds the tokens.

**Fee income** — The 1% fee tier earns 1% of every swap through your price range, proportional to your share of liquidity at that tick. PRIME pool volumes vary significantly day-to-day.

**Gas costs** — Opening a position requires 2 ERC20 approvals + a mint transaction on Ethereum. At $10–20 gas per tx, a very small position may not recoup gas costs quickly.

**PRIME price risk** — This strategy holds PRIME. If PRIME price drops, the USD value of your position drops regardless of points earned.

**Smart contract risk** — Uniswap V3 is battle-tested. Standard DeFi risks apply.

---

## Dashboard and points tracking

Check your position's current points, modifier, and ratio at:

```
primelpdashboard.xyz/?address=<your_wallet>
```

The dashboard shows:
- **Position ID** — Uniswap V3 NFT token ID (appears a few hours after mint)
- **Current position ratio** — your PRIME:ETH split
- **Position modifier** — ~20–200, where 200 is optimal
- **TVL** — total value locked in the position
- **Status** — in-range or out-of-range

---

## Running the strategy

```bash
# Check current positions and compliance status
wayfinder path exec --path . --component main -- --mode check

# Preview a new position (no transaction)
wayfinder path exec --path . --component main -- --mode setup --dry-run

# Open a new position
wayfinder path exec --path . --component main -- --mode setup

# Collect accumulated swap fees
wayfinder path exec --path . --component main -- --mode collect

# Monitor continuously (every hour by default)
wayfinder path exec --path . --component main -- --mode monitor

# Auto-rebalance: exit out-of-range positions and re-enter daily (with stability check)
wayfinder path exec --path . --component main -- --mode auto

# Preview auto-rebalance actions without executing
wayfinder path exec --path . --component main -- --mode auto --dry-run
```

Or via the agent: *"check my Echelon LP position"*, *"set up an Echelon LP position"*, *"collect my Uniswap fees"*, *"start auto-rebalancing my LP"*.

---

## Configuration (inputs/config.yaml)

```yaml
wallet:
  label: main              # Wallet label in config.json (needs PRIME + WETH on Ethereum)

position:
  lower_pct: 25            # % below current price for lower bound
  upper_pct: 25            # % above current price for upper bound
  prime_amount: 100        # Max PRIME to deposit
  weth_amount: 0.05        # Max WETH to deposit
  slippage_bps: 300        # 3% slippage (recommended for 1% exotic pool)

echelon:
  owner_wallet: ""         # Your wallet address for dashboard link in check output

monitoring:
  check_interval_seconds: 3600  # 1-hour polling in loop mode (out-of-range is a slow event)

rebalance:
  enabled: true
  momentum_threshold_pct: 4.0   # Defer re-entry if 4h PRIME momentum exceeds this %
  volatility_threshold_pct: 3.0 # Defer re-entry if 12h hourly stddev exceeds this %
  swap_slippage_bps: 150        # Slippage tolerance for rebalance swap (150 = 1.5%)
```

**Lower and upper pct** must sum to between 20 and 80. The script will reject a configuration outside this range before attempting any transaction.

---

## Auto-rebalance mode

`--mode auto` runs a daily rebalance loop that exits out-of-range positions, rebalances the token ratio, and re-enters — all without manual intervention.

**What it does each cycle (every 24 hours):**

1. Reads all PRIME/WETH 1% positions via `check`
2. For any position that is **out of range**:
   - Removes liquidity and collects fees in one transaction
   - Runs a **price stability check** on the last 12 hours of PRIME price data:
     - If 4h momentum > `momentum_threshold_pct` OR 12h hourly-return stddev > `volatility_threshold_pct`, the re-entry is **deferred** to the next daily cycle
   - If price is stable, computes the ideal PRIME/WETH split for a centered new position
   - Swaps any excess token via SwapRouter02 (PRIME/WETH 1% pool) to reach the target ratio
   - Opens a new position centered on the current price using your configured range pct

**Dry-run support:**

```bash
wayfinder path exec --path . --component main -- --mode auto --dry-run
```

Dry-run logs every action but executes no transactions and submits no swaps.

**Rebalance configuration (`rebalance` block in `inputs/config.yaml`):**

```yaml
rebalance:
  enabled: true
  momentum_threshold_pct: 4.0   # Defer if 4h PRIME price move exceeds this %
  volatility_threshold_pct: 3.0 # Defer if 12h hourly stddev exceeds this %
  swap_slippage_bps: 150        # 1.5% slippage on rebalance swap
```

Set `enabled: false` to disable auto-rebalance entirely (the mode will exit immediately).

**Stability thresholds — guidance:**

| Market condition | Suggested thresholds |
|---|---|
| Calm / sideways | momentum 4%, vol 3% (default) |
| Moderately volatile | momentum 6%, vol 5% |
| Very volatile / use wider range | momentum 10%, vol 8% |

Tighter thresholds mean the strategy will defer re-entry more often during turbulent markets. This reduces the chance of re-entering just before another sharp move, at the cost of spending more time on the sidelines.

---

## When to rebalance manually

Consider closing and reopening your position manually when:

1. **Out of range** — position is earning zero points; price must come back or you must rebalance
2. **Ratio outside 40/60** — modifier drops to disqualifying levels; means price has drifted far from center
3. **Long out-of-range period** — if the position has been out of range for days, the price center has likely shifted

Use `--mode auto` to handle this automatically, or run `--mode setup` to open a new centered position after closing the old one manually.

---

## Republishing this path

```bash
wayfinder path fmt --path examples/paths/echelon-prime-lp-strategy
wayfinder path doctor --check --path examples/paths/echelon-prime-lp-strategy
wayfinder path publish --path examples/paths/echelon-prime-lp-strategy --owner-wallet 0x07e8618d1e67ef6efcc9730b54a347ec825ce9a1
```
