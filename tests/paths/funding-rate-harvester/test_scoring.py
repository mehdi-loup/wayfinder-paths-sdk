"""Pure-math tests for the funding-rate-harvester scoring module.

No network. Run with:
    poetry run pytest tests/paths/funding-rate-harvester -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/funding-rate-harvester"
sys.path.insert(0, str(PATH_DIR / "scripts"))

from scoring import (  # noqa: E402
    CarryComponents,
    ComboScore,
    best_combo_for_symbol,
    breakeven_hours,
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

HOUR = 3600.0


# ---------------------------------------------------------------------------
# Funding normalization across venue intervals
# ---------------------------------------------------------------------------

def test_normalize_funding_hourly_hl():
    # 0.01%/hr on Hyperliquid → 87.6% APR
    assert normalize_funding_apr(0.0001, 1.0) == pytest.approx(0.876)


def test_normalize_funding_8h_cex():
    # Same 0.01% per settlement every 8h is 8x smaller annualized
    assert normalize_funding_apr(0.0001, 8.0) == pytest.approx(0.876 / 8)


def test_normalize_funding_preserves_sign():
    # Negative funding (shorts pay) must stay negative — sign flip would
    # make the scorer chase un-harvestable markets.
    assert normalize_funding_apr(-0.0001, 1.0) < 0


def test_normalize_funding_rejects_bad_interval():
    with pytest.raises(ValueError):
        normalize_funding_apr(0.0001, 0)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def test_ema_seeds_with_first_sample():
    assert update_ema(None, 0.5, alpha=0.1) == 0.5


def test_ema_moves_toward_sample():
    alpha = ema_alpha(sample_interval_hours=1.0, ema_hours=72.0)
    ema = update_ema(0.10, 0.20, alpha)
    assert 0.10 < ema < 0.20
    # 72h horizon: a single hourly sample barely moves it
    assert ema < 0.105


def test_ema_alpha_longer_horizon_is_smoother():
    assert ema_alpha(1.0, 72.0) < ema_alpha(1.0, 12.0)


def test_ema_does_not_chase_spike():
    # A one-interval 10x funding spike must not dominate a 72h EMA.
    alpha = ema_alpha(1.0, 72.0)
    ema = 0.10
    ema = update_ema(ema, 1.0, alpha)
    assert ema < 0.12


# ---------------------------------------------------------------------------
# Net stacked carry
# ---------------------------------------------------------------------------

def test_net_apr_stacks_components():
    c = CarryComponents(
        funding_apr=0.12,
        spot_leg_apy=0.05,
        fee_apr=0.01,
        slippage_apr=0.005,
        financing_apr=0.002,
    )
    assert c.net_apr == pytest.approx(0.12 + 0.05 - 0.01 - 0.005 - 0.002)


def test_cost_apr_amortizes_round_trip():
    # 50bps round trip over 30 days ≈ 6.08% APR drag
    assert cost_apr(50, 30) == pytest.approx(0.005 * 365 / 30)


def test_cost_apr_rejects_zero_holding():
    with pytest.raises(ValueError):
        cost_apr(50, 0)


def test_rank_combos_orders_by_net_apr():
    lo = ComboScore("hyperliquid", "ETH", "hl_spot", CarryComponents(0.05, 0.0))
    hi = ComboScore("hyperliquid", "ETH", "etherfi", CarryComponents(0.05, 0.03))
    assert rank_combos([lo, hi])[0] is hi
    assert best_combo_for_symbol([lo, hi], "ETH") is hi
    assert best_combo_for_symbol([lo, hi], "BTC") is None


# ---------------------------------------------------------------------------
# Rotation gating: threshold AND breakeven AND dwell
# ---------------------------------------------------------------------------

ROTATION_DEFAULTS = {
    "notional_usd": 5_000.0,
    "migration_cost_usd": 2.0,
    "threshold_apr_bps": 400,
    "max_breakeven_hours": 48.0,
    "min_dwell_hours": 24.0,
    "hours_held": 48.0,
}


def test_rotation_migrates_when_all_gates_pass():
    d = rotation_decision(0.10, 0.20, **ROTATION_DEFAULTS)
    assert d.migrate
    assert d.apr_delta_bps == 1000
    assert d.breakeven_hours == pytest.approx(35.04, abs=0.1)


def test_rotation_breakeven_math_exact():
    # apr_delta 10% on $5000 = $500/yr = $0.05707/h → $2 cost = 35.04h
    assert breakeven_hours(2.0, 0.10, 5_000.0) == pytest.approx(35.04, abs=0.1)


def test_rotation_blocked_by_dwell():
    d = rotation_decision(0.10, 0.20, **{**ROTATION_DEFAULTS, "hours_held": 3.0})
    assert not d.migrate
    assert "dwell" in d.reason


def test_rotation_force_bypasses_dwell_not_breakeven():
    args = {**ROTATION_DEFAULTS, "hours_held": 3.0, "migration_cost_usd": 2.0}
    d = rotation_decision(0.10, 0.20, bypass_dwell=True, **args)
    assert d.migrate
    # Even forced, an unpayable migration cost still blocks
    d2 = rotation_decision(
        0.10, 0.20, bypass_dwell=True, **{**args, "migration_cost_usd": 500.0}
    )
    assert not d2.migrate
    assert "breakeven" in d2.reason


def test_rotation_blocked_below_threshold():
    d = rotation_decision(0.10, 0.13, **ROTATION_DEFAULTS)
    assert not d.migrate
    assert "threshold" in d.reason


def test_rotation_blocked_by_breakeven():
    d = rotation_decision(0.10, 0.20, **{**ROTATION_DEFAULTS, "migration_cost_usd": 500.0})
    assert not d.migrate
    assert "breakeven" in d.reason


def test_rotation_zero_incremental_carry_is_infinite_breakeven():
    assert breakeven_hours(10.0, 0.0, 5_000.0) == float("inf")
    d = rotation_decision(0.10, 0.10, **{**ROTATION_DEFAULTS, "threshold_apr_bps": -1})
    assert not d.migrate


def test_rotation_cost_greater_than_spread_never_migrates():
    # Candidate barely better but cost enormous relative to uplift
    d = rotation_decision(
        0.100,
        0.145,
        **{**ROTATION_DEFAULTS, "migration_cost_usd": 10_000.0},
    )
    assert not d.migrate


def test_rotation_zero_cost_migrates_instantly():
    d = rotation_decision(0.10, 0.20, **{**ROTATION_DEFAULTS, "migration_cost_usd": 0.0})
    assert d.migrate
    assert d.breakeven_hours == 0.0


def test_rotation_negative_delta_never_migrates():
    d = rotation_decision(0.20, 0.10, **ROTATION_DEFAULTS)
    assert not d.migrate


# ---------------------------------------------------------------------------
# Negative-carry exit with grace period
# ---------------------------------------------------------------------------

def test_negative_carry_no_exit_above_floor():
    should_exit, since = negative_carry_exit(
        0.05, floor_bps=200, below_floor_since_ts=1_000.0, now_ts=999_999.0, grace_hours=12.0
    )
    assert not should_exit
    assert since is None  # recovery resets the clock


def test_negative_carry_exit_after_grace():
    t0 = 1_000_000.0
    should_exit, since = negative_carry_exit(
        0.01, floor_bps=200, below_floor_since_ts=None, now_ts=t0, grace_hours=12.0
    )
    assert not should_exit
    assert since == t0
    should_exit, since = negative_carry_exit(
        0.01, floor_bps=200, below_floor_since_ts=since, now_ts=t0 + 13 * HOUR, grace_hours=12.0
    )
    assert should_exit
    assert since == t0


def test_negative_carry_boundary_is_inclusive_floor():
    # Exactly at the floor is NOT below it
    should_exit, since = negative_carry_exit(
        0.02, floor_bps=200, below_floor_since_ts=None, now_ts=0.0, grace_hours=12.0
    )
    assert not should_exit
    assert since is None


# ---------------------------------------------------------------------------
# Delta band + churn guard (hysteresis)
# ---------------------------------------------------------------------------

def test_delta_inside_band_no_rebalance():
    assert not delta_rebalance_decision(0.01, band_pct=1.5, hours_since_last_rebalance=None)


def test_delta_outside_band_rebalances():
    assert delta_rebalance_decision(0.02, band_pct=1.5, hours_since_last_rebalance=None)


def test_delta_churn_guard_blocks_within_window():
    assert not delta_rebalance_decision(0.02, band_pct=1.5, hours_since_last_rebalance=0.5)


def test_delta_churn_guard_yields_to_2x_band():
    assert delta_rebalance_decision(0.031, band_pct=1.5, hours_since_last_rebalance=0.5)


def test_delta_churn_guard_expires_after_window():
    assert delta_rebalance_decision(0.02, band_pct=1.5, hours_since_last_rebalance=1.5)


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------

def test_liquidation_ok_outside_buffer():
    assert (
        liquidation_action(
            0.30, liq_buffer_pct=0.15, available_margin_usd=100, margin_topup_usd=50
        )
        == "ok"
    )


def test_liquidation_adds_margin_first():
    assert (
        liquidation_action(
            0.10, liq_buffer_pct=0.15, available_margin_usd=100, margin_topup_usd=50
        )
        == "add_margin"
    )


def test_liquidation_reduces_when_no_margin():
    assert (
        liquidation_action(
            0.10, liq_buffer_pct=0.15, available_margin_usd=10, margin_topup_usd=50
        )
        == "reduce"
    )


def test_drawdown_halt():
    assert drawdown_halted(1_000.0, 910.0, max_drawdown_pct=8.0)
    assert not drawdown_halted(1_000.0, 930.0, max_drawdown_pct=8.0)
    assert not drawdown_halted(0.0, 0.0, max_drawdown_pct=8.0)


def test_stale_data_guard():
    now = 1_000_000.0
    assert is_stale(None, now, funding_interval_hours=1.0, max_intervals=2)
    assert is_stale(now - 3 * HOUR, now, funding_interval_hours=1.0, max_intervals=2)
    assert not is_stale(now - 1.5 * HOUR, now, funding_interval_hours=1.0, max_intervals=2)
    # 8h-interval venue tolerates proportionally older data
    assert not is_stale(now - 10 * HOUR, now, funding_interval_hours=8.0, max_intervals=2)


# ---------------------------------------------------------------------------
# Boros rate lock
# ---------------------------------------------------------------------------

def test_lock_opens_above_premium_threshold():
    d = lock_decision(0.15, 0.10, premium_threshold_bps=200, locked=False)
    assert d.action == "open"
    assert d.premium_bps == 500


def test_lock_stays_closed_below_threshold():
    d = lock_decision(0.11, 0.10, premium_threshold_bps=200, locked=False)
    assert d.action == "none"


def test_lock_holds_while_premium_positive():
    d = lock_decision(0.12, 0.10, premium_threshold_bps=200, locked=True)
    assert d.action == "hold"


def test_lock_unwinds_on_premium_inversion():
    d = lock_decision(0.08, 0.10, premium_threshold_bps=200, locked=True)
    assert d.action == "unwind"


# ---------------------------------------------------------------------------
# Sizing + idempotency
# ---------------------------------------------------------------------------

def test_required_margin_includes_buffer():
    assert required_margin_usd(3_000.0, 3.0, 0.25) == pytest.approx(1_250.0)
    with pytest.raises(ValueError):
        required_margin_usd(1_000.0, 0.0, 0.25)


def test_idempotency_key_is_deterministic_within_bucket():
    t0 = 900.0 * 1_111  # bucket-aligned
    b1 = epoch_bucket(t0)
    b2 = epoch_bucket(t0 + 899.0)
    b3 = epoch_bucket(t0 + 901.0)
    k1 = idempotency_key("funding-rate-harvester", "hyperliquid", "eth", "deposit", b1)
    k2 = idempotency_key("funding-rate-harvester", "hyperliquid", "ETH", "deposit", b2)
    assert k1 == k2  # same bucket + case-insensitive asset → crash re-run dedupes
    assert k1 != idempotency_key("funding-rate-harvester", "hyperliquid", "ETH", "deposit", b3)
