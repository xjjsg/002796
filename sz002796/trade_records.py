"""Unified trade-record schema and replay helpers.

Backtests and the realtime runtime both write CSV trade ledgers.  This module keeps
their column order, legacy aliases, and cash/share replay rules in one place so
the web runtime can consume backtest trades directly and append realtime trades
without changing accounting semantics.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ANCHOR_PCT, INITIAL_CASH, INITIAL_SHARES, INITIAL_TARGET_PCT, LOT_SIZE, parse_dt
from .execution import TradeCosts, calculate_trade_costs
from .position import PositionMode, TradeRecord


TRADE_LOG_COLUMNS = [
    "timestamp",
    "source",
    "tick_time",
    "side",
    "price",
    "last_price",
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
    "day_trade_count",
    "reason",
    "detail",
    "execution_source",
    "orderbook_fallback",
    "day_vwap_dev",
    "local_vwap_dev",
    "velocity",
    "acceleration",
    "vol_mom",
    "day_return",
    "vwap",
    "local_vwap",
    "range_position",
    "orderbook_imbalance",
    "cross_buy_score",
    "cross_sell_score",
    "local_trim_score",
    "local_cover_score",
    "buy_timing_score",
    "sell_timing_score",
]


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def canonicalize_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with the current column names filled from legacy aliases."""
    result = dict(row)
    result["timestamp"] = _first(result, "timestamp", "time")
    result["asset_after"] = _first(result, "asset_after", "asset")
    result["position_pct_after"] = _first(result, "position_pct_after", "position_pct")
    result["last_price"] = _first(result, "last_price", "price")
    return result


def read_trade_rows(path: str | None) -> list[dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        return [canonicalize_trade_row(row) for row in csv.DictReader(f)]


def ensure_trade_log_schema(path: str, required_fields: list[str] | None = None) -> list[str]:
    required_fields = required_fields or TRADE_LOG_COLUMNS
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return required_fields

    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        old_fields = reader.fieldnames or []
        rows = [canonicalize_trade_row(row) for row in reader]

    merged_fields = required_fields + [field for field in old_fields if field not in required_fields]
    if old_fields == merged_fields:
        return merged_fields

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=merged_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in merged_fields})
    os.replace(tmp_path, path)
    return merged_fields


def trade_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    row = canonicalize_trade_row(row)
    return (
        str(row.get("timestamp") or ""),
        str(row.get("side") or ""),
        str(row.get("price") or ""),
        str(row.get("shares") or ""),
        str(row.get("reason") or ""),
        str(row.get("detail") or ""),
    )


def trade_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[datetime, int]:
    idx, row = item
    row = canonicalize_trade_row(row)
    timestamp = parse_dt(row.get("timestamp"))
    return timestamp or datetime.max, idx


def trade_row_timestamp(row: dict[str, Any]) -> datetime | None:
    row = canonicalize_trade_row(row)
    return parse_dt(row.get("timestamp"))


def latest_trade_timestamp(rows: list[dict[str, Any]]) -> datetime | None:
    return max(
        (dt for dt in (trade_row_timestamp(row) for row in rows) if dt is not None),
        default=None,
    )


def backtest_summary_path_for_trade_log(path: str | None) -> Path | None:
    if not path:
        return None
    return Path(path).with_name("summary.json")


def read_backtest_coverage_end(path: str | None) -> datetime | None:
    summary_path = backtest_summary_path_for_trade_log(path)
    if summary_path is None or not summary_path.exists():
        return None
    try:
        with summary_path.open("r", encoding="utf-8") as f:
            summary = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    end_time = parse_dt(summary.get("end_time"))
    if end_time is not None:
        return end_time

    end_date = summary.get("end_date")
    if end_date:
        return parse_dt(f"{end_date} 15:00:00")
    return None


def seed_coverage_end(
    seed_trade_log_path: str | None,
    seed_rows: list[dict[str, Any]],
) -> tuple[datetime | None, str, datetime | None]:
    last_trade_at = latest_trade_timestamp(seed_rows)
    summary_end = read_backtest_coverage_end(seed_trade_log_path)
    if summary_end is not None and (last_trade_at is None or summary_end >= last_trade_at):
        return summary_end, "summary", last_trade_at
    if last_trade_at is not None:
        return last_trade_at, "trades", last_trade_at
    if summary_end is not None:
        return summary_end, "summary", last_trade_at
    return None, "none", last_trade_at


def merge_seed_and_runtime_trade_rows(
    seed_rows: list[dict[str, Any]],
    runtime_rows_all: list[dict[str, Any]],
    seed_trade_log_path: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    coverage_end, coverage_source, last_trade_at = seed_coverage_end(seed_trade_log_path, seed_rows)
    runtime_rows: list[dict[str, Any]] = []
    ignored_runtime_rows = 0
    for row in runtime_rows_all:
        runtime_dt = trade_row_timestamp(row)
        if coverage_end is not None and runtime_dt is not None and runtime_dt <= coverage_end:
            ignored_runtime_rows += 1
            continue
        runtime_rows.append(row)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    duplicate_count = 0

    for row in seed_rows + runtime_rows:
        key = trade_identity(row)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        rows.append(row)

    source = "none"
    if seed_rows and runtime_rows:
        source = "backtest+runtime"
    elif seed_rows:
        source = "backtest"
    elif runtime_rows:
        source = "runtime"

    return rows, {
        "source": source,
        "seed_rows": len(seed_rows),
        "runtime_rows": len(runtime_rows),
        "runtime_rows_total": len(runtime_rows_all),
        "ignored_runtime_rows_in_seed_window": ignored_runtime_rows,
        "seed_last_timestamp": last_trade_at.isoformat(sep=" ") if last_trade_at else None,
        "seed_coverage_end": coverage_end.isoformat(sep=" ") if coverage_end else None,
        "seed_coverage_source": coverage_source,
        "duplicate_rows": duplicate_count,
    }


def mode_from_target_pct(target_pct: float) -> PositionMode:
    if target_pct < ANCHOR_PCT - 0.03:
        return PositionMode.DEFENSE
    if target_pct > ANCHOR_PCT + 0.03:
        return PositionMode.ATTACK
    return PositionMode.NEUTRAL


def apply_trade_row(
    row: dict[str, Any],
    cash: float,
    shares: int,
    last_target_pct: float,
    last_mode: PositionMode,
) -> tuple[float, int, float, PositionMode, TradeRecord | None, dict[str, Any], str | None]:
    row = canonicalize_trade_row(row)
    side = str(row.get("side", "") or "").upper()
    price = float(row.get("price", 0.0) or 0.0)
    requested_shares = int(float(row.get("shares", 0) or 0))
    traded_shares = int(requested_shares / LOT_SIZE) * LOT_SIZE
    timestamp = parse_dt(row.get("timestamp")) or datetime.now()
    target_pct = _safe_float(row.get("target_pct"))
    if target_pct is None:
        target_pct = _safe_float(row.get("position_pct_after"))
    if target_pct is None:
        target_pct = last_target_pct

    mode_value = str(row.get("mode", "") or "")
    try:
        mode = PositionMode(mode_value) if mode_value else mode_from_target_pct(target_pct)
    except ValueError:
        mode = mode_from_target_pct(target_pct)

    reason = str(row.get("reason", "") or "")
    detail = str(row.get("detail", "") or "")
    warning = None

    if side not in {"BUY", "SELL"} or price <= 0 or traded_shares <= 0:
        return cash, shares, target_pct, mode, None, {}, f"ignored invalid trade row at {timestamp.isoformat()}"

    if side == "SELL" and traded_shares > shares:
        warning = f"sell shares capped from {traded_shares} to {shares} at {timestamp.isoformat()}"
        traded_shares = int(shares / LOT_SIZE) * LOT_SIZE
        if traded_shares <= 0:
            return cash, shares, target_pct, mode, None, {}, warning

    costs = calculate_trade_costs(side, price, traded_shares)
    if side == "BUY":
        cash -= costs.buy_cash_required
        shares += traded_shares
    else:
        cash += costs.sell_cash_received
        shares -= traded_shares

    record = TradeRecord(
        timestamp=timestamp,
        side=side,
        price=price,
        shares=traded_shares,
        position_shares=shares,
        cash_after=cash,
        target_pct=target_pct,
        mode=mode.value,
        reason=reason,
        detail=detail,
    )
    cost_row = {
        "shares": traded_shares,
        "amount": costs.amount,
        "commission": costs.commission,
        "stamp_tax": costs.stamp_tax,
        "position_shares": shares,
        "cash_after": cash,
        "target_pct": target_pct,
        "mode": mode.value,
    }
    return cash, shares, target_pct, mode, record, cost_row, warning


def replay_trade_rows(
    rows: list[dict[str, Any]],
    initial_cash: float = INITIAL_CASH,
    initial_shares: int = INITIAL_SHARES,
    initial_target_pct: float = INITIAL_TARGET_PCT,
) -> tuple[float, int, float, PositionMode, list[TradeRecord], list[str]]:
    cash = initial_cash
    shares = initial_shares
    target_pct = initial_target_pct
    mode = mode_from_target_pct(target_pct)
    records: list[TradeRecord] = []
    warnings: list[str] = []

    for _, row in sorted(enumerate(rows), key=trade_sort_key):
        cash, shares, target_pct, mode, record, _, warning = apply_trade_row(
            row,
            cash,
            shares,
            target_pct,
            mode,
        )
        if warning:
            warnings.append(warning)
        if record is not None:
            records.append(record)
    return cash, shares, target_pct, mode, records, warnings


def trade_from_dict(row: dict[str, Any]) -> TradeRecord:
    row = canonicalize_trade_row(row)
    timestamp = parse_dt(row.get("timestamp")) or datetime.now()
    return TradeRecord(
        timestamp=timestamp,
        side=str(row.get("side", "")),
        price=float(row.get("price", 0.0) or 0.0),
        shares=int(float(row.get("shares", 0) or 0)),
        position_shares=int(float(row.get("position_shares", 0) or 0)),
        cash_after=float(row.get("cash_after", 0.0) or 0.0),
        target_pct=float(row.get("target_pct", 0.0) or 0.0),
        mode=str(row.get("mode", PositionMode.NEUTRAL.value)),
        reason=str(row.get("reason", "")),
        detail=str(row.get("detail", "")),
    )


def _tick_time_text(tick: dict[str, Any] | None) -> str:
    if not tick:
        return ""
    server_time = tick.get("server_time")
    if server_time:
        return str(server_time)
    value = tick.get("Time") or tick.get("dt")
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    return str(value or "")


def trade_to_dict(
    trade: TradeRecord,
    *,
    strategy: Any | None = None,
    tick: dict[str, Any] | None = None,
    source: str = "",
    costs: TradeCosts | None = None,
    execution_source: str = "",
    orderbook_fallback: bool | str = "",
    mark_price: float | None = None,
) -> dict[str, Any]:
    if costs is None:
        costs = calculate_trade_costs(trade.side, trade.price, trade.shares)

    last_price = None
    if tick:
        last_price = tick.get("price", tick.get("Close"))
    if last_price in (None, ""):
        last_price = mark_price if mark_price is not None else trade.price
    current_price = float(last_price or trade.price)
    asset_after = None
    position_pct_after = None
    if strategy is not None:
        asset_after = strategy.total_asset(current_price)
        position_pct_after = strategy.current_position_pct(current_price)
    else:
        asset_after = trade.cash_after + trade.position_shares * current_price
        position_pct_after = trade.position_shares * current_price / asset_after if asset_after > 0 else 0.0

    row: dict[str, Any] = {
        "timestamp": trade.timestamp.isoformat(sep=" ", timespec="seconds"),
        "source": source,
        "tick_time": _tick_time_text(tick),
        "side": trade.side,
        "price": trade.price,
        "last_price": current_price,
        "shares": trade.shares,
        "amount": costs.amount,
        "commission": costs.commission,
        "stamp_tax": costs.stamp_tax,
        "cash_after": trade.cash_after,
        "position_shares": trade.position_shares,
        "asset_after": asset_after,
        "position_pct_after": position_pct_after,
        "target_pct": trade.target_pct,
        "mode": trade.mode,
        "day_trade_count": getattr(strategy, "day_trade_count", "") if strategy is not None else "",
        "reason": trade.reason,
        "detail": trade.detail,
        "execution_source": execution_source,
        "orderbook_fallback": orderbook_fallback,
    }

    if strategy is not None:
        snapshot = getattr(getattr(strategy, "factor_calc", None), "last_snapshot", None)
        if snapshot is not None:
            row.update(
                {
                    "day_vwap_dev": snapshot.day_vwap_dev,
                    "local_vwap_dev": snapshot.local_vwap_dev,
                    "velocity": snapshot.velocity,
                    "acceleration": snapshot.acceleration,
                    "vol_mom": snapshot.vol_mom,
                    "day_return": snapshot.day_return,
                    "vwap": snapshot.vwap,
                    "local_vwap": snapshot.local_vwap,
                    "range_position": snapshot.range_position,
                    "orderbook_imbalance": snapshot.orderbook_imbalance,
                    "cross_buy_score": strategy._score_cross_buy(snapshot),
                    "cross_sell_score": strategy._score_cross_sell(snapshot),
                    "local_trim_score": strategy._score_local_trim(snapshot),
                    "local_cover_score": strategy._score_local_cover(snapshot),
                    "buy_timing_score": strategy._score_buy_timing(snapshot),
                    "sell_timing_score": strategy._score_sell_timing(snapshot),
                }
            )
    return row
