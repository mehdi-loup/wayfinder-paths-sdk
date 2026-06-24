"""PRIME Daily Intel — one-shot snapshot for $PRIME token.

Modules:
  1. price_momentum   — spot price, 7d/30d change, annualized vol (Delta Lab)
  2. cross_chain      — Base vs ETH on-chain price spread + bridge cost estimate
  3. uniswap_v3       — PRIME/WETH 0.3% and 1% pools on Ethereum (liquidity, price, TVL)
  4. aerodrome        — CL200-WETH/PRIME on Base (TVL, gauge, fee APY from Delta Lab)
  5. alpha_signals    — scored PRIME mentions from Alpha Lab (last 24h)
  6. onchain_pulse    — large Transfer events on Base + Ethereum (last 24h)

Run:
  poetry run python examples/paths/prime-daily-intel/scripts/main.py
  poetry run python examples/paths/prime-daily-intel/scripts/main.py --config inputs/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from wayfinder_paths.core.clients import ALPHA_LAB_CLIENT, DELTA_LAB_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

CHAIN_ETH = 1
CHAIN_BASE = 8453

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

POOL_V3_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

GAUGE_ABI = [
    {
        "inputs": [],
        "name": "rewardToken",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Aerodrome Slipstream slot0 has 6 outputs (no feeProtocol vs Uniswap V3's 7)
AERO_POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Approximate bridge cost: BRAP cross-chain swap fee estimate (~0.3% + gas)
BRIDGE_COST_PCT = 0.3


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        v = float(value)
        return default if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return default


def _sqrtprice_to_price(sqrt_price_x96: int, *, token0_decimals: int = 18, token1_decimals: int = 18) -> float:
    """Convert Uniswap V3 sqrtPriceX96 to human-readable token1/token0 price."""
    if sqrt_price_x96 == 0:
        return 0.0
    price_raw = (sqrt_price_x96 / (2**96)) ** 2
    return price_raw * (10**token0_decimals) / (10**token1_decimals)


# ---------------------------------------------------------------------------
# Module 1: Price momentum
# ---------------------------------------------------------------------------

async def get_price_momentum() -> dict[str, Any]:
    try:
        ts_data = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol="PRIME", lookback_days=30, series="price"
        )
        price_df = ts_data.get("price")
        if price_df is None or price_df.empty:
            return {"error": "no price data"}

        prices = price_df["price_usd"].dropna()
        if len(prices) < 2:
            return {"error": "insufficient price data"}

        current = float(prices.iloc[-1])
        ts_7d_ago = prices.index[-1] - timedelta(days=7)
        ts_30d_ago = prices.index[-1] - timedelta(days=30)

        price_7d_ago = float(prices.asof(ts_7d_ago)) if len(prices) > 7 else None
        price_30d_ago = float(prices.asof(ts_30d_ago)) if len(prices) > 28 else None

        returns = prices.pct_change().dropna()
        ann_vol = float(np.std(returns) * math.sqrt(24 * 365)) if len(returns) > 1 else None

        change_7d = ((current / price_7d_ago) - 1) if price_7d_ago else None
        change_30d = ((current / price_30d_ago) - 1) if price_30d_ago else None

        return {
            "price_usd": round(current, 6),
            "change_7d": round(change_7d, 4) if change_7d is not None else None,
            "change_30d": round(change_30d, 4) if change_30d is not None else None,
            "ann_vol": round(ann_vol, 4) if ann_vol is not None else None,
            "series_hours": len(prices),
            "as_of": prices.index[-1].isoformat(),
        }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Module 2: Cross-chain spread
# ---------------------------------------------------------------------------

async def get_cross_chain_spread(cfg: dict[str, Any], prime_price_usd: float, weth_price_usd: float) -> dict[str, Any]:
    """Derive on-chain PRIME price on Ethereum from the 0.3% V3 pool."""
    try:
        pool_addr = cfg["uniswap_v3"]["pools"][0]["address"]
        prime_eth = cfg["tokens"]["prime_eth"]

        async with web3_from_chain_id(CHAIN_ETH) as w3:
            pool = w3.eth.contract(address=w3.to_checksum_address(pool_addr), abi=POOL_V3_ABI)
            slot0, t0 = await asyncio.gather(
                pool.functions.slot0().call(),
                pool.functions.token0().call(),
            )

        sqrt_price = slot0[0]
        if sqrt_price == 0 or weth_price_usd == 0:
            return {"error": "could not derive on-chain price"}

        prime_is_t0 = t0.lower() == prime_eth.lower()
        prime_in_weth = _sqrtprice_to_price(sqrt_price) if prime_is_t0 else (
            1 / _sqrtprice_to_price(sqrt_price) if _sqrtprice_to_price(sqrt_price) > 0 else 0.0
        )
        prime_eth_usd = prime_in_weth * weth_price_usd

        spread_pct = ((prime_eth_usd / prime_price_usd) - 1) * 100 if prime_price_usd > 0 else None
        net_spread_pct = (spread_pct - BRIDGE_COST_PCT) if spread_pct is not None else None
        cheaper = "ethereum" if (spread_pct or 0) < 0 else "base"
        alert = abs(net_spread_pct or 0) >= cfg["thresholds"]["spread_alert_pct"]

        return {
            "price_base_usd": round(prime_price_usd, 6),
            "price_eth_usd": round(prime_eth_usd, 6),
            "weth_price_usd": round(weth_price_usd, 2),
            "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
            "net_spread_after_bridge_pct": round(net_spread_pct, 3) if net_spread_pct is not None else None,
            "cheaper_chain": cheaper,
            "bridge_cost_est_pct": BRIDGE_COST_PCT,
            "alert": alert,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Module 3: Uniswap V3 pools
# ---------------------------------------------------------------------------

async def get_uniswap_v3_pools(cfg: dict[str, Any], weth_price_usd: float, prime_price_usd: float) -> list[dict[str, Any]]:
    results = []
    prime_eth = cfg["tokens"]["prime_eth"]
    weth_eth = cfg["tokens"]["weth_eth"]

    async with web3_from_chain_id(CHAIN_ETH) as w3:
        for pool_cfg in cfg["uniswap_v3"]["pools"]:
            try:
                addr = w3.to_checksum_address(pool_cfg["address"])
                pool = w3.eth.contract(address=addr, abi=POOL_V3_ABI)

                slot0, liq, t0 = await asyncio.gather(
                    pool.functions.slot0().call(),
                    pool.functions.liquidity().call(),
                    pool.functions.token0().call(),
                )

                sqrt_price = slot0[0]
                tick = slot0[1]
                prime_is_t0 = t0.lower() == prime_eth.lower()

                if prime_is_t0:
                    prime_in_weth = _sqrtprice_to_price(sqrt_price)
                else:
                    raw = _sqrtprice_to_price(sqrt_price)
                    prime_in_weth = (1 / raw) if raw > 0 else 0.0

                prime_price_onchain = prime_in_weth * weth_price_usd

                prime_contract = w3.eth.contract(address=w3.to_checksum_address(prime_eth), abi=ERC20_ABI)
                weth_contract = w3.eth.contract(address=w3.to_checksum_address(weth_eth), abi=ERC20_ABI)
                prime_bal_raw, weth_bal_raw = await asyncio.gather(
                    prime_contract.functions.balanceOf(addr).call(),
                    weth_contract.functions.balanceOf(addr).call(),
                )

                prime_bal = prime_bal_raw / 1e18
                weth_bal = weth_bal_raw / 1e18
                tvl_usd = prime_bal * prime_price_usd + weth_bal * weth_price_usd

                results.append({
                    "pool": pool_cfg["label"],
                    "address": pool_cfg["address"],
                    "tick": tick,
                    "liquidity": liq,
                    "prime_in_weth": round(prime_in_weth, 8),
                    "prime_price_usd": round(prime_price_onchain, 6),
                    "prime_balance": round(prime_bal, 2),
                    "weth_balance": round(weth_bal, 4),
                    "tvl_usd": round(tvl_usd, 2),
                    "active": liq > 0,
                })
            except Exception as exc:
                results.append({"pool": pool_cfg["label"], "error": str(exc)})

    return results


# ---------------------------------------------------------------------------
# Module 4: Aerodrome CL200-WETH/PRIME
# ---------------------------------------------------------------------------

async def get_aerodrome_pool(cfg: dict[str, Any], weth_price_usd: float, prime_price_usd: float) -> dict[str, Any]:
    aero_cfg = cfg["aerodrome"]
    prime_base = cfg["tokens"]["prime_base"]
    weth_base = cfg["tokens"]["weth_base"]

    try:
        async with web3_from_chain_id(CHAIN_BASE) as w3:
            pool = w3.eth.contract(address=w3.to_checksum_address(aero_cfg["pool_address"]), abi=AERO_POOL_ABI)
            gauge = w3.eth.contract(address=w3.to_checksum_address(aero_cfg["gauge_address"]), abi=GAUGE_ABI)

            slot0, liq, t0, reward_token = await asyncio.gather(
                pool.functions.slot0().call(),
                pool.functions.liquidity().call(),
                pool.functions.token0().call(),
                gauge.functions.rewardToken().call(),
            )
            zero = "0x0000000000000000000000000000000000000000"
            gauge_alive = reward_token.lower() != zero

            prime_contract = w3.eth.contract(address=w3.to_checksum_address(prime_base), abi=ERC20_ABI)
            weth_contract = w3.eth.contract(address=w3.to_checksum_address(weth_base), abi=ERC20_ABI)
            prime_bal_raw, weth_bal_raw = await asyncio.gather(
                prime_contract.functions.balanceOf(w3.to_checksum_address(aero_cfg["pool_address"])).call(),
                weth_contract.functions.balanceOf(w3.to_checksum_address(aero_cfg["pool_address"])).call(),
            )

        prime_bal = prime_bal_raw / 1e18
        weth_bal = weth_bal_raw / 1e18
        tvl_usd = prime_bal * prime_price_usd + weth_bal * weth_price_usd

        # WETH (0x4200...) < PRIME (0xfA98...) so WETH is token0 on Base
        weth_is_t0 = t0.lower() == weth_base.lower()
        sqrt_price = slot0[0]
        raw = _sqrtprice_to_price(sqrt_price)
        prime_in_weth = (1 / raw) if (weth_is_t0 and raw > 0) else raw
        prime_price_onchain = prime_in_weth * weth_price_usd

        # Supplement with Delta Lab fee APY
        dl_apy: float | None = None
        try:
            apy_data = await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="PRIME", lookback_days=7, limit=10)
            for opp in apy_data.get("opportunities", []):
                extra = (opp.get("instrument") or {}).get("extra") or {}
                if extra.get("pool_address", "").lower() == aero_cfg["pool_address"].lower():
                    dl_apy = _safe(opp.get("apy", {}).get("value"))
                    break
        except Exception:
            pass

        return {
            "label": aero_cfg["label"],
            "pool_address": aero_cfg["pool_address"],
            "gauge_alive": gauge_alive,
            "liquidity": liq,
            "active": liq > 0,
            "prime_balance": round(prime_bal, 2),
            "weth_balance": round(weth_bal, 4),
            "tvl_usd": round(tvl_usd, 2),
            "prime_price_usd": round(prime_price_onchain, 6),
            "fee_apy_pct": round(dl_apy * 100, 2) if dl_apy is not None else None,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Module 5: Alpha Lab signals
# ---------------------------------------------------------------------------

async def get_alpha_signals(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = cfg["thresholds"]
    since = (_utc_now() - timedelta(hours=24)).isoformat()
    try:
        results = await ALPHA_LAB_CLIENT.search(
            search="PRIME",
            created_after=since,
            min_score=thresholds["alpha_min_score"],
            limit=thresholds["alpha_limit"],
        )
        items = results if isinstance(results, list) else (results.get("results") or [])
        signals = []
        for item in items:
            signals.append({
                "score": _safe(item.get("insightfulness_score")),
                "type": item.get("scan_type"),
                "summary": (item.get("summary") or item.get("content") or "")[:200],
                "created_at": item.get("created_at"),
                "url": item.get("url"),
            })
        return sorted(signals, key=lambda x: x["score"], reverse=True)
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Module 6: On-chain pulse (large Transfer events)
# ---------------------------------------------------------------------------

async def get_onchain_pulse(cfg: dict[str, Any], prime_price_usd: float) -> dict[str, Any]:
    threshold = cfg["thresholds"]["large_transfer_prime"]
    lookback_h = cfg["thresholds"]["pulse_lookback_hours"]
    tokens = cfg["tokens"]

    BLOCKS_PER_HOUR = {CHAIN_ETH: 300, CHAIN_BASE: 1800}

    async def scan_chain(chain_id: int, token_addr: str) -> list[dict[str, Any]]:
        transfers: list[dict[str, Any]] = []
        blocks_back = lookback_h * BLOCKS_PER_HOUR[chain_id]
        try:
            async with web3_from_chain_id(chain_id) as w3:
                latest = await w3.eth.block_number
                from_block = max(0, latest - blocks_back)
                checksum = w3.to_checksum_address(token_addr)
                transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").hex()
                logs = await w3.eth.get_logs({
                    "fromBlock": from_block,
                    "toBlock": "latest",
                    "address": checksum,
                    "topics": [transfer_topic],
                })
                for log in logs:
                    try:
                        value = int(log["data"].hex(), 16) / 1e18
                        if value >= threshold:
                            from_addr = "0x" + log["topics"][1].hex()[-40:]
                            to_addr = "0x" + log["topics"][2].hex()[-40:]
                            transfers.append({
                                "block": log["blockNumber"],
                                "from": from_addr,
                                "to": to_addr,
                                "amount_prime": round(value, 2),
                                "value_usd": round(value * prime_price_usd, 2),
                                "tx": log["transactionHash"].hex(),
                            })
                    except Exception:
                        continue
        except Exception as exc:
            return [{"error": str(exc)}]
        return sorted(transfers, key=lambda x: x["amount_prime"], reverse=True)[:10]

    eth_transfers, base_transfers = await asyncio.gather(
        scan_chain(CHAIN_ETH, tokens["prime_eth"]),
        scan_chain(CHAIN_BASE, tokens["prime_base"]),
    )

    clean_eth = [t for t in eth_transfers if "error" not in t]
    clean_base = [t for t in base_transfers if "error" not in t]

    return {
        "ethereum": clean_eth,
        "base": clean_base,
        "summary": {
            "eth_large_transfers": len(clean_eth),
            "base_large_transfers": len(clean_base),
            "threshold_prime": threshold,
            "lookback_hours": lookback_h,
        },
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def run(config_path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(config_path.read_text())

    print("  [1/6] price momentum...", flush=True)
    price_data = await get_price_momentum()
    prime_price_usd = _safe(price_data.get("price_usd"))

    print("  [2/6] WETH price...", flush=True)
    weth_price_usd = 0.0
    try:
        weth_ts = await DELTA_LAB_CLIENT.get_asset_timeseries(symbol="ETH", lookback_days=1, series="price", limit=2)
        weth_df = weth_ts.get("price")
        if weth_df is not None and not weth_df.empty:
            weth_price_usd = float(weth_df["price_usd"].iloc[-1])
    except Exception:
        pass

    print("  [3/6] cross-chain spread...", flush=True)
    cross_chain = await get_cross_chain_spread(cfg, prime_price_usd, weth_price_usd)

    print("  [4/6] Uniswap V3 pools...", flush=True)
    uniswap_v3 = await get_uniswap_v3_pools(cfg, weth_price_usd, prime_price_usd)

    print("  [5/6] Aerodrome LP...", flush=True)
    aerodrome = await get_aerodrome_pool(cfg, weth_price_usd, prime_price_usd)

    print("  [6/6] Alpha signals + on-chain pulse...", flush=True)
    alpha_signals, onchain_pulse = await asyncio.gather(
        get_alpha_signals(cfg),
        get_onchain_pulse(cfg, prime_price_usd),
    )

    return {
        "as_of": _utc_now().isoformat(),
        "price_momentum": price_data,
        "cross_chain": cross_chain,
        "uniswap_v3": uniswap_v3,
        "aerodrome": aerodrome,
        "alpha_signals": alpha_signals,
        "onchain_pulse": onchain_pulse,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PRIME Daily Intel snapshot.")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    config_path = args.config or root / "inputs" / "config.yaml"

    load_config("config.dev.json")

    print("PRIME Daily Intel", flush=True)
    result = asyncio.run(run(config_path))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
