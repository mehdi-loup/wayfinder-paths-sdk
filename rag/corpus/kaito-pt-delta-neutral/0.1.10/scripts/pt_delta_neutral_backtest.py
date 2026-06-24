"""Backtest: Pendle PT-sKAITO fixed yield + Hyperliquid KAITO short hedge.

Data sources:
- Delta Lab: KAITO price + funding rate timeseries (hourly)
- Pendle API: PT-sKAITO market history (ptPrice, impliedApy) via adapter

Strategy:
- Long leg: buy PT-sKAITO (fixed yield accrues as PT converges to par at maturity)
- Short leg: short KAITO perp on Hyperliquid (hedges directional exposure)
- Hedge ratio: pt_price / underlying_price (approximate PT delta)

Funding sign convention:
- Positive funding → longs pay shorts → good for our short
- Negative funding → shorts pay longs → bad for our short
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    lookback_days: int
    notional_usd: float
    leverage: float
    market_address: str
    chain_id: int
    limit: int


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_float(v: object) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except Exception:
        return float("nan")
    return f if math.isfinite(f) else float("nan")


def _normalize_ts(ts: str) -> str:
    """Normalize timestamp to hourly resolution for joining."""
    return ts.replace("Z", "").split(".")[0][:13] + ":00:00"


async def _load_delta_lab_data(
    *, symbol: str, lookback_days: int, limit: int
) -> tuple[dict[str, float], dict[str, float]]:
    """Load price and funding data from Delta Lab. Returns (price_map, funding_map)."""
    from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient

    client = DeltaLabClient()
    series = await client.get_asset_timeseries(
        symbol=symbol,
        lookback_days=lookback_days,
        limit=limit,
        series="price,funding",
    )

    price_df = series.get("price")
    funding_df = series.get("funding")
    if price_df is None or price_df.empty:
        raise RuntimeError("Delta Lab returned no price data for " + symbol)

    # Build price lookup: ts -> price_usd
    price_map: dict[str, float] = {}
    price_reset = price_df.reset_index()
    for _, row in price_reset.iterrows():
        ts = row.get("ts")
        if ts is None:
            continue
        key = _normalize_ts(
            ts.to_pydatetime().replace(tzinfo=UTC).isoformat()
        )
        price_map[key] = _to_float(row.get("price_usd"))

    # Build funding lookup: ts -> funding_rate (hourly)
    funding_map: dict[str, float] = {}
    if funding_df is not None and not funding_df.empty:
        funding_reset = funding_df.reset_index()
        for _, row in funding_reset.iterrows():
            ts = row.get("ts")
            if ts is None:
                continue
            key = _normalize_ts(
                ts.to_pydatetime().replace(tzinfo=UTC).isoformat()
            )
            rate = _to_float(row.get("funding_rate"))
            # Keep highest OI venue if duplicates
            existing = funding_map.get(key)
            if existing is None or not math.isfinite(existing):
                funding_map[key] = rate

    return price_map, funding_map


async def _load_pendle_history(
    *, chain_id: int, market_address: str, lookback_days: int
) -> dict[str, dict[str, float]]:
    """Load PT price and implied APY from Pendle API. Returns ts -> {pt_price, implied_apy}."""
    from wayfinder_paths.adapters.pendle_adapter.adapter import PendleAdapter

    adapter = PendleAdapter()
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)

    data = await adapter.fetch_market_history(
        chain_id=chain_id,
        market_address=market_address,
        time_frame="hour",
        timestamp_start=_iso(start),
        timestamp_end=_iso(end),
    )

    results = data.get("results") or data.get("data") or []
    if isinstance(results, dict):
        results = results.get("results", [])
    if not results:
        raise RuntimeError(
            f"Pendle API returned no history for market {market_address} on chain {chain_id}"
        )

    pendle_map: dict[str, dict[str, float]] = {}
    for row in results:
        ts_raw = row.get("timestamp") or row.get("ts")
        if not ts_raw:
            continue
        key = _normalize_ts(str(ts_raw))
        pt_price = _to_float(row.get("ptPrice"))
        implied_apy = _to_float(row.get("impliedApy"))
        if math.isfinite(pt_price) and pt_price > 0:
            pendle_map[key] = {
                "pt_price": pt_price,
                "implied_apy": implied_apy if math.isfinite(implied_apy) else 0.0,
            }

    return pendle_map


def _run_backtest(
    *,
    config: BacktestConfig,
    price_map: dict[str, float],
    funding_map: dict[str, float],
    pendle_map: dict[str, dict[str, float]],
) -> dict[str, Any]:
    # Find common timestamps across all three data sources
    common_ts = sorted(
        set(price_map.keys()) & set(pendle_map.keys())
    )
    if len(common_ts) < 2:
        raise RuntimeError(
            f"Not enough overlapping data points ({len(common_ts)}). "
            "Check that Pendle history and Delta Lab price data overlap in time."
        )

    notional = config.notional_usd
    half_notional = notional / 2.0

    # Initialize at first timestamp
    t0 = common_ts[0]
    pt_price_0 = pendle_map[t0]["pt_price"]  # USD price per PT
    spot_price_0 = price_map[t0]

    # Long leg: buy PT-sKAITO worth half_notional
    # ptPrice from Pendle API is already in USD (not a ratio to underlying)
    pt_shares = half_notional / pt_price_0

    # Short leg: short KAITO perp — size to match PT's delta exposure
    # PT delta ≈ pt_price / spot_price (how much PT moves per $1 of KAITO)
    pt_delta = pt_price_0 / spot_price_0
    short_notional_usd = half_notional * pt_delta * config.leverage
    short_shares = short_notional_usd / spot_price_0

    # Track cumulative PnL components
    cum_funding_usd = 0.0
    cum_pt_carry_usd = 0.0
    prev_spot = spot_price_0
    cum_short_pnl = 0.0

    # Cash = total notional minus what we spent on PT; rest is perp margin
    cash = notional - half_notional

    nav_series: list[float] = []
    points: list[dict[str, Any]] = []
    peak_nav = notional
    max_dd = 0.0
    returns: list[float] = []

    for i, ts in enumerate(common_ts):
        spot = price_map[ts]
        pt_data = pendle_map[ts]
        pt_price = pt_data["pt_price"]
        implied_apy = pt_data["implied_apy"]
        funding_rate = funding_map.get(ts, 0.0)
        if not math.isfinite(funding_rate):
            funding_rate = 0.0

        if i > 0:
            # Time delta (hours → years)
            t_prev = _parse_iso(common_ts[i - 1] + "Z")
            t_now = _parse_iso(ts + "Z")
            dt_hours = (t_now - t_prev).total_seconds() / 3600.0
            dt_years = dt_hours / 8760.0

            # Short perp mark-to-market PnL
            short_mtm = -short_shares * (spot - prev_spot)
            cum_short_pnl += short_mtm

            # Short perp funding income/cost
            # funding_rate is hourly; positive = shorts receive
            funding_pnl = funding_rate * short_notional_usd * dt_hours
            cum_funding_usd += funding_pnl

            # PT carry (implied APY accrual approximation)
            pt_carry = implied_apy * dt_years * (pt_shares * pt_price)
            cum_pt_carry_usd += pt_carry

        # Current leg values (pt_price is already USD)
        pt_leg_usd = pt_shares * pt_price
        short_leg_usd = cash + cum_short_pnl + cum_funding_usd

        nav = pt_leg_usd + short_leg_usd
        nav_series.append(nav)

        if nav > peak_nav:
            peak_nav = nav
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

        if i > 0:
            prev_nav = nav_series[-2]
            ret = (nav - prev_nav) / prev_nav if prev_nav > 0 else 0.0
            returns.append(ret)

        # Hedge ratio (PT delta approximation: pt_price_usd / spot_price_usd)
        hedge_ratio = pt_price / spot if spot > 0 and math.isfinite(pt_price) else 1.0

        prev_spot = spot

        points.append({
            "ts": ts.replace(" ", "T") + "Z" if "T" not in ts else ts + "Z",
            "kaito_price_usd": round(spot, 6),
            "pt_price": round(pt_price, 6),
            "implied_apy": round(implied_apy, 6),
            "funding_rate": round(funding_rate, 10),
            "nav_usd": round(nav, 2),
            "pt_leg_usd": round(pt_leg_usd, 2),
            "short_leg_usd": round(short_leg_usd, 2),
            "cumulative_funding_usd": round(cum_funding_usd, 2),
            "cumulative_pt_carry_usd": round(cum_pt_carry_usd, 2),
            "hedge_ratio": round(hedge_ratio, 4),
        })

    # Compute summary stats
    start_nav = notional
    end_nav = nav_series[-1]
    total_return = (end_nav / start_nav - 1.0) if start_nav > 0 else 0.0

    # Duration in days
    t_start = _parse_iso(common_ts[0] + "Z")
    t_end = _parse_iso(common_ts[-1] + "Z")
    duration_days = (t_end - t_start).total_seconds() / 86400.0
    ann_factor = 365.0 / duration_days if duration_days > 0 else 1.0
    ann_return = (1.0 + total_return) ** ann_factor - 1.0

    # Sharpe (annualized, hourly returns)
    if returns:
        import statistics

        mean_ret = statistics.mean(returns)
        std_ret = statistics.stdev(returns) if len(returns) > 1 else 1e-10
        hourly_sharpe = mean_ret / std_ret if std_ret > 0 else 0.0
        sharpe = hourly_sharpe * math.sqrt(8760)
    else:
        sharpe = 0.0

    # Avg rates
    valid_apy = [p["implied_apy"] for p in points if math.isfinite(p["implied_apy"])]
    valid_fr = [p["funding_rate"] for p in points if math.isfinite(p["funding_rate"])]
    avg_implied_apy = sum(valid_apy) / len(valid_apy) if valid_apy else 0.0
    avg_funding_rate = sum(valid_fr) / len(valid_fr) if valid_fr else 0.0

    # Price movement P&L = total profit minus carry components
    total_profit = end_nav - start_nav
    price_movement_usd = total_profit - cum_pt_carry_usd - cum_funding_usd

    summary = {
        "startNavUsd": round(start_nav, 2),
        "endNavUsd": round(end_nav, 2),
        "totalReturnPct": round(total_return * 100, 3),
        "annualizedReturnPct": round(ann_return * 100, 3),
        "sharpe": round(sharpe, 3),
        "maxDrawdownPct": round(max_dd * 100, 3),
        "totalFundingUsd": round(cum_funding_usd, 2),
        "totalPtCarryUsd": round(cum_pt_carry_usd, 2),
        "priceMovementUsd": round(price_movement_usd, 2),
        "avgImpliedApy": round(avg_implied_apy, 6),
        "avgFundingRateHourly": round(avg_funding_rate, 10),
        "avgFundingRateAnnualized": round(avg_funding_rate * 8760, 6),
        "durationDays": round(duration_days, 1),
        "dataPoints": len(points),
        "generatedAt": _iso(datetime.now(UTC)),
    }

    return {
        "schemaVersion": "0.1",
        "source": "delta-lab+pendle",
        "asset": {"symbol": config.symbol},
        "notionalUsdDefault": config.notional_usd,
        "leverage": config.leverage,
        "market": {
            "chain_id": config.chain_id,
            "market_address": config.market_address,
            "maturity": "2026-07-30T00:00:00Z",
        },
        "summary": summary,
        "points": points,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest Pendle PT-sKAITO + Hyperliquid KAITO short delta-neutral carry."
    )
    parser.add_argument("--symbol", default="KAITO")
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--notional-usd", type=float, default=100_000)
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument(
        "--market-address",
        default="0xb0eb82ba25ffa51641d8613d270ad79183171fac",
        help="Pendle market address for PT-sKAITO on Base",
    )
    parser.add_argument("--chain-id", type=int, default=8453)
    parser.add_argument("--out", default="", help="Output JSON path")
    args = parser.parse_args()

    config = BacktestConfig(
        symbol=str(args.symbol).upper().strip(),
        lookback_days=int(args.lookback_days),
        notional_usd=float(args.notional_usd),
        leverage=max(1.0, min(2.0, float(args.leverage))),
        market_address=str(args.market_address),
        chain_id=int(args.chain_id),
        limit=int(args.limit),
    )

    async def _run() -> dict[str, Any]:
        price_map, funding_map = await _load_delta_lab_data(
            symbol=config.symbol,
            lookback_days=config.lookback_days,
            limit=config.limit,
        )
        pendle_map = await _load_pendle_history(
            chain_id=config.chain_id,
            market_address=config.market_address,
            lookback_days=config.lookback_days,
        )
        return _run_backtest(
            config=config,
            price_map=price_map,
            funding_map=funding_map,
            pendle_map=pendle_map,
        )

    result = asyncio.run(_run())

    if args.out:
        from pathlib import Path

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2))
        print(json.dumps({"ok": True, "out": str(out_path), "summary": result["summary"]}, indent=2))
    else:
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
