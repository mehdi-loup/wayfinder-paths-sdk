---
name: using-derive-adapter
description: How to use the Derive adapter in Wayfinder Paths for options discovery, ticker quotes, authenticated account reads, margin checks, and signed order/cancel workflows.
metadata:
  tags: wayfinder, derive, options, perps, spot, orderbook, quotes, margin, positions, signed-orders
---

## When to use

Use this skill when you are:

- Discovering Derive options by currency, expiry, strike, or option type.
- Reading Derive best bid/ask, mark, index, option greeks, or open interest from ticker data.
- Reading Derive subaccounts, positions, open orders, or margin state.
- Preparing or validating a signed Derive order payload.
- Cancelling an existing Derive order.

## How to use

- `wayfinder_paths/adapters/derive_adapter/README.md` - Adapter overview, auth, examples, and source docs.
- [rules/high-value-reads.md](rules/high-value-reads.md) - Market discovery, quotes, and account reads.
- [rules/execution.md](rules/execution.md) - Signed order debug/submit and cancel workflows.
- [rules/gotchas.md](rules/gotchas.md) - Derive wallet auth, signing boundaries, orderbook/WebSocket scope, and simulation limits.
