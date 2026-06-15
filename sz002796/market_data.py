"""Historical market-data loader for 002796.SZ CSV files.

The loader accepts both older minute-bar files and newer realtime orderbook
files. It normalizes columns, sorts by timestamp and local millisecond, removes
duplicate snapshots, and recomputes tick volume/amount from cumulative fields so
bad legacy delta columns cannot pollute the strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .config import DATA_DIR as CONFIG_DATA_DIR, PRICE_JUMP_THRESHOLD

DATA_DIR = Path(CONFIG_DATA_DIR)

BID_COLS = [f"bp{i}" for i in range(1, 6)]
BID_VOL_COLS = [f"bv{i}" for i in range(1, 6)]
ASK_COLS = [f"sp{i}" for i in range(1, 6)]
ASK_VOL_COLS = [f"sv{i}" for i in range(1, 6)]
ORDERBOOK_COLS = [col for pair in zip(BID_COLS, BID_VOL_COLS) for col in pair] + [
    col for pair in zip(ASK_COLS, ASK_VOL_COLS) for col in pair
]

NUMERIC_COLS = [
    "local_time_ms",
    "price",
    "open",
    "high",
    "low",
    "prev_close",
    "cum_volume",
    "cum_amount",
] + ORDERBOOK_COLS

STANDARD_COLUMNS = [
    "date",
    "dt",
    "local_time_ms",
    "server_time",
    "price",
    "open",
    "high",
    "low",
    "prev_close",
    "cum_volume",
    "cum_amount",
    "tick_vol",
    "tick_amt",
] + ORDERBOOK_COLS + ["signal", "is_realtime", "is_tick_history", "source_file"]


@dataclass(frozen=True)
class MarketDataBundle:
    frame: pd.DataFrame
    warnings: list[str]
    files: list[Path]


def _date_from_path(path: Path) -> str:
    prefix = "sz002796-"
    if not path.stem.startswith(prefix):
        raise ValueError(f"unexpected market data file name: {path.name}")
    return path.stem[len(prefix) :]


def _round_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column in NUMERIC_COLS:
        if column not in df.columns:
            df[column] = 0
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    return df


def _drop_backsteps(df: pd.DataFrame, date_str: str, warnings: list[str]) -> pd.DataFrame:
    total_removed = 0
    while True:
        vol_backstep = df["cum_volume"].diff().fillna(0.0) < 0
        amt_backstep = df["cum_amount"].diff().fillna(0.0) < 0
        backstep = vol_backstep | amt_backstep
        removed = int(backstep.sum())
        if removed == 0:
            break
        total_removed += removed
        df = df.loc[~backstep].copy()
    if total_removed:
        warnings.append(f"{date_str}: removed {total_removed} cumulative backstep rows")
    return df


def _load_one_file(path: Path, warnings: list[str]) -> pd.DataFrame:
    date_str = _date_from_path(path)
    df = pd.read_csv(path, encoding="utf-8-sig")
    original_columns = set(df.columns)

    if "server_time" not in df.columns:
        raise ValueError(f"{path.name}: missing server_time column")

    realtime = "local_time_ms" in original_columns and ("bp1" in original_columns or "sp1" in original_columns)
    tick_history = not realtime and len(df) > 1000
    df["date"] = date_str
    df["server_time"] = df["server_time"].astype(str).str.strip()
    df["dt"] = pd.to_datetime(df["date"] + " " + df["server_time"], errors="coerce")
    df["is_realtime"] = bool(realtime)
    df["is_tick_history"] = bool(tick_history)
    df["source_file"] = path.name
    if "signal" not in df.columns:
        df["signal"] = "HOLD"

    df = _round_numeric_columns(df)
    invalid_dt = int(df["dt"].isna().sum())
    if invalid_dt:
        warnings.append(f"{date_str}: dropped {invalid_dt} rows with invalid timestamps")
        df = df.loc[df["dt"].notna()].copy()

    non_positive_price = int((df["price"] <= 0).sum())
    if non_positive_price:
        raise ValueError(f"{path.name}: {non_positive_price} rows have non-positive price")

    df = df.sort_values(["dt", "local_time_ms", "cum_volume", "cum_amount"]).reset_index(drop=True)
    duplicated_dt = int(df["dt"].duplicated().sum())
    if duplicated_dt:
        warnings.append(f"{date_str}: normalized {duplicated_dt} duplicate timestamps")
        df = (
            df.sort_values(["dt", "cum_volume", "cum_amount", "local_time_ms"])
            .drop_duplicates(subset=["dt"], keep="last")
            .sort_values(["dt", "local_time_ms"])
            .reset_index(drop=True)
        )

    df = _drop_backsteps(df, date_str, warnings)
    df["tick_vol"] = df["cum_volume"].diff().fillna(df["cum_volume"]).clip(lower=0.0)
    df["tick_amt"] = df["cum_amount"].diff().fillna(df["cum_amount"]).clip(lower=0.0)

    jumps = int((df["price"].pct_change().abs() > PRICE_JUMP_THRESHOLD).sum())
    if jumps:
        threshold_pct = PRICE_JUMP_THRESHOLD * 100
        warnings.append(f"{date_str}: {jumps} price jumps above {threshold_pct:.0f}% between samples")

    return df[STANDARD_COLUMNS]


def load_market_data(
    start_date: str = "2026-01-05",
    end_date: str | None = None,
    data_dir: str | Path = DATA_DIR,
) -> MarketDataBundle:
    data_path = Path(data_dir)
    files = []
    for path in sorted(data_path.glob("sz002796-*.csv")):
        date_str = _date_from_path(path)
        if date_str < start_date:
            continue
        if end_date is not None and date_str > end_date:
            continue
        files.append(path)

    if not files:
        raise FileNotFoundError(f"no sz002796 CSV files found in {data_path} from {start_date}")

    warnings: list[str] = []
    frames = [_load_one_file(path, warnings) for path in files]
    frame = (
        pd.concat(frames, ignore_index=True)
        .sort_values(["dt", "local_time_ms"])
        .reset_index(drop=True)
    )
    frame["tick_vol"] = frame.groupby("date", sort=False)["cum_volume"].diff().fillna(frame["cum_volume"]).clip(lower=0.0)
    frame["tick_amt"] = frame.groupby("date", sort=False)["cum_amount"].diff().fillna(frame["cum_amount"]).clip(lower=0.0)
    return MarketDataBundle(frame=frame[STANDARD_COLUMNS], warnings=warnings, files=files)


def row_to_tick(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    value = row.get("dt")
    if hasattr(value, "to_pydatetime"):
        dt_value = value.to_pydatetime()
    else:
        dt_value = value

    tick = {
        "Time": dt_value,
        "dt": dt_value,
        "server_time": row.get("server_time", ""),
        "local_time_ms": int(float(row.get("local_time_ms", 0) or 0)),
        "Close": float(row.get("price", 0.0) or 0.0),
        "price": float(row.get("price", 0.0) or 0.0),
        "open": float(row.get("open", 0.0) or 0.0),
        "high": float(row.get("high", 0.0) or 0.0),
        "low": float(row.get("low", 0.0) or 0.0),
        "prev_close": float(row.get("prev_close", 0.0) or 0.0),
        "Volume": float(row.get("cum_volume", 0.0) or 0.0),
        "Amount": float(row.get("cum_amount", 0.0) or 0.0),
        "cum_volume": float(row.get("cum_volume", 0.0) or 0.0),
        "cum_amount": float(row.get("cum_amount", 0.0) or 0.0),
        "tick_vol": float(row.get("tick_vol", 0.0) or 0.0),
        "tick_amt": float(row.get("tick_amt", 0.0) or 0.0),
        "_is_realtime": bool(row.get("is_realtime", False)),
        "_is_tick_history": bool(row.get("is_tick_history", False)),
    }
    for column in ORDERBOOK_COLS:
        tick[column] = float(row.get(column, 0.0) or 0.0)
    return tick
