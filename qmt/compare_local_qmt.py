"""Compare local CSV market data with miniQMT historical tick data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from qmt.adapter import TICK_FIELDS, iter_qmt_strategy_ticks
from qmt.config import OUTPUT_ROOT, TARGET_SYMBOL
from qmt.xtquant_env import import_xtdata, xtquant_compatibility, ensure_xtquant_path
from sz002796.market_data import DATA_DIR as LOCAL_DATA_DIR, load_market_data


def _date_text(value: Any) -> str:
    return str(value)[:10]


def _safe_pct_diff(left: float, right: float) -> float | None:
    if right == 0:
        return None
    return left / right - 1.0


def _orderbook_valid_ratio(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    cols = [col for col in ("bp1", "sp1") if col in df.columns]
    if not cols:
        return 0.0
    valid = False
    for col in cols:
        valid = valid | (pd.to_numeric(df[col], errors="coerce").fillna(0.0) > 0)
    return float(valid.mean())


def _aggregate_qmt_minutes(qmt_df: pd.DataFrame) -> pd.DataFrame:
    df = qmt_df.copy()
    df["minute"] = df["dt"].dt.floor("min")
    grouped = df.groupby("minute", sort=True)
    return pd.DataFrame(
        {
            "qmt_open": grouped["price"].first(),
            "qmt_high": grouped["price"].max(),
            "qmt_low": grouped["price"].min(),
            "qmt_close": grouped["price"].last(),
            "qmt_tick_vol": grouped["tick_vol"].sum(),
            "qmt_tick_amt": grouped["tick_amt"].sum(),
            "qmt_cum_volume": grouped["cum_volume"].last(),
            "qmt_cum_amount": grouped["cum_amount"].last(),
            "qmt_rows": grouped.size(),
        }
    )


def _aggregate_local_minutes(local_df: pd.DataFrame) -> pd.DataFrame:
    df = local_df.copy()
    df["minute"] = pd.to_datetime(df["dt"]).dt.floor("min")
    grouped = df.groupby("minute", sort=True)
    is_realtime = bool(df["is_realtime"].any()) if "is_realtime" in df.columns else False
    if is_realtime:
        high = grouped["price"].max()
        low = grouped["price"].min()
        open_price = grouped["price"].first()
    else:
        high = grouped["high"].max()
        low = grouped["low"].min()
        open_price = grouped["open"].first()
    return pd.DataFrame(
        {
            "local_open": open_price,
            "local_high": high,
            "local_low": low,
            "local_close": grouped["price"].last(),
            "local_tick_vol": grouped["tick_vol"].sum(),
            "local_tick_amt": grouped["tick_amt"].sum(),
            "local_cum_volume": grouped["cum_volume"].last(),
            "local_cum_amount": grouped["cum_amount"].last(),
            "local_rows": grouped.size(),
        }
    )


def _load_qmt_ticks(symbol: str, start_time: str, end_time: str, download: bool) -> pd.DataFrame:
    compatibility = xtquant_compatibility(ensure_xtquant_path())
    if not compatibility["matching_native_module"]:
        raise RuntimeError(
            "current Python cannot load miniQMT xtquant native module; "
            f"python={compatibility['python_version']} expected={compatibility['expected_native_tag']}"
        )
    xtdata = import_xtdata()
    try:
        xtdata.enable_hello = False
    except Exception:
        pass
    if download:
        xtdata.download_history_data2([symbol], period="tick", start_time=start_time, end_time=end_time)
    data = xtdata.get_market_data_ex(
        TICK_FIELDS,
        [symbol],
        period="tick",
        start_time=start_time,
        end_time=end_time,
    )
    if not data or symbol not in data:
        raise RuntimeError(f"no QMT tick data returned for {symbol}")
    rows = list(iter_qmt_strategy_ticks(data[symbol]))
    if not rows:
        raise RuntimeError(f"QMT tick data for {symbol} had no usable positive-price rows")
    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["dt"])
    df["date"] = df["dt"].dt.strftime("%Y-%m-%d")
    return df.sort_values("dt").reset_index(drop=True)


def _compare_one_day(local_day: pd.DataFrame, qmt_day: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    date = _date_text(local_day.iloc[0]["date"])
    local_format = "realtime_orderbook" if bool(local_day["is_realtime"].any()) else "minute_csv"
    qmt_minutes = _aggregate_qmt_minutes(qmt_day)
    local_minutes = _aggregate_local_minutes(local_day)
    joined = local_minutes.join(qmt_minutes, how="inner")

    close_diff = joined["local_close"] - joined["qmt_close"] if not joined.empty else pd.Series(dtype=float)
    high_diff = joined["local_high"] - joined["qmt_high"] if not joined.empty else pd.Series(dtype=float)
    low_diff = joined["local_low"] - joined["qmt_low"] if not joined.empty else pd.Series(dtype=float)
    vol_diff = joined["local_tick_vol"] - joined["qmt_tick_vol"] if not joined.empty else pd.Series(dtype=float)

    local_last = local_day.iloc[-1]
    qmt_last = qmt_day.iloc[-1]
    summary = {
        "date": date,
        "local_format": local_format,
        "local_rows": int(len(local_day)),
        "qmt_rows": int(len(qmt_day)),
        "matched_minutes": int(len(joined)),
        "local_first_time": str(local_day.iloc[0]["dt"]),
        "qmt_first_time": str(qmt_day.iloc[0]["dt"]),
        "local_last_time": str(local_last["dt"]),
        "qmt_last_time": str(qmt_last["dt"]),
        "local_first_price": float(local_day.iloc[0]["price"]),
        "qmt_first_price": float(qmt_day.iloc[0]["price"]),
        "local_last_price": float(local_last["price"]),
        "qmt_last_price": float(qmt_last["price"]),
        "last_price_diff": float(local_last["price"] - qmt_last["price"]),
        "last_cum_volume_diff_pct": _safe_pct_diff(float(local_last["cum_volume"]), float(qmt_last["cum_volume"])),
        "last_cum_amount_diff_pct": _safe_pct_diff(float(local_last["cum_amount"]), float(qmt_last["cum_amount"])),
        "local_orderbook_valid_ratio": _orderbook_valid_ratio(local_day),
        "qmt_orderbook_valid_ratio": _orderbook_valid_ratio(qmt_day),
        "minute_close_mae": float(close_diff.abs().mean()) if not close_diff.empty else None,
        "minute_close_max_abs": float(close_diff.abs().max()) if not close_diff.empty else None,
        "minute_high_mae": float(high_diff.abs().mean()) if not high_diff.empty else None,
        "minute_low_mae": float(low_diff.abs().mean()) if not low_diff.empty else None,
        "minute_tick_vol_mae": float(vol_diff.abs().mean()) if not vol_diff.empty else None,
    }

    minute_detail = pd.DataFrame()
    if not joined.empty:
        minute_detail = pd.DataFrame(
            {
                "date": date,
                "minute": joined.index.astype(str),
                "local_price": joined["local_close"].astype(float).values,
                "qmt_close": joined["qmt_close"].astype(float).values,
                "close_diff": close_diff.astype(float).values,
                "local_high": joined["local_high"].astype(float).values,
                "qmt_high": joined["qmt_high"].astype(float).values,
                "local_low": joined["local_low"].astype(float).values,
                "qmt_low": joined["qmt_low"].astype(float).values,
                "local_tick_vol": joined["local_tick_vol"].astype(float).values,
                "qmt_tick_vol": joined["qmt_tick_vol"].astype(float).values,
                "qmt_rows": joined["qmt_rows"].astype(int).values,
            }
        )
    return summary, minute_detail


def compare_local_and_qmt(
    symbol: str = TARGET_SYMBOL,
    start_time: str = "20260323",
    end_time: str = "",
    local_data_dir: str | Path = LOCAL_DATA_DIR,
    output_dir: str | Path | None = None,
    download: bool = False,
) -> dict[str, Any]:
    qmt_df = _load_qmt_ticks(symbol, start_time, end_time, download=download)
    if qmt_df.empty:
        raise RuntimeError(f"no QMT tick data returned for {symbol}")
    start_date = qmt_df["date"].min()
    end_date = qmt_df["date"].max()
    local_bundle = load_market_data(start_date=start_date, end_date=end_date, data_dir=local_data_dir)
    local_df = local_bundle.frame.copy()
    local_df["date"] = local_df["date"].astype(str)
    local_df["dt"] = pd.to_datetime(local_df["dt"])

    local_dates = set(local_df["date"])
    qmt_dates = set(qmt_df["date"])
    overlap_dates = sorted(local_dates & qmt_dates)
    daily: list[dict[str, Any]] = []
    minute_details: list[pd.DataFrame] = []

    for date in overlap_dates:
        local_day = local_df.loc[local_df["date"] == date].sort_values("dt")
        qmt_day = qmt_df.loc[qmt_df["date"] == date].sort_values("dt")
        one_day, detail = _compare_one_day(local_day, qmt_day)
        daily.append(one_day)
        if not detail.empty:
            minute_details.append(detail)

    daily_df = pd.DataFrame(daily)
    output_path = Path(output_dir) if output_dir else OUTPUT_ROOT.parent / "analysis" / f"local_qmt_compare_{start_date}_to_{end_date}"
    output_path.mkdir(parents=True, exist_ok=True)
    daily_csv = output_path / "daily_comparison.csv"
    minute_csv = output_path / "minute_mismatches.csv"
    summary_path = output_path / "summary.json"

    daily_df.to_csv(daily_csv, index=False, encoding="utf-8-sig")
    if minute_details:
        minute_df = pd.concat(minute_details, ignore_index=True)
        minute_df["abs_close_diff"] = minute_df["close_diff"].abs()
        minute_df.sort_values("abs_close_diff", ascending=False).head(500).to_csv(
            minute_csv,
            index=False,
            encoding="utf-8-sig",
        )
    else:
        pd.DataFrame().to_csv(minute_csv, index=False, encoding="utf-8-sig")

    summary = {
        "symbol": symbol,
        "qmt_start_time": str(qmt_df.iloc[0]["dt"]),
        "qmt_end_time": str(qmt_df.iloc[-1]["dt"]),
        "qmt_rows": int(len(qmt_df)),
        "local_rows": int(len(local_df)),
        "overlap_days": int(len(overlap_dates)),
        "local_only_days": sorted(local_dates - qmt_dates),
        "qmt_only_days": sorted(qmt_dates - local_dates),
        "local_data_warnings": local_bundle.warnings,
        "local_orderbook_days": int((daily_df.get("local_orderbook_valid_ratio", pd.Series(dtype=float)) > 0).sum()),
        "qmt_orderbook_days": int((daily_df.get("qmt_orderbook_valid_ratio", pd.Series(dtype=float)) > 0).sum()),
        "median_minute_close_mae": float(daily_df["minute_close_mae"].median()) if not daily_df.empty else None,
        "max_daily_minute_close_max_abs": float(daily_df["minute_close_max_abs"].max()) if not daily_df.empty else None,
        "median_last_cum_volume_diff_pct": float(daily_df["last_cum_volume_diff_pct"].median()) if not daily_df.empty else None,
        "median_last_cum_amount_diff_pct": float(daily_df["last_cum_amount_diff_pct"].median()) if not daily_df.empty else None,
        "outputs": {
            "daily_comparison_csv": str(daily_csv),
            "minute_mismatches_csv": str(minute_csv),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare local CSV data with QMT historical tick data.")
    parser.add_argument("--symbol", default=TARGET_SYMBOL)
    parser.add_argument("--start-time", default="20260323")
    parser.add_argument("--end-time", default="")
    parser.add_argument("--local-data-dir", default=str(LOCAL_DATA_DIR))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()

    summary = compare_local_and_qmt(
        symbol=args.symbol,
        start_time=args.start_time,
        end_time=args.end_time,
        local_data_dir=args.local_data_dir,
        output_dir=args.output_dir or None,
        download=args.download,
    )
    print(f"qmt_rows={summary['qmt_rows']} local_rows={summary['local_rows']} overlap_days={summary['overlap_days']}")
    print(f"local_orderbook_days={summary['local_orderbook_days']} qmt_orderbook_days={summary['qmt_orderbook_days']}")
    print(f"median_minute_close_mae={summary['median_minute_close_mae']}")
    print(f"daily={summary['outputs']['daily_comparison_csv']}")
    print(f"mismatches={summary['outputs']['minute_mismatches_csv']}")
    print(f"summary={summary['outputs']['summary_json']}")


if __name__ == "__main__":
    main()
