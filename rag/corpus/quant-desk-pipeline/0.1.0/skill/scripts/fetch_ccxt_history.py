"""
CCXT Binance historical OHLCV fetcher for Phase 5d.

Hyperliquid's candle API caps at ~5000 bars per symbol regardless of request
window (~200 days for hourly). That's too short for honest walk-forward. Binance
spot via CCXT goes back to 2017+ for majors — use this for all Phase 5d
multi-year tests.

Usage:
    from examples.fetch_ccxt_history import fetch_multi_symbol_history

    prices = await fetch_multi_symbol_history(
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        start="2023-10-01",
        end="2026-04-15",
        interval="1h",
    )
    # prices is a DataFrame[timestamp × symbol] of closes, aligned across symbols.

Returned DataFrame has columns keyed by the base asset (e.g., "BTC" not
"BTC/USDT") so it drops into the same backtest + signal_fn pipeline used
everywhere else in the skill.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pandas as pd

from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter
from wayfinder_paths.core.utils.dataframe_cache import disk_cached

# Quant-desk fetches share a /tmp cache so grid-search cells (and reruns within
# a session) reuse paginated CCXT history instead of re-fetching from Binance.
# /tmp survives across sessions on the same host but is OS-cleared on reboot —
# right semantics for ephemeral research data.
_CCXT_CACHE_DIR = "/tmp/wayfinder_quant_desk_cache"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


async def _fetch_one(adapter: CCXTAdapter, symbol: str, start_ms: int, end_ms: int, interval: str) -> pd.DataFrame:
    all_candles = []
    cursor = start_ms
    pages = 0
    while cursor < end_ms and pages < 100:
        batch = await adapter.binance.fetch_ohlcv(symbol, interval, since=cursor, limit=1000)
        if not batch:
            break
        all_candles.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break
        # advance 1 interval beyond last candle
        interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
        cursor = last_ts + interval_ms
        pages += 1
    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=["t", "o", "h", "l", "c", "v"])
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["t"]).set_index("t").sort_index()
    df = df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
    return df


@disk_cached(namespace="ccxt_prices", cache_dir=_CCXT_CACHE_DIR, end_arg="end")
async def fetch_multi_symbol_history(
    symbols: list[str],
    start: str,
    end: str,
    interval: str = "1h",
) -> pd.DataFrame:
    """Fetch aligned hourly closes for multiple symbols from Binance via CCXT.

    Args:
        symbols: List of CCXT pair strings, e.g. ["BTC/USDT", "ETH/USDT"].
        start, end: ISO date strings (UTC).
        interval: "1m" | "5m" | "15m" | "1h" | "4h" | "1d".

    Returns:
        DataFrame indexed by UTC timestamps, columns = base asset names (e.g. "BTC").
    """
    adapter = CCXTAdapter(exchanges={"binance": {}})
    try:
        start_dt = _parse_iso(start)
        end_dt = _parse_iso(end)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        series_map = {}
        for sym in symbols:
            df = await _fetch_one(adapter, sym, start_ms, end_ms, interval)
            if df.empty:
                continue
            # base asset — "BTC/USDT" → "BTC"
            base = sym.split("/")[0]
            series_map[base] = df["c"].astype(float)

        if not series_map:
            raise ValueError("No data fetched for any symbol")

        # Align on common index
        prices = pd.concat(series_map, axis=1)
        prices = prices.ffill().dropna(how="any")
        return prices
    finally:
        await adapter.close()


@disk_cached(namespace="ccxt_funding", cache_dir=_CCXT_CACHE_DIR, end_arg="end")
async def fetch_funding_history_optional(symbols: list[str], start: str, end: str) -> pd.DataFrame | None:
    """Best-effort funding-rate fetch from Binance perps (spot symbols won't have funding).

    Returns None if the exchange or symbol doesn't support funding-rate history,
    so callers can branch rather than fail.
    """
    adapter = CCXTAdapter(exchanges={"binance": {}})
    try:
        start_dt = _parse_iso(start)
        start_ms = int(start_dt.timestamp() * 1000)

        series_map = {}
        for sym in symbols:
            try:
                # Binance perp format: "BTC/USDT:USDT"
                perp_symbol = f"{sym.split('/')[0]}/USDT:USDT"
                batch = await adapter.binance.fetch_funding_rate_history(perp_symbol, since=start_ms, limit=1000)
                if not batch:
                    continue
                rows = [(r["timestamp"], r.get("fundingRate")) for r in batch if r.get("fundingRate") is not None]
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=["t", "rate"])
                df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
                df = df.set_index("t").sort_index()
                base = sym.split("/")[0]
                series_map[base] = df["rate"].astype(float)
            except Exception:
                continue

        if not series_map:
            return None
        return pd.concat(series_map, axis=1)
    finally:
        await adapter.close()


if __name__ == "__main__":
    async def _demo():
        prices = await fetch_multi_symbol_history(
            ["BTC/USDT", "ETH/USDT"],
            start="2024-01-01",
            end="2024-03-01",
            interval="1h",
        )
        print(f"fetched {prices.shape}")
        print(prices.head())
        print(prices.tail())

    asyncio.run(_demo())
