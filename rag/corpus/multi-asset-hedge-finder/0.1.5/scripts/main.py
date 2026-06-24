"""Hedge Finder — policy-backed portfolio hedge compiler.

This entrypoint reads the path inputs/policy, fetches Delta Lab data, writes
phase artifacts under `.wf-artifacts/<run_id>/`, and prints the final path
output contract as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from wayfinder_paths.core.clients import DELTA_LAB_CLIENT

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compute_leverage import CHECK_FREQUENCY_SURVIVAL_HOURS, compute_safe_leverage

MIN_REQUIRED_HOURS = 168
FACTOR_SYMBOLS = ("BTC", "ETH")


@dataclass(frozen=True)
class Holding:
    symbol: str
    resolved_symbol: str
    notional_usd: float
    side: str
    chain: str | None = None
    venue: str | None = None
    token_address: str | None = None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    match_type: str
    preferred_side: str
    correlation: float
    hedge_score: float
    funding_now: float
    funding_mean_7d: float
    funding_mean_30d: float
    funding_std_30d: float
    ann_funding_rate: float
    ann_funding_cost_pct: float
    funding_regime: str
    oi_usd: float
    volume_usd: float
    spread_bps: float
    mark_price_usd: float
    hours: int
    returns: pd.Series
    # Cointegration metrics
    cointegrated: bool = False
    adf_stat: float = 0.0
    coint_hedge_ratio: float = 0.0
    half_life: float = float("inf")
    prices: pd.Series | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime | pd.Timestamp | None = None) -> str:
    current = _utc_now() if value is None else pd.Timestamp(value).to_pydatetime()
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonify(inner) for key, inner in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime | pd.Timestamp):
        return _utc_iso(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, pd.Series):
        return [_jsonify(item) for item in value.tolist()]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonify(payload), indent=2) + "\n", encoding="utf-8")


def _artifact_writer(root: Path, run_id: str) -> tuple[Path, Any]:
    artifact_root = root / ".wf-artifacts" / run_id
    artifact_root.mkdir(parents=True, exist_ok=True)

    def writer(name: str, payload: dict[str, Any]) -> None:
        _write_json(artifact_root / name, payload)

    return artifact_root, writer


def _series_column(frame: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in frame.columns:
            return frame[name]
    raise KeyError(f"Missing expected series column. Tried: {', '.join(names)}")


def _latest_value(frame: pd.DataFrame, *names: str, default: float = 0.0) -> float:
    for name in names:
        if name in frame.columns and not frame[name].dropna().empty:
            return _safe_float(frame[name].dropna().iloc[-1], default)
    return default


def _row_value(row: dict[str, Any], *names: str, default: float = 0.0) -> float:
    for name in names:
        if name in row and row[name] is not None:
            return _safe_float(row[name], default)
    return default


def _log_returns(prices: pd.Series) -> pd.Series:
    clean = prices.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    return np.log(clean / clean.shift(1)).dropna()


def _adf_statistic(series: pd.Series) -> float:
    """Augmented Dickey-Fuller test statistic (no scipy dependency).

    Runs ADF(1): ΔY_t = α + β*Y_{t-1} + γ*ΔY_{t-1} + ε_t
    Returns the t-statistic for β. More negative = more stationary.
    Critical values (5%): ~-2.86 for n=250, ~-2.87 for n=500.
    """
    y = series.dropna().to_numpy(dtype=float)
    if len(y) < 24:
        return 0.0
    dy = np.diff(y)
    y_lag = y[:-1]
    dy_lag = np.concatenate([[0.0], dy[:-1]])
    # ΔY_t = α + β*Y_{t-1} + γ*ΔY_{t-1}
    n = len(dy)
    x = np.column_stack([np.ones(n), y_lag, dy_lag])
    dy_vec = dy
    try:
        coeffs, residuals, _, _ = np.linalg.lstsq(x, dy_vec, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    beta = coeffs[1]
    fitted = x @ coeffs
    resid = dy_vec - fitted
    s2 = float((resid ** 2).sum()) / max(n - 3, 1)
    xtx_inv = np.linalg.pinv(x.T @ x)
    se_beta = math.sqrt(max(s2 * xtx_inv[1, 1], 1e-30))
    return float(beta / se_beta)


def _engle_granger(
    y: pd.Series,
    x: pd.Series,
) -> dict[str, float]:
    """Engle-Granger cointegration test.

    Regress y on x, then test residuals for stationarity via ADF.
    Returns hedge_ratio (regression coefficient), adf_stat, and p-value approximation.
    """
    aligned = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return {"hedge_ratio": 0.0, "adf_stat": 0.0, "cointegrated": False, "half_life": float("inf")}
    y_arr = aligned["y"].to_numpy(dtype=float)
    x_arr = aligned["x"].to_numpy(dtype=float)
    x_mat = np.column_stack([np.ones(len(x_arr)), x_arr])
    try:
        coeffs, *_ = np.linalg.lstsq(x_mat, y_arr, rcond=None)
    except np.linalg.LinAlgError:
        return {"hedge_ratio": 0.0, "adf_stat": 0.0, "cointegrated": False, "half_life": float("inf")}
    hedge_ratio = float(coeffs[1])
    residuals = pd.Series(y_arr - x_mat @ coeffs, index=aligned.index)
    adf_stat = _adf_statistic(residuals)
    # Approximate: ADF stat < -2.87 at 5% significance → cointegrated
    cointegrated = adf_stat < -2.87
    half_life = _half_life(residuals)
    return {
        "hedge_ratio": round(hedge_ratio, 6),
        "adf_stat": round(adf_stat, 4),
        "cointegrated": cointegrated,
        "half_life": round(half_life, 1),
    }


def _half_life(spread: pd.Series) -> float:
    """Half-life of mean reversion via AR(1) on the spread.

    ΔS_t = λ * S_{t-1} + ε → half-life = -ln(2) / ln(1 + λ)
    """
    s = spread.dropna().to_numpy(dtype=float)
    if len(s) < 24:
        return float("inf")
    ds = np.diff(s)
    s_lag = s[:-1]
    x = np.column_stack([np.ones(len(s_lag)), s_lag])
    try:
        coeffs, *_ = np.linalg.lstsq(x, ds, rcond=None)
    except np.linalg.LinAlgError:
        return float("inf")
    lam = float(coeffs[1])
    if lam >= 0:
        return float("inf")  # not mean-reverting
    try:
        return -math.log(2) / math.log(1 + lam)
    except (ValueError, ZeroDivisionError):
        return float("inf")


def _annualized_vol(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return _safe_float(returns.std() * math.sqrt(24 * 365))


def _max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    cumulative = np.exp(returns.cumsum())
    running_max = cumulative.cummax()
    drawdown = cumulative / running_max - 1
    return _safe_float(drawdown.min())


def _sharpe_ratio(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    vol = returns.std()
    if vol == 0 or pd.isna(vol):
        return 0.0
    return _safe_float((returns.mean() / vol) * math.sqrt(24 * 365))


def _screen_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "results"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _candidate_symbol(row: dict[str, Any]) -> str:
    for key in ("basis_symbol", "symbol", "base_symbol", "asset_symbol"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _candidate_to_dict(c: Candidate) -> dict[str, Any]:
    """Serialize a Candidate to dict, excluding heavy series fields."""
    d = asdict(c)
    d.pop("returns", None)
    d.pop("prices", None)
    return d


def _funding_regime(mean_rate: float) -> str:
    if abs(mean_rate) < 0.0001:
        return "neutral"
    return "shorts pay" if mean_rate > 0 else "longs pay"


def _side_from_weight(weight: float) -> str:
    return "short" if weight < 0 else "long"


def _side_funding_cost_pct(ann_funding_rate: float, side: str) -> float:
    return ann_funding_rate if side == "long" else -ann_funding_rate


def _beta(target: pd.Series, factor: pd.Series) -> float:
    aligned = pd.concat([target, factor], axis=1).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return 0.0
    factor_var = _safe_float(aligned.iloc[:, 1].var())
    if factor_var == 0:
        return 0.0
    return _safe_float(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / factor_var)


def _rolling_beta(target: pd.Series, factor: pd.Series, window: int = MIN_REQUIRED_HOURS) -> dict[str, float]:
    aligned = pd.concat([target, factor], axis=1).dropna()
    if len(aligned) < max(window, 24):
        beta_value = _beta(target, factor)
        return {
            "beta_current": beta_value,
            "beta_mean": beta_value,
            "beta_std": 0.0,
        }
    target_series = aligned.iloc[:, 0]
    factor_series = aligned.iloc[:, 1]
    cov = target_series.rolling(window).cov(factor_series)
    var = factor_series.rolling(window).var()
    rolling = (cov / var).replace([np.inf, -np.inf], np.nan).dropna()
    if rolling.empty:
        beta_value = _beta(target, factor)
        return {
            "beta_current": beta_value,
            "beta_mean": beta_value,
            "beta_std": 0.0,
        }
    return {
        "beta_current": _safe_float(rolling.iloc[-1]),
        "beta_mean": _safe_float(rolling.mean()),
        "beta_std": _safe_float(rolling.std()),
    }


def _model_r2(target: pd.Series, factors: dict[str, pd.Series]) -> float:
    if not factors:
        return 0.0
    aligned = pd.concat([target, *factors.values()], axis=1).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return 0.0
    y = aligned.iloc[:, 0].to_numpy(dtype=float)
    x = aligned.iloc[:, 1:].to_numpy(dtype=float)
    x = np.column_stack([np.ones(len(x)), x])
    coeffs, *_ = np.linalg.lstsq(x, y, rcond=None)
    fitted = x @ coeffs
    ss_res = float(((y - fitted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot == 0:
        return 0.0
    return _clamp(1 - ss_res / ss_tot, 0.0, 1.0)


async def _resolve_symbol(symbol: str) -> str:
    try:
        payload = await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol)
    except Exception:
        return symbol
    basis = payload.get("basis") if isinstance(payload, dict) else None
    if isinstance(basis, dict):
        root_symbol = str(basis.get("root_symbol") or "").strip()
        if root_symbol:
            return root_symbol
    resolved = str(payload.get("symbol") or "").strip() if isinstance(payload, dict) else ""
    return resolved or symbol


async def _fetch_price_series(symbol: str, lookback_days: int) -> pd.Series:
    payload = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol=symbol,
        series=["price"],
        lookback_days=lookback_days,
    )
    frame = payload.get("price")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"No price series returned for {symbol}")
    prices = _series_column(frame, "price_usd", "close", "price", "mark_price_usd").astype(float)
    return prices.rename(symbol)


async def _fetch_factor_returns(lookback_days: int) -> dict[str, pd.Series]:
    factor_returns: dict[str, pd.Series] = {}
    for symbol in FACTOR_SYMBOLS:
        try:
            prices = await _fetch_price_series(symbol, lookback_days)
        except Exception:
            continue
        factor_returns[symbol] = _log_returns(prices).rename(symbol)
    return factor_returns


async def _build_holdings(
    raw_holdings: list[dict[str, Any]],
) -> tuple[list[Holding], list[str]]:
    holdings: list[Holding] = []
    unresolved: list[str] = []
    for item in raw_holdings:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        resolved = await _resolve_symbol(symbol)
        if not resolved:
            unresolved.append(symbol)
            continue
        holdings.append(
            Holding(
                symbol=symbol,
                resolved_symbol=resolved,
                notional_usd=_safe_float(item.get("notional_usd")),
                side=str(item.get("side") or "long").strip().lower() or "long",
                chain=str(item.get("chain") or "").strip() or None,
                venue=str(item.get("venue") or "").strip() or None,
                token_address=str(item.get("token_address") or "").strip() or None,
            )
        )
    return holdings, unresolved


async def _build_exposure_stage(
    *,
    holdings: list[Holding],
    lookback_days: int,
) -> dict[str, Any]:
    series_by_symbol: dict[str, pd.Series] = {}
    asset_stats: list[dict[str, Any]] = []
    unavailable_symbols: list[str] = []

    for holding in holdings:
        key = holding.resolved_symbol
        if key not in series_by_symbol:
            try:
                series_by_symbol[key] = await _fetch_price_series(key, lookback_days)
            except Exception:
                unavailable_symbols.append(holding.symbol)
                continue

    usable_holdings = [holding for holding in holdings if holding.resolved_symbol in series_by_symbol]
    if not usable_holdings:
        return {
            "usable_holdings": [],
            "unavailable_symbols": unavailable_symbols,
            "combined_prices": pd.DataFrame(),
            "asset_returns": pd.DataFrame(),
            "portfolio_returns": pd.Series(dtype=float),
            "portfolio_prices": pd.Series(dtype=float),
            "gross_notional_usd": 0.0,
            "summary": {
                "assets": [],
                "portfolio_series_hours": 0,
                "portfolio_ann_vol": 0.0,
                "portfolio_max_dd": 0.0,
                "portfolio_sharpe": 0.0,
                "insufficient_data": True,
            },
        }

    # Separate assets with sufficient hourly data from those without.
    # Assets with < MIN_REQUIRED_HOURS of data are moved to insufficient_symbols
    # and get soft recommendations instead of blocking the entire portfolio.
    sufficient_holdings: list[Holding] = []
    insufficient_symbols: list[str] = []
    for holding in usable_holdings:
        key = holding.resolved_symbol
        series = series_by_symbol.get(key)
        if series is not None and len(series.dropna()) >= MIN_REQUIRED_HOURS:
            sufficient_holdings.append(holding)
        else:
            insufficient_symbols.append(holding.symbol)

    if not sufficient_holdings:
        # Fall back: try with whatever we have, even if short
        sufficient_holdings = usable_holdings
        insufficient_symbols = []

    combined = pd.concat(
        [series_by_symbol[holding.resolved_symbol] for holding in sufficient_holdings],
        axis=1,
    )
    combined = combined.loc[:, ~combined.columns.duplicated()].dropna()
    asset_returns = combined.apply(_log_returns).dropna()

    weights: dict[str, float] = {}
    for holding in sufficient_holdings:
        signed_notional = holding.notional_usd * (1 if holding.side == "long" else -1)
        weights[holding.resolved_symbol] = weights.get(holding.resolved_symbol, 0.0) + signed_notional

    gross_notional = sum(abs(weight) for weight in weights.values())
    portfolio_returns = pd.Series(dtype=float)
    if gross_notional > 0 and not asset_returns.empty:
        weighted_series = []
        for symbol, signed_notional in weights.items():
            if symbol in asset_returns:
                weighted_series.append(asset_returns[symbol] * (signed_notional / gross_notional))
        if weighted_series:
            portfolio_returns = sum(weighted_series).dropna()

    # Synthetic portfolio price index for cointegration tests (cumulative returns → price)
    portfolio_prices = pd.Series(dtype=float)
    if not portfolio_returns.empty:
        portfolio_prices = (np.exp(portfolio_returns.cumsum()) * 100.0).rename("portfolio_price")

    for holding in sufficient_holdings:
        symbol = holding.resolved_symbol
        returns = asset_returns[symbol] if symbol in asset_returns else pd.Series(dtype=float)
        asset_stats.append(
            {
                "symbol": holding.symbol,
                "resolved_symbol": symbol,
                "notional_usd": round(holding.notional_usd, 2),
                "side": holding.side,
                "hours": int(combined[symbol].dropna().shape[0]) if symbol in combined else 0,
                "ann_vol": round(_annualized_vol(returns), 4),
            }
        )

    summary = {
        "assets": asset_stats,
        "portfolio_series_hours": int(len(portfolio_returns)),
        "portfolio_ann_vol": round(_annualized_vol(portfolio_returns), 4),
        "portfolio_max_dd": round(_max_drawdown(portfolio_returns), 4),
        "portfolio_sharpe": round(_sharpe_ratio(portfolio_returns), 4),
        "gross_portfolio_notional_usd": round(gross_notional, 2),
        "series_start": _utc_iso(combined.index.min()) if not combined.empty else "",
        "series_end": _utc_iso(combined.index.max()) if not combined.empty else "",
        "unavailable_symbols": unavailable_symbols,
        "insufficient_data": len(portfolio_returns) < MIN_REQUIRED_HOURS,
    }

    return {
        "usable_holdings": usable_holdings,
        "unavailable_symbols": unavailable_symbols,
        "combined_prices": combined,
        "asset_returns": asset_returns,
        "portfolio_returns": portfolio_returns,
        "portfolio_prices": portfolio_prices,
        "gross_notional_usd": gross_notional,
        "summary": summary,
    }


async def _build_candidate(
    *,
    symbol: str,
    match_type: str,
    portfolio_returns: pd.Series,
    portfolio_prices: pd.Series,
    lookback_days: int,
    max_funding_cost_pct: float,
    max_funding_std_30d: float = 0.0005,
    max_spread_bps: float = 50.0,
    hedge_priority: float = 0.5,
    screen_row: dict[str, Any] | None = None,
) -> Candidate | None:
    try:
        payload = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=symbol,
            series=["price", "funding"],
            lookback_days=lookback_days,
            venue="hyperliquid",
        )
    except Exception:
        return None

    price_frame = payload.get("price")
    if not isinstance(price_frame, pd.DataFrame) or price_frame.empty:
        return None

    candidate_returns = _log_returns(
        _series_column(price_frame, "price_usd", "close", "price", "mark_price_usd").rename(symbol)
    )
    aligned = pd.concat(
        [portfolio_returns.rename("portfolio"), candidate_returns.rename("candidate")],
        axis=1,
    ).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return None

    correlation = _safe_float(aligned["portfolio"].corr(aligned["candidate"]))
    preferred_side = "short" if correlation >= 0 else "long"

    # Cointegration test on price levels (not returns)
    candidate_prices = _series_column(price_frame, "price_usd", "close", "price", "mark_price_usd").astype(float)
    coint = _engle_granger(portfolio_prices, candidate_prices)

    funding_frame = payload.get("funding")
    funding_now = 0.0
    funding_mean_7d = 0.0
    funding_mean_30d = 0.0
    funding_std_30d = 0.0
    ann_funding_rate = 0.0
    oi_usd = _row_value(screen_row or {}, "oi_now", "oi_usd")
    volume_usd = _row_value(screen_row or {}, "volume_24h", "volume_usd")
    spread_bps = _row_value(screen_row or {}, "spread_bps", "mark_spread_bps")
    mark_price_usd = _row_value(screen_row or {}, "mark_price_usd", "price_usd")

    if isinstance(funding_frame, pd.DataFrame) and not funding_frame.empty:
        funding_series = _series_column(
            funding_frame,
            "funding_rate",
            "funding_now",
            "funding",
        ).astype(float)
        funding_now = _safe_float(funding_series.dropna().iloc[-1]) if not funding_series.dropna().empty else 0.0
        recent = funding_series.tail(min(168, len(funding_series)))
        funding_mean_7d = _safe_float(recent.mean())
        funding_mean_30d = _safe_float(funding_series.mean())
        funding_std_30d = _safe_float(funding_series.std())
        ann_funding_rate = funding_mean_30d * 24 * 365
        oi_usd = oi_usd or _latest_value(funding_frame, "oi_usd", "oi_now")
        volume_usd = volume_usd or _latest_value(funding_frame, "volume_usd", "volume_24h")
        spread_bps = spread_bps or _latest_value(funding_frame, "spread_bps", "mark_spread_bps")
        mark_price_usd = mark_price_usd or _latest_value(
            funding_frame,
            "mark_price_usd",
            "price_usd",
            "mark_price",
        )

    ann_funding_cost_pct = _side_funding_cost_pct(ann_funding_rate, preferred_side) * 100
    funding_drag_ratio = max(ann_funding_cost_pct, 0.0) / max(max_funding_cost_pct, 1.0)

    # Tightness score: blend of cointegration quality and correlation
    # Cointegrated pairs with short half-life are the tightest hedges
    coint_quality = 0.0
    if coint["cointegrated"]:
        # ADF stat is negative; more negative = stronger. Normalize: -5 → 1.0, -2.87 → 0.0
        adf_norm = _clamp((-coint["adf_stat"] - 2.87) / 2.13, 0.0, 1.0)
        # Half-life: shorter is better. 1h → 1.0, 168h+ → 0.0
        hl = max(coint["half_life"], 1.0)
        hl_norm = _clamp(1.0 - math.log(hl) / math.log(168), 0.0, 1.0)
        coint_quality = 0.6 * adf_norm + 0.4 * hl_norm
    tightness = max(coint_quality, abs(correlation) * 0.5)  # fall back to correlation if not cointegrated

    # Scoring weights interpolated by hedge_priority:
    #   0.0 = tightest (tightness 0.50, cost 0.10, stability 0.20, spread 0.20)
    #   1.0 = most profitable (tightness 0.05, cost 0.55, stability 0.30, spread 0.10)
    p = _clamp(hedge_priority, 0.0, 1.0)
    w_tight = 0.50 - 0.45 * p     # 0.50 → 0.05
    w_cost = 0.10 + 0.45 * p      # 0.10 → 0.55
    w_stability = 0.20 + 0.10 * p  # 0.20 → 0.30
    w_spread = 0.20 - 0.10 * p    # 0.20 → 0.10

    cost_score = max(0.0, 1.0 - funding_drag_ratio)
    funding_stability = (
        max(0.0, 1.0 - funding_std_30d / max_funding_std_30d)
        if max_funding_std_30d > 0
        else 0.5
    )
    spread_penalty = min(spread_bps / max(max_spread_bps, 1.0), 1.0) if max_spread_bps > 0 else 0.0
    base_score = (
        tightness * w_tight
        + cost_score * w_cost
        + funding_stability * w_stability
        + (1.0 - spread_penalty) * w_spread
    )
    if match_type == "direct":
        base_score += 1.0

    return Candidate(
        symbol=symbol,
        match_type=match_type,
        preferred_side=preferred_side,
        correlation=correlation,
        hedge_score=round(base_score, 4),
        funding_now=round(funding_now, 6),
        funding_mean_7d=round(funding_mean_7d, 6),
        funding_mean_30d=round(funding_mean_30d, 6),
        funding_std_30d=round(funding_std_30d, 6),
        ann_funding_rate=round(ann_funding_rate, 4),
        ann_funding_cost_pct=round(ann_funding_cost_pct, 2),
        funding_regime=_funding_regime(funding_mean_30d),
        oi_usd=round(oi_usd, 2),
        volume_usd=round(volume_usd, 2),
        spread_bps=round(spread_bps, 2),
        mark_price_usd=round(mark_price_usd, 6),
        hours=int(len(aligned)),
        returns=aligned["candidate"].rename(symbol),
        cointegrated=coint["cointegrated"],
        adf_stat=coint["adf_stat"],
        coint_hedge_ratio=coint["hedge_ratio"],
        half_life=coint["half_life"],
        prices=candidate_prices,
    )


async def _build_search_stage(
    *,
    holdings: list[Holding],
    portfolio_returns: pd.Series,
    portfolio_prices: pd.Series,
    lookback_days: int,
    decision: dict[str, Any],
    risk: dict[str, Any],
    hedge_priority: float = 0.5,
) -> dict[str, Any]:
    max_cost_pct = _safe_float(
        decision.get("max_annual_funding_cost_pct"),
        _safe_float(risk.get("max_annual_funding_drag_pct"), 25.0),
    )
    min_oi_usd = _safe_float(risk.get("min_perp_oi_usd"), 500_000.0)
    min_volume_usd = _safe_float(risk.get("min_perp_24h_volume_usd"), 250_000.0)
    max_spread_bps = _safe_float(risk.get("max_spread_bps"), 50.0)
    max_legs = int(_safe_float(decision.get("max_hedge_legs"), 4))
    direct_symbols = {holding.resolved_symbol for holding in holdings}

    direct_rows: dict[str, dict[str, Any]] = {}
    for symbol in sorted(direct_symbols):
        try:
            payload = await DELTA_LAB_CLIENT.screen_perp(
                basis=symbol,
                venue="hyperliquid",
                limit=5,
            )
        except Exception:
            continue
        for row in _screen_rows(payload):
            candidate_symbol = _candidate_symbol(row)
            if candidate_symbol:
                direct_rows[candidate_symbol] = row

    broad_rows: dict[str, dict[str, Any]] = {}
    try:
        payload = await DELTA_LAB_CLIENT.screen_perp(
            sort="oi_now",
            order="desc",
            venue="hyperliquid",
            limit=100,
        )
    except Exception:
        payload = {}
    for row in _screen_rows(payload):
        candidate_symbol = _candidate_symbol(row)
        if candidate_symbol:
            broad_rows[candidate_symbol] = row

    candidates: list[Candidate] = []
    rejected: list[dict[str, Any]] = []

    ordered_symbols: list[str] = sorted(direct_rows)
    broad_fetch_limit = max(max_legs * 3, 12)
    for symbol, row in broad_rows.items():
        if symbol in direct_rows:
            continue
        oi_usd = _row_value(row, "oi_now", "oi_usd")
        volume_usd = _row_value(row, "volume_24h", "volume_usd")
        spread_bps = _row_value(row, "spread_bps", "mark_spread_bps")
        if oi_usd and oi_usd < min_oi_usd:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"oi_usd {oi_usd:.0f} below {min_oi_usd:.0f}",
                }
            )
            continue
        if volume_usd and volume_usd < min_volume_usd:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"volume_usd {volume_usd:.0f} below {min_volume_usd:.0f}",
                }
            )
            continue
        if spread_bps and spread_bps > max_spread_bps:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"spread {spread_bps:.2f} bps above {max_spread_bps:.2f} bps cap",
                }
            )
            continue
        ordered_symbols.append(symbol)
        if len(ordered_symbols) >= len(direct_rows) + broad_fetch_limit:
            break

    max_funding_std = _safe_float(decision.get("max_funding_std_30d"), 0.0005)

    for symbol in ordered_symbols:
        row = direct_rows.get(symbol) or broad_rows.get(symbol) or {}
        match_type = "direct" if symbol in direct_rows or symbol in direct_symbols else "proxy"
        candidate = await _build_candidate(
            symbol=symbol,
            match_type=match_type,
            portfolio_returns=portfolio_returns,
            portfolio_prices=portfolio_prices,
            lookback_days=lookback_days,
            max_funding_cost_pct=max_cost_pct,
            max_funding_std_30d=max_funding_std,
            max_spread_bps=max_spread_bps,
            hedge_priority=hedge_priority,
            screen_row=row,
        )
        if candidate is None:
            rejected.append({"symbol": symbol, "reason": "insufficient overlapping price history"})
            continue
        if candidate.match_type != "direct" and abs(candidate.correlation) < 0.20:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"correlation {candidate.correlation:.2f} below 0.20 floor",
                }
            )
            continue
        if candidate.oi_usd > 0 and candidate.oi_usd < min_oi_usd:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"oi_usd {candidate.oi_usd:.0f} below {min_oi_usd:.0f}",
                }
            )
            continue
        if candidate.volume_usd > 0 and candidate.volume_usd < min_volume_usd:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"volume_usd {candidate.volume_usd:.0f} below {min_volume_usd:.0f}",
                }
            )
            continue
        if candidate.ann_funding_cost_pct > max_cost_pct:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": (
                        f"annual funding cost {candidate.ann_funding_cost_pct:.2f}% "
                        f"above {max_cost_pct:.2f}% cap"
                    ),
                }
            )
            continue
        if candidate.spread_bps and candidate.spread_bps > max_spread_bps:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": f"spread {candidate.spread_bps:.2f} bps above {max_spread_bps:.2f} bps cap",
                }
            )
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda item: (item.match_type != "direct", -item.hedge_score))
    kept = candidates[: max(max_legs * 2, 4)]

    return {
        "candidates": kept,
        "artifact": {
            "direct_matches": [_candidate_to_dict(item) for item in kept if item.match_type == "direct"],
            "candidates": [_candidate_to_dict(item) for item in kept if item.match_type != "direct"],
            "rejected": rejected,
            "screened_count": len(direct_rows) + len(broad_rows),
            "surviving_count": len(kept),
        },
    }


def _evaluate_combo(
    *,
    combo: tuple[Candidate, ...],
    portfolio_returns: pd.Series,
    factor_returns: dict[str, pd.Series],
    gross_notional_usd: float,
    min_leg_notional_usd: float,
    max_position_oi_fraction: float = 0.02,
    max_funding_std_30d: float = 0.0005,
    max_spread_bps: float = 50.0,
) -> dict[str, Any] | None:
    aligned = pd.concat(
        [portfolio_returns.rename("portfolio"), *[candidate.returns.rename(candidate.symbol) for candidate in combo]],
        axis=1,
    ).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return None

    y = aligned["portfolio"].to_numpy(dtype=float)
    x = aligned.drop(columns=["portfolio"]).to_numpy(dtype=float)
    try:
        weights, *_ = np.linalg.lstsq(x, -y, rcond=None)
    except np.linalg.LinAlgError:
        return None

    if not np.isfinite(weights).all():
        return None

    hedged = pd.Series(y + x @ weights, index=aligned.index, name="hedged")
    unhedged = aligned["portfolio"]
    unhedged_var = _safe_float(unhedged.var())
    hedged_var = _safe_float(hedged.var())
    variance_reduction = 0.0 if unhedged_var == 0 else 1 - hedged_var / unhedged_var
    ann_vol = _annualized_vol(hedged)
    max_dd = _max_drawdown(hedged)
    residual_vol_ratio = 1.0 if unhedged_var == 0 else _annualized_vol(hedged) / max(_annualized_vol(unhedged), 1e-9)

    legs: list[dict[str, Any]] = []
    ann_funding_drag_pct = 0.0
    total_notional_usd = 0.0
    max_leg_oi_usd = 0.0
    max_leg_volume_usd = 0.0
    max_leg_spread_bps = 0.0

    for candidate, weight in zip(combo, weights, strict=True):
        notional_usd = abs(weight) * gross_notional_usd
        if notional_usd < min_leg_notional_usd:
            return None
        side = _side_from_weight(float(weight))
        leg_funding_cost_pct = _side_funding_cost_pct(candidate.ann_funding_rate, side) * 100
        ann_funding_drag_pct += (notional_usd / max(gross_notional_usd, 1.0)) * leg_funding_cost_pct
        total_notional_usd += notional_usd
        max_leg_oi_usd = max(max_leg_oi_usd, candidate.oi_usd)
        max_leg_volume_usd = max(max_leg_volume_usd, candidate.volume_usd)
        max_leg_spread_bps = max(max_leg_spread_bps, candidate.spread_bps)
        legs.append(
            {
                "symbol": candidate.symbol,
                "side": side,
                "match_type": candidate.match_type,
                "hedge_ratio": round(float(weight), 4),
                "notional_usd": round(notional_usd, 2),
                "correlation": round(candidate.correlation, 4),
                "ann_funding_cost_pct": round(leg_funding_cost_pct, 2),
                "oi_usd": round(candidate.oi_usd, 2),
                "volume_usd": round(candidate.volume_usd, 2),
                "spread_bps": round(candidate.spread_bps, 2),
                "score": round(candidate.hedge_score, 4),
                "cointegrated": candidate.cointegrated,
                "adf_stat": candidate.adf_stat,
                "half_life": candidate.half_life,
            }
        )

    net_betas = {
        symbol: _beta(hedged, factor_series)
        for symbol, factor_series in factor_returns.items()
    }
    max_net_beta = max((abs(value) for value in net_betas.values()), default=0.0)
    cost_adjusted_improvement = variance_reduction - ann_funding_drag_pct / 100

    # Blowout score: worst leg determines combo safety
    blowout_components: list[float] = []
    for candidate, leg_info in zip(combo, legs, strict=True):
        leg_notional = _safe_float(leg_info.get("notional_usd"))
        # OI safety: only penalize when OI data is available
        if candidate.oi_usd > 0 and max_position_oi_fraction > 0:
            oi_frac = leg_notional / candidate.oi_usd
            oi_ok = max(0.0, 1.0 - oi_frac / max_position_oi_fraction)
        else:
            oi_ok = 0.5  # neutral when data unavailable
        # Funding stability
        if max_funding_std_30d > 0:
            fund_stab = max(0.0, 1.0 - candidate.funding_std_30d / max_funding_std_30d)
        else:
            fund_stab = 0.5
        # Spread safety
        if candidate.spread_bps > 0 and max_spread_bps > 0:
            spread_ok = max(0.0, 1.0 - candidate.spread_bps / max_spread_bps)
        else:
            spread_ok = 0.5  # neutral when data unavailable
        blowout_components.append((oi_ok + fund_stab + spread_ok) / 3.0)
    blowout_score = min(blowout_components) if blowout_components else 0.5

    return {
        "hedge_id": "+".join(f"{leg['symbol']}-{leg['side']}" for leg in legs),
        "legs": legs,
        "hedged_ann_vol": round(ann_vol, 4),
        "hedged_max_dd": round(max_dd, 4),
        "variance_reduction_pct": round(variance_reduction * 100, 2),
        "ann_funding_cost_pct": round(ann_funding_drag_pct, 2),
        "cost_adjusted_improvement": round(cost_adjusted_improvement, 4),
        "blowout_score": round(blowout_score, 4),
        "net_betas": {symbol: round(value, 4) for symbol, value in net_betas.items()},
        "net_beta": round(max_net_beta, 4),
        "residual_vol_ratio": round(residual_vol_ratio, 4),
        "total_notional_usd": round(total_notional_usd, 2),
        "max_leg_oi_usd": round(max_leg_oi_usd, 2),
        "max_leg_volume_usd": round(max_leg_volume_usd, 2),
        "max_leg_spread_bps": round(max_leg_spread_bps, 2),
        "verdict": "pending",
    }


def _build_optimizer_stage(
    *,
    candidates: list[Candidate],
    portfolio_returns: pd.Series,
    factor_returns: dict[str, pd.Series],
    gross_notional_usd: float,
    max_hedge_legs: int,
    min_leg_notional_usd: float,
    decision: dict[str, Any],
    risk: dict[str, Any],
    hedge_priority: float = 0.5,
) -> dict[str, Any]:
    if not candidates or portfolio_returns.empty or gross_notional_usd <= 0:
        return {"evaluated": []}

    max_position_oi_fraction = _safe_float(decision.get("max_position_oi_fraction"), 0.02)
    max_funding_std = _safe_float(decision.get("max_funding_std_30d"), 0.0005)
    max_spread = _safe_float(risk.get("max_spread_bps"), 50.0)

    pool = candidates[: min(max(max_hedge_legs * 2, 6), len(candidates))]
    max_combo_size = min(max_hedge_legs, len(pool), 3)
    evaluated: list[dict[str, Any]] = []
    for combo_size in range(1, max_combo_size + 1):
        for combo in combinations(pool, combo_size):
            evaluation = _evaluate_combo(
                combo=combo,
                portfolio_returns=portfolio_returns,
                factor_returns=factor_returns,
                gross_notional_usd=gross_notional_usd,
                min_leg_notional_usd=min_leg_notional_usd,
                max_position_oi_fraction=max_position_oi_fraction,
                max_funding_std_30d=max_funding_std,
                max_spread_bps=max_spread,
            )
            if evaluation is not None:
                evaluated.append(evaluation)
    # Ranking: direct matches first, then blend cost vs tightness by hedge_priority
    p = _clamp(hedge_priority, 0.0, 1.0)

    def _combo_sort_key(item: dict[str, Any]) -> tuple:
        legs = item.get("legs") or []
        direct_count = sum(1 for leg in legs if leg.get("match_type") == "direct")
        cost = _safe_float(item.get("ann_funding_cost_pct"))
        var_red = _safe_float(item.get("variance_reduction_pct"))
        # Blend: at p=0 (tightest), rank by variance reduction; at p=1 (profitable), rank by cost
        # Composite score: higher is better
        composite = (1.0 - p) * var_red + p * (-cost)
        return (
            -direct_count,                                       # most direct matches first
            -composite,                                          # then blended score
            -_safe_float(item.get("blowout_score"), 0.5),        # then safest
        )
    evaluated.sort(key=_combo_sort_key)
    return {"evaluated": evaluated}


def _build_skeptic_stage(
    *,
    evaluated: list[dict[str, Any]],
    portfolio_returns: pd.Series,
    decision: dict[str, Any],
    null_reason: str,
) -> dict[str, Any]:
    null_baseline = {
        "ann_vol": round(_annualized_vol(portfolio_returns), 4),
        "max_dd": round(_max_drawdown(portfolio_returns), 4),
    }
    min_variance_reduction_pct = _safe_float(decision.get("min_variance_reduction_pct"), 3.0)
    max_residual_vol_ratio = _safe_float(decision.get("max_residual_vol_ratio"), 0.95)
    max_net_beta = _safe_float(decision.get("max_net_beta"), 0.15)
    min_blowout_score = _safe_float(decision.get("min_blowout_score"), 0.30)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in evaluated:
        reasons: list[str] = []
        if _safe_float(item.get("variance_reduction_pct")) < min_variance_reduction_pct:
            reasons.append(
                f"variance_reduction {item['variance_reduction_pct']:.2f}% below {min_variance_reduction_pct:.2f}% floor"
            )
        if _safe_float(item.get("residual_vol_ratio"), 1.0) > max_residual_vol_ratio:
            reasons.append(
                f"residual_vol_ratio {item['residual_vol_ratio']:.2f} above {max_residual_vol_ratio:.2f}"
            )
        if _safe_float(item.get("net_beta")) > max_net_beta:
            reasons.append(
                f"net_beta {item['net_beta']:.2f} above {max_net_beta:.2f}"
            )
        if _safe_float(item.get("blowout_score"), 1.0) < min_blowout_score:
            reasons.append(
                f"blowout_score {item.get('blowout_score', 0):.2f} below {min_blowout_score:.2f} safety floor"
            )
        if _safe_float(item.get("cost_adjusted_improvement")) < -0.05:
            reasons.append("cost-adjusted improvement is deeply negative")
        if reasons:
            rejected.append({"hedge_id": item["hedge_id"], "reason": "; ".join(reasons)})
            item["verdict"] = "reject"
            continue
        item["verdict"] = "pass"
        accepted.append(item)

    selected = accepted[0] if accepted else None
    return {
        "null_baseline": null_baseline,
        "evaluated": accepted[:10],
        "rejected": rejected[:10],
        "selected": selected["hedge_id"] if selected else "null-state",
        "selected_item": selected,
        "null_selected": selected is None,
        "null_reason": null_reason,
    }


def _build_risk_stage(
    *,
    selected: dict[str, Any] | None,
    decision: dict[str, Any],
    risk: dict[str, Any],
    constraints: dict[str, Any],
    gross_notional_usd: float,
) -> dict[str, Any]:
    if selected is None:
        return {
            "mode": "null",
            "passed": True,
            "notional_ok": True,
            "leverage_ok": True,
            "funding_ok": True,
            "liquidity_ok": True,
            "spread_ok": True,
            "min_leg_ok": True,
            "downgrade_reasons": [],
            "rejection_reasons": [],
            "total_notional_usd": 0.0,
            "required_leverage": 0.0,
            "total_ann_funding_cost_pct": 0.0,
        }

    max_notional_usd = min(
        _safe_float(risk.get("max_notional_usd"), float("inf")),
        _safe_float(constraints.get("hedge_budget_usd"), float("inf")),
    )
    max_allowed_leverage = min(
        _safe_float(risk.get("max_leverage"), 3.0),
        _safe_float(constraints.get("max_leverage"), 3.0),
    )
    max_allowed_funding_pct = min(
        _safe_float(risk.get("max_annual_funding_drag_pct"), 30.0),
        _safe_float(decision.get("max_annual_funding_cost_pct"), 25.0),
    )
    min_leg_notional_usd = _safe_float(constraints.get("min_leg_notional_usd"), 100.0)
    min_oi_usd = _safe_float(risk.get("min_perp_oi_usd"), 500_000.0)
    min_volume_usd = _safe_float(risk.get("min_perp_24h_volume_usd"), 250_000.0)
    max_spread_bps = _safe_float(risk.get("max_spread_bps"), 50.0)

    total_notional_usd = _safe_float(selected.get("total_notional_usd"))
    required_leverage = max(
        1.0,
        total_notional_usd / max(_safe_float(constraints.get("hedge_budget_usd"), 1.0), 1.0),
    )
    legs = selected.get("legs") if isinstance(selected.get("legs"), list) else []
    notional_ok = total_notional_usd <= max_notional_usd
    leverage_ok = required_leverage <= max_allowed_leverage
    funding_ok = _safe_float(selected.get("ann_funding_cost_pct")) <= max_allowed_funding_pct
    liquidity_ok = all(
        (_safe_float(leg.get("oi_usd")) <= 0 or _safe_float(leg.get("oi_usd")) >= min_oi_usd)
        and (
            _safe_float(leg.get("volume_usd")) <= 0
            or _safe_float(leg.get("volume_usd")) >= min_volume_usd
        )
        for leg in legs
    )
    spread_ok = all(
        _safe_float(leg.get("spread_bps")) <= max_spread_bps or _safe_float(leg.get("spread_bps")) == 0
        for leg in legs
    )
    min_leg_ok = all(
        _safe_float(leg.get("notional_usd")) >= min_leg_notional_usd for leg in legs
    )

    rejection_reasons: list[str] = []
    downgrade_reasons: list[str] = []
    if not leverage_ok:
        rejection_reasons.append(
            f"required leverage {required_leverage:.2f} exceeds {max_allowed_leverage:.2f}x cap"
        )
    if not liquidity_ok:
        rejection_reasons.append("one or more legs fall below OI or volume liquidity floors")
    if not spread_ok:
        rejection_reasons.append("one or more legs exceed max spread threshold")
    if not notional_ok:
        downgrade_reasons.append(
            f"total hedge notional ${total_notional_usd:,.0f} exceeds ${max_notional_usd:,.0f} budget"
        )
    if not funding_ok:
        downgrade_reasons.append(
            f"annual funding cost {selected['ann_funding_cost_pct']:.2f}% exceeds {max_allowed_funding_pct:.2f}% cap"
        )
    if not min_leg_ok:
        downgrade_reasons.append("one or more hedge legs fall below the minimum leg notional")

    if rejection_reasons:
        mode = "null"
        passed = False
    elif downgrade_reasons:
        mode = "draft"
        passed = False
    else:
        mode = "armed"
        passed = True

    return {
        "mode": mode,
        "passed": passed,
        "notional_ok": notional_ok,
        "leverage_ok": leverage_ok,
        "funding_ok": funding_ok,
        "liquidity_ok": liquidity_ok,
        "spread_ok": spread_ok,
        "min_leg_ok": min_leg_ok,
        "downgrade_reasons": downgrade_reasons,
        "rejection_reasons": rejection_reasons,
        "total_notional_usd": round(total_notional_usd, 2),
        "required_leverage": round(required_leverage, 2),
        "total_ann_funding_cost_pct": round(_safe_float(selected.get("ann_funding_cost_pct")), 2),
    }


def _build_job_stage(
    *,
    selected: dict[str, Any] | None,
    risk_checks: dict[str, Any],
    constraints: dict[str, Any],
    scheduler: dict[str, Any],
) -> dict[str, Any]:
    poll_seconds = int(_safe_float(scheduler.get("poll_seconds"), 3600))
    next_check = _utc_now() + timedelta(seconds=poll_seconds)
    drift_threshold_pct = round(_safe_float(scheduler.get("drift_threshold_pct"), 20.0), 2)
    cooldown_seconds = int(_safe_float(scheduler.get("rebalance_cooldown_seconds"), 86400))
    frequency = str(constraints.get("rebalance_frequency") or "daily")
    if selected is None or risk_checks["mode"] == "null":
        return {
            "mode": "null",
            "armed": False,
            "positions": [],
            "rebalance_rules": {
                "drift_threshold_pct": drift_threshold_pct,
                "frequency": frequency,
                "cooldown_seconds": cooldown_seconds,
            },
            "monitoring": {
                "funding_alert_threshold_pct": 0,
                "correlation_drift_alert": 0.0,
                "oi_drop_alert_pct": 50,
            },
            "invalidation": [],
            "poll_every": poll_seconds,
            "next_check": _utc_iso(next_check),
        }

    positions = [
        {
            "symbol": leg["symbol"],
            "side": leg["side"],
            "notional_usd": leg["notional_usd"],
            "leverage": leg.get("safe_leverage", risk_checks["required_leverage"]),
        }
        for leg in selected.get("legs", [])
    ]
    invalidation = [
        "portfolio-hedge correlation drops below 0.40 for 48 consecutive hours",
        f"annualized funding cost exceeds {risk_checks['total_ann_funding_cost_pct'] + 10:.2f}%",
        "any hedge leg falls below liquidity floors or spread limits",
    ]
    return {
        "mode": risk_checks["mode"],
        "armed": risk_checks["mode"] == "armed",
        "positions": positions,
        "rebalance_rules": {
            "drift_threshold_pct": drift_threshold_pct,
            "frequency": frequency,
            "cooldown_seconds": cooldown_seconds,
        },
        "monitoring": {
            "funding_alert_threshold_pct": round(
                _safe_float(risk_checks.get("total_ann_funding_cost_pct"), 0.0) + 10.0,
                2,
            ),
            "correlation_drift_alert": 0.30,
            "oi_drop_alert_pct": 50,
        },
        "invalidation": invalidation,
        "poll_every": poll_seconds,
        "next_check": _utc_iso(next_check),
    }


async def run(
    *,
    root: Path,
    assets_path: Path,
    constraints_path: Path,
    policy_path: Path,
    run_id: str,
) -> dict[str, Any]:
    assets = _load_yaml(assets_path)
    constraints = _load_yaml(constraints_path)
    policy = _load_yaml(policy_path)
    decision = policy.get("decision") if isinstance(policy.get("decision"), dict) else {}
    risk = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    scheduler = (
        policy.get("scheduler") if isinstance(policy.get("scheduler"), dict) else {}
    )
    null_state_policy = (
        policy.get("null_state") if isinstance(policy.get("null_state"), dict) else {}
    )
    lookback_days = int(
        _safe_float(
            constraints.get("lookback_days"),
            _safe_float(
                ((policy.get("signals") or {}).get("portfolio_return") or {}).get("lookback_days"),
                30.0,
            ),
        )
    )
    hedge_priority = _clamp(_safe_float(constraints.get("hedge_priority"), 0.5), 0.0, 1.0)
    holdings, unresolved_symbols = await _build_holdings(
        [item for item in assets.get("holdings", []) if isinstance(item, dict)]
    )
    artifact_root, write_artifact = _artifact_writer(root, run_id)

    exposure = await _build_exposure_stage(holdings=holdings, lookback_days=lookback_days)
    exposure_summary = {
        **exposure["summary"],
        "unresolved_symbols": unresolved_symbols,
    }
    write_artifact("exposure_reader.json", exposure_summary)

    portfolio_returns = exposure["portfolio_returns"]
    factor_returns = await _fetch_factor_returns(lookback_days)
    factor_betas: dict[str, dict[str, float]] = {}
    for factor_name, factor_series in factor_returns.items():
        factor_betas[factor_name] = _rolling_beta(portfolio_returns, factor_series)

    model_r2 = _model_r2(portfolio_returns, factor_returns)
    beta_stable = all(
        _safe_float(metrics.get("beta_std")) <= 0.25 for metrics in factor_betas.values()
    )
    hedge_ratios = {
        "single_factor": {
            factor: round(-metrics["beta_current"], 4)
            for factor, metrics in factor_betas.items()
        }
    }
    beta_summary = {
        "factor_betas": [
            {
                "factor": factor,
                **{key: round(_safe_float(value), 4) for key, value in metrics.items()},
            }
            for factor, metrics in factor_betas.items()
        ],
        "residual_ann_vol": round(_annualized_vol(portfolio_returns), 4),
        "hedge_ratios": hedge_ratios,
        "model_r2": round(model_r2, 4),
        "beta_stable": beta_stable,
        "low_explanatory_power": model_r2 < 0.10,
    }
    write_artifact("beta_modeler.json", beta_summary)

    insufficient_data = bool(exposure_summary.get("insufficient_data"))
    if insufficient_data or portfolio_returns.empty or exposure["gross_notional_usd"] <= 0:
        null_reason = (
            "Insufficient overlapping hourly history to estimate a reliable hedge."
            if insufficient_data
            else "No usable holdings remain after symbol resolution and market-data fetches."
        )
        risk_checks = _build_risk_stage(
            selected=None,
            decision=decision,
            risk=risk,
            constraints=constraints,
            gross_notional_usd=exposure["gross_notional_usd"],
        )
        job = _build_job_stage(
            selected=None,
            risk_checks=risk_checks,
            constraints=constraints,
            scheduler=scheduler,
        )
        final = {
            "signal_snapshot": {
                **exposure_summary,
                "factor_betas": {
                    factor: round(metrics.get("beta_current", 0.0), 4)
                    for factor, metrics in factor_betas.items()
                },
                "artifacts_run_id": run_id,
            },
            "selected_playbook": {"id": "null-state", "score": 0.0, "mode": "null"},
            "candidate_expressions": [],
            "null_state": {"selected": True, "reason": null_reason},
            "risk_checks": risk_checks,
            "job": job,
            "next_invalidation": "collect more history before retrying hedge compilation",
        }
        write_artifact("risk_gate.json", risk_checks)
        write_artifact("job.json", job)
        write_artifact("finalize.json", final)
        return final

    search = await _build_search_stage(
        holdings=exposure["usable_holdings"],
        portfolio_returns=portfolio_returns,
        portfolio_prices=exposure["portfolio_prices"],
        lookback_days=lookback_days,
        decision=decision,
        risk=risk,
        hedge_priority=hedge_priority,
    )
    write_artifact("hedge_search.json", search["artifact"])

    optimizer = _build_optimizer_stage(
        candidates=search["candidates"],
        portfolio_returns=portfolio_returns,
        factor_returns=factor_returns,
        gross_notional_usd=exposure["gross_notional_usd"],
        max_hedge_legs=int(_safe_float(decision.get("max_hedge_legs"), 4.0)),
        min_leg_notional_usd=_safe_float(constraints.get("min_leg_notional_usd"), 100.0),
        decision=decision,
        risk=risk,
        hedge_priority=hedge_priority,
    )
    write_artifact(
        "optimizer.json",
        {"evaluated": optimizer["evaluated"][:10]},
    )

    skeptic = _build_skeptic_stage(
        evaluated=optimizer["evaluated"],
        portfolio_returns=portfolio_returns,
        decision=decision,
        null_reason=str(
            null_state_policy.get("reason")
            or "No hedge materially improves the portfolio over doing nothing."
        ),
    )
    skeptic_artifact = {
        "null_baseline": skeptic["null_baseline"],
        "evaluated": skeptic["evaluated"],
        "rejected": skeptic["rejected"],
        "selected": skeptic["selected"],
        "null_selected": skeptic["null_selected"],
    }
    write_artifact("skeptic.json", skeptic_artifact)

    selected = skeptic["selected_item"]

    # Compute safe leverage per leg via historical backtest
    check_freq = str(constraints.get("check_frequency") or "daily")
    survival_hours = CHECK_FREQUENCY_SURVIVAL_HOURS.get(check_freq, 36)
    leverage_backtest_cfg = (
        policy.get("leverage_backtest")
        if isinstance(policy.get("leverage_backtest"), dict)
        else {}
    )
    stop_frac = _safe_float(leverage_backtest_cfg.get("stop_frac"), 0.75)
    fee_eps = _safe_float(leverage_backtest_cfg.get("fee_eps"), 0.003)
    max_allowed_leverage = int(
        min(
            _safe_float(risk.get("max_leverage"), 3.0),
            _safe_float(constraints.get("max_leverage"), 3.0),
        )
    )

    leverage_results: list[dict[str, Any]] = []
    if selected is not None:
        for leg in selected.get("legs", []):
            leg_symbol = str(leg.get("symbol") or "")
            candidate_match = next(
                (c for c in search["candidates"] if c.symbol == leg_symbol),
                None,
            )
            if candidate_match is not None:
                # Use the candidate's price/funding series for leverage backtest
                try:
                    ts_payload = await DELTA_LAB_CLIENT.get_asset_timeseries(
                        symbol=leg_symbol,
                        series=["price", "funding"],
                        lookback_days=lookback_days,
                        venue="hyperliquid",
                    )
                    price_frame = ts_payload.get("price")
                    funding_frame = ts_payload.get("funding")
                    if isinstance(price_frame, pd.DataFrame) and not price_frame.empty:
                        price_col = _series_column(price_frame, "price_usd", "close", "price", "mark_price_usd")
                        funding_col = (
                            _series_column(funding_frame, "funding_rate", "funding_now", "funding")
                            if isinstance(funding_frame, pd.DataFrame) and not funding_frame.empty
                            else pd.Series(0.0, index=price_col.index)
                        )
                        lev_result = compute_safe_leverage(
                            price_series=price_col,
                            funding_series=funding_col,
                            survival_hours=survival_hours,
                            stop_frac=stop_frac,
                            fee_eps=fee_eps,
                            max_leverage=max_allowed_leverage,
                        )
                        leverage_results.append({"symbol": leg_symbol, **lev_result})
                        leg["safe_leverage"] = lev_result["safe_leverage"]
                        continue
                except Exception:
                    pass
            leg["safe_leverage"] = 1
            leverage_results.append({"symbol": leg_symbol, "safe_leverage": 1, "insufficient_history": True})

    write_artifact("leverage_backtest.json", {
        "check_frequency": check_freq,
        "survival_hours": survival_hours,
        "results": leverage_results,
    })

    risk_checks = _build_risk_stage(
        selected=selected,
        decision=decision,
        risk=risk,
        constraints=constraints,
        gross_notional_usd=exposure["gross_notional_usd"],
    )
    write_artifact("risk_gate.json", risk_checks)

    job = _build_job_stage(
        selected=selected,
        risk_checks=risk_checks,
        constraints=constraints,
        scheduler=scheduler,
    )
    write_artifact("job.json", job)

    single_leg_candidates = [
        item
        for item in optimizer["evaluated"]
        if len(item.get("legs", [])) == 1
    ][:5]
    candidate_expressions: list[dict[str, Any]] = [
        {
            "id": item["hedge_id"],
            "type": "hyperliquid_perp",
            "symbol": item["legs"][0]["symbol"],
            "side": item["legs"][0]["side"],
            "match_type": item["legs"][0]["match_type"],
            "hedge_ratio": item["legs"][0]["hedge_ratio"],
            "variance_reduction_pct": item["variance_reduction_pct"],
            "ann_funding_cost_pct": item["ann_funding_cost_pct"],
            "correlation": item["legs"][0]["correlation"],
            "cointegrated": item["legs"][0].get("cointegrated", False),
            "adf_stat": item["legs"][0].get("adf_stat", 0.0),
            "half_life_hours": item["legs"][0].get("half_life", float("inf")),
            "blowout_score": item.get("blowout_score", 0.5),
            "score": item["cost_adjusted_improvement"],
        }
        for item in single_leg_candidates
    ]

    # Soft recommendations for assets with no time series but a direct basis perp
    for symbol in exposure.get("unavailable_symbols", []):
        direct_hit = search["artifact"].get("direct_matches", [])
        if any(d.get("symbol") == symbol for d in direct_hit):
            candidate_expressions.append({
                "id": f"{symbol}-short-soft",
                "type": "hyperliquid_perp",
                "symbol": symbol,
                "side": "short",
                "match_type": "basis_only",
                "confidence": "low",
                "note": (
                    f"Short {symbol} perp appears to be a direct basis match "
                    "but we lack price history to compute correlation/variance. "
                    "Recommended as a manual hedge candidate."
                ),
            })

    signal_snapshot = {
        **exposure_summary,
        "factor_betas": {
            factor: round(metrics.get("beta_current", 0.0), 4)
            for factor, metrics in factor_betas.items()
        },
        "factor_beta_details": beta_summary["factor_betas"],
        "model_r2": beta_summary["model_r2"],
        "beta_stable": beta_summary["beta_stable"],
        "screened_candidates": search["artifact"]["screened_count"],
        "surviving_candidates": search["artifact"]["surviving_count"],
        "artifacts_run_id": run_id,
    }

    if selected is None:
        selected_playbook = {"id": "null-state", "score": 0.0, "mode": "null"}
        null_state = {"selected": True, "reason": skeptic["null_reason"]}
        next_invalidation = "portfolio annualized volatility or factor beta materially changes"
    else:
        selected_playbook = {
            "id": selected["hedge_id"],
            "score": round(_safe_float(selected.get("cost_adjusted_improvement")), 4),
            "mode": risk_checks["mode"],
            "legs": selected.get("legs", []),
        }
        null_state = {
            "selected": False,
            "reason": (
                f"{selected['hedge_id']} clears variance, beta, and cost thresholds."
            ),
        }
        next_invalidation = (
            job["invalidation"][0]
            if isinstance(job.get("invalidation"), list) and job["invalidation"]
            else "portfolio-hedge relationship materially changes"
        )

    final = {
        "signal_snapshot": signal_snapshot,
        "selected_playbook": selected_playbook,
        "candidate_expressions": candidate_expressions,
        "null_state": null_state,
        "risk_checks": risk_checks,
        "job": job,
        "next_invalidation": next_invalidation,
    }
    write_artifact("finalize.json", final)
    return final


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile a Hyperliquid hedge recommendation.")
    parser.add_argument("--assets", type=Path, default=None, help="Override inputs/assets.yaml")
    parser.add_argument(
        "--constraints",
        type=Path,
        default=None,
        help="Override inputs/constraints.yaml",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Override policy/default.yaml",
    )
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID") or "", help="Artifact run id")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    root = Path(__file__).resolve().parent.parent
    assets_path = args.assets or root / "inputs" / "assets.yaml"
    constraints_path = args.constraints or root / "inputs" / "constraints.yaml"
    policy_path = args.policy or root / "policy" / "default.yaml"
    run_id = args.run_id.strip() or _utc_now().strftime("hedge-run-%Y%m%d-%H%M%S")

    result = asyncio.run(
        run(
            root=root,
            assets_path=assets_path,
            constraints_path=constraints_path,
            policy_path=policy_path,
            run_id=run_id,
        )
    )
    print(json.dumps(_jsonify(result), indent=2))


if __name__ == "__main__":
    main()
