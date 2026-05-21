# Derive Execution

Derive order submission is a trading action. It is not just an HTTP request.

Derive requires two signatures for sensitive workflows:

- Endpoint authentication: `X-LyraWallet`, `X-LyraTimestamp`, `X-LyraSignature`.
- Action signature: the signed order payload fields sent to `private/order` or `private/order_debug`.

The adapter handles endpoint auth when a signing callback is configured. It does not build or sign Derive action payloads.

## Signed Order Dry-Run

Use `dry_run=True` first. This calls `private/order_debug`, which Derive documents as read-only.

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
```

Only submit live after the user has reviewed the instrument, side, size, price, max fee, margin impact, expiry, and account/subaccount.

```python
ok, result = await adapter.submit_order(order)
```

Required signed order fields:

- `amount`
- `direction`
- `instrument_name`
- `limit_price`
- `max_fee`
- `nonce`
- `signature`
- `signature_expiry_sec`
- `signer`
- `subaccount_id`

## Cancel

Cancelling is an admin-scoped private endpoint.

```python
ok, result = await adapter.cancel_order(
    instrument_name="ETH-20260522-2500-C",
    order_id="...",
    subaccount_id=12345,
)
```

Confirm the order belongs to the intended subaccount and instrument before cancelling. Use `get_open_orders(subaccount_id=...)` first.
