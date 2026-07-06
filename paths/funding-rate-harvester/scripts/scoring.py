"""
Pure scoring + decision math for the funding-rate-harvester path.

Everything here is deterministic and I/O-free: funding normalization across
venue funding intervals, EMA maintenance, net stacked-carry scoring, rotation
threshold/breakeven/dwell gating, negative-carry exit, delta-band rebalance
with churn guard, liquidation/drawdown/stale-data rails, and the Boros
rate-lock decision. main.py feeds it live data; tests feed it fixtures.

Conventions:
- APRs/APYs are decimal floats (0.10 = 10%). Thresholds are integer bps.
- Funding sign follows the perp convention: positive funding = shorts receive,
  negative = shorts pay. A short-perp harvester wants positive funding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

HOURS_PER_YEAR = 24.0 * 365.0

HYPERLIQUID_FUNDING_INTERVAL_HOURS = 1.0
# Most CEXs settle every 8h — matters from v1.1, normalized already.
DEFAULT_CEX_FUNDING_INTERVAL_HOURS = 8.0


def normalize_funding_apr(rate_per_interval: float, interval_hours: float) -> float:
    """Annualize a per-settlement funding rate for any venue interval."""
    if interval_hours <= 0:
        raise ValueError(f"interval_hours must be positive, got {interval_hours}")
    return rate_per_interval * (HOURS_PER_YEAR / interval_hours)


def ema_alpha(sample_interval_hours: float, ema_hours: float) -> float:
    """Decay-based alpha so EMA horizon is expressed in hours, not sample counts."""
    if ema_hours <= 0:
        return 1.0
    return 1.0 - math.exp(-max(sample_interval_hours, 0.0) / ema_hours)


def update_ema(prev_ema: float | None, sample: float, alpha: float) -> float:
    if prev_ema is None:
        return sample
    return prev_ema + alpha * (sample - prev_ema)


def cost_apr(total_cost_bps: float, holding_days: float) -> float:
    """Amortize a one-time round-trip cost (bps of notional) over a holding period."""
    if holding_days <= 0:
        raise ValueError(f"holding_days must be positive, got {holding_days}")
    return (total_cost_bps / 10_000.0) * (365.0 / holding_days)


def boros_fixed_apr(mid_apr_total_tenor: float, remaining_days: float) -> float:
    """Boros mid_apr is the TOTAL remaining-tenor yield, not annualized."""
    if remaining_days <= 0:
        return 0.0
    return (mid_apr_total_tenor / remaining_days) * 365.0


@dataclass
class CarryComponents:
    """Per-combo stacked carry decomposition. All fields annualized decimals."""

    funding_apr: float
    spot_leg_apy: float
    fee_apr: float = 0.0
    slippage_apr: float = 0.0
    financing_apr: float = 0.0

    @property
    def net_apr(self) -> float:
        return (
            self.funding_apr
            + self.spot_leg_apy
            - self.fee_apr
            - self.slippage_apr
            - self.financing_apr
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "funding_apr": self.funding_apr,
            "spot_leg_apy": self.spot_leg_apy,
            "fee_apr": self.fee_apr,
            "slippage_apr": self.slippage_apr,
            "financing_apr": self.financing_apr,
            "net_apr": self.net_apr,
        }


@dataclass
class ComboScore:
    """One (hedge venue, asset, spot leg) candidate with its stacked carry."""

    venue: str
    symbol: str
    spot_leg: str
    components: CarryComponents
    funding_interval_hours: float = HYPERLIQUID_FUNDING_INTERVAL_HOURS
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def net_apr(self) -> float:
        return self.components.net_apr

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "spot_leg": self.spot_leg,
            "funding_interval_hours": self.funding_interval_hours,
            **self.components.to_dict(),
            "meta": self.meta,
        }


def rank_combos(combos: list[ComboScore]) -> list[ComboScore]:
    return sorted(combos, key=lambda c: c.net_apr, reverse=True)


def best_combo_for_symbol(combos: list[ComboScore], symbol: str) -> ComboScore | None:
    ranked = rank_combos([c for c in combos if c.symbol == symbol])
    return ranked[0] if ranked else None


def breakeven_hours(
    migration_cost_usd: float, apr_delta: float, notional_usd: float
) -> float:
    """Hours for the incremental carry to pay back the migration cost."""
    if migration_cost_usd <= 0:
        return 0.0
    hourly_uplift_usd = notional_usd * apr_delta / HOURS_PER_YEAR
    if hourly_uplift_usd <= 0:
        return float("inf")
    return migration_cost_usd / hourly_uplift_usd


@dataclass
class RotationDecision:
    migrate: bool
    reason: str
    apr_delta_bps: int = 0
    breakeven_hours: float = float("inf")

    def to_dict(self) -> dict[str, Any]:
        return {
            "migrate": self.migrate,
            "reason": self.reason,
            "apr_delta_bps": self.apr_delta_bps,
            "breakeven_hours": (
                None if self.breakeven_hours == float("inf") else round(self.breakeven_hours, 2)
            ),
        }


def rotation_decision(
    current_net_apr: float,
    candidate_net_apr: float,
    *,
    notional_usd: float,
    migration_cost_usd: float,
    threshold_apr_bps: int,
    max_breakeven_hours: float,
    min_dwell_hours: float,
    hours_held: float,
    bypass_dwell: bool = False,
) -> RotationDecision:
    """Migrate only when threshold AND breakeven AND dwell all pass.

    `bypass_dwell` backs `rotate --force`: it relaxes only the dwell
    (minimum-hold) gate. The threshold and breakeven gates always apply.
    """
    apr_delta = candidate_net_apr - current_net_apr
    apr_delta_bps = int(round(apr_delta * 10_000))
    if not bypass_dwell and hours_held < min_dwell_hours:
        return RotationDecision(
            False,
            f"dwell {hours_held:.1f}h < min_dwell_hours {min_dwell_hours:.0f}h",
            apr_delta_bps,
        )
    if apr_delta_bps <= threshold_apr_bps:
        return RotationDecision(
            False,
            f"apr_delta {apr_delta_bps}bps <= threshold {threshold_apr_bps}bps",
            apr_delta_bps,
        )
    be_hours = breakeven_hours(migration_cost_usd, apr_delta, notional_usd)
    if be_hours >= max_breakeven_hours:
        be_label = "inf" if be_hours == float("inf") else f"{be_hours:.1f}h"
        return RotationDecision(
            False,
            f"breakeven {be_label} >= max_breakeven_hours {max_breakeven_hours:.0f}h",
            apr_delta_bps,
            be_hours,
        )
    return RotationDecision(
        True,
        f"apr_delta {apr_delta_bps}bps, breakeven {be_hours:.1f}h",
        apr_delta_bps,
        be_hours,
    )


def negative_carry_exit(
    best_net_apr: float,
    *,
    floor_bps: int,
    below_floor_since_ts: float | None,
    now_ts: float,
    grace_hours: float,
) -> tuple[bool, float | None]:
    """Exit to stables when the best available combo stays below the carry floor.

    Returns (should_exit, new_below_floor_since_ts). Caller persists the
    timestamp across cycles; recovery above the floor resets it.
    """
    if best_net_apr * 10_000 >= floor_bps:
        return False, None
    since = below_floor_since_ts if below_floor_since_ts is not None else now_ts
    hours_below = (now_ts - since) / 3600.0
    return hours_below >= grace_hours, since


def delta_rebalance_decision(
    delta_ratio: float,
    *,
    band_pct: float,
    hours_since_last_rebalance: float | None,
    churn_window_hours: float = 1.0,
    churn_multiplier: float = 2.0,
) -> bool:
    """Rebalance when |delta|/notional exceeds the band.

    Churn guard: within `churn_window_hours` of the previous rebalance, only
    re-rebalance if the drift exceeds `churn_multiplier`× the band.
    """
    drift_pct = abs(delta_ratio) * 100.0
    if drift_pct <= band_pct:
        return False
    if (
        hours_since_last_rebalance is not None
        and hours_since_last_rebalance < churn_window_hours
    ):
        return drift_pct > band_pct * churn_multiplier
    return True


LiquidationAction = Literal["ok", "add_margin", "reduce"]


def liquidation_action(
    liq_distance_pct: float,
    *,
    liq_buffer_pct: float,
    available_margin_usd: float,
    margin_topup_usd: float,
) -> LiquidationAction:
    """Add margin first, reduce size second, never skip."""
    if liq_distance_pct >= liq_buffer_pct:
        return "ok"
    if available_margin_usd >= margin_topup_usd and margin_topup_usd > 0:
        return "add_margin"
    return "reduce"


def drawdown_halted(
    reference_value_usd: float, current_value_usd: float, max_drawdown_pct: float
) -> bool:
    if reference_value_usd <= 0:
        return False
    drawdown_pct = (reference_value_usd - current_value_usd) / reference_value_usd * 100.0
    return drawdown_pct >= max_drawdown_pct


def is_stale(
    last_sample_ts: float | None,
    now_ts: float,
    *,
    funding_interval_hours: float,
    max_intervals: float,
) -> bool:
    if last_sample_ts is None:
        return True
    age_hours = (now_ts - last_sample_ts) / 3600.0
    return age_hours > funding_interval_hours * max_intervals


LockAction = Literal["open", "hold", "unwind", "none"]


@dataclass
class LockDecision:
    action: LockAction
    premium_bps: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "premium_bps": self.premium_bps, "reason": self.reason}


def lock_decision(
    floating_ema_apr: float,
    fixed_apr: float,
    *,
    premium_threshold_bps: int,
    locked: bool,
) -> LockDecision:
    """Boros lock gate: convert floating funding to fixed while the spread is rich.

    Opens the lock (short YU: receive fixed, pay floating) when the floating
    EMA exceeds the implied fixed by the configured premium; unwinds when the
    premium inverts (floating below fixed).
    """
    premium_bps = int(round((floating_ema_apr - fixed_apr) * 10_000))
    if locked:
        if premium_bps < 0:
            return LockDecision("unwind", premium_bps, "premium inverted (floating < fixed)")
        return LockDecision("hold", premium_bps, "lock active, premium not inverted")
    if premium_bps > premium_threshold_bps:
        return LockDecision(
            "open",
            premium_bps,
            f"floating exceeds fixed by {premium_bps}bps > {premium_threshold_bps}bps",
        )
    return LockDecision(
        "none",
        premium_bps,
        f"premium {premium_bps}bps <= threshold {premium_threshold_bps}bps",
    )


def required_margin_usd(
    notional_usd: float, leverage: float, margin_buffer_pct: float
) -> float:
    if leverage <= 0:
        raise ValueError(f"leverage must be positive, got {leverage}")
    return (notional_usd / leverage) * (1.0 + margin_buffer_pct)


def epoch_bucket(now_ts: float, bucket_seconds: int = 900) -> int:
    return int(now_ts // bucket_seconds)


def idempotency_key(
    path_slug: str, venue: str, asset: str, action: str, bucket: int
) -> str:
    """Ledger/state key so crash re-runs never double-execute a step.

    The (venue, asset) scoping is the v1.1 cross-venue saga foundation.
    """
    return f"{path_slug}:{venue}:{asset.upper()}:{action}:{bucket}"
