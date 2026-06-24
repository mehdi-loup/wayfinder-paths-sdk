"""
Echelon LP Program — main component

Manages PRIME/WETH Uniswap V3 1% positions for the Echelon Prime LP Rewards Program.
Positions must meet three criteria to earn points:
  1. PRIME/WETH pair on Uniswap V3, 1% fee tier (fee=10000, tick spacing=200)
  2. Combined range width 20%–80% (|lower_pct| + upper_pct)
  3. Position ratio 40/60–60/40 PRIME:ETH by USD value when in range

Out-of-range positions earn zero points regardless of range width or setup.

Modes:
  --mode check    Report current PRIME/WETH 1% positions and Echelon compliance
  --mode setup    Open a new Echelon-compliant LP position (requires wallet signing)
  --mode monitor  Loop continuously, re-checking positions at configured interval
  --mode collect  Collect accrued swap fees from PRIME/WETH 1% positions
  --mode auto     Daily evaluation: exit out-of-range positions, check price stability,
                  swap to balanced ratio, and re-enter centered on the current price

Usage:
  poetry run python examples/paths/echelon-lp/scripts/main.py --mode check
  poetry run python examples/paths/echelon-lp/scripts/main.py --mode setup --dry-run
  poetry run python examples/paths/echelon-lp/scripts/main.py --mode monitor
  poetry run python examples/paths/echelon-lp/scripts/main.py --mode auto --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.uniswap_v3_math import (
    amounts_for_liq_inrange,
    deadline,
    get_pool_slot0,
    slippage_min,
    sqrt_price_x96_from_tick,
    tick_to_price,
)
from wayfinder_paths.mcp.scripting import get_adapter

CHAIN_ETH = 1
PRIME_ETH = "0xb23d80f5FefcDDaa212212F028021B41DEd428CF"
WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
FEE_1PCT = 10_000
TICK_SPACING = 200
DASHBOARD_BASE = "primelpdashboard.xyz"

# Echelon program bounds
RANGE_MIN_COMBINED_PCT = 20.0
RANGE_MAX_COMBINED_PCT = 80.0
RATIO_MIN_PRIME_PCT = 40.0
RATIO_MAX_PRIME_PCT = 60.0

# Uniswap V3 SwapRouter02 on Ethereum
_SWAP_ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
_SWAP_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]
# skip swap below this threshold to avoid dust transactions (~0.001 tokens)
_SWAP_DUST_WEI = 10**15


def _validate_range(lower_pct: float, upper_pct: float) -> None:
    combined = lower_pct + upper_pct
    if not (RANGE_MIN_COMBINED_PCT <= combined <= RANGE_MAX_COMBINED_PCT):
        raise ValueError(
            f"Combined range {lower_pct:.1f}% + {upper_pct:.1f}% = {combined:.1f}% "
            f"violates the Echelon requirement of {RANGE_MIN_COMBINED_PCT}%–{RANGE_MAX_COMBINED_PCT}%."
        )
    if lower_pct <= 0 or upper_pct <= 0:
        raise ValueError("lower_pct and upper_pct must both be positive.")


def _tick_for_pct(current_tick: int, pct: float, *, is_lower: bool) -> int:
    ratio = (1.0 - pct / 100.0) if is_lower else (1.0 + pct / 100.0)
    offset = math.log(ratio) / math.log(1.0001)
    raw = current_tick + offset
    if is_lower:
        return int(math.floor(raw / TICK_SPACING) * TICK_SPACING)
    return int(math.ceil(raw / TICK_SPACING) * TICK_SPACING)


def _range_from_ticks(tick_lower: int, tick_upper: int, current_tick: int) -> tuple[float, float, float]:
    p_cur = tick_to_price(current_tick)
    p_lo = tick_to_price(tick_lower)
    p_hi = tick_to_price(tick_upper)
    lower_pct = (1.0 - p_lo / p_cur) * 100.0
    upper_pct = (p_hi / p_cur - 1.0) * 100.0
    return lower_pct, upper_pct, lower_pct + upper_pct


def _ratio_pct(amount0_prime: int, amount1_weth: int, prime_usd: float, eth_usd: float) -> float:
    prime_val = (amount0_prime / 1e18) * prime_usd
    weth_val = (amount1_weth / 1e18) * eth_usd
    total = prime_val + weth_val
    if total == 0.0:
        return 0.0
    return prime_val / total * 100.0


def _ratio_label(ratio_pct: float) -> str:
    if RATIO_MIN_PRIME_PCT <= ratio_pct <= RATIO_MAX_PRIME_PCT:
        if 45.0 <= ratio_pct <= 55.0:
            return "OPTIMAL"
        return "QUALIFYING"
    return "OUT_OF_RATIO"


def _estimate_mint_ratio(
    sqrt_p: int, tick_lower: int, tick_upper: int, prime_usd: float, eth_usd: float
) -> float:
    """Estimate PRIME% at mint by computing token proportions at current price with unit liquidity."""
    sqrt_a = sqrt_price_x96_from_tick(tick_lower)
    sqrt_b = sqrt_price_x96_from_tick(tick_upper)
    amt0, amt1 = amounts_for_liq_inrange(sqrt_p, sqrt_a, sqrt_b, 10**24)
    return _ratio_pct(amt0, amt1, prime_usd, eth_usd)


async def fetch_prices() -> tuple[float, float]:
    """Return (prime_price_usd, eth_price_usd)."""
    eth_ts, prime_ts = await asyncio.gather(
        DELTA_LAB_CLIENT.get_asset_timeseries(symbol="ETH", series="price", lookback_days=1, limit=2),
        DELTA_LAB_CLIENT.get_asset_timeseries(symbol="PRIME", series="price", lookback_days=1, limit=2),
    )
    eth_df = eth_ts.get("price")
    prime_df = prime_ts.get("price")
    if eth_df is None or eth_df.empty:
        raise RuntimeError("Could not fetch ETH price from Delta Lab")
    if prime_df is None or prime_df.empty:
        raise RuntimeError("Could not fetch PRIME price from Delta Lab")
    return (
        float(prime_df["price_usd"].dropna().iloc[-1]),
        float(eth_df["price_usd"].dropna().iloc[-1]),
    )


async def _make_adapter(wallet_label: str) -> UniswapAdapter:
    return await get_adapter(UniswapAdapter, wallet_label, config_overrides={"chain_id": CHAIN_ETH})


async def check_positions(cfg: dict[str, Any]) -> dict[str, Any]:
    wallet_label = cfg["wallet"]["label"]
    owner_wallet = cfg.get("echelon", {}).get("owner_wallet", "")

    adapter = await _make_adapter(wallet_label)
    prime_usd, eth_usd = await fetch_prices()

    ok, pool_address = await adapter.get_pool(PRIME_ETH, WETH_ETH, FEE_1PCT)
    if not ok or not pool_address:
        raise RuntimeError(f"Could not find PRIME/WETH 1% pool on Ethereum: {pool_address}")

    slot0 = await get_pool_slot0(pool_address, CHAIN_ETH, 18, 18)
    current_tick: int = slot0["tick"]
    sqrt_p: int = slot0["sqrt_price_x96"]
    # price = WETH per PRIME (both 18 dec)
    prime_price_in_weth: float = slot0["price"]

    ok, all_positions = await adapter.get_positions()
    if not ok:
        raise RuntimeError(f"Could not fetch positions: {all_positions}")

    prime_lower = PRIME_ETH.lower()
    weth_lower = WETH_ETH.lower()
    qualifying = [
        p for p in all_positions
        if p["fee"] == FEE_1PCT
        and {p["token0"].lower(), p["token1"].lower()} == {prime_lower, weth_lower}
    ]

    position_reports = []
    for pos in qualifying:
        token_id = pos["token_id"]
        tick_lower = pos["tick_lower"]
        tick_upper = pos["tick_upper"]
        liquidity = pos["liquidity"]

        in_range = tick_lower <= current_tick <= tick_upper

        lower_pct, upper_pct, combined_pct = _range_from_ticks(tick_lower, tick_upper, current_tick)
        range_valid = RANGE_MIN_COMBINED_PCT <= combined_pct <= RANGE_MAX_COMBINED_PCT

        sqrt_a = sqrt_price_x96_from_tick(tick_lower)
        sqrt_b = sqrt_price_x96_from_tick(tick_upper)
        amt0, amt1 = amounts_for_liq_inrange(sqrt_p, sqrt_a, sqrt_b, liquidity)
        ratio = _ratio_pct(amt0, amt1, prime_usd, eth_usd)
        ratio_label = _ratio_label(ratio) if in_range else "OUT_OF_RANGE"

        echelon_ok = in_range and range_valid and ratio_label in ("OPTIMAL", "QUALIFYING")

        position_reports.append({
            "token_id": token_id,
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "lower_pct": round(lower_pct, 1),
            "upper_pct": round(upper_pct, 1),
            "combined_range_pct": round(combined_pct, 1),
            "range_valid": range_valid,
            "in_range": in_range,
            "liquidity": liquidity,
            "prime_amount": round(amt0 / 1e18, 4),
            "weth_amount": round(amt1 / 1e18, 6),
            "prime_usd_value": round((amt0 / 1e18) * prime_usd, 2),
            "weth_usd_value": round((amt1 / 1e18) * eth_usd, 2),
            "ratio_prime_pct": round(ratio, 1),
            "ratio_label": ratio_label,
            "echelon_qualifying": echelon_ok,
        })

    dashboard_url = f"{DASHBOARD_BASE}/?address={owner_wallet}" if owner_wallet else DASHBOARD_BASE

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "pool_address": pool_address,
        "current_tick": current_tick,
        "sqrt_price_x96": sqrt_p,
        "prime_price_usd": round(prime_usd, 4),
        "eth_price_usd": round(eth_usd, 2),
        "prime_in_weth": round(prime_price_in_weth, 8),
        "positions_found": len(qualifying),
        "positions": position_reports,
        "dashboard_url": dashboard_url,
    }


def _print_check(result: dict[str, Any]) -> None:
    print(f"\n[{result['as_of']}]")
    print(f"  PRIME: ${result['prime_price_usd']}  |  ETH: ${result['eth_price_usd']:,.2f}")
    print(f"  Pool: {result['pool_address']}")
    print(f"  Dashboard: {result['dashboard_url']}")
    print()

    positions = result["positions"]
    if not positions:
        print("  No PRIME/WETH 1% positions found for this wallet.")
        print("  Run --mode setup to create one.")
        return

    for p in positions:
        status = "✓ QUALIFYING" if p["echelon_qualifying"] else "✗ NOT QUALIFYING"
        print(f"  Position #{p['token_id']}  {status}")
        print(f"    Range:    -{p['lower_pct']:.1f}% / +{p['upper_pct']:.1f}%  "
              f"(combined {p['combined_range_pct']:.1f}%  "
              f"{'OK' if p['range_valid'] else 'INVALID — must be 20%–80%'})")
        print(f"    In-range: {'YES' if p['in_range'] else 'NO — earning zero points'}")
        if p["in_range"]:
            print(f"    Ratio:    {p['ratio_prime_pct']:.1f}% PRIME / "
                  f"{100 - p['ratio_prime_pct']:.1f}% ETH  [{p['ratio_label']}]")
            print(f"    Value:    ${p['prime_usd_value']:.2f} PRIME + ${p['weth_usd_value']:.2f} ETH")
        else:
            print(f"    Ratio:    {p['ratio_prime_pct']:.1f}% PRIME / "
                  f"{100 - p['ratio_prime_pct']:.1f}% ETH  (out of range)")
        print()


async def setup_position(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    pos_cfg = cfg["position"]
    lower_pct = float(pos_cfg["lower_pct"])
    upper_pct = float(pos_cfg["upper_pct"])
    prime_amount = float(pos_cfg["prime_amount"])
    weth_amount = float(pos_cfg["weth_amount"])
    slippage_bps = int(pos_cfg.get("slippage_bps", 300))

    _validate_range(lower_pct, upper_pct)

    wallet_label = cfg["wallet"]["label"]
    adapter = await _make_adapter(wallet_label)
    prime_usd, eth_usd = await fetch_prices()

    ok, pool_address = await adapter.get_pool(PRIME_ETH, WETH_ETH, FEE_1PCT)
    if not ok or not pool_address:
        raise RuntimeError(f"Could not find PRIME/WETH 1% pool: {pool_address}")

    slot0 = await get_pool_slot0(pool_address, CHAIN_ETH, 18, 18)
    current_tick: int = slot0["tick"]
    sqrt_p: int = slot0["sqrt_price_x96"]

    tick_lower = _tick_for_pct(current_tick, lower_pct, is_lower=True)
    tick_upper = _tick_for_pct(current_tick, upper_pct, is_lower=False)

    # Confirm actual range percentages after tick snapping
    actual_lower_pct, actual_upper_pct, actual_combined = _range_from_ticks(
        tick_lower, tick_upper, current_tick
    )
    _validate_range(actual_lower_pct, actual_upper_pct)

    est_ratio = _estimate_mint_ratio(sqrt_p, tick_lower, tick_upper, prime_usd, eth_usd)
    ratio_label = _ratio_label(est_ratio)

    amount0_desired = int(prime_amount * 1e18)
    amount1_desired = int(weth_amount * 1e18)

    print(f"\n  Planned position:")
    print(f"    Pair:     PRIME / WETH  (1% fee tier, tick spacing {TICK_SPACING})")
    print(f"    Range:    -{actual_lower_pct:.1f}% / +{actual_upper_pct:.1f}%  "
          f"(combined {actual_combined:.1f}%  — Echelon valid: 20%–80%)")
    print(f"    Ticks:    {tick_lower} → {tick_upper}  (current: {current_tick})")
    print(f"    Est. ratio at mint: {est_ratio:.1f}% PRIME  [{ratio_label}]")
    print(f"    Max deposit: {prime_amount} PRIME + {weth_amount} WETH")
    print(f"    Prices:   PRIME ${prime_usd:.4f}  |  ETH ${eth_usd:,.2f}")
    print(f"    Slippage: {slippage_bps / 100:.1f}%")

    if ratio_label == "OUT_OF_RATIO":
        print(f"\n  WARNING: Estimated ratio {est_ratio:.1f}% PRIME is outside the 40–60% qualifying band.")
        print("  Consider using a more symmetric range (equal lower_pct and upper_pct).")

    if dry_run:
        print("\n  [dry-run] No transaction submitted.")
        return {
            "action": "dry_run",
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "estimated_ratio_prime_pct": round(est_ratio, 1),
            "ratio_label": ratio_label,
        }

    print("\n  Submitting mint transaction...")
    ok, result = await adapter.add_liquidity(
        token0=PRIME_ETH,
        token1=WETH_ETH,
        fee=FEE_1PCT,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_desired=amount0_desired,
        amount1_desired=amount1_desired,
        slippage_bps=slippage_bps,
    )
    if not ok:
        raise RuntimeError(f"add_liquidity failed: {result}")

    print(f"  Minted! TX: {result}")
    print(f"  Check dashboard in a few hours for position ID and points:")
    owner_wallet = cfg.get("echelon", {}).get("owner_wallet", "")
    if owner_wallet:
        print(f"  {DASHBOARD_BASE}/?address={owner_wallet}")
    return {"action": "minted", "tx_hash": result}


async def collect_fees(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    wallet_label = cfg["wallet"]["label"]
    adapter = await _make_adapter(wallet_label)

    ok, all_positions = await adapter.get_positions()
    if not ok:
        raise RuntimeError(f"Could not fetch positions: {all_positions}")

    prime_lower = PRIME_ETH.lower()
    weth_lower = WETH_ETH.lower()
    targets = [
        p for p in all_positions
        if p["fee"] == FEE_1PCT
        and {p["token0"].lower(), p["token1"].lower()} == {prime_lower, weth_lower}
    ]

    if not targets:
        print("  No PRIME/WETH 1% positions to collect from.")
        return {"action": "collect", "positions_collected": 0}

    collected = []
    for pos in targets:
        token_id = pos["token_id"]
        ok, fees = await adapter.get_uncollected_fees(token_id)
        if ok:
            prime_fees = fees["amount0"] / 1e18
            weth_fees = fees["amount1"] / 1e18
            print(f"  Position #{token_id}: {prime_fees:.6f} PRIME + {weth_fees:.8f} WETH uncollected")
        if dry_run:
            collected.append({"token_id": token_id, "action": "dry_run"})
            continue
        ok, tx = await adapter.collect_fees(token_id)
        if ok:
            print(f"  Position #{token_id}: fees collected  TX: {tx}")
            collected.append({"token_id": token_id, "tx_hash": tx})
        else:
            print(f"  Position #{token_id}: collect failed — {tx}")
            collected.append({"token_id": token_id, "error": tx})

    return {"action": "collect", "positions_collected": len(collected), "results": collected}


async def _stability_check(rebalance_cfg: dict[str, Any]) -> tuple[bool, str]:
    """Return (is_stable, description) using 12h of hourly PRIME prices."""
    ts = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol="PRIME", series="price", lookback_days=1, limit=14
    )
    df = ts.get("price")
    if df is None or df.empty:
        return False, "could not fetch PRIME price history"
    prices = df["price_usd"].dropna().tolist()
    if len(prices) < 5:
        return False, f"insufficient price history ({len(prices)} hourly points)"

    momentum_threshold = float(rebalance_cfg.get("momentum_threshold_pct", 4.0))
    vol_threshold = float(rebalance_cfg.get("volatility_threshold_pct", 3.0))

    # 4h momentum: compare current price to price 4 hours ago
    momentum_pct = abs((prices[-1] - prices[-5]) / prices[-5] * 100)
    if momentum_pct > momentum_threshold:
        return False, (
            f"4h momentum {momentum_pct:.1f}% exceeds threshold {momentum_threshold:.1f}%"
        )

    # 12h return stddev across hourly intervals
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    vol_pct = statistics.stdev(returns) * 100 if len(returns) >= 2 else 0.0
    if vol_pct > vol_threshold:
        return False, (
            f"12h return stddev {vol_pct:.2f}% exceeds threshold {vol_threshold:.1f}%"
        )

    return True, f"stable (4h momentum={momentum_pct:.1f}%, 12h vol={vol_pct:.2f}%)"


async def _swap_exact_input(
    adapter: UniswapAdapter,
    token_in: str,
    token_out: str,
    amount_in_wei: int,
    min_out_wei: int,
) -> tuple[bool, Any]:
    """Swap exact input via Uniswap V3 SwapRouter02 on the PRIME/WETH 1% pool."""
    try:
        await ensure_allowance(
            token_address=to_checksum_address(token_in),
            owner=adapter.owner,
            spender=to_checksum_address(_SWAP_ROUTER),
            amount=amount_in_wei,
            chain_id=adapter.chain_id,
            signing_callback=adapter.sign_callback,
            approval_amount=amount_in_wei * 2,
        )
        params = (
            to_checksum_address(token_in),
            to_checksum_address(token_out),
            FEE_1PCT,
            adapter.owner,
            amount_in_wei,
            min_out_wei,
            0,  # sqrtPriceLimitX96 — 0 means no price limit
        )
        tx = await encode_call(
            target=_SWAP_ROUTER,
            abi=_SWAP_ROUTER_ABI,
            fn_name="exactInputSingle",
            args=[params],
            from_address=adapter.owner,
            chain_id=adapter.chain_id,
        )
        tx_hash = await send_transaction(tx, adapter.sign_callback)
        return True, tx_hash
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def rebalance_position(
    pos: dict[str, Any],
    cfg: dict[str, Any],
    adapter: UniswapAdapter,
    prime_usd: float,
    eth_usd: float,
    current_tick: int,
    sqrt_p: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    token_id = pos["token_id"]
    pos_cfg = cfg["position"]
    rebalance_cfg = cfg.get("rebalance", {})
    swap_slippage_bps = int(rebalance_cfg.get("swap_slippage_bps", 150))

    tick_lower = _tick_for_pct(current_tick, float(pos_cfg["lower_pct"]), is_lower=True)
    tick_upper = _tick_for_pct(current_tick, float(pos_cfg["upper_pct"]), is_lower=False)

    if dry_run:
        est_ratio = _estimate_mint_ratio(sqrt_p, tick_lower, tick_upper, prime_usd, eth_usd)
        print(
            f"  [dry-run] Would exit position #{token_id} and re-enter at "
            f"ticks {tick_lower}→{tick_upper}  (est. ratio {est_ratio:.1f}% PRIME)"
        )
        return {"action": "dry_run", "token_id": token_id}

    # snapshot balances before removal so we know exactly what we receive
    prime_before = await get_token_balance(PRIME_ETH, CHAIN_ETH, adapter.owner)
    weth_before = await get_token_balance(WETH_ETH, CHAIN_ETH, adapter.owner)

    print(f"  Removing position #{token_id} (includes fee collection)...")
    ok, remove_tx = await adapter.remove_liquidity(token_id, collect=True)
    if not ok:
        return {"action": "exit_failed", "token_id": token_id, "error": remove_tx}
    print(f"  Removed: {remove_tx}")

    prime_received = (await get_token_balance(PRIME_ETH, CHAIN_ETH, adapter.owner)) - prime_before
    weth_received = (await get_token_balance(WETH_ETH, CHAIN_ETH, adapter.owner)) - weth_before
    print(f"  Received: {prime_received / 1e18:.4f} PRIME + {weth_received / 1e18:.6f} WETH")

    stable, reason = await _stability_check(rebalance_cfg)
    if not stable:
        print(f"  Re-entry deferred — {reason}")
        return {
            "action": "deferred",
            "token_id": token_id,
            "reason": reason,
            "prime_received": prime_received,
            "weth_received": weth_received,
            "remove_tx": remove_tx,
        }
    print(f"  Price check: {reason}")

    # compute target PRIME amount for the new centered position
    target_prime_pct = _estimate_mint_ratio(sqrt_p, tick_lower, tick_upper, prime_usd, eth_usd)
    total_usd = prime_received / 1e18 * prime_usd + weth_received / 1e18 * eth_usd
    target_prime_wei = int(total_usd * (target_prime_pct / 100) / prime_usd * 1e18)
    delta_prime = prime_received - target_prime_wei

    swap_tx: str | None = None
    if delta_prime > _SWAP_DUST_WEI:
        # excess PRIME — sell some for WETH
        expected_weth = int(delta_prime * prime_usd / eth_usd)
        min_out = slippage_min(expected_weth, swap_slippage_bps)
        print(f"  Swapping {delta_prime / 1e18:.4f} PRIME → WETH...")
        ok, swap_tx = await _swap_exact_input(adapter, PRIME_ETH, WETH_ETH, delta_prime, min_out)
        if not ok:
            return {"action": "swap_failed", "token_id": token_id, "error": swap_tx, "remove_tx": remove_tx}
        print(f"  Swap TX: {swap_tx}")
    elif -delta_prime > _SWAP_DUST_WEI:
        # excess WETH — sell some for PRIME
        weth_to_sell = int(-delta_prime * prime_usd / eth_usd)
        min_out = slippage_min(-delta_prime, swap_slippage_bps)
        print(f"  Swapping {weth_to_sell / 1e18:.6f} WETH → PRIME...")
        ok, swap_tx = await _swap_exact_input(adapter, WETH_ETH, PRIME_ETH, weth_to_sell, min_out)
        if not ok:
            return {"action": "swap_failed", "token_id": token_id, "error": swap_tx, "remove_tx": remove_tx}
        print(f"  Swap TX: {swap_tx}")

    # re-read balances post-swap to get accurate mint amounts
    prime_final = await get_token_balance(PRIME_ETH, CHAIN_ETH, adapter.owner)
    weth_final = await get_token_balance(WETH_ETH, CHAIN_ETH, adapter.owner)

    # cap to configured max amounts
    amount0 = min(prime_final, int(float(pos_cfg["prime_amount"]) * 1e18))
    amount1 = min(weth_final, int(float(pos_cfg["weth_amount"]) * 1e18))

    print(f"  Opening new position: {amount0 / 1e18:.4f} PRIME + {amount1 / 1e18:.6f} WETH...")
    ok, mint_tx = await adapter.add_liquidity(
        token0=PRIME_ETH,
        token1=WETH_ETH,
        fee=FEE_1PCT,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_desired=amount0,
        amount1_desired=amount1,
        slippage_bps=int(pos_cfg.get("slippage_bps", 300)),
    )
    if not ok:
        return {"action": "mint_failed", "token_id": token_id, "error": mint_tx, "remove_tx": remove_tx, "swap_tx": swap_tx}

    print(f"  Rebalanced! Mint TX: {mint_tx}")
    return {
        "action": "rebalanced",
        "old_token_id": token_id,
        "remove_tx": remove_tx,
        "swap_tx": swap_tx,
        "mint_tx": mint_tx,
        "new_tick_lower": tick_lower,
        "new_tick_upper": tick_upper,
        "new_ratio_est": round(target_prime_pct, 1),
    }


async def run(config_path: Path, mode: str, dry_run: bool) -> None:
    cfg = yaml.safe_load(config_path.read_text())

    if mode == "check":
        result = await check_positions(cfg)
        _print_check(result)
        print(json.dumps(result, indent=2, default=str))

    elif mode == "setup":
        result = await setup_position(cfg, dry_run=dry_run)
        print(json.dumps(result, indent=2, default=str))

    elif mode == "monitor":
        interval = int(cfg.get("monitoring", {}).get("check_interval_seconds", 300))
        print(f"Monitoring PRIME/WETH 1% positions (interval: {interval}s, Ctrl+C to stop)")
        while True:
            try:
                result = await check_positions(cfg)
                _print_check(result)
            except Exception as e:
                print(f"  [error] {e}")
            await asyncio.sleep(interval)

    elif mode == "collect":
        result = await collect_fees(cfg, dry_run=dry_run)
        print(json.dumps(result, indent=2, default=str))

    elif mode == "auto":
        rebalance_cfg = cfg.get("rebalance", {})
        if not rebalance_cfg.get("enabled", True):
            print("Auto-rebalance is disabled in config (rebalance.enabled: false).")
            return

        print("Auto-rebalance active — evaluating once daily (Ctrl+C to stop)")
        while True:
            print(f"\n[{datetime.now(UTC).isoformat()}] Daily evaluation...")
            try:
                result = await check_positions(cfg)
                _print_check(result)

                out_of_range = [p for p in result["positions"] if not p["in_range"]]
                if not out_of_range:
                    print("  All positions in range — no action needed.")
                else:
                    adapter = await _make_adapter(cfg["wallet"]["label"])
                    prime_usd = result["prime_price_usd"]
                    eth_usd = result["eth_price_usd"]
                    current_tick = result["current_tick"]
                    sqrt_p = result["sqrt_price_x96"]

                    for pos in out_of_range:
                        print(f"\n  Position #{pos['token_id']} is out of range — initiating rebalance...")
                        rb_result = await rebalance_position(
                            pos, cfg, adapter, prime_usd, eth_usd,
                            current_tick, sqrt_p, dry_run=dry_run,
                        )
                        print(json.dumps(rb_result, indent=2, default=str))
            except Exception as e:
                print(f"  [error] {e}")

            await asyncio.sleep(86400)


def main() -> None:
    parser = argparse.ArgumentParser(description="Echelon LP Program")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--mode",
        choices=["check", "setup", "monitor", "collect", "auto"],
        default="check",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate without submitting transactions")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    config_path = args.config or root / "inputs" / "config.yaml"

    load_config()
    asyncio.run(run(config_path, args.mode, args.dry_run))


if __name__ == "__main__":
    main()
