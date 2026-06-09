"""
Per-venue read + write wiring for the stablecoin-yield-rotator path.

Each venue exposes:
- `scan(chain_id, asset_symbols) -> list[VenueRow]` — read-only ranked candidates
- `positions(chain_id, asset_symbols, account) -> list[Position]` — current supply per (asset, market)
- `lend(chain_id, market_id, raw_amount) -> tuple[bool, dict]` — supply
- `unlend(chain_id, market_id, raw_amount, withdraw_full) -> tuple[bool, dict]` — withdraw

`market_id` semantics differ per venue:
- aave_v3 / sparklend / hyperlend: underlying token address
- morpho_blue_market: market unique key (bytes32 hex)
- euler_v2: vault address (also the share token)
- moonwell: mToken address (underlying lives in asset_address)
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.adapters.hyperlend_adapter.adapter import HyperlendAdapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter
from wayfinder_paths.core.constants.erc4626_abi import ERC4626_ABI
from wayfinder_paths.core.utils.symbols import normalize_symbol
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.scripting import get_adapter

VENUE_CHAIN_SUPPORT: dict[str, set[int]] = {
    "aave_v3": {1, 8453, 42161},
    "morpho_blue_market": {1, 8453},
    "morpho_vault": {1, 8453},
    "sparklend": {1},
    "euler_v2": {1, 8453, 42161},
    "hyperlend": {999},
    "moonwell": {8453},
}

# Venues whose adapter exposes lend/unlend in this repo. SparkLend is currently
# read-only via the path because SparkLendAdapter has no supply/withdraw methods.
EXECUTABLE_VENUES: set[str] = {"aave_v3", "morpho_blue_market", "morpho_vault", "euler_v2", "hyperlend", "moonwell"}

ALLOWED_STABLES = {"USDC", "USDT", "DAI"}

# Moonwell mToken exchange-rate scaling (matches the adapter's own 1e18 convention).
MANTISSA = 10**18


@dataclass
class VenueRow:
    venue: str
    chain_id: int
    asset_symbol: str
    asset_address: str
    market_id: str
    decimals: int
    supply_apy: float
    utilization: float | None
    supply_cap_headroom_raw: int | None
    tvl_usd: float | None
    is_frozen: bool = False
    is_paused: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    venue: str
    chain_id: int
    asset_symbol: str
    asset_address: str
    market_id: str
    decimals: int
    supply_raw: int
    supply_usd: float | None


def _matches_asset(symbol: str | None, allowed: set[str]) -> bool:
    if not symbol:
        return False
    canonical = normalize_symbol(symbol) or symbol.upper()
    if canonical in allowed:
        return True
    return symbol.upper() in allowed


def _asset_symbol(symbol: str | None) -> str:
    canonical = normalize_symbol(symbol)
    return canonical.upper() if canonical else str(symbol or "").upper()


def _moonwell_underlying_symbol(mtoken_symbol: str | None) -> str:
    # Moonwell market symbols are the mToken symbol (e.g. "mUSDC"); strip the
    # leading "m" to recover the underlying symbol for asset matching.
    s = str(mtoken_symbol or "")
    return s[1:] if s[:1] == "m" else s


def _venue_supports(venue: str, chain_id: int) -> bool:
    return chain_id in VENUE_CHAIN_SUPPORT.get(venue, set())


# ---------------------------------------------------------------------------
# Aave V3
# ---------------------------------------------------------------------------

async def _scan_aave_v3(adapter: AaveV3Adapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    ok, markets = await adapter.get_all_markets(chain_id=chain_id, include_rewards=False)
    if not ok:
        raise RuntimeError(f"aave_v3 get_all_markets failed on chain {chain_id}: {markets}")
    rows: list[VenueRow] = []
    for m in markets:
        if not _matches_asset(m.get("symbol"), allowed):
            continue
        if not m.get("is_active") or m.get("is_paused"):
            continue
        tvl_raw = int(m.get("tvl") or 0)
        debt_raw = int(m.get("total_variable_debt") or 0)
        utilization = (debt_raw / tvl_raw) if tvl_raw else 0.0
        price_usd = float(m.get("price_usd") or 0.0)
        decimals = int(m.get("decimals") or 6)
        tvl_usd = (tvl_raw / (10**decimals)) * price_usd if price_usd else None
        rows.append(VenueRow(
            venue="aave_v3",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(m.get("symbol")),
            asset_address=str(m.get("underlying")),
            market_id=str(m.get("underlying")),
            decimals=decimals,
            supply_apy=float(m.get("supply_apy") or 0.0),
            utilization=utilization,
            supply_cap_headroom_raw=m.get("supply_cap_headroom"),
            tvl_usd=tvl_usd,
            is_frozen=bool(m.get("is_frozen")),
            is_paused=bool(m.get("is_paused")),
            extra={"a_token": m.get("a_token")},
        ))
    return rows


async def _aave_positions(adapter: AaveV3Adapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    ok, state = await adapter.get_full_user_state_per_chain(
        chain_id=chain_id, account=account, include_rewards=False, include_zero_positions=False
    )
    if not ok:
        raise RuntimeError(f"aave_v3 user state failed on chain {chain_id}: {state}")
    positions: list[Position] = []
    for p in state.get("positions") or []:
        if not _matches_asset(p.get("symbol"), allowed):
            continue
        supply_raw = int(p.get("supply_raw") or 0)
        if supply_raw <= 0:
            continue
        positions.append(Position(
            venue="aave_v3",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(p.get("symbol")),
            asset_address=str(p.get("underlying") or p.get("underlying_asset") or ""),
            market_id=str(p.get("underlying") or p.get("underlying_asset") or ""),
            decimals=int(p.get("decimals") or 6),
            supply_raw=supply_raw,
            supply_usd=float(p.get("supply_usd")) if p.get("supply_usd") is not None else None,
        ))
    return positions


# ---------------------------------------------------------------------------
# Morpho (Blue markets — single-asset supply via `lend(market_unique_key=...)`)
# ---------------------------------------------------------------------------

async def _scan_morpho(adapter: MorphoAdapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    ok, markets = await adapter.get_all_markets(chain_id=chain_id)
    if not ok:
        raise RuntimeError(f"morpho_blue_market get_all_markets failed on chain {chain_id}: {markets}")
    rows: list[VenueRow] = []
    for m in markets:
        loan = m.get("loan") or {}
        state = m.get("state") or {}
        if not _matches_asset(loan.get("symbol"), allowed):
            continue
        if not m.get("listed"):
            continue
        warning_values = {str(w).lower() for w in (m.get("warnings") or [])}
        if warning_values & {"paused", "frozen"}:
            continue
        supply_apy = float(state.get("supply_apy") or 0.0)
        utilization = float(state.get("utilization") or 0.0) if state.get("utilization") is not None else None
        decimals = int(loan.get("decimals") or 6)
        tvl_usd = state.get("supply_assets_usd")
        rows.append(VenueRow(
            venue="morpho_blue_market",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(loan.get("symbol")),
            asset_address=str(loan.get("address")),
            market_id=str(m.get("uniqueKey")),
            decimals=decimals,
            supply_apy=supply_apy,
            utilization=utilization,
            supply_cap_headroom_raw=int(state.get("liquidity_assets") or 0) or None,
            tvl_usd=float(tvl_usd) if tvl_usd is not None else None,
            extra={"collateral_symbol": (m.get("collateral") or {}).get("symbol")},
        ))
    return rows


async def _morpho_positions(adapter: MorphoAdapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    ok, state = await adapter.get_full_user_state_per_chain(chain_id=chain_id, account=account)
    if not ok:
        raise RuntimeError(f"morpho_blue_market user state failed on chain {chain_id}: {state}")
    positions: list[Position] = []
    for p in state.get("positions") or []:
        loan = p.get("loan") or {}
        if not _matches_asset(loan.get("symbol"), allowed):
            continue
        supply_raw = int(p.get("supply_raw") or p.get("supplyAssets") or 0)
        if supply_raw <= 0:
            continue
        positions.append(Position(
            venue="morpho_blue_market",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(loan.get("symbol")),
            asset_address=str(loan.get("address") or ""),
            market_id=str(p.get("marketUniqueKey") or p.get("uniqueKey") or ""),
            decimals=int(loan.get("decimals") or 6),
            supply_raw=supply_raw,
            supply_usd=float(p.get("supply_usd")) if p.get("supply_usd") is not None else None,
        ))
    return positions


# ---------------------------------------------------------------------------
# Morpho Vaults / MetaMorpho (ERC-4626 vault shares)
# ---------------------------------------------------------------------------

def _vault_apy(vault: dict[str, Any]) -> float:
    state = vault.get("state") or {}
    for key in ("net_apy", "apy", "apy_with_rewards"):
        value = state.get(key)
        if value is not None:
            return float(value or 0.0)
    return 0.0


async def _scan_morpho_vault(adapter: MorphoAdapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    ok, vaults = await adapter.get_all_vaults(chain_id=chain_id, listed=True, include_v2=True)
    if not ok:
        raise RuntimeError(f"morpho_vault get_all_vaults failed on chain {chain_id}: {vaults}")
    rows: list[VenueRow] = []
    for v in vaults:
        asset = v.get("asset") or {}
        state = v.get("state") or {}
        if not _matches_asset(asset.get("symbol"), allowed):
            continue
        if not v.get("listed"):
            continue
        warning_values = {str(w).lower() for w in (v.get("warnings") or [])}
        if warning_values & {"paused", "frozen"}:
            continue
        decimals = int(asset.get("decimals") or 6)
        total_assets_usd = state.get("total_assets_usd")
        rows.append(VenueRow(
            venue="morpho_vault",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(asset.get("symbol")),
            asset_address=str(asset.get("address") or ""),
            market_id=str(v.get("address")),
            decimals=decimals,
            supply_apy=_vault_apy(v),
            utilization=None,
            supply_cap_headroom_raw=None,
            tvl_usd=float(total_assets_usd) if total_assets_usd is not None else None,
            extra={
                "name": v.get("name"),
                "symbol": v.get("symbol"),
                "version": v.get("version"),
            },
        ))
    return rows


async def _morpho_vault_positions(adapter: MorphoAdapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    rows = await _scan_morpho_vault(adapter, chain_id, allowed)
    positions: list[Position] = []
    async with web3_from_chain_id(chain_id) as web3:
        acct = web3.to_checksum_address(account)
        sem = asyncio.Semaphore(12)

        async def _position(row: VenueRow) -> Position | None:
            vault = web3.to_checksum_address(row.market_id)
            contract = web3.eth.contract(address=vault, abi=ERC4626_ABI)
            async with sem:
                shares = int(await contract.functions.balanceOf(acct).call(block_identifier="latest") or 0)
                if shares <= 0:
                    return None
                assets = int(await contract.functions.convertToAssets(shares).call(block_identifier="latest") or 0)
                if assets <= 0:
                    return None
                return Position(
                    venue="morpho_vault",
                    chain_id=chain_id,
                    asset_symbol=row.asset_symbol,
                    asset_address=row.asset_address,
                    market_id=row.market_id,
                    decimals=row.decimals,
                    supply_raw=assets,
                    supply_usd=assets / (10**row.decimals),
                )

        found = await asyncio.gather(*[_position(row) for row in rows])
    positions.extend(p for p in found if p is not None)
    return positions


# ---------------------------------------------------------------------------
# SparkLend (Ethereum mainnet only)
# ---------------------------------------------------------------------------

async def _scan_sparklend(adapter: SparkLendAdapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    ok, markets = await adapter.get_all_markets(chain_id=chain_id, include_caps=True)
    if not ok:
        raise RuntimeError(f"sparklend get_all_markets failed on chain {chain_id}: {markets}")
    rows: list[VenueRow] = []
    for m in markets:
        if not _matches_asset(m.get("symbol"), allowed):
            continue
        if m.get("is_frozen") or m.get("is_paused"):
            continue
        tvl_raw = int(m.get("tvl") or 0)
        debt_raw = int(m.get("total_variable_debt") or 0)
        utilization = (debt_raw / tvl_raw) if tvl_raw else 0.0
        price_usd = float(m.get("price_usd") or 0.0)
        decimals = int(m.get("decimals") or 6)
        tvl_usd = (tvl_raw / (10**decimals)) * price_usd if price_usd else None
        rows.append(VenueRow(
            venue="sparklend",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(m.get("symbol")),
            asset_address=str(m.get("underlying")),
            market_id=str(m.get("underlying")),
            decimals=decimals,
            supply_apy=float(m.get("supply_apy") or 0.0),
            utilization=utilization,
            supply_cap_headroom_raw=m.get("supply_cap_headroom"),
            tvl_usd=tvl_usd,
            is_frozen=bool(m.get("is_frozen")),
            is_paused=bool(m.get("is_paused")),
        ))
    return rows


async def _sparklend_positions(adapter: SparkLendAdapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    ok, state = await adapter.get_full_user_state(chain_id=chain_id, account=account)
    if not ok:
        raise RuntimeError(f"sparklend user state failed on chain {chain_id}: {state}")
    positions: list[Position] = []
    for p in state.get("positions") or []:
        if not _matches_asset(p.get("symbol"), allowed):
            continue
        supply_raw = int(p.get("supply_raw") or 0)
        if supply_raw <= 0:
            continue
        positions.append(Position(
            venue="sparklend",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(p.get("symbol")),
            asset_address=str(p.get("underlying") or ""),
            market_id=str(p.get("underlying") or ""),
            decimals=int(p.get("decimals") or 6),
            supply_raw=supply_raw,
            supply_usd=None,
        ))
    return positions


# ---------------------------------------------------------------------------
# Euler V2
# ---------------------------------------------------------------------------

async def _scan_euler_v2(adapter: EulerV2Adapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    ok, markets = await adapter.get_all_markets(chain_id=chain_id, perspective="governed", concurrency=8)
    if not ok:
        raise RuntimeError(f"euler_v2 get_all_markets failed on chain {chain_id}: {markets}")
    rows: list[VenueRow] = []
    for m in markets:
        if not _matches_asset(m.get("asset_symbol"), allowed):
            continue
        cash = int(m.get("cash") or 0)
        total_borrowed = int(m.get("total_borrowed") or m.get("total_borrows") or 0)
        total = cash + total_borrowed
        utilization = (total_borrowed / total) if total else 0.0
        decimals = int(m.get("asset_decimals") or 6)
        tvl_usd = total / (10**decimals)
        rows.append(VenueRow(
            venue="euler_v2",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(m.get("asset_symbol")),
            asset_address=str(m.get("asset") or m.get("underlying") or ""),
            market_id=str(m.get("vault")),
            decimals=decimals,
            supply_apy=float(m.get("supply_apy") or 0.0),
            utilization=utilization,
            supply_cap_headroom_raw=cash or None,
            tvl_usd=tvl_usd,
            extra={"vault": m.get("vault")},
        ))
    return rows


async def _euler_positions(adapter: EulerV2Adapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    ok_markets, markets_payload = await adapter.get_all_markets(chain_id=chain_id, perspective="governed", concurrency=8)
    if not ok_markets:
        raise RuntimeError(f"euler_v2 markets failed on chain {chain_id}: {markets_payload}")
    market_by_vault: dict[str, dict[str, Any]] = {}
    for market in markets_payload:
        if not _matches_asset(market.get("asset_symbol"), allowed):
            continue
        vault = str(market.get("vault") or "").lower()
        if vault:
            market_by_vault[vault] = market

    ok, state = await adapter.get_full_user_state(chain_id=chain_id, account=account, include_zero_positions=False)
    if not ok:
        raise RuntimeError(f"euler_v2 user state failed on chain {chain_id}: {state}")
    positions: list[Position] = []
    seen_vaults: set[str] = set()
    for p in state.get("positions") or []:
        vault = str(p.get("vault") or "").lower()
        market = market_by_vault.get(vault)
        if market is None:
            continue
        supply_raw = int(p.get("assets") or 0)
        if supply_raw <= 0:
            continue
        seen_vaults.add(vault)
        positions.append(Position(
            venue="euler_v2",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(market.get("asset_symbol")),
            asset_address=str(market.get("asset") or market.get("underlying") or p.get("asset") or p.get("underlying") or ""),
            market_id=str(market.get("vault") or p.get("vault") or ""),
            decimals=int(market.get("asset_decimals") or 6),
            supply_raw=supply_raw,
            supply_usd=None,
        ))

    # AccountLens only reports EVC-enabled vaults. Plain supply-only eVault deposits
    # can hold shares without being enabled as collateral/controller, so scan the
    # verified stable vault share balances as a fallback.
    async with web3_from_chain_id(chain_id) as web3:
        acct = web3.to_checksum_address(account)
        sem = asyncio.Semaphore(12)

        async def _share_position(vault_key: str, market: dict[str, Any]) -> Position | None:
            if vault_key in seen_vaults:
                return None
            vault_addr = web3.to_checksum_address(str(market.get("vault")))
            contract = web3.eth.contract(address=vault_addr, abi=ERC4626_ABI)

            async with sem:
                shares = int(await contract.functions.balanceOf(acct).call(block_identifier="latest") or 0)
                if shares <= 0:
                    return None
                assets = int(await contract.functions.convertToAssets(shares).call(block_identifier="latest") or 0)
                if assets <= 0:
                    return None
                decimals = int(market.get("asset_decimals") or 6)
                return Position(
                    venue="euler_v2",
                    chain_id=chain_id,
                    asset_symbol=_asset_symbol(market.get("asset_symbol")),
                    asset_address=str(market.get("asset") or market.get("underlying") or ""),
                    market_id=str(market.get("vault") or ""),
                    decimals=decimals,
                    supply_raw=assets,
                    supply_usd=assets / (10**decimals),
                )

        fallback_positions = await asyncio.gather(
            *[_share_position(vault_key, market) for vault_key, market in market_by_vault.items()]
        )
        positions.extend(p for p in fallback_positions if p is not None)
    return positions


# ---------------------------------------------------------------------------
# Hyperlend (HyperEVM only)
# ---------------------------------------------------------------------------

async def _scan_hyperlend(adapter: HyperlendAdapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    # Hyperlend is HyperEVM-only; the adapter ignores chain_id and reads from a fixed pool.
    ok, markets = await adapter.get_all_markets()
    if not ok:
        raise RuntimeError(f"hyperlend get_all_markets failed: {markets}")
    rows: list[VenueRow] = []
    for m in markets:
        if not _matches_asset(m.get("symbol"), allowed):
            continue
        if m.get("is_frozen") or m.get("is_paused"):
            continue
        tvl_raw = int(m.get("tvl") or 0)
        debt_raw = int(m.get("total_variable_debt") or 0)
        utilization = (debt_raw / tvl_raw) if tvl_raw else 0.0
        price_usd = float(m.get("price_usd") or 0.0)
        decimals = int(m.get("decimals") or 6)
        tvl_usd = (tvl_raw / (10**decimals)) * price_usd if price_usd else None
        rows.append(VenueRow(
            venue="hyperlend",
            chain_id=chain_id,
            asset_symbol=_asset_symbol(m.get("symbol")),
            asset_address=str(m.get("underlying")),
            market_id=str(m.get("underlying")),
            decimals=decimals,
            supply_apy=float(m.get("supply_apy") or 0.0),
            utilization=utilization,
            supply_cap_headroom_raw=m.get("supply_cap_headroom"),
            tvl_usd=tvl_usd,
        ))
    return rows


async def _hyperlend_positions(adapter: HyperlendAdapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    # Hyperlend chain_id is fixed at HyperEVM (999); the param is here for the dispatcher contract.
    ok, state = await adapter.get_full_user_state(account=account, include_zero_positions=False)
    if not ok:
        raise RuntimeError(f"hyperlend user state failed: {state}")
    positions: list[Position] = []
    for p in state.get("positions") or []:
        if not _matches_asset(p.get("symbol"), allowed):
            continue
        # AssetInfo exposes `supply` (human float) and `decimals`; convert to raw.
        decimals = int(p.get("decimals") or 6)
        supply_human = float(p.get("supply") or 0.0)
        supply_raw = int(round(supply_human * (10 ** decimals)))
        if supply_raw <= 0:
            continue
        underlying = str(p.get("underlying") or "")
        positions.append(Position(
            venue="hyperlend",
            chain_id=999,
            asset_symbol=_asset_symbol(p.get("symbol")),
            asset_address=underlying,
            market_id=underlying,
            decimals=decimals,
            supply_raw=supply_raw,
            supply_usd=float(p.get("supply_usd")) if p.get("supply_usd") is not None else None,
        ))
    return positions


# ---------------------------------------------------------------------------
# Moonwell (Compound-fork mTokens on Base — supply via mint, withdraw via redeem)
# market_id is the mToken address; the underlying lives in asset_address.
# ---------------------------------------------------------------------------

async def _scan_moonwell(adapter: MoonwellAdapter, chain_id: int, allowed: set[str]) -> list[VenueRow]:
    ok, markets = await adapter.get_all_markets(include_apy=True, include_rewards=False, include_usd=True)
    if not ok or not isinstance(markets, list):
        raise RuntimeError(f"moonwell get_all_markets failed on chain {chain_id}: {markets}")
    rows: list[VenueRow] = []
    for m in markets:
        underlying_symbol = _moonwell_underlying_symbol(m.get("symbol"))
        if not _matches_asset(underlying_symbol, allowed):
            continue
        if m.get("mintPaused") or not m.get("isListed", True):
            continue
        symbol = _asset_symbol(underlying_symbol)
        # Underlying decimals (mTokenDecimals is 8 and not what callers need).
        decimals = 18 if symbol == "DAI" else 6
        cash = int(m.get("cash") or 0)
        borrows = int(m.get("totalBorrows") or 0)
        reserves = int(m.get("totalReserves") or 0)
        denom = cash + borrows - reserves
        utilization = (borrows / denom) if denom > 0 else 0.0
        exchange_rate = int(m.get("exchangeRate") or 0)
        supply_underlying_raw = (int(m.get("totalSupply") or 0) * exchange_rate) // MANTISSA if exchange_rate else 0
        supply_cap = int(m.get("supplyCap") or 0)
        headroom = (supply_cap - supply_underlying_raw) if supply_cap > 0 else None
        tvl_usd = m.get("totalSupplyUsd")
        rows.append(VenueRow(
            venue="moonwell",
            chain_id=chain_id,
            asset_symbol=symbol,
            asset_address=str(m.get("underlying") or ""),
            market_id=str(m.get("mtoken")),
            decimals=decimals,
            supply_apy=float(m.get("supplyApy") or 0.0),
            utilization=utilization,
            supply_cap_headroom_raw=headroom,
            tvl_usd=float(tvl_usd) if tvl_usd is not None else None,
            is_frozen=False,
            is_paused=bool(m.get("mintPaused")),
            extra={"mtoken": m.get("mtoken")},
        ))
    # Moonwell still lists the legacy USDbC market under the symbol "mUSDC", so two
    # markets can map to the same stable. Freeze all but the deepest-TVL one per stable
    # (native USDC over legacy USDbC) so the duplicate is excluded as a rotation *target*.
    # Don't drop it: a holder of the legacy market must stay visible in the scan so the
    # rotator can move them *out* — a missing market trips DiscoveryGapError and halts.
    best_by_symbol: dict[str, VenueRow] = {}
    for row in rows:
        cur = best_by_symbol.get(row.asset_symbol)
        if cur is None or (row.tvl_usd or 0.0) > (cur.tvl_usd or 0.0):
            best_by_symbol[row.asset_symbol] = row
    for row in rows:
        if row is not best_by_symbol[row.asset_symbol]:
            row.is_frozen = True
            row.extra["frozen_reason"] = "duplicate Moonwell market for this stable (lower TVL)"
    return rows


async def _moonwell_positions(adapter: MoonwellAdapter, chain_id: int, allowed: set[str], account: str) -> list[Position]:
    ok, markets = await adapter.get_all_markets(include_apy=False, include_rewards=False, include_usd=False)
    if not ok or not isinstance(markets, list):
        raise RuntimeError(f"moonwell get_all_markets failed on chain {chain_id}: {markets}")
    positions: list[Position] = []
    for m in markets:
        underlying_symbol = _moonwell_underlying_symbol(m.get("symbol"))
        if not _matches_asset(underlying_symbol, allowed):
            continue
        mtoken = str(m.get("mtoken"))
        ok_pos, pos = await adapter.get_pos(mtoken=mtoken, account=account)
        if not ok_pos or not isinstance(pos, dict):
            continue
        supply_raw = int(pos.get("underlying_balance") or 0)
        if supply_raw <= 0:
            continue
        symbol = _asset_symbol(underlying_symbol)
        positions.append(Position(
            venue="moonwell",
            chain_id=chain_id,
            asset_symbol=symbol,
            asset_address=str(m.get("underlying") or ""),
            market_id=mtoken,
            decimals=18 if symbol == "DAI" else 6,
            supply_raw=supply_raw,
            supply_usd=None,
        ))
    return positions


# ---------------------------------------------------------------------------
# Adapter factories
# ---------------------------------------------------------------------------

async def get_read_adapter(venue: str) -> Any:
    if venue == "aave_v3":
        return await get_adapter(AaveV3Adapter)
    if venue == "morpho_blue_market":
        return await get_adapter(MorphoAdapter)
    if venue == "morpho_vault":
        return await get_adapter(MorphoAdapter)
    if venue == "sparklend":
        return await get_adapter(SparkLendAdapter)
    if venue == "euler_v2":
        return await get_adapter(EulerV2Adapter)
    if venue == "hyperlend":
        return await get_adapter(HyperlendAdapter)
    if venue == "moonwell":
        return await get_adapter(MoonwellAdapter)
    raise ValueError(f"unknown venue: {venue}")


async def get_write_adapter(venue: str, wallet_label: str) -> Any:
    """Return a write-enabled adapter for `venue` signed by `wallet_label`."""
    if venue == "euler_v2":
        sign_cb, addr = await get_wallet_signing_callback(wallet_label)
        return await get_adapter(
            EulerV2Adapter,
            config_overrides={"strategy_wallet": {"address": addr}},
            strategy_wallet_signing_callback=sign_cb,
        )
    if venue == "aave_v3":
        return await get_adapter(AaveV3Adapter, wallet_label)
    if venue == "morpho_blue_market":
        return await get_adapter(MorphoAdapter, wallet_label)
    if venue == "morpho_vault":
        return await get_adapter(MorphoAdapter, wallet_label)
    if venue == "sparklend":
        return await get_adapter(SparkLendAdapter, wallet_label)
    if venue == "hyperlend":
        return await get_adapter(HyperlendAdapter, wallet_label)
    if venue == "moonwell":
        return await get_adapter(MoonwellAdapter, wallet_label)
    raise ValueError(f"unknown venue: {venue}")


# ---------------------------------------------------------------------------
# Top-level scan / positions / lend / unlend dispatchers
# ---------------------------------------------------------------------------

_SCAN_FNS = {
    "aave_v3": _scan_aave_v3,
    "morpho_blue_market": _scan_morpho,
    "morpho_vault": _scan_morpho_vault,
    "sparklend": _scan_sparklend,
    "euler_v2": _scan_euler_v2,
    "hyperlend": _scan_hyperlend,
    "moonwell": _scan_moonwell,
}

_POSITION_FNS = {
    "aave_v3": _aave_positions,
    "morpho_blue_market": _morpho_positions,
    "morpho_vault": _morpho_vault_positions,
    "sparklend": _sparklend_positions,
    "euler_v2": _euler_positions,
    "hyperlend": _hyperlend_positions,
    "moonwell": _moonwell_positions,
}


class DiscoveryError(RuntimeError):
    """Raised when a (venue, chain) read fails in strict mode."""


async def scan_all(
    venues: list[str],
    chains: list[int],
    assets: list[str],
    *,
    strict: bool = True,
    failure_log: list[dict[str, Any]] | None = None,
) -> list[VenueRow]:
    """Run scans for every (venue, chain) tuple in parallel, returning a flat list of rows.

    `strict=True` (default) raises DiscoveryError on the first failure so fund-moving
    callers don't operate on partial data. Set `strict=False` for read-only UX where
    partial results are acceptable; pass `failure_log=[]` to capture errors so callers
    can surface them in the JSON response.
    """
    allowed = {a.upper() for a in assets} & ALLOWED_STABLES
    if not allowed:
        raise ValueError(f"no allowed stablecoins in {assets}; supported: {ALLOWED_STABLES}")

    tasks: list[tuple[str, int, asyncio.Task[list[VenueRow]]]] = []
    for venue in venues:
        if venue not in _SCAN_FNS:
            if strict:
                raise DiscoveryError(f"unknown venue {venue}")
            logger.warning(f"unknown venue {venue}, skipping")
            if failure_log is not None:
                failure_log.append({"venue": venue, "chain_id": None, "error": "unknown venue"})
            continue
        adapter = await get_read_adapter(venue)
        for chain_id in chains:
            if not _venue_supports(venue, chain_id):
                continue
            tasks.append((venue, chain_id, asyncio.create_task(_SCAN_FNS[venue](adapter, chain_id, allowed))))

    rows: list[VenueRow] = []
    failures: list[dict[str, Any]] = []
    for venue, chain_id, task in tasks:
        try:
            rows.extend(await task)
        except Exception as exc:  # noqa: BLE001
            failures.append({"venue": venue, "chain_id": chain_id, "error": str(exc)})
            logger.warning(f"scan failed for {venue}/{chain_id}: {exc}")
    if failures and strict:
        raise DiscoveryError(f"scan failures: {failures}")
    if failure_log is not None:
        failure_log.extend(failures)
    return rows


async def positions_all(
    venues: list[str],
    chains: list[int],
    assets: list[str],
    account: str,
    *,
    strict: bool = True,
    failure_log: list[dict[str, Any]] | None = None,
) -> list[Position]:
    """Aggregate user positions across (venue, chain). Strict by default. See scan_all for `failure_log`."""
    allowed = {a.upper() for a in assets} & ALLOWED_STABLES
    tasks: list[tuple[str, int, asyncio.Task[list[Position]]]] = []
    for venue in venues:
        fn = _POSITION_FNS.get(venue)
        if fn is None:
            if strict:
                raise DiscoveryError(f"no position function for venue {venue}")
            if failure_log is not None:
                failure_log.append({"venue": venue, "chain_id": None, "error": "no position function"})
            continue
        adapter = await get_read_adapter(venue)
        for chain_id in chains:
            if not _venue_supports(venue, chain_id):
                continue
            tasks.append((venue, chain_id, asyncio.create_task(fn(adapter, chain_id, allowed, account))))

    out: list[Position] = []
    failures: list[dict[str, Any]] = []
    for venue, chain_id, task in tasks:
        try:
            out.extend(await task)
        except Exception as exc:  # noqa: BLE001
            failures.append({"venue": venue, "chain_id": chain_id, "error": str(exc)})
            logger.warning(f"positions failed for {venue}/{chain_id}: {exc}")
    if failures and strict:
        raise DiscoveryError(f"positions failures: {failures}")
    if failure_log is not None:
        failure_log.extend(failures)
    return out


async def lend(venue: str, wallet_label: str, chain_id: int, market_id: str, raw_amount: int) -> tuple[bool, Any]:
    """Supply raw_amount of underlying to (venue, chain, market_id). Returns (ok, tx_payload)."""
    if venue not in EXECUTABLE_VENUES:
        raise NotImplementedError(
            f"venue {venue!r} is not executable in this path "
            f"(no lend/unlend on its adapter). Executable venues: {sorted(EXECUTABLE_VENUES)}"
        )
    adapter = await get_write_adapter(venue, wallet_label)
    if venue == "aave_v3":
        return await adapter.lend(chain_id=chain_id, underlying_token=market_id, qty=raw_amount)
    if venue == "morpho_blue_market":
        return await adapter.lend(chain_id=chain_id, market_unique_key=market_id, qty=raw_amount)
    if venue == "morpho_vault":
        return await adapter.vault_deposit(chain_id=chain_id, vault_address=market_id, assets=raw_amount)
    if venue == "sparklend":
        return await adapter.lend(chain_id=chain_id, underlying_token=market_id, qty=raw_amount)
    if venue == "euler_v2":
        return await adapter.lend(chain_id=chain_id, vault=market_id, amount=raw_amount)
    if venue == "hyperlend":
        return await adapter.lend(underlying_token=market_id, qty=raw_amount, chain_id=chain_id)
    if venue == "moonwell":
        # market_id is the mToken; lend() needs the underlying for the approval.
        ok_pos, pos = await adapter.get_pos(mtoken=market_id)
        if not ok_pos or not isinstance(pos, dict):
            return False, {"error": f"moonwell underlying lookup failed at {market_id}: {pos}"}
        return await adapter.lend(mtoken=market_id, underlying_token=str(pos["underlying_token"]), amount=raw_amount)
    raise ValueError(f"unknown venue: {venue}")


async def unlend(venue: str, wallet_label: str, chain_id: int, market_id: str, raw_amount: int, withdraw_full: bool = False) -> tuple[bool, Any]:
    """Withdraw raw_amount (or full) from (venue, chain, market_id). Returns (ok, tx_payload)."""
    if venue not in EXECUTABLE_VENUES:
        raise NotImplementedError(
            f"venue {venue!r} is not executable in this path "
            f"(no lend/unlend on its adapter). Executable venues: {sorted(EXECUTABLE_VENUES)}"
        )
    adapter = await get_write_adapter(venue, wallet_label)
    if venue == "aave_v3":
        return await adapter.unlend(chain_id=chain_id, underlying_token=market_id, qty=raw_amount, withdraw_full=withdraw_full)
    if venue == "morpho_blue_market":
        return await adapter.unlend(chain_id=chain_id, market_unique_key=market_id, qty=raw_amount, withdraw_full=withdraw_full)
    if venue == "morpho_vault":
        if withdraw_full:
            account = _wallet_address_for_unlend(adapter)
            async with web3_from_chain_id(chain_id) as web3:
                contract = web3.eth.contract(address=web3.to_checksum_address(market_id), abi=ERC4626_ABI)
                shares = int(await contract.functions.balanceOf(web3.to_checksum_address(account)).call(block_identifier="latest") or 0)
            if shares <= 0:
                return False, {"error": f"no Morpho vault shares to redeem at {market_id}"}
            return await adapter.vault_redeem(chain_id=chain_id, vault_address=market_id, shares=shares)
        return await adapter.vault_withdraw(chain_id=chain_id, vault_address=market_id, assets=raw_amount)
    if venue == "sparklend":
        return await adapter.unlend(chain_id=chain_id, underlying_token=market_id, qty=raw_amount, withdraw_full=withdraw_full)
    if venue == "euler_v2":
        return await adapter.unlend(chain_id=chain_id, vault=market_id, amount=raw_amount, withdraw_full=withdraw_full)
    if venue == "hyperlend":
        # HyperlendAdapter has no withdraw_full path and rejects qty<=0; resolve the live raw
        # supply from positions when the caller asked for a full withdraw.
        if withdraw_full or raw_amount <= 0:
            account = _wallet_address_for_unlend(adapter)
            read_adapter = await get_read_adapter("hyperlend")
            positions = await _hyperlend_positions(read_adapter, chain_id, ALLOWED_STABLES, account)
            match = next((p for p in positions if p.market_id.lower() == market_id.lower()), None)
            if match is None or match.supply_raw <= 0:
                return False, {"error": f"no Hyperlend position to withdraw at {market_id}"}
            raw_amount = match.supply_raw
        return await adapter.unlend(underlying_token=market_id, qty=raw_amount, chain_id=chain_id)
    if venue == "moonwell":
        # unlend() redeems mToken units, not underlying. Resolve the live position to
        # convert a partial underlying amount via the exchange rate, or redeem in full.
        ok_pos, pos = await adapter.get_pos(mtoken=market_id)
        if not ok_pos or not isinstance(pos, dict):
            return False, {"error": f"moonwell get_pos failed at {market_id}: {pos}"}
        mtoken_balance = int(pos.get("mtoken_balance") or 0)
        if mtoken_balance <= 0:
            return False, {"error": f"no Moonwell position to withdraw at {market_id}"}
        if withdraw_full or raw_amount <= 0:
            redeem_amount = mtoken_balance
        else:
            exchange_rate = int(pos.get("exchange_rate") or 0)
            redeem_amount = (int(raw_amount) * MANTISSA) // exchange_rate if exchange_rate else mtoken_balance
            redeem_amount = min(redeem_amount, mtoken_balance)
        return await adapter.unlend(mtoken=market_id, amount=redeem_amount)
    raise ValueError(f"unknown venue: {venue}")


def _wallet_address_for_unlend(adapter: Any) -> str:
    """Best-effort lookup of the signer address attached to a write adapter."""
    addr = getattr(adapter, "wallet_address", None)
    if addr:
        return str(addr)
    cfg = getattr(adapter, "config", None) or {}
    main = (cfg.get("main_wallet") or {}) if isinstance(cfg, dict) else {}
    if main.get("address"):
        return str(main["address"])
    raise RuntimeError("Hyperlend full-withdraw needs the signer address but none is wired on the adapter")
