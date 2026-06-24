"""Reproduce the combined drift+stable velocity spread backtest.

Requires wayfinder-paths SDK with a valid API key in config.json.

Usage:
    poetry run python scripts/reproduce.py
    # Or via MCP: mcp__wayfinder__run_script(script_path="scripts/reproduce.py")
"""
import asyncio
import math
from itertools import combinations

import numpy as np
import pandas as pd

from wayfinder_paths.core.config import load_config
load_config("config.json")
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT
from wayfinder_paths.core.backtesting import run_backtest
from wayfinder_paths.core.backtesting.types import BacktestConfig

# ── Config ─────────────────────────────────────────────────────
DRIFT_PAIRS = [("NEAR", "AVAX"), ("WLD", "TAO"), ("TRB", "ETH")]
STABLE_PAIRS = [("TRB", "AVAX"), ("SOL", "SUI"), ("JUP", "ETH")]
DRIFT_LB, DRIFT_EZ = 200, 0.8
STABLE_LB, STABLE_EZ = 96, 2.0
VB, LEVERAGE = 6, 1.5
ALL_SYMS = sorted({s for p in DRIFT_PAIRS + STABLE_PAIRS for s in p})


# ── Velocity z-score signal ────────────────────────────────────
def gen_velocity(prices, pairs, lb, ez, vb=6, weight_scale=1.0):
    syms = list(prices.columns)
    w = weight_scale / len(pairs)
    n = len(prices)
    pos = np.zeros((n, len(syms)))
    si = {s: i for i, s in enumerate(syms)}
    zs = {}
    for a, b in pairs:
        lr = np.log(prices[a].values / prices[b].values)
        rm = pd.Series(lr).rolling(lb).mean().values
        rs = pd.Series(lr).rolling(lb).std().values
        zs[f"{a}/{b}"] = np.where(rs > 0, (lr - rm) / rs, 0.0)
    state = {k: 0 for k in zs}
    for i in range(lb, n):
        for a, b in pairs:
            k = f"{a}/{b}"
            z = zs[k][i]
            if np.isnan(z):
                z = 0.0
            zprev = zs[k][i - vb] if i >= vb and not np.isnan(zs[k][i - vb]) else z
            dz = z - zprev
            st = state[k]
            if st == 1 and z >= 0:
                st = 0
            elif st == -1 and z <= 0:
                st = 0
            if st == 0:
                if z < -ez and dz > 0:
                    st = 1
                elif z > ez and dz < 0:
                    st = -1
            state[k] = st
            if st == 1:
                pos[i, si[a]] += w
                pos[i, si[b]] -= w
            elif st == -1:
                pos[i, si[a]] -= w
                pos[i, si[b]] += w
    return pd.DataFrame(pos, index=prices.index, columns=syms)


# ── Pair selection helpers ─────────────────────────────────────
def ou_half_life(spread):
    y, x = spread[1:], spread[:-1]
    if len(x) < 20:
        return float("inf")
    xm = x.mean()
    d = np.sum((x - xm) ** 2)
    if d == 0:
        return float("inf")
    b = np.sum((x - xm) * (y - xm)) / d
    if b >= 1 or b <= 0:
        return float("inf")
    return np.log(2) / (-np.log(b))


def engle_granger_p(y, x):
    n = len(y)
    if n < 50:
        return 1.0
    X = np.column_stack([np.ones(n), x])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    r = y - X @ b
    dy, yl = np.diff(r), r[:-1]
    d = np.sum(yl ** 2)
    if d == 0:
        return 1.0
    rho = np.sum(yl * dy) / d
    se = np.sqrt(np.sum((dy - rho * yl) ** 2) / (len(dy) - 1)) / np.sqrt(d)
    if se == 0:
        return 1.0
    t = rho / se
    if t < -3.90: return 0.01
    if t < -3.34: return 0.03
    if t < -3.04: return 0.08
    if t < -2.58: return 0.15
    return 0.50


def score_pair(prices, a, b):
    la, lb_ = np.log(prices[a].values), np.log(prices[b].values)
    hl = ou_half_life(la - lb_)
    cp = engle_granger_p(la, lb_)
    if hl < 12 or math.isinf(hl):
        return -999, hl, cp
    sc = max(0, 1 - abs(hl - 72) / 500) * 3 + (1.0 if cp <= 0.05 else 0.5 if cp <= 0.15 else 0.0) * 2
    return sc, hl, cp


def select_pairs(prices, n_pairs=3):
    """Score all pairs and return top n diversified (no shared symbols)."""
    scores = []
    for a, b in combinations(prices.columns, 2):
        sc, hl, cp = score_pair(prices, a, b)
        scores.append({"a": a, "b": b, "score": sc, "hl": hl, "coint_p": cp})
    scores.sort(key=lambda x: x["score"], reverse=True)
    selected, used = [], set()
    for s in scores:
        if s["score"] <= 0:
            break
        if s["a"] not in used and s["b"] not in used:
            selected.append((s["a"], s["b"]))
            used.update([s["a"], s["b"]])
            print(f"  {s['a']}/{s['b']}: HL={s['hl']:.0f}h, p={s['coint_p']:.3f}, score={s['score']:.2f}")
            if len(selected) >= n_pairs:
                break
    return selected


# ── Main ───────────────────────────────────────────────────────
async def main():
    print("Fetching data...")
    ps, fs = {}, {}
    for sym in ALL_SYMS:
        r = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=sym, lookback_days=200, limit=5000, series=["price", "funding"])
        pdf = r.get("price", pd.DataFrame())
        if pdf.empty:
            continue
        pdf = pdf.sort_index()
        col = "price_usd" if "price_usd" in pdf.columns else pdf.columns[0]
        ps[sym] = pdf[col]
        fdf = r.get("funding", pd.DataFrame())
        if not fdf.empty:
            fdf = fdf.sort_index()
            num = fdf.select_dtypes(include=[np.number]).columns
            if len(num) > 0:
                fs[sym] = fdf[num[0]]
        print(f"  {sym}: {len(ps[sym])} bars")

    # Align timestamps
    shared = ps[ALL_SYMS[0]].index
    for s in ALL_SYMS[1:]:
        shared = shared.intersection(ps[s].index)
    shared = shared.sort_values()
    all_p = pd.DataFrame({s: ps[s].reindex(shared) for s in ALL_SYMS}).dropna()
    all_f = pd.DataFrame({
        s: fs[s][~fs[s].index.duplicated(keep="last")].reindex(all_p.index, method="ffill").fillna(0)
        for s in ALL_SYMS if s in fs})
    print(f"\nAligned: {len(all_p)} bars ({len(all_p)/24:.0f} days)")

    # Generate combined positions
    print("\nGenerating signals...")
    pos_d = gen_velocity(all_p, DRIFT_PAIRS, DRIFT_LB, DRIFT_EZ, VB, weight_scale=0.5)
    pos_s = gen_velocity(all_p, STABLE_PAIRS, STABLE_LB, STABLE_EZ, VB, weight_scale=0.5)
    pos = pos_d + pos_s

    # Backtest
    tf = all_f[[s for s in ALL_SYMS if s in all_f.columns]]
    cfg = BacktestConfig(
        fee_rate=0.00035, slippage_rate=0.0001, leverage=LEVERAGE,
        initial_capital=1.0, funding_rates=tf, enable_liquidation=True,
        periods_per_year=8760, rebalance_threshold=0.02)
    r = run_backtest(all_p, pos, cfg)

    print(f"\nResults:")
    print(f"  Sharpe:  {r.stats['sharpe']:.2f}")
    print(f"  Return:  {r.stats['total_return']:.2%}")
    print(f"  Max DD:  {r.stats['max_drawdown']:.2%}")
    print(f"  Trades:  {r.stats['trade_count']}")
    print(f"  Win Rate: {r.stats.get('win_rate', 0):.0%}")


if __name__ == "__main__":
    asyncio.run(main())
