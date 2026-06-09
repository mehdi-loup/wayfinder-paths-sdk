"""
Quote-rotation constraint engine for the stablecoin-yield-rotator path.

Given a flat scan and a list of current positions, propose rotations subject to:
- min_apy_delta_bps: minimum APY uplift required to rotate
- gas_amortization_days: rotation must pay back gas within this window
- max_gas_usd_per_rotation: hard ceiling on per-rotation gas spend
- max_position_pct_per_venue: diversification cap
- utilization spike guard: skip target if util > 95% or supply-cap headroom < 5% of position size
- cross-chain bridge gating: only when uplift_usd × payback_days > bridge_fee_usd × 2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from venues import EXECUTABLE_VENUES, Position, VenueRow

UTIL_SPIKE_CEILING = 0.95
HEADROOM_FRACTION_FLOOR = 0.05
DEFAULT_MAX_STABLECOIN_APY = 0.50


class DiscoveryGapError(RuntimeError):
    """Raised when quote_rotation can't resolve a current position's APY from the scan."""

# Same-chain rotations are typically cheap; cross-chain involves a bridge plus
# two writes. Gas estimates here are coarse defaults; refine with `eth_estimateGas`
# or by feeding measured numbers through `--gas-overrides` in `quote-rotation`.
DEFAULT_SAME_CHAIN_GAS_USD = 4.0
DEFAULT_CROSS_CHAIN_GAS_USD = 12.0
DEFAULT_BRIDGE_FEE_USD = 6.0


@dataclass
class RotationLeg:
    asset_symbol: str
    from_venue: str | None
    from_chain_id: int | None
    from_market_id: str | None
    to_venue: str
    to_chain_id: int
    to_market_id: str
    raw_amount: int
    decimals: int
    current_apy: float
    target_apy: float
    apy_delta_bps: int
    estimated_uplift_usd_30d: float
    estimated_gas_usd: float
    estimated_bridge_fee_usd: float
    payback_days: float
    is_cross_chain: bool
    skipped: bool = False
    skip_reason: str | None = None
    # Populated for cross-chain legs after planning. Carries the BRAP quote that the
    # user confirms; execution re-quotes and refuses to broadcast if it's materially worse.
    bridge_quote: dict[str, Any] | None = None
    # Source/destination underlying token addresses for the cross-chain leg. Cached on
    # the plan to avoid having to re-scan during execution.
    bridge_from_token: str | None = None
    bridge_to_token: str | None = None
    # Source underlying token address — execution measures the wallet balance delta of
    # this token across the withdraw so the deposit/bridge uses what was actually
    # redeemed (a venue may redeem less than the planned amount, e.g. an ERC-4626 maxRedeem cap).
    from_asset_address: str | None = None


@dataclass
class RotationPlan:
    legs: list[RotationLeg] = field(default_factory=list)
    skipped: list[RotationLeg] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "legs": [leg_to_dict(leg) for leg in self.legs],
            "skipped": [leg_to_dict(leg) for leg in self.skipped],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RotationPlan:
        plan = cls()
        plan.legs = [leg_from_dict(leg) for leg in d.get("legs", [])]
        plan.skipped = [leg_from_dict(leg) for leg in d.get("skipped", [])]
        return plan


def leg_to_dict(leg: RotationLeg) -> dict:
    return {
        "asset_symbol": leg.asset_symbol,
        "from": None if leg.from_venue is None else f"{leg.from_venue}@{leg.from_chain_id}",
        "from_market_id": leg.from_market_id,
        "to": f"{leg.to_venue}@{leg.to_chain_id}",
        "to_market_id": leg.to_market_id,
        "decimals": leg.decimals,
        "raw_amount": leg.raw_amount,
        "human_amount": leg.raw_amount / (10 ** leg.decimals),
        "current_apy": round(leg.current_apy, 6),
        "target_apy": round(leg.target_apy, 6),
        "apy_delta_bps": leg.apy_delta_bps,
        "estimated_uplift_usd_30d": round(leg.estimated_uplift_usd_30d, 4),
        "estimated_gas_usd": round(leg.estimated_gas_usd, 4),
        "estimated_bridge_fee_usd": round(leg.estimated_bridge_fee_usd, 4),
        "payback_days": round(leg.payback_days, 2) if leg.payback_days != float("inf") else None,
        "is_cross_chain": leg.is_cross_chain,
        "skipped": leg.skipped,
        "skip_reason": leg.skip_reason,
        "bridge_quote": leg.bridge_quote,
        "bridge_from_token": leg.bridge_from_token,
        "bridge_to_token": leg.bridge_to_token,
        "from_asset_address": leg.from_asset_address,
    }


def leg_from_dict(d: dict[str, Any]) -> RotationLeg:
    from_str = d.get("from")
    from_venue: str | None = None
    from_chain_id: int | None = None
    if from_str:
        venue_part, chain_part = from_str.rsplit("@", 1)
        from_venue, from_chain_id = venue_part, int(chain_part)
    to_venue, to_chain_id = d["to"].rsplit("@", 1)
    pd = d.get("payback_days")
    return RotationLeg(
        asset_symbol=d["asset_symbol"],
        from_venue=from_venue,
        from_chain_id=from_chain_id,
        from_market_id=d.get("from_market_id"),
        to_venue=to_venue,
        to_chain_id=int(to_chain_id),
        to_market_id=d["to_market_id"],
        raw_amount=d["raw_amount"],
        decimals=d["decimals"],
        current_apy=d["current_apy"],
        target_apy=d["target_apy"],
        apy_delta_bps=d["apy_delta_bps"],
        estimated_uplift_usd_30d=d["estimated_uplift_usd_30d"],
        estimated_gas_usd=d["estimated_gas_usd"],
        estimated_bridge_fee_usd=d["estimated_bridge_fee_usd"],
        payback_days=float(pd) if pd is not None else float("inf"),
        is_cross_chain=d["is_cross_chain"],
        skipped=d.get("skipped", False),
        skip_reason=d.get("skip_reason"),
        bridge_quote=d.get("bridge_quote"),
        bridge_from_token=d.get("bridge_from_token"),
        bridge_to_token=d.get("bridge_to_token"),
        from_asset_address=d.get("from_asset_address"),
    )


def _best_target_for_asset(rows: list[VenueRow], asset: str) -> list[VenueRow]:
    """Return rows for `asset` sorted by supply_apy descending. Only executable venues are eligible
    targets — non-executable venues remain visible via scan/status but won't be planned for rotation."""
    matching = [
        r for r in rows
        if r.asset_symbol == asset and r.venue in EXECUTABLE_VENUES
        and not r.is_frozen and not r.is_paused
    ]
    matching.sort(key=lambda r: r.supply_apy, reverse=True)
    return matching


def _passes_target_guard(
    row: VenueRow,
    raw_amount: int,
    *,
    min_target_tvl_usd: float | None = None,
    max_target_apy: float | None = DEFAULT_MAX_STABLECOIN_APY,
) -> tuple[bool, str | None]:
    if max_target_apy is not None and row.supply_apy > max_target_apy:
        return False, f"supply_apy {row.supply_apy:.2%} > max_target_apy {max_target_apy:.0%}"
    if row.utilization is not None and row.utilization > UTIL_SPIKE_CEILING:
        return False, f"utilization {row.utilization:.2%} > {UTIL_SPIKE_CEILING:.0%}"
    if min_target_tvl_usd is not None:
        if row.tvl_usd is None:
            return False, "missing tvl_usd"
        if row.tvl_usd < min_target_tvl_usd:
            return False, f"tvl_usd {row.tvl_usd:.2f} < min_target_tvl_usd {min_target_tvl_usd:.2f}"
    if row.supply_cap_headroom_raw is not None and raw_amount > 0:
        if row.supply_cap_headroom_raw < raw_amount * HEADROOM_FRACTION_FLOOR:
            return False, f"supply_cap_headroom < {HEADROOM_FRACTION_FLOOR:.0%} of position"
    return True, None


def _payback_days(uplift_usd_30d: float, gas_usd: float, bridge_fee_usd: float) -> float:
    """Days to break even on rotation cost via uplift APY."""
    cost = gas_usd + bridge_fee_usd
    if cost <= 0:
        return 0.0
    daily_uplift = uplift_usd_30d / 30.0
    if daily_uplift <= 0:
        return float("inf")
    return cost / daily_uplift


def _aggregate_positions(positions: list[Position]) -> dict[str, list[Position]]:
    """Group positions by asset_symbol."""
    by_asset: dict[str, list[Position]] = {}
    for p in positions:
        by_asset.setdefault(p.asset_symbol, []).append(p)
    return by_asset


def quote_rotation(
    scan: list[VenueRow],
    positions: list[Position],
    *,
    min_apy_delta_bps: int = 50,
    gas_amortization_days: int = 30,
    max_gas_usd_per_rotation: float = 25.0,
    max_position_pct_per_venue: int = 50,
    blocklist_markets: list[str] | None = None,
    same_chain_gas_usd: float = DEFAULT_SAME_CHAIN_GAS_USD,
    cross_chain_gas_usd: float = DEFAULT_CROSS_CHAIN_GAS_USD,
    bridge_fee_usd: float = DEFAULT_BRIDGE_FEE_USD,
    asset_price_usd: float = 1.0,
    min_target_tvl_usd: float | None = None,
    max_target_apy: float | None = DEFAULT_MAX_STABLECOIN_APY,
) -> RotationPlan:
    """Build a rotation plan that satisfies all configured constraints.

    `asset_price_usd` defaults to 1.0 because we operate strictly on stablecoins;
    override per-asset if you want to model depegs.
    """
    blocked = {m.lower() for m in (blocklist_markets or [])}
    plan = RotationPlan()

    by_asset = _aggregate_positions(positions)
    total_supply_by_asset = {
        a: sum(p.supply_raw / (10 ** p.decimals) for p in ps) for a, ps in by_asset.items()
    }

    # Diversification cap: cap the rotation size into any single venue.
    venue_cap_fraction = max_position_pct_per_venue / 100.0

    for asset, asset_positions in by_asset.items():
        targets = [r for r in _best_target_for_asset(scan, asset) if r.market_id.lower() not in blocked]
        if not targets:
            continue

        for pos in asset_positions:
            if pos.venue not in EXECUTABLE_VENUES:
                # We can't unlend from a non-executable venue (no adapter write methods). Skip it
                # with a clear reason instead of producing an unexecutable leg.
                plan.skipped.append(RotationLeg(
                    asset_symbol=asset, from_venue=pos.venue, from_chain_id=pos.chain_id,
                    from_market_id=pos.market_id, to_venue="", to_chain_id=0, to_market_id="",
                    raw_amount=pos.supply_raw, decimals=pos.decimals,
                    current_apy=0.0, target_apy=0.0, apy_delta_bps=0,
                    estimated_uplift_usd_30d=0.0, estimated_gas_usd=0.0,
                    estimated_bridge_fee_usd=0.0, payback_days=0.0,
                    is_cross_chain=False, skipped=True,
                    skip_reason=f"source venue {pos.venue!r} is not executable in this path",
                ))
                continue
            current_apy_candidates = [
                r.supply_apy for r in scan
                if r.venue == pos.venue and r.chain_id == pos.chain_id and r.market_id == pos.market_id
            ]
            if not current_apy_candidates:
                # The position's own market is missing from scan → discovery is incomplete.
                # Refuse to plan rather than inflate apy_delta_bps using a 0.0 default.
                raise DiscoveryGapError(
                    f"current market {pos.venue}@{pos.chain_id}/{pos.market_id} missing from scan; "
                    "rerun with strict scan or add the venue to the scan inputs"
                )
            current_apy = current_apy_candidates[0]

            # Find the best target that beats current_apy by min_apy_delta_bps and
            # passes constraints. Walk down the ranked list so utilization-spike
            # skips fall through to the second-best.
            chosen: VenueRow | None = None
            chosen_skip_reason: str | None = None
            for cand in targets:
                if cand.venue == pos.venue and cand.chain_id == pos.chain_id and cand.market_id == pos.market_id:
                    continue
                delta_bps = int(round((cand.supply_apy - current_apy) * 10_000))
                if delta_bps < min_apy_delta_bps:
                    chosen_skip_reason = f"apy_delta {delta_bps}bps < min {min_apy_delta_bps}bps"
                    break  # ranked list — nothing below will beat min_apy_delta_bps

                ok, reason = _passes_target_guard(
                    cand,
                    pos.supply_raw,
                    min_target_tvl_usd=min_target_tvl_usd,
                    max_target_apy=max_target_apy,
                )
                if not ok:
                    chosen_skip_reason = reason
                    continue
                chosen = cand
                break

            if chosen is None:
                leg = RotationLeg(
                    asset_symbol=asset,
                    from_venue=pos.venue,
                    from_chain_id=pos.chain_id,
                    from_market_id=pos.market_id,
                    to_venue="",
                    to_chain_id=0,
                    to_market_id="",
                    raw_amount=pos.supply_raw,
                    decimals=pos.decimals,
                    current_apy=current_apy,
                    target_apy=0.0,
                    apy_delta_bps=0,
                    estimated_uplift_usd_30d=0.0,
                    estimated_gas_usd=0.0,
                    estimated_bridge_fee_usd=0.0,
                    payback_days=0.0,
                    is_cross_chain=False,
                    skipped=True,
                    skip_reason=chosen_skip_reason or "no candidate beat min_apy_delta_bps",
                )
                plan.skipped.append(leg)
                continue

            # Diversification cap: size the rotation so the target venue does not exceed
            # max_position_pct_per_venue of total `asset` portfolio. Exclude the source
            # position itself from the "currently in target" count — we're moving it.
            raw_amount = pos.supply_raw
            current_into_target = sum(
                p.supply_raw / (10 ** p.decimals)
                for p in asset_positions
                if p.venue == chosen.venue and p.chain_id == chosen.chain_id
                and (p.venue, p.chain_id, p.market_id) != (pos.venue, pos.chain_id, pos.market_id)
            )
            total = total_supply_by_asset.get(asset) or 0.0
            if total > 0:
                max_into_target = total * venue_cap_fraction
                allowed_human = max(0.0, max_into_target - current_into_target)
                allowed_raw = int(allowed_human * (10 ** pos.decimals))
                if allowed_raw <= 0:
                    leg = RotationLeg(
                        asset_symbol=asset,
                        from_venue=pos.venue,
                        from_chain_id=pos.chain_id,
                        from_market_id=pos.market_id,
                        to_venue=chosen.venue,
                        to_chain_id=chosen.chain_id,
                        to_market_id=chosen.market_id,
                        raw_amount=pos.supply_raw,
                        decimals=pos.decimals,
                        current_apy=current_apy,
                        target_apy=chosen.supply_apy,
                        apy_delta_bps=int(round((chosen.supply_apy - current_apy) * 10_000)),
                        estimated_uplift_usd_30d=0.0,
                        estimated_gas_usd=0.0,
                        estimated_bridge_fee_usd=0.0,
                        payback_days=0.0,
                        is_cross_chain=chosen.chain_id != pos.chain_id,
                        skipped=True,
                        skip_reason=f"diversification_cap {max_position_pct_per_venue}% already reached on {chosen.venue}@{chosen.chain_id}",
                    )
                    plan.skipped.append(leg)
                    continue
                raw_amount = min(raw_amount, allowed_raw)

            is_cross_chain = chosen.chain_id != pos.chain_id
            target_apy = chosen.supply_apy
            apy_delta_bps = int(round((target_apy - current_apy) * 10_000))

            position_usd = (raw_amount / (10 ** pos.decimals)) * asset_price_usd
            uplift_usd_30d = position_usd * max(0.0, target_apy - current_apy) * (30.0 / 365.0)
            gas_usd = cross_chain_gas_usd if is_cross_chain else same_chain_gas_usd
            bridge_usd = bridge_fee_usd if is_cross_chain else 0.0
            payback = _payback_days(uplift_usd_30d, gas_usd, bridge_usd)

            leg = RotationLeg(
                asset_symbol=asset,
                from_venue=pos.venue,
                from_chain_id=pos.chain_id,
                from_market_id=pos.market_id,
                to_venue=chosen.venue,
                to_chain_id=chosen.chain_id,
                to_market_id=chosen.market_id,
                raw_amount=raw_amount,
                decimals=pos.decimals,
                current_apy=current_apy,
                target_apy=target_apy,
                apy_delta_bps=apy_delta_bps,
                estimated_uplift_usd_30d=uplift_usd_30d,
                estimated_gas_usd=gas_usd,
                estimated_bridge_fee_usd=bridge_usd,
                payback_days=payback,
                is_cross_chain=is_cross_chain,
                from_asset_address=pos.asset_address,
            )

            # Constraint checks
            if gas_usd > max_gas_usd_per_rotation:
                leg.skipped = True
                leg.skip_reason = f"gas {gas_usd:.2f} > max_gas_usd_per_rotation {max_gas_usd_per_rotation}"
                plan.skipped.append(leg)
                continue
            if payback > gas_amortization_days:
                leg.skipped = True
                leg.skip_reason = f"payback {payback:.1f}d > gas_amortization_days {gas_amortization_days}"
                plan.skipped.append(leg)
                continue
            if is_cross_chain and not (uplift_usd_30d * (gas_amortization_days / 30.0) > bridge_usd * 2):
                leg.skipped = True
                leg.skip_reason = "cross_chain uplift × payback_days <= bridge_fee × 2"
                plan.skipped.append(leg)
                continue

            plan.legs.append(leg)

    return plan
