from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest

from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter
from wayfinder_paths.core.backtesting.data import (
    drop_incomplete_bars,
    fetch_funding_rates,
    fetch_prices,
)
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT

pytestmark = [
    pytest.mark.live_data,
    pytest.mark.skipif(
        os.getenv("WAYFINDER_LIVE_DATA_TESTS") != "1",
        reason="set WAYFINDER_LIVE_DATA_TESTS=1 to hit live market-data endpoints",
    ),
]


def _assert_no_incomplete_hourly_bar(df: pd.DataFrame, now: datetime) -> None:
    assert not df.empty
    idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    assert (idx + pd.Timedelta(hours=1) <= pd.Timestamp(now)).all()


@pytest.mark.asyncio
async def test_hyperliquid_direct_candles_filter_current_bar():
    now = datetime.now(UTC)
    start = now - timedelta(hours=6)
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": "1h",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(now.timestamp() * 1000),
        },
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post("https://api.hyperliquid.xyz/info", json=payload)
        response.raise_for_status()
        rows = response.json()

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    prices = df.set_index("timestamp")["c"].astype(float).to_frame("BTC")
    filtered = drop_incomplete_bars(prices, "1h", as_of=now, timestamp_label="open")

    _assert_no_incomplete_hourly_bar(filtered, now)
    if rows and pd.Timestamp(rows[-1]["T"], unit="ms", tz="UTC") > pd.Timestamp(now):
        assert filtered.index[-1] < prices.index[-1]


@pytest.mark.asyncio
async def test_ccxt_binance_ohlcv_filter_current_bar():
    now = datetime.now(UTC)
    adapter = CCXTAdapter(exchanges={"binance": {}})
    try:
        rows = await adapter.binance.fetch_ohlcv("BTC/USDT", "1h", limit=6)
    finally:
        await adapter.close()

    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"])
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    prices = df.set_index("timestamp")["c"].astype(float).to_frame("BTC")
    filtered = drop_incomplete_bars(prices, "1h", as_of=now, timestamp_label="open")

    _assert_no_incomplete_hourly_bar(filtered, now)


@pytest.mark.asyncio
async def test_delta_lab_price_timeseries_filter_current_bar():
    now = datetime.now(UTC)
    data = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol="BTC",
        series="price",
        lookback_days=1,
        limit=30,
        as_of=now,
    )
    price = data["price"][["price_usd"]].rename(columns={"price_usd": "BTC"})
    filtered = drop_incomplete_bars(price, "1h", as_of=now, timestamp_label="open")

    _assert_no_incomplete_hourly_bar(filtered, now)


@pytest.mark.asyncio
async def test_sdk_fetchers_return_only_completed_hourly_bars():
    now = datetime.now(UTC)
    start = now - timedelta(days=1)
    start_s = start.isoformat()
    end_s = now.isoformat()

    for source in ("hyperliquid", "ccxt", "delta_lab"):
        prices = await fetch_prices(
            ["BTC"], start_s, end_s, interval="1h", source=source
        )
        _assert_no_incomplete_hourly_bar(prices, now)

    funding = await fetch_funding_rates(["BTC"], start_s, end_s, venue="hyperliquid")
    _assert_no_incomplete_hourly_bar(funding, now)
