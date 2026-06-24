"""Build applet data snapshot from a hedge-finder run's artifacts.

Usage:
    python scripts/build_applet_data.py <run_dir> [--out applet/dist/data/hedge_snapshot.json]

Reads: exposure_reader.json, quant_results.json, critic_verdict.json, job.json
Fetches: hourly price + funding timeseries from Delta Lab
Writes: a single JSON snapshot for the applet to render.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure lib is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import fetch_price_series, fetch_timeseries, safe_float, series_column, write_artifact


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


async def build_snapshot(run_dir: Path, lookback_days: int = 30) -> dict:
    exposure = load_json(run_dir / "exposure_reader.json")
    quant = load_json(run_dir / "quant_results.json")
    critic = load_json(run_dir / "critic_verdict.json")
    job = load_json(run_dir / "job.json")

    selected = critic.get("selected_hedge") or quant["top_combos"][0]
    legs = selected["legs"]
    portfolio_assets = exposure["assets"]

    # Fetch price series for portfolio assets
    port_prices = {}
    for a in portfolio_assets:
        sym = a["resolved_symbol"]
        try:
            s = await fetch_price_series(sym, lookback_days)
            if s is not None and len(s) > 10:
                port_prices[sym] = s[~s.index.duplicated(keep="last")]
        except Exception:
            pass

    # Fetch price + funding series for hedge legs
    hedge_prices = {}
    hedge_funding = {}
    for leg in legs:
        sym = leg["symbol"]
        try:
            payload = await fetch_timeseries(sym, lookback_days, venue="hyperliquid")
            pf = payload.get("price")
            if isinstance(pf, pd.DataFrame) and not pf.empty:
                ps = series_column(pf, "price_usd", "close", "price", "mark_price_usd").astype(float)
                hedge_prices[sym] = ps[~ps.index.duplicated(keep="last")]
            ff = payload.get("funding")
            if isinstance(ff, pd.DataFrame) and not ff.empty:
                fs = series_column(ff, "funding_rate", "funding_now", "funding").astype(float)
                hedge_funding[sym] = fs[~fs.index.duplicated(keep="last")]
        except Exception:
            pass

    # Build common date index
    all_series = list(port_prices.values()) + list(hedge_prices.values())
    if not all_series:
        raise RuntimeError("No price data fetched")
    common_idx = all_series[0].index
    for s in all_series[1:]:
        common_idx = common_idx.intersection(s.index)
    common_idx = common_idx.sort_values()

    # Portfolio returns (weighted by notional)
    gross_notional = sum(a["notional_usd"] for a in portfolio_assets)
    port_rets = pd.Series(0.0, index=common_idx)
    for a in portfolio_assets:
        sym = a["resolved_symbol"]
        if sym in port_prices:
            pr = port_prices[sym].reindex(common_idx).ffill().pct_change().fillna(0)
            weight = a["notional_usd"] / gross_notional
            sign = 1.0 if a["side"] == "long" else -1.0
            port_rets += pr * weight * sign

    # Hedge returns (weighted by hedge ratios)
    hedge_notional = sum(leg["notional_usd"] for leg in legs)
    hedge_rets = pd.Series(0.0, index=common_idx)
    for leg in legs:
        sym = leg["symbol"]
        if sym in hedge_prices:
            pr = hedge_prices[sym].reindex(common_idx).ffill().pct_change().fillna(0)
            weight = leg["notional_usd"] / hedge_notional
            sign = -1.0 if leg["side"] == "short" else 1.0
            hedge_rets += pr * weight * sign

    # Hedged portfolio = portfolio + hedge (scaled by notional ratio)
    hedge_scale = hedge_notional / gross_notional
    hedged_rets = port_rets + hedge_rets * hedge_scale

    # Cumulative returns
    port_cum = (1 + port_rets).cumprod() - 1
    hedged_cum = (1 + hedged_rets).cumprod() - 1

    # Funding income per leg (hourly)
    funding_income = {}
    for leg in legs:
        sym = leg["symbol"]
        if sym in hedge_funding:
            fs = hedge_funding[sym].reindex(common_idx).ffill().fillna(0)
            # funding_rate per hour * notional / 8760 doesn't work because
            # funding is already per-hour rate. Income = rate * notional_usd
            # Sign: short receives positive funding (longs pay shorts)
            sign = 1.0 if leg["side"] == "short" else -1.0
            hourly_income = fs * leg["notional_usd"] * sign
            funding_income[sym] = hourly_income

    # Aggregate daily funding income
    daily_dates = []
    daily_income_per_leg = {sym: [] for sym in funding_income}
    daily_total = []
    if funding_income:
        all_funding = pd.DataFrame(funding_income)
        all_funding.index = pd.to_datetime(all_funding.index)
        daily = all_funding.resample("D").sum()
        daily_dates = [d.strftime("%Y-%m-%d") for d in daily.index]
        for sym in funding_income:
            daily_income_per_leg[sym] = [safe_float(v) for v in daily[sym].tolist()]
        daily_total = [safe_float(v) for v in daily.sum(axis=1).tolist()]

    # Cumulative funding income
    cum_funding = np.cumsum(daily_total).tolist() if daily_total else []

    # Rolling correlation (72h window) between portfolio and hedge returns
    window = min(72, len(common_idx) - 1)
    if window > 5:
        roll_corr = port_rets.rolling(window).corr(hedge_rets * -1).fillna(0)
    else:
        roll_corr = pd.Series(0.0, index=common_idx)

    # Rolling net beta (72h) — portfolio regressed on BTC
    btc_prices = port_prices.get("BTC") or hedge_prices.get("BTC")
    if btc_prices is not None and len(btc_prices) > 10:
        btc_rets = btc_prices.reindex(common_idx).ffill().pct_change().fillna(0)
        if window > 5:
            roll_beta_vals = []
            for i in range(len(common_idx)):
                if i < window:
                    roll_beta_vals.append(0.0)
                else:
                    chunk_port = hedged_rets.iloc[i - window : i]
                    chunk_btc = btc_rets.iloc[i - window : i]
                    cov = chunk_port.cov(chunk_btc)
                    var = chunk_btc.var()
                    roll_beta_vals.append(safe_float(cov / var if var > 0 else 0))
            roll_beta = pd.Series(roll_beta_vals, index=common_idx)
        else:
            roll_beta = pd.Series(0.0, index=common_idx)
    else:
        roll_beta = pd.Series(0.0, index=common_idx)

    # Subsample to ~500 points for chart rendering
    max_chart_points = 500
    step = max(1, len(common_idx) // max_chart_points)
    chart_idx = common_idx[::step]

    dates_iso = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in chart_idx]

    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_dir.name,
        "lookback_days": lookback_days,
        "verdict": critic.get("verdict", "unknown"),
        "portfolio": {
            "assets": portfolio_assets,
            "gross_notional_usd": gross_notional,
            "ann_vol": safe_float(exposure.get("portfolio_ann_vol")),
            "max_dd": safe_float(exposure.get("portfolio_max_dd")),
            "sharpe": safe_float(exposure.get("portfolio_sharpe")),
            "factor_betas": exposure.get("factor_betas", {}),
        },
        "hedge": {
            "hedge_id": selected.get("hedge_id", ""),
            "legs": [
                {
                    "symbol": l["symbol"],
                    "side": l["side"],
                    "notional_usd": l["notional_usd"],
                    "correlation": safe_float(l.get("correlation")),
                    "ann_funding_cost_pct": safe_float(l.get("ann_funding_cost_pct")),
                    "half_life": safe_float(l.get("half_life")),
                    "safe_leverage": l.get("safe_leverage", selected.get("safe_leverage", {}).get(l["symbol"], 1)),
                    "hedge_ratio": safe_float(l.get("hedge_ratio")),
                }
                for l in legs
            ],
            "total_notional_usd": safe_float(selected.get("total_notional_usd")),
            "variance_reduction_pct": safe_float(selected.get("variance_reduction_pct")),
            "hedged_ann_vol": safe_float(selected.get("hedged_ann_vol")),
            "hedged_max_dd": safe_float(selected.get("hedged_max_dd")),
            "net_beta": safe_float(selected.get("net_beta")),
            "blowout_score": safe_float(selected.get("blowout_score")),
            "ann_funding_cost_pct": safe_float(selected.get("ann_funding_cost_pct")),
            "cost_adjusted_improvement": safe_float(selected.get("cost_adjusted_improvement")),
        },
        "risk_checks": critic.get("selected_hedge", {}).get("risk_checks", {}),
        "charts": {
            "dates": dates_iso,
            "portfolio_cum_return": [safe_float(port_cum.reindex(chart_idx).iloc[i]) for i in range(len(chart_idx))],
            "hedged_cum_return": [safe_float(hedged_cum.reindex(chart_idx).iloc[i]) for i in range(len(chart_idx))],
            "rolling_correlation": [safe_float(roll_corr.reindex(chart_idx).iloc[i]) for i in range(len(chart_idx))],
            "rolling_beta": [safe_float(roll_beta.reindex(chart_idx).iloc[i]) for i in range(len(chart_idx))],
        },
        "funding": {
            "daily_dates": daily_dates,
            "daily_income_per_leg": daily_income_per_leg,
            "daily_total": daily_total,
            "cumulative": cum_funding,
            "total_income_usd": safe_float(sum(daily_total)) if daily_total else 0,
            "ann_income_usd": safe_float(sum(daily_total) * 365 / max(len(daily_total), 1)) if daily_total else 0,
        },
        "monitoring": job.get("monitoring", {}),
        "invalidation": job.get("invalidation", []),
    }
    return snapshot


if __name__ == "__main__":
    import asyncio

    from wayfinder_paths.core.config import load_config

    load_config("config.json")

    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".wf-artifacts/hedge-finder-live-002")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".claude/skills/hedge-finder/path/applet/dist/data/hedge_snapshot.json")

    snapshot = asyncio.run(build_snapshot(run_dir))
    write_artifact(out_path, snapshot)
    print(f"Snapshot written to {out_path} ({len(json.dumps(snapshot)) / 1024:.1f} KB)")
