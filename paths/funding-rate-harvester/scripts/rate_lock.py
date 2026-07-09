"""
Boros fixed-rate lock for the funding-rate-harvester path.

A harvested short perp receives floating funding. When the floating EMA
exceeds the Boros implied fixed by the configured premium, we short YU on the
matching Boros market (receive fixed, pay floating) — converting the floating
stream to fixed for the tenor. Lock PnL is tracked separately from pair carry.

Market/tenor selection and sizing are pure functions (tested); BorosRateLock
is thin execution glue over the injected BorosAdapter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

BOROS_PLATFORM = "hyperliquid"

# Boros YU sizing depends on the market's collateral token: USDT-collateral
# markets are ~$1/YU, HYPE-collateral markets are 1 HYPE/YU.
USDT_TOKEN_ID = 3
HYPE_TOKEN_ID = 5

# Margin heuristic (see /using-boros-adapter): required margin scales with
# size × max(|implied APR|, floor); never size YU 1:1 against collateral. This
# is deliberately conservative (no time-to-maturity discount) — it over-holds
# collateral rather than risk a thin buffer.
MIN_RATE_FLOOR = 0.02
SAFETY_UTILIZATION = 0.6

DEFAULT_MIN_TENOR_DAYS = 5.0
DEFAULT_MAX_TENOR_DAYS = 45.0
DEFAULT_MIN_NOTIONAL_OI = 100_000.0


# ---------------------------------------------------------------------------
# Pure selection + sizing (unit-tested)
# ---------------------------------------------------------------------------

def select_lock_market(
    quotes: list[dict[str, Any]],
    *,
    min_tenor_days: float = DEFAULT_MIN_TENOR_DAYS,
    max_tenor_days: float = DEFAULT_MAX_TENOR_DAYS,
    min_notional_oi: float = DEFAULT_MIN_NOTIONAL_OI,
    target_tenor_days: float | None = None,
) -> dict[str, Any] | None:
    """Pick the lock tenor: shortest eligible tenor unless a target is given.

    Shorter tenors minimize the time the lock can diverge from the pair's
    remaining life; `target_tenor_days` (from `lock --tenor`) overrides with
    the closest match instead.
    """
    eligible = [
        q for q in quotes
        if min_tenor_days <= float(q.get("tenor_days") or 0.0) <= max_tenor_days
        and float(q.get("notional_oi") or 0.0) >= min_notional_oi
    ]
    if not eligible:
        return None
    if target_tenor_days is not None:
        return min(
            eligible, key=lambda q: abs(float(q["tenor_days"]) - target_tenor_days)
        )
    return min(eligible, key=lambda q: float(q["tenor_days"]))


def lock_size_yu(
    collateral_token_id: int,
    *,
    short_notional_usd: float,
    short_size_units: float,
) -> float:
    if collateral_token_id == HYPE_TOKEN_ID:
        return short_size_units
    return short_notional_usd


def required_lock_collateral(
    size_yu: float,
    implied_apr: float,
    *,
    rate_floor: float = MIN_RATE_FLOOR,
    utilization: float = SAFETY_UTILIZATION,
) -> float:
    """Collateral (in YU-denominated units) to hold the lock with buffer."""
    return size_yu * max(abs(implied_apr), rate_floor) / utilization


@dataclass
class LockQuote:
    market_id: int
    symbol: str
    tenor_days: float
    maturity_ts: float
    fixed_apr: float  # annualized implied fixed APR (executable bid, else mid)
    collateral_token_id: int
    size_yu: float
    required_collateral: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "symbol": self.symbol,
            "tenor_days": round(self.tenor_days, 2),
            "maturity_ts": self.maturity_ts,
            "fixed_apr": round(self.fixed_apr, 6),
            "collateral_token_id": self.collateral_token_id,
            "size_yu": round(self.size_yu, 6),
            "required_collateral": round(self.required_collateral, 6),
        }


# ---------------------------------------------------------------------------
# Execution glue (BorosAdapter injected; adapter methods return (ok, data))
# ---------------------------------------------------------------------------

class BorosRateLock:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def quote_lock(
        self,
        symbol: str,
        *,
        short_notional_usd: float,
        short_size_units: float,
        min_tenor_days: float = DEFAULT_MIN_TENOR_DAYS,
        max_tenor_days: float = DEFAULT_MAX_TENOR_DAYS,
        target_tenor_days: float | None = None,
    ) -> tuple[bool, LockQuote | str]:
        ok, quotes = await self.adapter.list_tenor_quotes(
            underlying_symbol=symbol.upper(), platform=BOROS_PLATFORM
        )
        if not ok:
            return False, f"list_tenor_quotes failed: {quotes}"
        rows = [
            {
                "market_id": q.market_id,
                "tenor_days": q.tenor_days,
                "maturity": q.maturity,
                "mid_apr": q.mid_apr,
                "notional_oi": getattr(q, "notional_oi", None) or 0.0,
            }
            for q in quotes
        ]
        selected = select_lock_market(
            rows,
            min_tenor_days=min_tenor_days,
            max_tenor_days=max_tenor_days,
            target_tenor_days=target_tenor_days,
        )
        if selected is None:
            return False, f"no Boros {symbol} market within tenor bounds"

        ok, market = await self.adapter.quote_market_by_id(int(selected["market_id"]))
        if not ok:
            return False, f"quote_market_by_id failed: {market}"
        remaining_days = max((market.maturity_ts - time.time()) / 86400.0, 0.0)
        # Short YU sells fixed — the bid is the executable side when present.
        # These adapter fields (`*_apr`) are already annualized implied APRs; do
        # not re-annualize by tenor.
        fixed_apr = (
            market.best_bid_apr if market.best_bid_apr is not None else market.mid_apr
        )
        size_yu = lock_size_yu(
            int(market.collateral_token_id),
            short_notional_usd=short_notional_usd,
            short_size_units=short_size_units,
        )
        return True, LockQuote(
            market_id=int(market.market_id),
            symbol=symbol.upper(),
            tenor_days=remaining_days,
            maturity_ts=float(market.maturity_ts),
            fixed_apr=float(fixed_apr),
            collateral_token_id=int(market.collateral_token_id),
            size_yu=size_yu,
            required_collateral=required_lock_collateral(size_yu, float(fixed_apr)),
        )

    async def open_lock(self, quote: LockQuote) -> tuple[bool, dict[str, Any]]:
        token_id = quote.collateral_token_id
        ok, balances = await self.adapter.get_account_balances(token_id=token_id)
        if not ok:
            return False, {"error": f"get_account_balances failed: {balances}"}
        cross = float((balances or {}).get("cross") or 0.0)
        if cross < quote.required_collateral:
            ok, asset = await self.adapter.get_asset_by_token_id(token_id=token_id)
            if not ok:
                return False, {"error": f"get_asset_by_token_id failed: {asset}"}
            shortfall = quote.required_collateral - cross
            amount_wei = int(round(shortfall * 10 ** int(asset["decimals"])))
            ok, dep = await self.adapter.deposit_to_cross_margin(
                collateral_address=asset["address"],
                amount_wei=amount_wei,
                token_id=token_id,
                market_id=quote.market_id,
            )
            if not ok:
                return False, {
                    "error": (
                        f"collateral deposit failed (need {shortfall:.4f} of token_id={token_id} "
                        f"on Arbitrum in the wallet): {dep}"
                    )
                }
        ok, order = await self.adapter.place_rate_order(
            market_id=quote.market_id,
            token_id=token_id,
            size_yu_wei=int(quote.size_yu * 1e18),
            side="short",
        )
        if not ok:
            return False, {"error": f"place_rate_order failed: {order}"}
        return True, {"quote": quote.to_dict(), "order": order}

    async def unwind_lock(self, market_id: int) -> tuple[bool, dict[str, Any]]:
        ok, res = await self.adapter.close_positions_market(market_id)
        if not ok:
            return False, {"error": f"close_positions_market failed: {res}"}
        return True, {"market_id": market_id, "result": res}

    async def lock_positions(self) -> tuple[bool, list[dict[str, Any]] | str]:
        """Active Boros rate positions with PnL, reported separately in status."""
        ok, positions = await self.adapter.get_active_positions()
        if not ok:
            return False, f"get_active_positions failed: {positions}"
        return True, [
            {
                "market_id": p.get("market_id"),
                "side": p.get("side"),
                "size": p.get("size"),
                "pnl": p.get("pnl"),
            }
            for p in positions
        ]
