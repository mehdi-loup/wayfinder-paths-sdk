# Derive Gotchas

## Derive Wallet Is Not Always The EOA

Derive private REST auth uses `X-LyraWallet`, which the docs describe as the Derive wallet/account address, not necessarily the original owner EOA. Session keys can sign private requests, but the header wallet should still identify the Derive account.

When in doubt, pass both:

```python
adapter = await get_adapter(
    DeriveAdapter,
    "session-key-label",
    derive_wallet_address="0xDeriveAccountWallet",
)
```

## Read-Only Session Keys Cannot Trade

Derive session-key scopes matter:

- `read_only`: account info, orders, positions, history.
- `account`: account-level settings and RFQs, but not trading.
- `admin`: orders, cancel, deposit, withdraw, transfer, and other sensitive actions.

If an order or cancel fails with an auth/scope error, do not retry blindly. Check the key scope.

## Signed Order Payloads Are Caller-Owned

The adapter requires a complete signed order payload for `debug_order(...)` and `submit_order(...)`. It does not derive `max_fee`, build EIP-712/action signing payloads, or sign orders. Use Derive's current signing SDK/client docs for that payload and verify all fields against the current instrument constraints.

## Ticker Versus Orderbook

REST `public/get_tickers` gives best bid/ask, mark, index, stats, and option pricing fields. Full orderbook depth is a WebSocket channel named like:

```text
orderbook.{instrument_name}.{group}.{depth}
```

The adapter currently builds this channel name but does not maintain WebSocket subscriptions.

## Gorlami Limitation

Gorlami tests in this repo simulate EVM contract calls on forked chains. Derive option discovery, account reads, order debug, order submission, and cancel are Derive API/CLOB workflows, with settlement handled by Derive's matching/protocol pipeline. There is no current repo helper that can fork-simulate those API workflows on Derive Chain 957.

Use adapter unit tests and `submit_order(..., dry_run=True)` as deterministic validation before any live submit.

## Settlement And Margin

Derive options are European and settle to a 30 minute TWAP. Standard margin is centered around zero, and maintenance margin below zero is liquidatable. Portfolio margin can reduce margin requirements but is constrained to one denominated base asset per portfolio margin account.
