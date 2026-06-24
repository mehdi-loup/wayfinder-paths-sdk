"""Compute safe leverage for a hedge leg via historical backtest.

Inspired by the basis trading strategy's _first_stop_horizon / leverage sweep
(wayfinder_paths/strategies/basis_trading_strategy/strategy.py).

The approach: sweep leverage from max down to 1. For each level, walk every
possible starting hour in the lookback window and simulate a short perp position.
If ANY starting point hits the stop threshold within the survival window, that
leverage is unsafe. Return the highest L that survives everywhere.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

CHECK_FREQUENCY_SURVIVAL_HOURS: dict[str, int] = {
    "hourly": 2,
    "daily": 36,
    "weekly": 192,
    "biweekly": 384,
}


def _safe_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _first_stop_horizon(
    *,
    prices: list[float],
    funding: list[float],
    start_idx: int,
    leverage: int,
    stop_frac: float,
    fee_eps: float,
    max_hours: int,
) -> int:
    """Walk forward from start_idx and return the hour at which the position
    would hit the stop threshold. Returns max_hours if it survives."""
    n = min(len(prices), len(funding)) - 1
    if start_idx >= n:
        return 0

    entry = prices[start_idx]
    if entry <= 0:
        return 1

    threshold = stop_frac * (1.0 / float(max(1, leverage)))
    peak = entry
    cum_neg_f = 0.0
    horizon = min(max_hours, n - start_idx)

    for j in range(1, horizon + 1):
        idx = start_idx + j
        p = prices[idx]
        if p > peak:
            peak = p

        runup = (peak / entry) - 1.0

        rate = funding[idx]
        if rate < 0.0:
            cum_neg_f += (-rate) * (1.0 + runup)

        req = runup + cum_neg_f + fee_eps
        if req >= threshold:
            return j

    return horizon


def compute_safe_leverage(
    *,
    price_series: pd.Series,
    funding_series: pd.Series,
    survival_hours: int,
    stop_frac: float = 0.75,
    fee_eps: float = 0.003,
    max_leverage: int = 5,
) -> dict:
    """Find the highest leverage where a short perp position survives
    `survival_hours` at every starting point in the history.

    Returns a dict with safe_leverage, survival_hours, worst_drawdown_pct,
    and per-leverage survival stats.
    """
    aligned = pd.concat(
        [price_series.rename("price"), funding_series.rename("funding")],
        axis=1,
    ).dropna()

    if len(aligned) < max(survival_hours, 24):
        return {
            "safe_leverage": 1,
            "survival_hours": survival_hours,
            "worst_drawdown_pct": 0.0,
            "insufficient_history": True,
            "history_hours": len(aligned),
            "leverage_details": [],
        }

    prices = aligned["price"].tolist()
    funding_rates = aligned["funding"].tolist()
    n = len(prices)

    # Track worst drawdown across all windows for reporting
    worst_drawdown = 0.0
    for i in range(n - 1):
        entry = prices[i]
        if entry <= 0:
            continue
        window_end = min(i + survival_hours, n - 1)
        peak = max(prices[i : window_end + 1])
        dd = (peak / entry) - 1.0
        if dd > worst_drawdown:
            worst_drawdown = dd

    leverage_details: list[dict] = []
    safe_leverage = 1

    for lev in range(max_leverage, 0, -1):
        failed = False
        min_survival = survival_hours
        fail_count = 0
        test_count = 0

        # Test every starting point that has enough runway
        for i in range(max(0, n - survival_hours - 1)):
            test_count += 1
            horizon = _first_stop_horizon(
                prices=prices,
                funding=funding_rates,
                start_idx=i,
                leverage=lev,
                stop_frac=stop_frac,
                fee_eps=fee_eps,
                max_hours=survival_hours,
            )
            if horizon < min_survival:
                min_survival = horizon
            if horizon < survival_hours:
                failed = True
                fail_count += 1

        detail = {
            "leverage": lev,
            "survived": not failed,
            "min_survival_hours": min_survival,
            "fail_count": fail_count,
            "test_count": test_count,
            "fail_rate": round(fail_count / max(test_count, 1), 4),
        }
        leverage_details.append(detail)

        if not failed:
            safe_leverage = lev
            break

    return {
        "safe_leverage": safe_leverage,
        "survival_hours": survival_hours,
        "worst_drawdown_pct": round(worst_drawdown * 100, 2),
        "insufficient_history": False,
        "history_hours": n,
        "leverage_details": leverage_details,
    }
