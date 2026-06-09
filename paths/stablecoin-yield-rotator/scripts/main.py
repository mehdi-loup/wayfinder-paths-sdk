"""
Stablecoin Yield Rotator — main entrypoint.

Actions:
  scan            — read-only ranked APY table for (asset, venue, chain) tuples
  quote-rotation  — proposed rotation deltas vs current positions; respects all constraints
  status          — aggregated positions + USD totals + blended APY
  deposit         — initial deposit into top-ranked venue for an asset
  update          — execute the rotation produced by quote-rotation; halts on first revert
  withdraw        — full or partial liquidate to stablecoin in wallet
  gorlami-scenario — Base fork dry run: scan → deposit → status → withdraw → status
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

PATH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PATH_DIR / "scripts"))

from rotation import (  # noqa: E402
    DEFAULT_MAX_STABLECOIN_APY,
    HEADROOM_FRACTION_FLOOR,
    UTIL_SPIKE_CEILING,
    RotationLeg,
    RotationPlan,
    leg_to_dict,
    quote_rotation,
)
from venues import (  # noqa: E402
    EXECUTABLE_VENUES,
    Position,
    VenueRow,
    lend,
    positions_all,
    scan_all,
    unlend,
)

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter  # noqa: E402
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT  # noqa: E402
from wayfinder_paths.core.constants.chains import (  # noqa: E402
    CHAIN_ID_BASE,
    CHAIN_ID_TO_CODE,
)
from wayfinder_paths.core.constants.contracts import BASE_USDC  # noqa: E402
from wayfinder_paths.core.utils.gorlami import gorlami_fork  # noqa: E402
from wayfinder_paths.core.utils.tokens import get_token_balance  # noqa: E402
from wayfinder_paths.core.utils.units import to_erc20_raw, to_wei_eth  # noqa: E402
from wayfinder_paths.core.utils.wallets import (  # noqa: E402
    get_wallet_signing_callback,
    load_wallets,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id  # noqa: E402
from wayfinder_paths.mcp.scripting import get_adapter  # noqa: E402

SCAN_CACHE_DIR = PATH_DIR / "inputs" / ".scan_cache"
SCAN_CACHE_TTL_SECONDS = 900
SCAN_CACHE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((PATH_DIR / "inputs" / name).read_text(encoding="utf-8")) or {}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _decimals_for(asset: str) -> int:
    return 18 if asset.upper() == "DAI" else 6


def _to_raw(asset: str, human: float) -> int:
    return int(round(human * (10 ** _decimals_for(asset))))


async def _resolve_wallet_label(config: dict[str, Any]) -> str:
    # Hosted execution supplies wallets with generated labels, so a bundled
    # default like "main" won't match. Use the configured label when it exists,
    # else fall back to the only available wallet, else fail with the choices.
    configured = str(config.get("wallet") or "").strip()
    labels = [str(w.get("label") or "").strip() for w in await load_wallets() if w.get("label")]
    if configured and configured in labels:
        return configured
    if len(labels) == 1:
        return labels[0]
    raise SystemExit(
        f"Wallet '{configured or '(unset)'}' not found; available: {labels or 'none'}. "
        "Set 'wallet' in inputs/config.yaml."
    )


def _scan_cache_payload(config: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    constraints = config.get("constraints") or {}
    return {
        "schema": SCAN_CACHE_SCHEMA_VERSION,
        "strict": strict,
        "venues": sorted(str(v) for v in (config.get("venues") or [])),
        "chains": sorted(int(c) for c in (config.get("chains") or [])),
        "assets": sorted(str(a).upper() for a in (config.get("assets") or [])),
        "constraints": {
            "max_scan_apy": constraints.get("max_scan_apy", DEFAULT_MAX_STABLECOIN_APY),
            "max_scan_utilization": constraints.get("max_scan_utilization", UTIL_SPIKE_CEILING),
            "min_scan_tvl_usd": constraints.get("min_scan_tvl_usd"),
        },
    }


def _scan_cache_path(config: dict[str, Any], *, strict: bool) -> Path:
    payload = _scan_cache_payload(config, strict=strict)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    return SCAN_CACHE_DIR / f"{digest}.json"


def _rows_from_dicts(rows: list[dict[str, Any]]) -> list[VenueRow]:
    return [VenueRow(**row) for row in rows]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    tmp.replace(path)


async def _scan_all_cached(
    config: dict[str, Any],
    *,
    strict: bool = True,
    failure_log: list[dict[str, Any]] | None = None,
) -> list[VenueRow]:
    path = _scan_cache_path(config, strict=strict)
    now = time.time()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if (
            cached.get("schema") == SCAN_CACHE_SCHEMA_VERSION
            and now - float(cached["ts"]) <= SCAN_CACHE_TTL_SECONDS
        ):
            if failure_log is not None:
                failure_log.extend(cached.get("failures") or [])
            return _rows_from_dicts(cached.get("rows") or [])
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ignoring scan cache read failure at {path}: {exc}")

    failures: list[dict[str, Any]] = []
    rows = await scan_all(
        venues=list(config.get("venues") or []),
        chains=[int(c) for c in (config.get("chains") or [])],
        assets=list(config.get("assets") or []),
        strict=strict,
        failure_log=failures,
    )
    if failure_log is not None:
        failure_log.extend(failures)
    _write_json_atomic(path, {
        "schema": SCAN_CACHE_SCHEMA_VERSION,
        "ts": now,
        "key": _scan_cache_payload(config, strict=strict),
        "failures": failures,
        "rows": [row.to_dict() for row in rows],
    })
    return rows


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def _scan_exclusion_reason(row: VenueRow, config: dict[str, Any]) -> str | None:
    constraints = config.get("constraints") or {}
    max_utilization = float(constraints.get("max_scan_utilization", UTIL_SPIKE_CEILING))
    max_apy_raw = constraints.get("max_scan_apy", DEFAULT_MAX_STABLECOIN_APY)
    max_apy = float(max_apy_raw) if max_apy_raw is not None else None
    min_tvl_usd_raw = constraints.get("min_scan_tvl_usd")
    min_tvl_usd = float(min_tvl_usd_raw) if min_tvl_usd_raw is not None else None

    # Rows that are visible but not eligible as targets — keep them out of `ranked` so
    # consumers (applets/users) don't treat them as actionable.
    if row.is_frozen:
        return str(row.extra.get("frozen_reason") or "frozen")
    if row.is_paused:
        return "paused"
    if row.asset_address.strip().lower() in {"", "none", "0x0000000000000000000000000000000000000000"}:
        return "missing underlying asset address"
    if max_apy is not None and row.supply_apy > max_apy:
        return f"supply_apy {row.supply_apy:.2%} > max_scan_apy {max_apy:.0%}"
    if row.utilization is not None and row.utilization > max_utilization:
        return f"utilization {row.utilization:.2%} > {max_utilization:.0%}"
    if min_tvl_usd is not None:
        if row.tvl_usd is None:
            return "missing tvl_usd"
        if row.tvl_usd < min_tvl_usd:
            return f"tvl_usd {row.tvl_usd:.2f} < min_scan_tvl_usd {min_tvl_usd:.2f}"
    return None


async def action_scan(config: dict[str, Any]) -> dict[str, Any]:
    # Scan is read-only UX; tolerate partial discovery and surface failures in the response.
    failures: list[dict[str, Any]] = []
    rows = await _scan_all_cached(config, strict=False, failure_log=failures)
    ranked_rows: list[VenueRow] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        reason = _scan_exclusion_reason(row, config)
        if reason is None:
            ranked_rows.append(row)
        else:
            excluded.append({**row.to_dict(), "exclude_reason": reason})

    ranked_rows.sort(key=lambda r: r.supply_apy, reverse=True)
    excluded.sort(key=lambda r: float(r.get("supply_apy") or 0.0), reverse=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in ranked_rows:
        grouped.setdefault(r.asset_symbol, []).append(r.to_dict())
    return {
        "action": "scan",
        "status": "partial" if failures else "ok",
        "row_count": len(rows),
        "ranked_count": len(ranked_rows),
        "excluded_count": len(excluded),
        "failure_count": len(failures),
        "failures": failures,
        "excluded": excluded,
        "by_asset": grouped,
        "ranked": [r.to_dict() for r in ranked_rows],
    }


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

async def action_status(config: dict[str, Any]) -> dict[str, Any]:
    label = await _resolve_wallet_label(config)
    _, address = await get_wallet_signing_callback(label)
    # Status is informational; tolerate partial discovery instead of aborting.
    position_failures: list[dict[str, Any]] = []
    scan_failures: list[dict[str, Any]] = []
    positions = await positions_all(
        venues=list(config.get("venues") or []),
        chains=[int(c) for c in (config.get("chains") or [])],
        assets=list(config.get("assets") or []),
        account=address,
        strict=False,
        failure_log=position_failures,
    )

    # Reuse cached scan APYs for read-only status; positions are still fetched live.
    scan = await _scan_all_cached(config, strict=False, failure_log=scan_failures)
    apy_by_market: dict[tuple[str, int, str], float] = {
        (r.venue, r.chain_id, r.market_id): r.supply_apy for r in scan
    }

    enriched: list[dict[str, Any]] = []
    total_supply_usd = 0.0
    weighted_apy_num = 0.0
    for p in positions:
        human = p.supply_raw / (10 ** p.decimals)
        usd = p.supply_usd if p.supply_usd is not None else human  # stablecoins ≈ $1
        apy = apy_by_market.get((p.venue, p.chain_id, p.market_id), 0.0)
        total_supply_usd += usd
        weighted_apy_num += usd * apy
        enriched.append({
            **asdict(p),
            "human_amount": human,
            "supply_usd_estimate": usd,
            "current_apy": apy,
        })

    blended_apy = (weighted_apy_num / total_supply_usd) if total_supply_usd > 0 else 0.0
    return {
        "action": "status",
        "status": "partial" if (scan_failures or position_failures) else "ok",
        "wallet": label,
        "address": address,
        "positions": enriched,
        "total_supply_usd": round(total_supply_usd, 4),
        "blended_apy": round(blended_apy, 6),
        "scan_failures": scan_failures,
        "position_failures": position_failures,
    }


# ---------------------------------------------------------------------------
# quote-rotation
# ---------------------------------------------------------------------------

async def action_quote_rotation(config: dict[str, Any]) -> dict[str, Any]:
    label = await _resolve_wallet_label(config)
    _, address = await get_wallet_signing_callback(label)
    plan, scan, positions = await _build_typed_plan(config, address)
    plan_dict = plan.to_dict()
    return {
        "action": "quote-rotation",
        "wallet": label,
        "address": address,
        "plan": plan_dict,
        "scan_row_count": len(scan),
        "position_count": len(positions),
    }


# ---------------------------------------------------------------------------
# deposit
# ---------------------------------------------------------------------------

async def action_deposit(config: dict[str, Any], asset: str, human_amount: float) -> dict[str, Any]:
    asset = asset.upper()
    if asset not in {"USDC", "USDT", "DAI"}:
        raise ValueError(f"unsupported asset {asset}")
    label = await _resolve_wallet_label(config)
    _, address = await get_wallet_signing_callback(label)
    min_gas_wei = int(config.get("min_gas_wei", DEFAULT_MIN_GAS_WEI))
    raw_amount = _to_raw(asset, human_amount)

    scan_config = {**config, "assets": [asset]}
    scan = await _scan_all_cached(scan_config)
    candidates = sorted(
        [r for r in scan if r.asset_symbol == asset and r.venue in EXECUTABLE_VENUES
         and not r.is_frozen and not r.is_paused],
        key=lambda r: r.supply_apy,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"no executable venues for {asset}")

    # Pick top-ranked that has enough headroom.
    target: VenueRow | None = None
    skipped: list[dict[str, Any]] = []
    for cand in candidates:
        if cand.supply_cap_headroom_raw is not None and cand.supply_cap_headroom_raw < raw_amount:
            skipped.append({"venue": cand.venue, "chain_id": cand.chain_id, "reason": "supply_cap_headroom < amount"})
            continue
        if cand.utilization is not None and cand.utilization > 0.95:
            skipped.append({"venue": cand.venue, "chain_id": cand.chain_id, "reason": f"utilization {cand.utilization:.2%} > 95%"})
            continue
        target = cand
        break

    if target is None:
        raise RuntimeError(f"all candidates failed headroom/utilization checks for {asset}: {skipped}")

    recheck = await _recheck_target_before_deposit(
        target.venue,
        target.chain_id,
        target.market_id,
        asset,
        raw_amount,
        config,
        strict_full_amount=True,
    )
    if not recheck["ok"]:
        return {
            "action": "deposit", "status": "halted",
            "reason": f"target re-check failed: {recheck['reason']}",
            "target": target.to_dict(),
            "recheck": recheck,
        }

    gas_check = await _check_gas_for_chains({target.chain_id}, address, min_gas_wei)
    if gas_check["insufficient"]:
        return {
            "action": "deposit", "status": "halted",
            "reason": "insufficient native gas on target chain",
            "gas_check": gas_check, "target": target.to_dict(),
        }

    ok, tx = await lend(
        venue=target.venue,
        wallet_label=label,
        chain_id=target.chain_id,
        market_id=target.market_id,
        raw_amount=raw_amount,
    )
    if not ok:
        raise RuntimeError(f"deposit failed at {target.venue}@{target.chain_id}: {tx}")

    return {
        "action": "deposit",
        "status": "ok",
        "asset": asset,
        "human_amount": human_amount,
        "raw_amount": raw_amount,
        "target": target.to_dict(),
        "skipped_candidates": skipped,
        "gas_check": gas_check,
        "tx": tx,
    }


# ---------------------------------------------------------------------------
# update (rotation execution)
# ---------------------------------------------------------------------------

def _summarize_quote(quote: dict[str, Any]) -> dict[str, Any]:
    """Pull the human-meaningful fields out of a BRAP quote for plan display."""
    return {
        "provider": quote.get("provider"),
        "input_amount": quote.get("input_amount") or quote.get("inputAmount"),
        "output_amount": quote.get("output_amount") or quote.get("outputAmount"),
        "from_amount_usd": quote.get("from_amount_usd"),
        "to_amount_usd": quote.get("to_amount_usd"),
        "estimated_fee_usd": quote.get("estimated_fee_usd") or quote.get("fees_usd"),
        "estimated_duration_seconds": quote.get("estimated_duration_seconds") or quote.get("duration_seconds"),
        "slippage": quote.get("slippage"),
    }


async def _quote_bridge(
    *,
    from_chain_id: int,
    to_chain_id: int,
    from_token_address: str,
    to_token_address: str,
    raw_amount: int,
    sender: str,
    slippage_bps: int,
) -> tuple[bool, dict[str, Any] | str]:
    """Read-only BRAP quote — does not broadcast. Returns (ok, quote_or_error)."""
    adapter = BRAPAdapter()
    try:
        ok, quote = await adapter.best_quote(
            from_token_address=from_token_address,
            to_token_address=to_token_address,
            from_chain_id=from_chain_id,
            to_chain_id=to_chain_id,
            from_address=sender,
            amount=str(raw_amount),
            slippage=slippage_bps / 10_000.0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if not ok:
        return False, str(quote)
    return True, quote  # type: ignore[return-value]


def _quote_output_amount(quote: dict[str, Any] | None) -> int:
    """BRAP quotes use either snake_case (`output_amount`) or camelCase (`outputAmount`)."""
    if not quote:
        return 0
    raw = quote.get("output_amount")
    if raw is None:
        raw = quote.get("outputAmount")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def _recheck_target_before_deposit(
    venue: str,
    chain_id: int,
    market_id: str,
    asset_symbol: str,
    raw_amount: int,
    config: dict[str, Any],
    *,
    strict_full_amount: bool = False,
) -> dict[str, Any]:
    """Fresh target-market guard check immediately before fund-moving execution."""
    rows = await scan_all(
        venues=[venue],
        chains=[chain_id],
        assets=[asset_symbol],
    )
    target = next(
        (
            row for row in rows
            if row.venue == venue and row.chain_id == chain_id
            and row.market_id.lower() == market_id.lower()
        ),
        None,
    )
    if target is None:
        return {"ok": False, "reason": "target market missing from fresh scan"}

    reason = _scan_exclusion_reason(target, config)
    if reason is not None:
        return {"ok": False, "reason": reason, "target": target.to_dict()}
    if target.is_frozen or target.is_paused:
        return {"ok": False, "reason": "target market frozen or paused", "target": target.to_dict()}
    if target.supply_cap_headroom_raw is not None and raw_amount > 0:
        min_headroom = raw_amount if strict_full_amount else int(raw_amount * HEADROOM_FRACTION_FLOOR)
        if target.supply_cap_headroom_raw < min_headroom:
            reason = (
                "supply_cap_headroom < amount"
                if strict_full_amount
                else f"supply_cap_headroom < {HEADROOM_FRACTION_FLOOR:.0%} of amount"
            )
            return {"ok": False, "reason": reason, "target": target.to_dict()}
    return {"ok": True, "target": target.to_dict()}


async def _execute_bridge(
    *,
    wallet_label: str,
    from_chain_id: int,
    to_chain_id: int,
    from_token_address: str,
    to_token_address: str,
    raw_amount: int,
    sender: str,
    slippage_bps: int,
    locked_quote: dict[str, Any] | None,
    min_output_fraction: float = 0.95,
) -> tuple[bool, dict[str, Any]]:
    """Re-quote, verify against the locked plan quote, then execute via swap_from_quote.

    `locked_quote` is the plan-time quote that the user confirmed. We refuse to broadcast
    if the fresh quote's output is below `min_output_fraction × locked_output`. The BRAP
    adapter is built via `get_adapter(...)` so the wallet signer is wired — without that,
    `swap_from_quote` calls `send_transaction(..., None)` and reverts after the source
    unlend has already moved funds.
    """
    src_code = CHAIN_ID_TO_CODE.get(from_chain_id)
    dst_code = CHAIN_ID_TO_CODE.get(to_chain_id)
    if not src_code or not dst_code:
        return False, {"error": f"unsupported chain pair {from_chain_id}->{to_chain_id}"}

    adapter = await get_adapter(BRAPAdapter, wallet_label)
    try:
        from_token = await TOKEN_CLIENT.get_token_details(f"{src_code}_{from_token_address.lower()}")
        to_token = await TOKEN_CLIENT.get_token_details(f"{dst_code}_{to_token_address.lower()}")
    except Exception as exc:  # noqa: BLE001
        return False, {"error": f"token lookup failed: {exc}"}
    if not from_token or not to_token:
        return False, {"error": "token lookup returned None"}

    fresh_ok, fresh_quote = await adapter.best_quote(
        from_token_address=from_token_address,
        to_token_address=to_token_address,
        from_chain_id=from_chain_id,
        to_chain_id=to_chain_id,
        from_address=sender,
        amount=str(raw_amount),
        slippage=slippage_bps / 10_000.0,
    )
    if not fresh_ok or not isinstance(fresh_quote, dict):
        return False, {"error": f"re-quote failed: {fresh_quote}"}

    locked_out = _quote_output_amount(locked_quote)
    fresh_out = _quote_output_amount(fresh_quote)
    if locked_out > 0 and fresh_out < int(locked_out * min_output_fraction):
        return False, {
            "error": "fresh BRAP quote output is materially worse than confirmed plan",
            "locked_output": locked_out,
            "fresh_output": fresh_out,
            "min_required_fraction": min_output_fraction,
        }

    try:
        ok, result = await adapter.swap_from_quote(
            from_token=from_token,
            to_token=to_token,
            from_address=sender,
            quote=fresh_quote,
            strategy_name="stablecoin-yield-rotator",
        )
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc)}
    if not ok:
        return False, {"error": str(result)}
    return True, {
        "bridge_result": result,
        "executed_quote": _summarize_quote(fresh_quote),
        "locked_quote": _summarize_quote(locked_quote) if locked_quote else None,
    }


async def _execute_leg(
    leg: RotationLeg,
    wallet_label: str,
    sender_address: str,
    slippage_bps: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Withdraw → bridge (if cross-chain) → deposit. Halt on first revert.

    Returns the final receipt payload. Raises on revert.
    """
    receipts: dict[str, Any] = {}

    # 1. Withdraw only the planned amount from the source venue.
    if leg.from_venue is None or leg.from_market_id is None or leg.from_chain_id is None:
        raise RuntimeError("rotation leg missing source venue")
    target_recheck = await _recheck_target_before_deposit(
        leg.to_venue,
        leg.to_chain_id,
        leg.to_market_id,
        leg.asset_symbol,
        leg.raw_amount,
        config,
    )
    if not target_recheck["ok"]:
        raise RuntimeError(f"target re-check failed before withdraw: {target_recheck['reason']}")
    receipts["target_recheck_before_withdraw"] = target_recheck

    # A venue may redeem less than the planned amount (e.g. an ERC-4626 maxRedeem cap),
    # so measure the source underlying balance across the withdraw and spend only what
    # actually came back — never the planned amount.
    if not leg.from_asset_address:
        raise RuntimeError("rotation leg missing from_asset_address; re-run quote-rotation")
    src_before = await get_token_balance(
        token_address=leg.from_asset_address, chain_id=leg.from_chain_id, wallet_address=sender_address,
    )
    ok, withdraw_tx = await unlend(
        venue=leg.from_venue,
        wallet_label=wallet_label,
        chain_id=leg.from_chain_id,
        market_id=leg.from_market_id,
        raw_amount=leg.raw_amount,
        withdraw_full=False,
    )
    if not ok:
        raise RuntimeError(f"withdraw reverted at {leg.from_venue}@{leg.from_chain_id}: {withdraw_tx}")
    src_after = await get_token_balance(
        token_address=leg.from_asset_address, chain_id=leg.from_chain_id, wallet_address=sender_address,
    )
    withdrawn = src_after - src_before
    if withdrawn <= 0:
        raise RuntimeError(
            f"withdraw produced no source balance delta on {leg.from_chain_id} "
            f"(before={src_before} after={src_after}); refusing to deposit"
        )
    receipts["withdraw"] = withdraw_tx
    receipts["source_withdrawn"] = {"planned": leg.raw_amount, "actual": withdrawn}

    # 2. Bridge if cross-chain. The leg carries pre-resolved underlying addresses + a locked
    #    BRAP quote from plan time. Re-quote at execution, refuse if materially worse, and
    #    track the destination balance delta so the deposit uses what actually arrived.
    deposit_amount = withdrawn
    if leg.is_cross_chain:
        if withdrawn < leg.raw_amount:
            # The locked bridge quote was sized for the planned amount; it's no longer
            # valid for a reduced redemption. Refuse rather than over-bridge.
            raise RuntimeError(
                f"source redeemed {withdrawn} < planned {leg.raw_amount}; locked bridge quote stale — re-run quote-rotation"
            )
        if not (leg.bridge_from_token and leg.bridge_to_token):
            raise RuntimeError("cross-chain leg missing bridge_from_token/bridge_to_token; re-run quote-rotation")

        balance_before = await get_token_balance(
            token_address=leg.bridge_to_token, chain_id=leg.to_chain_id, wallet_address=sender_address,
        )
        bridge_ok, bridge_payload = await _execute_bridge(
            wallet_label=wallet_label,
            from_chain_id=leg.from_chain_id,  # type: ignore[arg-type]
            to_chain_id=leg.to_chain_id,
            from_token_address=leg.bridge_from_token,
            to_token_address=leg.bridge_to_token,
            raw_amount=leg.raw_amount,
            sender=sender_address,
            slippage_bps=slippage_bps,
            locked_quote=leg.bridge_quote,
        )
        if not bridge_ok:
            raise RuntimeError(f"bridge failed: {bridge_payload}")
        balance_after = await get_token_balance(
            token_address=leg.bridge_to_token, chain_id=leg.to_chain_id, wallet_address=sender_address,
        )
        delta = balance_after - balance_before
        if delta <= 0:
            raise RuntimeError(
                f"bridge produced no destination balance delta on {leg.to_chain_id} "
                f"(before={balance_before} after={balance_after}); refusing to deposit"
            )
        deposit_amount = delta
        receipts["bridge"] = {
            **bridge_payload,
            "destination_balance_before": balance_before,
            "destination_balance_after": balance_after,
            "destination_balance_delta": delta,
        }

    # 3. Deposit the actual received amount (post-bridge for cross-chain, withdrawn raw for same-chain).
    if leg.is_cross_chain and deposit_amount != leg.raw_amount:
        target_recheck = await _recheck_target_before_deposit(
            leg.to_venue,
            leg.to_chain_id,
            leg.to_market_id,
            leg.asset_symbol,
            deposit_amount,
            config,
        )
        if not target_recheck["ok"]:
            raise RuntimeError(f"target re-check failed before deposit: {target_recheck['reason']}")
        receipts["target_recheck_before_deposit"] = target_recheck

    ok, deposit_tx = await lend(
        venue=leg.to_venue,
        wallet_label=wallet_label,
        chain_id=leg.to_chain_id,
        market_id=leg.to_market_id,
        raw_amount=deposit_amount,
    )
    if not ok:
        raise RuntimeError(f"deposit reverted at {leg.to_venue}@{leg.to_chain_id}: {deposit_tx}")
    receipts["deposit"] = deposit_tx
    receipts["deposit_amount"] = deposit_amount

    return receipts


async def _build_typed_plan(config: dict[str, Any], address: str) -> tuple[RotationPlan, list[VenueRow], list[Position]]:
    constraints = config.get("constraints") or {}
    slippage_bps = int(config.get("slippage_bps", 30))
    scan, positions = await asyncio.gather(
        _scan_all_cached(config),
        positions_all(
            venues=list(config.get("venues") or []),
            chains=[int(c) for c in (config.get("chains") or [])],
            assets=list(config.get("assets") or []),
            account=address,
        ),
    )
    plan = quote_rotation(
        scan=scan,
        positions=positions,
        min_apy_delta_bps=int(constraints.get("min_apy_delta_bps", 50)),
        gas_amortization_days=int(constraints.get("gas_amortization_days", 30)),
        max_gas_usd_per_rotation=float(constraints.get("max_gas_usd_per_rotation", 25)),
        max_position_pct_per_venue=int(constraints.get("max_position_pct_per_venue", 50)),
        blocklist_markets=list(constraints.get("blocklist_markets") or []),
        min_target_tvl_usd=(
            float(constraints["min_scan_tvl_usd"])
            if constraints.get("min_scan_tvl_usd") is not None
            else None
        ),
        max_target_apy=(
            float(constraints["max_scan_apy"])
            if constraints.get("max_scan_apy") is not None
            else DEFAULT_MAX_STABLECOIN_APY
        ),
    )

    # Post-process: attach a real BRAP quote to every cross-chain leg so the user can
    # see the actual route + output + fee before confirming. This satisfies the repo's
    # quote-before-swap rule.
    for leg in plan.legs:
        if not leg.is_cross_chain:
            continue
        src = next((r for r in scan
                    if r.venue == leg.from_venue and r.chain_id == leg.from_chain_id
                    and r.market_id == leg.from_market_id), None)
        dst = next((r for r in scan
                    if r.venue == leg.to_venue and r.chain_id == leg.to_chain_id
                    and r.market_id == leg.to_market_id), None)
        if src is None or dst is None:
            leg.skipped = True
            leg.skip_reason = "could not resolve underlying token addresses for bridge quote"
            continue
        leg.bridge_from_token = src.asset_address
        leg.bridge_to_token = dst.asset_address
        ok, quote_or_err = await _quote_bridge(
            from_chain_id=leg.from_chain_id,  # type: ignore[arg-type]
            to_chain_id=leg.to_chain_id,
            from_token_address=src.asset_address,
            to_token_address=dst.asset_address,
            raw_amount=leg.raw_amount,
            sender=address,
            slippage_bps=slippage_bps,
        )
        if not ok:
            leg.skipped = True
            leg.skip_reason = f"bridge quote failed: {quote_or_err}"
            continue
        leg.bridge_quote = quote_or_err  # type: ignore[assignment]

    # Move skipped legs out of plan.legs into plan.skipped so callers don't try to execute them.
    still_executable = [leg for leg in plan.legs if not leg.skipped]
    newly_skipped = [leg for leg in plan.legs if leg.skipped]
    plan.legs = still_executable
    plan.skipped.extend(newly_skipped)

    return plan, scan, positions


DEFAULT_MIN_GAS_WEI = 500_000_000_000_000  # 0.0005 ETH/HYPE-equivalent floor per chain
DEFAULT_GORLAMI_NATIVE_ETH = Decimal("0.2")


async def _gas_balance_wei(chain_id: int, address: str) -> int:
    async with web3_from_chain_id(chain_id) as w3:
        return int(await w3.eth.get_balance(w3.to_checksum_address(address)))


async def _check_gas_for_chains(chain_ids: set[int], address: str, min_gas_wei: int) -> dict[str, Any]:
    """Verify native gas balance on every chain in `chain_ids`."""
    insufficient: list[dict[str, Any]] = []
    balances: dict[int, int] = {}
    for chain_id in chain_ids:
        try:
            bal = await _gas_balance_wei(chain_id, address)
        except Exception as exc:  # noqa: BLE001
            insufficient.append({"chain_id": chain_id, "error": f"balance_read_failed: {exc}"})
            continue
        balances[chain_id] = bal
        if bal < min_gas_wei:
            insufficient.append({"chain_id": chain_id, "balance_wei": bal, "min_required_wei": min_gas_wei})
    return {"balances": balances, "insufficient": insufficient}


async def _check_gas_budget(plan_legs: list[RotationLeg], address: str, min_gas_wei: int) -> dict[str, Any]:
    """Verify native gas on every chain in the rotation path (across all legs)."""
    chains: set[int] = set()
    for leg in plan_legs:
        if leg.from_chain_id is not None:
            chains.add(leg.from_chain_id)
        chains.add(leg.to_chain_id)
    return await _check_gas_for_chains(chains, address, min_gas_wei)


async def action_update(config: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    """Re-quote the rotation, gas-check, and execute every non-skipped leg sequentially.

    Two safety gates before any fund movement:
    - explicit `--confirm` flag (otherwise emit the plan with status=requires_confirmation)
    - native gas balance > min on every chain in the rotation path

    The scan used for planning may come from the wallet-agnostic scan cache, but wallet
    positions are always fetched live and executable plans are never cached.
    """
    label = await _resolve_wallet_label(config)
    _, address = await get_wallet_signing_callback(label)
    slippage_bps = int(config.get("slippage_bps", 30))
    min_gas_wei = int(config.get("min_gas_wei", DEFAULT_MIN_GAS_WEI))

    plan, _scan, _positions = await _build_typed_plan(config, address)
    if not plan.legs:
        return {
            "action": "update",
            "status": "no-op",
            "reason": "no legs passed constraints",
            "skipped": [leg_to_dict(s) for s in plan.skipped],
        }

    if not confirmed:
        return {
            "action": "update",
            "status": "requires_confirmation",
            "reason": "rotation plan ready; re-run with --confirm to broadcast",
            "plan": {
                "legs": [leg_to_dict(_l) for _l in plan.legs],
                "skipped": [leg_to_dict(_l) for _l in plan.skipped],
            },
        }

    gas_check = await _check_gas_budget(plan.legs, address, min_gas_wei)
    if gas_check["insufficient"]:
        return {
            "action": "update",
            "status": "halted",
            "reason": "insufficient native gas on one or more chains in the rotation path",
            "gas_check": gas_check,
            "plan": [leg_to_dict(_l) for _l in plan.legs],
        }

    executed: list[dict[str, Any]] = []
    for leg in plan.legs:
        try:
            receipts = await _execute_leg(
                leg,
                wallet_label=label,
                sender_address=address,
                slippage_bps=slippage_bps,
                config=config,
            )
            executed.append({"leg": leg_to_dict(leg), "receipts": receipts})
        except Exception as exc:  # noqa: BLE001
            executed.append({"leg": leg_to_dict(leg), "error": str(exc)})
            return {
                "action": "update",
                "status": "halted",
                "executed": executed,
                "remaining_legs": [leg_to_dict(_l) for _l in plan.legs[len(executed):]],
                "reason": f"halted on revert in leg {len(executed)}: {exc}",
            }

    return {"action": "update", "status": "ok", "executed": executed, "gas_check": gas_check}


# ---------------------------------------------------------------------------
# withdraw
# ---------------------------------------------------------------------------

async def action_withdraw(config: dict[str, Any], human_amount: float | None) -> dict[str, Any]:
    """Liquidate to stablecoin in the wallet. Full if `human_amount is None`, else partial.

    Withdraws are issued per-position; partial withdraws are pro-rata across positions
    of the same asset. To withdraw a specific market, run `unlend` directly.
    """
    label = await _resolve_wallet_label(config)
    _, address = await get_wallet_signing_callback(label)
    min_gas_wei = int(config.get("min_gas_wei", DEFAULT_MIN_GAS_WEI))
    positions = await positions_all(
        venues=list(config.get("venues") or []),
        chains=[int(c) for c in (config.get("chains") or [])],
        assets=list(config.get("assets") or []),
        account=address,
    )
    if not positions:
        return {"action": "withdraw", "status": "no-op", "reason": "no positions"}

    # Filter to executable positions and gas-check every chain we'll touch.
    positions = [p for p in positions if p.venue in EXECUTABLE_VENUES]
    if not positions:
        return {"action": "withdraw", "status": "no-op", "reason": "no executable positions"}
    gas_check = await _check_gas_for_chains({p.chain_id for p in positions}, address, min_gas_wei)
    if gas_check["insufficient"]:
        return {
            "action": "withdraw", "status": "halted",
            "reason": "insufficient native gas on one or more chains",
            "gas_check": gas_check,
            "positions": [asdict(p) for p in positions],
        }

    receipts: list[dict[str, Any]] = []

    if human_amount is None:
        # Full liquidation across all positions.
        for p in positions:
            ok, tx = await unlend(
                venue=p.venue,
                wallet_label=label,
                chain_id=p.chain_id,
                market_id=p.market_id,
                raw_amount=0,
                withdraw_full=True,
            )
            entry = {"position": asdict(p), "tx": tx}
            if not ok:
                entry["error"] = "withdraw_full failed"
                receipts.append(entry)
                return {"action": "withdraw", "status": "halted", "receipts": receipts, "reason": f"halted on {p.venue}@{p.chain_id}"}
            receipts.append(entry)
        return {"action": "withdraw", "status": "ok", "scope": "full", "receipts": receipts}

    # Partial: pro-rata across positions, weighted by supply_raw within each asset.
    positions_by_asset: dict[str, list[Position]] = {}
    for p in positions:
        positions_by_asset.setdefault(p.asset_symbol, []).append(p)
    for asset, ps in positions_by_asset.items():
        total = sum(p.supply_raw for p in ps)
        if total <= 0:
            continue
        target_raw = _to_raw(asset, human_amount)
        if target_raw > total:
            target_raw = total
        for p in ps:
            share = (p.supply_raw / total) * target_raw
            qty = int(round(share))
            if qty <= 0:
                continue
            ok, tx = await unlend(
                venue=p.venue,
                wallet_label=label,
                chain_id=p.chain_id,
                market_id=p.market_id,
                raw_amount=qty,
                withdraw_full=False,
            )
            entry = {"position": asdict(p), "raw_qty": qty, "tx": tx}
            if not ok:
                entry["error"] = "partial withdraw failed"
                receipts.append(entry)
                return {"action": "withdraw", "status": "halted", "receipts": receipts, "reason": f"halted on {p.venue}@{p.chain_id}"}
            receipts.append(entry)
    return {"action": "withdraw", "status": "ok", "scope": "partial", "receipts": receipts}


# ---------------------------------------------------------------------------
# gorlami-scenario
# ---------------------------------------------------------------------------

async def action_gorlami_scenario(
    config: dict[str, Any],
    *,
    asset: str = "USDC",
    human_amount: float = 10.0,
) -> dict[str, Any]:
    """Run a deposit/status/withdraw dry run on a Gorlami Base fork.

    This intentionally uses a single Base venue. Gorlami forks cover EVM state, but
    cross-chain bridge delivery needs a second fork plus explicit destination seeding.
    """
    asset = asset.upper()
    if asset != "USDC":
        raise ValueError("gorlami-scenario currently supports USDC on Base only")

    amount_dec = Decimal(str(human_amount))
    if amount_dec <= 0:
        raise ValueError("gorlami-scenario requires --amount > 0")

    label = await _resolve_wallet_label(config)
    _, address = await get_wallet_signing_callback(label)

    scenario_config = deepcopy(config)
    scenario_config["wallet"] = label
    scenario_config["chains"] = [CHAIN_ID_BASE]
    scenario_config["assets"] = ["USDC"]
    scenario_config["venues"] = ["aave_v3"]

    seed_usdc = amount_dec * Decimal("2")
    native_balances = {address: to_wei_eth(DEFAULT_GORLAMI_NATIVE_ETH)}
    erc20_balances = [(BASE_USDC, address, int(to_erc20_raw(seed_usdc, decimals=6)))]

    async with gorlami_fork(
        CHAIN_ID_BASE,
        native_balances=native_balances,
        erc20_balances=erc20_balances,
    ) as (_, fork_info):
        scan = await action_scan(scenario_config)
        deposit = await action_deposit(scenario_config, asset="USDC", human_amount=float(amount_dec))
        status_after_deposit = await action_status(scenario_config)
        withdraw = await action_withdraw(scenario_config, human_amount=None)
        status_after_withdraw = await action_status(scenario_config)

    return {
        "action": "gorlami-scenario",
        "status": "ok",
        "wallet": label,
        "address": address,
        "chain_id": CHAIN_ID_BASE,
        "asset": "USDC",
        "human_amount": float(amount_dec),
        "seeded": {
            "native_eth": str(DEFAULT_GORLAMI_NATIVE_ETH),
            "usdc": str(seed_usdc),
        },
        "fork": {
            "fork_id": fork_info.get("fork_id"),
            "rpc_url": fork_info.get("rpc_url"),
        },
        "scenario_config": {
            "chains": scenario_config["chains"],
            "assets": scenario_config["assets"],
            "venues": scenario_config["venues"],
        },
        "steps": {
            "scan": scan,
            "deposit": deposit,
            "status_after_deposit": status_after_deposit,
            "withdraw": withdraw,
            "status_after_withdraw": status_after_withdraw,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

ACTIONS = ("scan", "quote-rotation", "deposit", "update", "status", "withdraw", "gorlami-scenario")


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    config = load_yaml("config.yaml")
    if args.action == "scan":
        return await action_scan(config)
    if args.action == "status":
        return await action_status(config)
    if args.action == "quote-rotation":
        return await action_quote_rotation(config)
    if args.action == "deposit":
        if args.amount is None or args.asset is None:
            raise SystemExit("deposit requires --amount and --asset")
        return await action_deposit(config, asset=args.asset, human_amount=args.amount)
    if args.action == "update":
        return await action_update(config, confirmed=args.confirm)
    if args.action == "withdraw":
        return await action_withdraw(config, human_amount=args.amount)
    if args.action == "gorlami-scenario":
        return await action_gorlami_scenario(
            config,
            asset=args.asset or "USDC",
            human_amount=args.amount if args.amount is not None else 10.0,
        )
    raise SystemExit(f"unknown action {args.action}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stablecoin Yield Rotator")
    parser.add_argument("--action", choices=ACTIONS, default="scan")
    parser.add_argument("--asset", choices=["USDC", "USDT", "DAI"], help="Asset for deposit")
    parser.add_argument("--amount", type=float, help="Human amount (e.g., 100.0)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to actually broadcast `update`. Without it, update emits the plan only.",
    )
    args = parser.parse_args()

    try:
        payload = asyncio.run(_main(args))
    except Exception as exc:  # noqa: BLE001
        logger.exception("action failed")
        emit({"action": args.action, "status": "error", "error": str(exc)})
        raise SystemExit(1) from exc

    emit(payload)


if __name__ == "__main__":
    main()
