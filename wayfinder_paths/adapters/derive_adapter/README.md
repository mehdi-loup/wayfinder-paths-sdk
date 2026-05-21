# DeriveAdapter

Derive options, perps, and spot REST adapter for Wayfinder Paths.

- **Type**: `DERIVE`
- **Module**: `wayfinder_paths.adapters.derive_adapter.adapter.DeriveAdapter`
- **Default API**: `https://api.lyra.finance`
- **Demo API**: `https://api-demo.lyra.finance`

## Scope

This adapter emphasizes Derive options workflows:

- Public discovery: active option instruments, expiries, strikes, fee/tick constraints.
- Public quote reads: `public/get_tickers` and `public/get_ticker`.
- Account reads: subaccounts, subaccount aggregate state, positions, open orders, margin simulation.
- Order workflows: signed order debug and signed order submission pass-through, plus cancel.

Full WebSocket streaming, deposits, withdrawals, collateral transfers, position transfers, RFQs, liquidation actions, session-key management, and on-chain Derive Chain contract calls are intentionally out of scope for the first adapter.

## Auth And Signing

Derive private REST endpoints require these headers:

- `X-LyraWallet`: the Derive wallet/account address.
- `X-LyraTimestamp`: current UTC timestamp in milliseconds.
- `X-LyraSignature`: standard Ethereum message signature of the timestamp.

When constructed through `get_adapter(DeriveAdapter, "main")`, Wayfinder can auto-wire `sign_hash_callback` and `wallet_address`. If your Derive wallet differs from the signing wallet/session key, pass `derive_wallet_address` explicitly.

Order submission is self-custodial. Derive requires endpoint authentication plus a signed order payload. This adapter does **not** recreate Derive's EIP-712/action-signing SDK. Callers must provide `signature`, `signer`, `nonce`, `max_fee`, `signature_expiry_sec`, and the other required order fields.

## Usage

```python
from wayfinder_paths.adapters.derive_adapter import DeriveAdapter

adapter = DeriveAdapter()

ok, options = await adapter.list_options(currency="ETH")
ok, expiries = await adapter.list_option_expiries(currency="ETH")
ok, tickers = await adapter.get_option_tickers(
    currency="ETH",
    expiry_date=expiries[0]["expiry_date"],
)
```

Authenticated reads:

```python
from wayfinder_paths.adapters.derive_adapter import DeriveAdapter
from wayfinder_paths.mcp.scripting import get_adapter

adapter = await get_adapter(
    DeriveAdapter,
    "main",
    derive_wallet_address="0xYourDeriveWallet",
)

ok, subaccounts = await adapter.get_subaccounts()
ok, positions = await adapter.get_positions(subaccount_id=12345)
ok, margin = await adapter.get_margin(subaccount_id=12345)
```

Signed order dry-run before live submit:

```python
order = {
    "subaccount_id": 12345,
    "instrument_name": "ETH-20260522-2500-C",
    "direction": "buy",
    "amount": "0.1",
    "limit_price": "20",
    "max_fee": "2",
    "nonce": DeriveAdapter.new_order_nonce(),
    "signature_expiry_sec": 1779327600,
    "signer": "0xSignerOrSessionKey",
    "signature": "0xSignedOrderPayload",
    "order_type": "limit",
    "time_in_force": "gtc",
}

ok, debug = await adapter.submit_order(order, dry_run=True)
if ok:
    ok, result = await adapter.submit_order(order)
```

## Derive Docs Used

- https://docs.derive.xyz/llms.txt
- https://docs.derive.xyz/docs/about-derive.md
- https://docs.derive.xyz/docs/supported-products-1.md
- https://docs.derive.xyz/docs/standard-margin-1.md
- https://docs.derive.xyz/docs/portfolio-margin-1.md
- https://docs.derive.xyz/docs/settlements.md
- https://docs.derive.xyz/docs/lyra-chain.md
- https://docs.derive.xyz/reference/overview.md
- https://docs.derive.xyz/reference/json-rpc.md
- https://docs.derive.xyz/reference/authentication.md
- https://docs.derive.xyz/reference/session-keys.md
- https://docs.derive.xyz/reference/rate-limits.md
- https://docs.derive.xyz/reference/post_public-get-instruments.md
- https://docs.derive.xyz/reference/post_public-get-tickers.md
- https://docs.derive.xyz/reference/orderbook-instrument_name-group-depth.md
- https://docs.derive.xyz/reference/post_private-order-debug.md
- https://docs.derive.xyz/reference/post_private-order.md
- https://docs.derive.xyz/reference/post_private-cancel.md

## Testing

```bash
poetry run pytest -o addopts= wayfinder_paths/adapters/derive_adapter -q
```
