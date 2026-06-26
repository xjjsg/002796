"""Realtime market-data source adapters for the web runtime."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Callable

from .fetcher import TencentFetcher


@dataclass(frozen=True)
class MarketSourceOption:
    source_id: str
    label: str
    detail: str


MARKET_SOURCE_OPTIONS: dict[str, MarketSourceOption] = {
    "tencent": MarketSourceOption("tencent", "现有接口", "Tencent realtime quote API"),
    "qmt": MarketSourceOption("qmt", "QMT", "miniQMT realtime tick subscription"),
}


def normalize_market_source_id(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "tencent"
    for source_id, option in MARKET_SOURCE_OPTIONS.items():
        if text in {source_id, option.label.lower()}:
            return source_id
    aliases = {
        "腾讯": "tencent",
        "腾讯接口": "tencent",
        "现有": "tencent",
        "当前接口": "tencent",
        "miniqmt": "qmt",
        "xtquant": "qmt",
    }
    return aliases.get(text, "tencent")


def market_source_label(source_id: str) -> str:
    option = MARKET_SOURCE_OPTIONS.get(normalize_market_source_id(source_id))
    return option.label if option else str(source_id)


def symbol_to_qmt_symbol(symbol: str) -> str:
    text = str(symbol or "").strip()
    upper = text.upper()
    if upper.endswith((".SZ", ".SH")):
        return upper
    if upper.startswith("SZ") and len(upper) == 8:
        return f"{upper[2:]}.SZ"
    if upper.startswith("SH") and len(upper) == 8:
        return f"{upper[2:]}.SH"
    return upper


class RealtimeMarketSource:
    source_id = ""
    label = ""

    async def start(self) -> None:
        return None

    async def fetch_initial_tick(self) -> dict[str, Any] | None:
        return await self.fetch()

    async def fetch(self) -> dict[str, Any] | None:
        raise NotImplementedError

    async def reset_stale_guard(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @property
    def active_source_id(self) -> str:
        return self.source_id

    @property
    def active_label(self) -> str:
        return self.label

    def pop_status_events(self) -> list[str]:
        return []

    def _mark_tick(self, tick: dict[str, Any] | None) -> dict[str, Any] | None:
        if tick is None:
            return None
        tick["market_source"] = self.source_id
        tick["market_source_label"] = self.label
        return tick


class TencentMarketDataSource(RealtimeMarketSource):
    source_id = "tencent"
    label = MARKET_SOURCE_OPTIONS[source_id].label

    def __init__(self, symbol: str, fetcher: TencentFetcher | None = None):
        self.symbol = symbol
        self.fetcher = fetcher or TencentFetcher(symbol)
        self.session: Any | None = None

    async def start(self) -> None:
        if self.session is not None:
            return
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError("缺少 aiohttp 依赖，无法使用现有接口") from exc
        self.session = aiohttp.ClientSession()

    async def fetch_initial_tick(self) -> dict[str, Any] | None:
        tick = await self.fetch()
        await self.reset_stale_guard()
        return tick

    async def fetch(self) -> dict[str, Any] | None:
        if self.session is None:
            await self.start()
        tick = await self.fetcher.fetch(self.session)
        return self._mark_tick(tick)

    async def reset_stale_guard(self) -> None:
        if hasattr(self.fetcher, "last_server_ts"):
            self.fetcher.last_server_ts = None

    async def close(self) -> None:
        if self.session is not None:
            session = self.session
            self.session = None
            await session.close()


class QmtMarketDataSource(RealtimeMarketSource):
    source_id = "qmt"
    label = MARKET_SOURCE_OPTIONS[source_id].label

    def __init__(
        self,
        symbol: str,
        *,
        feed: Any | None = None,
        feed_factory: Callable[[str], Any] | None = None,
        queue_timeout_seconds: float = 0.2,
    ):
        self.symbol = symbol_to_qmt_symbol(symbol)
        self.feed = feed
        self.feed_factory = feed_factory
        self.queue_timeout_seconds = queue_timeout_seconds
        self.started = False

    def _ensure_feed(self) -> Any:
        if self.feed is None:
            if self.feed_factory is not None:
                self.feed = self.feed_factory(self.symbol)
            else:
                from qmt.live_data import RealtimeTickFeed

                self.feed = RealtimeTickFeed(symbol=self.symbol)
        return self.feed

    async def start(self) -> None:
        if self.started:
            return
        feed = self._ensure_feed()
        await asyncio.to_thread(feed.start)
        self.started = True

    async def fetch_initial_tick(self) -> dict[str, Any] | None:
        feed = self._ensure_feed()
        if hasattr(feed, "fetch_full_tick"):
            tick = await asyncio.to_thread(feed.fetch_full_tick)
            self._raise_if_feed_error()
            marked = self._mark_tick(tick)
            if marked is not None:
                return marked
        return await self.fetch()

    async def fetch(self) -> dict[str, Any] | None:
        if not self.started:
            await self.start()
        feed = self._ensure_feed()
        tick = await asyncio.to_thread(feed.wait_next, timeout=self.queue_timeout_seconds)
        self._raise_if_feed_error()
        if tick is None:
            return None

        latest = tick
        while True:
            next_tick = feed.wait_next(timeout=0)
            if next_tick is None:
                break
            latest = next_tick
        return self._mark_tick(latest)

    async def close(self) -> None:
        if self.feed is not None and hasattr(self.feed, "stop"):
            await asyncio.to_thread(self.feed.stop)
        self.started = False

    def _raise_if_feed_error(self) -> None:
        stats = getattr(self.feed, "stats", None)
        last_error = getattr(stats, "last_error", None)
        if last_error:
            raise RuntimeError(f"QMT tick callback error: {last_error}")


class FallbackMarketDataSource(RealtimeMarketSource):
    """Primary source with automatic fallback to a secondary source."""

    def __init__(
        self,
        primary: RealtimeMarketSource,
        fallback: RealtimeMarketSource,
        *,
        no_tick_timeout_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.active = primary
        self.no_tick_timeout_seconds = float(no_tick_timeout_seconds)
        self.clock = clock or time.monotonic
        self._last_primary_tick_ts: float | None = None
        self._status_events: list[str] = []
        self._fallback_active = False
        self.source_id = primary.source_id
        self.label = primary.label

    @property
    def active_source_id(self) -> str:
        return self.active.source_id

    @property
    def active_label(self) -> str:
        return self.active.label

    def pop_status_events(self) -> list[str]:
        events = []
        for source in (self.primary, self.fallback):
            events.extend(source.pop_status_events())
        events.extend(self._status_events)
        self._status_events = []
        return events

    async def start(self) -> None:
        try:
            await self.primary.start()
            self.active = self.primary
            self._last_primary_tick_ts = None
        except Exception as exc:
            await self._switch_to_fallback(f"{self.primary.label} 启动失败: {exc}")

    async def fetch_initial_tick(self) -> dict[str, Any] | None:
        return await self._call_active("fetch_initial_tick")

    async def fetch(self) -> dict[str, Any] | None:
        return await self._call_active("fetch")

    async def reset_stale_guard(self) -> None:
        await self.active.reset_stale_guard()

    async def close(self) -> None:
        for source in (self.primary, self.fallback):
            try:
                await source.close()
            except Exception:
                pass

    async def _call_active(self, method_name: str) -> dict[str, Any] | None:
        method = getattr(self.active, method_name)
        try:
            tick = await method()
        except Exception as exc:
            if self.active is self.primary:
                await self._switch_to_fallback(f"{self.primary.label} 行情失败: {exc}")
                tick = await getattr(self.active, method_name)()
            else:
                raise
        else:
            if self.active is self.primary:
                if tick is not None:
                    self._last_primary_tick_ts = self.clock()
                elif self._primary_no_tick_expired():
                    await self._switch_to_fallback(
                        f"{self.primary.label} 超过 {self.no_tick_timeout_seconds:.0f} 秒无有效 tick"
                    )
                    tick = await getattr(self.active, method_name)()
        if tick is not None and self._fallback_active:
            tick["requested_market_source"] = self.primary.source_id
            tick["market_source_fallback"] = True
        return tick

    def _primary_no_tick_expired(self) -> bool:
        if self.no_tick_timeout_seconds < 0:
            return False
        if self._last_primary_tick_ts is None:
            self._last_primary_tick_ts = self.clock()
            return False
        return self.clock() - self._last_primary_tick_ts >= self.no_tick_timeout_seconds

    async def _switch_to_fallback(self, reason: str) -> None:
        if self._fallback_active:
            return
        try:
            await self.primary.close()
        except Exception:
            pass
        await self.fallback.start()
        self.active = self.fallback
        self._fallback_active = True
        self._status_events.append(f"[FEED] {reason}，已自动切换到 {self.fallback.label}")


def create_market_data_source(source_id: str | None, symbol: str, **kwargs: Any) -> RealtimeMarketSource:
    source_id = normalize_market_source_id(source_id)
    no_tick_timeout_seconds = float(kwargs.pop("no_tick_timeout_seconds", 30.0))
    clock = kwargs.pop("clock", None)
    if source_id == "qmt":
        primary = QmtMarketDataSource(symbol, **kwargs)
        fallback = TencentMarketDataSource(symbol)
        return FallbackMarketDataSource(
            primary,
            fallback,
            no_tick_timeout_seconds=no_tick_timeout_seconds,
            clock=clock,
        )
    return TencentMarketDataSource(symbol, **kwargs)
