"""Overwrite local sz002796 market CSVs with a QMT-enhanced data set.

The updater keeps a full backup before replacing market files. QMT historical
ticks are used where available; local realtime orderbook files are preserved by
default because miniQMT historical tick data does not include bid/ask depth.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from qmt.compare_local_qmt import _load_qmt_ticks
from qmt.config import START_TIME, TARGET_SYMBOL
from sz002796.market_data import DATA_DIR as LOCAL_DATA_DIR


MARKET_PREFIX = "sz002796-"
MARKET_SUFFIX = ".csv"
QMT_COLUMNS = [
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
]


def _date_from_market_path(path: Path) -> str:
    name = path.name
    if not (name.startswith(MARKET_PREFIX) and name.endswith(MARKET_SUFFIX)):
        raise ValueError(f"unexpected market file name: {name}")
    return name[len(MARKET_PREFIX) : -len(MARKET_SUFFIX)]


def _market_files(data_dir: Path) -> list[Path]:
    return sorted(data_dir.glob(f"{MARKET_PREFIX}*{MARKET_SUFFIX}"))


def _is_orderbook_file(path: Path) -> bool:
    try:
        columns = pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns
    except Exception:
        return False
    names = set(columns)
    return "local_time_ms" in names and ("bp1" in names or "sp1" in names)


def _default_end_time(data_dir: Path) -> str:
    files = _market_files(data_dir)
    if not files:
        return ""
    latest = max(_date_from_market_path(path) for path in files)
    return latest.replace("-", "") + "150000"


def _safe_backup_dir(data_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = data_dir.parent / f"{data_dir.name}_backup_{stamp}"
    suffix = 1
    while backup_path.exists():
        backup_path = data_dir.parent / f"{data_dir.name}_backup_{stamp}_{suffix}"
        suffix += 1
    return backup_path


def _write_qmt_day(path: Path, day: pd.DataFrame) -> None:
    output = pd.DataFrame(
        {
            "server_time": day["dt"].dt.strftime("%H:%M:%S"),
            "price": day["price"].astype(float),
            "open": day["open"].astype(float),
            "high": day["high"].astype(float),
            "low": day["low"].astype(float),
            "prev_close": day["prev_close"].astype(float),
            "cum_volume": day["cum_volume"].astype(float),
            "cum_amount": day["cum_amount"].astype(float),
            "tick_vol": day["tick_vol"].astype(float),
            "tick_amt": day["tick_amt"].astype(float),
        }
    )
    output.to_csv(path, index=False, columns=QMT_COLUMNS, encoding="utf-8-sig")


def build_updated_data(
    *,
    symbol: str = TARGET_SYMBOL,
    start_time: str = START_TIME,
    end_time: str,
    data_dir: str | Path = LOCAL_DATA_DIR,
    output_dir: str | Path,
    download: bool = False,
    preserve_local_orderbook: bool = True,
) -> dict[str, Any]:
    data_path = Path(data_dir).resolve()
    staging_path = Path(output_dir).resolve()
    staging_path.mkdir(parents=True, exist_ok=True)

    local_files = _market_files(data_path)
    local_by_date = {_date_from_market_path(path): path for path in local_files}
    local_orderbook_dates = {
        date for date, path in local_by_date.items() if _is_orderbook_file(path)
    }

    qmt_df = _load_qmt_ticks(symbol, start_time, end_time, download=download)
    if qmt_df.empty:
        raise RuntimeError(f"no QMT tick data returned for {symbol}")
    qmt_dates = sorted(str(date) for date in qmt_df["date"].unique())

    selected_dates = sorted(set(local_by_date) | set(qmt_dates))
    qmt_written_dates: list[str] = []
    local_copied_dates: list[str] = []
    local_orderbook_preserved_dates: list[str] = []
    local_only_preserved_dates: list[str] = []

    for date in selected_dates:
        destination = staging_path / f"{MARKET_PREFIX}{date}{MARKET_SUFFIX}"
        has_qmt = date in qmt_dates
        has_local = date in local_by_date
        preserve_orderbook = (
            preserve_local_orderbook and date in local_orderbook_dates and has_local
        )
        if preserve_orderbook:
            shutil.copy2(local_by_date[date], destination)
            local_copied_dates.append(date)
            local_orderbook_preserved_dates.append(date)
            continue
        if has_qmt:
            day = qmt_df.loc[qmt_df["date"] == date].sort_values("dt")
            _write_qmt_day(destination, day)
            qmt_written_dates.append(date)
            continue
        if has_local:
            shutil.copy2(local_by_date[date], destination)
            local_copied_dates.append(date)
            local_only_preserved_dates.append(date)

    return {
        "symbol": symbol,
        "data_dir": str(data_path),
        "staging_dir": str(staging_path),
        "start_time": start_time,
        "end_time": end_time,
        "download": bool(download),
        "qmt_start_time": str(qmt_df.iloc[0]["dt"]),
        "qmt_end_time": str(qmt_df.iloc[-1]["dt"]),
        "qmt_rows": int(len(qmt_df)),
        "qmt_dates": qmt_dates,
        "local_dates_before": sorted(local_by_date),
        "local_orderbook_dates_before": sorted(local_orderbook_dates),
        "market_dates_after": selected_dates,
        "qmt_written_dates": qmt_written_dates,
        "local_copied_dates": local_copied_dates,
        "local_orderbook_preserved_dates": local_orderbook_preserved_dates,
        "local_only_preserved_dates": local_only_preserved_dates,
        "market_file_count_after": len(selected_dates),
        "preserve_local_orderbook": bool(preserve_local_orderbook),
    }


def apply_update(data_dir: str | Path, staging_dir: str | Path) -> dict[str, Any]:
    data_path = Path(data_dir).resolve()
    staging_path = Path(staging_dir).resolve()
    if not data_path.exists() or not data_path.is_dir():
        raise FileNotFoundError(f"data directory does not exist: {data_path}")
    if data_path.name != "sz002796" or data_path.parent.name != "data":
        raise ValueError(f"refusing to overwrite unexpected data directory: {data_path}")
    staged_market_files = _market_files(staging_path)
    if not staged_market_files:
        raise RuntimeError(f"no staged market files found in {staging_path}")

    backup_path = _safe_backup_dir(data_path)
    shutil.copytree(data_path, backup_path)

    removed = 0
    for path in _market_files(data_path):
        path.unlink()
        removed += 1

    copied = 0
    for path in staged_market_files:
        shutil.copy2(path, data_path / path.name)
        copied += 1

    return {
        "backup_dir": str(backup_path),
        "removed_market_files": removed,
        "copied_market_files": copied,
    }


def update_local_data(
    *,
    symbol: str = TARGET_SYMBOL,
    start_time: str = START_TIME,
    end_time: str | None = None,
    data_dir: str | Path = LOCAL_DATA_DIR,
    output_dir: str | Path | None = None,
    download: bool = False,
    preserve_local_orderbook: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    data_path = Path(data_dir).resolve()
    effective_end_time = end_time or _default_end_time(data_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = (
        Path(output_dir).resolve()
        if output_dir
        else data_path.parents[1] / "qmt" / "analysis" / f"update_local_data_{stamp}"
    )
    staging_dir = run_dir / "staging"

    summary = build_updated_data(
        symbol=symbol,
        start_time=start_time,
        end_time=effective_end_time,
        data_dir=data_path,
        output_dir=staging_dir,
        download=download,
        preserve_local_orderbook=preserve_local_orderbook,
    )
    summary["run_dir"] = str(run_dir)
    summary["applied"] = False
    if apply:
        summary.update(apply_update(data_path, staging_dir))
        summary["applied"] = True

    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    summary["summary_json"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Overwrite data/sz002796 with QMT-enhanced market CSVs.")
    parser.add_argument("--symbol", default=TARGET_SYMBOL)
    parser.add_argument("--start-time", default=START_TIME)
    parser.add_argument("--end-time", default="")
    parser.add_argument("--data-dir", default=str(LOCAL_DATA_DIR))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--replace-orderbook-days", action="store_true")
    parser.add_argument("--apply", action="store_true", help="actually overwrite data/sz002796")
    args = parser.parse_args()

    summary = update_local_data(
        symbol=args.symbol,
        start_time=args.start_time,
        end_time=args.end_time or None,
        data_dir=args.data_dir,
        output_dir=args.output_dir or None,
        download=args.download,
        preserve_local_orderbook=not args.replace_orderbook_days,
        apply=args.apply,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
