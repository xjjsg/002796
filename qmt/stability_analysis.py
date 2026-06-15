"""Parameter and feature stability analysis for the updated V6 data set."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from sz002796.backtest import (
    V6BacktestExecutionStrategy,
    benchmark_all_in,
    benchmark_buy_and_hold,
    max_drawdown,
    seed_initial_position,
)
from sz002796.config import (
    BENCHMARK_TARGET_PCT,
    INITIAL_CAPITAL,
    INITIAL_STRATEGY_TARGET_PCT,
    START_DATE,
    clamp,
)
from sz002796.factors import FactorSnapshot, IntradayFactorCalc
from sz002796.market_data import DATA_DIR, ORDERBOOK_COLS, load_market_data


FEATURE_COLUMNS = [
    "day_vwap_dev",
    "local_vwap_dev",
    "velocity",
    "acceleration",
    "vol_mom",
    "day_return",
    "open_gap",
    "open_return",
    "high_return",
    "pullback_from_high",
    "range_position",
    "below_vwap_ratio",
    "vwap_slope_15m",
    "vwap_slope_30m",
    "local_price_std",
    "local_vwap_z",
    "opening_range_position",
    "consecutive_above_vwap",
    "consecutive_below_vwap",
    "new_high_count_30m",
    "new_low_count_30m",
    "bid_depth",
    "ask_depth",
    "orderbook_imbalance",
]

SCORE_COLUMNS = [
    "score_macro_sell",
    "score_macro_buy",
    "score_local_trim",
    "score_local_cover",
    "score_main_flow_guard",
]

FORWARD_HORIZONS_MINUTES = [5, 15, 30]

PARAMETER_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("baseline", {}),
    ("old_defaults", {"local_enter_score": 0.70, "main_flow_guard_score": 0.60}),
    ("cross_enter_0.20", {"cross_enter_score": 0.20}),
    ("cross_enter_0.30", {"cross_enter_score": 0.30}),
    ("local_enter_0.60", {"local_enter_score": 0.60}),
    ("local_enter_0.70", {"local_enter_score": 0.70}),
    ("local_enter_0.80", {"local_enter_score": 0.80}),
    ("main_flow_guard_0.60", {"main_flow_guard_score": 0.60}),
    ("local_cover_0.75", {"local_cover_enter_score": 0.75}),
    ("local_cover_0.95", {"local_cover_enter_score": 0.95}),
    ("cooldown_30", {"cooldown_minutes": 30}),
    ("cooldown_60", {"cooldown_minutes": 60}),
    ("max_day_trades_2", {"max_day_trades": 2}),
    ("max_day_trades_4", {"max_day_trades": 4}),
    ("main_flow_guard_0.50", {"main_flow_guard_score": 0.50}),
    ("main_flow_guard_0.70", {"main_flow_guard_score": 0.70}),
]

KEY_PARAMETER_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("baseline", {}),
    ("old_defaults", {"local_enter_score": 0.70, "main_flow_guard_score": 0.60}),
    ("cross_enter_0.20", {"cross_enter_score": 0.20}),
    ("cross_enter_0.30", {"cross_enter_score": 0.30}),
    ("local_enter_0.60", {"local_enter_score": 0.60}),
    ("local_enter_0.70", {"local_enter_score": 0.70}),
    ("local_enter_0.80", {"local_enter_score": 0.80}),
    ("main_flow_guard_0.60", {"main_flow_guard_score": 0.60}),
    ("main_flow_guard_0.50", {"main_flow_guard_score": 0.50}),
    ("main_flow_guard_0.70", {"main_flow_guard_score": 0.70}),
]


def _score_macro_sell(f: FactorSnapshot) -> float:
    if not (f.day_return < 0.08 and f.day_vwap_dev > 0.018 and f.velocity > 0 and f.acceleration < 0):
        return 0.0
    dev_score = clamp((f.day_vwap_dev - 0.018) / 0.032)
    acc_score = clamp((-f.acceleration) / 0.008)
    vel_score = clamp(f.velocity / 0.008)
    return clamp(0.45 * dev_score + 0.30 * acc_score + 0.25 * vel_score)


def _score_macro_buy(f: FactorSnapshot) -> float:
    if not (f.day_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0 and f.vol_mom > 1.8):
        return 0.0
    dev_score = clamp((-f.day_vwap_dev - 0.004) / 0.020)
    acc_score = clamp(f.acceleration / 0.008)
    vel_score = clamp(f.velocity / 0.008)
    vol_score = clamp((f.vol_mom - 1.8) / 1.8)
    return clamp(0.35 * dev_score + 0.25 * acc_score + 0.15 * vel_score + 0.25 * vol_score)


def _score_local_trim(f: FactorSnapshot) -> float:
    if not (f.local_vwap_dev > 0.006 and f.acceleration < 0):
        return 0.0
    dev_score = clamp((f.local_vwap_dev - 0.006) / 0.018)
    acc_score = clamp((-f.acceleration) / 0.006)
    return clamp(0.60 * dev_score + 0.40 * acc_score)


def _score_local_cover(f: FactorSnapshot) -> float:
    if not (f.local_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0):
        return 0.0
    dev_score = clamp((-f.local_vwap_dev - 0.004) / 0.016)
    acc_score = clamp(f.acceleration / 0.006)
    vel_score = clamp(f.velocity / 0.006)
    return clamp(0.55 * dev_score + 0.30 * acc_score + 0.15 * vel_score)


def _score_main_flow_guard(f: FactorSnapshot) -> float:
    drop = clamp((-f.day_return - 0.025) / 0.060)
    vwap_break = clamp((-f.day_vwap_dev - 0.012) / 0.035)
    high_pullback = clamp((-f.pullback_from_high - 0.025) / 0.055)
    low_range = clamp((0.35 - f.range_position) / 0.35)
    volume_push = clamp((f.vol_mom - 1.2) / 2.0)
    return clamp(
        0.30 * drop
        + 0.25 * vwap_break
        + 0.25 * high_pullback
        + 0.10 * low_range
        + 0.10 * volume_push
    )


def _source_segment_by_day(df: pd.DataFrame) -> dict[str, str]:
    result: dict[str, str] = {}
    for date, day in df.groupby("date", sort=True):
        if bool(day["is_realtime"].any()):
            segment = "local_orderbook"
        elif len(day) > 1000:
            segment = "qmt_tick_history"
        else:
            segment = "legacy_minute"
        result[str(date)] = segment
    return result


def _tick_from_tuple(row: Any) -> dict[str, Any]:
    tick = {
        "Time": row.dt,
        "dt": row.dt,
        "server_time": row.server_time,
        "local_time_ms": int(float(row.local_time_ms or 0)),
        "Close": float(row.price or 0.0),
        "price": float(row.price or 0.0),
        "open": float(row.open or 0.0),
        "high": float(row.high or 0.0),
        "low": float(row.low or 0.0),
        "prev_close": float(row.prev_close or 0.0),
        "Volume": float(row.cum_volume or 0.0),
        "Amount": float(row.cum_amount or 0.0),
        "cum_volume": float(row.cum_volume or 0.0),
        "cum_amount": float(row.cum_amount or 0.0),
        "tick_vol": float(row.tick_vol or 0.0),
        "tick_amt": float(row.tick_amt or 0.0),
        "_is_realtime": bool(row.is_realtime),
        "_is_tick_history": bool(getattr(row, "is_tick_history", False)),
    }
    for column in ORDERBOOK_COLS:
        tick[column] = float(getattr(row, column, 0.0) or 0.0)
    return tick


def _snapshot_row(row: Any, source_segment: str, snapshot: FactorSnapshot) -> dict[str, Any]:
    data = {
        "date": str(row.date),
        "month": str(row.date)[:7],
        "dt": row.dt,
        "server_time": row.server_time,
        "source_segment": source_segment,
        "is_realtime": bool(row.is_realtime),
        "price": float(row.price),
    }
    for column in FEATURE_COLUMNS:
        data[column] = float(getattr(snapshot, column))
    data.update(
        {
            "score_macro_sell": _score_macro_sell(snapshot),
            "score_macro_buy": _score_macro_buy(snapshot),
            "score_local_trim": _score_local_trim(snapshot),
            "score_local_cover": _score_local_cover(snapshot),
            "score_main_flow_guard": _score_main_flow_guard(snapshot),
        }
    )
    return data


def build_factor_frame(df: pd.DataFrame) -> pd.DataFrame:
    day_source = _source_segment_by_day(df)
    calc = IntradayFactorCalc(local_window=30)
    rows: list[dict[str, Any]] = []
    current_date = None
    intraday = df.loc[(df["server_time"] >= "09:30:00") & (df["server_time"] <= "15:00:00")]

    for row in intraday.itertuples(index=False):
        date = str(row.date)
        is_new_day = date != current_date
        current_date = date
        tick = _tick_from_tuple(row)
        snapshot = calc.update(tick, is_new_day)
        rows.append(_snapshot_row(row, day_source[date], snapshot))

    factor_df = pd.DataFrame(rows)
    factor_df["dt"] = pd.to_datetime(factor_df["dt"])
    factor_df = factor_df.sort_values("dt").reset_index(drop=True)
    return add_forward_returns(factor_df)


def add_forward_returns(factor_df: pd.DataFrame) -> pd.DataFrame:
    result = factor_df.copy()
    result["eod_return"] = np.nan
    for minutes in FORWARD_HORIZONS_MINUTES:
        result[f"fwd_ret_{minutes}m"] = np.nan

    for _, index in result.groupby("date", sort=False).groups.items():
        locs = np.array(list(index), dtype=int)
        day = result.loc[locs].sort_values("dt")
        ordered_locs = day.index.to_numpy()
        prices = day["price"].to_numpy(dtype=float)
        times = day["dt"].astype("int64").to_numpy()
        eod_price = prices[-1]
        result.loc[ordered_locs, "eod_return"] = eod_price / prices - 1.0
        for minutes in FORWARD_HORIZONS_MINUTES:
            horizon_ns = int(minutes * 60 * 1_000_000_000)
            future_pos = np.searchsorted(times, times + horizon_ns, side="left")
            values = np.full(len(prices), np.nan)
            valid = future_pos < len(prices)
            values[valid] = prices[future_pos[valid]] / prices[valid] - 1.0
            result.loc[ordered_locs, f"fwd_ret_{minutes}m"] = values
    return result


def _feature_stats(frame: pd.DataFrame, period_column: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for period, group in frame.groupby(period_column, sort=True):
        for feature in FEATURE_COLUMNS + SCORE_COLUMNS:
            values = pd.to_numeric(group[feature], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if values.empty:
                continue
            rows.append(
                {
                    "period_type": period_column,
                    "period": period,
                    "feature": feature,
                    "count": int(values.size),
                    "mean": float(values.mean()),
                    "std": float(values.std(ddof=0)),
                    "min": float(values.min()),
                    "p05": float(values.quantile(0.05)),
                    "p25": float(values.quantile(0.25)),
                    "median": float(values.median()),
                    "p75": float(values.quantile(0.75)),
                    "p95": float(values.quantile(0.95)),
                    "max": float(values.max()),
                }
            )
    return pd.DataFrame(rows)


def feature_distribution_tables(frame: pd.DataFrame) -> pd.DataFrame:
    full = frame.copy()
    full["full_sample"] = "full"
    return pd.concat(
        [
            _feature_stats(full, "full_sample"),
            _feature_stats(frame, "source_segment"),
            _feature_stats(frame, "month"),
        ],
        ignore_index=True,
    )


def _corr(left: pd.Series, right: pd.Series) -> float | None:
    data = pd.DataFrame({"left": left, "right": right}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 100:
        return None
    if data["left"].std(ddof=0) <= 0 or data["right"].std(ddof=0) <= 0:
        return None
    value = data["left"].corr(data["right"])
    if value is None or not math.isfinite(float(value)):
        return None
    return float(value)


def feature_ic_tables(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    targets = [f"fwd_ret_{minutes}m" for minutes in FORWARD_HORIZONS_MINUTES] + ["eod_return"]
    rows: list[dict[str, Any]] = []
    scoped = [("full", "full", frame)]
    scoped.extend((("source_segment", str(k), g) for k, g in frame.groupby("source_segment", sort=True)))
    scoped.extend((("month", str(k), g) for k, g in frame.groupby("month", sort=True)))

    for period_type, period, group in scoped:
        for feature in FEATURE_COLUMNS + SCORE_COLUMNS:
            for target in targets:
                value = _corr(group[feature], group[target])
                if value is None:
                    continue
                rows.append(
                    {
                        "period_type": period_type,
                        "period": period,
                        "feature": feature,
                        "target": target,
                        "ic": value,
                        "count": int(pd.DataFrame({"x": group[feature], "y": group[target]}).dropna().shape[0]),
                    }
                )

    ic_df = pd.DataFrame(rows)
    summary_rows: list[dict[str, Any]] = []
    source_ic = ic_df.loc[ic_df["period_type"] == "source_segment"]
    for (feature, target), group in source_ic.groupby(["feature", "target"], sort=True):
        values = group["ic"].astype(float)
        if values.empty:
            continue
        full_match = ic_df.loc[
            (ic_df["period_type"] == "full")
            & (ic_df["feature"] == feature)
            & (ic_df["target"] == target),
            "ic",
        ]
        full_ic = float(full_match.iloc[0]) if not full_match.empty else None
        same_sign_rate = None
        if full_ic is not None and abs(full_ic) > 1e-12:
            same_sign_rate = float(((values * full_ic) > 0).mean())
        summary_rows.append(
            {
                "feature": feature,
                "target": target,
                "full_ic": full_ic,
                "source_period_count": int(len(values)),
                "source_ic_mean": float(values.mean()),
                "source_ic_std": float(values.std(ddof=0)),
                "source_ic_min": float(values.min()),
                "source_ic_max": float(values.max()),
                "same_sign_rate_vs_full": same_sign_rate,
            }
        )
    return ic_df, pd.DataFrame(summary_rows)


def _psi(expected: pd.Series, actual: pd.Series) -> float | None:
    expected = pd.to_numeric(expected, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    actual = pd.to_numeric(actual, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(expected) < 100 or len(actual) < 100:
        return None
    quantiles = expected.quantile(np.linspace(0.0, 1.0, 11)).drop_duplicates().to_numpy()
    if len(quantiles) < 4:
        return None
    quantiles[0] = -np.inf
    quantiles[-1] = np.inf
    expected_counts = pd.cut(expected, quantiles, include_lowest=True).value_counts(sort=False)
    actual_counts = pd.cut(actual, quantiles, include_lowest=True).value_counts(sort=False)
    expected_pct = expected_counts / expected_counts.sum()
    actual_pct = actual_counts / actual_counts.sum()
    eps = 1e-6
    value = ((actual_pct + eps) - (expected_pct + eps)) * np.log((actual_pct + eps) / (expected_pct + eps))
    return float(value.sum())


def psi_table(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for feature in FEATURE_COLUMNS + SCORE_COLUMNS:
        expected = frame[feature]
        for period, group in frame.groupby("source_segment", sort=True):
            value = _psi(expected, group[feature])
            if value is None:
                continue
            rows.append({"feature": feature, "period_type": "source_segment", "period": period, "psi_vs_full": value})
        for period, group in frame.groupby("month", sort=True):
            value = _psi(expected, group[feature])
            if value is None:
                continue
            rows.append({"feature": feature, "period_type": "month", "period": period, "psi_vs_full": value})
    return pd.DataFrame(rows)


def score_threshold_tables(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    specs = {
        "score_macro_sell": ("sell", [0.20, 0.25, 0.30, 0.35, 0.40]),
        "score_macro_buy": ("buy", [0.20, 0.25, 0.30, 0.35, 0.40]),
        "score_local_trim": ("sell", [0.60, 0.70, 0.80, 0.90]),
        "score_local_cover": ("buy", [0.75, 0.85, 0.95]),
        "score_main_flow_guard": ("sell", [0.50, 0.60, 0.70]),
    }
    scopes = [("full", "full", frame)]
    scopes.extend((("source_segment", str(k), g) for k, g in frame.groupby("source_segment", sort=True)))
    scopes.extend((("month", str(k), g) for k, g in frame.groupby("month", sort=True)))

    rows: list[dict[str, Any]] = []
    for period_type, period, group in scopes:
        for score, (direction, thresholds) in specs.items():
            sign = -1.0 if direction == "sell" else 1.0
            for threshold in thresholds:
                hits = group.loc[group[score] >= threshold].copy()
                edge_30m = sign * pd.to_numeric(hits["fwd_ret_30m"], errors="coerce")
                edge_eod = sign * pd.to_numeric(hits["eod_return"], errors="coerce")
                edge_30m = edge_30m.replace([np.inf, -np.inf], np.nan).dropna()
                edge_eod = edge_eod.replace([np.inf, -np.inf], np.nan).dropna()
                rows.append(
                    {
                        "period_type": period_type,
                        "period": period,
                        "score": score,
                        "direction": direction,
                        "threshold": threshold,
                        "sample_count": int(len(group)),
                        "hit_count": int(len(hits)),
                        "hit_rate": float(len(hits) / len(group)) if len(group) else 0.0,
                        "edge_30m_mean": float(edge_30m.mean()) if not edge_30m.empty else None,
                        "edge_30m_median": float(edge_30m.median()) if not edge_30m.empty else None,
                        "edge_30m_win_rate": float((edge_30m > 0).mean()) if not edge_30m.empty else None,
                        "edge_eod_mean": float(edge_eod.mean()) if not edge_eod.empty else None,
                        "edge_eod_median": float(edge_eod.median()) if not edge_eod.empty else None,
                        "edge_eod_win_rate": float((edge_eod > 0).mean()) if not edge_eod.empty else None,
                    }
                )
    detail = pd.DataFrame(rows)
    source = detail.loc[detail["period_type"] == "source_segment"].copy()
    summary_rows: list[dict[str, Any]] = []
    for (score, threshold), group in source.groupby(["score", "threshold"], sort=True):
        edge = pd.to_numeric(group["edge_30m_mean"], errors="coerce").dropna()
        hits = pd.to_numeric(group["hit_count"], errors="coerce").fillna(0)
        if edge.empty:
            continue
        full = detail.loc[
            (detail["period_type"] == "full")
            & (detail["score"] == score)
            & (detail["threshold"] == threshold)
        ]
        summary_rows.append(
            {
                "score": score,
                "threshold": float(threshold),
                "full_hit_count": int(full["hit_count"].iloc[0]) if not full.empty else None,
                "full_hit_rate": float(full["hit_rate"].iloc[0]) if not full.empty else None,
                "full_edge_30m_mean": float(full["edge_30m_mean"].iloc[0]) if not full.empty and pd.notna(full["edge_30m_mean"].iloc[0]) else None,
                "source_period_count": int(len(group)),
                "source_total_hits": int(hits.sum()),
                "source_edge_30m_mean": float(edge.mean()),
                "source_edge_30m_std": float(edge.std(ddof=0)),
                "positive_source_edge_rate": float((edge > 0).mean()),
                "min_source_edge_30m": float(edge.min()),
            }
        )
    return detail, pd.DataFrame(summary_rows)


def _slice(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    result = df
    if start:
        result = result.loc[result["date"].astype(str) >= start]
    if end:
        result = result.loc[result["date"].astype(str) <= end]
    return result.copy()


def _prepare_backtest_frame(
    data_dir: str | Path = DATA_DIR,
    start_date: str = START_DATE,
    end_date: str | None = None,
) -> pd.DataFrame:
    bundle = load_market_data(start_date=start_date, end_date=end_date, data_dir=data_dir)
    df = bundle.frame.copy()
    df["date"] = df["date"].astype(str)
    df["month"] = df["date"].str.slice(0, 7)
    df["source_segment"] = df["date"].map(_source_segment_by_day(df))
    return df


def _run_strategy(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    if df.empty:
        raise ValueError("empty backtest frame")
    first_row = df.iloc[0]
    last_row = df.iloc[-1]
    first_price = float(first_row["price"])
    last_price = float(last_row["price"])

    strategy = V6BacktestExecutionStrategy(initial_capital=INITIAL_CAPITAL, **params)
    seed_initial_position(strategy, first_row, INITIAL_STRATEGY_TARGET_PCT)
    benchmark = benchmark_buy_and_hold(INITIAL_CAPITAL, first_price, last_price, BENCHMARK_TARGET_PCT)
    full_hold = benchmark_all_in(INITIAL_CAPITAL, first_price, last_price)

    equity_curve: list[float] = []
    for row in df.itertuples(index=False):
        tick = _tick_from_tuple(row)
        strategy.on_tick(tick)
        equity_curve.append(strategy.total_asset(float(row.price)))

    final_asset = equity_curve[-1]
    benchmark_final = benchmark.cash_after_buy + benchmark.buy_shares * last_price
    full_hold_final = full_hold.cash_after_buy + full_hold.buy_shares * last_price
    trade_amount = sum(float(record["amount"]) for record in strategy.execution_records)
    return {
        "start_time": str(first_row["dt"]),
        "end_time": str(last_row["dt"]),
        "rows": int(len(df)),
        "final_asset": float(final_asset),
        "return": float(final_asset / INITIAL_CAPITAL - 1.0),
        "benchmark_70pct_return": float(benchmark_final / INITIAL_CAPITAL - 1.0),
        "full_hold_return": float(full_hold_final / INITIAL_CAPITAL - 1.0),
        "alpha_vs_70pct": float(final_asset / INITIAL_CAPITAL - benchmark_final / INITIAL_CAPITAL),
        "alpha_vs_full_hold": float(final_asset / INITIAL_CAPITAL - full_hold_final / INITIAL_CAPITAL),
        "max_drawdown": float(max_drawdown(equity_curve)),
        "trade_count": int(len(strategy.execution_records)),
        "turnover": float(trade_amount / INITIAL_CAPITAL),
        "final_position_pct": float(strategy.current_position_pct(last_price)),
        "orderbook_fallback_count": int(strategy.orderbook_fallback_count),
    }


def _variant_list(variant_set: str) -> list[tuple[str, dict[str, Any]]]:
    if variant_set == "baseline":
        return [("baseline", {})]
    if variant_set == "key":
        return KEY_PARAMETER_VARIANTS
    return PARAMETER_VARIANTS


def _backtest_task(task: dict[str, Any]) -> dict[str, Any]:
    df = _prepare_backtest_frame(task["data_dir"], task["start_date"], task.get("end_date") or None)
    period_type = task["period_type"]
    period = task["period"]
    if period_type == "source_segment":
        df = df.loc[df["source_segment"] == period].copy()
    elif period_type == "month":
        df = df.loc[df["month"] == period].copy()
    result = _run_strategy(df, task["params"])
    result.update(
        {
            "period_type": period_type,
            "period": period,
            "variant": task["variant"],
            "params": json.dumps(task["params"], ensure_ascii=False, sort_keys=True),
        }
    )
    return result


def backtest_parameter_tables(
    df: pd.DataFrame,
    *,
    data_dir: str | Path = DATA_DIR,
    start_date: str = START_DATE,
    end_date: str | None = None,
    variant_set: str = "full",
    workers: int = 1,
) -> pd.DataFrame:
    variants = _variant_list(variant_set)
    tasks: list[dict[str, Any]] = []
    for variant_name, params in variants:
        tasks.append(
            {
                "data_dir": str(data_dir),
                "start_date": start_date,
                "end_date": end_date,
                "period_type": "full",
                "period": "full",
                "variant": variant_name,
                "params": params,
            }
        )
    for segment in sorted(df["source_segment"].dropna().unique()):
        tasks.append(
            {
                "data_dir": str(data_dir),
                "start_date": start_date,
                "end_date": end_date,
                "period_type": "source_segment",
                "period": str(segment),
                "variant": "baseline",
                "params": {},
            }
        )
    for month in sorted(df["month"].dropna().unique()):
        tasks.append(
            {
                "data_dir": str(data_dir),
                "start_date": start_date,
                "end_date": end_date,
                "period_type": "month",
                "period": str(month),
                "variant": "baseline",
                "params": {},
            }
        )

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            return pd.DataFrame(list(executor.map(_backtest_task, tasks)))

    rows: list[dict[str, Any]] = []
    scopes: list[tuple[str, str, pd.DataFrame, list[tuple[str, dict[str, Any]]]]] = [
        ("full", "full", df, variants),
    ]
    for segment, group in df.groupby("source_segment", sort=True):
        scopes.append(("source_segment", str(segment), group.copy(), [("baseline", {})]))
    for month, group in df.groupby("month", sort=True):
        scopes.append(("month", str(month), group.copy(), [("baseline", {})]))

    for period_type, period, frame, variants in scopes:
        for variant_name, params in variants:
            result = _run_strategy(frame, params)
            result.update(
                {
                    "period_type": period_type,
                    "period": period,
                    "variant": variant_name,
                    "params": json.dumps(params, ensure_ascii=False, sort_keys=True),
                }
            )
            rows.append(result)
    return pd.DataFrame(rows)


def run_analysis(
    *,
    start_date: str = START_DATE,
    end_date: str | None = None,
    data_dir: str | Path = DATA_DIR,
    output_dir: str | Path | None = None,
    skip_backtests: bool = False,
    backtests_only: bool = False,
    variant_set: str = "full",
    workers: int = 1,
) -> dict[str, Any]:
    bundle = load_market_data(start_date=start_date, end_date=end_date, data_dir=data_dir)
    df = bundle.frame.copy()
    df["date"] = df["date"].astype(str)
    df["month"] = df["date"].str.slice(0, 7)
    source_by_day = _source_segment_by_day(df)
    df["source_segment"] = df["date"].map(source_by_day)

    output_path = Path(output_dir) if output_dir else Path("qmt") / "analysis" / f"stability_{datetime.now():%Y%m%d_%H%M%S}"
    output_path.mkdir(parents=True, exist_ok=True)

    if backtests_only:
        backtests = backtest_parameter_tables(
            df,
            data_dir=data_dir,
            start_date=start_date,
            end_date=end_date,
            variant_set=variant_set,
            workers=max(1, workers),
        )
        backtest_path = output_path / "parameter_backtests.csv"
        backtests.to_csv(backtest_path, index=False, encoding="utf-8-sig")
        summary_path = output_path / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        summary.update(
            {
                "start_date": start_date,
                "end_date": end_date or str(df["date"].max()),
                "data_rows": int(len(df)),
                "data_files": int(len(bundle.files)),
                "source_segments": {str(k): int(v) for k, v in df["source_segment"].value_counts().sort_index().items()},
                "warnings": bundle.warnings,
            }
        )
        outputs = dict(summary.get("outputs") or {})
        outputs["parameter_backtests_csv"] = str(backtest_path)
        outputs["summary_json"] = str(summary_path)
        summary["outputs"] = outputs
        full = backtests.loc[(backtests["period_type"] == "full") & (backtests["variant"] == "baseline")]
        if not full.empty:
            summary["baseline_full_backtest"] = full.iloc[0].to_dict()
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return summary

    factor_frame = build_factor_frame(df)
    factor_path = output_path / "factor_samples.csv"
    factor_frame.to_csv(factor_path, index=False, encoding="utf-8-sig")

    distribution = feature_distribution_tables(factor_frame)
    distribution_path = output_path / "feature_distribution.csv"
    distribution.to_csv(distribution_path, index=False, encoding="utf-8-sig")

    ic_detail, ic_summary = feature_ic_tables(factor_frame)
    ic_detail_path = output_path / "feature_ic_detail.csv"
    ic_summary_path = output_path / "feature_ic_summary.csv"
    ic_detail.to_csv(ic_detail_path, index=False, encoding="utf-8-sig")
    ic_summary.to_csv(ic_summary_path, index=False, encoding="utf-8-sig")

    psi = psi_table(factor_frame)
    psi_path = output_path / "feature_psi.csv"
    psi.to_csv(psi_path, index=False, encoding="utf-8-sig")

    threshold_detail, threshold_summary = score_threshold_tables(factor_frame)
    threshold_detail_path = output_path / "score_threshold_detail.csv"
    threshold_summary_path = output_path / "score_threshold_summary.csv"
    threshold_detail.to_csv(threshold_detail_path, index=False, encoding="utf-8-sig")
    threshold_summary.to_csv(threshold_summary_path, index=False, encoding="utf-8-sig")

    backtest_path = None
    backtests = pd.DataFrame()
    if not skip_backtests:
        backtests = backtest_parameter_tables(
            df,
            data_dir=data_dir,
            start_date=start_date,
            end_date=end_date,
            variant_set=variant_set,
            workers=max(1, workers),
        )
        backtest_path = output_path / "parameter_backtests.csv"
        backtests.to_csv(backtest_path, index=False, encoding="utf-8-sig")

    summary = {
        "start_date": start_date,
        "end_date": end_date or str(df["date"].max()),
        "data_rows": int(len(df)),
        "factor_rows": int(len(factor_frame)),
        "data_files": int(len(bundle.files)),
        "source_segments": {str(k): int(v) for k, v in df["source_segment"].value_counts().sort_index().items()},
        "warnings": bundle.warnings,
        "outputs": {
            "factor_samples_csv": str(factor_path),
            "feature_distribution_csv": str(distribution_path),
            "feature_ic_detail_csv": str(ic_detail_path),
            "feature_ic_summary_csv": str(ic_summary_path),
            "feature_psi_csv": str(psi_path),
            "score_threshold_detail_csv": str(threshold_detail_path),
            "score_threshold_summary_csv": str(threshold_summary_path),
            "parameter_backtests_csv": str(backtest_path) if backtest_path else None,
            "summary_json": str(output_path / "summary.json"),
        },
    }
    if not backtests.empty:
        full = backtests.loc[(backtests["period_type"] == "full") & (backtests["variant"] == "baseline")]
        if not full.empty:
            summary["baseline_full_backtest"] = full.iloc[0].to_dict()

    summary_path = output_path / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V6 feature and parameter stability analysis.")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--skip-backtests", action="store_true")
    parser.add_argument("--backtests-only", action="store_true")
    parser.add_argument("--variant-set", choices=["baseline", "key", "full"], default="full")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    summary = run_analysis(
        start_date=args.start_date,
        end_date=args.end_date or None,
        data_dir=args.data_dir,
        output_dir=args.output_dir or None,
        skip_backtests=args.skip_backtests,
        backtests_only=args.backtests_only,
        variant_set=args.variant_set,
        workers=args.workers,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
