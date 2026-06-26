"""Shared constants and small utility functions for the V6 system.

This module is the only place that defines project paths, trading-cost
constants, position anchors, and common date helpers. Keeping these values here
prevents web runtime, backtest, and strategy code from drifting apart.
"""
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional

# --- Trading Constants ---
LOT_SIZE = 100
COMMISSION_RATE = 0.0001
STAMP_DUTY_RATE = 0.0005
MIN_COMMISSION = 5.0

# --- Position Limits ---
FLOOR_PCT = 0.40
CEIL_PCT = 1.00
ANCHOR_PCT = 0.70

# --- Data Quality Thresholds ---
PRICE_JUMP_THRESHOLD = 0.08

# --- Project Configuration ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYMBOL_CODE = "sz002796"
SYMBOL_NAME = "世嘉科技"
DATA_DIR = str(PROJECT_ROOT / "data" / "sz002796")
STATE_FILE = os.path.join(DATA_DIR, f"{SYMBOL_CODE}_v6_strategy_state.json")
TRADE_LOG_FILE = os.path.join(DATA_DIR, f"{SYMBOL_CODE}_v6_strategy_trades.csv")
BACKTEST_RECORD_DIR = os.environ.get(
    "V6_BACKTEST_RECORD_DIR",
    str(PROJECT_ROOT / "backtest_records" / "v6_seed70_100w_2026-01-05_to_latest"),
)
BACKTEST_TRADE_LOG_FILE = os.path.join(BACKTEST_RECORD_DIR, "trades.csv")
FETCH_INTERVAL = 3.0
STATE_SAVE_INTERVAL = 60.0
LOCAL_T0_ENTER_SCORE = 0.80
WEB_MARKET_SOURCE = os.environ.get("WEB_MARKET_SOURCE", "tencent")

# --- Backtest Configuration ---
INITIAL_CAPITAL = 1_000_000.0
INITIAL_CASH = INITIAL_CAPITAL
INITIAL_SHARES = 0
INITIAL_TARGET_PCT = 0.0
INITIAL_STRATEGY_TARGET_PCT = 0.70
BENCHMARK_TARGET_PCT = 0.70
START_DATE = "2026-01-05"


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    if (datetime.strptime("09:25:00", "%H:%M:%S").time() <= t <= datetime.strptime("11:30:00", "%H:%M:%S").time()) or \
       (datetime.strptime("13:00:00", "%H:%M:%S").time() <= t <= datetime.strptime("15:00:00", "%H:%M:%S").time()):
        return True
    return False


def get_next_window() -> tuple[datetime, str]:
    now = datetime.now()
    if now.weekday() >= 5:
        days_ahead = 7 - now.weekday()
        next_open = datetime(now.year, now.month, now.day, 9, 25, 0) + timedelta(days=days_ahead)
        return next_open, "下周开盘"
        
    t = now.time()
    if t < datetime.strptime("09:25:00", "%H:%M:%S").time():
        return datetime(now.year, now.month, now.day, 9, 25, 0), "早盘集合竞价"
    elif datetime.strptime("11:30:00", "%H:%M:%S").time() < t < datetime.strptime("13:00:00", "%H:%M:%S").time():
        return datetime(now.year, now.month, now.day, 13, 0, 0), "下午开盘"
    else:
        days_ahead = 3 if now.weekday() == 4 else 1
        next_open = datetime(now.year, now.month, now.day, 9, 25, 0) + timedelta(days=days_ahead)
        return next_open, "下一交易日开盘"


def seconds_until(dt: datetime) -> float:
    return (dt - datetime.now()).total_seconds()
