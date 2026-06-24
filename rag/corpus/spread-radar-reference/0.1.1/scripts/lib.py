"""Spread Radar shared library.

All pipeline agents import from here. Do not re-implement these functions inline.

Functions:
    fetch_universe(symbols, lookback_days) -> (prices_df, funding_df)
    ou_half_life(spread) -> float
    engle_granger_p(y, x) -> float
    score_pair(prices, a, b) -> (score, half_life, coint_p)
    select_pairs(prices, n_pairs) -> list[dict]
    check_pair_stability(prices, a, b, train_frac) -> dict
    gen_velocity(prices, pairs, lb, ez, vb, weight_scale) -> positions_df
    backtest(prices, positions, funding, leverage, fee_rate) -> dict
    run_walk_forward(prices, funding, pairs, lb, ez, ...) -> dict
    sweep_signal(prices, funding, pairs, label) -> list[dict]
    run_full_pipeline(prices, funding, stable_pairs, drift_pairs) -> dict
"""
from __future__ import annotations

import asyncio
import math
from itertools import combinations

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting import run_backtest
from wayfinder_paths.core.backtesting.types import BacktestConfig
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT


# ── Data fetching ──────────────────────────────────────────────

async def fetch_universe(
    symbols: list[str],
    lookback_days: int = 200,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch hourly prices and funding for symbols, return aligned DataFrames."""
    ps, fs = {}, {}
    for sym in symbols:
        for attempt in range(3):
            try:
                r = await DELTA_LAB_CLIENT.get_asset_timeseries(
                    symbol=sym, lookback_days=lookback_days, limit=5000,
                    series=["price", "funding"],
                )
                pdf = r.get("price", pd.DataFrame())
                if pdf.empty:
                    break
                pdf = pdf.sort_index()
                col = "price_usd" if "price_usd" in pdf.columns else pdf.columns[0]
                ps[sym] = pdf[col]
                fdf = r.get("funding", pd.DataFrame())
                if not fdf.empty:
                    fdf = fdf.sort_index()
                    num = fdf.select_dtypes(include=[np.number]).columns
                    if len(num) > 0:
                        fs[sym] = fdf[num[0]]
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)

    available = [s for s in symbols if s in ps and len(ps[s]) >= 500]
    if len(available) < 2:
        raise ValueError(f"Only {len(available)} symbols have enough data")

    shared = ps[available[0]].index
    for s in available[1:]:
        shared = shared.intersection(ps[s].index)
    shared = shared.sort_values()

    prices = pd.DataFrame({s: ps[s].reindex(shared) for s in available}).dropna()
    funding = pd.DataFrame({
        s: fs[s][~fs[s].index.duplicated(keep="last")]
        .reindex(prices.index, method="ffill").fillna(0)
        for s in available if s in fs
    })
    return prices, funding


# ── Pair selection ─────────────────────────────────────────────

def ou_half_life(spread: np.ndarray) -> float:
    """Ornstein-Uhlenbeck half-life in bars. Returns inf if not mean-reverting."""
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


def engle_granger_p(y: np.ndarray, x: np.ndarray) -> float:
    """Engle-Granger cointegration test. Returns approximate p-value."""
    n = len(y)
    if n < 50:
        return 1.0
    X = np.column_stack([np.ones(n), x])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    resid = y - X @ b
    dy, yl = np.diff(resid), resid[:-1]
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


def score_pair(prices: pd.DataFrame, a: str, b: str) -> tuple[float, float, float]:
    """Score a pair by half-life + cointegration. Returns (score, half_life, coint_p)."""
    la = np.log(prices[a].values)
    lb_ = np.log(prices[b].values)
    hl = ou_half_life(la - lb_)
    cp = engle_granger_p(la, lb_)
    if hl < 12 or math.isinf(hl):
        return -999, hl, cp
    hl_score = max(0, 1 - abs(hl - 72) / 500) * 3
    coint_score = (1.0 if cp <= 0.05 else 0.5 if cp <= 0.15 else 0.0) * 2
    return hl_score + coint_score, hl, cp


def select_pairs(
    prices: pd.DataFrame,
    n_pairs: int = 3,
    diversified: bool = True,
) -> list[dict]:
    """Score all pairs, return top n. Each dict has: a, b, score, hl, coint_p.

    If diversified=True, no symbol appears in more than one pair.
    """
    scores = []
    for a, b in combinations(prices.columns, 2):
        sc, hl, cp = score_pair(prices, a, b)
        scores.append({"a": a, "b": b, "score": sc, "hl": hl, "coint_p": cp})
    scores.sort(key=lambda x: x["score"], reverse=True)

    if not diversified:
        return [s for s in scores if s["score"] > 0][:n_pairs]

    selected, used = [], set()
    for s in scores:
        if s["score"] <= 0:
            break
        if s["a"] not in used and s["b"] not in used:
            selected.append(s)
            used.update([s["a"], s["b"]])
            if len(selected) >= n_pairs:
                break
    return selected


def check_pair_stability(
    prices: pd.DataFrame,
    a: str,
    b: str,
    train_frac: float = 0.6,
) -> dict:
    """Check if a pair is cointegrated in both train and test halves."""
    split = int(len(prices) * train_frac)
    train_p, test_p = prices.iloc[:split], prices.iloc[split:]

    _, hl_tr, cp_tr = score_pair(train_p, a, b)
    _, hl_te, cp_te = score_pair(test_p, a, b)

    spread = np.log(prices[a].values / prices[b].values)
    spread_te = spread[split:]
    drift = abs(spread_te[-1] - spread_te[0])

    stable = (hl_tr < 300 and cp_tr <= 0.15 and hl_te < 300 and cp_te <= 0.15)
    return {
        "a": a, "b": b, "stable": stable, "drift": drift,
        "hl_train": hl_tr, "coint_p_train": cp_tr,
        "hl_test": hl_te, "coint_p_test": cp_te,
    }


# ── Signal generation ─────────────────────────────────────────

def pair_zscores(
    prices: pd.DataFrame,
    pairs: list[tuple[str, str]],
    lb: int,
) -> dict[str, np.ndarray]:
    """Rolling z-score of log(price_A / price_B) per pair. Depends only on lb.

    Factored out so a parameter sweep can compute it once per lookback instead
    of once per (lb, ez, leverage) config.
    """
    zs = {}
    for a, b in pairs:
        lr = np.log(prices[a].values / prices[b].values)
        rm = pd.Series(lr).rolling(lb).mean().values
        rs = pd.Series(lr).rolling(lb).std().values
        zs[f"{a}/{b}"] = np.where(rs > 0, (lr - rm) / rs, 0.0)
    return zs


def gen_velocity(
    prices: pd.DataFrame,
    pairs: list[tuple[str, str]],
    lb: int,
    ez: float,
    vb: int = 6,
    weight_scale: float = 1.0,
    zs: dict[str, np.ndarray] | None = None,
) -> pd.DataFrame:
    """Velocity-filtered z-score signal.

    For each pair, compute rolling z-score of log(price_A / price_B).
    Enter only when z is extreme AND moving toward zero (velocity confirmation).

    Args:
        prices: DataFrame with columns per symbol, hourly bars.
        pairs: List of (symbol_A, symbol_B) tuples.
        lb: Lookback window in bars for rolling mean/std.
        ez: Entry z-score threshold (enter when |z| > ez).
        vb: Velocity bars — compute dz = z[now] - z[now - vb].
            Enter long A/short B only if z < -ez AND dz > 0 (reverting up).
            Enter short A/long B only if z > ez AND dz < 0 (reverting down).
        weight_scale: Scale factor for position weights (use 0.5 for 50/50 split).

    Returns:
        DataFrame of target weights in [-weight_scale, weight_scale] per symbol.
        Exit when z crosses zero.
    """
    syms = list(prices.columns)
    w = weight_scale / len(pairs)
    n = len(prices)
    pos = np.zeros((n, len(syms)))
    si = {s: i for i, s in enumerate(syms)}
    if zs is None:
        zs = pair_zscores(prices, pairs, lb)
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
            # Exit when z crosses zero
            if st == 1 and z >= 0:
                st = 0
            elif st == -1 and z <= 0:
                st = 0
            # Enter only with velocity confirmation
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


# ── Backtesting ────────────────────────────────────────────────

def backtest(
    prices: pd.DataFrame,
    positions: pd.DataFrame,
    funding: pd.DataFrame,
    leverage: float = 1.5,
    fee_rate: float = 0.00035,
    track_positions: bool = True,
) -> dict:
    """Run backtest, return stats dict + equity curve.

    track_positions=False skips per-bar position snapshots — set it when the
    caller won't read positions_over_time (e.g. a sweep without decomposition).
    """
    syms = list(positions.columns)
    tf = funding[[s for s in syms if s in funding.columns]]
    cfg = BacktestConfig(
        fee_rate=fee_rate, slippage_rate=0.0001, leverage=leverage,
        initial_capital=1.0, funding_rates=tf, enable_liquidation=True,
        periods_per_year=8760, rebalance_threshold=0.02,
        track_positions=track_positions, validate_positions=False,
    )
    r = run_backtest(prices[syms], positions, cfg)
    return {
        "sharpe": r.stats["sharpe"],
        "total_return": r.stats["total_return"],
        "max_drawdown": r.stats["max_drawdown"],
        "trade_count": r.stats.get("trade_count", 0),
        "win_rate": r.stats.get("win_rate", 0),
        "sortino": r.stats.get("sortino", 0),
        "liquidated": r.liquidated,
        "equity_curve": r.equity_curve,
        "positions_over_time": r.positions_over_time,
    }


def run_walk_forward(
    prices: pd.DataFrame,
    funding: pd.DataFrame,
    pairs: list[tuple[str, str]],
    lb: int,
    ez: float,
    vb: int = 6,
    leverage: float = 1.5,
    weight_scale: float = 1.0,
    train_frac: float = 0.6,
    fee_rate: float = 0.00035,
    decompose: bool = False,
    positions: pd.DataFrame | None = None,
) -> dict:
    """Run signal on full data, measure train and OOS metrics separately.

    This is the honest evaluation method: the signal runs continuously
    (no restart at split), and metrics are measured on each half independently.

    Pass `positions` to reuse a precomputed signal (it depends only on lb/ez/vb/
    weight_scale, not leverage — so a sweep can share it across leverage levels).
    Set decompose=True only for final results — it's expensive.
    """
    pair_syms = sorted({s for p in pairs for s in p})
    pos = (
        positions
        if positions is not None
        else gen_velocity(prices[pair_syms], pairs, lb, ez, vb, weight_scale)
    )
    r = backtest(prices, pos, funding, leverage, fee_rate, track_positions=decompose)

    ec = r["equity_curve"]
    split_idx = int(len(ec) * train_frac)

    def metrics(equity_slice):
        ret = equity_slice.iloc[-1] / equity_slice.iloc[0] - 1
        rets = equity_slice.pct_change().dropna()
        sharpe = float(rets.mean() / rets.std() * np.sqrt(8760)) if rets.std() > 0 else 0
        dd = float((equity_slice / equity_slice.cummax() - 1).min())
        return {"sharpe": sharpe, "return": float(ret), "max_dd": dd}

    train_m = metrics(ec.iloc[:split_idx])
    oos_m = metrics(ec.iloc[split_idx:])

    result = {
        "train": train_m,
        "oos": oos_m,
        "full": {"sharpe": r["sharpe"], "return": r["total_return"], "max_dd": r["max_drawdown"]},
        "trade_count": r["trade_count"],
        "liquidated": r["liquidated"],
        "equity_curve": ec,
        "split_idx": split_idx,
    }

    # P&L decomposition — vectorized, only when requested
    if decompose:
        pot = r["positions_over_time"]
        price_changes = prices[pair_syms].diff()
        pnl_by_pair = {}
        for a, b in pairs:
            pnl = 0.0
            for s in [a, b]:
                if s in pot.columns:
                    # Vectorized: sum(position[t-1] * price_change[t]) for OOS period
                    held = pot[s].iloc[split_idx:-1].values
                    dp = price_changes[s].iloc[split_idx + 1:].values
                    pnl += float(np.nansum(held * dp))
            base = float(ec.iloc[split_idx])
            pnl_by_pair[f"{a}/{b}"] = pnl / base if base > 0 else 0.0
        result["pnl_by_pair"] = pnl_by_pair

    return result


# ── Pre-built sweep (agents MUST use these, not custom grids) ──

_COARSE_GRID = {
    "lookbacks": [96, 144, 200],
    "entry_zs": [0.8, 1.5, 2.0],
    "leverages": [1.0, 1.5, 2.0],
}


def sweep_signal(
    prices: pd.DataFrame,
    funding: pd.DataFrame,
    pairs: list[tuple[str, str]],
    label: str = "basket",
) -> list[dict]:
    """Fixed-grid parameter sweep for one basket. Returns sorted by OOS Sharpe.

    This is the ONLY function agents should call for signal research.
    The grid is intentionally small (27 configs) to finish in ~30s.
    """
    results = []
    grid = _COARSE_GRID
    total = len(grid["lookbacks"]) * len(grid["entry_zs"]) * len(grid["leverages"])
    best_sharpe = -999
    tested = 0
    pair_syms = sorted({s for p in pairs for s in p})
    sub_prices = prices[pair_syms]
    for lb in grid["lookbacks"]:
        zs = pair_zscores(sub_prices, pairs, lb)  # once per lookback (not per ez/lev)
        for ez in grid["entry_zs"]:
            # Signal is leverage-independent — generate once, reuse across leverages.
            pos = gen_velocity(sub_prices, pairs, lb, ez, zs=zs)
            for lev in grid["leverages"]:
                r = run_walk_forward(prices, funding, pairs, lb, ez,
                                     leverage=lev, decompose=False, positions=pos)
                tested += 1
                if not r["liquidated"]:
                    entry = {"lb": lb, "ez": ez, "lev": lev, **r}
                    results.append(entry)
                    if r["oos"]["sharpe"] > best_sharpe:
                        best_sharpe = r["oos"]["sharpe"]
                        print(f"  {label} [{tested}/{total}] lb={lb} ez={ez} lev={lev} "
                              f"→ OOS Sharpe={r['oos']['sharpe']:.2f}, "
                              f"Ret={r['oos']['return']:.2%}")
    results.sort(key=lambda x: x["oos"]["sharpe"], reverse=True)
    return results


def run_full_pipeline(
    prices: pd.DataFrame,
    funding: pd.DataFrame,
    stable_pairs: list[tuple[str, str]],
    drift_pairs: list[tuple[str, str]],
) -> dict:
    """Run the complete signal research pipeline. Returns structured results.

    1. Sweep stable basket (27 configs, ~30s)
    2. Sweep drift basket (27 configs, ~30s)
    3. Test combined at 3 leverage levels (~10s)
    4. Decompose top results

    Total: ~70s.
    """
    print("Stage 1: Stable basket sweep...")
    stable_results = sweep_signal(prices, funding, stable_pairs, "stable")

    print("\nStage 2: Drift basket sweep...")
    drift_results = sweep_signal(prices, funding, drift_pairs, "drift")

    # Combined: use best lb/ez per basket, sweep leverage
    print("\nStage 3: Combined sweep...")
    combined_results = []
    best_s = stable_results[0] if stable_results else None
    best_d = drift_results[0] if drift_results else None

    if best_s and best_d:
        for lev in _COARSE_GRID["leverages"]:
            pair_syms = sorted({s for p in stable_pairs + drift_pairs for s in p})
            pos_s = gen_velocity(prices[pair_syms], stable_pairs,
                                best_s["lb"], best_s["ez"], weight_scale=0.5)
            pos_d = gen_velocity(prices[pair_syms], drift_pairs,
                                best_d["lb"], best_d["ez"], weight_scale=0.5)
            pos = pos_s + pos_d
            r = backtest(prices, pos, funding, leverage=lev)
            ec = r["equity_curve"]
            split_idx = int(len(ec) * 0.6)

            def _m(sl):
                ret = sl.iloc[-1] / sl.iloc[0] - 1
                rets = sl.pct_change().dropna()
                shp = float(rets.mean() / rets.std() * np.sqrt(8760)) if rets.std() > 0 else 0
                dd = float((sl / sl.cummax() - 1).min())
                return {"sharpe": shp, "return": float(ret), "max_dd": dd}

            entry = {
                "lev": lev,
                "stable_lb": best_s["lb"], "stable_ez": best_s["ez"],
                "drift_lb": best_d["lb"], "drift_ez": best_d["ez"],
                "oos": _m(ec.iloc[split_idx:]),
                "train": _m(ec.iloc[:split_idx]),
                "full": {"sharpe": r["sharpe"], "return": r["total_return"],
                         "max_dd": r["max_drawdown"]},
                "trade_count": r["trade_count"],
                "liquidated": r["liquidated"],
                "equity_curve": ec,
                "split_idx": split_idx,
            }
            combined_results.append(entry)
            print(f"  combined lev={lev} → OOS Sharpe={entry['oos']['sharpe']:.2f}, "
                  f"Ret={entry['oos']['return']:.2%}")

    combined_results.sort(key=lambda x: x["oos"]["sharpe"], reverse=True)

    # Pick overall best
    all_candidates = []
    for r in stable_results[:3]:
        all_candidates.append(("stable", r))
    for r in drift_results[:3]:
        all_candidates.append(("drift", r))
    for r in combined_results[:3]:
        all_candidates.append(("combined", r))
    all_candidates.sort(key=lambda x: x[1]["oos"]["sharpe"], reverse=True)
    best_type, best_config = all_candidates[0] if all_candidates else ("none", {})

    # Decompose top result
    if best_type == "combined" and best_s and best_d:
        r_final = run_walk_forward(
            prices, funding, stable_pairs,
            best_s["lb"], best_s["ez"], leverage=best_config["lev"],
            weight_scale=0.5, decompose=True,
        )
        r_final2 = run_walk_forward(
            prices, funding, drift_pairs,
            best_d["lb"], best_d["ez"], leverage=best_config["lev"],
            weight_scale=0.5, decompose=True,
        )
        best_config["pnl_by_pair"] = {
            **(r_final.get("pnl_by_pair", {})),
            **(r_final2.get("pnl_by_pair", {})),
        }
    elif best_type in ("stable", "drift"):
        pairs = stable_pairs if best_type == "stable" else drift_pairs
        r_final = run_walk_forward(
            prices, funding, pairs,
            best_config["lb"], best_config["ez"],
            leverage=best_config["lev"], decompose=True,
        )
        best_config["pnl_by_pair"] = r_final.get("pnl_by_pair", {})

    # Robustness stats
    all_oos = [r["oos"]["sharpe"] for r in stable_results + drift_results + combined_results]
    profitable = sum(1 for s in all_oos if s > 0)
    gt3 = sum(1 for s in all_oos if s > 3)

    print(f"\nDone. Best: {best_type} OOS Sharpe={best_config.get('oos', {}).get('sharpe', 0):.2f}")
    print(f"Robustness: {profitable}/{len(all_oos)} profitable, "
          f"{gt3}/{len(all_oos)} Sharpe>3, "
          f"median={np.median(all_oos):.2f}")

    return {
        "stable_results": [{k: v for k, v in r.items() if k != "equity_curve"}
                          for r in stable_results[:5]],
        "drift_results": [{k: v for k, v in r.items() if k != "equity_curve"}
                         for r in drift_results[:5]],
        "combined_results": [{k: v for k, v in r.items() if k != "equity_curve"}
                            for r in combined_results],
        "best_type": best_type,
        "best_config": {k: v for k, v in best_config.items() if k != "equity_curve"},
        "robustness": {
            "total_configs": len(all_oos),
            "profitable_pct": profitable / len(all_oos) if all_oos else 0,
            "sharpe_gt3_pct": gt3 / len(all_oos) if all_oos else 0,
            "median_oos_sharpe": float(np.median(all_oos)) if all_oos else 0,
        },
    }
