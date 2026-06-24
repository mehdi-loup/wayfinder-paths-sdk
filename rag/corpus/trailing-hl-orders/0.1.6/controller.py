"""Pure trailing-order controller — no adapter coupling, deterministic.

Handles three kinds of trailing orders:

- trailing_sl: stop-loss that follows the favorable extreme, closes on pullback
- trailing_tp: take-profit that arms only after the position has moved in our favor
  by activation_pct, then closes on pullback by offset_pct
- trailing_entry: limit entry that tracks the ADVERSE extreme and fires once price
  reverses by offset_pct (wait-for-reversal semantics)

OCO (one-cancels-other) is resolved outside this module by the caller passing
peer_fired=True; the controller reacts by emitting CANCEL.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

Side = Literal["long", "short"]
Kind = Literal["trailing_sl", "trailing_tp", "trailing_entry"]
Mode = Literal["resting", "monitor"]


@dataclass(frozen=True)
class TrailingConfig:
    coin: str
    side: Side
    kind: Kind
    offset_pct: float
    mode: Mode = "resting"
    activation_pct: float | None = None  # TP only
    oco_peer: str | None = None


@dataclass(frozen=True)
class TrailingState:
    peak: float | None = None
    activated: bool = False
    reference_price: float | None = None  # TP-only entry reference
    last_trigger_price: float | None = None
    cloid: str | None = None
    oid: int | None = None
    fired: bool = False
    cancelled: bool = False


class Action(StrEnum):
    HOLD = "hold"
    INITIALIZE = "initialize"
    UPDATE_TRAIL = "update_trail"
    FIRE_CLOSE = "fire_close"
    FIRE_ENTRY = "fire_entry"
    CANCEL = "cancel"


@dataclass(frozen=True)
class ControllerDecision:
    action: Action
    next_state: TrailingState
    trigger_price: float | None = None
    reason: str = ""


def _favorable_extreme(side: Side, peak: float | None, mid: float, kind: Kind) -> float:
    # trailing_entry tracks the ADVERSE extreme (so we can detect a reversal).
    if kind == "trailing_entry":
        if side == "long":
            return mid if peak is None else min(peak, mid)
        return mid if peak is None else max(peak, mid)
    # SL / TP track the favorable extreme.
    if side == "long":
        return mid if peak is None else max(peak, mid)
    return mid if peak is None else min(peak, mid)


def _trigger_from_peak(side: Side, peak: float, offset_pct: float, kind: Kind) -> float:
    if kind == "trailing_entry":
        # Fire once price reverses off the adverse extreme by offset_pct.
        return peak * (1 + offset_pct) if side == "long" else peak * (1 - offset_pct)
    # SL / TP close: long closes when price drops offset_pct below peak; short inverse.
    return peak * (1 - offset_pct) if side == "long" else peak * (1 + offset_pct)


def _crossed(side: Side, mid: float, trigger: float, kind: Kind) -> bool:
    if kind == "trailing_entry":
        return mid >= trigger if side == "long" else mid <= trigger
    return mid <= trigger if side == "long" else mid >= trigger


def _tp_moved_enough(
    side: Side, mid: float, reference: float, activation_pct: float
) -> bool:
    moved = (
        (mid - reference) / reference
        if side == "long"
        else (reference - mid) / reference
    )
    return moved >= activation_pct


def step(
    cfg: TrailingConfig,
    state: TrailingState,
    mid: float,
    *,
    peer_fired: bool = False,
) -> ControllerDecision:
    """Advance one trailing order by one price observation."""
    if state.fired or state.cancelled:
        return ControllerDecision(Action.HOLD, state, reason="terminal")

    if peer_fired:
        return ControllerDecision(
            Action.CANCEL, replace(state, cancelled=True), reason="oco_peer_fired"
        )

    # Trailing-TP pre-activation gate.
    if cfg.kind == "trailing_tp" and not state.activated:
        if cfg.activation_pct is None:
            # No gate: behave like a regular trailing close that starts immediately.
            return _initial_or_trail(cfg, state, mid, treat_as_activated=True)
        if state.reference_price is None:
            return ControllerDecision(
                Action.INITIALIZE,
                replace(state, reference_price=mid),
                reason="tp_reference_set",
            )
        if not _tp_moved_enough(
            cfg.side, mid, state.reference_price, cfg.activation_pct
        ):
            return ControllerDecision(
                Action.HOLD, state, reason="tp_awaiting_activation"
            )
        # Activation crossed — adopt the current mid as the initial peak.
        trigger = _trigger_from_peak(cfg.side, mid, cfg.offset_pct, cfg.kind)
        return ControllerDecision(
            Action.INITIALIZE,
            TrailingState(
                peak=mid,
                activated=True,
                reference_price=state.reference_price,
                last_trigger_price=trigger,
                cloid=state.cloid,
                oid=state.oid,
            ),
            trigger_price=trigger,
            reason="tp_activated",
        )

    return _initial_or_trail(cfg, state, mid, treat_as_activated=False)


def _initial_or_trail(
    cfg: TrailingConfig,
    state: TrailingState,
    mid: float,
    *,
    treat_as_activated: bool,
) -> ControllerDecision:
    if state.peak is None:
        new_peak = _favorable_extreme(cfg.side, None, mid, cfg.kind)
        trigger = _trigger_from_peak(cfg.side, new_peak, cfg.offset_pct, cfg.kind)
        return ControllerDecision(
            Action.INITIALIZE,
            TrailingState(
                peak=new_peak,
                activated=state.activated or treat_as_activated,
                reference_price=state.reference_price,
                last_trigger_price=trigger,
                cloid=state.cloid,
                oid=state.oid,
            ),
            trigger_price=trigger,
            reason="initialized",
        )

    trigger_current = state.last_trigger_price
    if trigger_current is not None and _crossed(
        cfg.side, mid, trigger_current, cfg.kind
    ):
        fired = replace(state, fired=True)
        action = (
            Action.FIRE_ENTRY if cfg.kind == "trailing_entry" else Action.FIRE_CLOSE
        )
        return ControllerDecision(
            action, fired, trigger_price=trigger_current, reason="crossed"
        )

    new_peak = _favorable_extreme(cfg.side, state.peak, mid, cfg.kind)
    if new_peak != state.peak:
        new_trigger = _trigger_from_peak(cfg.side, new_peak, cfg.offset_pct, cfg.kind)
        return ControllerDecision(
            Action.UPDATE_TRAIL,
            replace(state, peak=new_peak, last_trigger_price=new_trigger),
            trigger_price=new_trigger,
            reason="peak_updated",
        )
    return ControllerDecision(Action.HOLD, state, reason="no_move")
