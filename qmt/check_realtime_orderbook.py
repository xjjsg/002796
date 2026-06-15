"""Check whether live miniQMT tick subscription contains orderbook fields."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt.adapter import QmtTickNormalizer
from qmt.config import OUTPUT_ROOT, TARGET_SYMBOL
from qmt.xtquant_env import ensure_xtquant_path, import_xtdata, xtquant_compatibility


def _parse_clock(text: str) -> tuple[int, int, int]:
    parts = str(text).replace(".", ":").split(":")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3:
        raise ValueError("start time must look like HH:MM, HH.MM, or HH:MM:SS")
    hour, minute, second = (int(part) for part in parts)
    if hour == 1:
        hour = 13
    return hour, minute, second


def _target_datetime(start_at: str) -> datetime:
    hour, minute, second = _parse_clock(start_at)
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    return target


def _wait_until(target: datetime, max_wait_seconds: int | None = None) -> None:
    while True:
        now = datetime.now()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        if max_wait_seconds is not None and remaining > max_wait_seconds:
            raise TimeoutError(f"target start time {target} is more than {max_wait_seconds}s away")
        sleep_seconds = min(remaining, 30.0)
        print(f"waiting for {target:%Y-%m-%d %H:%M:%S}, {remaining:.0f}s remaining")
        time.sleep(sleep_seconds)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _iter_payload_rows(payload: Any, symbol: str):
    data = payload.get(symbol) if isinstance(payload, dict) else payload
    if data is None:
        return
    if hasattr(data, "iterrows"):
        for index, row in data.iterrows():
            yield row, index
        return
    if isinstance(data, (list, tuple)):
        for row in data:
            yield row, None
        return
    if isinstance(data, dict):
        if any(key in data for key in ("lastPrice", "time", "askPrice1", "bidPrice1")):
            yield data, None
        else:
            for row in data.values():
                yield row, None


def check_realtime_orderbook(
    symbol: str = TARGET_SYMBOL,
    start_at: str = "13:00:00",
    duration_seconds: int = 60,
    output_dir: str | Path | None = None,
    max_wait_seconds: int | None = None,
) -> dict[str, Any]:
    compatibility = xtquant_compatibility(ensure_xtquant_path())
    if not compatibility["matching_native_module"]:
        raise RuntimeError(
            "current Python cannot load miniQMT xtquant native module; "
            f"python={compatibility['python_version']} expected={compatibility['expected_native_tag']}"
        )

    target = _target_datetime(start_at)
    if datetime.now() < target:
        _wait_until(target, max_wait_seconds=max_wait_seconds)
    else:
        print(f"start time {target:%Y-%m-%d %H:%M:%S} has passed; starting immediately")

    xtdata = import_xtdata()
    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    normalizer = QmtTickNormalizer()
    stats = {
        "symbol": symbol,
        "requested_start_at": start_at,
        "actual_start_time": datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": duration_seconds,
        "callback_count": 0,
        "tick_count": 0,
        "positive_price_count": 0,
        "bid_ask_price_valid_count": 0,
        "bid_ask_price_and_volume_valid_count": 0,
        "first_tick_time": None,
        "last_tick_time": None,
        "last_price": None,
        "last_bp1": None,
        "last_bv1": None,
        "last_sp1": None,
        "last_sv1": None,
        "samples": [],
        "raw_samples": [],
    }

    def on_quote(payload):
        stats["callback_count"] += 1
        if len(stats["raw_samples"]) < 3:
            stats["raw_samples"].append(_jsonable(payload))
        for row, index in _iter_payload_rows(payload, symbol) or []:
            tick = normalizer.normalize(row, index=index)
            stats["tick_count"] += 1
            if tick["price"] > 0:
                stats["positive_price_count"] += 1
            price_valid = tick["bp1"] > 0 and tick["sp1"] > 0
            full_valid = price_valid and tick["bv1"] > 0 and tick["sv1"] > 0
            if price_valid:
                stats["bid_ask_price_valid_count"] += 1
            if full_valid:
                stats["bid_ask_price_and_volume_valid_count"] += 1
            if tick["Time"] is not None:
                text_time = tick["Time"].isoformat(sep=" ", timespec="seconds")
                stats["first_tick_time"] = stats["first_tick_time"] or text_time
                stats["last_tick_time"] = text_time
            stats["last_price"] = tick["price"]
            stats["last_bp1"] = tick["bp1"]
            stats["last_bv1"] = tick["bv1"]
            stats["last_sp1"] = tick["sp1"]
            stats["last_sv1"] = tick["sv1"]
            if len(stats["samples"]) < 20:
                stats["samples"].append(
                    {
                        "time": tick["Time"].isoformat(sep=" ", timespec="seconds") if tick["Time"] else None,
                        "price": tick["price"],
                        "bp1": tick["bp1"],
                        "bv1": tick["bv1"],
                        "sp1": tick["sp1"],
                        "sv1": tick["sv1"],
                        "has_bid_ask_price": price_valid,
                        "has_bid_ask_price_and_volume": full_valid,
                    }
                )

    print(f"subscribing {symbol} tick quotes for {duration_seconds}s")
    seq = xtdata.subscribe_quote(symbol, period="tick", callback=on_quote)
    stats["subscribe_seq"] = seq
    try:
        deadline = time.time() + duration_seconds
        while time.time() < deadline:
            time.sleep(0.5)
    finally:
        try:
            xtdata.unsubscribe_quote(seq)
        except Exception as exc:
            stats["unsubscribe_error"] = repr(exc)

    stats["actual_end_time"] = datetime.now().isoformat(timespec="seconds")
    tick_count = max(1, int(stats["tick_count"]))
    stats["bid_ask_price_valid_ratio"] = stats["bid_ask_price_valid_count"] / tick_count
    stats["bid_ask_price_and_volume_valid_ratio"] = stats["bid_ask_price_and_volume_valid_count"] / tick_count
    stats["has_realtime_orderbook"] = stats["bid_ask_price_valid_count"] > 0

    output_path = Path(output_dir) if output_dir else OUTPUT_ROOT.parent / "analysis" / "realtime_orderbook"
    output_path.mkdir(parents=True, exist_ok=True)
    filename_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_path / f"realtime_orderbook_{symbol.replace('.', '_')}_{filename_time}.json"
    report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    stats["report_path"] = str(report_path)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Check realtime QMT tick orderbook availability.")
    parser.add_argument("--symbol", default=TARGET_SYMBOL)
    parser.add_argument("--start-at", default="13:00:00", help="default 13:00:00; '1.30' is treated as 13:30")
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--max-wait-seconds", type=int, default=0)
    args = parser.parse_args()

    max_wait = args.max_wait_seconds if args.max_wait_seconds > 0 else None
    stats = check_realtime_orderbook(
        symbol=args.symbol,
        start_at=args.start_at,
        duration_seconds=args.duration_seconds,
        output_dir=args.output_dir or None,
        max_wait_seconds=max_wait,
    )
    print(json.dumps({k: v for k, v in stats.items() if k not in {"raw_samples"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
