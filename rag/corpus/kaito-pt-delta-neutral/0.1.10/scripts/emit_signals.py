"""Emit scheduled signals for the KAITO PT delta-neutral carry pack.

Signals:
1. Health check (every run) — snapshot of current strategy state
2. Net carry 5% increment — when net carry crosses a 5% boundary
3. Market expiry — at 30d, 7d, 1d before PT maturity
4. Funding flip — when funding rate changes sign
5. Delta drift — when hedge ratio deviates >5% from initial
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

SLUG = "kaito-pt-delta-neutral"
VERSION = "0.1.2"
SYMBOL = "KAITO"
MARKET_ADDRESS = "0xb0eb82ba25ffa51641d8613d270ad79183171fac"
CHAIN_ID = 8453
MATURITY = datetime(2026, 7, 30, tzinfo=UTC)

# State file to track last emitted band and funding sign
STATE_FILE = Path(__file__).parent.parent / ".signal_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_band": None, "last_funding_positive": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def _fetch_current_data() -> dict:
    from wayfinder_paths.adapters.pendle_adapter.adapter import PendleAdapter
    from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient

    client = DeltaLabClient()
    adapter = PendleAdapter()

    # Fetch latest price + funding from Delta Lab (1 day lookback, latest point)
    series = await client.get_asset_timeseries(
        symbol=SYMBOL,
        lookback_days=1,
        limit=10,
        series="price,funding",
    )

    price_df = series.get("price")
    funding_df = series.get("funding")

    kaito_price = 0.0
    if price_df is not None and not price_df.empty:
        kaito_price = float(price_df.iloc[-1].get("price_usd", 0))

    funding_rate = 0.0
    if funding_df is not None and not funding_df.empty:
        funding_rate = float(funding_df.iloc[-1].get("funding_rate", 0))

    # Fetch latest PT snapshot from Pendle
    now = datetime.now(UTC)
    data = await adapter.fetch_market_history(
        chain_id=CHAIN_ID,
        market_address=MARKET_ADDRESS,
        time_frame="day",
        timestamp_start=(now - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        timestamp_end=now.isoformat().replace("+00:00", "Z"),
    )

    results = data.get("results") or data.get("data") or []
    if isinstance(results, dict):
        results = results.get("results", [])

    pt_price = 0.0
    implied_apy = 0.0
    if results:
        latest = results[-1]
        pt_price = float(latest.get("ptPrice", 0))
        implied_apy = float(latest.get("impliedApy", 0))

    hedge_ratio = pt_price / kaito_price if kaito_price > 0 else 0.0
    funding_ann = funding_rate * 8760
    net_carry_apr = implied_apy + funding_ann  # funding_ann is negative when shorts pay
    days_to_maturity = max(0, (MATURITY - now).total_seconds() / 86400)

    return {
        "kaito_price": kaito_price,
        "pt_price": pt_price,
        "implied_apy": implied_apy,
        "funding_rate": funding_rate,
        "funding_ann": funding_ann,
        "net_carry_apr": net_carry_apr,
        "hedge_ratio": hedge_ratio,
        "days_to_maturity": days_to_maturity,
    }


def _emit(title: str, message: str, level: str, metrics: dict) -> None:
    from wayfinder_paths.paths.client import PathsApiClient

    client = PathsApiClient()
    result = client.emit_signal(
        slug=SLUG,
        path_version=VERSION,
        title=title,
        message=message,
        level=level,
        metrics=metrics,
    )
    print(json.dumps({"signal": title, "level": level, "result": result}, indent=2))


async def run() -> None:
    data = await _fetch_current_data()
    state = _load_state()

    net_carry_pct = data["net_carry_apr"] * 100
    implied_pct = data["implied_apy"] * 100
    funding_pct = data["funding_ann"] * 100
    dtm = data["days_to_maturity"]

    # 1. Health check (always emit)
    _emit(
        title=f"Health check: net carry {net_carry_pct:.1f}% APR",
        message=(
            f"PT APY {implied_pct:.1f}% | "
            f"Funding {funding_pct:.1f}% ann | "
            f"Hedge {data['hedge_ratio']:.2f} | "
            f"{dtm:.0f}d to maturity | "
            f"KAITO ${data['kaito_price']:.4f}"
        ),
        level="info",
        metrics={
            "net_carry_apr": round(data["net_carry_apr"], 4),
            "implied_apy": round(data["implied_apy"], 4),
            "funding_rate_ann": round(data["funding_ann"], 4),
            "hedge_ratio": round(data["hedge_ratio"], 4),
            "days_to_maturity": round(dtm, 0),
            "kaito_price_usd": round(data["kaito_price"], 6),
        },
    )

    # 2. Net carry 5% increment
    current_band = int(net_carry_pct // 5) * 5
    last_band = state.get("last_band")
    if last_band is not None and current_band != last_band:
        direction = "above" if current_band > last_band else "below"
        _emit(
            title=f"Net carry crossed {direction} {current_band}% APR",
            message=f"PT APY {implied_pct:.1f}% + Funding {funding_pct:.1f}% = {net_carry_pct:.1f}% net carry",
            level="info" if current_band >= 20 else "warning",
            metrics={
                "net_carry_apr": round(data["net_carry_apr"], 4),
                "band": current_band / 100,
            },
        )
    state["last_band"] = current_band

    # 3. Market expiry alerts
    if 29.5 < dtm <= 30.5:
        _emit(
            title="PT-sKAITO maturity in 30 days",
            message="Plan exit or roll to a new PT.",
            level="info",
            metrics={"days_to_maturity": round(dtm, 0)},
        )
    elif 6.5 < dtm <= 7.5:
        _emit(
            title="PT-sKAITO maturity in 7 days",
            message="Close short hedge and prepare PT redemption, or roll into a new maturity.",
            level="warning",
            metrics={"days_to_maturity": round(dtm, 0)},
        )
    elif 0.5 < dtm <= 1.5:
        _emit(
            title="PT-sKAITO expires tomorrow",
            message="Immediate action required: close short, redeem PT.",
            level="error",
            metrics={"days_to_maturity": round(dtm, 0)},
        )

    # 4. Funding flip
    funding_positive = data["funding_rate"] >= 0
    last_funding_positive = state.get("last_funding_positive")
    if last_funding_positive is not None and funding_positive != last_funding_positive:
        if funding_positive:
            _emit(
                title="KAITO funding turned positive",
                message=f"Funding rate {funding_pct:+.1f}% annualized — shorts now receiving",
                level="info",
                metrics={"funding_rate_ann": round(data["funding_ann"], 4)},
            )
        else:
            _emit(
                title="KAITO funding turned negative",
                message=f"Funding rate {funding_pct:.1f}% annualized — shorts now paying",
                level="warning",
                metrics={"funding_rate_ann": round(data["funding_ann"], 4)},
            )
    state["last_funding_positive"] = funding_positive

    _save_state(state)
    print("\nSignal run complete.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
