"""
Funding Rate Harvester — main entrypoint.

Delta-neutral funding harvester with a triple carry stack: Hyperliquid perp
shorts hedged by yield-bearing spot legs (Pendle PT / weETH / sUSDe / HL
spot), an optional Boros fixed-rate lock on the harvested funding, and
breakeven-gated rotation across assets and spot legs.

Actions:
  discover  — read-only ranked table of (asset, spot leg) net stacked carry
  quote     — full carry decomposition for one symbol/size (+ Boros lock quote)
  deposit   — open a pair: hedge (HL short) first, then spot leg
  update    — core loop: rails → negative-carry exit → rotation → delta → lock
  rotate    — evaluate rotation now (--force relaxes the dwell gate; breakeven always applies)
  lock      — manually open a Boros fixed-rate lock sized to the short leg
  unlock    — manually unwind a Boros lock
  status    — per-pair carry, PnL, lock PnL (separate), liq distance
  unwind    — close one/all pairs (spot first, hedge last)
  exit      — settle to USDC and transfer remaining balance to the main wallet

Fund-moving actions emit a plan with status=requires_confirmation unless
--confirm is passed. Orchestration only — all math lives in scoring.py.
"""

from __future__ import annotations

# ruff: noqa: E402 — sibling imports need the sys.path insert first
import argparse
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

PATH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PATH_DIR / "scripts"))

from legs import (  # noqa: E402
    SUSDE_TOKEN_ID,
    WEETH_TOKEN_ID,
    EthenaLeg,
    EtherfiLeg,
    HedgeVenue,
    HlSpotLeg,
    HyperliquidHedge,
    PairExecutor,
    PaperHedge,
    PaperSpotLeg,
    PendlePtLeg,
    SpotLeg,
    SpotPosition,
    open_failure_leaves_exposure,
)
from legs import USDC_BY_CHAIN as LEG_USDC_BY_CHAIN  # noqa: E402
from rate_lock import BorosRateLock, LockQuote  # noqa: E402
from scoring import (  # noqa: E402
    HOURS_PER_YEAR,
    CarryComponents,
    ComboScore,
    cost_apr,
    delta_rebalance_decision,
    drawdown_halted,
    ema_alpha,
    epoch_bucket,
    idempotency_key,
    is_stale,
    liquidation_action,
    lock_decision,
    negative_carry_exit,
    normalize_funding_apr,
    rank_combos,
    required_margin_usd,
    rotation_decision,
    update_ema,
)

from wayfinder_paths.adapters.balance_adapter.adapter import (
    BalanceAdapter,  # noqa: E402
)
from wayfinder_paths.adapters.boros_adapter import BorosAdapter  # noqa: E402
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter  # noqa: E402
from wayfinder_paths.adapters.ethena_vault_adapter import (
    EthenaVaultAdapter,  # noqa: E402
)
from wayfinder_paths.adapters.hyperliquid_adapter import (
    HyperliquidAdapter,  # noqa: E402
)
from wayfinder_paths.adapters.ledger_adapter import LedgerAdapter  # noqa: E402
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter  # noqa: E402
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT  # noqa: E402
from wayfinder_paths.core.clients.HyperliquidDataClient import (  # noqa: E402
    HYPERLIQUID_DATA_CLIENT,
)
from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT  # noqa: E402
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT  # noqa: E402
from wayfinder_paths.core.config import CONFIG  # noqa: E402
from wayfinder_paths.core.constants import HYPERLIQUID_BRIDGE_ADDRESS  # noqa: E402
from wayfinder_paths.core.constants.hyperliquid import (  # noqa: E402
    MIN_DEPOSIT_USD,
    MIN_ORDER_USD_NOTIONAL,
)
from wayfinder_paths.core.utils.tokens import get_token_balance  # noqa: E402
from wayfinder_paths.core.utils.wallets import (  # noqa: E402
    find_wallet_by_label,
    get_wallet_signing_callback,
    load_wallets,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id  # noqa: E402
from wayfinder_paths.mcp.scripting import get_adapter  # noqa: E402
from wayfinder_paths.runner.monitor_state import (  # noqa: E402
    read_monitor_state,
    write_monitor_state,
)

PATH_SLUG = "funding-rate-harvester"
STATE_NAME = "funding_rate_harvester"

# Pin durable state to one namespace so the interactive CLI and any runner job
# share a single position book. A runner job defaults WAYFINDER_KV_NAMESPACE to
# the job name (in its own isolated subprocess), so without this an interactively
# opened paper/live pair would be invisible to the scheduled `update`, and vice
# versa. Overriding is safe: jobs run in separate processes, so this can't leak
# into another job's namespace.
os.environ["WAYFINDER_KV_NAMESPACE"] = STATE_NAME

WEETH_MAINNET = "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee"
SUSDE_MAINNET = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
USDC_MAINNET = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_ARBITRUM_TOKEN_ID = "usd-coin-arbitrum"

# Per-side execution-cost estimates (bps of notional) used in the carry score;
# override in inputs/config.yaml under costs.
DEFAULT_HEDGE_TAKER_FEE_BPS = 4.5  # HL base-tier taker
DEFAULT_SPOT_COST_BPS = {"hl_spot": 4.5, "pendle_pt": 30.0, "etherfi": 25.0, "ethena": 25.0}
DEFAULT_SLIPPAGE_COST_BPS_PER_FILL = 5.0
DEFAULT_COST_AMORTIZATION_DAYS = 30.0
# Per-migration gas allowance by chain (USD): a mainnet swap costs dollars, an
# L2 swap costs cents. etherfi/ethena are mainnet legs; a Pendle PT leg uses its
# market's chain. Unknown chain → assume mainnet (never under-charge breakeven).
EVM_LEG_GAS_USD_BY_CHAIN = {1: 5.0, 8453: 0.10, 42161: 0.20}
DEFAULT_EVM_LEG_GAS_USD = 5.0
MIN_GAS_WEI = 3 * 10**14  # ~0.0003 native

EXECUTED_KEY_TTL_S = 7 * 86400


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((PATH_DIR / "inputs" / name).read_text(encoding="utf-8")) or {}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _round(value: Any) -> Any:
    return round(value, 4) if isinstance(value, (int, float)) else value


def _compact_combo(d: dict[str, Any]) -> dict[str, Any]:
    """One flat row per combo — drops the nested `meta`/interval detail."""
    meta = d.get("meta") or {}
    return {
        "symbol": d.get("symbol"),
        "spot_leg": d.get("spot_leg"),
        "net_apr": _round(d.get("net_apr")),
        "funding_apr": _round(d.get("funding_apr")),
        "spot_leg_apy": _round(d.get("spot_leg_apy")),
        "cost_apr": _round(
            (d.get("fee_apr") or 0.0)
            + (d.get("slippage_apr") or 0.0)
            + (d.get("financing_apr") or 0.0)
        ),
        "ema_mature": meta.get("ema_mature"),
    }


def compactify(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim verbose nested combo/history structures for human review."""
    action = payload.get("action")
    if action == "discover":
        payload["ranked"] = [_compact_combo(c) for c in payload.get("ranked", [])]
        payload.pop("best_spot_leg_by_symbol", None)
    elif action == "quote":
        payload["quotes"] = [
            {
                **_compact_combo(q),
                "size_usd": q.get("size_usd"),
                "daily_net_carry_usd": q.get("daily_net_carry_usd"),
                "breakeven_days": q.get("breakeven_days"),
            }
            for q in payload.get("quotes", [])
        ]
    elif action == "status":
        payload.pop("recent_history", None)
    return payload


def _now() -> float:
    return time.time()


def _default_state() -> dict[str, Any]:
    return {
        "pairs": {},
        "funding_ema": {},
        "ema_seed_failed": {},
        "below_floor_since_ts": None,
        "executed_keys": {},
        "reference_value_usd": 0.0,
        "halted": False,
        "halt_reason": None,
        "stale_alerted": False,
        "history": [],
        "paper": {},
        "paper_hours": 0.0,
        "last_paper_tick_ts": None,
    }


def load_state() -> dict[str, Any]:
    state = read_monitor_state(STATE_NAME, _default_state())
    for key, value in _default_state().items():
        state.setdefault(key, value)
    return state


def save_state(state: dict[str, Any]) -> None:
    write_monitor_state(STATE_NAME, state)


def _already_executed(state: dict[str, Any], key: str) -> bool:
    return key in state["executed_keys"]


def _mark_executed(state: dict[str, Any], key: str) -> None:
    now = _now()
    state["executed_keys"][key] = now
    state["executed_keys"] = {
        k: ts for k, ts in state["executed_keys"].items() if now - ts < EXECUTED_KEY_TTL_S
    }


def _log_history(state: dict[str, Any], event: dict[str, Any]) -> None:
    state["history"] = (state["history"] + [{**event, "ts": _now()}])[-50:]


async def _notify(title: str, message: str) -> None:
    try:
        await NOTIFY_CLIENT.notify(title, message)
    except Exception as exc:
        logger.warning(f"notify failed: {exc}")


# ---------------------------------------------------------------------------
# Wallet resolution (session-connected wallet preferred; see skill notes)
# ---------------------------------------------------------------------------

async def _resolve_wallet_label(config: dict[str, Any]) -> str:
    configured = str(config.get("wallet") or "").strip()
    wallets = await load_wallets()
    labels = [str(w.get("label") or "").strip() for w in wallets if w.get("label")]
    remote_labels = [
        str(w.get("label") or "").strip()
        for w in wallets
        if w.get("type") == "remote" and w.get("label")
    ]
    if configured and configured in remote_labels:
        return configured
    if len(remote_labels) == 1:
        return remote_labels[0]
    if configured and configured in labels:
        return configured
    if len(labels) == 1:
        return labels[0]
    raise SystemExit(
        f"Wallet '{configured or '(unset)'}' not found; available: {labels or 'none'}. "
        "Set 'wallet' in inputs/config.yaml."
    )


async def _resolve_operating_label(config: dict[str, Any], main_label: str) -> str:
    strategy_label = str(config.get("strategy_wallet") or "").strip()
    if strategy_label and await find_wallet_by_label(strategy_label) is not None:
        return strategy_label
    return main_label


# ---------------------------------------------------------------------------
# Context: adapters + hedge + legs wiring (paper wrappers when mode=paper)
# ---------------------------------------------------------------------------

class Ctx:
    config: dict[str, Any]
    universe: dict[str, Any]
    state: dict[str, Any]
    main_label: str
    label: str
    address: str
    paper: bool
    hedge: HedgeVenue
    live_hedge: HyperliquidHedge
    legs: dict[str, SpotLeg]
    executor: PairExecutor
    brap: BRAPAdapter
    ledger: LedgerAdapter | None
    boros_lock: BorosRateLock | None


async def _token_price(token_id: str) -> float | None:
    try:
        details = await TOKEN_CLIENT.get_token_details(token_id, market_data=True)
        price = details.get("current_price")
        return float(price) if price is not None else None
    except Exception as exc:
        logger.warning(f"price lookup failed for {token_id}: {exc}")
        return None


async def _weeth_yield_apy(_symbol: str) -> float | None:
    """ether.fi staking APY via Delta Lab (weETH yield-token feed)."""
    try:
        found = await DELTA_LAB_CLIENT.search_assets(query="weETH", chain_id=1)
        for asset in found.get("assets", []):
            if str(asset.get("symbol", "")).upper() != "WEETH":
                continue
            latest = await DELTA_LAB_CLIENT.get_asset_yield_latest(
                asset_id=int(asset["asset_id"])
            )
            if latest is not None and latest.apy_base is not None:
                return float(latest.apy_base)
        return None
    except Exception as exc:
        logger.warning(f"weETH yield lookup failed: {exc}")
        return None


async def build_ctx(
    config: dict[str, Any],
    universe: dict[str, Any],
    state: dict[str, Any],
    *,
    need_boros: bool = False,
) -> Ctx:
    ctx = Ctx()
    ctx.config = config
    ctx.universe = universe
    ctx.state = state
    ctx.paper = str(config.get("mode") or "live").lower() == "paper"
    ctx.main_label = await _resolve_wallet_label(config)
    ctx.label = await _resolve_operating_label(config, ctx.main_label)

    hl_adapter = await get_adapter(HyperliquidAdapter, ctx.label)
    _, ctx.address = await get_wallet_signing_callback(ctx.label)
    builder_fee = (CONFIG.get("strategy") or {}).get("builder_fee")

    ctx.brap = await get_adapter(BRAPAdapter, ctx.label)
    pendle = await get_adapter(PendleAdapter, ctx.label)
    ethena = await get_adapter(EthenaVaultAdapter, ctx.label)

    ctx.ledger = None
    if bool(config.get("ledger_record", True)):
        try:
            ctx.ledger = await get_adapter(LedgerAdapter)
        except Exception as exc:
            logger.warning(f"ledger adapter unavailable: {exc}")

    ctx.live_hedge = HyperliquidHedge(hl_adapter, ctx.address, builder_fee)

    address = ctx.address

    async def weeth_balance() -> float:
        raw = await get_token_balance(WEETH_MAINNET, chain_id=1, wallet_address=address)
        return raw / 1e18

    async def susde_balance() -> float:
        raw = await get_token_balance(SUSDE_MAINNET, chain_id=1, wallet_address=address)
        return raw / 1e18

    pendle_cfg = config.get("pendle") or {}
    live_legs: dict[str, SpotLeg] = {
        "hl_spot": HlSpotLeg(
            hl_adapter,
            ctx.address,
            builder_fee,
            slippage=float(config.get("slippage_bps", 25)) / 10_000,
        ),
        "pendle_pt": PendlePtLeg(
            pendle,
            ctx.address,
            min_liquidity_usd=float(pendle_cfg.get("min_liquidity_usd", 250_000)),
            min_days_to_expiry=float(pendle_cfg.get("min_days_to_expiry", 7)),
            slippage=float(config.get("slippage_bps", 25)) / 10_000,
        ),
        "etherfi": EtherfiLeg(
            ctx.brap,
            ctx.address,
            balance_lookup=weeth_balance,
            yield_lookup=_weeth_yield_apy,
            price_lookup=_token_price,
        ),
        "ethena": EthenaLeg(
            ctx.brap,
            ctx.address,
            ethena_adapter=ethena,
            balance_lookup=susde_balance,
            price_lookup=_token_price,
        ),
    }
    enabled = [name for name in (config.get("spot_legs") or list(live_legs)) if name in live_legs]
    live_legs = {name: live_legs[name] for name in enabled}

    if ctx.paper:
        slippage_bps = float(config.get("slippage_bps", 25))
        paper_state = state["paper"]
        ctx.hedge = PaperHedge(ctx.live_hedge, paper_state, slippage_bps=slippage_bps)

        def paper_price_fn(leg_name: str):
            async def price(symbol: str) -> float | None:
                if leg_name == "hl_spot":
                    return await ctx.live_hedge.mark_price(symbol)
                if leg_name == "etherfi":
                    return await _token_price(WEETH_TOKEN_ID)
                if leg_name == "ethena":
                    return await _token_price(SUSDE_TOKEN_ID)
                return None  # pendle_pt marks at entry value

            return price

        ctx.legs = {
            name: PaperSpotLeg(
                leg, paper_state, slippage_bps=slippage_bps, price_fn=paper_price_fn(name)
            )
            for name, leg in live_legs.items()
        }
    else:
        ctx.hedge = ctx.live_hedge
        ctx.legs = live_legs

    ctx.executor = PairExecutor(ctx.hedge, ctx.legs)

    ctx.boros_lock = None
    rate_lock_cfg = config.get("rate_lock") or {}
    if need_boros or bool(rate_lock_cfg.get("enabled")):
        boros = await get_adapter(BorosAdapter, ctx.label)
        ctx.boros_lock = BorosRateLock(boros)
    return ctx


# ---------------------------------------------------------------------------
# Collection + scoring (orchestration; math in scoring.py)
# ---------------------------------------------------------------------------

MAX_EMA_SEEDS_PER_RUN = 20
EMA_SEED_RETRY_S = 6 * 3600


async def _seed_funding_ema(ctx: Ctx, symbol: str, ema_hours: float) -> float | None:
    """Annualized mean of realized funding over the EMA window, or None."""
    failed_at = ctx.state["ema_seed_failed"].get(symbol)
    if failed_at is not None and _now() - float(failed_at) < EMA_SEED_RETRY_S:
        return None
    try:
        end_ms = int(_now() * 1000)
        start_ms = end_ms - int(ema_hours * 3600 * 1000)
        entries = await HYPERLIQUID_DATA_CLIENT.get_funding_history(symbol, start_ms, end_ms)
        rates = []
        for entry in entries:
            value = entry.get("fundingRate", entry.get("funding_rate"))
            if value is not None:
                rates.append(float(value))
        if not rates:
            raise RuntimeError("empty funding history")
        mean_rate = sum(rates) / len(rates)
        return normalize_funding_apr(mean_rate, ctx.hedge.funding_interval_hours)
    except Exception as exc:
        logger.warning(f"EMA history seed failed for {symbol}: {exc}")
        ctx.state["ema_seed_failed"][symbol] = _now()
        return None


async def collect_market_rows(
    ctx: Ctx, excluded: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Per-symbol funding/EMA/oi rows for the whitelist (+ dynamic discovery).

    When `excluded` is provided, symbols dropped by the OI/funding filters are
    appended as `{symbol, reason}` so callers (discover) can explain the drops.
    """
    scoring_cfg = ctx.config.get("scoring") or {}
    filters = ctx.universe.get("filters") or {}
    ema_hours = float(scoring_cfg.get("funding_ema_hours", 72))

    snapshot = await ctx.hedge.perp_snapshot()
    whitelist = {str(s).upper() for s in (ctx.universe.get("symbols") or [])}
    symbols = set(whitelist)

    delta_lab_funding: dict[str, Any] = {}
    if bool(ctx.universe.get("allow_dynamic_discovery")):
        try:
            screened = await DELTA_LAB_CLIENT.screen_perp(
                sort="funding_now", order="desc", limit=100, venue="hyperliquid"
            )
            for row in screened.get("data", []):
                sym = str(row.get("base_symbol") or "").upper()
                if not sym:
                    continue
                delta_lab_funding[sym] = row.get("funding_now")
                if sym in snapshot:
                    symbols.add(sym)
        except Exception as exc:
            logger.warning(f"Delta Lab dynamic discovery unavailable: {exc}")

    now = _now()
    rows: dict[str, dict[str, Any]] = {}
    seeds_this_run = 0
    # Whitelist symbols seed before dynamically-discovered ones so a flood of
    # discovered alts can't starve core markets of the per-run seed budget.
    for sym in sorted(symbols, key=lambda s: (s not in whitelist, s)):
        snap = snapshot.get(sym)
        if snap is None:
            continue
        funding_apr_now = normalize_funding_apr(
            snap["funding_per_interval"], ctx.hedge.funding_interval_hours
        )
        ema_key = f"{ctx.hedge.name}:{sym}"
        ema_state = ctx.state["funding_ema"].get(ema_key) or {}
        seeded_from_history = bool(ema_state.get("seeded_from_history"))
        if not seeded_from_history and seeds_this_run < MAX_EMA_SEEDS_PER_RUN:
            # Seed (or re-seed) the EMA from realized funding history until it
            # takes — not only on first sight — so a one-interval spike can't
            # masquerade as a 72h EMA. A symbol whose first-sight seed was
            # skipped (per-run budget) or failed (transient API) would otherwise
            # stay spot-seeded and immature for a full EMA window instead of
            # maturing as soon as history is available.
            seeds_this_run += 1
            seed = await _seed_funding_ema(ctx, sym, ema_hours)
            if seed is not None:
                ema_state = {"ema_apr": seed, "last_sample_ts": now - 3600.0}
                seeded_from_history = True
        last_ts = ema_state.get("last_sample_ts")
        dt_hours = min((now - last_ts) / 3600.0, 6.0) if last_ts else 1.0
        ema = update_ema(
            ema_state.get("ema_apr"), funding_apr_now, ema_alpha(dt_hours, ema_hours)
        )
        first_sample_ts = float(ema_state.get("first_sample_ts") or now)
        ctx.state["funding_ema"][ema_key] = {
            "ema_apr": ema,
            "last_sample_ts": now,
            "first_sample_ts": first_sample_ts,
            "seeded_from_history": seeded_from_history,
        }
        # A history-seeded EMA is trustworthy immediately; a spot-seeded one
        # (history API failed) only matures after a full EMA window of live
        # samples — until then it's a spike wearing an EMA label.
        ema_mature = seeded_from_history or (now - first_sample_ts) >= ema_hours * 3600.0
        rows[sym] = {
            "symbol": sym,
            "funding_apr_now": funding_apr_now,
            "funding_ema_apr": ema,
            "ema_seeded_from_history": seeded_from_history,
            "ema_mature": ema_mature,
            "mark_price": snap["mark_price"],
            "open_interest_usd": snap["open_interest_usd"],
            "delta_lab_funding_now": delta_lab_funding.get(sym),
            "last_sample_ts": now,
        }

    min_oi = float(filters.get("min_oi_usd", 0))
    min_funding_bps = float(filters.get("min_funding_apr_bps", 0))
    open_symbols = set(ctx.state["pairs"])
    kept: dict[str, dict[str, Any]] = {}
    for sym, row in rows.items():
        if sym in open_symbols:
            kept[sym] = row
            continue
        if row["open_interest_usd"] < min_oi:
            if excluded is not None:
                excluded.append({
                    "symbol": sym,
                    "reason": f"open_interest ${row['open_interest_usd']:,.0f} "
                    f"< min_oi_usd ${min_oi:,.0f}",
                })
            continue
        funding_bps = row["funding_ema_apr"] * 10_000
        if funding_bps < min_funding_bps:
            if excluded is not None:
                excluded.append({
                    "symbol": sym,
                    "reason": f"funding EMA {funding_bps:.0f}bps "
                    f"< min_funding_apr_bps {min_funding_bps:.0f}bps",
                })
            continue
        kept[sym] = row
    return kept


async def apply_volatility_filter(
    ctx: Ctx,
    rows: dict[str, dict[str, Any]],
    excluded: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Drop symbols above the realized-vol cap (30d daily). Discover-only.

    Records `{symbol, reason}` into `excluded` for symbols dropped over the cap.
    """
    filters = ctx.universe.get("filters") or {}
    max_vol_pct = filters.get("max_lookback_volatility_pct")
    if max_vol_pct is None:
        return rows
    end_ms = int(_now() * 1000)
    start_ms = end_ms - 30 * 86400 * 1000
    kept: dict[str, dict[str, Any]] = {}
    for sym, row in rows.items():
        try:
            candles = await HYPERLIQUID_DATA_CLIENT.get_candles(sym, start_ms, end_ms, "1d")
            closes = [float(v) for c in candles if (v := c.get("c")) is not None]
            if len(closes) < 5:
                kept[sym] = row
                continue
            returns = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0]
            mean = sum(returns) / len(returns)
            var = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
            vol_pct = math.sqrt(var) * math.sqrt(365) * 100
            row["realized_vol_pct_30d"] = round(vol_pct, 2)
            if vol_pct <= float(max_vol_pct) or sym in ctx.state["pairs"]:
                kept[sym] = row
            elif excluded is not None:
                excluded.append({
                    "symbol": sym,
                    "reason": f"realized_vol {vol_pct:.0f}% "
                    f"> max_lookback_volatility_pct {float(max_vol_pct):.0f}%",
                })
        except Exception as exc:
            logger.warning(f"vol filter unavailable for {sym}: {exc}")
            kept[sym] = row
    return kept


def _cost_config(ctx: Ctx) -> tuple[float, dict[str, float], float, float]:
    costs = ctx.config.get("costs") or {}
    taker = float(costs.get("hedge_taker_fee_bps", DEFAULT_HEDGE_TAKER_FEE_BPS))
    spot_costs = {**DEFAULT_SPOT_COST_BPS, **(costs.get("spot_cost_bps") or {})}
    amort_days = float(
        (ctx.config.get("scoring") or {}).get(
            "cost_amortization_days", DEFAULT_COST_AMORTIZATION_DAYS
        )
    )
    # Expected slippage per fill for scoring — distinct from `slippage_bps`,
    # the protective execution tolerance (scoring with the tolerance would
    # overstate the drag ~5x and block every combo).
    slippage_cost_bps = float(
        costs.get("slippage_cost_bps_per_fill", DEFAULT_SLIPPAGE_COST_BPS_PER_FILL)
    )
    return taker, spot_costs, amort_days, slippage_cost_bps


async def build_combos(
    ctx: Ctx, rows: dict[str, dict[str, Any]]
) -> tuple[list[ComboScore], list[dict[str, Any]]]:
    taker, spot_costs, amort_days, slippage_cost_bps = _cost_config(ctx)
    priority = [name for name in (ctx.config.get("spot_legs") or []) if name in ctx.legs]
    combos: list[ComboScore] = []
    exclusions: list[dict[str, Any]] = []
    for sym, row in rows.items():
        for leg_name in priority:
            leg = ctx.legs[leg_name]
            try:
                if not await leg.supports(sym):
                    continue
                apy = await leg.yield_apy(sym)
            except Exception as exc:
                exclusions.append({"symbol": sym, "leg": leg_name, "reason": str(exc)})
                continue
            if apy is None:
                exclusions.append(
                    {"symbol": sym, "leg": leg_name, "reason": "yield data unavailable"}
                )
                continue
            # Round trip: hedge entry+exit + spot entry+exit; slippage on all 4 fills.
            fee_bps = 2 * taker + 2 * spot_costs.get(leg_name, 25.0)
            combos.append(
                ComboScore(
                    venue=ctx.hedge.name,
                    symbol=sym,
                    spot_leg=leg_name,
                    components=CarryComponents(
                        funding_apr=row["funding_ema_apr"],
                        spot_leg_apy=float(apy),
                        fee_apr=cost_apr(fee_bps, amort_days),
                        slippage_apr=cost_apr(4 * slippage_cost_bps, amort_days),
                    ),
                    funding_interval_hours=ctx.hedge.funding_interval_hours,
                    meta={
                        "funding_apr_now": row["funding_apr_now"],
                        "mark_price": row["mark_price"],
                        "open_interest_usd": row["open_interest_usd"],
                        "ema_mature": bool(row.get("ema_mature", False)),
                        "ema_seeded_from_history": bool(
                            row.get("ema_seeded_from_history", False)
                        ),
                        "delta_lab_funding_now": row.get("delta_lab_funding_now"),
                        "realized_vol_pct_30d": row.get("realized_vol_pct_30d"),
                    },
                )
            )
    return rank_combos(combos), exclusions


def _combo_for(combos: list[ComboScore], symbol: str, leg: str) -> ComboScore | None:
    for combo in combos:
        if combo.symbol == symbol.upper() and combo.spot_leg == leg:
            return combo
    return None


async def _migration_cost_usd(
    ctx: Ctx,
    notional_usd: float,
    from_leg: str,
    from_symbol: str,
    to_leg: str,
    to_symbol: str,
) -> float:
    taker, spot_costs, _amort, slippage_cost_bps = _cost_config(ctx)
    close_bps = taker + spot_costs.get(from_leg, 25.0) + 2 * slippage_cost_bps
    open_bps = taker + spot_costs.get(to_leg, 25.0) + 2 * slippage_cost_bps
    gas = await _leg_gas_usd(ctx, from_leg, from_symbol) + await _leg_gas_usd(
        ctx, to_leg, to_symbol
    )
    return notional_usd * (close_bps + open_bps) / 10_000 + gas


# ---------------------------------------------------------------------------
# Funding + gas plumbing
# ---------------------------------------------------------------------------

async def _ensure_hl_funding(ctx: Ctx, needed_usd: float) -> tuple[bool, str]:
    """Top up the HL account from wallet Arbitrum USDC when short."""
    free = await ctx.hedge.free_margin_usd()
    if free >= needed_usd:
        return True, f"HL free margin ${free:.2f} covers ${needed_usd:.2f}"
    shortfall = max(needed_usd - free, MIN_DEPOSIT_USD)
    if ctx.paper:
        ctx.state["paper"]["usdc"] = float(ctx.state["paper"].get("usdc", 0.0)) + shortfall
        return True, f"paper: credited ${shortfall:.2f} virtual USDC"
    wallet = await find_wallet_by_label(ctx.label)
    if wallet is None:
        return False, f"wallet {ctx.label!r} not found"
    sign_cb, _ = await get_wallet_signing_callback(ctx.label)
    balance = await get_adapter(BalanceAdapter, ctx.main_label, ctx.label)
    ok, res = await balance.send_to_address(
        token_id=USDC_ARBITRUM_TOKEN_ID,
        amount=int(shortfall * 1e6),
        from_wallet=wallet,
        to_address=HYPERLIQUID_BRIDGE_ADDRESS,
        signing_callback=sign_cb,
    )
    if not ok:
        return False, f"USDC bridge to HL failed: {res}"
    confirmed, final_balance = await ctx.live_hedge.adapter.wait_for_deposit(
        address=ctx.address, expected_increase=shortfall, timeout_s=240, poll_interval_s=10
    )
    if not confirmed:
        return False, (
            f"USDC sent to HL bridge but not credited within timeout "
            f"(HL balance ${final_balance:.2f})"
        )
    return True, f"bridged ${shortfall:.2f} USDC to HL (balance ${final_balance:.2f})"


async def _leg_usdc_requirement(
    ctx: Ctx, leg_name: str, symbol: str
) -> tuple[int, str] | None:
    """(chain_id, USDC address) the spot leg draws from, or None for HL legs.

    Pendle resolves to the actual PT market's chain — Base/Arbitrum markets
    do NOT need mainnet USDC.
    """
    if leg_name in ("etherfi", "ethena"):
        return 1, USDC_MAINNET
    if leg_name == "pendle_pt":
        leg = ctx.legs.get("pendle_pt")
        if leg is None:
            return None
        target = getattr(leg, "live", leg)  # paper wrapper delegates discovery
        if not isinstance(target, PendlePtLeg):
            return None
        market = await target.find_market(symbol)
        if market is None:
            return None
        chain_id = int(market["chainId"])
        return chain_id, LEG_USDC_BY_CHAIN[chain_id]
    return None


async def _leg_chain_ids(ctx: Ctx, leg_name: str, symbol: str) -> list[int]:
    requirement = await _leg_usdc_requirement(ctx, leg_name, symbol)
    return [requirement[0]] if requirement else []


async def _leg_gas_usd(ctx: Ctx, leg_name: str, symbol: str) -> float:
    """Per-migration gas for one spot leg, priced by the chain it executes on."""
    if leg_name == "hl_spot":
        return 0.0
    requirement = await _leg_usdc_requirement(ctx, leg_name, symbol)
    chain_id = requirement[0] if requirement else 1
    return EVM_LEG_GAS_USD_BY_CHAIN.get(chain_id, DEFAULT_EVM_LEG_GAS_USD)


async def _intended_lot(ctx: Ctx, leg_name: str, symbol: str) -> dict[str, Any]:
    """Lot identity resolved before opening (exact PT market for Pendle)."""
    if leg_name != "pendle_pt":
        return {}
    leg = ctx.legs.get("pendle_pt")
    target = getattr(leg, "live", leg)
    if not isinstance(target, PendlePtLeg):
        return {}
    market = await target.find_market(symbol)
    if market is None:
        return {}
    return {"pt_address": market.get("ptAddress"), "chain_id": market.get("chainId")}


async def _check_gas(ctx: Ctx, chain_ids: list[int]) -> list[int]:
    """Chains where the wallet lacks native gas."""
    if ctx.paper:
        return []
    starved: list[int] = []
    for chain_id in chain_ids:
        try:
            async with web3_from_chain_id(chain_id) as w3:
                balance = await w3.eth.get_balance(ctx.address)
            if balance < MIN_GAS_WEI:
                starved.append(chain_id)
        except Exception as exc:
            logger.warning(f"gas check failed on chain {chain_id}: {exc}")
    return starved


async def _record_ledger(
    ctx: Ctx,
    *,
    kind: str,
    usd_value: float,
    data: dict[str, Any],
    chain_id: int = 1,  # must match the USDC_MAINNET default token below
    token_address: str = "",
    token_amount: float = 0.0,
) -> None:
    if ctx.ledger is None:
        return
    try:
        if kind == "deposit":
            await ctx.ledger.record_deposit(
                wallet_address=ctx.address,
                chain_id=chain_id,
                token_address=token_address or USDC_MAINNET,
                token_amount=token_amount or usd_value,
                usd_value=usd_value,
                data=data,
                strategy_name=PATH_SLUG,
            )
        else:
            await ctx.ledger.record_withdrawal(
                wallet_address=ctx.address,
                chain_id=chain_id,
                token_address=token_address or USDC_MAINNET,
                token_amount=token_amount or usd_value,
                usd_value=usd_value,
                data=data,
                strategy_name=PATH_SLUG,
            )
    except Exception as exc:
        logger.warning(f"ledger record failed ({kind}): {exc}")


# ---------------------------------------------------------------------------
# Pair valuation / status helpers
# ---------------------------------------------------------------------------

async def _pair_snapshot(ctx: Ctx, symbol: str, pair: dict[str, Any]) -> dict[str, Any]:
    hedge_pos = await ctx.hedge.short_position(symbol)
    leg = ctx.legs.get(pair["spot_leg"])
    spot_pos = (
        await leg.position(symbol, pair.get("spot_lot") or None) if leg else None
    )
    spot_usd = spot_pos.usd_value if spot_pos and spot_pos.usd_value is not None else None
    # Cap at the recorded lot: wallet-wide balances may include unrelated
    # holdings that are not this pair's (and must never be valued or sold as such).
    lot_units = float((pair.get("spot_lot") or {}).get("units") or 0.0)
    if spot_pos and lot_units > 0 and spot_pos.units > lot_units:
        scale = lot_units / spot_pos.units
        if spot_usd is not None:
            spot_usd *= scale
        spot_pos = SpotPosition(
            spot_pos.leg, spot_pos.symbol, lot_units, spot_usd,
            {**spot_pos.meta, "lot_capped": True},
        )
    short_notional = hedge_pos.notional_usd if hedge_pos else 0.0
    delta_ratio = None
    if hedge_pos and short_notional > 0 and spot_usd is not None:
        delta_ratio = (spot_usd - short_notional) / short_notional
    value = (
        (spot_usd or 0.0)
        + (hedge_pos.margin_used_usd if hedge_pos else 0.0)
        + (hedge_pos.unrealized_pnl_usd if hedge_pos else 0.0)
    )
    days_held = (_now() - float(pair.get("opened_ts") or _now())) / 86400.0
    dwell_hours = days_held * 24.0
    min_dwell = float((ctx.config.get("rotation") or {}).get("min_dwell_hours", 24))
    return {
        "symbol": symbol,
        "spot_leg": pair["spot_leg"],
        "status": pair.get("status", "open"),
        "venue": pair.get("venue", ctx.hedge.name),
        "hedge": hedge_pos.to_dict() if hedge_pos else None,
        "spot": spot_pos.to_dict() if spot_pos else None,
        "delta_ratio": delta_ratio,
        "value_usd": round(value, 2),
        "entry_value_usd": pair.get("entry_value_usd"),
        "mtm_pnl_usd": (
            round(value - float(pair["entry_value_usd"]), 2)
            if pair.get("entry_value_usd") is not None
            else None
        ),
        "accrued_funding_usd_est": round(float(pair.get("accrued_funding_usd", 0.0)), 4),
        "accrued_spot_yield_usd_est": round(float(pair.get("accrued_spot_yield_usd", 0.0)), 4),
        "days_held": round(days_held, 2),
        "next_rotation_eval_in_hours": round(max(min_dwell - dwell_hours, 0.0), 1),
        "lock": pair.get("lock"),
    }


async def _lock_pnl(ctx: Ctx, pair: dict[str, Any]) -> Any:
    lock = pair.get("lock")
    if not lock:
        return None
    if ctx.paper:
        return lock.get("paper_pnl_usd", 0.0)
    if ctx.boros_lock is None:
        return None
    ok, positions = await ctx.boros_lock.lock_positions()
    if not ok or isinstance(positions, str):
        return None
    for pos in positions:
        if int(pos.get("market_id") or -1) == int(lock.get("market_id") or -2):
            return pos.get("pnl")
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def action_discover(ctx: Ctx, top_n: int) -> dict[str, Any]:
    excluded: list[dict[str, Any]] = []
    rows = await collect_market_rows(ctx, excluded=excluded)
    rows = await apply_volatility_filter(ctx, rows, excluded=excluded)
    combos, leg_exclusions = await build_combos(ctx, rows)
    save_state(ctx.state)  # persist EMA updates
    best_by_symbol: dict[str, dict[str, Any]] = {}
    for combo in combos:
        best_by_symbol.setdefault(combo.symbol, combo.to_dict())
    return {
        "action": "discover",
        "mode": "paper" if ctx.paper else "live",
        "ranked": [c.to_dict() for c in combos[:top_n]],
        "best_spot_leg_by_symbol": best_by_symbol,
        "excluded": excluded + leg_exclusions,
        "note": (
            "net_apr = funding EMA + spot-leg yield − fees − slippage (amortized); "
            "positive funding = shorts receive"
        ),
    }


async def _quote_payload(ctx: Ctx, symbol: str, size_usd: float) -> dict[str, Any]:
    sym = symbol.upper()
    rows = await collect_market_rows(ctx)
    if sym not in rows:
        raise SystemExit(
            f"{sym} not in scannable universe (not listed, below min_oi_usd, or below "
            "min_funding_apr_bps — check inputs/universe.yaml)"
        )
    combos, exclusions = await build_combos(ctx, {sym: rows[sym]})
    if not combos:
        raise SystemExit(f"no spot leg available for {sym}: {exclusions}")
    quotes = []
    for combo in combos:
        entry_exit_cost = (
            await _migration_cost_usd(
                ctx, size_usd, combo.spot_leg, combo.symbol, combo.spot_leg, combo.symbol
            )
            / 2
        )
        round_trip_cost = entry_exit_cost * 2
        daily_carry = size_usd * combo.net_apr / 365.0
        quotes.append(
            {
                **combo.to_dict(),
                "size_usd": size_usd,
                "entry_cost_usd_est": round(entry_exit_cost, 2),
                "round_trip_cost_usd_est": round(round_trip_cost, 2),
                "daily_net_carry_usd": round(daily_carry, 4),
                "breakeven_days": (
                    round(round_trip_cost / daily_carry, 2) if daily_carry > 0 else None
                ),
            }
        )
    payload: dict[str, Any] = {
        "action": "quote",
        "symbol": sym,
        "mode": "paper" if ctx.paper else "live",
        "quotes": quotes,
        "excluded": exclusions,
    }
    rate_lock_cfg = ctx.config.get("rate_lock") or {}
    if bool(rate_lock_cfg.get("enabled")) and ctx.boros_lock is not None:
        mark = rows[sym]["mark_price"]
        ok, lock_quote = await ctx.boros_lock.quote_lock(
            sym, short_notional_usd=size_usd, short_size_units=size_usd / mark
        )
        if ok and isinstance(lock_quote, LockQuote):
            floating = rows[sym]["funding_ema_apr"]
            decision = lock_decision(
                floating,
                lock_quote.fixed_apr,
                premium_threshold_bps=int(rate_lock_cfg.get("lock_premium_threshold_apr_bps", 200)),
                locked=False,
            )
            payload["boros_lock"] = {
                **lock_quote.to_dict(),
                "floating_ema_apr": floating,
                "decision": decision.to_dict(),
            }
        else:
            payload["boros_lock"] = {"unavailable": str(lock_quote)}
    save_state(ctx.state)
    return payload


async def action_deposit(
    ctx: Ctx,
    symbol: str,
    amount: float,
    gas: float,
    leg_override: str | None,
    confirm: bool,
    *,
    skip_paper_gate: bool = False,
) -> dict[str, Any]:
    sym = symbol.upper()
    _check_mode_consistency(ctx)
    risk = ctx.config.get("risk") or {}
    hedge_cfg = ctx.config.get("hedge") or {}
    scoring_cfg = ctx.config.get("scoring") or {}
    if ctx.state["halted"]:
        raise SystemExit(f"halted: {ctx.state['halt_reason']} — run update --confirm --resume")
    if bool(ctx.config.get("paused")):
        raise SystemExit("paused: true in inputs/config.yaml — kill switch active")
    if not ctx.paper and not skip_paper_gate:
        gate_hours = float(ctx.config.get("paper_gate_hours", 48))
        paper_hours = float(ctx.state.get("paper_hours", 0.0))
        if gate_hours > 0 and paper_hours < gate_hours:
            raise SystemExit(
                f"paper gate: {paper_hours:.1f}h of paper updates recorded < required "
                f"{gate_hours:.0f}h. Run `mode: paper` updates under the runner first, "
                "or pass --skip-paper-gate to override deliberately."
            )
    if sym in ctx.state["pairs"]:
        pair_status = ctx.state["pairs"][sym].get("status", "open")
        raise SystemExit(
            f"{sym} pair already recorded (status={pair_status}) — use update/rotate, "
            f"or `unwind --symbol {sym} --confirm` to clear a half-open pair"
        )
    max_position = float(risk.get("max_position_usd", 5_000))
    if amount > max_position:
        raise SystemExit(f"amount ${amount:.2f} > max_position_usd ${max_position:.2f}")
    open_notional = 0.0
    for open_sym, pair in ctx.state["pairs"].items():
        snap = await _pair_snapshot(ctx, open_sym, pair)
        open_notional += (snap.get("hedge") or {}).get("notional_usd") or 0.0
    max_total = float(risk.get("max_total_notional_usd", 10_000))
    if open_notional + amount > max_total:
        raise SystemExit(
            f"total notional ${open_notional + amount:.2f} would exceed "
            f"max_total_notional_usd ${max_total:.2f}"
        )
    if amount < MIN_ORDER_USD_NOTIONAL:
        raise SystemExit(f"amount below HL ${MIN_ORDER_USD_NOTIONAL:.0f} order minimum")

    quote = await _quote_payload(ctx, sym, amount)
    quotes_by_leg = {q["spot_leg"]: q for q in quote["quotes"]}
    if leg_override:
        if leg_override not in quotes_by_leg:
            raise SystemExit(f"leg {leg_override!r} unavailable for {sym}")
        chosen = quotes_by_leg[leg_override]
    else:
        chosen = quote["quotes"][0]
    min_net_bps = float(scoring_cfg.get("min_net_carry_apr_bps", 1000))
    if chosen["net_apr"] * 10_000 < min_net_bps:
        raise SystemExit(
            f"net carry {chosen['net_apr'] * 10_000:.0f}bps below "
            f"min_net_carry_apr_bps {min_net_bps:.0f}"
        )
    if not (chosen.get("meta") or {}).get("ema_mature", False):
        raise SystemExit(
            f"{sym} funding EMA is immature (history seeding unavailable and the "
            "EMA window hasn't elapsed) — the current reading could be a "
            "one-interval spike. Wait for history to seed or the window to fill."
        )

    leverage = int(hedge_cfg.get("leverage_cap", 3))
    margin_needed = required_margin_usd(
        amount, leverage, float(hedge_cfg.get("margin_buffer_pct", 0.25))
    )
    leg_name = chosen["spot_leg"]
    hl_needed = margin_needed + (amount if leg_name == "hl_spot" else 0.0)

    plan = {
        "action": "deposit",
        "symbol": sym,
        "spot_leg": leg_name,
        "notional_usd": amount,
        "hedge_margin_usd": round(margin_needed, 2),
        "hl_funding_needed_usd": round(hl_needed, 2),
        "quote": chosen,
        "mode": "paper" if ctx.paper else "live",
    }
    if not confirm:
        return {**plan, "status": "requires_confirmation", "note": "re-run with --confirm"}

    key = idempotency_key(PATH_SLUG, ctx.hedge.name, sym, "deposit", epoch_bucket(_now()))
    if _already_executed(ctx.state, key):
        return {**plan, "status": "skipped", "note": f"idempotency key {key} already executed"}

    # All read-only pre-position checks run BEFORE anything moves funds (gas
    # top-up, HL bridging) — a transient read failure must abort a deposit
    # that hasn't spent anything yet, not one that already bridged.
    # The intended lot identity (exact PT market for Pendle) is resolved here
    # so every later read binds to it, never to whichever wallet holding
    # shares the symbol root.
    intended_lot = await _intended_lot(ctx, leg_name, sym)
    # Pre-open spot units: the pair's lot is the balance DELTA, so unrelated
    # holdings the wallet already had are never attributed to (or sold by)
    # this pair. Refusing on a failed read is deliberate — a fund-moving open
    # must not guess the baseline in a shared wallet.
    try:
        pre_spot = await ctx.legs[leg_name].position(sym, intended_lot or None)
        pre_spot_units = pre_spot.units if pre_spot else 0.0
    except Exception as exc:
        raise SystemExit(
            f"pre-open spot balance read failed for {sym}/{leg_name}: {exc} — "
            "aborting so existing wallet holdings cannot be misattributed to this pair"
        ) from exc

    starved = await _check_gas(ctx, await _leg_chain_ids(ctx, leg_name, sym))
    if starved and gas > 0:
        gas_results = []
        for chain_id in starved:
            native = {1: "ethereum-ethereum", 8453: "ethereum-base", 42161: "ethereum-arbitrum"}[
                chain_id
            ]
            ok, res = await ctx.brap.swap_from_token_ids(
                from_token_id=USDC_ARBITRUM_TOKEN_ID,
                to_token_id=native,
                from_address=ctx.address,
                amount=str(int(gas * 1e6)),
                strategy_name=PATH_SLUG,
            )
            gas_results.append({"chain_id": chain_id, "ok": ok, "result": str(res)[:200]})
        plan["gas_topups"] = gas_results
        starved = await _check_gas(ctx, await _leg_chain_ids(ctx, leg_name, sym))
    if starved:
        raise SystemExit(
            f"no native gas on chains {starved} — fund gas there (or pass --gas) before deposit"
        )

    if not ctx.paper:
        # Verify the spot leg's own USDC BEFORE the hedge opens — otherwise a
        # hedge-first entry fails at the spot step by construction.
        requirement = await _leg_usdc_requirement(ctx, leg_name, sym)
        if requirement is not None:
            req_chain, req_usdc = requirement
            usdc_balance = (
                await get_token_balance(
                    req_usdc, chain_id=req_chain, wallet_address=ctx.address
                )
            ) / 10**6
            if usdc_balance < amount:
                raise SystemExit(
                    f"{leg_name} leg needs ${amount:.2f} USDC on chain {req_chain}; "
                    f"wallet has ${usdc_balance:.2f} — fund it before deposit"
                )
            plan["leg_funding"] = {
                "chain_id": req_chain,
                "usdc_balance": round(usdc_balance, 2),
            }

    ok, msg = await _ensure_hl_funding(ctx, hl_needed)
    if not ok:
        raise SystemExit(msg)
    plan["hl_funding"] = msg

    if ctx.paper and leg_name != "hl_spot":
        # EVM legs draw from the same virtual USDC pool in paper mode.
        ctx.state["paper"]["usdc"] = float(ctx.state["paper"].get("usdc", 0.0)) + amount

    slippage = float(ctx.config.get("slippage_bps", 25)) / 10_000
    # Durable pre-record: a crash or partial failure mid-open must leave a
    # recoverable pair in state so `unwind --symbol` closes exactly what filled.
    ctx.state["pairs"][sym] = {
        "spot_leg": leg_name,
        "venue": ctx.hedge.name,
        "opened_ts": _now(),
        "entry_notional_usd": amount,
        "entry_value_usd": None,
        "last_rebalance_ts": None,
        "accrued_funding_usd": 0.0,
        "accrued_spot_yield_usd": 0.0,
        "last_accrual_ts": _now(),
        "lock": None,
        "spot_lot": {**intended_lot, "units": None},
        "pre_spot_units": pre_spot_units,
        "status": "opening",
        "mode": "paper" if ctx.paper else "live",
    }
    save_state(ctx.state)
    ok, report = await ctx.executor.open_pair(
        sym, leg_name, amount, leverage=leverage, slippage=slippage
    )
    plan["execution"] = report
    if not ok:
        if open_failure_leaves_exposure(report):
            # Record what actually filled so recovery closes ONLY that.
            post_units = pre_spot_units
            try:
                post_pos = await ctx.legs[leg_name].position(sym, intended_lot or None)
                post_units = post_pos.units if post_pos else 0.0
            except Exception as exc:
                logger.warning(f"post-failure spot read failed for {sym}: {exc}")
            failure_lot = _lot_from_open(report, pre_spot_units, post_units)
            ctx.state["pairs"][sym].update(
                {"status": "half_open", "spot_lot": {**intended_lot, **failure_lot}}
            )
            _log_history(
                ctx.state,
                {"event": "half_open", "symbol": sym, "error": report.get("error")},
            )
            await _notify(
                f"{PATH_SLUG}: half-open pair on {sym}",
                f"Open failed with live exposure: {report.get('error')}. "
                f"Recover with: unwind --symbol {sym} --confirm",
            )
        else:
            ctx.state["pairs"].pop(sym, None)
        save_state(ctx.state)
        raise SystemExit(json.dumps(report, default=str))

    _mark_executed(ctx.state, key)
    hedge_pos = await ctx.hedge.short_position(sym)
    leg_pos = await ctx.legs[leg_name].position(sym, intended_lot or None)
    lot = {
        **intended_lot,
        **_lot_from_open(report, pre_spot_units, leg_pos.units if leg_pos else 0.0),
    }
    lot_fraction = (
        min(lot["units"] / leg_pos.units, 1.0) if leg_pos and leg_pos.units > 0 else 1.0
    )
    spot_value = (
        leg_pos.usd_value * lot_fraction
        if leg_pos and leg_pos.usd_value is not None
        else amount
    )
    entry_value = spot_value + (hedge_pos.margin_used_usd if hedge_pos else margin_needed)
    ctx.state["pairs"][sym].update(
        {"entry_value_usd": entry_value, "status": "open", "spot_lot": lot}
    )
    ctx.state["reference_value_usd"] = float(ctx.state["reference_value_usd"]) + entry_value
    _log_history(ctx.state, {"event": "deposit", "symbol": sym, "leg": leg_name, "usd": amount})
    await _record_ledger(
        ctx, kind="deposit", usd_value=amount, data={"idempotency_key": key, "leg": leg_name}
    )
    save_state(ctx.state)
    return {**plan, "status": "executed"}


def _lot_from_open(
    report: dict[str, Any], pre_units: float, post_units: float
) -> dict[str, Any]:
    """The pair's spot lot: exactly what THIS open filled, nothing else."""
    lot: dict[str, Any] = {}
    steps = {s.get("step"): s.get("result") or {} for s in report.get("steps", [])}
    if "paired_atomic" in steps:
        lot["units"] = float(steps["paired_atomic"].get("filled_spot") or 0.0)
    elif "spot_open" in steps:
        result = steps["spot_open"]
        if result.get("units") is not None:
            lot["units"] = float(result["units"])
        market = result.get("market")
        if isinstance(market, dict):
            lot["pt_address"] = market.get("ptAddress")
            lot["chain_id"] = market.get("chainId")
    if float(lot.get("units") or 0.0) <= 0.0:
        lot["units"] = max(post_units - pre_units, 0.0)
    return lot


async def _close_pair_full(
    ctx: Ctx, symbol: str, *, reason: str, confirm: bool
) -> dict[str, Any]:
    sym = symbol.upper()
    pair = ctx.state["pairs"].get(sym)
    if pair is None:
        raise SystemExit(f"no open pair for {sym}")
    before = await _pair_snapshot(ctx, sym, pair)
    plan = {"symbol": sym, "reason": reason, "before": before}
    if not confirm:
        return {**plan, "status": "requires_confirmation"}

    if pair.get("lock") and ctx.boros_lock is not None and not ctx.paper:
        ok, res = await ctx.boros_lock.unwind_lock(int(pair["lock"]["market_id"]))
        plan["lock_unwind"] = res
        if not ok:
            raise SystemExit(f"Boros lock unwind failed, aborting pair close: {res}")
    elif pair.get("lock") and ctx.paper:
        plan["lock_unwind"] = {"paper": True}

    slippage = float(ctx.config.get("slippage_bps", 25)) / 10_000
    lot = pair.get("spot_lot") or {}
    if lot.get("units") is not None:
        close_units: float | None = float(lot["units"])
    elif pair.get("pre_spot_units") is not None:
        # Interrupted open (crash before the lot was finalized): close only
        # what the open added on top of the recorded baseline.
        leg = ctx.legs.get(pair["spot_leg"])
        pos = await leg.position(sym, lot or None) if leg else None
        close_units = max(
            (pos.units if pos else 0.0) - float(pair["pre_spot_units"]), 0.0
        )
    else:
        raise SystemExit(
            f"{sym} pair has no lot record — refusing a wallet-wide close. Verify "
            "balances and set pairs[sym].spot_lot.units in the state file first."
        )
    # Durable closing marker: a crash between the spot close and the hedge
    # close must never leave a naked short recorded as a healthy pair.
    pair["status"] = "closing"
    save_state(ctx.state)
    ok, report = await ctx.executor.close_pair(
        sym,
        pair["spot_leg"],
        slippage=slippage,
        units=close_units,
        lot=lot or None,
    )
    plan["execution"] = report
    if not ok:
        steps = {s.get("step"): s for s in report.get("steps", [])}
        if (steps.get("spot_close") or {}).get("ok"):
            # Spot is gone but the hedge refused to close: that's a naked
            # short, not a healthy carry pair — impair it so update suspends
            # carry actions instead of scoring it.
            pair["status"] = "impaired"
            _log_history(
                ctx.state,
                {"event": "impaired", "symbol": sym,
                 "error": f"hedge close failed after spot closed: {report.get('error')}"},
            )
            await _notify(
                f"{PATH_SLUG}: naked short on {sym}",
                f"Spot leg closed but the hedge close failed: {report.get('error')}. "
                f"Retry unwind --symbol {sym} --confirm",
            )
        save_state(ctx.state)
        raise SystemExit(json.dumps(report, default=str))

    realized_vs_entry = None
    if pair.get("entry_value_usd") is not None and before.get("value_usd") is not None:
        realized_vs_entry = round(before["value_usd"] - float(pair["entry_value_usd"]), 2)
    plan["reconciliation"] = {
        "entry_value_usd": pair.get("entry_value_usd"),
        "exit_value_usd_est": before.get("value_usd"),
        "realized_vs_entry_usd": realized_vs_entry,
        "accrued_funding_usd_est": pair.get("accrued_funding_usd"),
        "accrued_spot_yield_usd_est": pair.get("accrued_spot_yield_usd"),
    }
    ctx.state["reference_value_usd"] = max(
        0.0, float(ctx.state["reference_value_usd"]) - float(pair.get("entry_value_usd") or 0.0)
    )
    ctx.state["pairs"].pop(sym, None)
    _log_history(ctx.state, {"event": "unwind", "symbol": sym, "reason": reason})
    await _record_ledger(
        ctx,
        kind="withdrawal",
        usd_value=float(before.get("value_usd") or 0.0),
        data={"reason": reason, "reconciliation": plan["reconciliation"]},
    )
    save_state(ctx.state)
    return {**plan, "status": "executed"}


ARB_USDC_ADDRESS = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
# Haircut on freed-capital estimates for close-side slippage and fees.
ROTATION_PROCEEDS_HAIRCUT = 0.98


async def _preflight_rotation_open(
    ctx: Ctx,
    symbol: str,
    amount: float,
    *,
    to_leg: str,
    candidate_net_apr: float,
    candidate_ema_mature: bool,
    from_snapshot: dict[str, Any],
    from_leg: str,
    from_lot: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Verify the destination pair can open with the funds the close will free.

    Conservative: proceeds land where the closing leg lives (HL for hl_spot,
    mainnet USDC for EVM legs) and are NOT re-routed across venues in v1.0 —
    an unverifiable destination skips the migration, leaving the current pair
    untouched.
    """
    risk = ctx.config.get("risk") or {}
    scoring_cfg = ctx.config.get("scoring") or {}
    hedge_cfg = ctx.config.get("hedge") or {}
    if amount < MIN_ORDER_USD_NOTIONAL:
        return False, f"target amount ${amount:.2f} below HL order minimum"
    if amount > float(risk.get("max_position_usd", 5_000)):
        return False, f"target amount ${amount:.2f} exceeds max_position_usd"
    min_net_bps = float(scoring_cfg.get("min_net_carry_apr_bps", 1000))
    if candidate_net_apr * 10_000 < min_net_bps:
        return False, (
            f"candidate net carry {candidate_net_apr * 10_000:.0f}bps below "
            f"min_net_carry_apr_bps {min_net_bps:.0f} — deposit would refuse it"
        )
    if not candidate_ema_mature:
        return False, "candidate funding EMA is immature — deposit would refuse it"
    starved = await _check_gas(ctx, await _leg_chain_ids(ctx, to_leg, symbol))
    if starved:
        return False, f"no native gas on chains {starved} for leg {to_leg}"
    if ctx.paper:
        return True, "preflight ok (paper)"

    hedge_part = from_snapshot.get("hedge") or {}
    spot_part = from_snapshot.get("spot") or {}
    freed_margin = max(
        float(hedge_part.get("margin_used_usd") or 0.0)
        + float(hedge_part.get("unrealized_pnl_usd") or 0.0),
        0.0,
    )
    spot_proceeds = float(spot_part.get("usd_value") or 0.0)
    hl_freed = freed_margin + (spot_proceeds if from_leg == "hl_spot" else 0.0)
    hl_freed *= ROTATION_PROCEEDS_HAIRCUT
    # Where the closing spot leg's proceeds land: BRAP legs swap back to
    # mainnet USDC; a Pendle close pays USDC on the PT market's own chain.
    proceeds_chain: int | None = None
    if from_leg in ("etherfi", "ethena"):
        proceeds_chain = 1
    elif from_leg == "pendle_pt":
        chain = (from_lot or {}).get("chain_id")
        proceeds_chain = int(chain) if chain is not None else None

    leverage = int(hedge_cfg.get("leverage_cap", 3))
    margin_needed = required_margin_usd(
        amount, leverage, float(hedge_cfg.get("margin_buffer_pct", 0.25))
    )
    required_hl = margin_needed + (amount if to_leg == "hl_spot" else 0.0)
    hl_available = await ctx.hedge.free_margin_usd() + hl_freed
    if hl_available < required_hl:
        shortfall = required_hl - hl_available
        try:
            arb_usdc = (
                await get_token_balance(
                    ARB_USDC_ADDRESS, chain_id=42161, wallet_address=ctx.address
                )
            ) / 1e6
        except Exception as exc:
            return False, f"cannot verify Arbitrum USDC for HL top-up: {exc}"
        if arb_usdc < shortfall:
            return False, (
                f"HL funding shortfall ${shortfall:.2f} exceeds wallet Arbitrum "
                f"USDC ${arb_usdc:.2f}"
            )
    requirement = await _leg_usdc_requirement(ctx, to_leg, symbol)
    if requirement is not None:
        req_chain, req_usdc = requirement
        freed_here = (
            spot_proceeds * ROTATION_PROCEEDS_HAIRCUT
            if proceeds_chain == req_chain
            else 0.0
        )
        try:
            chain_usdc = (
                await get_token_balance(
                    req_usdc, chain_id=req_chain, wallet_address=ctx.address
                )
            ) / 1e6
        except Exception as exc:
            return False, f"cannot verify chain-{req_chain} USDC for {to_leg} leg: {exc}"
        if chain_usdc + freed_here < amount:
            return False, (
                f"{to_leg} leg needs ${amount:.2f} USDC on chain {req_chain}; available "
                f"${chain_usdc:.2f} + freed ${freed_here:.2f} — cross-chain "
                "proceeds routing lands in v1.1"
            )
    return True, "preflight ok"


async def _reduce_pair(
    ctx: Ctx, symbol: str, pair: dict[str, Any], *, fraction: float, slippage: float
) -> tuple[bool, dict[str, Any]]:
    """Shrink BOTH legs proportionally, hedge first.

    Reducing only the hedge would break delta-neutrality and the next delta
    rebalance would re-open the short, undoing the risk reduction. Failure
    handling keeps risk monotone: a failed hedge reduce aborts before the spot
    leg is touched (the pair stays symmetric at full size); a spot failure
    after the hedge shrank leaves excess long exposure, so the pair is marked
    `impaired` — carry actions suspend and the operator is alerted.

    On success the reduced slice is treated as returned capital:
    entry_notional/entry_value scale down and the drawdown reference releases
    the freed portion, so a deliberate de-risk never reads as a loss.
    """
    fraction = min(max(fraction, 0.0), 1.0)
    results: dict[str, Any] = {"fraction": round(fraction, 4)}
    is_open = pair.get("status", "open") == "open"
    lot = pair.get("spot_lot") or {}
    lot_units = float(lot.get("units") or 0.0)
    if is_open and lot_units <= 0:
        # Refuse before touching either leg: reducing the hedge without a
        # trusted spot lot would end asymmetric with nothing safe to trim.
        results["error"] = "open pair has no lot record — refusing to reduce"
        return False, results
    hedge_pos = await ctx.hedge.short_position(symbol)
    if hedge_pos and hedge_pos.size_units > 0:
        ok, res = await ctx.hedge.close_short(
            symbol, hedge_pos.size_units * fraction, slippage
        )
        results["hedge_reduce"] = res
        if not ok:
            results["error"] = f"hedge reduce failed, spot untouched: {res.get('error')}"
            return False, results
    if not is_open:
        # Recovery-state pair: there is no trusted matched spot, so shrinking
        # the short IS the de-risk — never touch wallet spot balances here.
        results["note"] = "non-open pair: hedge-only de-risk"
        pair["last_rebalance_ts"] = _now()
        return True, results
    leg = ctx.legs.get(pair["spot_leg"])
    spot_pos = await leg.position(symbol, lot or None) if leg else None
    base_units = min(spot_pos.units, lot_units) if spot_pos else 0.0
    if leg and base_units > 0:
        ok, res = await leg.close(symbol, base_units * fraction, lot or None)
        results["spot_reduce"] = res
        if not ok:
            pair["status"] = "impaired"
            results["error"] = (
                f"spot reduce failed after hedge shrank — pair impaired: {res.get('error')}"
            )
            _log_history(
                ctx.state,
                {"event": "impaired", "symbol": symbol, "error": results["error"]},
            )
            await _notify(
                f"{PATH_SLUG}: impaired pair on {symbol}",
                f"{results['error']}. Carry actions suspended; resolve with "
                f"unwind --symbol {symbol} --confirm",
            )
            return False, results
    pair["last_rebalance_ts"] = _now()
    remaining = 1.0 - fraction
    pair["entry_notional_usd"] = float(pair.get("entry_notional_usd") or 0.0) * remaining
    if lot_units > 0:
        pair["spot_lot"] = {**lot, "units": lot_units * remaining}
    entry_value = pair.get("entry_value_usd")
    if entry_value is not None:
        released = float(entry_value) * fraction
        pair["entry_value_usd"] = float(entry_value) * remaining
        ctx.state["reference_value_usd"] = max(
            0.0, float(ctx.state["reference_value_usd"]) - released
        )
        results["released_reference_usd"] = round(released, 2)
    return True, results


async def _evaluate_rotation(
    ctx: Ctx, combos: list[ComboScore], *, bypass_dwell: bool
) -> list[dict[str, Any]]:
    rotation_cfg = ctx.config.get("rotation") or {}
    evaluations = []
    for sym, pair in ctx.state["pairs"].items():
        if pair.get("status", "open") != "open":
            continue
        current = _combo_for(combos, sym, pair["spot_leg"])
        candidates = [
            c for c in combos if not (c.symbol == sym and c.spot_leg == pair["spot_leg"])
        ]
        if current is None or not candidates:
            continue
        best = candidates[0]
        snap = await ctx.hedge.short_position(sym)
        notional = snap.notional_usd if snap else float(pair.get("entry_notional_usd") or 0.0)
        cost = await _migration_cost_usd(
            ctx, notional, pair["spot_leg"], sym, best.spot_leg, best.symbol
        )
        hours_held = (_now() - float(pair.get("opened_ts") or _now())) / 3600.0
        decision = rotation_decision(
            current.net_apr,
            best.net_apr,
            notional_usd=notional,
            migration_cost_usd=cost,
            threshold_apr_bps=int(rotation_cfg.get("threshold_apr_bps", 400)),
            max_breakeven_hours=float(rotation_cfg.get("max_breakeven_hours", 48)),
            min_dwell_hours=float(rotation_cfg.get("min_dwell_hours", 24)),
            hours_held=hours_held,
            bypass_dwell=bypass_dwell,
        )
        evaluations.append(
            {
                "symbol": sym,
                "current": current.to_dict(),
                "candidate": best.to_dict(),
                "migration_cost_usd": round(cost, 2),
                "decision": decision.to_dict(),
            }
        )
    return evaluations


async def _execute_rotation(ctx: Ctx, evaluation: dict[str, Any]) -> dict[str, Any]:
    sym = evaluation["symbol"]
    candidate = evaluation["candidate"]
    key = idempotency_key(
        PATH_SLUG, ctx.hedge.name, sym, f"rotate_to_{candidate['symbol']}", epoch_bucket(_now())
    )
    if _already_executed(ctx.state, key):
        return {"status": "skipped", "note": f"idempotency key {key} already executed"}
    pair = ctx.state["pairs"][sym]
    notional = float(pair.get("entry_notional_usd") or 0.0)
    before = await _pair_snapshot(ctx, sym, pair)
    target_amount = min(float(before.get("value_usd") or notional), notional) or notional

    # Prove the replacement can open BEFORE closing anything — otherwise a
    # gas/funding/carry failure downstream turns a migration into a full exit.
    ok_pre, pre_msg = await _preflight_rotation_open(
        ctx,
        candidate["symbol"],
        target_amount,
        to_leg=candidate["spot_leg"],
        candidate_net_apr=float(candidate["net_apr"]),
        candidate_ema_mature=bool((candidate.get("meta") or {}).get("ema_mature", False)),
        from_snapshot=before,
        from_leg=pair["spot_leg"],
        from_lot=pair.get("spot_lot"),
    )
    if not ok_pre:
        return {"status": "skipped", "note": f"preflight failed: {pre_msg}"}

    closed = await _close_pair_full(ctx, sym, reason="rotation", confirm=True)
    recovered = float(
        (closed.get("reconciliation") or {}).get("exit_value_usd_est") or notional
    )
    try:
        opened = await action_deposit(
            ctx,
            candidate["symbol"],
            min(recovered, notional) if notional else recovered,
            gas=0.0,
            leg_override=candidate["spot_leg"],
            confirm=True,
            skip_paper_gate=True,  # capital was already live in the closed pair
        )
    except SystemExit as exc:
        # Old pair is closed, new one refused: funds sit in stables. Surface
        # it loudly instead of letting it read like a routine exit.
        _log_history(
            ctx.state,
            {"event": "rotation_reopen_failed", "from": sym, "to": candidate["symbol"],
             "error": str(exc)},
        )
        save_state(ctx.state)
        await _notify(
            f"{PATH_SLUG}: rotation re-open failed",
            f"Closed {sym} for rotation to {candidate['symbol']}/{candidate['spot_leg']} "
            f"but the new deposit refused: {exc}. Funds are settled in stables — "
            "re-deposit manually or let the next update retry.",
        )
        return {
            "status": "reopen_failed",
            "closed": closed,
            "error": str(exc),
            "note": "funds settled to stables; no new position was opened",
        }
    _mark_executed(ctx.state, key)
    _log_history(
        ctx.state,
        {"event": "rotation", "from": sym, "to": candidate["symbol"], "leg": candidate["spot_leg"]},
    )
    save_state(ctx.state)
    return {"status": "executed", "closed": closed, "opened": opened}


async def _accrue_estimates(ctx: Ctx, rows: dict[str, dict[str, Any]]) -> None:
    now = _now()
    for sym, pair in ctx.state["pairs"].items():
        row = rows.get(sym)
        last = float(pair.get("last_accrual_ts") or now)
        hours = max(0.0, (now - last) / 3600.0)
        if row is None or hours <= 0 or pair.get("status", "open") != "open":
            pair["last_accrual_ts"] = now
            continue
        snap = await ctx.hedge.short_position(sym)
        notional = snap.notional_usd if snap else float(pair.get("entry_notional_usd") or 0.0)
        pair["accrued_funding_usd"] = float(pair.get("accrued_funding_usd", 0.0)) + (
            notional * row["funding_apr_now"] * hours / HOURS_PER_YEAR
        )
        leg = ctx.legs.get(pair["spot_leg"])
        apy = await leg.yield_apy(sym) if leg else None
        if apy:
            pair["accrued_spot_yield_usd"] = float(pair.get("accrued_spot_yield_usd", 0.0)) + (
                notional * float(apy) * hours / HOURS_PER_YEAR
            )
        pair["last_accrual_ts"] = now


async def _boros_step(
    ctx: Ctx, rows: dict[str, dict[str, Any]], *, confirm: bool
) -> list[dict[str, Any]]:
    rate_lock_cfg = ctx.config.get("rate_lock") or {}
    if not bool(rate_lock_cfg.get("enabled")) or ctx.boros_lock is None:
        return []
    threshold = int(rate_lock_cfg.get("lock_premium_threshold_apr_bps", 200))
    results = []
    for sym, pair in ctx.state["pairs"].items():
        row = rows.get(sym)
        if row is None or pair.get("status", "open") != "open":
            continue
        snap = await ctx.hedge.short_position(sym)
        if snap is None:
            continue
        locked = bool(pair.get("lock"))
        if locked:
            fixed = float(pair["lock"]["fixed_apr"])
            decision = lock_decision(
                row["funding_ema_apr"], fixed, premium_threshold_bps=threshold, locked=True
            )
            if decision.action == "unwind" and confirm:
                if ctx.paper:
                    pair["lock"] = None
                    results.append({"symbol": sym, "action": "unwind", "paper": True})
                else:
                    ok, res = await ctx.boros_lock.unwind_lock(int(pair["lock"]["market_id"]))
                    if ok:
                        pair["lock"] = None
                    results.append({"symbol": sym, "action": "unwind", "ok": ok, "result": res})
            else:
                results.append({"symbol": sym, "action": decision.action, **decision.to_dict()})
            continue
        ok, quote = await ctx.boros_lock.quote_lock(
            sym, short_notional_usd=snap.notional_usd, short_size_units=snap.size_units
        )
        if not ok or not isinstance(quote, LockQuote):
            results.append({"symbol": sym, "action": "none", "note": str(quote)})
            continue
        decision = lock_decision(
            row["funding_ema_apr"], quote.fixed_apr, premium_threshold_bps=threshold, locked=False
        )
        if decision.action == "open" and confirm:
            if ctx.paper:
                pair["lock"] = {**quote.to_dict(), "opened_ts": _now(), "paper": True}
                results.append({"symbol": sym, "action": "open", "paper": True, "quote": quote.to_dict()})
            else:
                ok_open, res = await ctx.boros_lock.open_lock(quote)
                if ok_open:
                    pair["lock"] = {**quote.to_dict(), "opened_ts": _now()}
                results.append({"symbol": sym, "action": "open", "ok": ok_open, "result": res})
        else:
            results.append(
                {"symbol": sym, "action": decision.action, **decision.to_dict(), "quote": quote.to_dict()}
            )
    return results


async def action_update(ctx: Ctx, *, confirm: bool, resume: bool) -> dict[str, Any]:
    _check_mode_consistency(ctx)
    config = ctx.config
    risk = config.get("risk") or {}
    hedge_cfg = config.get("hedge") or {}
    scoring_cfg = config.get("scoring") or {}
    report: dict[str, Any] = {
        "action": "update",
        "mode": "paper" if ctx.paper else "live",
        "confirm": confirm,
    }

    if ctx.state["halted"] and not resume:
        report["status"] = "halted"
        report["halt_reason"] = ctx.state["halt_reason"]
        report["note"] = "re-run with --confirm --resume after reviewing the halt"
        return report
    if resume and confirm:
        ctx.state["halted"] = False
        ctx.state["halt_reason"] = None
        save_state(ctx.state)
        report["resumed"] = True

    if ctx.paper:
        # Accumulate paper runtime toward the live gate. Hours only count for
        # confirmed update cycles with an open paper pair — idle or dry-run
        # ticks are not rehearsal. Capped per cycle so a long gap between two
        # runs doesn't count as continuous.
        has_open_pair = any(
            p.get("status", "open") == "open" for p in ctx.state["pairs"].values()
        )
        last_tick = ctx.state.get("last_paper_tick_ts")
        if last_tick and confirm and has_open_pair:
            ctx.state["paper_hours"] = float(ctx.state.get("paper_hours", 0.0)) + min(
                (_now() - float(last_tick)) / 3600.0, 1.0
            )
        ctx.state["last_paper_tick_ts"] = _now()
        report["paper_hours"] = round(float(ctx.state.get("paper_hours", 0.0)), 2)
        if not (confirm and has_open_pair):
            report["paper_hours_note"] = (
                "not accruing: gate hours require --confirm cycles with an open paper pair"
            )
            reason = "pass --confirm" if not confirm else "no open paper pair"
            report.setdefault("warnings", []).append(
                f"PAPER_HOURS_NOT_ACCRUING: {reason} — this cycle does not count "
                "toward the live-deposit gate"
            )

    # 1. Collect (also refreshes EMA state). Failure → stale path below.
    rows: dict[str, dict[str, Any]] = {}
    collect_error = None
    try:
        rows = await collect_market_rows(ctx)
    except Exception as exc:
        collect_error = str(exc)
        logger.error(f"market collection failed: {exc}")

    # 2. Safety rails run before any execution step, every cycle.
    rails: dict[str, Any] = {}
    stale_intervals = float(risk.get("stale_data_intervals", 2))
    stale_syms = [
        sym
        for sym in ctx.state["pairs"]
        if is_stale(
            (ctx.state["funding_ema"].get(f"{ctx.hedge.name}:{sym}") or {}).get("last_sample_ts"),
            _now(),
            funding_interval_hours=ctx.hedge.funding_interval_hours,
            max_intervals=stale_intervals,
        )
    ]
    rotation_frozen = bool(stale_syms) or collect_error is not None
    rails["stale"] = {
        "symbols": stale_syms,
        "collect_error": collect_error,
        "rotation_frozen": rotation_frozen,
    }
    if rotation_frozen and not ctx.state.get("stale_alerted"):
        await _notify(
            f"{PATH_SLUG}: stale funding data",
            f"Rotation frozen. stale={stale_syms} collect_error={collect_error}",
        )
        ctx.state["stale_alerted"] = True
    elif not rotation_frozen:
        ctx.state["stale_alerted"] = False

    # Kill switch: only delta + liquidation checks below, then return.
    paused = bool(config.get("paused"))

    half_open = [
        s for s, p in ctx.state["pairs"].items() if p.get("status", "open") != "open"
    ]
    rails["half_open_pairs"] = half_open
    if half_open:
        rails["half_open_note"] = (
            f"recover with: unwind --symbol <SYM> --confirm; carry actions are "
            f"suspended for {half_open}"
        )

    liq_buffer = float(risk.get("liq_buffer_pct", 0.15))
    leverage_cap = int(hedge_cfg.get("leverage_cap", 3))
    liq_reports = []
    reduced_syms: set[str] = set()
    total_value = 0.0
    for sym, pair in list(ctx.state["pairs"].items()):
        snap_pos = await ctx.hedge.short_position(sym)
        pair_snap = await _pair_snapshot(ctx, sym, pair)
        total_value += float(pair_snap.get("value_usd") or 0.0)
        if snap_pos is None:
            continue
        liq_distance = snap_pos.liq_distance_pct
        if liq_distance is None:
            continue
        margin_topup = required_margin_usd(snap_pos.notional_usd, leverage_cap, 0.0) * 0.25
        wallet_arb_usdc = 0.0
        if not ctx.paper:
            try:
                wallet_arb_usdc = (
                    await get_token_balance(
                        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                        chain_id=42161,
                        wallet_address=ctx.address,
                    )
                ) / 1e6
            except Exception:
                wallet_arb_usdc = 0.0
        else:
            wallet_arb_usdc = margin_topup  # paper can always credit
        act = liquidation_action(
            liq_distance,
            liq_buffer_pct=liq_buffer,
            available_margin_usd=wallet_arb_usdc,
            margin_topup_usd=margin_topup,
        )
        entry: dict[str, Any] = {
            "symbol": sym,
            "liq_distance_pct": round(liq_distance, 4),
            "action": act,
        }
        if act != "ok" and confirm:
            if act == "add_margin":
                ok, msg = await _ensure_hl_funding(
                    ctx, await ctx.hedge.free_margin_usd() + margin_topup
                )
                entry["executed"] = msg if ok else f"margin add failed: {msg}"
                if not ok:
                    act = "reduce"
            if act == "reduce":
                slippage = float(config.get("slippage_bps", 25)) / 10_000
                ok_reduce, reduce_res = await _reduce_pair(
                    ctx, sym, pair, fraction=0.25, slippage=slippage
                )
                entry["executed_reduce"] = reduce_res
                entry["reduce_ok"] = ok_reduce
                if not ok_reduce and "impaired" not in str(reduce_res.get("error")):
                    # Hedge reduce refused with the pair intact — escalate so
                    # the operator sees a liq guard that could not act.
                    await _notify(
                        f"{PATH_SLUG}: liquidation reduce failed on {sym}",
                        f"{reduce_res.get('error')} — position remains at full "
                        f"size with liq distance {entry['liq_distance_pct']}",
                    )
                reduced_syms.add(sym)
        liq_reports.append(entry)

        # Leverage-cap re-check: drift can push effective leverage above cap.
        if snap_pos.margin_used_usd > 0:
            effective = snap_pos.notional_usd / snap_pos.margin_used_usd
            if effective > leverage_cap * 1.25 and confirm and not paused:
                slippage = float(config.get("slippage_bps", 25)) / 10_000
                ok_reduce, reduce_res = await _reduce_pair(
                    ctx,
                    sym,
                    pair,
                    fraction=1 - leverage_cap / effective,
                    slippage=slippage,
                )
                liq_reports.append(
                    {
                        "symbol": sym,
                        "action": "leverage_cap_reduce",
                        "ok": ok_reduce,
                        "result": reduce_res,
                    }
                )
                reduced_syms.add(sym)
    rails["liquidation"] = liq_reports

    if reduced_syms:
        # Reductions just shrank positions AND released part of the drawdown
        # reference — comparing the pre-reduction value against the reduced
        # reference would mask a real drawdown. Re-mark everything.
        total_value = 0.0
        for sym, pair in ctx.state["pairs"].items():
            fresh = await _pair_snapshot(ctx, sym, pair)
            total_value += float(fresh.get("value_usd") or 0.0)

    reference = float(ctx.state["reference_value_usd"] or 0.0)
    max_dd = float(risk.get("max_drawdown_pct", 8))
    if ctx.state["pairs"] and drawdown_halted(reference, total_value, max_dd):
        ctx.state["halted"] = True
        ctx.state["halt_reason"] = (
            f"drawdown: value ${total_value:.2f} vs reference ${reference:.2f} "
            f"exceeds {max_dd}%"
        )
        save_state(ctx.state)
        await _notify(f"{PATH_SLUG}: drawdown halt", ctx.state["halt_reason"])
        report["rails"] = rails
        report["status"] = "halted"
        report["halt_reason"] = ctx.state["halt_reason"]
        return report
    rails["drawdown"] = {
        "reference_usd": reference,
        "value_usd": round(total_value, 2),
        "max_drawdown_pct": max_dd,
    }
    report["rails"] = rails

    if paused:
        report["status"] = "paused"
        report["note"] = "kill switch active: only delta + liquidation checks ran"
        save_state(ctx.state)
        return report

    await _accrue_estimates(ctx, rows)

    # 3-5. Score + negative-carry exit. Missing/stale data must never age
    # into an unwind: with no scoreable combos the floor clock freezes
    # instead of scoring "no data" as 0% carry. Only MATURE combos count as
    # alternatives — an immature EMA spike isn't depositable, so it must
    # neither keep the book deployed nor attract a rotation. Held pairs are
    # always mature (deposit and rotation refuse immature entries).
    combos, exclusions = await build_combos(ctx, rows) if rows else ([], [])
    mature_combos = [c for c in combos if c.meta.get("ema_mature")]
    report["excluded_combos"] = exclusions
    report["immature_combos"] = [
        f"{c.symbol}/{c.spot_leg}" for c in combos if not c.meta.get("ema_mature")
    ]
    floor_bps = int(scoring_cfg.get("unwind_carry_floor_bps", 200))
    grace_hours = float(scoring_cfg.get("grace_hours", 12))
    if rotation_frozen or not mature_combos:
        report["negative_carry"] = {
            "skipped": "market data stale or unscoreable — floor clock frozen",
            "below_floor_since_ts": ctx.state.get("below_floor_since_ts"),
        }
        if ctx.state["pairs"] and not mature_combos and not rotation_frozen:
            await _notify(
                f"{PATH_SLUG}: open pairs unscoreable",
                f"Collect succeeded but no mature combo could be scored "
                f"(exclusions: {exclusions}). Carry decisions are suspended.",
            )
    else:
        best_apr = mature_combos[0].net_apr
        should_exit, since = negative_carry_exit(
            best_apr,
            floor_bps=floor_bps,
            below_floor_since_ts=ctx.state.get("below_floor_since_ts"),
            now_ts=_now(),
            grace_hours=grace_hours,
        )
        ctx.state["below_floor_since_ts"] = since
        report["negative_carry"] = {
            "best_net_apr": best_apr,
            "floor_bps": floor_bps,
            "below_floor_since_ts": since,
            "exit": should_exit,
        }
        if should_exit and ctx.state["pairs"]:
            if confirm:
                unwinds = [
                    await _close_pair_full(ctx, sym, reason="negative_carry", confirm=True)
                    for sym in list(ctx.state["pairs"])
                ]
                ctx.state["below_floor_since_ts"] = None
                report["negative_carry"]["unwinds"] = unwinds
            else:
                report["negative_carry"]["planned"] = "unwind all pairs (requires --confirm)"
            save_state(ctx.state)
            report["status"] = "executed" if confirm else "requires_confirmation"
            return report

    # 6. Rotation (frozen while data is stale).
    if not rotation_frozen:
        evaluations = await _evaluate_rotation(ctx, mature_combos, bypass_dwell=False)
        report["rotation"] = evaluations
        migrations = [e for e in evaluations if e["decision"]["migrate"]]
        if migrations:
            if confirm:
                # One migration per cycle keeps the blast radius bounded.
                report["rotation_executed"] = await _execute_rotation(ctx, migrations[0])
            else:
                report["rotation_planned"] = migrations
    else:
        report["rotation"] = "frozen (stale data)"

    # 7. Delta check with churn guard. Pairs the liq/leverage guard reduced
    # this cycle are skipped — rebalancing them would re-open the short the
    # guard just shrank.
    band_pct = float(hedge_cfg.get("target_delta_band_pct", 1.5))
    delta_reports = []
    for sym, pair in ctx.state["pairs"].items():
        if sym in reduced_syms or pair.get("status", "open") != "open":
            continue
        pair_snap = await _pair_snapshot(ctx, sym, pair)
        delta_ratio = pair_snap.get("delta_ratio")
        if delta_ratio is None:
            continue
        last_reb = pair.get("last_rebalance_ts")
        hours_since = (_now() - last_reb) / 3600.0 if last_reb else None
        if delta_rebalance_decision(
            delta_ratio, band_pct=band_pct, hours_since_last_rebalance=hours_since
        ):
            entry = {"symbol": sym, "delta_ratio": round(delta_ratio, 4), "action": "rebalance"}
            if confirm:
                slippage = float(config.get("slippage_bps", 25)) / 10_000
                hedge_pos = await ctx.hedge.short_position(sym)
                delta_usd = abs(delta_ratio) * (hedge_pos.notional_usd if hedge_pos else 0.0)
                if delta_usd >= MIN_ORDER_USD_NOTIONAL and hedge_pos:
                    if delta_ratio > 0:
                        ok, res = await ctx.hedge.open_short(sym, delta_usd, slippage)
                    else:
                        units = delta_usd / (hedge_pos.mark_price or 1.0)
                        ok, res = await ctx.hedge.close_short(sym, units, slippage)
                    entry["executed"] = res
                    pair["last_rebalance_ts"] = _now()
                else:
                    entry["note"] = "delta below HL order minimum"
            delta_reports.append(entry)
    report["delta"] = delta_reports

    # 8. Boros rate-lock decision.
    report["rate_lock"] = await _boros_step(ctx, rows, confirm=confirm)

    # 9. Persist state (EMA, accruals, executed keys are the ledger of record
    # for idempotency; capital flows were recorded per action).
    save_state(ctx.state)
    report["status"] = "executed" if confirm else "evaluated"
    return report


async def action_rotate(ctx: Ctx, *, force: bool, confirm: bool) -> dict[str, Any]:
    _check_mode_consistency(ctx)
    rows = await collect_market_rows(ctx)
    combos, exclusions = await build_combos(ctx, rows)
    mature_combos = [c for c in combos if c.meta.get("ema_mature")]
    evaluations = await _evaluate_rotation(ctx, mature_combos, bypass_dwell=force)
    save_state(ctx.state)
    report: dict[str, Any] = {
        "action": "rotate",
        "force": force,
        "evaluations": evaluations,
        "excluded_combos": exclusions,
    }
    migrations = [e for e in evaluations if e["decision"]["migrate"]]
    if not migrations:
        report["status"] = "no_migration"
        return report
    if not confirm:
        report["status"] = "requires_confirmation"
        return report
    report["executed"] = await _execute_rotation(ctx, migrations[0])
    report["status"] = "executed"
    return report


async def action_lock(
    ctx: Ctx, symbol: str, tenor: float | None, *, confirm: bool
) -> dict[str, Any]:
    _check_mode_consistency(ctx)
    sym = symbol.upper()
    pair = ctx.state["pairs"].get(sym)
    if pair is None:
        raise SystemExit(f"no open pair for {sym} — locks size to the short leg")
    if pair.get("status", "open") != "open":
        raise SystemExit(
            f"{sym} pair is {pair.get('status')} — locks only apply to healthy open "
            f"pairs; recover it first (unwind --symbol {sym} --confirm)"
        )
    if pair.get("lock"):
        raise SystemExit(f"{sym} already locked (market {pair['lock']['market_id']})")
    if ctx.boros_lock is None:
        raise SystemExit("Boros unavailable — set rate_lock.enabled or retry")
    snap = await ctx.hedge.short_position(sym)
    if snap is None:
        raise SystemExit(f"no live short for {sym}")
    ok, quote = await ctx.boros_lock.quote_lock(
        sym,
        short_notional_usd=snap.notional_usd,
        short_size_units=snap.size_units,
        target_tenor_days=tenor,
    )
    if not ok or not isinstance(quote, LockQuote):
        raise SystemExit(str(quote))
    payload = {"action": "lock", "symbol": sym, "quote": quote.to_dict()}
    if not confirm:
        return {**payload, "status": "requires_confirmation"}
    if ctx.paper:
        pair["lock"] = {**quote.to_dict(), "opened_ts": _now(), "paper": True}
        save_state(ctx.state)
        return {**payload, "status": "executed", "paper": True}
    ok, res = await ctx.boros_lock.open_lock(quote)
    if not ok:
        raise SystemExit(json.dumps(res, default=str))
    pair["lock"] = {**quote.to_dict(), "opened_ts": _now()}
    _log_history(ctx.state, {"event": "lock", "symbol": sym, "market_id": quote.market_id})
    save_state(ctx.state)
    return {**payload, "status": "executed", "result": res}


async def action_unlock(ctx: Ctx, symbol: str, *, confirm: bool) -> dict[str, Any]:
    _check_mode_consistency(ctx)
    sym = symbol.upper()
    pair = ctx.state["pairs"].get(sym)
    if pair is None or not pair.get("lock"):
        raise SystemExit(f"no lock open for {sym}")
    payload = {"action": "unlock", "symbol": sym, "lock": pair["lock"]}
    if not confirm:
        return {**payload, "status": "requires_confirmation"}
    if ctx.paper:
        pair["lock"] = None
        save_state(ctx.state)
        return {**payload, "status": "executed", "paper": True}
    if ctx.boros_lock is None:
        raise SystemExit("Boros unavailable")
    ok, res = await ctx.boros_lock.unwind_lock(int(pair["lock"]["market_id"]))
    if not ok:
        raise SystemExit(json.dumps(res, default=str))
    pair["lock"] = None
    _log_history(ctx.state, {"event": "unlock", "symbol": sym})
    save_state(ctx.state)
    return {**payload, "status": "executed", "result": res}


async def action_status(ctx: Ctx) -> dict[str, Any]:
    # Status must stay readable for inspection, but positions are read through
    # the CURRENT mode's wrappers — flag mismatched pairs as unreliable.
    mode_mismatch = _mode_mismatched_pairs(ctx)
    pairs = []
    for sym, pair in ctx.state["pairs"].items():
        snap = await _pair_snapshot(ctx, sym, pair)
        snap["lock_pnl_usd"] = await _lock_pnl(ctx, pair)
        ema = ctx.state["funding_ema"].get(f"{ctx.hedge.name}:{sym}") or {}
        snap["funding_ema_apr"] = ema.get("ema_apr")
        pairs.append(snap)
    free_margin = None
    try:
        free_margin = await ctx.hedge.free_margin_usd()
    except Exception as exc:
        logger.warning(f"free margin read failed: {exc}")
    return {
        "action": "status",
        "mode": "paper" if ctx.paper else "live",
        "mode_mismatch_warning": (
            f"pairs {mode_mismatch} were not opened in the configured mode — "
            "their positions below are read through the wrong wrappers and are "
            "unreliable; switch mode back before acting"
            if mode_mismatch
            else None
        ),
        "wallet": ctx.label,
        "address": ctx.address,
        "halted": ctx.state["halted"],
        "halt_reason": ctx.state["halt_reason"],
        "paused": bool(ctx.config.get("paused")),
        "pairs": pairs,
        "hl_free_margin_usd": free_margin,
        "reference_value_usd": ctx.state["reference_value_usd"],
        "below_floor_since_ts": ctx.state["below_floor_since_ts"],
        "paper_state": ctx.state["paper"] if ctx.paper else None,
        "recent_history": ctx.state["history"][-10:],
    }


async def action_unwind(ctx: Ctx, symbol: str | None, *, confirm: bool) -> dict[str, Any]:
    _check_mode_consistency(ctx)
    targets = [symbol.upper()] if symbol else list(ctx.state["pairs"])
    if not targets:
        return {"action": "unwind", "status": "no_open_pairs"}
    results = [
        await _close_pair_full(ctx, sym, reason="manual_unwind", confirm=confirm)
        for sym in targets
    ]
    return {
        "action": "unwind",
        "status": "executed" if confirm else "requires_confirmation",
        "pairs": results,
    }


async def action_exit(ctx: Ctx, *, confirm: bool) -> dict[str, Any]:
    _check_mode_consistency(ctx)
    if ctx.state["pairs"]:
        raise SystemExit(
            f"open pairs remain: {list(ctx.state['pairs'])} — run unwind first, then exit"
        )
    report: dict[str, Any] = {"action": "exit", "mode": "paper" if ctx.paper else "live"}
    if ctx.paper:
        report["paper_final"] = ctx.state["paper"]
        report["status"] = "executed"
        return report
    free = await ctx.hedge.free_margin_usd()
    steps = []
    plan = {"hl_withdraw_usd": round(free, 2), "transfer_to": ctx.main_label}
    if not confirm:
        return {**report, "plan": plan, "status": "requires_confirmation"}
    if free > 2.0:  # HL withdraw carries a ~$1 fee; dust isn't worth it
        ok, res = await ctx.live_hedge.adapter.withdraw(amount=free, address=ctx.address)
        steps.append({"step": "hl_withdraw", "ok": ok, "result": str(res)[:200]})
        if not ok:
            raise SystemExit(f"HL withdraw failed: {res}")
    if ctx.label != ctx.main_label:
        main_wallet = await find_wallet_by_label(ctx.main_label)
        main_address = str((main_wallet or {}).get("address") or "")
        if not main_address:
            raise SystemExit(f"main wallet {ctx.main_label!r} has no address")
        sign_cb, _ = await get_wallet_signing_callback(ctx.label)
        wallet = await find_wallet_by_label(ctx.label)
        if wallet is None:
            raise SystemExit(f"wallet {ctx.label!r} not found")
        balance = await get_adapter(BalanceAdapter, ctx.main_label, ctx.label)
        for token_id, chain_id, address, decimals in (
            (USDC_ARBITRUM_TOKEN_ID, 42161, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
            ("usd-coin-ethereum", 1, USDC_MAINNET, 6),
        ):
            raw = await get_token_balance(address, chain_id=chain_id, wallet_address=ctx.address)
            if raw <= 0:
                continue
            ok, res = await balance.send_to_address(
                token_id=token_id,
                amount=raw,
                from_wallet=wallet,
                to_address=main_address,
                signing_callback=sign_cb,
            )
            steps.append(
                {"step": f"transfer_{token_id}", "ok": ok, "usd": raw / 10**decimals}
            )
            if not ok:
                raise SystemExit(f"transfer {token_id} failed: {res}")
    else:
        steps.append({"step": "transfer", "note": "operating wallet is the main wallet"})
    await _record_ledger(ctx, kind="withdrawal", usd_value=free, data={"event": "exit"})
    _log_history(ctx.state, {"event": "exit"})
    save_state(ctx.state)
    return {**report, "steps": steps, "status": "executed"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "discover", "quote", "deposit", "update", "rotate",
            "lock", "unlock", "status", "unwind", "exit",
        ],
    )
    parser.add_argument("--symbol", help="asset symbol, e.g. ETH")
    # One USD-notional flag for both quote and deposit. --size is a deprecated
    # alias so a value passed under either name can't be silently masked by the
    # other's default; quote falls back to 1000 only when neither is given.
    parser.add_argument(
        "--amount",
        "--size",
        dest="amount",
        type=float,
        default=None,
        help="USD notional (quote defaults to 1000 if omitted; deposit requires it)",
    )
    parser.add_argument("--gas", type=float, default=0.0, help="USD of USDC to convert to gas")
    parser.add_argument("--leg", help="override spot leg selection for deposit")
    parser.add_argument("--top", type=int, default=10, help="rows for discover")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="trim verbose nested combo/history structures for human review",
    )
    parser.add_argument("--tenor", type=float, help="target Boros tenor in days for lock")
    parser.add_argument("--force", action="store_true", help="rotate: relax the dwell gate (breakeven still applies)")
    parser.add_argument("--confirm", action="store_true", help="execute fund-moving steps")
    parser.add_argument("--resume", action="store_true", help="update: clear a drawdown halt")
    parser.add_argument(
        "--skip-paper-gate",
        action="store_true",
        help="deposit: open live positions without the recorded paper-mode hours",
    )
    return parser


def _mode_mismatched_pairs(ctx: Ctx) -> list[str]:
    """Pairs whose recorded mode disagrees with (or is missing vs) config mode.

    A record without a mode is treated as a mismatch, not assumed safe — it
    predates the guard and needs an explicit `mode` set in the state file.
    """
    current = "paper" if ctx.paper else "live"
    return [s for s, p in ctx.state["pairs"].items() if p.get("mode") != current]


def _check_mode_consistency(ctx: Ctx) -> None:
    """Refuse to act on pairs opened in the other mode.

    Flipping live→paper with real positions open would let the paper wrappers
    mask them from every rail; paper→live would try to close positions that
    don't exist on-venue.
    """
    mismatched = _mode_mismatched_pairs(ctx)
    if mismatched:
        current = "paper" if ctx.paper else "live"
        raise SystemExit(
            f"pairs {mismatched} were not opened in the configured mode "
            f"({current}) — switch mode back and unwind them first (records "
            "missing a mode need pairs[sym].mode set in the state file)"
        )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_yaml("config.yaml")
    universe = load_yaml("universe.yaml")
    state = load_state()
    need_boros = args.action in ("lock", "unlock")
    ctx = await build_ctx(config, universe, state, need_boros=need_boros)

    if args.action == "discover":
        return await action_discover(ctx, args.top)
    if args.action == "quote":
        if not args.symbol:
            raise SystemExit("--symbol required for quote")
        quote_size = args.amount if args.amount is not None else 1000.0
        return await _quote_payload(ctx, args.symbol, quote_size)
    if args.action == "deposit":
        if not args.symbol or args.amount is None:
            raise SystemExit("--symbol and --amount required for deposit")
        return await action_deposit(
            ctx,
            args.symbol,
            args.amount,
            args.gas,
            args.leg,
            args.confirm,
            skip_paper_gate=args.skip_paper_gate,
        )
    if args.action == "update":
        return await action_update(ctx, confirm=args.confirm, resume=args.resume)
    if args.action == "rotate":
        return await action_rotate(ctx, force=args.force, confirm=args.confirm)
    if args.action == "lock":
        if not args.symbol:
            raise SystemExit("--symbol required for lock")
        return await action_lock(ctx, args.symbol, args.tenor, confirm=args.confirm)
    if args.action == "unlock":
        if not args.symbol:
            raise SystemExit("--symbol required for unlock")
        return await action_unlock(ctx, args.symbol, confirm=args.confirm)
    if args.action == "status":
        return await action_status(ctx)
    if args.action == "unwind":
        return await action_unwind(ctx, args.symbol, confirm=args.confirm)
    if args.action == "exit":
        return await action_exit(ctx, confirm=args.confirm)
    raise SystemExit(f"unknown action {args.action}")


def main() -> None:
    args = _parser().parse_args()
    payload = asyncio.run(run(args))
    if args.compact:
        payload = compactify(payload)
    emit(payload)


if __name__ == "__main__":
    main()
