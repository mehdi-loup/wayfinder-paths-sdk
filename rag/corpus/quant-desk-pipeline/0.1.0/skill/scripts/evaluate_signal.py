"""
3-phase evaluation harness for a candidate regime signal.

Usage:

    from examples.evaluate_signal import evaluate_signal
    verdict = await evaluate_signal(
        signal_fn=my_signal_fn,
        signal_name="my_signal",
        symbols=["BTC", "ETH", "SOL"],   # picked in Phase 4b
        start="2024-01-01",
        end="2026-04-15",
        bar_interval="1h",
        forward_bars=24,
    )
    print(verdict["verdict"])  # PASS / HOLD / REJECT

The pass/fail thresholds are fixed to prevent p-hacking. The signal-side knobs
(bar interval, forward horizon, universe, data source) are *configurable* so
each paper can be tested at its native frequency rather than shoehorned into
1h/24h. Phase 4b (universe selection) chooses `symbols`; the harness does not
silently override.

Phase 1: bucket forward-return diagnostic    →  spread ≥ 50 bps + rank-coherent
Phase 2: rolling-rank gated long-only        →  Sharpe ≥ 0.5, beats baseline
Phase 3: walk-forward split (60/40)          →  test Sharpe ≥ 0.5, ≥ 50% of train

Data source defaults to **CCXT Binance** spot, which has multi-year history back
to ~2017 for majors. Pass `data_source="hyperliquid"` only for HL-native symbols
(HIP-3 perps, HYPE, etc.).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting import (
    fetch_funding_rates,
    fetch_lending_rates,
    fetch_prices,
    quick_backtest,
)

# Bars per year per interval (used to annualize bucket-level Sharpe).
_BARS_PER_YEAR: dict[str, int] = {
    "15m": 365 * 24 * 4,
    "1h": 365 * 24,
    "4h": 365 * 6,
    "1d": 365,
}

# Default rolling-rank window in *bars* per interval (~30 calendar days).
_DEFAULT_ROLL_WINDOW_BARS: dict[str, int] = {
    "15m": 30 * 24 * 4,
    "1h": 30 * 24,
    "4h": 30 * 6,
    "1d": 30,
}

DEFAULTS: dict[str, Any] = {
    # Universe + window are caller-provided (Phase 4b picks them).
    "symbols": ["BTC", "ETH"],
    "start": "2024-01-01",
    "end": "2026-04-15",
    # Horizon. forward_bars is in *bars*, so "1d-ahead on daily" = forward_bars=1
    # and "1d-ahead on hourly" = forward_bars=24.
    "bar_interval": "1h",
    "forward_bars": 24,
    # None → falls back to _DEFAULT_ROLL_WINDOW_BARS[bar_interval].
    "roll_window_bars": None,
    "n_buckets": 4,
    "train_frac": 0.60,
    # "ccxt" (Binance spot, multi-year) | "hyperliquid" (HL-native only).
    "data_source": "ccxt",
    # Phase 1
    "phase1_min_spread_bps": 50.0,
    # Phase 2
    "phase2_min_sharpe": 0.5,
    "phase2_sharpe_over_baseline": 0.5,
    "phase2_max_mdd_ratio": 0.60,
    "phase2_max_trades": 250,
    # Phase 3
    "phase3_min_test_sharpe": 0.5,
    "phase3_test_train_ratio": 0.50,
}

SignalFn = Callable[[pd.DataFrame, pd.DataFrame, dict | None], pd.DataFrame]


def _bucket_forward_returns(
    signal: pd.DataFrame,
    prices: pd.DataFrame,
    forward_bars: int,
    n_buckets: int,
    periods_per_year: int,
) -> pd.DataFrame:
    fwd = prices.pct_change(forward_bars).shift(-forward_bars)
    rows = []
    for sym in prices.columns:
        if sym not in signal.columns:
            continue
        s = signal[sym].dropna()
        r = fwd[sym]
        merged = pd.concat([s, r], axis=1, join="inner").dropna()
        if len(merged) < 200:
            continue
        merged.columns = ["signal", "return"]
        try:
            merged["bucket"] = pd.qcut(
                merged["signal"], n_buckets, labels=False, duplicates="drop"
            )
        except ValueError:
            continue

        ann_scale = np.sqrt(periods_per_year / forward_bars)
        for b in sorted(merged["bucket"].dropna().unique()):
            grp = merged[merged["bucket"] == b]["return"]
            if len(grp) == 0:
                continue
            sharpe = grp.mean() / grp.std() * ann_scale if grp.std() > 0 else 0.0
            rows.append(
                {
                    "symbol": sym,
                    "bucket": int(b),
                    "n": len(grp),
                    "mean_ret_bps": grp.mean() * 10_000,
                    "sharpe": sharpe,
                }
            )
    return pd.DataFrame(rows)


def _phase1_verdict(bucket_df: pd.DataFrame, min_spread_bps: float) -> dict[str, Any]:
    """Phase 1: quartile diagnostic.

    Pass if |top-vs-bottom spread| ≥ threshold AND signal structure is interpretable:
    either strictly monotone, or the extreme bucket (min or max by forward return)
    is the same across symbols (tail consistency).

    Pure middle-bucket effects are rejected as likely noise.
    """
    if bucket_df.empty:
        return {"pass": False, "reason": "no bucket data", "metrics": {}}

    per_symbol = {}
    favor_high_votes = []
    worst_buckets: list[int] = []
    best_buckets: list[int] = []
    for sym, sub in bucket_df.groupby("symbol"):
        sub = sub.sort_values("bucket")
        means = sub["mean_ret_bps"].to_numpy()
        buckets = sub["bucket"].to_numpy()
        if len(means) < 2:
            continue
        spread = means[-1] - means[0]
        monotone_up = all(means[i] <= means[i + 1] for i in range(len(means) - 1))
        monotone_down = all(means[i] >= means[i + 1] for i in range(len(means) - 1))
        monotone = monotone_up or monotone_down
        worst = int(buckets[int(np.argmin(means))])
        best = int(buckets[int(np.argmax(means))])
        per_symbol[sym] = {
            "spread_bps": float(spread),
            "monotone": bool(monotone),
            "worst_bucket": worst,
            "best_bucket": best,
            "buckets": sub[["bucket", "mean_ret_bps", "sharpe", "n"]].to_dict("records"),
        }
        favor_high_votes.append(spread > 0)
        worst_buckets.append(worst)
        best_buckets.append(best)

    if not per_symbol:
        return {"pass": False, "reason": "no per-symbol stats", "metrics": {}}

    mean_abs_spread = float(np.mean([abs(v["spread_bps"]) for v in per_symbol.values()]))
    any_monotone = any(v["monotone"] for v in per_symbol.values())
    worst_consistent = len(set(worst_buckets)) == 1 if worst_buckets else False
    best_consistent = len(set(best_buckets)) == 1 if best_buckets else False
    tail_consistent = worst_consistent or best_consistent
    # favor_high: long when signal is high. True if high-bucket returns > low-bucket on average.
    favor_high = sum(favor_high_votes) > len(favor_high_votes) / 2.0

    ok = mean_abs_spread >= min_spread_bps and (any_monotone or tail_consistent)
    # An inverted signal (low buckets > high buckets across symbols) is a *finding*,
    # not a failure: -signal has edge. Flag so the report can surface it.
    inverted_edge = (
        mean_abs_spread >= min_spread_bps
        and (any_monotone or tail_consistent)
        and not favor_high
    )
    reason = (
        f"mean |spread|={mean_abs_spread:.1f}bps "
        f"(≥{min_spread_bps}? {mean_abs_spread >= min_spread_bps}), "
        f"monotone={any_monotone}, tail_consistent={tail_consistent} "
        f"(worst buckets={worst_buckets}, best buckets={best_buckets})"
    )
    return {
        "pass": ok,
        "favor_high": favor_high,
        "inverted_edge": inverted_edge,
        "reason": reason,
        "metrics": {
            "mean_abs_spread_bps": mean_abs_spread,
            "any_monotone": any_monotone,
            "tail_consistent": tail_consistent,
            "worst_buckets": worst_buckets,
            "best_buckets": best_buckets,
            "per_symbol": per_symbol,
            "inverted_edge": inverted_edge,
        },
    }


def _make_gate_strategy(
    signal: pd.DataFrame,
    roll_window_bars: int,
    favor_high: bool,
    quantile: float = 0.25,
):
    def strategy(prices: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        sig = signal.reindex(prices.index).ffill()
        rank = sig.rolling(roll_window_bars).rank(pct=True)
        gate = (rank > (1 - quantile)) if favor_high else (rank < quantile)
        return (gate.astype(float) / len(prices.columns)).fillna(0.0)

    return strategy


def _buy_hold(prices: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    return pd.DataFrame(1.0 / len(prices.columns), index=prices.index, columns=prices.columns)


async def _run_backtest(
    strategy_fn,
    symbols: list[str],
    start: str,
    end: str,
    bar_interval: str,
) -> dict[str, Any]:
    result = await quick_backtest(
        strategy_fn,
        symbols,
        start,
        end,
        leverage=1.0,
        include_funding=True,
        interval=bar_interval,
    )
    return dict(result.stats)


async def _fetch_prices_for_eval(
    symbols: list[str],
    start: str,
    end: str,
    bar_interval: str,
    data_source: str,
) -> pd.DataFrame:
    """Phase-5-default fetcher. CCXT Binance for multi-year coverage; HL for native symbols.

    `quick_backtest` keeps using its own data path — this is only for signal
    computation (Phase 1 bucket diagnostic). Using the same source for both is
    a future cleanup; the present code accepts mild source asymmetry because
    closes from CCXT and HL track each other tightly for liquid majors.
    """
    if data_source == "ccxt":
        try:
            from .fetch_ccxt_history import fetch_multi_symbol_history  # type: ignore
        except ImportError:
            from fetch_ccxt_history import fetch_multi_symbol_history  # type: ignore

        pairs = [f"{s}/USDT" for s in symbols]
        prices = await fetch_multi_symbol_history(pairs, start, end, interval=bar_interval)
        return prices
    if data_source == "hyperliquid":
        return await fetch_prices(symbols, start, end, interval=bar_interval, source="hyperliquid")
    raise ValueError(f"Unknown data_source: {data_source!r} (expected 'ccxt' or 'hyperliquid')")


async def _phase2_evaluate(
    signal: pd.DataFrame,
    symbols: list[str],
    start: str,
    end: str,
    roll_window_bars: int,
    favor_high: bool,
    bar_interval: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    baseline = await _run_backtest(_buy_hold, symbols, start, end, bar_interval)
    gate = _make_gate_strategy(signal, roll_window_bars, favor_high)
    gated = await _run_backtest(gate, symbols, start, end, bar_interval)

    base_sharpe = baseline.get("sharpe", 0.0) or 0.0
    base_mdd = baseline.get("max_drawdown", 0.0) or 0.0
    g_sharpe = gated.get("sharpe", 0.0) or 0.0
    g_mdd = gated.get("max_drawdown", 0.0) or 0.0
    g_trades = gated.get("trade_count", 0) or 0

    # MDD is negative; |g_mdd| / |base_mdd| ≤ threshold means smaller drawdown
    mdd_ratio = abs(g_mdd) / abs(base_mdd) if base_mdd else 1.0
    sharpe_over = g_sharpe - base_sharpe

    checks = {
        "sharpe_min": g_sharpe >= thresholds["phase2_min_sharpe"],
        "sharpe_over_baseline": sharpe_over >= thresholds["phase2_sharpe_over_baseline"],
        "mdd_ratio_ok": mdd_ratio <= thresholds["phase2_max_mdd_ratio"],
        "trades_ok": g_trades <= thresholds["phase2_max_trades"],
    }
    ok = all(checks.values())

    return {
        "pass": ok,
        "reason": f"checks={checks}",
        "metrics": {
            "baseline": baseline,
            "gated": gated,
            "sharpe_over_baseline": sharpe_over,
            "mdd_ratio": mdd_ratio,
            "checks": checks,
        },
    }


def _split_dates(start: str, end: str, train_frac: float) -> tuple[str, str, str]:
    t0 = pd.Timestamp(start)
    t1 = pd.Timestamp(end)
    cut = t0 + (t1 - t0) * train_frac
    return start, cut.strftime("%Y-%m-%d"), end


async def _phase3_walk_forward(
    signal: pd.DataFrame,
    symbols: list[str],
    start: str,
    end: str,
    roll_window_bars: int,
    favor_high: bool,
    bar_interval: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    s_train, cut, s_test_end = _split_dates(start, end, thresholds["train_frac"])

    gate = _make_gate_strategy(signal, roll_window_bars, favor_high)
    train = await _run_backtest(gate, symbols, s_train, cut, bar_interval)
    test = await _run_backtest(gate, symbols, cut, s_test_end, bar_interval)

    train_sharpe = train.get("sharpe", 0.0) or 0.0
    test_sharpe = test.get("sharpe", 0.0) or 0.0
    ratio = (test_sharpe / train_sharpe) if train_sharpe > 0 else 0.0

    passed = (
        test_sharpe >= thresholds["phase3_min_test_sharpe"]
        and ratio >= thresholds["phase3_test_train_ratio"]
    )
    held = (not passed) and test_sharpe > 0
    verdict = "PASS" if passed else ("HOLD" if held else "REJECT")

    return {
        "verdict": verdict,
        "reason": (
            f"train_sharpe={train_sharpe:.2f}, test_sharpe={test_sharpe:.2f}, "
            f"ratio={ratio:.2f}"
        ),
        "metrics": {
            "train": train,
            "test": test,
            "ratio": ratio,
            "split_cut": cut,
        },
    }


async def evaluate_signal(
    signal_fn: SignalFn,
    signal_name: str,
    *,
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    bar_interval: str | None = None,
    forward_bars: int | None = None,
    roll_window_bars: int | None = None,
    data_source: str | None = None,
    thresholds: dict[str, Any] | None = None,
    include_lending: bool = False,
) -> dict[str, Any]:
    """Run the 3-phase evaluation on a candidate signal.

    Signal-side knobs (callers should set these to match the paper):
      - bar_interval: "15m" | "1h" | "4h" | "1d". Drives data fetch and Sharpe annualization.
      - forward_bars: forward-return horizon in *bars* (e.g. 24 on 1h = 1 day).
      - roll_window_bars: rolling rank window. Defaults to ~30 calendar days for the interval.
      - data_source: "ccxt" (default, Binance spot, multi-year) | "hyperliquid".

    Universe knobs (set by Phase 4b, NOT by the harness):
      - symbols: tickers to test on.
      - start / end: window covering the requested period (CCXT supports multi-year).

    Returns:
      {"name", "verdict": PASS|HOLD|REJECT, "reason", "phases": {phase1, phase2, phase3}}
    """
    params = {**DEFAULTS, **(thresholds or {})}
    symbols = symbols or params["symbols"]
    start = start or params["start"]
    end = end or params["end"]
    bar_interval = bar_interval or params["bar_interval"]
    forward_bars = forward_bars if forward_bars is not None else params["forward_bars"]
    data_source = data_source or params["data_source"]

    if bar_interval not in _BARS_PER_YEAR:
        return {
            "name": signal_name,
            "verdict": "REJECT",
            "reason": f"unsupported bar_interval={bar_interval!r}; supported: {list(_BARS_PER_YEAR)}",
            "phases": {},
        }

    periods_per_year = _BARS_PER_YEAR[bar_interval]
    if roll_window_bars is None:
        roll_window_bars = params["roll_window_bars"] or _DEFAULT_ROLL_WINDOW_BARS[bar_interval]

    prices = await _fetch_prices_for_eval(symbols, start, end, bar_interval, data_source)
    # Funding history is still fetched via the existing path (HL-backed).
    # CCXT funding is best-effort and not all symbols have it; for signals that
    # depend on funding, prefer data_source="hyperliquid" or pre-fetch externally.
    try:
        funding = await fetch_funding_rates(symbols, start, end)
    except Exception:
        funding = pd.DataFrame(index=prices.index)
    lending = None
    if include_lending:
        lending = {
            "supply": {
                s: (await fetch_lending_rates(s, start, end)).get("supply")
                for s in symbols
            }
        }

    signal = signal_fn(prices, funding, lending)
    # Safety: reindex to prices, allow NaN
    signal = signal.reindex(prices.index)
    if not set(signal.columns) >= set(prices.columns):
        return {
            "name": signal_name,
            "verdict": "REJECT",
            "reason": f"signal columns {list(signal.columns)} missing prices columns {list(prices.columns)}",
            "phases": {},
        }

    # Phase 1
    bucket_df = _bucket_forward_returns(
        signal, prices, forward_bars, params["n_buckets"], periods_per_year
    )
    phase1 = _phase1_verdict(bucket_df, params["phase1_min_spread_bps"])
    if not phase1["pass"]:
        return {
            "name": signal_name,
            "verdict": "REJECT",
            "reason": f"Phase 1: {phase1['reason']}",
            "phases": {"phase1": phase1},
        }

    favor_high = phase1["favor_high"]

    # Phase 2
    phase2 = await _phase2_evaluate(
        signal, symbols, start, end, roll_window_bars, favor_high, bar_interval, params
    )
    if not phase2["pass"]:
        return {
            "name": signal_name,
            "verdict": "REJECT",
            "reason": f"Phase 2: {phase2['reason']}",
            "phases": {"phase1": phase1, "phase2": phase2},
        }

    # Phase 3
    phase3 = await _phase3_walk_forward(
        signal, symbols, start, end, roll_window_bars, favor_high, bar_interval, params
    )
    return {
        "name": signal_name,
        "verdict": phase3["verdict"],
        "reason": f"Phase 3: {phase3['reason']}",
        "phases": {"phase1": phase1, "phase2": phase2, "phase3": phase3},
    }


def format_verdict(result: dict[str, Any]) -> str:
    """Pretty-print a verdict for human consumption."""
    lines = [
        f"=== {result['name']} ===",
        f"Verdict: {result['verdict']}",
        f"Reason: {result['reason']}",
    ]
    phases = result.get("phases", {})
    if "phase1" in phases:
        p1 = phases["phase1"]
        inv = " (inverted edge: -signal has the spread)" if p1.get("inverted_edge") else ""
        lines.append(
            f"Phase 1: {'PASS' if p1['pass'] else 'FAIL'} — "
            f"mean |spread|={p1['metrics'].get('mean_abs_spread_bps', 0):.1f}bps, "
            f"monotone={p1['metrics'].get('any_monotone', False)}{inv}"
        )
    if "phase2" in phases:
        p2 = phases["phase2"]
        m = p2["metrics"]
        b = m.get("baseline", {})
        g = m.get("gated", {})
        lines.append(
            f"Phase 2: {'PASS' if p2['pass'] else 'FAIL'}"
            f" — baseline sharpe={b.get('sharpe', 0):.2f}"
            f" | gated sharpe={g.get('sharpe', 0):.2f}"
            f" ret={g.get('total_return', 0):.2%}"
            f" mdd={g.get('max_drawdown', 0):.2%}"
            f" trades={g.get('trade_count', 0)}"
        )
    if "phase3" in phases:
        p3 = phases["phase3"]
        m = p3["metrics"]
        tr = m.get("train", {})
        te = m.get("test", {})
        lines.append(
            f"Phase 3: train_sharpe={tr.get('sharpe', 0):.2f}, "
            f"test_sharpe={te.get('sharpe', 0):.2f}, "
            f"ratio={m.get('ratio', 0):.2f}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Quick self-test: vol_zscore known-pass signal (from our prior research)
    import sys

    def vol_zscore_signal(prices, funding, lending):
        r = prices.pct_change()
        vol = r.rolling(24).std() * np.sqrt(24)
        return (vol - vol.rolling(720).mean()) / vol.rolling(720).std()

    async def _test() -> None:
        verdict = await evaluate_signal(vol_zscore_signal, "vol_zscore_selftest")
        print(format_verdict(verdict))

    asyncio.run(_test())
