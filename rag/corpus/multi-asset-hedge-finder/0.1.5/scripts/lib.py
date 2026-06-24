"""Hedge Finder shared library.

All pipeline agents import from here. Do not re-implement these functions inline.

Functions:
    # Data fetching
    resolve_symbol(symbol) -> str
    fetch_price_series(symbol, lookback_days) -> pd.Series
    fetch_funding_series(symbol, lookback_days) -> pd.Series
    fetch_universe(symbols, lookback_days) -> (prices_df, funding_df)
    screen_perps(basis, sort, limit) -> list[dict]
    fetch_factor_returns(lookback_days) -> dict[str, pd.Series]
    build_holdings(raw_holdings) -> (list[Holding], list[str])

    # Portfolio construction
    build_portfolio(holdings, lookback_days) -> dict
    build_portfolio_prices(portfolio_returns) -> pd.Series

    # Cointegration & statistics
    engle_granger(y, x) -> dict
    half_life(spread) -> float
    adf_statistic(series) -> float
    rolling_beta(target, factor, window) -> dict
    model_r2(target, factors) -> float
    beta(target, factor) -> float

    # Returns & risk
    log_returns(prices) -> pd.Series
    annualized_vol(returns) -> float
    max_drawdown(returns) -> float
    sharpe_ratio(returns) -> float

    # Scoring & evaluation
    evaluate_combo(combo, portfolio_returns, factor_returns, ...) -> dict
    compute_blowout_score(candidates, legs, policy_params) -> float
    score_candidate(correlation, funding_drag_ratio, funding_std, spread_bps, ...) -> float

    # Utilities
    safe_float(value, default) -> float
    clamp(value, lower, upper) -> float
    jsonify(value) -> Any
    load_yaml(path) -> dict
    write_artifact(path, payload) -> None
    utc_now() -> datetime
    utc_iso(value) -> str
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from wayfinder_paths.core.clients import DELTA_LAB_CLIENT

MIN_REQUIRED_HOURS = 168
FACTOR_SYMBOLS = ("BTC", "ETH")


# ── Data classes ──────────────────────────────────────────────


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
    cointegrated: bool = False
    adf_stat: float = 0.0
    coint_hedge_ratio: float = 0.0
    half_life: float = float("inf")
    prices: pd.Series | None = None


# ── Utilities ─────────────────────────────────────────────────


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | pd.Timestamp | None = None) -> str:
    current = utc_now() if value is None else pd.Timestamp(value).to_pydatetime()
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonify(inner) for key, inner in value.items()}
    if isinstance(value, list | tuple):
        return [jsonify(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime | pd.Timestamp):
        return utc_iso(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, pd.Series):
        return [jsonify(item) for item in value.tolist()]
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonify(payload), indent=2) + "\n", encoding="utf-8")


def candidate_to_dict(c: Candidate) -> dict[str, Any]:
    d = asdict(c)
    d.pop("returns", None)
    d.pop("prices", None)
    return d


# ── Series helpers ────────────────────────────────────────────


def series_column(frame: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in frame.columns:
            return frame[name]
    raise KeyError(f"Missing expected series column. Tried: {', '.join(names)}")


def latest_value(frame: pd.DataFrame, *names: str, default: float = 0.0) -> float:
    for name in names:
        if name in frame.columns and not frame[name].dropna().empty:
            return safe_float(frame[name].dropna().iloc[-1], default)
    return default


def row_value(row: dict[str, Any], *names: str, default: float = 0.0) -> float:
    for name in names:
        if name in row and row[name] is not None:
            return safe_float(row[name], default)
    return default


def screen_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "results"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def candidate_symbol(row: dict[str, Any]) -> str:
    for key in ("basis_symbol", "symbol", "base_symbol", "asset_symbol"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def funding_regime(mean_rate: float) -> str:
    if abs(mean_rate) < 0.0001:
        return "neutral"
    return "shorts pay" if mean_rate > 0 else "longs pay"


def side_from_weight(weight: float) -> str:
    return "short" if weight < 0 else "long"


def side_funding_cost_pct(ann_funding_rate: float, side: str) -> float:
    return ann_funding_rate if side == "long" else -ann_funding_rate


# ── Returns & risk ────────────────────────────────────────────


def log_returns(prices: pd.Series) -> pd.Series:
    clean = prices.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    return np.log(clean / clean.shift(1)).dropna()


def annualized_vol(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return safe_float(returns.std() * math.sqrt(24 * 365))


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    cumulative = np.exp(returns.cumsum())
    running_max = cumulative.cummax()
    drawdown = cumulative / running_max - 1
    return safe_float(drawdown.min())


def sharpe_ratio(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    vol = returns.std()
    if vol == 0 or pd.isna(vol):
        return 0.0
    return safe_float((returns.mean() / vol) * math.sqrt(24 * 365))


# ── Cointegration & statistics ────────────────────────────────


def adf_statistic(series: pd.Series) -> float:
    """ADF(1) test statistic. More negative = more stationary.
    Critical value (5%): ~-2.87 for n=500.
    """
    y = series.dropna().to_numpy(dtype=float)
    if len(y) < 24:
        return 0.0
    dy = np.diff(y)
    y_lag = y[:-1]
    dy_lag = np.concatenate([[0.0], dy[:-1]])
    n = len(dy)
    x = np.column_stack([np.ones(n), y_lag, dy_lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(x, dy, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    b = coeffs[1]
    resid = dy - x @ coeffs
    s2 = float((resid ** 2).sum()) / max(n - 3, 1)
    xtx_inv = np.linalg.pinv(x.T @ x)
    se = math.sqrt(max(s2 * xtx_inv[1, 1], 1e-30))
    return float(b / se)


def half_life(spread: pd.Series) -> float:
    """Half-life of mean reversion via AR(1). Returns hours."""
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
        return float("inf")
    try:
        return -math.log(2) / math.log(1 + lam)
    except (ValueError, ZeroDivisionError):
        return float("inf")


def engle_granger(y: pd.Series, x: pd.Series) -> dict[str, Any]:
    """Engle-Granger cointegration test on price levels."""
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
    hr = float(coeffs[1])
    residuals = pd.Series(y_arr - x_mat @ coeffs, index=aligned.index)
    adf = adf_statistic(residuals)
    hl = half_life(residuals)
    return {
        "hedge_ratio": round(hr, 6),
        "adf_stat": round(adf, 4),
        "cointegrated": adf < -2.87,
        "half_life": round(hl, 1),
    }


def beta(target: pd.Series, factor: pd.Series) -> float:
    aligned = pd.concat([target, factor], axis=1).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return 0.0
    factor_var = safe_float(aligned.iloc[:, 1].var())
    if factor_var == 0:
        return 0.0
    return safe_float(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / factor_var)


def rolling_beta(target: pd.Series, factor: pd.Series, window: int = MIN_REQUIRED_HOURS) -> dict[str, float]:
    aligned = pd.concat([target, factor], axis=1).dropna()
    if len(aligned) < max(window, 24):
        b = beta(target, factor)
        return {"beta_current": b, "beta_mean": b, "beta_std": 0.0}
    ts = aligned.iloc[:, 0]
    fs = aligned.iloc[:, 1]
    cov = ts.rolling(window).cov(fs)
    var = fs.rolling(window).var()
    rolling = (cov / var).replace([np.inf, -np.inf], np.nan).dropna()
    if rolling.empty:
        b = beta(target, factor)
        return {"beta_current": b, "beta_mean": b, "beta_std": 0.0}
    return {
        "beta_current": safe_float(rolling.iloc[-1]),
        "beta_mean": safe_float(rolling.mean()),
        "beta_std": safe_float(rolling.std()),
    }


def model_r2(target: pd.Series, factors: dict[str, pd.Series]) -> float:
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
    return clamp(1 - ss_res / ss_tot, 0.0, 1.0)


# ── Data fetching ─────────────────────────────────────────────


async def resolve_symbol(symbol: str) -> str:
    try:
        payload = await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol)
    except Exception:
        return symbol
    basis = payload.get("basis") if isinstance(payload, dict) else None
    if isinstance(basis, dict):
        root = str(basis.get("root_symbol") or "").strip()
        if root:
            return root
    resolved = str(payload.get("symbol") or "").strip() if isinstance(payload, dict) else ""
    return resolved or symbol


async def fetch_price_series(symbol: str, lookback_days: int) -> pd.Series:
    payload = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol=symbol, series=["price"], lookback_days=lookback_days,
    )
    frame = payload.get("price")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"No price series returned for {symbol}")
    return series_column(frame, "price_usd", "close", "price", "mark_price_usd").astype(float).rename(symbol)


async def fetch_timeseries(symbol: str, lookback_days: int, venue: str | None = None) -> dict[str, pd.DataFrame]:
    """Fetch price + funding DataFrames for a symbol."""
    kwargs: dict[str, Any] = {"symbol": symbol, "series": ["price", "funding"], "lookback_days": lookback_days}
    if venue:
        kwargs["venue"] = venue
    return await DELTA_LAB_CLIENT.get_asset_timeseries(**kwargs)


async def fetch_factor_returns(lookback_days: int) -> dict[str, pd.Series]:
    result: dict[str, pd.Series] = {}
    for sym in FACTOR_SYMBOLS:
        try:
            prices = await fetch_price_series(sym, lookback_days)
        except Exception:
            continue
        result[sym] = log_returns(prices).rename(sym)
    return result


async def screen_perps(
    basis: str | None = None,
    sort: str = "oi_now",
    order: str = "desc",
    venue: str = "hyperliquid",
    limit: int = 100,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"sort": sort, "order": order, "venue": venue, "limit": limit}
    if basis:
        kwargs["basis"] = basis
    return await DELTA_LAB_CLIENT.screen_perp(**kwargs)


async def build_holdings(raw_holdings: list[dict[str, Any]]) -> tuple[list[Holding], list[str]]:
    holdings: list[Holding] = []
    unresolved: list[str] = []
    for item in raw_holdings:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        resolved = await resolve_symbol(symbol)
        if not resolved:
            unresolved.append(symbol)
            continue
        holdings.append(Holding(
            symbol=symbol,
            resolved_symbol=resolved,
            notional_usd=safe_float(item.get("notional_usd")),
            side=str(item.get("side") or "long").strip().lower() or "long",
            chain=str(item.get("chain") or "").strip() or None,
            venue=str(item.get("venue") or "").strip() or None,
            token_address=str(item.get("token_address") or "").strip() or None,
        ))
    return holdings, unresolved


# ── Portfolio construction ────────────────────────────────────


async def build_portfolio(
    holdings: list[Holding],
    lookback_days: int,
) -> dict[str, Any]:
    """Fetch prices, align, compute returns. Separates sufficient from insufficient assets."""
    series_by_symbol: dict[str, pd.Series] = {}
    unavailable: list[str] = []

    for h in holdings:
        key = h.resolved_symbol
        if key not in series_by_symbol:
            try:
                series_by_symbol[key] = await fetch_price_series(key, lookback_days)
            except Exception:
                unavailable.append(h.symbol)

    usable = [h for h in holdings if h.resolved_symbol in series_by_symbol]
    if not usable:
        return {
            "holdings": [], "insufficient_symbols": [], "unavailable_symbols": unavailable,
            "portfolio_returns": pd.Series(dtype=float), "portfolio_prices": pd.Series(dtype=float),
            "asset_returns": pd.DataFrame(), "gross_notional_usd": 0.0, "summary": {"insufficient_data": True},
        }

    # Separate assets with enough data from those without
    sufficient: list[Holding] = []
    insufficient: list[str] = []
    for h in usable:
        s = series_by_symbol.get(h.resolved_symbol)
        if s is not None and len(s.dropna()) >= MIN_REQUIRED_HOURS:
            sufficient.append(h)
        else:
            insufficient.append(h.symbol)
    if not sufficient:
        sufficient = usable
        insufficient = []

    combined = pd.concat([series_by_symbol[h.resolved_symbol] for h in sufficient], axis=1)
    combined = combined.loc[:, ~combined.columns.duplicated()].dropna()
    asset_returns = combined.apply(log_returns).dropna()

    weights: dict[str, float] = {}
    for h in sufficient:
        signed = h.notional_usd * (1 if h.side == "long" else -1)
        weights[h.resolved_symbol] = weights.get(h.resolved_symbol, 0.0) + signed
    gross_notional = sum(abs(w) for w in weights.values())

    portfolio_returns = pd.Series(dtype=float)
    if gross_notional > 0 and not asset_returns.empty:
        weighted = [asset_returns[sym] * (n / gross_notional) for sym, n in weights.items() if sym in asset_returns]
        if weighted:
            portfolio_returns = sum(weighted).dropna()

    portfolio_prices = pd.Series(dtype=float)
    if not portfolio_returns.empty:
        portfolio_prices = (np.exp(portfolio_returns.cumsum()) * 100.0).rename("portfolio_price")

    asset_stats = []
    for h in sufficient:
        sym = h.resolved_symbol
        r = asset_returns[sym] if sym in asset_returns else pd.Series(dtype=float)
        asset_stats.append({
            "symbol": h.symbol, "resolved_symbol": sym,
            "notional_usd": round(h.notional_usd, 2), "side": h.side,
            "hours": int(combined[sym].dropna().shape[0]) if sym in combined else 0,
            "ann_vol": round(annualized_vol(r), 4),
        })

    summary = {
        "assets": asset_stats,
        "portfolio_series_hours": int(len(portfolio_returns)),
        "portfolio_ann_vol": round(annualized_vol(portfolio_returns), 4),
        "portfolio_max_dd": round(max_drawdown(portfolio_returns), 4),
        "portfolio_sharpe": round(sharpe_ratio(portfolio_returns), 4),
        "gross_portfolio_notional_usd": round(gross_notional, 2),
        "series_start": utc_iso(combined.index.min()) if not combined.empty else "",
        "series_end": utc_iso(combined.index.max()) if not combined.empty else "",
        "unavailable_symbols": unavailable,
        "insufficient_symbols": insufficient,
        "insufficient_data": len(portfolio_returns) < MIN_REQUIRED_HOURS,
    }

    return {
        "holdings": sufficient,
        "insufficient_symbols": insufficient,
        "unavailable_symbols": unavailable,
        "portfolio_returns": portfolio_returns,
        "portfolio_prices": portfolio_prices,
        "asset_returns": asset_returns,
        "gross_notional_usd": gross_notional,
        "summary": summary,
    }


# ── Candidate building ────────────────────────────────────────


async def build_candidate(
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
    """Build a hedge candidate with correlation, cointegration, and funding analysis."""
    try:
        payload = await fetch_timeseries(symbol, lookback_days, venue="hyperliquid")
    except Exception:
        return None

    price_frame = payload.get("price")
    if not isinstance(price_frame, pd.DataFrame) or price_frame.empty:
        return None

    candidate_returns = log_returns(
        series_column(price_frame, "price_usd", "close", "price", "mark_price_usd").rename(symbol)
    )
    aligned = pd.concat(
        [portfolio_returns.rename("portfolio"), candidate_returns.rename("candidate")],
        axis=1,
    ).dropna()
    if len(aligned) < MIN_REQUIRED_HOURS:
        return None

    correlation = safe_float(aligned["portfolio"].corr(aligned["candidate"]))
    preferred_side = "short" if correlation >= 0 else "long"

    # Cointegration test on price levels
    candidate_prices = series_column(price_frame, "price_usd", "close", "price", "mark_price_usd").astype(float)
    coint = engle_granger(portfolio_prices, candidate_prices)

    # Funding analysis
    funding_frame = payload.get("funding")
    funding_now = funding_mean_7d = funding_mean_30d = funding_std_30d = ann_funding_rate = 0.0
    oi_usd = row_value(screen_row or {}, "oi_now", "oi_usd")
    volume_usd = row_value(screen_row or {}, "volume_24h", "volume_usd")
    spread_bps = row_value(screen_row or {}, "spread_bps", "mark_spread_bps")
    mark_price_usd = row_value(screen_row or {}, "mark_price_usd", "price_usd")

    if isinstance(funding_frame, pd.DataFrame) and not funding_frame.empty:
        fs = series_column(funding_frame, "funding_rate", "funding_now", "funding").astype(float)
        funding_now = safe_float(fs.dropna().iloc[-1]) if not fs.dropna().empty else 0.0
        recent = fs.tail(min(168, len(fs)))
        funding_mean_7d = safe_float(recent.mean())
        funding_mean_30d = safe_float(fs.mean())
        funding_std_30d = safe_float(fs.std())
        ann_funding_rate = funding_mean_30d * 24 * 365
        oi_usd = oi_usd or latest_value(funding_frame, "oi_usd", "oi_now")
        volume_usd = volume_usd or latest_value(funding_frame, "volume_usd", "volume_24h")
        spread_bps = spread_bps or latest_value(funding_frame, "spread_bps", "mark_spread_bps")
        mark_price_usd = mark_price_usd or latest_value(funding_frame, "mark_price_usd", "price_usd", "mark_price")

    ann_funding_cost_pct = side_funding_cost_pct(ann_funding_rate, preferred_side) * 100
    funding_drag_ratio = max(ann_funding_cost_pct, 0.0) / max(max_funding_cost_pct, 1.0)

    score = score_candidate(
        correlation=correlation,
        funding_drag_ratio=funding_drag_ratio,
        funding_std_30d=funding_std_30d,
        spread_bps=spread_bps,
        max_funding_std_30d=max_funding_std_30d,
        max_spread_bps=max_spread_bps,
        hedge_priority=hedge_priority,
        coint_quality=coint,
        match_type=match_type,
    )

    return Candidate(
        symbol=symbol, match_type=match_type, preferred_side=preferred_side,
        correlation=correlation, hedge_score=round(score, 4),
        funding_now=round(funding_now, 6), funding_mean_7d=round(funding_mean_7d, 6),
        funding_mean_30d=round(funding_mean_30d, 6), funding_std_30d=round(funding_std_30d, 6),
        ann_funding_rate=round(ann_funding_rate, 4), ann_funding_cost_pct=round(ann_funding_cost_pct, 2),
        funding_regime=funding_regime(funding_mean_30d),
        oi_usd=round(oi_usd, 2), volume_usd=round(volume_usd, 2),
        spread_bps=round(spread_bps, 2), mark_price_usd=round(mark_price_usd, 6),
        hours=int(len(aligned)), returns=aligned["candidate"].rename(symbol),
        cointegrated=coint["cointegrated"], adf_stat=coint["adf_stat"],
        coint_hedge_ratio=coint["hedge_ratio"], half_life=coint["half_life"],
        prices=candidate_prices,
    )


def score_candidate(
    *,
    correlation: float,
    funding_drag_ratio: float,
    funding_std_30d: float,
    spread_bps: float,
    max_funding_std_30d: float,
    max_spread_bps: float,
    hedge_priority: float,
    coint_quality: dict[str, Any],
    match_type: str,
) -> float:
    """Score a candidate using priority-weighted tightness vs profitability."""
    # Tightness from cointegration quality
    cq = 0.0
    if coint_quality.get("cointegrated"):
        adf_norm = clamp((-coint_quality["adf_stat"] - 2.87) / 2.13, 0.0, 1.0)
        hl = max(coint_quality.get("half_life", 168.0), 1.0)
        hl_norm = clamp(1.0 - math.log(hl) / math.log(168), 0.0, 1.0)
        cq = 0.6 * adf_norm + 0.4 * hl_norm
    tightness = max(cq, abs(correlation) * 0.5)

    p = clamp(hedge_priority, 0.0, 1.0)
    w_tight = 0.50 - 0.45 * p
    w_cost = 0.10 + 0.45 * p
    w_stab = 0.20 + 0.10 * p
    w_spread = 0.20 - 0.10 * p

    cost_score = max(0.0, 1.0 - funding_drag_ratio)
    fund_stab = max(0.0, 1.0 - funding_std_30d / max_funding_std_30d) if max_funding_std_30d > 0 else 0.5
    spread_pen = min(spread_bps / max(max_spread_bps, 1.0), 1.0) if max_spread_bps > 0 else 0.0

    score = tightness * w_tight + cost_score * w_cost + fund_stab * w_stab + (1.0 - spread_pen) * w_spread
    if match_type == "direct":
        score += 1.0
    return score


# ── Combo evaluation ──────────────────────────────────────────


def evaluate_combo(
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
    """Evaluate a hedge combo: regression, variance reduction, blowout, net beta."""
    aligned = pd.concat(
        [portfolio_returns.rename("portfolio"), *[c.returns.rename(c.symbol) for c in combo]],
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
    uvar = safe_float(unhedged.var())
    hvar = safe_float(hedged.var())
    var_red = 0.0 if uvar == 0 else 1 - hvar / uvar
    ann_vol_h = annualized_vol(hedged)
    mdd = max_drawdown(hedged)
    rvr = 1.0 if uvar == 0 else ann_vol_h / max(annualized_vol(unhedged), 1e-9)

    legs: list[dict[str, Any]] = []
    ann_drag = total_notional = 0.0

    for c, w in zip(combo, weights, strict=True):
        notional = abs(w) * gross_notional_usd
        if notional < min_leg_notional_usd:
            return None
        side = side_from_weight(float(w))
        leg_cost = side_funding_cost_pct(c.ann_funding_rate, side) * 100
        ann_drag += (notional / max(gross_notional_usd, 1.0)) * leg_cost
        total_notional += notional
        legs.append({
            "symbol": c.symbol, "side": side, "match_type": c.match_type,
            "hedge_ratio": round(float(w), 4), "notional_usd": round(notional, 2),
            "correlation": round(c.correlation, 4), "ann_funding_cost_pct": round(leg_cost, 2),
            "oi_usd": round(c.oi_usd, 2), "volume_usd": round(c.volume_usd, 2),
            "spread_bps": round(c.spread_bps, 2), "score": round(c.hedge_score, 4),
            "cointegrated": c.cointegrated, "adf_stat": c.adf_stat, "half_life": c.half_life,
        })

    net_betas = {sym: beta(hedged, fs) for sym, fs in factor_returns.items()}
    max_nb = max((abs(v) for v in net_betas.values()), default=0.0)
    cai = var_red - ann_drag / 100

    # Blowout score
    blowout_parts: list[float] = []
    for c, leg in zip(combo, legs, strict=True):
        ln = safe_float(leg.get("notional_usd"))
        oi_ok = max(0.0, 1.0 - ln / c.oi_usd / max_position_oi_fraction) if c.oi_usd > 0 and max_position_oi_fraction > 0 else 0.5
        fs_ok = max(0.0, 1.0 - c.funding_std_30d / max_funding_std_30d) if max_funding_std_30d > 0 else 0.5
        sp_ok = max(0.0, 1.0 - c.spread_bps / max_spread_bps) if c.spread_bps > 0 and max_spread_bps > 0 else 0.5
        blowout_parts.append((oi_ok + fs_ok + sp_ok) / 3.0)
    bs = min(blowout_parts) if blowout_parts else 0.5

    return {
        "hedge_id": "+".join(f"{l['symbol']}-{l['side']}" for l in legs),
        "legs": legs,
        "hedged_ann_vol": round(ann_vol_h, 4), "hedged_max_dd": round(mdd, 4),
        "variance_reduction_pct": round(var_red * 100, 2),
        "ann_funding_cost_pct": round(ann_drag, 2),
        "cost_adjusted_improvement": round(cai, 4),
        "blowout_score": round(bs, 4),
        "net_betas": {s: round(v, 4) for s, v in net_betas.items()},
        "net_beta": round(max_nb, 4),
        "residual_vol_ratio": round(rvr, 4),
        "total_notional_usd": round(total_notional, 2),
        "verdict": "pending",
    }


def rank_combos(
    evaluated: list[dict[str, Any]],
    hedge_priority: float = 0.5,
) -> list[dict[str, Any]]:
    """Rank combos: direct matches first, then blend cost vs tightness by priority."""
    p = clamp(hedge_priority, 0.0, 1.0)

    def key(item: dict[str, Any]) -> tuple:
        legs = item.get("legs") or []
        dc = sum(1 for l in legs if l.get("match_type") == "direct")
        cost = safe_float(item.get("ann_funding_cost_pct"))
        vr = safe_float(item.get("variance_reduction_pct"))
        composite = (1.0 - p) * vr + p * (-cost)
        return (-dc, -composite, -safe_float(item.get("blowout_score"), 0.5))

    return sorted(evaluated, key=key)
