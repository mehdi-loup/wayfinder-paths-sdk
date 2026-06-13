"""Concentrated LP Manager - controller.

All 9 actions: scan, quote-open, open, status, rebalance, compound, close, attach, detach.
Real adapter wiring for Uniswap V3 (Ethereum/Arbitrum/Base) and Aerodrome Slipstream (Base).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.adapters.aerodrome_slipstream_adapter import (
    AerodromeSlipstreamAdapter,
)
from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.uniswap_v3_abi import UNISWAP_V3_POOL_ABI
from wayfinder_paths.core.utils.uniswap_v3_math import (
    amounts_for_liq_inrange,
    liq_for_amounts,
    sqrt_price_x96_from_tick,
    sqrt_price_x96_to_price,
    ticks_for_range,
)
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.scripting import get_adapter

PATH_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = PATH_DIR / ".state"
CONFIG_PATH = PATH_DIR / "inputs" / "config.yaml"
POOLS_PATH = PATH_DIR / "inputs" / "pools.yaml"

VENUE_UNI = "uniswap_v3"
VENUE_AERO = "aerodrome_slipstream"
SUPPORTED_VENUES = (VENUE_UNI, VENUE_AERO)

SECONDS_PER_DAY = 86_400
SECONDS_PER_YEAR = 365 * SECONDS_PER_DAY

# Repo's UNISWAP_V3_POOL_ABI only has slot0; we need more pool view methods.
_POOL_VIEW_ABI = UNISWAP_V3_POOL_ABI + [
    {"name": "liquidity", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "fee", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "tickSpacing", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "int24"}]},
]

# Aliases that should match each other in scan --pair filter.
_TOKEN_ALIASES = {"ETH": {"ETH", "WETH"}, "WETH": {"ETH", "WETH"}}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# --- per-pool persistent state (cooldown + daily cap) ---


def _state_file(pool_address: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{pool_address.lower()}.json"


def load_pool_state(pool_address: str) -> dict[str, Any]:
    f = _state_file(pool_address)
    if not f.exists():
        return {"last_rebalance_ts": 0, "rebalance_count_by_day": {}}
    return json.loads(f.read_text(encoding="utf-8"))


def save_pool_state(pool_address: str, state: dict[str, Any]) -> None:
    _state_file(pool_address).write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


def record_rebalance(pool_address: str) -> None:
    state = load_pool_state(pool_address)
    state["last_rebalance_ts"] = int(time.time())
    counts = state.setdefault("rebalance_count_by_day", {})
    today = utc_today()
    counts[today] = int(counts.get(today, 0)) + 1
    # Trim to last 14 days
    cutoff = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() - 14 * SECONDS_PER_DAY
    )
    state["rebalance_count_by_day"] = {
        k: v
        for k, v in counts.items()
        if datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() >= cutoff
    }
    save_pool_state(pool_address, state)


def cooldown_check(
    pool_address: str, cooldown_minutes: int, daily_cap: int
) -> tuple[bool, str | None]:
    state = load_pool_state(pool_address)
    now_ts = int(time.time())
    last_ts = int(state.get("last_rebalance_ts", 0))
    if last_ts and now_ts - last_ts < cooldown_minutes * 60:
        wait_s = cooldown_minutes * 60 - (now_ts - last_ts)
        return False, f"cooldown active; {wait_s}s remaining"
    today_count = int(state.get("rebalance_count_by_day", {}).get(utc_today(), 0))
    if today_count >= daily_cap:
        return False, f"daily cap reached ({today_count}/{daily_cap})"
    return True, None


# --- pool config + handle abstraction ---


def get_pool_cfg(pools: dict[str, Any], pool_address: str) -> dict[str, Any]:
    target = pool_address.lower()
    for entry in pools.get("positions") or []:
        if str(entry.get("pool", "")).lower() == target:
            return entry
    raise SystemExit(f"pool {pool_address} not in inputs/pools.yaml")


def merged_strategy(config: dict[str, Any], pool_cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(config.get("default_strategy") or {})
    out.update(pool_cfg.get("strategy") or {})
    return out


class PoolHandle:
    """Unified read/write surface across Uniswap V3 and Aerodrome Slipstream."""

    def __init__(
        self,
        *,
        venue: str,
        chain_id: int,
        pool_address: str,
        wallet_address: str,
        adapter: UniswapAdapter | AerodromeSlipstreamAdapter,
    ) -> None:
        self.venue = venue
        self.chain_id = int(chain_id)
        self.pool_address = to_checksum_address(pool_address)
        self.wallet_address = to_checksum_address(wallet_address)
        self.adapter = adapter
        self._state_cache: dict[str, Any] | None = None

    async def pool_state(self) -> dict[str, Any]:
        if self._state_cache:
            return self._state_cache
        if self.venue == VENUE_AERO:
            ok, data = await self.adapter.slipstream_pool_state(pool=self.pool_address)
            if not ok or not isinstance(data, dict):
                raise RuntimeError(f"slipstream_pool_state failed: {data}")
            dec0, dec1 = await asyncio.gather(
                _erc20_decimals(data["token0"], self.chain_id),
                _erc20_decimals(data["token1"], self.chain_id),
            )
            data["decimals0"], data["decimals1"] = dec0, dec1
            data["fee_pct"] = float(int(data["fee_pips"])) / 1_000_000.0
            self._state_cache = data
            return data
        # Uniswap V3
        async with web3_from_chain_id(self.chain_id) as w3:
            pool = w3.eth.contract(address=self.pool_address, abi=_POOL_VIEW_ABI)
            slot0, liquidity, fee, token0, token1 = await asyncio.gather(
                pool.functions.slot0().call(),
                pool.functions.liquidity().call(),
                pool.functions.fee().call(),
                pool.functions.token0().call(),
                pool.functions.token1().call(),
            )
        token0 = to_checksum_address(token0)
        token1 = to_checksum_address(token1)
        dec0, dec1 = await asyncio.gather(
            _erc20_decimals(token0, self.chain_id),
            _erc20_decimals(token1, self.chain_id),
        )
        spacing_for_fee = {100: 1, 500: 10, 3000: 60, 10000: 200}
        spacing = spacing_for_fee.get(int(fee), 60)
        data = {
            "pool": self.pool_address,
            "token0": token0,
            "token1": token1,
            "decimals0": dec0,
            "decimals1": dec1,
            "sqrt_price_x96": int(slot0[0]),
            "tick": int(slot0[1]),
            "tick_spacing": spacing,
            "liquidity": int(liquidity),
            "fee_pips": int(fee),
            "fee_pct": float(int(fee)) / 1_000_000.0,
        }
        self._state_cache = data
        return data

    async def list_positions(self) -> list[dict[str, Any]]:
        if self.venue == VENUE_AERO:
            ok, state = await self.adapter.get_full_user_state(
                account=self.wallet_address, include_zero_positions=False
            )
            if not ok or not isinstance(state, dict):
                return []
            out: list[dict[str, Any]] = []
            for d in state.get("deployments") or []:
                for p in d.get("positions") or []:
                    if str(p.get("pool", "")).lower() == self.pool_address.lower():
                        out.append(p)
            return out
        ok, positions = await self.adapter.get_positions()
        if not ok or not isinstance(positions, list):
            return []
        st = await self.pool_state()
        t0 = st["token0"].lower()
        t1 = st["token1"].lower()
        fee = st["fee_pips"]
        return [
            p
            for p in positions
            if str(p.get("token0", "")).lower() == t0
            and str(p.get("token1", "")).lower() == t1
            and int(p.get("fee", -1)) == fee
            and int(p.get("liquidity", 0)) > 0
        ]

    async def mint(
        self,
        *,
        tick_lower: int,
        tick_upper: int,
        amount0: int,
        amount1: int,
        slippage_bps: int,
    ) -> dict[str, Any]:
        st = await self.pool_state()
        if self.venue == VENUE_AERO:
            ok, data = await self.adapter.mint_position(
                token0=st["token0"],
                token1=st["token1"],
                tick_spacing=int(st["tick_spacing"]),
                tick_lower=int(tick_lower),
                tick_upper=int(tick_upper),
                amount0_desired=int(amount0),
                amount1_desired=int(amount1),
                slippage_bps=int(slippage_bps),
            )
            if not ok:
                raise RuntimeError(f"mint failed: {data}")
            return {"tx_hash": data.get("tx"), "token_id": data.get("token_id")}
        ok, tx = await self.adapter.add_liquidity(
            token0=st["token0"],
            token1=st["token1"],
            fee=int(st["fee_pips"]),
            tick_lower=int(tick_lower),
            tick_upper=int(tick_upper),
            amount0_desired=int(amount0),
            amount1_desired=int(amount1),
            slippage_bps=int(slippage_bps),
        )
        if not ok:
            raise RuntimeError(f"mint failed: {tx}")
        token_id = await _extract_uniswap_token_id(str(tx), self.chain_id)
        return {"tx_hash": str(tx), "token_id": token_id}

    async def increase(
        self,
        *,
        token_id: int,
        amount0: int,
        amount1: int,
        slippage_bps: int,
    ) -> dict[str, Any]:
        if self.venue == VENUE_AERO:
            ok, data = await self.adapter.increase_liquidity(
                token_id=int(token_id),
                amount0_desired=int(amount0),
                amount1_desired=int(amount1),
                slippage_bps=int(slippage_bps),
            )
            if not ok:
                raise RuntimeError(f"increase failed: {data}")
            return {"tx_hash": data.get("tx")}
        ok, tx = await self.adapter.increase_liquidity(
            token_id=int(token_id),
            amount0_desired=int(amount0),
            amount1_desired=int(amount1),
            slippage_bps=int(slippage_bps),
        )
        if not ok:
            raise RuntimeError(f"increase failed: {tx}")
        return {"tx_hash": str(tx)}

    async def collect(self, token_id: int) -> dict[str, Any]:
        if self.venue == VENUE_AERO:
            ok, data = await self.adapter.collect_fees(token_id=int(token_id))
            if not ok:
                raise RuntimeError(f"collect failed: {data}")
            return {"tx_hash": data.get("tx")}
        ok, tx = await self.adapter.collect_fees(int(token_id))
        if not ok:
            raise RuntimeError(f"collect failed: {tx}")
        return {"tx_hash": str(tx)}

    async def remove_all(self, token_id: int, *, burn: bool, slippage_bps: int) -> dict[str, Any]:
        # Get current liquidity from a fresh position read.
        if self.venue == VENUE_AERO:
            ok, pos = await self.adapter.get_pos(
                token_id=int(token_id), account=self.wallet_address
            )
            if not ok or not isinstance(pos, dict):
                raise RuntimeError(f"get_pos failed: {pos}")
            liquidity = int(pos.get("liquidity") or 0)
            if liquidity > 0:
                ok, data = await self.adapter.decrease_liquidity(
                    token_id=int(token_id),
                    liquidity=liquidity,
                    slippage_bps=int(slippage_bps),
                )
                if not ok:
                    raise RuntimeError(f"decrease failed: {data}")
            ok, collected = await self.adapter.collect_fees(token_id=int(token_id))
            if not ok:
                raise RuntimeError(f"collect failed: {collected}")
            tx_hashes = [collected.get("tx")]
            if burn:
                ok, burned = await self.adapter.burn_position(token_id=int(token_id))
                if not ok:
                    raise RuntimeError(f"burn failed: {burned}")
                tx_hashes.append(burned.get("tx"))
            return {"tx_hashes": tx_hashes}
        ok, tx = await self.adapter.remove_liquidity(
            int(token_id), liquidity=None, slippage_bps=int(slippage_bps),
            collect=True, burn=burn,
        )
        if not ok:
            raise RuntimeError(f"remove failed: {tx}")
        return {"tx_hashes": [str(tx)]}

    async def get_uncollected_fees(self, token_id: int) -> dict[str, int]:
        if self.venue == VENUE_AERO:
            ok, pos = await self.adapter.get_pos(
                token_id=int(token_id), account=self.wallet_address
            )
            if not ok or not isinstance(pos, dict):
                return {"amount0": 0, "amount1": 0}
            return {
                "amount0": int(pos.get("tokensOwed0") or 0),
                "amount1": int(pos.get("tokensOwed1") or 0),
            }
        ok, fees = await self.adapter.get_uncollected_fees(int(token_id))
        if not ok or not isinstance(fees, dict):
            return {"amount0": 0, "amount1": 0}
        return fees


# --- ERC20 + token-id helpers ---


_DECIMALS_CACHE: dict[tuple[int, str], int] = {}


async def _erc20_decimals(address: str, chain_id: int) -> int:
    key = (int(chain_id), address.lower())
    if key in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[key]
    async with web3_from_chain_id(chain_id) as w3:
        contract = w3.eth.contract(address=to_checksum_address(address), abi=ERC20_ABI)
        decimals = int(await contract.functions.decimals().call())
    _DECIMALS_CACHE[key] = decimals
    return decimals


async def _erc20_balance(address: str, chain_id: int, wallet: str) -> int:
    async with web3_from_chain_id(chain_id) as w3:
        contract = w3.eth.contract(address=to_checksum_address(address), abi=ERC20_ABI)
        return int(
            await contract.functions.balanceOf(to_checksum_address(wallet)).call()
        )


async def _native_balance(chain_id: int, wallet: str) -> int:
    async with web3_from_chain_id(chain_id) as w3:
        return int(await w3.eth.get_balance(to_checksum_address(wallet)))


async def _gas_reserve_error(
    config: dict[str, Any], chain_id: int, wallet: str
) -> str | None:
    reserve_native = float(config.get("gas_reserve_native_eth") or 0)
    if reserve_native <= 0:
        return None
    reserve_raw = int(reserve_native * 10**18)
    balance_raw = await _native_balance(chain_id, wallet)
    if balance_raw < reserve_raw:
        return (
            f"native gas balance below reserve: have {balance_raw}, "
            f"need at least {reserve_raw}"
        )
    return None


async def _erc20_symbol(address: str, chain_id: int) -> str:
    try:
        async with web3_from_chain_id(chain_id) as w3:
            contract = w3.eth.contract(
                address=to_checksum_address(address), abi=ERC20_ABI
            )
            return str(await contract.functions.symbol().call())
    except Exception:  # noqa: BLE001
        return address[:6]


async def _extract_uniswap_token_id(tx_hash: str, chain_id: int) -> int | None:
    """Parse the IncreaseLiquidity event from the Uniswap V3 NPM mint receipt."""
    try:
        async with web3_from_chain_id(chain_id) as w3:
            receipt = await w3.eth.get_transaction_receipt(tx_hash)
        # IncreaseLiquidity(uint256 indexed tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)
        topic = (
            "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
        )
        if int(receipt.get("status", 1)) != 1:
            return None
        for log in receipt["logs"]:
            topic0 = log["topics"][0].hex() if log["topics"] else ""
            if topic0 and not topic0.startswith("0x"):
                topic0 = f"0x{topic0}"
            if topic0.lower() == topic.lower():
                return int(log["topics"][1].hex(), 16)
        return None
    except Exception:  # noqa: BLE001
        return None


# --- range strategies ---


def compute_range_ticks(
    strategy_cfg: dict[str, Any],
    pool_state: dict[str, Any],
) -> tuple[int, int]:
    spacing = int(pool_state["tick_spacing"])
    current_tick = int(pool_state["tick"])
    style = str(strategy_cfg.get("range_strategy") or "static_pct")
    if style == "static_pct":
        width_pct = float(strategy_cfg.get("range_width_pct") or 5)
        return ticks_for_range(current_tick, int(width_pct * 100), spacing)
    # atr_band / vol_scaled: width = range_width_atr * sigma_per_year scaled to a daily tick range.
    # Without subgraph swap volume / volatility on every venue, fall back to static_pct equivalent.
    width_atr = float(strategy_cfg.get("range_width_atr") or 1.5)
    fallback_pct = max(2.0, width_atr * 4.0)  # rough proxy
    return ticks_for_range(current_tick, int(fallback_pct * 100), spacing)


# --- price + IL math ---


def _human_price(pool_state: dict[str, Any]) -> float:
    return float(
        sqrt_price_x96_to_price(
            int(pool_state["sqrt_price_x96"]),
            int(pool_state["decimals0"]),
            int(pool_state["decimals1"]),
        )
    )


def _position_amounts_at_price(
    *,
    liquidity: int,
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
    decimals0: int,
    decimals1: int,
) -> tuple[float, float]:
    sqrt_pl = sqrt_price_x96_from_tick(int(tick_lower))
    sqrt_pu = sqrt_price_x96_from_tick(int(tick_upper))
    sqrt_p = max(min(int(sqrt_price_x96), sqrt_pu), sqrt_pl)
    amt0_raw, amt1_raw = amounts_for_liq_inrange(
        sqrt_p, sqrt_pl, sqrt_pu, int(liquidity)
    )
    return (
        from_erc20_raw(amt0_raw, int(decimals0)),
        from_erc20_raw(amt1_raw, int(decimals1)),
    )


def _fit_amounts_to_range(
    *,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    available0: int,
    available1: int,
    max0: int | None = None,
    max1: int | None = None,
) -> tuple[int, int]:
    sqrt_pl = sqrt_price_x96_from_tick(tick_lower)
    sqrt_pu = sqrt_price_x96_from_tick(tick_upper)
    ref_liq = 1 << 96
    ref0, ref1 = amounts_for_liq_inrange(
        int(sqrt_price_x96), sqrt_pl, sqrt_pu, ref_liq
    )
    cap0 = max(0, int(available0))
    cap1 = max(0, int(available1))
    if max0 is not None:
        cap0 = min(cap0, max(0, int(max0)))
    if max1 is not None:
        cap1 = min(cap1, max(0, int(max1)))

    if ref0 == 0:
        return 0, cap1
    if ref1 == 0:
        return cap0, 0

    scale = min(cap0 / ref0, cap1 / ref1)
    amount0 = int(ref0 * scale * 0.999)
    amount1 = int(ref1 * scale * 0.999)
    return max(0, amount0), max(0, amount1)


def _quote_open_amounts_raw(
    *,
    state: dict[str, Any],
    tick_lower: int,
    tick_upper: int,
    size_usd: float,
) -> tuple[int, int]:
    price = _human_price(state)
    sqrt_p = int(state["sqrt_price_x96"])
    sqrt_pl = sqrt_price_x96_from_tick(tick_lower)
    sqrt_pu = sqrt_price_x96_from_tick(tick_upper)
    ref_liq = 1 << 96
    ref_amt0, ref_amt1 = amounts_for_liq_inrange(sqrt_p, sqrt_pl, sqrt_pu, ref_liq)
    dec0 = int(state["decimals0"])
    dec1 = int(state["decimals1"])
    ref_t0 = from_erc20_raw(ref_amt0, dec0)
    ref_t1 = from_erc20_raw(ref_amt1, dec1)
    ref_value_t1 = ref_t0 * price + ref_t1
    if ref_value_t1 <= 0 or size_usd <= 0:
        amount0_human = ref_t0
        amount1_human = ref_t1
    else:
        scale = size_usd / ref_value_t1
        amount0_human = ref_t0 * scale
        amount1_human = ref_t1 * scale
    return int(amount0_human * (10**dec0)), int(amount1_human * (10**dec1))


def il_vs_hodl(
    *,
    initial_price: float,
    new_price: float,
    tick_lower: int,
    tick_upper: int,
    initial_amount0: float,
    initial_amount1: float,
    decimals0: int,
    decimals1: int,
    initial_liquidity: int,
) -> float:
    """Return (position_value - hodl_value) / hodl_value at the new price (in token1 units)."""
    if initial_price <= 0 or new_price <= 0:
        return 0.0
    # Convert new_price (token1 per token0, human) to a synthetic sqrt_price_x96.
    # human_price = (sqrt_p / 2^96) ** 2 * 10^(decimals0 - decimals1)
    raw_price = new_price * (10 ** (decimals1 - decimals0))
    if raw_price <= 0:
        return 0.0
    sqrt_p_x96 = int(math.sqrt(raw_price) * (1 << 96))
    new_amt0, new_amt1 = _position_amounts_at_price(
        liquidity=initial_liquidity,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        sqrt_price_x96=sqrt_p_x96,
        decimals0=decimals0,
        decimals1=decimals1,
    )
    position_value_t1 = new_amt0 * new_price + new_amt1
    hodl_value_t1 = initial_amount0 * new_price + initial_amount1
    if hodl_value_t1 <= 0:
        return 0.0
    return (position_value_t1 - hodl_value_t1) / hodl_value_t1


# --- adapter factory ---


async def _adapter_for(venue: str, chain_id: int, wallet_label: str):
    if venue == VENUE_UNI:
        return await get_adapter(
            UniswapAdapter, wallet_label, config_overrides={"chain_id": int(chain_id)}
        )
    if venue == VENUE_AERO:
        if int(chain_id) != 8453:
            raise SystemExit("aerodrome_slipstream is Base-only (chain 8453)")
        return await get_adapter(AerodromeSlipstreamAdapter, wallet_label)
    raise SystemExit(f"unsupported venue {venue}")


async def make_handle(pool_cfg: dict[str, Any], wallet_label: str) -> PoolHandle:
    venue = str(pool_cfg["venue"])
    if venue not in SUPPORTED_VENUES:
        raise SystemExit(f"unsupported venue {venue}; v0.1 supports {SUPPORTED_VENUES}")
    chain_id = int(pool_cfg["chain"])
    adapter = await _adapter_for(venue, chain_id, wallet_label)
    wallet_address = adapter.wallet_address if hasattr(adapter, "wallet_address") else adapter.owner  # type: ignore[attr-defined]
    return PoolHandle(
        venue=venue,
        chain_id=chain_id,
        pool_address=str(pool_cfg["pool"]),
        wallet_address=wallet_address,
        adapter=adapter,
    )


# --- ledger (best-effort) ---


async def _maybe_ledger(
    enabled: bool, wallet: str, action: str, payload: dict[str, Any]
) -> None:
    if not enabled:
        return
    try:
        ledger = LedgerAdapter({})
        await ledger.record_operation(
            wallet_address=wallet,
            operation_data=type("Op", (), {"model_dump": lambda self, mode: {"name": action, "payload": payload}})(),
            usd_value=float(payload.get("value_usd") or 0.0),
            strategy_name="concentrated-lp-manager",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"ledger record skipped ({action}): {exc}")


# --- actions ---


async def action_scan(args: argparse.Namespace, pools: dict[str, Any]) -> dict[str, Any]:
    """Read-only ranked listing of configured pools by realized fee APR.

    For v0.1, scans the pools declared in inputs/pools.yaml. Discovery of
    arbitrary candidate pools by pair (e.g. all USDC/ETH 0.05% pools across venues)
    is deferred to v0.2 — wire to PoolClient/POOL_CLIENT then.
    """
    rows: list[dict[str, Any]] = []
    for entry in pools.get("positions") or []:
        venue = str(entry.get("venue"))
        if args.venue and args.venue != venue:
            continue
        chain = int(entry.get("chain", 0))
        if args.chain and int(args.chain) != chain:
            continue
        pair = entry.get("pair") or []
        if args.pair and not _pair_matches(args.pair, pair):
            continue
        rows.append(
            {
                "venue": venue,
                "chain": chain,
                "pool": entry.get("pool"),
                "pair": pair,
                "target_usd": entry.get("target_usd"),
                "note": "scan v0.1: config-listed only; cross-venue discovery is v0.2",
            }
        )
    return {"action": "scan", "rows": rows}


def _pair_matches(filter_str: str, pair_list: list[str]) -> bool:
    def expand(s: str) -> set[str]:
        return _TOKEN_ALIASES.get(s, {s})

    filter_tokens = {p.strip().upper() for p in filter_str.replace("-", "/").split("/") if p.strip()}
    pool_tokens = {str(p).upper() for p in pair_list or []}
    filter_expanded = {alias for tok in filter_tokens for alias in expand(tok)}
    pool_expanded = {alias for tok in pool_tokens for alias in expand(tok)}
    # Loose match: any overlap between filter and pool symbols.
    return bool(filter_expanded & pool_expanded) and len(filter_tokens & pool_expanded) >= min(
        len(filter_tokens), len(pool_tokens)
    )


async def action_quote_open(
    args: argparse.Namespace,
    config: dict[str, Any],
    pools: dict[str, Any],
) -> dict[str, Any]:
    pool_cfg = get_pool_cfg(pools, args.pool)
    strategy = merged_strategy(config, pool_cfg)
    handle = await make_handle(pool_cfg, str(config.get("wallet", "main")))
    state = await handle.pool_state()

    tick_lower, tick_upper = compute_range_ticks(strategy, state)
    sym0, sym1 = await asyncio.gather(
        _erc20_symbol(state["token0"], handle.chain_id),
        _erc20_symbol(state["token1"], handle.chain_id),
    )

    size_usd = float(args.size or pool_cfg.get("target_usd") or 0)
    price = _human_price(state)
    sqrt_p = int(state["sqrt_price_x96"])
    sqrt_pl = sqrt_price_x96_from_tick(tick_lower)
    sqrt_pu = sqrt_price_x96_from_tick(tick_upper)

    # Optimal split: pick a reference liquidity, get the implied raw amounts,
    # convert to USD using token1 as the numeraire (assumes token1 ~ USD; for
    # non-stable pools the user should treat the split as a ratio, not USD).
    dec0 = int(state["decimals0"])
    dec1 = int(state["decimals1"])
    amount0_raw, amount1_raw = _quote_open_amounts_raw(
        state=state,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        size_usd=size_usd,
    )
    amount0_human = from_erc20_raw(amount0_raw, dec0)
    amount1_human = from_erc20_raw(amount1_raw, dec1)
    liq = int(liq_for_amounts(sqrt_p, sqrt_pl, sqrt_pu, amount0_raw, amount1_raw))

    # IL at +/-10/25/50% (token0 price moves)
    il = {}
    for delta in (-0.5, -0.25, -0.1, 0.1, 0.25, 0.5):
        new_price = price * (1 + delta)
        il[f"{int(delta * 100):+d}%"] = il_vs_hodl(
            initial_price=price,
            new_price=new_price,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            initial_amount0=amount0_human,
            initial_amount1=amount1_human,
            decimals0=dec0,
            decimals1=dec1,
            initial_liquidity=liq,
        )

    # Quick fee-APR estimate from active-liquidity share, using current pool liquidity.
    pool_liq = int(state.get("liquidity") or 0)
    expected_apr = None
    if pool_liq > 0 and liq > 0:
        share = float(liq) / float(pool_liq + liq)
        # Without subgraph volume, surface the share + fee tier as a "ballpark only" hint.
        expected_apr = {
            "active_liquidity_share_est": share,
            "fee_pct": float(state["fee_pct"]),
            "note": "Multiply share * pool_volume_usd_per_year * fee_pct for true APR; subgraph volume not wired in v0.1.",
        }

    return {
        "action": "quote-open",
        "pool": handle.pool_address,
        "venue": handle.venue,
        "chain": handle.chain_id,
        "pair": [sym0, sym1],
        "current_price_token1_per_token0": price,
        "current_tick": int(state["tick"]),
        "range": {
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "tick_spacing": int(state["tick_spacing"]),
            "strategy": strategy.get("range_strategy"),
        },
        "deposit_split": {
            "amount0_human": amount0_human,
            "amount1_human": amount1_human,
            "amount0_raw": amount0_raw,
            "amount1_raw": amount1_raw,
            "size_usd_assumed": size_usd,
        },
        "expected_apr": expected_apr,
        "impermanent_loss_at_price_move": il,
        "warning": "v0.1 does not pre-swap to balance the pair. Confirm the wallet "
        "holds at least amount0_raw of token0 and amount1_raw of token1 before `open`.",
    }


async def action_open(
    args: argparse.Namespace,
    config: dict[str, Any],
    pools: dict[str, Any],
) -> dict[str, Any]:
    pool_cfg = get_pool_cfg(pools, args.pool)
    strategy = merged_strategy(config, pool_cfg)
    handle = await make_handle(pool_cfg, str(config.get("wallet", "main")))
    state = await handle.pool_state()
    tick_lower, tick_upper = compute_range_ticks(strategy, state)
    gas_error = await _gas_reserve_error(config, handle.chain_id, handle.wallet_address)
    if gas_error:
        return {"action": "open", "ok": False, "error": gas_error}

    bal0, bal1 = await asyncio.gather(
        _erc20_balance(state["token0"], handle.chain_id, handle.wallet_address),
        _erc20_balance(state["token1"], handle.chain_id, handle.wallet_address),
    )

    size_usd = float(args.size or pool_cfg.get("target_usd") or 0)
    max0, max1 = _quote_open_amounts_raw(
        state=state,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        size_usd=size_usd,
    )
    amount0, amount1 = _fit_amounts_to_range(
        sqrt_price_x96=int(state["sqrt_price_x96"]),
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        available0=bal0,
        available1=bal1,
        max0=max0,
        max1=max1,
    )
    if amount0 <= 0 or amount1 <= 0:
        return {
            "action": "open",
            "ok": False,
            "error": (
                "wallet balance insufficient to fit requested range and size; "
                f"have token0={bal0} token1={bal1}, need up to token0={max0} token1={max1}"
            ),
        }

    slippage_bps = int(config.get("slippage_bps") or 30)
    result = await handle.mint(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0=amount0,
        amount1=amount1,
        slippage_bps=slippage_bps,
    )

    payload = {
        "pool": handle.pool_address,
        "venue": handle.venue,
        "chain": handle.chain_id,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "amount0": amount0,
        "amount1": amount1,
        "tx_hash": result.get("tx_hash"),
        "token_id": result.get("token_id"),
    }
    await _maybe_ledger(
        bool(config.get("ledger_record")), handle.wallet_address, "lp_open", payload
    )
    return {"action": "open", "ok": True, **payload}


async def action_status(
    args: argparse.Namespace,
    config: dict[str, Any],
    pools: dict[str, Any],
) -> dict[str, Any]:
    target = args.pool.lower() if args.pool else None
    out: list[dict[str, Any]] = []
    for entry in pools.get("positions") or []:
        if target and str(entry.get("pool", "")).lower() != target:
            continue
        try:
            handle = await make_handle(entry, str(config.get("wallet", "main")))
            state = await handle.pool_state()
            positions = await handle.list_positions()
        except Exception as exc:  # noqa: BLE001
            out.append({"pool": entry.get("pool"), "error": str(exc)})
            continue

        sym0, sym1 = await asyncio.gather(
            _erc20_symbol(state["token0"], handle.chain_id),
            _erc20_symbol(state["token1"], handle.chain_id),
        )
        price = _human_price(state)

        rows = []
        for pos in positions:
            tick_lower = int(pos.get("tick_lower") or pos.get("tickLower") or 0)
            tick_upper = int(pos.get("tick_upper") or pos.get("tickUpper") or 0)
            liquidity = int(pos.get("liquidity") or 0)
            token_id = int(pos.get("token_id") or pos.get("tokenId") or 0)

            current = int(state["tick"])
            in_range = tick_lower <= current < tick_upper
            amt0, amt1 = _position_amounts_at_price(
                liquidity=liquidity,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                sqrt_price_x96=int(state["sqrt_price_x96"]),
                decimals0=int(state["decimals0"]),
                decimals1=int(state["decimals1"]),
            )
            value_in_token1 = amt0 * price + amt1

            fees = await handle.get_uncollected_fees(token_id)
            fee0 = from_erc20_raw(int(fees["amount0"]), int(state["decimals0"]))
            fee1 = from_erc20_raw(int(fees["amount1"]), int(state["decimals1"]))
            fees_value_t1 = fee0 * price + fee1

            cooldown = load_pool_state(handle.pool_address)
            rebalances_today = int(
                cooldown.get("rebalance_count_by_day", {}).get(utc_today(), 0)
            )

            rows.append(
                {
                    "token_id": token_id,
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                    "current_tick": current,
                    "in_range": in_range,
                    "alert": None if in_range else "OUT_OF_RANGE: position is earning zero fees",
                    "amount0_human": amt0,
                    "amount1_human": amt1,
                    "value_token1_units": value_in_token1,
                    "uncollected_fees": {
                        "amount0_human": fee0,
                        "amount1_human": fee1,
                        "value_token1_units": fees_value_t1,
                    },
                    "rebalances_today": rebalances_today,
                    "last_rebalance_ts": int(cooldown.get("last_rebalance_ts", 0)),
                    "time_in_range_pct": (
                        "100.0 (current snapshot only; on-chain swap-event scan deferred to v0.2)"
                        if in_range
                        else "0.0 (current snapshot only)"
                    ),
                }
            )

        out.append(
            {
                "pool": handle.pool_address,
                "venue": handle.venue,
                "chain": handle.chain_id,
                "pair": [sym0, sym1],
                "current_price_token1_per_token0": price,
                "positions": rows,
            }
        )
    return {"action": "status", "as_of": datetime.now(UTC).isoformat(), "pools": out}


async def action_rebalance(
    args: argparse.Namespace,
    config: dict[str, Any],
    pools: dict[str, Any],
) -> dict[str, Any]:
    pool_cfg = get_pool_cfg(pools, args.pool)
    strategy = merged_strategy(config, pool_cfg)
    handle = await make_handle(pool_cfg, str(config.get("wallet", "main")))
    gas_error = await _gas_reserve_error(config, handle.chain_id, handle.wallet_address)
    if gas_error:
        return {"action": "rebalance", "ok": False, "error": gas_error}

    cooldown_min = int(strategy.get("rebalance_cooldown_minutes") or 60)
    daily_cap = int(strategy.get("max_rebalances_per_day") or 4)
    ok, reason = cooldown_check(handle.pool_address, cooldown_min, daily_cap)
    if not ok:
        return {"action": "rebalance", "ok": False, "skipped": reason}

    positions = await handle.list_positions()
    if not positions:
        return {"action": "rebalance", "ok": False, "error": "no active position"}
    pos = positions[0]
    token_id = int(pos.get("token_id") or pos.get("tokenId") or 0)

    slippage_bps = int(config.get("slippage_bps") or 30)
    state = await handle.pool_state()
    before0, before1 = await asyncio.gather(
        _erc20_balance(state["token0"], handle.chain_id, handle.wallet_address),
        _erc20_balance(state["token1"], handle.chain_id, handle.wallet_address),
    )

    # 1. decrease + collect + burn (acceptance criterion: ledger 3 rows)
    decrease_result = await handle.remove_all(token_id, burn=True, slippage_bps=slippage_bps)
    await _maybe_ledger(
        bool(config.get("ledger_record")), handle.wallet_address,
        "lp_rebalance_decrease",
        {"token_id": token_id, "tx_hashes": decrease_result.get("tx_hashes")},
    )

    # 2. swap to balance — venue-native swap deferred to v0.2 (PROMPT: "MEV awareness").
    # For v0.1, we re-mint with whatever the wallet has post-burn, fitted to the new range.
    handle._state_cache = None  # force re-read
    state = await handle.pool_state()
    after0, after1 = await asyncio.gather(
        _erc20_balance(state["token0"], handle.chain_id, handle.wallet_address),
        _erc20_balance(state["token1"], handle.chain_id, handle.wallet_address),
    )
    new_lower, new_upper = compute_range_ticks(strategy, state)
    amount0, amount1 = _fit_amounts_to_range(
        sqrt_price_x96=int(state["sqrt_price_x96"]),
        tick_lower=new_lower,
        tick_upper=new_upper,
        available0=max(0, after0 - before0),
        available1=max(0, after1 - before1),
    )

    if amount0 <= 0 and amount1 <= 0:
        return {
            "action": "rebalance",
            "ok": False,
            "error": "post-burn balances do not fit new range; manual swap needed before re-mint",
            "decrease_tx": decrease_result.get("tx_hashes"),
        }
    if amount0 <= 0 or amount1 <= 0:
        return {
            "action": "rebalance",
            "ok": False,
            "error": "post-burn proceeds are imbalanced; manual swap needed before re-mint",
            "decrease_tx": decrease_result.get("tx_hashes"),
        }

    mint_result = await handle.mint(
        tick_lower=new_lower,
        tick_upper=new_upper,
        amount0=amount0,
        amount1=amount1,
        slippage_bps=slippage_bps,
    )
    record_rebalance(handle.pool_address)
    await _maybe_ledger(
        bool(config.get("ledger_record")), handle.wallet_address,
        "lp_rebalance_mint",
        {"tick_lower": new_lower, "tick_upper": new_upper, **mint_result},
    )

    return {
        "action": "rebalance",
        "ok": True,
        "pool": handle.pool_address,
        "old_token_id": token_id,
        "new_token_id": mint_result.get("token_id"),
        "decrease_tx": decrease_result.get("tx_hashes"),
        "mint_tx": mint_result.get("tx_hash"),
        "new_range": {"tick_lower": new_lower, "tick_upper": new_upper},
    }


async def action_compound(
    args: argparse.Namespace,
    config: dict[str, Any],
    pools: dict[str, Any],
) -> dict[str, Any]:
    target = args.pool.lower() if args.pool else None
    results: list[dict[str, Any]] = []
    for entry in pools.get("positions") or []:
        if target and str(entry.get("pool", "")).lower() != target:
            continue
        strategy = merged_strategy(config, entry)
        if not bool(strategy.get("fee_compound", True)):
            results.append({"pool": entry.get("pool"), "skipped": "fee_compound disabled"})
            continue
        threshold = float(strategy.get("compound_threshold_usd") or 10)
        try:
            handle = await make_handle(entry, str(config.get("wallet", "main")))
            state = await handle.pool_state()
            positions = await handle.list_positions()
        except Exception as exc:  # noqa: BLE001
            results.append({"pool": entry.get("pool"), "error": str(exc)})
            continue
        gas_error = await _gas_reserve_error(config, handle.chain_id, handle.wallet_address)
        if gas_error:
            results.append({"pool": handle.pool_address, "error": gas_error})
            continue
        if not positions:
            results.append({"pool": entry.get("pool"), "skipped": "no active position"})
            continue
        pos = positions[0]
        token_id = int(pos.get("token_id") or pos.get("tokenId") or 0)

        fees = await handle.get_uncollected_fees(token_id)
        price = _human_price(state)
        fee0 = from_erc20_raw(int(fees["amount0"]), int(state["decimals0"]))
        fee1 = from_erc20_raw(int(fees["amount1"]), int(state["decimals1"]))
        fee_value_t1 = fee0 * price + fee1

        if fee_value_t1 < threshold:
            results.append(
                {
                    "pool": handle.pool_address,
                    "skipped": f"fees {fee_value_t1:.4f} < compound_threshold_usd {threshold}",
                }
            )
            continue

        slippage_bps = int(config.get("slippage_bps") or 30)
        before0, before1 = await asyncio.gather(
            _erc20_balance(state["token0"], handle.chain_id, handle.wallet_address),
            _erc20_balance(state["token1"], handle.chain_id, handle.wallet_address),
        )
        collected = await handle.collect(token_id)
        # Re-read balances post-collect, then increase liquidity in the existing range.
        after0, after1 = await asyncio.gather(
            _erc20_balance(state["token0"], handle.chain_id, handle.wallet_address),
            _erc20_balance(state["token1"], handle.chain_id, handle.wallet_address),
        )
        amount0 = max(0, int(after0 - before0))
        amount1 = max(0, int(after1 - before1))
        if amount0 <= 0 or amount1 <= 0:
            results.append(
                {
                    "pool": handle.pool_address,
                    "collected_tx": collected.get("tx_hash"),
                    "skipped": "post-collect balance imbalanced; venue-native swap-to-balance deferred to v0.2",
                }
            )
            continue
        increase = await handle.increase(
            token_id=token_id, amount0=amount0, amount1=amount1, slippage_bps=slippage_bps,
        )
        await _maybe_ledger(
            bool(config.get("ledger_record")), handle.wallet_address,
            "lp_compound", {"token_id": token_id, **increase},
        )
        results.append(
            {
                "pool": handle.pool_address,
                "token_id": token_id,
                "fees_value": fee_value_t1,
                "collected_tx": collected.get("tx_hash"),
                "increase_tx": increase.get("tx_hash"),
            }
        )
    return {"action": "compound", "results": results}


async def action_close(
    args: argparse.Namespace,
    config: dict[str, Any],
    pools: dict[str, Any],
) -> dict[str, Any]:
    pool_cfg = get_pool_cfg(pools, args.pool)
    handle = await make_handle(pool_cfg, str(config.get("wallet", "main")))
    gas_error = await _gas_reserve_error(config, handle.chain_id, handle.wallet_address)
    if gas_error:
        return {"action": "close", "ok": False, "error": gas_error}
    positions = await handle.list_positions()
    if not positions:
        return {"action": "close", "ok": False, "error": "no active position"}
    slippage_bps = int(config.get("slippage_bps") or 30)
    closed = []
    for pos in positions:
        token_id = int(pos.get("token_id") or pos.get("tokenId") or 0)
        result = await handle.remove_all(token_id, burn=True, slippage_bps=slippage_bps)
        await _maybe_ledger(
            bool(config.get("ledger_record")), handle.wallet_address,
            "lp_close", {"token_id": token_id, **result},
        )
        closed.append({"token_id": token_id, **result})
    return {"action": "close", "ok": True, "pool": handle.pool_address, "closed": closed}


def action_attach(config: dict[str, Any]) -> dict[str, Any]:
    interval = int((config.get("monitor") or {}).get("poll_interval_seconds") or 300)
    monitor_path = (PATH_DIR / "scripts" / "monitor.py").resolve()
    binary = shutil.which("wayfinder") or "wayfinder"
    cmd = [
        binary, "runner", "add-job",
        "--name", "concentrated-lp-manager-monitor",
        "--type", "script",
        "--script-path", str(monitor_path),
        "--interval", str(interval),
        "--config", "config.json",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        return {"action": "attach", "ok": False, "error": str(exc), "cmd": cmd}
    return {
        "action": "attach",
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "cmd": cmd,
    }


def action_detach() -> dict[str, Any]:
    binary = shutil.which("wayfinder") or "wayfinder"
    cmd = [binary, "runner", "delete", "concentrated-lp-manager-monitor"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        return {"action": "detach", "ok": False, "error": str(exc), "cmd": cmd}
    return {
        "action": "detach",
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


# --- argparse + dispatch ---


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Concentrated LP Manager controller")
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "scan", "quote-open", "open", "status", "rebalance",
            "compound", "close", "attach", "detach",
        ],
    )
    parser.add_argument("--pool", help="Pool address")
    parser.add_argument("--size", type=float, help="Deposit size in USD (for quote-open / open)")
    parser.add_argument("--venue", help="Filter by venue (scan)")
    parser.add_argument("--pair", help="Filter by pair like ETH/USDC (scan)")
    parser.add_argument("--chain", type=int, help="Filter by chain id (scan)")
    parser.add_argument(
        "--config-path", default=str(CONFIG_PATH), help="Path to inputs/config.yaml"
    )
    parser.add_argument(
        "--pools-path", default=str(POOLS_PATH), help="Path to inputs/pools.yaml"
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_yaml(Path(args.config_path))
    pools = load_yaml(Path(args.pools_path))

    if args.action == "scan":
        return await action_scan(args, pools)
    if args.action == "quote-open":
        if not args.pool:
            raise SystemExit("--pool is required for quote-open")
        return await action_quote_open(args, config, pools)
    if args.action == "open":
        if not args.pool:
            raise SystemExit("--pool is required for open")
        return await action_open(args, config, pools)
    if args.action == "status":
        return await action_status(args, config, pools)
    if args.action == "rebalance":
        if not args.pool:
            raise SystemExit("--pool is required for rebalance")
        return await action_rebalance(args, config, pools)
    if args.action == "compound":
        return await action_compound(args, config, pools)
    if args.action == "close":
        if not args.pool:
            raise SystemExit("--pool is required for close")
        return await action_close(args, config, pools)
    if args.action == "attach":
        return action_attach(config)
    if args.action == "detach":
        return action_detach()
    raise SystemExit(f"unknown action {args.action}")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    # Load global wayfinder config so adapters/clients can find the API key, RPCs, wallets.
    # Done here (not in _run) so test harnesses that pre-load config (e.g. for a Gorlami
    # fork) aren't clobbered by a re-load.
    load_config("config.json")
    payload = asyncio.run(_run(args))
    emit(payload)


if __name__ == "__main__":
    main()
