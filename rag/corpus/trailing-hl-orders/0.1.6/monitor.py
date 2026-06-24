"""Background monitor — runner invokes this every `interval_seconds`.

For each attached trailing config, read the latest mid, step the controller,
and act on the emitted decision (update resting trigger, fire close/entry,
or hold). Resolves OCO pairs so firing one leg cancels the other.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.paths import get_runner_paths

RUNNER_JOB_NAME = "trailing-hl-monitor"

# Make sibling modules (controller, state) importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller import (  # noqa: E402
    Action,
    TrailingConfig,
    TrailingState,
    step,
)
from state import (  # noqa: E402
    load_configs,
    load_states,
    remove_config,
    set_state,
)


async def _build_adapter(wallet_label: str) -> Any:
    # HL signs EIP-712 typed-data (not raw transactions), so we can't use the
    # generic `get_adapter()` helper — it wires the tx-signing callback and
    # every order fails with "Transaction must include these fields".
    #
    # Since PR #203 split `sign_callback` and `sign_typed_data_callback` on
    # HyperliquidAdapter, only the latter is read by _sign() for order
    # broadcast. Pass the typed-data callback into the correct kwarg —
    # routing it through `sign_callback` (the pre-split behaviour) leaves
    # `_sign_typed_data_callback` None and every tick raises
    # "No sign_typed_data_callback configured".
    from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
    from wayfinder_paths.core.config import CONFIG
    from wayfinder_paths.core.utils.wallets import (
        get_wallet_sign_typed_data_callback,
    )

    sign_cb, address = await get_wallet_sign_typed_data_callback(wallet_label)

    strategy_raw = CONFIG.get("strategy")
    strategy_cfg = strategy_raw if isinstance(strategy_raw, dict) else {}
    adapter_config: dict[str, Any] = dict(strategy_cfg)
    adapter_config["main_wallet"] = {"address": address}
    adapter_config["strategy_wallet"] = {"address": address}

    return HyperliquidAdapter(
        config=adapter_config,
        sign_typed_data_callback=sign_cb,
        wallet_address=address,
    )


async def _position_size(adapter: Any, address: str, coin: str) -> float | None:
    ok, data = await adapter.get_user_state(address)
    if not ok:
        return None
    for pos in data.get("assetPositions", []) or []:
        inner = pos.get("position", pos)
        if str(inner.get("coin")) == coin:
            try:
                return abs(float(inner.get("szi", 0.0)))
            except (TypeError, ValueError):
                return None
    return None


def _statuses(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = (result or {}).get("response", {}).get("data", {}) or {}
    raw = data.get("statuses") or []
    return [s for s in raw if isinstance(s, dict)]


def _per_order_error(result: dict[str, Any]) -> str | None:
    # HL returns outer status="ok" even when the specific order is rejected;
    # the real error lives in response.data.statuses[i].error. place_market_order
    # already does this check internally, but place_trigger_order does not.
    for s in _statuses(result):
        if err := s.get("error"):
            return str(err)
    return None


def _log_hl_result(label: str, result: dict[str, Any]) -> None:
    statuses = _statuses(result)
    outer = (result or {}).get("status")
    print(f"[trailing-hl] {label}: status={outer} statuses={statuses}")


async def _execute_close(
    adapter: Any, cfg_payload: dict[str, Any], trigger_price: float
) -> tuple[bool, str]:
    coin = cfg_payload["coin"]
    side = cfg_payload["side"]
    mode = cfg_payload.get("mode", "resting")

    asset_id = adapter.coin_to_asset.get(coin)
    if asset_id is None:
        return False, f"Unknown coin {coin!r}"
    size = await _position_size(adapter, adapter.wallet_address, coin)
    if not size or size <= 0:
        return False, f"No open {coin} position to close"

    is_buy_to_close = side == "short"
    if mode == "monitor":
        ok, result = await adapter.place_market_order(
            asset_id=asset_id,
            is_buy=is_buy_to_close,
            slippage=0.01,
            size=size,
            address=adapter.wallet_address,
            reduce_only=True,
        )
        _log_hl_result(f"{coin} monitor-close place_market_order", result)
        return ok, "market close" if ok else f"market close failed: {result}"

    # Resting mode — the trigger is supposed to be live on HL. Verify before
    # trusting it: HL's place_trigger_order endpoint returns outer status="ok"
    # even when the specific order was rejected (per-order error in statuses[]),
    # which means we can't assume the stop is armed just because we called the
    # place method. If the position is still open when our local cross fires,
    # fall back to a reduce-only market close so the user isn't left naked.
    size_still_open = await _position_size(adapter, adapter.wallet_address, coin)
    if not size_still_open or size_still_open <= 0:
        return True, "resting trigger already armed"

    ok, result = await adapter.place_market_order(
        asset_id=asset_id,
        is_buy=is_buy_to_close,
        slippage=0.01,
        size=size_still_open,
        address=adapter.wallet_address,
        reduce_only=True,
    )
    _log_hl_result(f"{coin} resting-fallback place_market_order", result)
    if ok:
        return True, "resting trigger did not fire; executed market close"
    return False, (f"resting trigger did not fire and market close failed: {result}")


async def _place_or_move_resting_trigger(
    adapter: Any,
    cfg_payload: dict[str, Any],
    new_trigger: float,
    existing_cloid: str | None,
    existing_oid: int | None,
) -> tuple[bool, str | None, int | None, str]:
    coin = cfg_payload["coin"]
    side = cfg_payload["side"]
    asset_id = adapter.coin_to_asset.get(coin)
    if asset_id is None:
        return False, existing_cloid, existing_oid, f"Unknown coin {coin!r}"
    size = await _position_size(adapter, adapter.wallet_address, coin)
    if not size or size <= 0:
        return False, existing_cloid, existing_oid, f"No open {coin} position"

    # oid is the canonical handle (HL returns it for trigger orders even when
    # no cloid was supplied); fall back to cloid-based cancel only if that's
    # all we have from a legacy state file.
    if existing_oid is not None:
        cancel_ok, cancel_result = await adapter.cancel_order(
            asset_id, existing_oid, adapter.wallet_address
        )
        _log_hl_result(
            f"{coin} cancel_order oid={existing_oid} ok={cancel_ok}", cancel_result
        )
    elif existing_cloid:
        cancel_ok, cancel_result = await adapter.cancel_order_by_cloid(
            asset_id, existing_cloid, adapter.wallet_address
        )
        _log_hl_result(
            f"{coin} cancel_order_by_cloid cloid={existing_cloid} ok={cancel_ok}",
            cancel_result,
        )

    # Both trailing_sl and trailing_tp fire on a PULLBACK from peak, i.e. a
    # stop-style trigger. HL's tpsl="tp" is a fixed take-profit that fires on
    # FAVORABLE movement to a threshold — wrong direction for this path. The
    # trailing_tp's "take-profit" semantics are in the pre-activation gate
    # (controlled locally); the resting exchange order is always a stop.
    tpsl = "sl"
    is_buy_to_close = side == "short"
    # HL price rules: max 5 significant digits AND max (6 - szDecimals)
    # decimals for perps (8 - szDecimals for spot). place_market_order rounds
    # internally via the same two steps; place_trigger_order does not, so an
    # unrounded trail like 40.11385 (5 decimals, HYPE allows 4) is rejected
    # with "Order has invalid price.". Mirror that logic here.
    price_decimals = adapter._get_price_decimals(asset_id)
    rounded_trigger = round(float(f"{new_trigger:.5g}"), price_decimals)
    ok, result = await adapter.place_trigger_order(
        asset_id=asset_id,
        is_buy=is_buy_to_close,
        trigger_price=rounded_trigger,
        size=size,
        address=adapter.wallet_address,
        tpsl=tpsl,
    )
    _log_hl_result(f"{coin} place_trigger_order ok={ok}", result)

    # place_trigger_order only checks the outer status; HL reports per-order
    # rejections inside response.data.statuses[i].error. Downgrade to failure
    # when that's present so a rejected stop doesn't look armed.
    if ok and (err := _per_order_error(result)):
        return (
            False,
            existing_cloid,
            existing_oid,
            (f"place_trigger_order rejected by HL: {err}"),
        )
    if not ok:
        return (
            False,
            existing_cloid,
            existing_oid,
            (f"place_trigger_order failed: {result}"),
        )

    new_cloid, new_oid = _extract_order_ref(result)
    return True, new_cloid or existing_cloid, new_oid or existing_oid, "ok"


def _extract_order_ref(result: dict[str, Any]) -> tuple[str | None, int | None]:
    cloid: str | None = None
    oid: int | None = None
    try:
        for s in _statuses(result):
            resting = s.get("resting") or s.get("filled") or {}
            if not cloid and (c := resting.get("cloid")):
                cloid = str(c)
            if oid is None and (o := resting.get("oid")) is not None:
                try:
                    oid = int(o)
                except (TypeError, ValueError):
                    oid = None
    except Exception:
        pass
    return cloid, oid


def _cfg_from_payload(payload: dict[str, Any]) -> TrailingConfig:
    keys = TrailingConfig.__dataclass_fields__.keys()
    return TrailingConfig(**{k: payload[k] for k in keys if k in payload})


def _state_from_raw(raw: dict[str, Any] | None) -> TrailingState:
    if not raw:
        return TrailingState()
    keys = TrailingState.__dataclass_fields__.keys()
    return TrailingState(**{k: raw[k] for k in keys if k in raw})


async def _tick_for_wallet(
    wallet_label: str, entries: list[tuple[str, dict[str, Any]]]
) -> None:
    adapter = await _build_adapter(wallet_label)
    ok, mids = await adapter.get_all_mid_prices()
    if not ok:
        print(f"[trailing-hl] {wallet_label}: failed to fetch mids; skipping")
        return

    peer_fires: set[str] = set()
    states_raw = load_states()

    # OCO peers reference each other by position_id (e.g. "HYPE-TP-...") but
    # state/config are keyed by the full "<wallet>::<coin>::<position_id>".
    # Index up front so a fire can map peer position_id → full key.
    key_by_position_id: dict[str, str] = {
        str(payload.get("position_id")): key for key, payload in entries
    }

    # First pass — detect crossings and enqueue peer cancels.
    pending: list[tuple[str, dict[str, Any], TrailingState, Any]] = []
    for key, payload in entries:
        cfg = _cfg_from_payload(payload)
        mid = mids.get(cfg.coin)
        if mid is None:
            print(f"[trailing-hl] {key}: no mid for {cfg.coin}; skipping")
            continue
        state = _state_from_raw(states_raw.get(key))
        decision = step(cfg, state, float(mid))
        pending.append((key, payload, state, decision))
        if decision.action in (Action.FIRE_CLOSE, Action.FIRE_ENTRY) and payload.get(
            "oco_peer"
        ):
            peer_key = key_by_position_id.get(str(payload["oco_peer"]))
            if peer_key:
                peer_fires.add(peer_key)

    # Second pass — apply decisions, honoring peer-cancel signals.
    for key, payload, prior, decision in pending:
        cfg = _cfg_from_payload(payload)
        if key in peer_fires and not decision.next_state.fired:
            cancelled = step(cfg, prior, 0.0, peer_fired=True)
            set_state(key, cancelled.next_state)
            remove_config(key)
            print(f"[trailing-hl] {key}: cancelled (peer fired)")
            continue

        if decision.action == Action.HOLD:
            continue

        if decision.action in (Action.INITIALIZE, Action.UPDATE_TRAIL):
            # trailing_entry has no resting-order concept on HL — there's no
            # primitive that opens a new position on a reversal, only
            # reduce-only triggers. The checker tracks the adverse peak in
            # state and fires a market order on FIRE_ENTRY. Skip the resting
            # path regardless of cfg.mode.
            is_entry_kind = cfg.kind == "trailing_entry"
            if (
                not is_entry_kind
                and cfg.mode == "resting"
                and decision.trigger_price is not None
            ):
                ok, new_cloid, new_oid, note = await _place_or_move_resting_trigger(
                    adapter,
                    payload,
                    decision.trigger_price,
                    prior.cloid,
                    prior.oid,
                )
                if ok:
                    final_state = TrailingState(
                        peak=decision.next_state.peak,
                        activated=decision.next_state.activated,
                        reference_price=decision.next_state.reference_price,
                        last_trigger_price=decision.next_state.last_trigger_price,
                        cloid=new_cloid,
                        oid=new_oid,
                        fired=decision.next_state.fired,
                        cancelled=decision.next_state.cancelled,
                    )
                    set_state(key, final_state)
                else:
                    # Don't advance state when the trigger didn't land —
                    # otherwise a later tick will think the stop is armed
                    # and FIRE_CLOSE off a phantom order. Next tick will
                    # see the same peak/mid and retry placement.
                    pass
                print(
                    f"[trailing-hl] {key}: {decision.action.value} @ {decision.trigger_price:.6g} ({note})"
                )
            else:
                set_state(key, decision.next_state)
                print(f"[trailing-hl] {key}: {decision.action.value} (monitor mode)")
            continue

        if decision.action == Action.FIRE_CLOSE:
            ok, note = await _execute_close(
                adapter, payload, decision.trigger_price or 0.0
            )
            set_state(key, decision.next_state)
            if ok:
                remove_config(key)
                print(f"[trailing-hl] {key}: FIRE_CLOSE ({note})")
            else:
                print(f"[trailing-hl] {key}: FIRE_CLOSE FAILED ({note})")
            continue

        if decision.action == Action.FIRE_ENTRY:
            # Trailing entry: fire a market order to open the position.
            asset_id = adapter.coin_to_asset.get(cfg.coin)
            size = payload.get("entry_size")
            if asset_id is None or not size:
                print(
                    f"[trailing-hl] {key}: FIRE_ENTRY skipped (missing asset_id or entry_size)"
                )
                continue
            ok, result = await adapter.place_market_order(
                asset_id=asset_id,
                is_buy=(cfg.side == "long"),
                slippage=0.01,
                size=float(size),
                address=adapter.wallet_address,
            )
            _log_hl_result(f"{cfg.coin} FIRE_ENTRY place_market_order ok={ok}", result)
            set_state(key, decision.next_state)
            if ok:
                remove_config(key)
                print(f"[trailing-hl] {key}: FIRE_ENTRY ok")
            else:
                print(f"[trailing-hl] {key}: FIRE_ENTRY FAILED ({result})")
            continue

        if decision.action == Action.CANCEL:
            set_state(key, decision.next_state)
            remove_config(key)
            print(f"[trailing-hl] {key}: cancelled")


def _pause_runner_self() -> None:
    # Self-cleanup without spawning a process: ask the runner daemon to PAUSE
    # this job via the local Unix socket. Pausing is accepted while the job
    # is running (unlike delete, which the daemon refuses). Paused jobs stop
    # ticking immediately after the current run finishes; attach.py resumes
    # or re-registers the job the next time a position is attached.
    paths = get_runner_paths()
    client = RunnerControlClient(sock_path=paths.sock_path)
    resp = client.call("pause_job", {"name": RUNNER_JOB_NAME})
    if resp.get("ok"):
        print(
            f"[trailing-hl] no configs remaining; paused runner job '{RUNNER_JOB_NAME}'"
        )
    else:
        print(
            f"[trailing-hl] no configs remaining; pause_job failed: {resp.get('error')!r}"
        )


async def main() -> None:
    configs = load_configs()
    if not configs:
        print("[trailing-hl] no active configs")
        _pause_runner_self()
        return

    by_wallet: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, payload in configs.items():
        wallet = str(payload.get("wallet_label") or "main")
        by_wallet.setdefault(wallet, []).append((key, payload))

    for wallet_label, entries in by_wallet.items():
        try:
            await _tick_for_wallet(wallet_label, entries)
        except Exception as exc:
            print(f"[trailing-hl] wallet={wallet_label}: tick failed: {exc!r}")

    if not load_configs():
        _pause_runner_self()


if __name__ == "__main__":
    asyncio.run(main())
