"""Normalize QMT tick rows into the V6 strategy tick shape."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator


TICK_FIELDS = [
    "time",
    "lastPrice",
    "open",
    "high",
    "low",
    "lastClose",
    "amount",
    "volume",
    "askPrice1",
    "askPrice2",
    "askPrice3",
    "askPrice4",
    "askPrice5",
    "bidPrice1",
    "bidPrice2",
    "bidPrice3",
    "bidPrice4",
    "bidPrice5",
    "askVol1",
    "askVol2",
    "askVol3",
    "askVol4",
    "askVol5",
    "bidVol1",
    "bidVol2",
    "bidVol3",
    "bidVol4",
    "bidVol5",
]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _get(row: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = default
        found = False
        if isinstance(row, Mapping):
            if key in row:
                value = row[key]
                found = True
        elif hasattr(row, "get"):
            try:
                value = row.get(key, default)
                found = value is not default
            except Exception:
                found = False
        if not found:
            try:
                value = row[key]
                found = True
            except Exception:
                pass
        if not found and hasattr(row, key):
            value = getattr(row, key)
            found = True
        if found and not _is_missing(value):
            return value
    return default


def _get_level_value(
    row: Any,
    scalar_key: str,
    list_key: str,
    legacy_key: str,
    level: int,
    default: Any = None,
) -> tuple[Any, str]:
    value = _get(row, scalar_key, default=None)
    if not _is_missing(value):
        return value, "qmt_scalar"

    values = _get(row, list_key, default=None)
    if not _is_missing(values) and not isinstance(values, (str, bytes, Mapping)):
        index = level - 1
        try:
            value = values[index]
        except Exception:
            try:
                value = values.iloc[index]
            except Exception:
                value = default
        if not _is_missing(value):
            return value, "qmt_list"

    value = _get(row, legacy_key, default=None)
    if not _is_missing(value):
        return value, "legacy"

    return default, "missing"


def _float(value: Any, default: float = 0.0) -> float:
    if _is_missing(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_qmt_datetime(value: Any) -> datetime | None:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()

    if isinstance(value, (int, float)):
        number = int(value)
        text = str(number)
        if len(text) >= 14 and text.startswith(("19", "20")):
            return datetime.strptime(text[:14], "%Y%m%d%H%M%S")
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000.0)

    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14 and digits.startswith(("19", "20")):
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
    if len(digits) == 8 and digits.startswith(("19", "20")):
        return datetime.strptime(digits, "%Y%m%d")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class QmtTickNormalizer:
    """Stateful converter that also computes per-tick volume and amount."""

    volume_multiplier: float = 100.0
    orderbook_volume_multiplier: float = 100.0
    current_date: str | None = None
    last_raw_volume: float = 0.0
    last_raw_amount: float = 0.0

    def normalize(self, row: Any, index: Any = None) -> dict[str, Any]:
        dt = _parse_qmt_datetime(
            _get(row, "Time", "time", "stime", "timestamp", "dt", default=index)
        )
        price = _float(_get(row, "lastPrice", "price", "Close", "close"))
        open_price = _float(_get(row, "open", "Open"), price)
        high = _float(_get(row, "high", "High"), price)
        low = _float(_get(row, "low", "Low"), price)
        prev_close = _float(
            _get(row, "lastClose", "prevClose", "preClose", "prev_close"),
            0.0,
        )
        raw_volume = _float(_get(row, "volume", "Volume", "cum_volume"))
        volume = raw_volume * self.volume_multiplier
        amount = _float(_get(row, "amount", "Amount", "cum_amount"))

        date_text = dt.strftime("%Y-%m-%d") if dt else ""
        if date_text != self.current_date:
            self.current_date = date_text
            self.last_raw_volume = 0.0
            self.last_raw_amount = 0.0

        tick_vol = max(0.0, volume - self.last_raw_volume) if volume >= self.last_raw_volume else volume
        tick_amt = max(0.0, amount - self.last_raw_amount) if amount >= self.last_raw_amount else amount
        self.last_raw_volume = volume
        self.last_raw_amount = amount

        tick = {
            "Time": dt,
            "dt": dt,
            "server_time": dt.strftime("%H:%M:%S") if dt else "",
            "Close": price,
            "price": price,
            "open": open_price,
            "high": high,
            "low": low,
            "prev_close": prev_close,
            "Volume": volume,
            "Amount": amount,
            "cum_volume": volume,
            "cum_amount": amount,
            "qmt_raw_volume": raw_volume,
            "tick_vol": tick_vol,
            "tick_amt": tick_amt,
            "_is_realtime": True,
        }
        for level in range(1, 6):
            ask_price, _ = _get_level_value(row, f"askPrice{level}", "askPrice", f"sp{level}", level)
            bid_price, _ = _get_level_value(row, f"bidPrice{level}", "bidPrice", f"bp{level}", level)
            ask_vol, ask_vol_source = _get_level_value(row, f"askVol{level}", "askVol", f"sv{level}", level)
            bid_vol, bid_vol_source = _get_level_value(row, f"bidVol{level}", "bidVol", f"bv{level}", level)

            tick[f"sp{level}"] = _float(ask_price)
            tick[f"bp{level}"] = _float(bid_price)
            tick[f"sv{level}"] = _float(ask_vol)
            tick[f"bv{level}"] = _float(bid_vol)
            if ask_vol_source.startswith("qmt_"):
                tick[f"sv{level}"] *= self.orderbook_volume_multiplier
            if bid_vol_source.startswith("qmt_"):
                tick[f"bv{level}"] *= self.orderbook_volume_multiplier
        return tick


def iter_qmt_strategy_ticks(data: Any) -> Iterator[dict[str, Any]]:
    normalizer = QmtTickNormalizer()
    if hasattr(data, "iterrows"):
        for index, row in data.iterrows():
            tick = normalizer.normalize(row, index=index)
            if tick["Time"] is not None and tick["price"] > 0:
                yield tick
        return

    rows: Iterable[Any]
    if isinstance(data, Mapping):
        rows = data.values()
    else:
        rows = data
    for row in rows:
        tick = normalizer.normalize(row)
        if tick["Time"] is not None and tick["price"] > 0:
            yield tick


def latest_tick_from_qmt_payload(payload: Any, symbol: str) -> dict[str, Any] | None:
    data = payload.get(symbol) if isinstance(payload, Mapping) else payload
    if data is None:
        return None
    if hasattr(data, "tail"):
        if len(data) == 0:
            return None
        row = data.tail(1).iloc[0]
        index = data.tail(1).index[0]
        return QmtTickNormalizer().normalize(row, index=index)
    if isinstance(data, list) or isinstance(data, tuple):
        if not data:
            return None
        return QmtTickNormalizer().normalize(data[-1])
    if isinstance(data, Mapping) and data:
        return QmtTickNormalizer().normalize(data)
    return None
