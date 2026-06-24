"""
PRIME Cross-Chain Arbitrage — main component

Monitors the PRIME price spread between Ethereum (Uniswap V3 0.30% pool) and
Base (Aerodrome CL200 pool). Executes simultaneous buy/sell when gross spread
exceeds the configured threshold (fees + gas + bridge amortisation).

Modes:
  --mode check   Read and report current spread only (no trades)
  --mode once    Run one cycle: check and execute if threshold is met
  --mode loop    Run continuously at check_interval_seconds cadence

Usage:
  poetry run python examples/paths/prime-arb-strategy/scripts/main.py --mode check
  poetry run python examples/paths/prime-arb-strategy/scripts/main.py --config inputs/config.yaml --mode once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.core.clients import DELTA_LAB_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

CHAIN_ETH = 1
CHAIN_BASE = 8453

POOL_V3_ABI = [
    {
        "inputs": [], "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view", "type": "function",
    },
    {
        "inputs": [], "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view", "type": "function",
    },
]

AERO_POOL_ABI = [
    {
        "inputs": [], "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view", "type": "function",
    },
    {
        "inputs": [], "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view", "type": "function",
    },
]


def _sqrtprice_to_price(sqrt_price_x96: int) -> float:
    if sqrt_price_x96 == 0:
        return 0.0
    return (sqrt_price_x96 / (2 ** 96)) ** 2


async def fetch_eth_usd() -> float:
    ts = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol="ETH", series="price", lookback_days=1, limit=2
    )
    df = ts.get("price")
    if df is None or df.empty:
        raise RuntimeError("Could not fetch ETH/USD price")
    return float(df["price_usd"].dropna().iloc[-1])


async def fetch_prices(cfg: dict[str, Any], eth_usd: float) -> dict[str, Any]:
    tokens = cfg["tokens"]
    uni_addr = cfg["uniswap_v3"]["pool_address"]
    aero_addr = cfg["aerodrome"]["pool_address"]

    async def _eth_price() -> float:
        async with web3_from_chain_id(CHAIN_ETH) as w3:
            pool = w3.eth.contract(address=w3.to_checksum_address(uni_addr), abi=POOL_V3_ABI)
            slot0, t0 = await asyncio.gather(
                pool.functions.slot0().call(),
                pool.functions.token0().call(),
            )
        raw = _sqrtprice_to_price(slot0[0])
        prime_is_t0 = t0.lower() == tokens["prime_eth"].lower()
        prime_in_weth = raw if prime_is_t0 else (1 / raw if raw > 0 else 0.0)
        return prime_in_weth * eth_usd

    async def _base_price() -> float:
        async with web3_from_chain_id(CHAIN_BASE) as w3:
            pool = w3.eth.contract(address=w3.to_checksum_address(aero_addr), abi=AERO_POOL_ABI)
            slot0, t0 = await asyncio.gather(
                pool.functions.slot0().call(),
                pool.functions.token0().call(),
            )
        raw = _sqrtprice_to_price(slot0[0])
        weth_is_t0 = t0.lower() == tokens["weth_base"].lower()
        prime_in_weth = (1 / raw) if (weth_is_t0 and raw > 0) else raw
        return prime_in_weth * eth_usd

    price_eth, price_base = await asyncio.gather(_eth_price(), _base_price())
    spread_pct = (price_eth / price_base - 1) * 100 if price_base > 0 else 0.0

    return {
        "price_eth_usd": price_eth,
        "price_base_usd": price_base,
        "spread_pct": spread_pct,
        "cheaper_chain": "ethereum" if spread_pct < 0 else "base",
        "eth_usd": eth_usd,
        "as_of": datetime.now(UTC).isoformat(),
    }


def compute_break_even(cfg: dict[str, Any]) -> float:
    exec_cfg = cfg["execution"]
    trade_size = float(exec_cfg["trade_size_usd"])
    fee_frac = 2 * (30 + 10) / 10_000  # 30bps fee + 10bps slippage, each side
    gas_usd = float(exec_cfg.get("gas_limit_eth_usd", 12.0)) * 0.6 + 0.05 + 2.0  # est + base + bridge
    return (fee_frac + gas_usd / trade_size) * 100


async def check_spread(cfg: dict[str, Any]) -> dict[str, Any]:
    eth_usd = await fetch_eth_usd()
    prices = await fetch_prices(cfg, eth_usd)
    be = compute_break_even(cfg)
    threshold = float(cfg["execution"]["min_gross_spread_pct"])
    abs_spread = abs(prices["spread_pct"])

    return {
        **prices,
        "break_even_pct": round(be, 3),
        "threshold_pct": threshold,
        "opportunity": abs_spread >= threshold,
        "net_spread_pct": round(abs_spread - be, 3) if abs_spread >= threshold else None,
    }


async def run_once(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Check spread and execute if threshold is met."""
    result = await check_spread(cfg)

    print(f"[{result['as_of']}]")
    print(f"  ETH:  ${result['price_eth_usd']:.4f}  |  Base: ${result['price_base_usd']:.4f}")
    print(f"  Spread: {result['spread_pct']:+.3f}%  |  Threshold: {result['threshold_pct']:.2f}%  |  Break-even: {result['break_even_pct']:.3f}%")

    if not result["opportunity"]:
        print("  → No opportunity (spread below threshold)")
        return {**result, "action": "skip"}

    buy_chain = result["cheaper_chain"]
    sell_chain = "base" if buy_chain == "ethereum" else "ethereum"
    trade_size = float(cfg["execution"]["trade_size_usd"])
    print(f"  → OPPORTUNITY: buy on {buy_chain}, sell on {sell_chain}  (net: {result['net_spread_pct']:+.3f}%)")

    if dry_run:
        print("  → [dry-run] skipping execution")
        return {**result, "action": "dry_run"}

    # NOTE: execution logic placeholder — full swap implementation requires
    # wallet configuration and BRAP/Uniswap/Aerodrome adapter wiring.
    # The strategy implementation is intentionally left as a stub at v0.1.0
    # pending the execution layer build-out.
    print("  → [stub] execution not yet implemented in v0.1.0")
    return {**result, "action": "stub"}


async def run_loop(cfg: dict[str, Any], dry_run: bool = False) -> None:
    interval = int(cfg["execution"].get("check_interval_seconds", 60))
    print(f"Running loop (interval: {interval}s, Ctrl+C to stop)")
    while True:
        try:
            await run_once(cfg, dry_run=dry_run)
        except Exception as e:
            print(f"  [error] {e}")
        await asyncio.sleep(interval)


async def run(config_path: Path, mode: str, dry_run: bool) -> None:
    cfg = yaml.safe_load(config_path.read_text())
    if mode == "check":
        result = await check_spread(cfg)
        print(json.dumps(result, indent=2, default=str))
    elif mode == "once":
        result = await run_once(cfg, dry_run=dry_run)
        print(json.dumps(result, indent=2, default=str))
    elif mode == "loop":
        await run_loop(cfg, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="PRIME Cross-Chain Arbitrage")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--mode", choices=["check", "once", "loop"], default="check")
    parser.add_argument("--dry-run", action="store_true", help="Read prices but skip execution")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    config_path = args.config or root / "inputs" / "config.yaml"

    load_config("config.dev.json")
    asyncio.run(run(config_path, args.mode, args.dry_run))


if __name__ == "__main__":
    main()
