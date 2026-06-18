"""Configuration for QMT-based V6 backtests and live sync."""
from __future__ import annotations

import os
from pathlib import Path

from sz002796.config import INITIAL_CAPITAL, INITIAL_STRATEGY_TARGET_PCT


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QMT_DIR = PROJECT_ROOT / "qmt"


def _usable_account_id(value: str | None) -> str:
    text = str(value or "").strip()
    placeholders = {
        "",
        "你的资金账号",
        "YOUR_ACCOUNT_ID",
        "your_account_id",
    }
    return "" if text in placeholders else text


QMT_INSTALL_DIR = Path(os.environ.get("QMT_INSTALL_DIR", r"D:\国金QMT交易端模拟"))
MINI_QMT_PATH = Path(os.environ.get("MINI_QMT_PATH", str(QMT_INSTALL_DIR / "userdata_mini")))
XTQUANT_SITE_PACKAGES = Path(
    os.environ.get(
        "XTQUANT_SITE_PACKAGES",
        str(QMT_INSTALL_DIR / "bin.x64" / "Lib" / "site-packages"),
    )
)

TARGET_SYMBOL = os.environ.get("QMT_TARGET_SYMBOL", "002796.SZ")
QMT_SIM_ACCOUNT = os.environ.get("QMT_SIM_ACCOUNT", "99005544")
QMT_BACKTEST_ACCOUNT = os.environ.get("QMT_BACKTEST_ACCOUNT", "testS")
ACCOUNT_ID = _usable_account_id(os.environ.get("QMT_ACCOUNT_ID")) or QMT_SIM_ACCOUNT

START_TIME = os.environ.get("QMT_BACKTEST_START_TIME", "20260105")
END_TIME = os.environ.get("QMT_BACKTEST_END_TIME", "")

OUTPUT_ROOT = Path(os.environ.get("QMT_BACKTEST_OUTPUT_ROOT", str(QMT_DIR / "backtest_records")))


def normalize_qmt_time_text(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def default_output_dir(start_time: str = START_TIME, end_time: str = END_TIME) -> Path:
    start_text = normalize_qmt_time_text(start_time) or "start"
    end_text = normalize_qmt_time_text(end_time) or "latest"
    return OUTPUT_ROOT / f"tick_v6_{start_text}_to_{end_text}"
