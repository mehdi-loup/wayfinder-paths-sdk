# Trailing Orders for Hyperliquid

## What this does

**Hyperliquid does not natively offer trailing stop-loss, trailing
take-profit, or trailing limit-entry orders.** This path fills that gap.
It adds trailing exits that follow the price as your trade moves in your
favor, so you lock in more gains (or cap adverse entries) without having
to babysit the chart.

**Example.** You buy HYPE at $30 and set a 5% trailing stop. If HYPE climbs
to $40, your stop automatically moves up to $38 (5% below the peak). If HYPE
then drops, the trade closes at $38 — locking in an $8 gain instead of
getting stopped out at $28.50 like a fixed stop would do.

> ⚠️ **This is a helper, not a live exchange-native trailing order.**
> The background checker runs on a schedule (default: every 5 minutes). It
> will **miss price action between run windows**, so sharp wicks or fast
> moves can blow past your intended trigger before the checker gets a turn.
> At higher leverage this materially raises your chance of liquidation.
> Use Safer mode (see below) whenever you can — it parks a live stop order
> on Hyperliquid itself so the exchange fills it even between ticks. If you
> need millisecond-accurate trailing, this path is not a substitute for a
> native exchange feature (which Hyperliquid doesn't currently expose).

## How to install

In Claude Code, just say:

> "Install the Hyperliquid trailing orders path."

Claude will pull the path, wire up the skill, and register the background
checker automatically. No files to edit; no settings to paste.

## How to use it

Next time you ask Claude to place a Hyperliquid trade, it'll offer to add a
trailing stop, take-profit, or limit-entry exit. **When** it asks depends
on the order type:

- **Limit orders (and trailing entries):** Claude asks **before** placing
  the order. A limit order sits on the book anyway, so there's no rush.
- **Market orders:** Claude places the market order **first**, then asks
  **after** the fill whether you want to attach a trailing stop, TP, or
  limit exit. This keeps the entry fast and avoids slippage from extra
  confirmation round-trips.

Say yes, pick a percentage, and you're done. A background checker keeps an
eye on the price every few minutes and acts for you.

You can cancel at any time with:

> "Stop the Hyperliquid trailing checker."

## Two safety modes

- **Safer (recommended).** A live stop order sits on Hyperliquid at the
  current trailing price. Even if your computer is off, Hyperliquid itself
  will fire the stop. The background checker just moves that stop up as the
  price rises.
- **Lighter.** No live stop order on Hyperliquid. The background checker
  watches the price and closes the trade when the trailing threshold is
  hit. This uses less exchange bandwidth but only works while your checker
  is running.

Claude asks which mode you want. If you don't know, stick with Safer.

## What gets installed

- A skill that tells Claude how to offer trailing orders before any
  Hyperliquid trade. Claude loads it automatically whenever you're placing
  or talking about an HL trade.
- A small background checker that the Wayfinder runner invokes every 5
  minutes to trail your open stops.
- An applet — a demo page that pulls real recent price data from Delta Lab
  and lets you move a slider to see how a trailing stop would have handled
  the last day, week, or month for BTC, ETH, SOL, or HYPE versus a fixed
  stop.

## Order types supported

All three kinds share one idea: a background checker tracks an *extreme*
price (the "peak" for longs, the "trough" for shorts) and fires when the
market reverses off that extreme by a percentage you set.

- **Trailing stop-loss.** The safety net. Armed immediately when you
  attach it. The peak follows the *favorable* direction (up for longs,
  down for shorts). If price pulls back from the peak by your
  `sl_pct`, the position closes at market.

  *Example.* HYPE long at $40, SL `sl_pct=5`. Peak climbs to $50,
  trigger ratchets to $47.50. If HYPE dips to $47.50, you exit there
  instead of ~$38 like a fixed stop would have done.

- **Trailing take-profit.** The tighter profit-lock. *Dormant* while the
  trade is flat or losing — the exchange sees no order yet. Once the
  trade is ahead by `activation_pct`, it arms and then behaves
  **identically** to a trailing stop-loss with `tp_pct`. The
  "take-profit" part is only the activation gate; once activated, it is
  a stop.

  *Example.* HYPE long at $40, TP `tp_pct=1`, `activation_pct=5`.
  Nothing on the exchange until HYPE reaches $42. Peak then climbs to
  $50, trigger ratchets to $49.50. If HYPE dips to $49.50, you exit and
  lock the gain. A looser SL alongside catches anything that moves
  before the TP ever arms.

  **Keep `tp_pct` smaller than `sl_pct`.** Once the TP activates, both
  legs trail the same peak. If their offsets match, they sit at the
  same trigger price and OCO is moot. A tighter TP fires first on a
  small pullback (locking profit); the looser SL remains as backup for
  the pre-activation phase.

- **Trailing entry.** You don't have a position yet. The peak tracks
  the *adverse* extreme (e.g. the lowest dip for a long). Once price
  reverses by `entry_pct` off that extreme, a market order fires to
  open the trade. Mirror of that for shorts.

You can attach a trailing stop **and** a trailing take-profit on the
same trade. Whichever one fires first automatically cancels the other
(OCO).

### Defaults the skill will use if you don't specify

| Parameter | Default | Why |
|---|---|---|
| `sl_pct` | 5% | Wide enough to absorb normal chop, tight enough to protect capital. |
| `tp_pct` | 1% | Tight pullback to lock profit once the trade is already up. |
| `activation_pct` | 5% | TP only starts trailing once you're meaningfully in the money. |
| `mode` | resting | Live stop sits on Hyperliquid — fires even if the checker is down. |
| `cadence_s` | 300 | Checker tick; drop this if you trade volatile coins with leverage. |

## What it won't do

- It will not open a brand new trade for you. You start the trade the usual
  way; the trailing logic attaches on top.
- It will not move money between wallets or exchanges. Everything happens
  on your main Hyperliquid account.
- It will not work on exchanges other than Hyperliquid in this version.

## Checking on it later

- "Show me active Hyperliquid trailing orders." — Claude reads the list.
- "Cancel the trailing stop on HYPE." — Claude removes it.
- "Pause the background checker." — Claude pauses the runner job.

## Running the demo applet locally

When the applet runs inside the Wayfinder Strategies host, it fetches live
price data from Delta Lab for BTC, ETH, SOL, and HYPE, and re-runs the
comparison whenever you change the token, window, direction, or
percentage. The math matches the live controller, so the results show how
the real thing would behave in a similar market.

Opening `applet/dist/index.html` directly from your file system works
too, but Delta Lab is cross-origin from a `file://` page — so the applet
falls back to a clearly-labelled demo price series. To see it with real
data, serve it behind the Strategies host or a same-origin proxy.

## Advanced: dedicated strategy wallet

By default the background checker runs on your main wallet — the simplest
setup. If you want a walled-off copy of capital for this, ask Claude to
"create a dedicated strategy wallet for trailing orders" and follow the
prompts. The checker will move to that wallet.

## File layout (for the curious)

```
examples/paths/trailing-hl-orders/
├── controller.py      # decides when to move, fire, or cancel (no exchange calls)
├── monitor.py         # runs every few minutes; talks to Hyperliquid
├── attach.py          # hooks a trailing config onto a fresh trade
├── state.py           # atomic JSON storage (survives session restarts)
├── skill/             # what Claude reads + pre-trade nudge
├── applet/            # the static backtest demo page
└── wfpath.yaml        # path manifest
```
