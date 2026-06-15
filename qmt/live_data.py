"""Realtime miniQMT tick subscription utilities.

This module only handles market data. It does not know about accounts and it
does not send orders.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from .adapter import QmtTickNormalizer
from .config import TARGET_SYMBOL
from .xtquant_env import import_xtdata


def iter_realtime_payload_rows(payload: Any, symbol: str) -> Iterable[tuple[Any, Any]]:
    """Yield ``(row, index)`` pairs from the common xtdata callback shapes."""
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
        if any(key in data for key in ("time", "lastPrice", "price", "askPrice", "bidPrice")):
            yield data, None
            return
        for row in data.values():
            yield row, None


@dataclass
class RealtimeTickStats:
    symbol: str
    callback_count: int = 0
    tick_count: int = 0
    dropped_count: int = 0
    first_tick_time: str | None = None
    last_tick_time: str | None = None
    last_price: float = 0.0
    last_bp1: float = 0.0
    last_sp1: float = 0.0
    last_bv1: float = 0.0
    last_sv1: float = 0.0
    last_error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "callback_count": self.callback_count,
            "tick_count": self.tick_count,
            "dropped_count": self.dropped_count,
            "first_tick_time": self.first_tick_time,
            "last_tick_time": self.last_tick_time,
            "last_price": self.last_price,
            "last_bp1": self.last_bp1,
            "last_sp1": self.last_sp1,
            "last_bv1": self.last_bv1,
            "last_sv1": self.last_sv1,
            "last_error": self.last_error,
            "started_at": self.started_at,
        }


class RealtimeTickFeed:
    """Subscribe to realtime QMT ticks and expose normalized ticks via a queue."""

    def __init__(
        self,
        symbol: str = TARGET_SYMBOL,
        max_queue_size: int = 10000,
        on_tick: Callable[[dict[str, Any]], None] | None = None,
        xtdata: Any | None = None,
    ):
        self.symbol = symbol
        self.on_tick = on_tick
        self.xtdata = xtdata
        self.normalizer = QmtTickNormalizer()
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue_size)
        self.stats = RealtimeTickStats(symbol=symbol)
        self.subscription_seq: int | None = None
        self.latest_tick: dict[str, Any] | None = None
        self._lock = threading.RLock()

    @property
    def subscribed(self) -> bool:
        return self.subscription_seq is not None

    def start(self) -> int:
        if self.xtdata is None:
            self.xtdata = import_xtdata()
        try:
            self.xtdata.enable_hello = False
        except Exception:
            pass
        self.subscription_seq = self.xtdata.subscribe_quote(self.symbol, period="tick", callback=self._on_quote)
        return int(self.subscription_seq)

    def stop(self) -> None:
        if self.xtdata is None or self.subscription_seq is None:
            return
        try:
            self.xtdata.unsubscribe_quote(self.subscription_seq)
        finally:
            self.subscription_seq = None

    def wait_next(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def fetch_full_tick(self) -> dict[str, Any] | None:
        if self.xtdata is None:
            self.xtdata = import_xtdata()
        payload = self.xtdata.get_full_tick([self.symbol])
        for row, index in iter_realtime_payload_rows(payload, self.symbol):
            tick = self.normalizer.normalize(row, index=index)
            if tick["Time"] is not None and tick["price"] > 0:
                tick["_is_realtime"] = True
                return tick
        return None

    def _enqueue_tick(self, tick: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(tick)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.stats.dropped_count += 1
            self.queue.put_nowait(tick)

    def _record_tick(self, tick: dict[str, Any]) -> None:
        dt = tick.get("Time")
        if dt is not None:
            text = dt.isoformat(sep=" ", timespec="seconds")
            self.stats.first_tick_time = self.stats.first_tick_time or text
            self.stats.last_tick_time = text
        self.stats.tick_count += 1
        self.stats.last_price = float(tick.get("price", 0.0) or 0.0)
        self.stats.last_bp1 = float(tick.get("bp1", 0.0) or 0.0)
        self.stats.last_sp1 = float(tick.get("sp1", 0.0) or 0.0)
        self.stats.last_bv1 = float(tick.get("bv1", 0.0) or 0.0)
        self.stats.last_sv1 = float(tick.get("sv1", 0.0) or 0.0)
        self.latest_tick = tick

    def _on_quote(self, payload: Any) -> None:
        with self._lock:
            self.stats.callback_count += 1
            try:
                rows = list(iter_realtime_payload_rows(payload, self.symbol) or [])
                for row, index in rows:
                    tick = self.normalizer.normalize(row, index=index)
                    if tick["Time"] is None or tick["price"] <= 0:
                        continue
                    tick["_is_realtime"] = True
                    tick["_local_time_ms"] = int(time.time() * 1000)
                    self._record_tick(tick)
                    self._enqueue_tick(tick)
                    if self.on_tick is not None:
                        self.on_tick(tick)
            except Exception as exc:
                self.stats.last_error = repr(exc)
