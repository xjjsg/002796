"""Walk-forward validation for anti-overfit V6 strategy changes.

This script is a diagnostics harness, not a production signal path. It compares
fixed variants on chronological validation folds and writes the evidence needed
before any candidate can be considered for default strategy use.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sz002796.backtest import (
    V6BacktestExecutionStrategy,
    benchmark_buy_and_hold,
    max_drawdown,
    seed_initial_position,
)
from sz002796.config import (
    BENCHMARK_TARGET_PCT,
    INITIAL_CAPITAL,
    INITIAL_STRATEGY_TARGET_PCT,
    PROJECT_ROOT,
    START_DATE,
)
from sz002796.market_data import DATA_DIR, ORDERBOOK_COLS, load_market_data


@dataclass(frozen=True)
class ValidationFold:
    name: str
    dev_start: str
    dev_end: str
    val_start: str
    val_end: str


@dataclass
class RunResult:
    fold: ValidationFold
    variant: str
    params: dict[str, Any]
    slippage_ticks: int
    equity: pd.DataFrame
    trades: pd.DataFrame
    orderbook_fallback_count: int
    limit_skip_count: int


DEFAULT_FOLDS: dict[str, ValidationFold] = {
    "mar": ValidationFold("jan_feb_to_mar", START_DATE, "2026-02-27", "2026-03-01", "2026-03-31"),
    "apr": ValidationFold("jan_mar_to_apr", START_DATE, "2026-03-31", "2026-04-01", "2026-04-30"),
    "may": ValidationFold("jan_apr_to_may", START_DATE, "2026-04-30", "2026-05-01", "2026-05-31"),
    "jun": ValidationFold("jan_may_to_jun", START_DATE, "2026-05-29", "2026-06-01", "2026-06-30"),
}

VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "no_local_t": {"enable_local_t": False},
    "legacy_local_t": {"trend_local_t_mode": "legacy"},
    "legacy_refill_lock": {
        "trend_local_t_mode": "legacy",
        "protect_local_short_floor_refill": True,
    },
    "cooldown_60": {"cooldown_minutes": 60},
}

LEGACY_COMPARE_VARIANTS: dict[str, dict[str, Any]] = {
    "baseline_directional": {},
    "legacy_local_t": VARIANTS["legacy_local_t"],
}

LEGACY_COMPARE_WINDOWS: dict[str, tuple[str | None, str | None]] = {
    "full_to_2026_06_15": (None, "2026-06-15"),
    "full_to_latest": (None, None),
    "may_validation": ("2026-05-01", "2026-05-31"),
    "jun_validation": ("2026-06-01", "2026-06-30"),
    "recent_fixed": ("2026-05-26", "2026-06-15"),
    "rolling_latest_15": ("ROLLING_15", None),
}

PRODUCTION_SCAN_FILES = [
    "sz002796/strategy_v6.py",
    "sz002796/factors.py",
    "sz002796/regime.py",
    "sz002796/position.py",
    "sz002796/backtest.py",
    "sz002796/execution.py",
]

FORBIDDEN_LOOKAHEAD_PATTERNS = [
    re.compile(r"\bshift\s*\(\s*-\d+", re.IGNORECASE),
    re.compile(r"\bcenter\s*=\s*True\b", re.IGNORECASE),
    re.compile(r"\boracle\b", re.IGNORECASE),
    re.compile(r"\bfuture[_a-z0-9]*\b", re.IGNORECASE),
    re.compile(r"\bforward[_a-z0-9]*\b", re.IGNORECASE),
    re.compile(r"\beod[_a-z0-9]*\b", re.IGNORECASE),
]

FLOOR_REFILL_REASONS = {
    "V6 floor refill",
    "V6 regime floor restore",
    "V6 local short hard-floor refill",
    "V6 local short hard-floor restore",
}

MODULE_REASON_MAP: dict[str, tuple[str, str]] = {
    "V6 cross-day add": ("3_cross_day_rebalance", "cross_day_rebalance"),
    "V6 cross-day reduce": ("3_cross_day_rebalance", "cross_day_rebalance"),
    "V6 local trim": ("4_intraday_t", "intraday_t"),
    "V6 local short entry": ("4_intraday_t", "intraday_t"),
    "V6 local short cover": ("4_intraday_t", "intraday_t"),
    "V6 local long entry": ("4_intraday_t", "intraday_t"),
    "V6 local long exit": ("4_intraday_t", "intraday_t"),
    "V6 local long profit exit": ("4_intraday_t", "intraday_t"),
    "V6 local long stop exit": ("4_intraday_t", "intraday_t"),
    "V6 local long time exit": ("4_intraday_t", "intraday_t"),
    "V6 local long trim exit": ("4_intraday_t", "intraday_t"),
    "V6 floor refill": ("5_trend_guard_position_correction", "trend_guard_position_correction"),
    "V6 regime floor restore": ("5_trend_guard_position_correction", "trend_guard_position_correction"),
    "V6 regime cap reduce": ("5_trend_guard_position_correction", "trend_guard_position_correction"),
    "V6 local short hard-floor refill": ("5_trend_guard_position_correction", "trend_guard_position_correction"),
    "V6 local short hard-floor restore": ("5_trend_guard_position_correction", "trend_guard_position_correction"),
    "V6 main flow guard reduce": ("6_main_flow_guard", "main_flow_guard"),
}


def _comma_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_int_list(value: str) -> list[int]:
    return [int(part) for part in _comma_list(value)]


def _timestamp_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _normalise_trade_frame(trades: pd.DataFrame) -> pd.DataFrame:
    result = trades.copy()
    if result.empty:
        result = pd.DataFrame(
            columns=[
                "timestamp",
                "side",
                "price",
                "shares",
                "amount",
                "commission",
                "stamp_tax",
                "cash_after",
                "position_shares",
                "asset_after",
                "position_pct_after",
                "target_pct",
                "mode",
                "reason",
                "detail",
                "execution_source",
                "orderbook_fallback",
            ]
        )
    if "timestamp" not in result.columns and "time" in result.columns:
        result["timestamp"] = result["time"]
    if "time" not in result.columns and "timestamp" in result.columns:
        result["time"] = result["timestamp"]
    if "asset" not in result.columns and "asset_after" in result.columns:
        result["asset"] = result["asset_after"]
    if "position_pct" not in result.columns and "position_pct_after" in result.columns:
        result["position_pct"] = result["position_pct_after"]
    if "orderbook_fallback" not in result.columns:
        result["orderbook_fallback"] = False
    return result


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


def _prepare_frame(data_dir: str | Path, start_date: str, end_date: str) -> pd.DataFrame:
    bundle = load_market_data(start_date=start_date, end_date=end_date, data_dir=data_dir)
    df = bundle.frame.copy()
    df["date"] = df["date"].astype(str)
    df["month"] = df["date"].str.slice(0, 7)
    df["source_segment"] = df["date"].map(_source_segment_by_day(df))
    return df


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


def scan_production_for_lookahead(project_root: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for rel_path in PRODUCTION_SCAN_FILES:
        path = project_root / rel_path
        if not path.exists():
            rows.append({"file": rel_path, "line": 0, "pattern": "missing_file", "text": ""})
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            if line.strip().startswith("from __future__ import"):
                continue
            for pattern in FORBIDDEN_LOOKAHEAD_PATTERNS:
                if pattern.search(line):
                    rows.append(
                        {
                            "file": rel_path,
                            "line": line_no,
                            "pattern": pattern.pattern,
                            "text": line.strip()[:240],
                        }
                    )
    return pd.DataFrame(rows, columns=["file", "line", "pattern", "text"])


def run_variant(
    df: pd.DataFrame,
    end_date: str,
    variant: str,
    params: dict[str, Any],
    slippage_ticks: int,
) -> RunResult:
    run_df = df.loc[(df["date"] >= START_DATE) & (df["date"] <= end_date)].copy()
    if run_df.empty:
        raise ValueError(f"empty frame through end_date={end_date}")

    first_row = run_df.iloc[0]
    first_price = float(first_row["price"])
    strategy_params = dict(params)
    strategy = V6BacktestExecutionStrategy(
        initial_capital=INITIAL_CAPITAL,
        fallback_slippage_ticks=slippage_ticks,
        **strategy_params,
    )
    seed_initial_position(strategy, first_row, INITIAL_STRATEGY_TARGET_PCT)
    benchmark = benchmark_buy_and_hold(INITIAL_CAPITAL, first_price, first_price, BENCHMARK_TARGET_PCT)

    equity_rows: list[dict[str, Any]] = []
    for row in run_df.itertuples(index=False):
        tick = _tick_from_tuple(row)
        strategy.on_tick(tick)
        mark_price = float(row.price)
        equity_rows.append(
            {
                "date": str(row.date),
                "month": str(row.month),
                "dt": _timestamp_text(row.dt),
                "price": mark_price,
                "asset": float(strategy.total_asset(mark_price)),
                "benchmark_asset": float(benchmark.cash_after_buy + benchmark.buy_shares * mark_price),
                "source_segment": str(row.source_segment),
                "orderbook_fallback_count_total": int(strategy.orderbook_fallback_count),
                "limit_skip_count_total": int(strategy.limit_up_buy_skip_count + strategy.limit_down_sell_skip_count),
            }
        )

    trades = _normalise_trade_frame(pd.DataFrame(strategy.execution_records))
    trades["date"] = trades["time"].astype(str).str.slice(0, 10)
    trades["month"] = trades["date"].str.slice(0, 7)
    source_by_day = dict(zip(run_df["date"], run_df["source_segment"]))
    trades["source_segment"] = trades["date"].map(source_by_day).fillna("unknown")

    return RunResult(
        fold=ValidationFold("full_run", START_DATE, end_date, START_DATE, end_date),
        variant=variant,
        params=strategy_params,
        slippage_ticks=slippage_ticks,
        equity=pd.DataFrame(equity_rows),
        trades=trades,
        orderbook_fallback_count=int(strategy.orderbook_fallback_count),
        limit_skip_count=int(strategy.limit_up_buy_skip_count + strategy.limit_down_sell_skip_count),
    )


def bind_fold(result: RunResult, fold: ValidationFold) -> RunResult:
    return RunResult(
        fold=fold,
        variant=result.variant,
        params=result.params,
        slippage_ticks=result.slippage_ticks,
        equity=result.equity,
        trades=result.trades,
        orderbook_fallback_count=result.orderbook_fallback_count,
        limit_skip_count=result.limit_skip_count,
    )


def _validation_slice(result: RunResult) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    eq = result.equity
    validation = eq.loc[(eq["date"] >= result.fold.val_start) & (eq["date"] <= result.fold.val_end)].copy()
    if validation.empty:
        raise ValueError(f"no validation rows for fold={result.fold.name}")
    before = eq.loc[eq["date"] < result.fold.val_start]
    start = before.iloc[-1] if not before.empty else validation.iloc[0]
    end = validation.iloc[-1]
    return validation, start, end


def validation_metrics(result: RunResult) -> dict[str, Any]:
    validation, start, end = _validation_slice(result)
    trades = result.trades
    val_trades = trades.loc[(trades["date"] >= result.fold.val_start) & (trades["date"] <= result.fold.val_end)].copy()
    start_asset = float(start["asset"])
    start_benchmark = float(start["benchmark_asset"])
    end_asset = float(end["asset"])
    end_benchmark = float(end["benchmark_asset"])
    strategy_pnl = end_asset - start_asset
    benchmark_pnl = end_benchmark - start_benchmark
    alpha_amount = strategy_pnl - benchmark_pnl
    trade_amount = float(pd.to_numeric(val_trades.get("amount", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum())
    fallback_count = int(val_trades.get("orderbook_fallback", pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
    start_limit_skips = int(start.get("limit_skip_count_total", 0))
    end_limit_skips = int(end.get("limit_skip_count_total", result.limit_skip_count))
    limit_skip_count = max(0, end_limit_skips - start_limit_skips)
    return {
        "fold": result.fold.name,
        "dev_start": result.fold.dev_start,
        "dev_end": result.fold.dev_end,
        "validation_start": result.fold.val_start,
        "validation_end": result.fold.val_end,
        "actual_validation_start": validation.iloc[0]["date"],
        "actual_validation_end": validation.iloc[-1]["date"],
        "variant": result.variant,
        "params": json.dumps(result.params, ensure_ascii=False, sort_keys=True),
        "fallback_slippage_ticks": result.slippage_ticks,
        "start_asset": round(start_asset, 4),
        "end_asset": round(end_asset, 4),
        "strategy_pnl": round(strategy_pnl, 4),
        "benchmark_pnl": round(benchmark_pnl, 4),
        "alpha_amount": round(alpha_amount, 4),
        "strategy_return": round(end_asset / start_asset - 1.0, 8) if start_asset else 0.0,
        "benchmark_return": round(end_benchmark / start_benchmark - 1.0, 8) if start_benchmark else 0.0,
        "alpha_return": round((end_asset / start_asset - 1.0) - (end_benchmark / start_benchmark - 1.0), 8)
        if start_asset and start_benchmark
        else 0.0,
        "max_drawdown": round(max_drawdown([start_asset] + validation["asset"].astype(float).tolist()), 8),
        "trade_count": int(len(val_trades)),
        "turnover": round(trade_amount / start_asset, 8) if start_asset else 0.0,
        "fallback_count": fallback_count,
        "limit_skip_count": limit_skip_count,
    }


def daily_validation_metrics(result: RunResult) -> pd.DataFrame:
    eq = result.equity
    rows: list[dict[str, Any]] = []
    validation_days = sorted(eq.loc[(eq["date"] >= result.fold.val_start) & (eq["date"] <= result.fold.val_end), "date"].unique())
    for date in validation_days:
        day = eq.loc[eq["date"] == date]
        if day.empty:
            continue
        before = eq.loc[eq["date"] < date]
        start = before.iloc[-1] if not before.empty else day.iloc[0]
        end = day.iloc[-1]
        strategy_pnl = float(end["asset"]) - float(start["asset"])
        benchmark_pnl = float(end["benchmark_asset"]) - float(start["benchmark_asset"])
        rows.append(
            {
                "fold": result.fold.name,
                "variant": result.variant,
                "fallback_slippage_ticks": result.slippage_ticks,
                "date": date,
                "month": str(date)[:7],
                "source_segment": str(end["source_segment"]),
                "strategy_pnl": round(strategy_pnl, 4),
                "benchmark_pnl": round(benchmark_pnl, 4),
                "alpha_amount": round(strategy_pnl - benchmark_pnl, 4),
                "end_asset": round(float(end["asset"]), 4),
                "end_benchmark_asset": round(float(end["benchmark_asset"]), 4),
            }
        )
    return pd.DataFrame(rows)


def reason_attribution(result: RunResult) -> pd.DataFrame:
    trades = result.trades.loc[
        (result.trades["date"] >= result.fold.val_start) & (result.trades["date"] <= result.fold.val_end)
    ].copy()
    base_cols = [
        "fold",
        "variant",
        "fallback_slippage_ticks",
        "reason",
        "side",
        "trade_count",
        "shares",
        "amount",
        "fallback_count",
    ]
    if trades.empty:
        return pd.DataFrame(columns=base_cols)
    trades["amount"] = pd.to_numeric(trades["amount"], errors="coerce").fillna(0.0)
    trades["shares"] = pd.to_numeric(trades["shares"], errors="coerce").fillna(0).astype(int)
    trades["orderbook_fallback"] = trades["orderbook_fallback"].fillna(False).astype(bool)
    grouped = (
        trades.groupby(["reason", "side"], dropna=False)
        .agg(
            trade_count=("time", "count"),
            shares=("shares", "sum"),
            amount=("amount", "sum"),
            fallback_count=("orderbook_fallback", "sum"),
        )
        .reset_index()
    )
    grouped.insert(0, "fallback_slippage_ticks", result.slippage_ticks)
    grouped.insert(0, "variant", result.variant)
    grouped.insert(0, "fold", result.fold.name)
    grouped["amount"] = grouped["amount"].round(4)
    return grouped[base_cols]


def source_attribution(daily: pd.DataFrame, result: RunResult) -> pd.DataFrame:
    trades = result.trades.loc[
        (result.trades["date"] >= result.fold.val_start) & (result.trades["date"] <= result.fold.val_end)
    ].copy()
    trade_group = pd.DataFrame(
        columns=["source_segment", "trade_count", "trade_amount", "fallback_count"]
    )
    if not trades.empty:
        trades["amount"] = pd.to_numeric(trades["amount"], errors="coerce").fillna(0.0)
        trades["orderbook_fallback"] = trades["orderbook_fallback"].fillna(False).astype(bool)
        trade_group = (
            trades.groupby("source_segment")
            .agg(
                trade_count=("time", "count"),
                trade_amount=("amount", "sum"),
                fallback_count=("orderbook_fallback", "sum"),
            )
            .reset_index()
        )
    if daily.empty:
        return pd.DataFrame()
    grouped = (
        daily.groupby("source_segment")
        .agg(
            days=("date", "nunique"),
            strategy_pnl=("strategy_pnl", "sum"),
            benchmark_pnl=("benchmark_pnl", "sum"),
            alpha_amount=("alpha_amount", "sum"),
        )
        .reset_index()
    )
    grouped = grouped.merge(trade_group, on="source_segment", how="left")
    grouped[["trade_count", "trade_amount", "fallback_count"]] = grouped[
        ["trade_count", "trade_amount", "fallback_count"]
    ].fillna(0)
    grouped.insert(0, "fallback_slippage_ticks", result.slippage_ticks)
    grouped.insert(0, "variant", result.variant)
    grouped.insert(0, "fold", result.fold.name)
    for column in ["strategy_pnl", "benchmark_pnl", "alpha_amount", "trade_amount"]:
        grouped[column] = grouped[column].astype(float).round(4)
    return grouped


def floor_refill_conflicts(result: RunResult) -> pd.DataFrame:
    trades = result.trades.loc[
        (result.trades["date"] >= result.fold.val_start) & (result.trades["date"] <= result.fold.val_end)
    ].copy()
    rows: list[dict[str, Any]] = []
    if trades.empty:
        return pd.DataFrame(
            columns=[
                "fold",
                "variant",
                "fallback_slippage_ticks",
                "date",
                "local_trim_count",
                "local_short_cover_count",
                "floor_refill_count",
                "local_short_entry_count",
                "open_short_then_floor_refill_count",
                "trim_then_floor_refill_count",
                "trim_then_floor_refill_amount",
            ]
        )
    trades["amount"] = pd.to_numeric(trades["amount"], errors="coerce").fillna(0.0)
    for date, day in trades.sort_values("time").groupby("date", sort=True):
        trim_seen = False
        trim_count = 0
        short_entry_count = 0
        cover_count = 0
        refill_count = 0
        refill_after_trim = 0
        refill_after_trim_amount = 0.0
        for record in day.itertuples(index=False):
            reason = str(record.reason)
            if reason == "V6 local trim":
                trim_seen = True
                trim_count += 1
            elif reason == "V6 local short entry":
                trim_seen = True
                short_entry_count += 1
            elif reason == "V6 local short cover":
                cover_count += 1
                trim_seen = False
            elif reason in FLOOR_REFILL_REASONS:
                refill_count += 1
                if trim_seen:
                    refill_after_trim += 1
                    refill_after_trim_amount += float(record.amount)
        if trim_count or cover_count or refill_count:
            rows.append(
                {
                    "fold": result.fold.name,
                    "variant": result.variant,
                    "fallback_slippage_ticks": result.slippage_ticks,
                    "date": date,
                    "local_trim_count": trim_count,
                    "local_short_cover_count": cover_count,
                    "floor_refill_count": refill_count,
                    "local_short_entry_count": short_entry_count,
                    "open_short_then_floor_refill_count": refill_after_trim,
                    "trim_then_floor_refill_count": refill_after_trim,
                    "trim_then_floor_refill_amount": round(refill_after_trim_amount, 4),
                }
            )
    return pd.DataFrame(rows)


def module_interval_attribution(result: RunResult) -> pd.DataFrame:
    eq = result.equity.copy()
    if eq.empty:
        return pd.DataFrame()
    trades = result.trades.copy()
    if trades.empty:
        return pd.DataFrame()
    eq["dt_ts"] = pd.to_datetime(eq["dt"])
    trades["time_ts"] = pd.to_datetime(trades["time"])
    trades = trades.sort_values("time_ts").reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    for index, trade in trades.iterrows():
        reason = str(trade["reason"])
        if reason == "initial 70% base position":
            continue
        start_candidates = eq.loc[eq["dt_ts"] >= trade["time_ts"]]
        if start_candidates.empty:
            continue
        start = start_candidates.iloc[0]
        next_trades = trades.loc[(trades.index > index) & (trades["reason"] != "initial 70% base position")]
        if next_trades.empty:
            interval = eq.loc[eq["dt_ts"] >= start["dt_ts"]]
        else:
            next_time = next_trades.iloc[0]["time_ts"]
            interval = eq.loc[(eq["dt_ts"] >= start["dt_ts"]) & (eq["dt_ts"] < next_time)]
        if interval.empty:
            interval = start_candidates.iloc[:1]
        end = interval.iloc[-1]
        strategy_pnl = float(end["asset"]) - float(start["asset"])
        benchmark_pnl = float(end["benchmark_asset"]) - float(start["benchmark_asset"])
        alpha = strategy_pnl - benchmark_pnl
        module_key, module_name = MODULE_REASON_MAP.get(reason, ("unmapped", "unmapped"))
        rows.append(
            {
                "time": trade["time"],
                "reason": reason,
                "side": trade["side"],
                "shares": int(trade["shares"]),
                "amount": float(trade["amount"]),
                "orderbook_fallback": bool(trade["orderbook_fallback"]),
                "module_key": module_key,
                "module_name": module_name,
                "start_dt": start["dt"],
                "end_dt": end["dt"],
                "strategy_pnl": round(strategy_pnl, 4),
                "benchmark_pnl": round(benchmark_pnl, 4),
                "interval_alpha_amount": round(alpha, 4),
                "win": alpha > 0,
            }
        )
    return pd.DataFrame(rows)


def module_attribution_summary(intervals: pd.DataFrame, result: RunResult) -> tuple[pd.DataFrame, pd.DataFrame]:
    meta_rows = [
        {
            "module_key": "1_market_regime_recognition",
            "module_name": "market_regime_recognition",
            "signals": 0,
            "wins": 0,
            "interval_alpha_amount": 0.0,
            "trade_amount": 0.0,
            "fallback_count": 0,
            "win_rate": "N/A",
            "alpha_contribution_rate": 0.0,
        },
        {
            "module_key": "2_position_framework",
            "module_name": "position_framework",
            "signals": 0,
            "wins": 0,
            "interval_alpha_amount": 0.0,
            "trade_amount": 0.0,
            "fallback_count": 0,
            "win_rate": "N/A",
            "alpha_contribution_rate": 0.0,
        },
    ]
    if intervals.empty:
        summary = pd.DataFrame(meta_rows)
        full_alpha = 0.0
        traded_alpha = 0.0
    else:
        summary = (
            intervals.groupby(["module_key", "module_name"], as_index=False)
            .agg(
                signals=("reason", "count"),
                wins=("win", "sum"),
                interval_alpha_amount=("interval_alpha_amount", "sum"),
                trade_amount=("amount", "sum"),
                fallback_count=("orderbook_fallback", "sum"),
            )
        )
        summary["win_rate"] = (summary["wins"] / summary["signals"]).round(4)
        traded_alpha = float(summary["interval_alpha_amount"].sum())
        if traded_alpha:
            summary["alpha_contribution_rate"] = (summary["interval_alpha_amount"] / traded_alpha).round(4)
        else:
            summary["alpha_contribution_rate"] = 0.0
        summary = pd.concat([pd.DataFrame(meta_rows), summary], ignore_index=True)
        full_alpha = float(result.equity.iloc[-1]["asset"]) - float(result.equity.iloc[-1]["benchmark_asset"])
    summary = summary.sort_values("module_key").reset_index(drop=True)
    metadata = pd.DataFrame(
        [
            {
                "variant": result.variant,
                "fallback_slippage_ticks": result.slippage_ticks,
                "full_alpha_amount": round(full_alpha, 4),
                "traded_interval_alpha_amount": round(traded_alpha, 4),
                "unassigned_alpha_amount": round(full_alpha - traded_alpha, 4),
                "attribution_method": "post_trade_interval_alpha_vs_70pct_benchmark",
            }
        ]
    )
    return summary, metadata


def local_t_contribution(metrics: pd.DataFrame) -> pd.DataFrame:
    needed = {"baseline", "no_local_t"}
    if not needed.issubset(set(metrics["variant"].unique())):
        return pd.DataFrame()
    key_cols = ["fold", "fallback_slippage_ticks"]
    baseline = metrics.loc[metrics["variant"] == "baseline"].set_index(key_cols)
    no_local = metrics.loc[metrics["variant"] == "no_local_t"].set_index(key_cols)
    joined = baseline.join(no_local, lsuffix="_baseline", rsuffix="_no_local_t", how="inner").reset_index()
    rows: list[dict[str, Any]] = []
    for row in joined.itertuples(index=False):
        rows.append(
            {
                "fold": row.fold,
                "fallback_slippage_ticks": row.fallback_slippage_ticks,
                "baseline_alpha_amount": row.alpha_amount_baseline,
                "no_local_t_alpha_amount": row.alpha_amount_no_local_t,
                "local_t_net_alpha_amount": round(row.alpha_amount_baseline - row.alpha_amount_no_local_t, 4),
                "baseline_trade_count": row.trade_count_baseline,
                "no_local_t_trade_count": row.trade_count_no_local_t,
                "local_t_trade_delta": int(row.trade_count_baseline - row.trade_count_no_local_t),
                "baseline_fallback_count": row.fallback_count_baseline,
                "no_local_t_fallback_count": row.fallback_count_no_local_t,
                "local_t_fallback_delta": int(row.fallback_count_baseline - row.fallback_count_no_local_t),
            }
        )
    return pd.DataFrame(rows)


def gate_summary(metrics: pd.DataFrame, sources: pd.DataFrame | None = None) -> pd.DataFrame:
    if "baseline" not in set(metrics["variant"].unique()):
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    baseline = metrics.loc[metrics["variant"] == "baseline"].copy()
    for variant in sorted(set(metrics["variant"].unique()) - {"baseline"}):
        candidate = metrics.loc[metrics["variant"] == variant].copy()
        joined = candidate.merge(
            baseline,
            on=["fold", "fallback_slippage_ticks"],
            suffixes=("", "_baseline"),
            how="inner",
        )
        if joined.empty:
            continue
        joined["alpha_edge_amount"] = joined["alpha_amount"] - joined["alpha_amount_baseline"]
        positive_edges = joined["alpha_edge_amount"].clip(lower=0.0)
        positive_sum = float(positive_edges.sum())
        max_positive_share = float(positive_edges.max() / positive_sum) if positive_sum > 0 else 1.0
        positive_folds = int((joined.groupby("fold")["alpha_edge_amount"].sum() > 0).sum())

        by_slip = joined.groupby("fallback_slippage_ticks").agg(
            alpha_amount=("alpha_amount", "sum"),
            baseline_alpha_amount=("alpha_amount_baseline", "sum"),
            trade_count=("trade_count", "sum"),
            baseline_trade_count=("trade_count_baseline", "sum"),
            fallback_count=("fallback_count", "sum"),
            baseline_fallback_count=("fallback_count_baseline", "sum"),
            max_drawdown=("max_drawdown", "max"),
            baseline_max_drawdown=("max_drawdown_baseline", "max"),
        )
        by_slip["alpha_edge_amount"] = by_slip["alpha_amount"] - by_slip["baseline_alpha_amount"]
        by_slip["trade_ratio"] = by_slip.apply(
            lambda row: float(row["trade_count"] / row["baseline_trade_count"]) if row["baseline_trade_count"] else 1.0,
            axis=1,
        )
        by_slip["fallback_ratio"] = by_slip.apply(
            lambda row: float(row["fallback_count"] / row["baseline_fallback_count"])
            if row["baseline_fallback_count"]
            else (1.0 if row["fallback_count"] == 0 else 999.0),
            axis=1,
        )
        by_slip["drawdown_delta"] = by_slip["max_drawdown"] - by_slip["baseline_max_drawdown"]

        slip1 = by_slip.loc[1] if 1 in by_slip.index else by_slip.iloc[0]
        slip5_edge = float(by_slip.loc[5, "alpha_edge_amount"]) if 5 in by_slip.index else None
        avg_alpha_edge = float(joined["alpha_edge_amount"].mean())
        total_alpha_edge = float(joined["alpha_edge_amount"].sum())
        trade_ratio_max = float(by_slip["trade_ratio"].max())
        fallback_ratio_max = float(by_slip["fallback_ratio"].max())
        drawdown_delta_max = float(by_slip["drawdown_delta"].max())

        positive_sources = 0
        max_positive_source_share = 1.0
        source_pass = True
        if sources is not None and not sources.empty:
            source_baseline = sources.loc[sources["variant"] == "baseline"].copy()
            source_candidate = sources.loc[sources["variant"] == variant].copy()
            source_joined = source_candidate.merge(
                source_baseline,
                on=["fold", "fallback_slippage_ticks", "source_segment"],
                suffixes=("", "_baseline"),
                how="inner",
            )
            if not source_joined.empty:
                source_joined["alpha_edge_amount"] = (
                    source_joined["alpha_amount"] - source_joined["alpha_amount_baseline"]
                )
                source_edges = source_joined.groupby("source_segment")["alpha_edge_amount"].sum()
                source_count = int(source_edges.shape[0])
                source_positive_edges = source_edges.clip(lower=0.0)
                source_positive_sum = float(source_positive_edges.sum())
                positive_sources = int((source_edges > 0).sum())
                max_positive_source_share = (
                    float(source_positive_edges.max() / source_positive_sum) if source_positive_sum > 0 else 1.0
                )
                required_positive_sources = min(2, source_count)
                source_pass = positive_sources >= required_positive_sources and max_positive_source_share <= 0.80

        alpha_pass = total_alpha_edge > 0 and avg_alpha_edge > 0
        concentration_pass = max_positive_share <= 0.70 and positive_folds >= 2
        slip5_pass = slip5_edge is not None and slip5_edge > 0
        trade_pass = trade_ratio_max <= 1.35
        fallback_pass = fallback_ratio_max <= 1.50
        drawdown_pass = drawdown_delta_max <= 0.03
        passes = all([alpha_pass, concentration_pass, slip5_pass, trade_pass, fallback_pass, drawdown_pass, source_pass])
        notes = []
        if not alpha_pass:
            notes.append("validation alpha not above baseline")
        if not concentration_pass:
            notes.append("benefit concentrated in too few folds")
        if not slip5_pass:
            notes.append("no 5-tick slippage edge")
        if not source_pass:
            notes.append("benefit concentrated in too few data sources")
        if not trade_pass:
            notes.append("trade count inflated")
        if not fallback_pass:
            notes.append("fallback count inflated")
        if not drawdown_pass:
            notes.append("drawdown worsened")

        rows.append(
            {
                "variant": variant,
                "fold_count": int(joined["fold"].nunique()),
                "slippage_cases": ",".join(str(int(x)) for x in sorted(joined["fallback_slippage_ticks"].unique())),
                "total_alpha_edge_amount": round(total_alpha_edge, 4),
                "avg_alpha_edge_amount": round(avg_alpha_edge, 4),
                "slip1_alpha_edge_amount": round(float(slip1["alpha_edge_amount"]), 4),
                "slip5_alpha_edge_amount": round(slip5_edge, 4) if slip5_edge is not None else "",
                "positive_folds": positive_folds,
                "max_positive_fold_share": round(max_positive_share, 4),
                "positive_sources": positive_sources,
                "max_positive_source_share": round(max_positive_source_share, 4),
                "max_trade_count_ratio": round(trade_ratio_max, 4),
                "max_fallback_count_ratio": round(fallback_ratio_max, 4),
                "max_drawdown_delta": round(drawdown_delta_max, 8),
                "passes_all_gates": passes,
                "gate_notes": "; ".join(notes) if notes else "pass",
            }
        )
    return pd.DataFrame(rows)


def write_outputs(
    output_dir: Path,
    scan: pd.DataFrame,
    metrics: pd.DataFrame,
    daily: pd.DataFrame,
    reasons: pd.DataFrame,
    sources: pd.DataFrame,
    local_t: pd.DataFrame,
    conflicts: pd.DataFrame,
    module_intervals: pd.DataFrame,
    module_summary: pd.DataFrame,
    module_metadata: pd.DataFrame,
    gates: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "production_lookahead_scan": output_dir / "production_lookahead_scan.csv",
        "validation_metrics": output_dir / "validation_metrics.csv",
        "daily_validation_metrics": output_dir / "daily_validation_metrics.csv",
        "reason_attribution": output_dir / "reason_attribution.csv",
        "source_attribution": output_dir / "source_attribution.csv",
        "local_t_contribution": output_dir / "local_t_contribution.csv",
        "floor_refill_conflicts": output_dir / "floor_refill_conflicts.csv",
        "module_interval_attribution": output_dir / "module_interval_attribution.csv",
        "module_attribution_summary": output_dir / "module_attribution_summary.csv",
        "module_attribution_metadata": output_dir / "module_attribution_metadata.csv",
        "gate_summary": output_dir / "gate_summary.csv",
        "summary": output_dir / "summary.json",
    }
    for name, path in outputs.items():
        if name == "summary":
            continue
        table = {
            "production_lookahead_scan": scan,
            "validation_metrics": metrics,
            "daily_validation_metrics": daily,
            "reason_attribution": reasons,
            "source_attribution": sources,
            "local_t_contribution": local_t,
            "floor_refill_conflicts": conflicts,
            "module_interval_attribution": module_intervals,
            "module_attribution_summary": module_summary,
            "module_attribution_metadata": module_metadata,
            "gate_summary": gates,
        }[name]
        table.to_csv(path, index=False, encoding="utf-8-sig")

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "policy": {
            "uses_future_data_for_signals": False,
            "uses_full_sample_parameter_selection": False,
            "variants_are_fixed": True,
            "production_scan_findings": int(len(scan)),
        },
        "args": {
            "folds": args.folds,
            "variants": args.variants,
            "slippage": args.slippage,
            "data_dir": args.data_dir,
        },
        "outputs": {name: str(path) for name, path in outputs.items()},
    }
    outputs["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {name: str(path) for name, path in outputs.items()}


def run_validation(args: argparse.Namespace) -> dict[str, str]:
    selected_folds = []
    for key in _comma_list(args.folds):
        if key in DEFAULT_FOLDS:
            selected_folds.append(DEFAULT_FOLDS[key])
            continue
        matches = [fold for fold in DEFAULT_FOLDS.values() if fold.name == key]
        if not matches:
            raise ValueError(f"unknown fold: {key}")
        selected_folds.append(matches[0])
    selected_variants = _comma_list(args.variants)
    unknown_variants = [name for name in selected_variants if name not in VARIANTS]
    if unknown_variants:
        raise ValueError(f"unknown variants: {', '.join(unknown_variants)}")
    slippage_values = _parse_int_list(args.slippage)
    end_date = max(fold.val_end for fold in selected_folds)

    scan = scan_production_for_lookahead(Path(PROJECT_ROOT))
    if not scan.empty and not args.allow_production_scan_findings:
        raise RuntimeError(
            "production lookahead scan found forbidden tokens; see production_lookahead_scan.csv after rerun "
            "with --allow-production-scan-findings if these are known false positives"
        )

    df = _prepare_frame(args.data_dir, START_DATE, end_date)
    metrics_rows: list[dict[str, Any]] = []
    daily_tables: list[pd.DataFrame] = []
    reason_tables: list[pd.DataFrame] = []
    source_tables: list[pd.DataFrame] = []
    conflict_tables: list[pd.DataFrame] = []

    run_cache: dict[tuple[str, int], RunResult] = {}
    for slippage_ticks in slippage_values:
        for variant in selected_variants:
            run_cache[(variant, slippage_ticks)] = run_variant(
                df,
                end_date,
                variant,
                VARIANTS[variant],
                slippage_ticks,
            )

    for fold in selected_folds:
        for slippage_ticks in slippage_values:
            for variant in selected_variants:
                result = bind_fold(run_cache[(variant, slippage_ticks)], fold)
                metrics_rows.append(validation_metrics(result))
                daily = daily_validation_metrics(result)
                daily_tables.append(daily)
                reason_tables.append(reason_attribution(result))
                source_tables.append(source_attribution(daily, result))
                conflict_tables.append(floor_refill_conflicts(result))

    metrics = pd.DataFrame(metrics_rows)
    daily_all = pd.concat(daily_tables, ignore_index=True) if daily_tables else pd.DataFrame()
    reasons = pd.concat(reason_tables, ignore_index=True) if reason_tables else pd.DataFrame()
    sources = pd.concat(source_tables, ignore_index=True) if source_tables else pd.DataFrame()
    conflicts = pd.concat(conflict_tables, ignore_index=True) if conflict_tables else pd.DataFrame()
    local_t = local_t_contribution(metrics)
    baseline_result = run_cache.get(("baseline", 1))
    if baseline_result is not None:
        module_intervals = module_interval_attribution(baseline_result)
        module_summary, module_metadata = module_attribution_summary(module_intervals, baseline_result)
    else:
        module_intervals = pd.DataFrame()
        module_summary = pd.DataFrame()
        module_metadata = pd.DataFrame()
    gates = gate_summary(metrics, sources)
    output_dir = Path(args.output_dir)
    return write_outputs(
        output_dir,
        scan,
        metrics,
        daily_all,
        reasons,
        sources,
        local_t,
        conflicts,
        module_intervals,
        module_summary,
        module_metadata,
        gates,
        args,
    )


def _run_curve_for_params(
    df: pd.DataFrame,
    params: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    strategy = V6BacktestExecutionStrategy(initial_capital=INITIAL_CAPITAL, **params)
    first_row = df.iloc[0]
    first_price = float(first_row["price"])
    seed_initial_position(strategy, first_row, INITIAL_STRATEGY_TARGET_PCT)
    benchmark = benchmark_buy_and_hold(INITIAL_CAPITAL, first_price, first_price, BENCHMARK_TARGET_PCT)

    equity_rows: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        tick = _tick_from_tuple(row)
        price = float(row.price)
        strategy.on_tick(tick)
        equity_rows.append(
            {
                "date": str(row.date),
                "dt": _timestamp_text(row.dt),
                "asset": float(strategy.total_asset(price)),
                "benchmark_asset": float(benchmark.cash_after_buy + benchmark.buy_shares * price),
            }
        )

    trades = _normalise_trade_frame(pd.DataFrame(strategy.execution_records))
    trades["date"] = trades["time"].astype(str).str.slice(0, 10)
    return pd.DataFrame(equity_rows), trades


def _legacy_compare_metric(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    start: str | None,
    end: str,
) -> dict[str, Any]:
    if start is None:
        window = equity.loc[equity["date"] <= end].copy()
        if window.empty:
            raise ValueError(f"empty legacy compare window start={start} end={end}")
        start_asset = INITIAL_CAPITAL
        start_benchmark = float(window.iloc[0]["benchmark_asset"])
        trade_window = trades.loc[trades["date"] <= end].copy()
        actual_start = str(window.iloc[0]["date"])
    else:
        window = equity.loc[(equity["date"] >= start) & (equity["date"] <= end)].copy()
        if window.empty:
            raise ValueError(f"empty legacy compare window start={start} end={end}")
        start_asset = float(window.iloc[0]["asset"])
        start_benchmark = float(window.iloc[0]["benchmark_asset"])
        trade_window = trades.loc[(trades["date"] >= start) & (trades["date"] <= end)].copy()
        actual_start = str(window.iloc[0]["date"])
    end_asset = float(window.iloc[-1]["asset"])
    end_benchmark = float(window.iloc[-1]["benchmark_asset"])
    strategy_return = end_asset / start_asset - 1.0 if start_asset else 0.0
    benchmark_return = end_benchmark / start_benchmark - 1.0 if start_benchmark else 0.0
    reason_counts = (
        trade_window["reason"].value_counts().sort_index().to_dict()
        if not trade_window.empty and "reason" in trade_window.columns
        else {}
    )
    return {
        "actual_start": actual_start,
        "actual_end": str(window.iloc[-1]["date"]),
        "strategy_return": round(strategy_return, 8),
        "benchmark_return": round(benchmark_return, 8),
        "alpha_return": round(strategy_return - benchmark_return, 8),
        "end_asset": round(end_asset, 4),
        "max_drawdown": round(max_drawdown(window["asset"].astype(float).tolist()), 8),
        "trade_count": int(len(trade_window)),
        "local_trade_count": int(
            trade_window["reason"].astype(str).str.contains("local", na=False).sum()
        )
        if not trade_window.empty and "reason" in trade_window.columns
        else 0,
        "reason_counts": reason_counts,
    }


def run_legacy_compare(args: argparse.Namespace) -> dict[str, str]:
    scan = scan_production_for_lookahead(Path(PROJECT_ROOT))
    if not scan.empty and not args.allow_production_scan_findings:
        raise RuntimeError(
            "production lookahead scan found forbidden tokens; rerun with --allow-production-scan-findings "
            "only after reviewing false positives"
        )
    bundle = load_market_data(start_date=START_DATE, data_dir=args.data_dir)
    df = bundle.frame.copy()
    df["date"] = df["date"].astype(str)
    latest = str(df.iloc[-1]["date"])
    unique_dates = list(df["date"].drop_duplicates())
    rolling_start = unique_dates[-15] if len(unique_dates) >= 15 else unique_dates[0]

    runs = {
        name: _run_curve_for_params(df, params)
        for name, params in LEGACY_COMPARE_VARIANTS.items()
    }
    metrics: dict[str, Any] = {}
    for window_name, (start, raw_end) in LEGACY_COMPARE_WINDOWS.items():
        window_start = rolling_start if start == "ROLLING_15" else start
        window_end = latest if raw_end is None else min(raw_end, latest)
        metrics[window_name] = {
            name: _legacy_compare_metric(equity, trades, window_start, window_end)
            for name, (equity, trades) in runs.items()
        }
        current = metrics[window_name]["baseline_directional"]
        legacy = metrics[window_name]["legacy_local_t"]
        metrics[window_name]["directional_minus_legacy"] = {
            "return_edge": round(current["strategy_return"] - legacy["strategy_return"], 8),
            "alpha_edge": round(current["alpha_return"] - legacy["alpha_return"], 8),
            "drawdown_delta": round(current["max_drawdown"] - legacy["max_drawdown"], 8),
            "trade_count_delta": current["trade_count"] - legacy["trade_count"],
            "local_trade_count_delta": current["local_trade_count"] - legacy["local_trade_count"],
        }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "legacy_compare.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "policy": {
            "uses_future_data_for_signals": False,
            "production_scan_findings": int(len(scan)),
            "comparison": "baseline_directional_vs_legacy_local_t",
        },
        "latest_date": latest,
        "rolling_15_start": rolling_start,
        "data_warnings": bundle.warnings,
        "metrics": metrics,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"legacy_compare": str(output_path)}


def build_parser() -> argparse.ArgumentParser:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description="Run anti-overfit walk-forward validation for V6 variants.")
    parser.add_argument("--folds", default="mar,apr,may,jun", help="Comma list: mar,apr,may,jun or full fold names.")
    parser.add_argument(
        "--variants",
        default="baseline,no_local_t,legacy_local_t,legacy_refill_lock,cooldown_60",
        help="Comma list of fixed variants.",
    )
    parser.add_argument("--slippage", default="1,2,3,5", help="Comma list of fallback slippage ticks.")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-dir", default=str(Path(PROJECT_ROOT) / "qmt" / "analysis" / f"anti_overfit_{stamp}"))
    parser.add_argument(
        "--legacy-compare-only",
        action="store_true",
        help="Run a lightweight current directional-T vs legacy local-T comparison and write legacy_compare.json.",
    )
    parser.add_argument(
        "--allow-production-scan-findings",
        action="store_true",
        help="Continue despite production scan findings; intended only for reviewing false positives.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.legacy_compare_only:
        outputs = run_legacy_compare(args)
        print(f"legacy_compare={outputs['legacy_compare']}")
        return
    outputs = run_validation(args)
    print(f"validation_metrics={outputs['validation_metrics']}")
    print(f"gate_summary={outputs['gate_summary']}")
    print(f"summary={outputs['summary']}")


if __name__ == "__main__":
    main()
