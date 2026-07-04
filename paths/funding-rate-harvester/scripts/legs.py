"""
Hedge-venue + spot-leg abstractions for the funding-rate-harvester path.

The hedge venue is an abstraction from day one (v1.1 adds CCXT venues); v1.0
ships Hyperliquid only. Spot legs are pluggable long hedges that may earn
yield themselves: pendle_pt (fixed), etherfi weETH (staking), ethena sUSDe
(vault), and hl_spot (zero-yield fallback that keeps smoke tests on-exchange).

Everything network-facing is dependency-injected (adapters passed in) so the
pure selection/ordering logic at the top is unit-testable with fakes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.paired_filler import (
    MIN_NOTIONAL_USD,
    FillConfig,
    PairedFiller,
)

SPOT_LEG_NAMES = ("pendle_pt", "etherfi", "ethena", "hl_spot")

# HL spot lists wrapped majors, not the native coins.
HL_SPOT_COIN_OVERRIDES = {"BTC": "UBTC", "ETH": "UETH"}

# Canonical USDC per chain for Pendle PT entries (chain of the PT market).
USDC_BY_CHAIN = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
}
USDC_DECIMALS = 6

# BRAP token ids for the mainnet yield-token legs. Entry/exit goes through the
# open market (BRAP swap) rather than stake/cooldown flows: ether.fi withdraws
# are async NFTs and sUSDe unstake has a ~7d cooldown — both would strand a
# rotation mid-flight.
USDC_ETHEREUM_TOKEN_ID = "usd-coin-ethereum"
WEETH_TOKEN_ID = "wrapped-eeth-ethereum"
SUSDE_TOKEN_ID = "ethena-staked-usde-ethereum"
WEETH_DECIMALS = 18
SUSDE_DECIMALS = 18

# Yield-wrapper prefixes, longest first, for mapping Pendle market names to
# the hedged symbol (weETH → ETH, sUSDe → USDE, sKAITO → KAITO, ...).
_WRAPPER_PREFIXES = ("wst", "we", "rs", "st", "cb", "ws", "w", "s", "r", "e")

PriceLookup = Callable[[str], Awaitable[float | None]]
YieldLookup = Callable[[str], Awaitable[float | None]]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no I/O)
# ---------------------------------------------------------------------------

def hl_spot_coin(symbol: str) -> str:
    sym = symbol.upper()
    return HL_SPOT_COIN_OVERRIDES.get(sym, sym)


def pt_market_root(market_name: str) -> str:
    """Reduce a Pendle market name to the symbol it is price-exposed to."""
    base = market_name.split("-")[0].split(" ")[0].strip()
    upper = base.upper()
    for prefix in _WRAPPER_PREFIXES:
        if upper.startswith(prefix.upper()) and len(base) > len(prefix):
            return base[len(prefix):].upper()
    return upper


def match_pt_markets(symbol: str, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Markets whose underlying exposure tracks `symbol`, best fixedApy first.

    A PT only hedges a perp short if its price tracks the shorted asset —
    PT-weETH hedges an ETH short, PT-sUSDe hedges USDE, never cross-asset.
    """
    sym = symbol.upper()
    matched = [
        m for m in markets
        if pt_market_root(str(m.get("marketName") or "")) == sym
        or str(m.get("marketName") or "").upper() == sym
    ]
    return sorted(matched, key=lambda m: float(m.get("fixedApy") or 0.0), reverse=True)


def select_spot_leg(
    symbol: str,
    leg_yields: dict[str, float | None],
    priority: list[str],
) -> tuple[str, float] | None:
    """Pick the highest-yield available leg; config priority breaks ties.

    `leg_yields` maps leg name → APY for legs that support `symbol`, with
    None meaning "supports the symbol but yield data unavailable" — those are
    excluded rather than scored as 0, so missing data can't mis-rank a combo.
    """
    candidates = [
        (name, apy) for name, apy in leg_yields.items()
        if apy is not None and name in priority
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[1], priority.index(item[0])))
    return candidates[0]


def open_pair_steps(hedge_venue: str, spot_leg: str) -> list[str]:
    """Hedge first on entry — a failed spot leg leaves a visible short, never
    a silent unhedged long. Same-venue HL pairs fill both legs atomically."""
    if hedge_venue == "hyperliquid" and spot_leg == "hl_spot":
        return ["paired_atomic"]
    return ["hedge_short", "spot_open"]


def close_pair_steps(hedge_venue: str, spot_leg: str) -> list[str]:
    """Spot first, hedge last on exit — the short keeps protecting the book
    until the long is gone."""
    return ["spot_close", "hedge_close"]


# ---------------------------------------------------------------------------
# Hedge venue abstraction (v1.0: Hyperliquid; v1.1 adds CCXT venues)
# ---------------------------------------------------------------------------

@dataclass
class HedgePosition:
    symbol: str
    size_units: float  # positive = short size in coin units
    notional_usd: float
    entry_price: float | None
    mark_price: float | None
    liq_price: float | None
    unrealized_pnl_usd: float
    margin_used_usd: float

    @property
    def liq_distance_pct(self) -> float | None:
        if not self.liq_price or not self.mark_price or self.mark_price <= 0:
            return None
        return abs(self.liq_price - self.mark_price) / self.mark_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "size_units": self.size_units,
            "notional_usd": self.notional_usd,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "liq_price": self.liq_price,
            "liq_distance_pct": self.liq_distance_pct,
            "unrealized_pnl_usd": self.unrealized_pnl_usd,
            "margin_used_usd": self.margin_used_usd,
        }


class HedgeVenue(ABC):
    name: str
    funding_interval_hours: float

    @abstractmethod
    async def perp_snapshot(self) -> dict[str, dict[str, float]]:
        """Per-coin {funding_per_interval, mark_price, open_interest_usd}."""

    @abstractmethod
    async def mark_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def ensure_leverage(self, symbol: str, leverage: int) -> tuple[bool, str]: ...

    @abstractmethod
    async def open_short(
        self, symbol: str, notional_usd: float, slippage: float
    ) -> tuple[bool, dict[str, Any]]: ...

    @abstractmethod
    async def close_short(
        self, symbol: str, size_units: float | None, slippage: float
    ) -> tuple[bool, dict[str, Any]]: ...

    @abstractmethod
    async def short_position(self, symbol: str) -> HedgePosition | None: ...

    @abstractmethod
    async def free_margin_usd(self) -> float: ...


class HyperliquidHedge(HedgeVenue):
    name = "hyperliquid"
    funding_interval_hours = 1.0

    def __init__(
        self,
        adapter: Any,
        address: str,
        builder_fee: dict[str, Any] | None = None,
    ) -> None:
        self.adapter = adapter
        self.address = address
        self.builder_fee = builder_fee
        self._leverage_set: set[str] = set()

    def perp_asset_id(self, symbol: str) -> int:
        asset_id = self.adapter.coin_to_asset.get(symbol.upper())
        if asset_id is None:
            raise RuntimeError(f"{symbol} perp not listed on Hyperliquid")
        return int(asset_id)

    async def perp_snapshot(self) -> dict[str, dict[str, float]]:
        ok, data = await self.adapter.get_meta_and_asset_ctxs()
        if not ok:
            raise RuntimeError(f"get_meta_and_asset_ctxs failed: {data}")
        meta, ctxs = data[0], data[1]
        snapshot: dict[str, dict[str, float]] = {}
        for i, asset in enumerate(meta.get("universe", [])):
            coin = str(asset.get("name") or "")
            if not coin or i >= len(ctxs):
                continue
            ctx = ctxs[i]
            mark = float(ctx.get("markPx") or 0.0)
            oi_units = float(ctx.get("openInterest") or 0.0)
            snapshot[coin] = {
                "funding_per_interval": float(ctx.get("funding") or 0.0),
                "mark_price": mark,
                "open_interest_usd": oi_units * mark,
            }
        return snapshot

    async def mark_price(self, symbol: str) -> float:
        ok, prices = await self.adapter.get_all_mid_prices()
        if not ok or not isinstance(prices, dict):
            raise RuntimeError(f"get_all_mid_prices failed: {prices}")
        price = float(prices.get(symbol.upper(), 0.0))
        if price <= 0:
            raise RuntimeError(f"no mid price for {symbol}")
        return price

    async def ensure_leverage(self, symbol: str, leverage: int) -> tuple[bool, str]:
        sym = symbol.upper()
        if self.builder_fee:
            ok_fee, fee_msg = await self.adapter.ensure_builder_fee_approved(
                address=self.address, builder_fee=self.builder_fee
            )
            if not ok_fee:
                return False, fee_msg
        if sym in self._leverage_set:
            return True, f"{sym} leverage already set"
        ok, res = await self.adapter.update_leverage(
            asset_id=self.perp_asset_id(sym),
            leverage=int(leverage),
            is_cross=True,
            address=self.address,
        )
        if not ok:
            return False, f"update_leverage failed: {res}"
        self._leverage_set.add(sym)
        return True, f"set {sym} leverage to {leverage}x (cross)"

    async def open_short(
        self, symbol: str, notional_usd: float, slippage: float
    ) -> tuple[bool, dict[str, Any]]:
        sym = symbol.upper()
        asset_id = self.perp_asset_id(sym)
        price = await self.mark_price(sym)
        size = self.adapter.get_valid_order_size(asset_id, notional_usd / price)
        if size <= 0:
            return False, {"error": f"{sym} short size rounds to 0"}
        if size * price < MIN_NOTIONAL_USD:
            return False, {
                "error": f"{sym} short notional ${size * price:.2f} < HL ${MIN_NOTIONAL_USD:.0f} minimum"
            }
        ok, res = await self.adapter.place_market_order(
            asset_id=asset_id,
            is_buy=False,
            slippage=slippage,
            size=float(size),
            address=self.address,
            builder=self.builder_fee,
        )
        if not ok:
            return False, {"error": str(res)}
        return True, {"size_units": float(size), "price": price, "result": res}

    async def close_short(
        self, symbol: str, size_units: float | None, slippage: float
    ) -> tuple[bool, dict[str, Any]]:
        sym = symbol.upper()
        position = await self.short_position(sym)
        if position is None:
            return True, {"size_units": 0.0, "note": "no short position"}
        asset_id = self.perp_asset_id(sym)
        target = position.size_units if size_units is None else min(size_units, position.size_units)
        size = self.adapter.get_valid_order_size(asset_id, target)
        if size <= 0:
            return True, {"size_units": 0.0, "note": "close size rounds to 0"}
        ok, res = await self.adapter.place_market_order(
            asset_id=asset_id,
            is_buy=True,
            slippage=slippage,
            size=float(size),
            address=self.address,
            reduce_only=True,
            builder=self.builder_fee,
        )
        if not ok:
            return False, {"error": str(res)}
        return True, {"size_units": float(size), "result": res}

    async def short_position(self, symbol: str) -> HedgePosition | None:
        sym = symbol.upper()
        ok, state = await self.adapter.get_user_state(self.address)
        if not ok or not isinstance(state, dict):
            raise RuntimeError(f"get_user_state failed: {state}")
        for entry in state.get("assetPositions", []):
            pos = entry.get("position") if isinstance(entry, dict) else None
            if not isinstance(pos, dict) or str(pos.get("coin")) != sym:
                continue
            szi = float(pos.get("szi") or 0.0)
            if szi >= 0:
                return None
            mark = None
            try:
                mark = await self.mark_price(sym)
            except RuntimeError:
                pass
            liq_px = pos.get("liquidationPx")
            entry_px = pos.get("entryPx")
            return HedgePosition(
                symbol=sym,
                size_units=abs(szi),
                notional_usd=float(pos.get("positionValue") or 0.0),
                entry_price=float(entry_px) if entry_px is not None else None,
                mark_price=mark,
                liq_price=float(liq_px) if liq_px is not None else None,
                unrealized_pnl_usd=float(pos.get("unrealizedPnl") or 0.0),
                margin_used_usd=float(pos.get("marginUsed") or 0.0),
            )
        return None

    async def free_margin_usd(self) -> float:
        ok, state = await self.adapter.get_user_state(self.address)
        if not ok or not isinstance(state, dict):
            raise RuntimeError(f"get_user_state failed: {state}")
        return float(state.get("withdrawable") or 0.0)


# ---------------------------------------------------------------------------
# Spot legs
# ---------------------------------------------------------------------------

@dataclass
class SpotPosition:
    leg: str
    symbol: str
    units: float
    usd_value: float | None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg": self.leg,
            "symbol": self.symbol,
            "units": self.units,
            "usd_value": self.usd_value,
            "meta": self.meta,
        }


class SpotLeg(ABC):
    name: str

    @abstractmethod
    async def supports(self, symbol: str) -> bool: ...

    @abstractmethod
    async def yield_apy(self, symbol: str) -> float | None:
        """Annualized leg yield; None = unavailable (combo excluded, not 0)."""

    @abstractmethod
    async def open(self, symbol: str, usd_amount: float) -> tuple[bool, dict[str, Any]]: ...

    @abstractmethod
    async def close(
        self, symbol: str, units: float | None
    ) -> tuple[bool, dict[str, Any]]: ...

    @abstractmethod
    async def position(self, symbol: str) -> SpotPosition | None: ...


class HlSpotLeg(SpotLeg):
    """Plain Hyperliquid spot long. Zero yield; exists so any HL-listed asset
    can be harvested and so the smoke test stays entirely on-exchange."""

    name = "hl_spot"

    def __init__(
        self,
        adapter: Any,
        address: str,
        builder_fee: dict[str, Any] | None = None,
    ) -> None:
        self.adapter = adapter
        self.address = address
        self.builder_fee = builder_fee

    async def _spot_asset_id(self, symbol: str) -> int | None:
        coin = hl_spot_coin(symbol)
        spot_id = await self.adapter.get_spot_asset_id(coin, "USDC")
        if spot_id is None:
            ok, spot_assets = await self.adapter.get_spot_assets()
            if ok and isinstance(spot_assets, dict):
                spot_id = spot_assets.get(f"{coin}/USDC")
        return int(spot_id) if spot_id is not None else None

    async def supports(self, symbol: str) -> bool:
        return await self._spot_asset_id(symbol) is not None

    async def yield_apy(self, symbol: str) -> float | None:
        return 0.0

    async def _spot_price(self, symbol: str) -> float:
        # Perp mid of the base symbol; wrapped spot (UBTC/UETH) tracks it.
        ok, prices = await self.adapter.get_all_mid_prices()
        if not ok or not isinstance(prices, dict):
            raise RuntimeError(f"get_all_mid_prices failed: {prices}")
        for key in (hl_spot_coin(symbol), symbol.upper()):
            price = float(prices.get(key, 0.0))
            if price > 0:
                return price
        raise RuntimeError(f"no price for {symbol}")

    async def open(self, symbol: str, usd_amount: float) -> tuple[bool, dict[str, Any]]:
        spot_id = await self._spot_asset_id(symbol)
        if spot_id is None:
            return False, {"error": f"no HL spot pair for {symbol}"}
        price = await self._spot_price(symbol)
        size = self.adapter.get_valid_order_size(spot_id, usd_amount / price)
        if size <= 0 or size * price < MIN_NOTIONAL_USD:
            return False, {"error": f"spot buy below HL ${MIN_NOTIONAL_USD:.0f} minimum"}
        ok, res = await self.adapter.place_market_order(
            asset_id=spot_id,
            is_buy=True,
            slippage=0.05,
            size=float(size),
            address=self.address,
            builder=self.builder_fee,
        )
        if not ok:
            return False, {"error": str(res)}
        return True, {"units": float(size), "price": price, "result": res}

    async def close(self, symbol: str, units: float | None) -> tuple[bool, dict[str, Any]]:
        spot_id = await self._spot_asset_id(symbol)
        if spot_id is None:
            return False, {"error": f"no HL spot pair for {symbol}"}
        position = await self.position(symbol)
        if position is None or position.units <= 0:
            return True, {"units": 0.0, "note": "no spot balance"}
        target = position.units if units is None else min(units, position.units)
        size = self.adapter.get_valid_order_size(spot_id, target)
        if size <= 0:
            return True, {"units": 0.0, "note": "close size rounds to 0"}
        ok, res = await self.adapter.place_market_order(
            asset_id=spot_id,
            is_buy=False,
            slippage=0.05,
            size=float(size),
            address=self.address,
            builder=self.builder_fee,
        )
        if not ok:
            return False, {"error": str(res)}
        return True, {"units": float(size), "result": res}

    async def position(self, symbol: str) -> SpotPosition | None:
        coin = hl_spot_coin(symbol)
        ok, state = await self.adapter.get_spot_user_state(self.address)
        if not ok or not isinstance(state, dict):
            raise RuntimeError(f"get_spot_user_state failed: {state}")
        for bal in state.get("balances", []):
            if str(bal.get("coin") or bal.get("token")) != coin:
                continue
            total = float(bal.get("total") or 0.0)
            if total <= 0:
                return None
            usd_value: float | None = None
            try:
                usd_value = total * await self._spot_price(symbol)
            except RuntimeError:
                pass
            return SpotPosition(self.name, symbol.upper(), total, usd_value, {"coin": coin})
        return None


class PendlePtLeg(SpotLeg):
    """Long the PT of a yield wrapper of the shorted symbol (fixed yield)."""

    name = "pendle_pt"

    def __init__(
        self,
        adapter: Any,
        wallet_address: str,
        *,
        chains: list[int] | None = None,
        min_liquidity_usd: float = 250_000.0,
        min_days_to_expiry: float = 7.0,
        slippage: float = 0.01,
    ) -> None:
        self.adapter = adapter
        self.wallet_address = wallet_address
        self.chains = chains or list(USDC_BY_CHAIN)
        self.min_liquidity_usd = min_liquidity_usd
        self.min_days_to_expiry = min_days_to_expiry
        self.slippage = slippage
        self._markets_cache: list[dict[str, Any]] | None = None

    async def _markets(self) -> list[dict[str, Any]]:
        if self._markets_cache is None:
            self._markets_cache = await self.adapter.list_active_pt_yt_markets(
                chains=self.chains,
                min_liquidity_usd=self.min_liquidity_usd,
                min_days_to_expiry=self.min_days_to_expiry,
                sort_by="fixed_apy",
                descending=True,
            )
        return self._markets_cache

    async def find_market(self, symbol: str) -> dict[str, Any] | None:
        matches = match_pt_markets(symbol, await self._markets())
        for market in matches:
            if int(market.get("chainId") or 0) in USDC_BY_CHAIN:
                return market
        return None

    async def supports(self, symbol: str) -> bool:
        return await self.find_market(symbol) is not None

    async def yield_apy(self, symbol: str) -> float | None:
        market = await self.find_market(symbol)
        if market is None:
            return None
        return float(market.get("fixedApy") or 0.0)

    async def open(self, symbol: str, usd_amount: float) -> tuple[bool, dict[str, Any]]:
        market = await self.find_market(symbol)
        if market is None:
            return False, {"error": f"no active PT market tracks {symbol}"}
        chain_id = int(market["chainId"])
        amount_raw = str(int(round(usd_amount * 10**USDC_DECIMALS)))
        ok, res = await self.adapter.execute_swap(
            chain=chain_id,
            market_address=market["marketAddress"],
            token_in=USDC_BY_CHAIN[chain_id],
            token_out=market["ptAddress"],
            amount_in=amount_raw,
            slippage=self.slippage,
        )
        if not ok:
            return False, dict(res) if isinstance(res, dict) else {"error": str(res)}
        return True, {"market": market, "result": res}

    async def _positions_raw(self) -> list[tuple[int, dict[str, Any]]]:
        found: list[tuple[int, dict[str, Any]]] = []
        for chain_id in self.chains:
            ok, state = await self.adapter.get_full_user_state_per_chain(
                chain=chain_id, account=self.wallet_address, include_prices=True
            )
            if not ok or not isinstance(state, dict):
                logger.warning(f"pendle user state failed on chain {chain_id}: {state}")
                continue
            for pos in state.get("positions", []):
                raw = int((pos.get("balances", {}).get("pt", {}) or {}).get("raw", 0) or 0)
                if raw > 0:
                    found.append((chain_id, pos))
        return found

    async def _position_for(self, symbol: str) -> tuple[int, dict[str, Any]] | None:
        for chain_id, pos in await self._positions_raw():
            if pt_market_root(str(pos.get("marketName") or "")) == symbol.upper():
                return chain_id, pos
        return None

    async def close(self, symbol: str, units: float | None) -> tuple[bool, dict[str, Any]]:
        located = await self._position_for(symbol)
        if located is None:
            return True, {"units": 0.0, "note": "no PT position"}
        chain_id, pos = located
        pt_address = str(pos.get("pt") or "")
        raw_balance = int((pos.get("balances", {}).get("pt", {}) or {}).get("raw", 0) or 0)
        market = await self.find_market(symbol)
        if market is None or str(market.get("ptAddress", "")).lower() != pt_address.lower():
            # Market expired (or rolled off active list): redeem PT → underlying.
            underlying = str(pos.get("underlying") or "")
            if not underlying:
                return False, {"error": f"expired PT {pt_address} has no underlying to redeem to"}
            ok, res = await self.adapter.execute_convert(
                chain=chain_id,
                slippage=self.slippage,
                inputs=[{"token": pt_address, "amount": str(raw_balance)}],
                outputs=[underlying],
            )
            if not ok:
                return False, dict(res) if isinstance(res, dict) else {"error": str(res)}
            return True, {"mode": "redeem", "underlying": underlying, "result": res}
        ok, res = await self.adapter.execute_swap(
            chain=chain_id,
            market_address=market["marketAddress"],
            token_in=pt_address,
            token_out=USDC_BY_CHAIN[chain_id],
            amount_in=str(raw_balance),
            slippage=self.slippage,
        )
        if not ok:
            return False, dict(res) if isinstance(res, dict) else {"error": str(res)}
        return True, {"mode": "swap", "result": res}

    async def position(self, symbol: str) -> SpotPosition | None:
        located = await self._position_for(symbol)
        if located is None:
            return None
        chain_id, pos = located
        pt_bal = pos.get("balances", {}).get("pt", {}) or {}
        raw = int(pt_bal.get("raw", 0) or 0)
        units = float(pt_bal.get("formatted") or raw / 1e18)
        usd = pt_bal.get("usd")
        return SpotPosition(
            self.name,
            symbol.upper(),
            units,
            float(usd) if usd is not None else None,
            {"chain_id": chain_id, "market_name": pos.get("marketName"), "pt": pos.get("pt")},
        )


class BrapTokenLeg(SpotLeg):
    """Base for mainnet yield tokens entered/exited via BRAP market swaps."""

    name = "brap_token"
    symbol_supported = ""
    token_id = ""
    token_decimals = 18

    def __init__(
        self,
        brap_adapter: Any,
        wallet_address: str,
        *,
        balance_lookup: Callable[[], Awaitable[float]],
        price_lookup: PriceLookup | None = None,
        strategy_name: str = "funding-rate-harvester",
    ) -> None:
        self.brap = brap_adapter
        self.wallet_address = wallet_address
        self._balance_lookup = balance_lookup
        self._price_lookup = price_lookup
        self.strategy_name = strategy_name

    async def supports(self, symbol: str) -> bool:
        return symbol.upper() == self.symbol_supported

    async def open(self, symbol: str, usd_amount: float) -> tuple[bool, dict[str, Any]]:
        if not await self.supports(symbol):
            return False, {"error": f"{self.name} leg only hedges {self.symbol_supported}"}
        amount_raw = str(int(round(usd_amount * 10**USDC_DECIMALS)))
        ok, res = await self.brap.swap_from_token_ids(
            from_token_id=USDC_ETHEREUM_TOKEN_ID,
            to_token_id=self.token_id,
            from_address=self.wallet_address,
            amount=amount_raw,
            strategy_name=self.strategy_name,
        )
        if not ok:
            return False, {"error": str(res)}
        return True, {"result": res}

    async def close(self, symbol: str, units: float | None) -> tuple[bool, dict[str, Any]]:
        balance = await self._balance_lookup()
        target = balance if units is None else min(units, balance)
        if target <= 0:
            return True, {"units": 0.0, "note": "no balance"}
        amount_raw = str(int(target * 10**self.token_decimals))
        ok, res = await self.brap.swap_from_token_ids(
            from_token_id=self.token_id,
            to_token_id=USDC_ETHEREUM_TOKEN_ID,
            from_address=self.wallet_address,
            amount=amount_raw,
            strategy_name=self.strategy_name,
        )
        if not ok:
            return False, {"error": str(res)}
        return True, {"units": target, "result": res}

    async def position(self, symbol: str) -> SpotPosition | None:
        units = await self._balance_lookup()
        if units <= 0:
            return None
        usd_value: float | None = None
        if self._price_lookup is not None:
            price = await self._price_lookup(self.token_id)
            if price:
                usd_value = units * price
        return SpotPosition(self.name, symbol.upper(), units, usd_value, {"token_id": self.token_id})


class EtherfiLeg(BrapTokenLeg):
    """weETH long hedging an ETH short; earns ether.fi staking yield."""

    name = "etherfi"
    symbol_supported = "ETH"
    token_id = WEETH_TOKEN_ID
    token_decimals = WEETH_DECIMALS

    def __init__(
        self,
        brap_adapter: Any,
        wallet_address: str,
        *,
        balance_lookup: Callable[[], Awaitable[float]],
        yield_lookup: YieldLookup,
        price_lookup: PriceLookup | None = None,
    ) -> None:
        super().__init__(
            brap_adapter,
            wallet_address,
            balance_lookup=balance_lookup,
            price_lookup=price_lookup,
        )
        self._yield_lookup = yield_lookup

    async def yield_apy(self, symbol: str) -> float | None:
        if not await self.supports(symbol):
            return None
        return await self._yield_lookup("ETH")


class EthenaLeg(BrapTokenLeg):
    """sUSDe long hedging a USDE short; earns the Ethena vault APY."""

    name = "ethena"
    symbol_supported = "USDE"
    token_id = SUSDE_TOKEN_ID
    token_decimals = SUSDE_DECIMALS

    def __init__(
        self,
        brap_adapter: Any,
        wallet_address: str,
        *,
        ethena_adapter: Any,
        balance_lookup: Callable[[], Awaitable[float]],
        price_lookup: PriceLookup | None = None,
    ) -> None:
        super().__init__(
            brap_adapter,
            wallet_address,
            balance_lookup=balance_lookup,
            price_lookup=price_lookup,
        )
        self.ethena = ethena_adapter

    async def yield_apy(self, symbol: str) -> float | None:
        if not await self.supports(symbol):
            return None
        ok, apy = await self.ethena.get_apy()
        if not ok:
            logger.warning(f"ethena get_apy failed: {apy}")
            return None
        return float(apy)


# ---------------------------------------------------------------------------
# Paper mode: same interfaces, live reads, simulated fills + virtual balances
# ---------------------------------------------------------------------------

class PaperHedge(HedgeVenue):
    """Reads pass through to the live venue; fills and balances are virtual.

    Decision logic upstream is identical to live — only execution is swapped.
    `state` is the mutable `paper` sub-dict of the path state; the caller
    persists it after each action.
    """

    def __init__(self, live: HedgeVenue, state: dict[str, Any], *, slippage_bps: float) -> None:
        self.live = live
        self.name = live.name
        self.funding_interval_hours = live.funding_interval_hours
        self.state = state
        self.slippage_bps = slippage_bps
        state.setdefault("shorts", {})
        state.setdefault("usdc", 0.0)
        state.setdefault("realized_pnl_usd", 0.0)

    async def perp_snapshot(self) -> dict[str, dict[str, float]]:
        return await self.live.perp_snapshot()

    async def mark_price(self, symbol: str) -> float:
        return await self.live.mark_price(symbol)

    async def ensure_leverage(self, symbol: str, leverage: int) -> tuple[bool, str]:
        self.state.setdefault("leverage", {})[symbol.upper()] = int(leverage)
        return True, f"paper: {symbol} leverage {leverage}x"

    async def open_short(
        self, symbol: str, notional_usd: float, slippage: float
    ) -> tuple[bool, dict[str, Any]]:
        sym = symbol.upper()
        mark = await self.live.mark_price(sym)
        fill_px = mark * (1 - self.slippage_bps / 10_000)  # short sells into the bid
        leverage = int(self.state.get("leverage", {}).get(sym, 1))
        margin = notional_usd / max(leverage, 1)
        if self.state["usdc"] < margin:
            return False, {
                "error": f"paper: margin ${margin:.2f} exceeds virtual USDC ${self.state['usdc']:.2f}"
            }
        size = notional_usd / fill_px
        short = self.state["shorts"].setdefault(
            sym, {"size_units": 0.0, "entry_px": fill_px, "margin_usd": 0.0, "leverage": leverage}
        )
        prev = short["size_units"]
        short["entry_px"] = (
            (short["entry_px"] * prev + fill_px * size) / (prev + size) if prev + size > 0 else fill_px
        )
        short["size_units"] = prev + size
        short["margin_usd"] += margin
        self.state["usdc"] -= margin
        return True, {"size_units": size, "price": fill_px, "paper": True}

    async def close_short(
        self, symbol: str, size_units: float | None, slippage: float
    ) -> tuple[bool, dict[str, Any]]:
        sym = symbol.upper()
        short = self.state["shorts"].get(sym)
        if not short or short["size_units"] <= 0:
            return True, {"size_units": 0.0, "note": "paper: no short"}
        mark = await self.live.mark_price(sym)
        fill_px = mark * (1 + self.slippage_bps / 10_000)  # buy-back lifts the ask
        size = short["size_units"] if size_units is None else min(size_units, short["size_units"])
        fraction = size / short["size_units"]
        pnl = (short["entry_px"] - fill_px) * size
        released_margin = short["margin_usd"] * fraction
        short["size_units"] -= size
        short["margin_usd"] -= released_margin
        self.state["usdc"] += released_margin + pnl
        self.state["realized_pnl_usd"] += pnl
        if short["size_units"] <= 1e-12:
            self.state["shorts"].pop(sym, None)
        return True, {"size_units": size, "price": fill_px, "pnl_usd": pnl, "paper": True}

    async def short_position(self, symbol: str) -> HedgePosition | None:
        sym = symbol.upper()
        short = self.state["shorts"].get(sym)
        if not short or short["size_units"] <= 0:
            return None
        mark = await self.live.mark_price(sym)
        leverage = max(int(short.get("leverage", 1)), 1)
        # Synthetic liquidation price so the liq rail is exercised in paper.
        liq_px = short["entry_px"] * (1 + 0.9 / leverage)
        return HedgePosition(
            symbol=sym,
            size_units=short["size_units"],
            notional_usd=short["size_units"] * mark,
            entry_price=short["entry_px"],
            mark_price=mark,
            liq_price=liq_px,
            unrealized_pnl_usd=(short["entry_px"] - mark) * short["size_units"],
            margin_used_usd=short["margin_usd"],
        )

    async def free_margin_usd(self) -> float:
        return float(self.state["usdc"])


class PaperSpotLeg(SpotLeg):
    """Wraps a real leg for supports/yield; fills and balances are virtual."""

    def __init__(
        self,
        live: SpotLeg,
        state: dict[str, Any],
        *,
        slippage_bps: float,
        price_fn: Callable[[str], Awaitable[float | None]],
    ) -> None:
        self.live = live
        self.name = live.name
        self.state = state
        self.slippage_bps = slippage_bps
        self._price_fn = price_fn
        state.setdefault("spot", {})
        state.setdefault("usdc", 0.0)

    def _key(self, symbol: str) -> str:
        return f"{self.name}:{symbol.upper()}"

    async def supports(self, symbol: str) -> bool:
        return await self.live.supports(symbol)

    async def yield_apy(self, symbol: str) -> float | None:
        return await self.live.yield_apy(symbol)

    async def open(self, symbol: str, usd_amount: float) -> tuple[bool, dict[str, Any]]:
        if self.state["usdc"] < usd_amount:
            return False, {
                "error": f"paper: ${usd_amount:.2f} exceeds virtual USDC ${self.state['usdc']:.2f}"
            }
        price = await self._price_fn(symbol)
        fill_px = (price or 1.0) * (1 + self.slippage_bps / 10_000)
        units = usd_amount / fill_px
        pos = self.state["spot"].setdefault(
            self._key(symbol), {"units": 0.0, "entry_usd": 0.0}
        )
        pos["units"] += units
        pos["entry_usd"] += usd_amount
        self.state["usdc"] -= usd_amount
        return True, {"units": units, "price": fill_px, "paper": True}

    async def close(self, symbol: str, units: float | None) -> tuple[bool, dict[str, Any]]:
        pos = self.state["spot"].get(self._key(symbol))
        if not pos or pos["units"] <= 0:
            return True, {"units": 0.0, "note": "paper: no spot balance"}
        price = await self._price_fn(symbol)
        size = pos["units"] if units is None else min(units, pos["units"])
        fraction = size / pos["units"]
        if price is None:
            usd_out = pos["entry_usd"] * fraction  # no mark available: exit at entry value
        else:
            usd_out = size * price * (1 - self.slippage_bps / 10_000)
        pos["units"] -= size
        pos["entry_usd"] *= 1 - fraction
        self.state["usdc"] += usd_out
        if pos["units"] <= 1e-12:
            self.state["spot"].pop(self._key(symbol), None)
        return True, {"units": size, "usd_out": usd_out, "paper": True}

    async def position(self, symbol: str) -> SpotPosition | None:
        pos = self.state["spot"].get(self._key(symbol))
        if not pos or pos["units"] <= 0:
            return None
        price = await self._price_fn(symbol)
        usd_value = pos["units"] * price if price is not None else pos["entry_usd"]
        return SpotPosition(
            self.name, symbol.upper(), pos["units"], usd_value, {"paper": True}
        )


# ---------------------------------------------------------------------------
# Pair execution (hedge-first entry, hedge-last exit)
# ---------------------------------------------------------------------------

class PairExecutor:
    def __init__(self, hedge: HedgeVenue, spot_legs: dict[str, SpotLeg]) -> None:
        self.hedge = hedge
        self.spot_legs = spot_legs

    def _leg(self, name: str) -> SpotLeg:
        leg = self.spot_legs.get(name)
        if leg is None:
            raise RuntimeError(f"spot leg {name!r} not configured")
        return leg

    async def open_pair(
        self,
        symbol: str,
        spot_leg_name: str,
        notional_usd: float,
        *,
        leverage: int,
        slippage: float,
    ) -> tuple[bool, dict[str, Any]]:
        leg = self._leg(spot_leg_name)
        steps = open_pair_steps(self.hedge.name, spot_leg_name)
        report: dict[str, Any] = {"symbol": symbol, "spot_leg": spot_leg_name, "steps": []}

        ok_lev, lev_msg = await self.hedge.ensure_leverage(symbol, leverage)
        if not ok_lev:
            return False, {**report, "error": lev_msg}

        # Paper mode wraps the live venue, so atomic fills only apply when the
        # hedge really is the live HL venue; paper falls through to sequential.
        if steps == ["paired_atomic"] and isinstance(self.hedge, HyperliquidHedge):
            return await self._open_paired_atomic(symbol, notional_usd, report)

        ok_h, hedge_res = await self.hedge.open_short(symbol, notional_usd, slippage)
        report["steps"].append({"step": "hedge_short", "ok": ok_h, "result": hedge_res})
        if not ok_h:
            return False, {**report, "error": f"hedge open failed: {hedge_res.get('error')}"}

        hedged_notional = float(hedge_res.get("size_units", 0.0)) * float(
            hedge_res.get("price") or 0.0
        )
        ok_s, spot_res = await leg.open(symbol, hedged_notional or notional_usd)
        report["steps"].append({"step": "spot_open", "ok": ok_s, "result": spot_res})
        if not ok_s:
            # Never leave a silent unhedged short: halt loudly with state +
            # explicit next actions for the operator.
            report["error"] = f"spot leg failed after hedge opened: {spot_res.get('error')}"
            report["unhedged_short"] = hedge_res
            report["remediation"] = [
                f"close hedge: --action unwind --symbol {symbol}",
                f"or retry spot leg: --action deposit --symbol {symbol} (spot only)",
            ]
            return False, report
        return True, report

    async def _open_paired_atomic(
        self, symbol: str, notional_usd: float, report: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        hedge = self.hedge
        assert isinstance(hedge, HyperliquidHedge)
        leg = self._leg("hl_spot")
        assert isinstance(leg, HlSpotLeg)
        sym = symbol.upper()
        spot_id = await leg._spot_asset_id(sym)
        if spot_id is None:
            return False, {**report, "error": f"no HL spot pair for {sym}"}
        price = await hedge.mark_price(sym)
        filler = PairedFiller(
            adapter=hedge.adapter,
            address=hedge.address,
            cfg=FillConfig(max_slip_bps=100),
        )
        try:
            filled_spot, filled_perp, spot_notional, perp_notional, *_pointers = (
                await filler.fill_pair_units(
                    coin=hl_spot_coin(sym),
                    spot_asset_id=spot_id,
                    perp_asset_id=hedge.perp_asset_id(sym),
                    total_units=notional_usd / price,
                    direction="long_spot_short_perp",
                    builder_fee=hedge.builder_fee,
                )
            )
        except Exception as exc:
            return False, {**report, "error": f"paired fill failed: {exc}"}
        report["steps"].append(
            {
                "step": "paired_atomic",
                "ok": True,
                "result": {
                    "filled_spot": filled_spot,
                    "filled_perp": filled_perp,
                    "spot_notional_usd": spot_notional,
                    "perp_notional_usd": perp_notional,
                },
            }
        )
        return True, report

    async def close_pair(
        self, symbol: str, spot_leg_name: str, *, slippage: float
    ) -> tuple[bool, dict[str, Any]]:
        leg = self._leg(spot_leg_name)
        report: dict[str, Any] = {"symbol": symbol, "spot_leg": spot_leg_name, "steps": []}

        ok_s, spot_res = await leg.close(symbol, None)
        report["steps"].append({"step": "spot_close", "ok": ok_s, "result": spot_res})
        if not ok_s:
            report["error"] = f"spot close failed: {spot_res.get('error')} — hedge left open intentionally"
            return False, report

        ok_h, hedge_res = await self.hedge.close_short(symbol, None, slippage)
        report["steps"].append({"step": "hedge_close", "ok": ok_h, "result": hedge_res})
        if not ok_h:
            report["error"] = f"hedge close failed: {hedge_res.get('error')}"
            return False, report
        return True, report
