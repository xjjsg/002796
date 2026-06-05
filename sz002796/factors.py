"""Intraday factor engine used by V6 strategy decisions.

The factor calculator is stateful per trading day. It receives normalized ticks
or minute rows and emits a FactorSnapshot with VWAP deviations, short-term
momentum, volume momentum, range position, and orderbook imbalance.
"""
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .config import parse_dt

@dataclass
class FactorSnapshot:
    price: float
    vwap: float
    day_vwap_dev: float
    local_vwap: float
    local_vwap_dev: float
    velocity: float
    acceleration: float
    vol_mom: float
    day_return: float
    tick_vol: float
    tick_amt: float
    open_price: float = 0.0
    open_gap: float = 0.0
    open_return: float = 0.0
    intraday_high: float = 0.0
    intraday_low: float = 0.0
    high_return: float = 0.0
    pullback_from_high: float = 0.0
    range_position: float = 0.5
    below_vwap_ratio: float = 0.0
    vwap_slope_15m: float = 0.0
    vwap_slope_30m: float = 0.0
    local_price_std: float = 0.0
    local_vwap_z: float = 0.0
    opening_range_high: float = 0.0
    opening_range_low: float = 0.0
    opening_range_position: float = 0.5
    break_opening_high: float = 0.0
    break_opening_low: float = 0.0
    consecutive_above_vwap: float = 0.0
    consecutive_below_vwap: float = 0.0
    new_high_count_30m: float = 0.0
    new_low_count_30m: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    orderbook_imbalance: float = 0.0



class IntradayFactorCalc:
    """Intraday VWAP factors with real-time rolling windows.

    The data set mixes minute bars and 3-second ticks. Local factors therefore
    use elapsed time instead of sample counts, so parameters keep the same
    meaning after the feed moves to 3-second data.
    """

    def __init__(
        self,
        local_window: int = 30,
        velocity_window_minutes: int = 5,
        acceleration_window_minutes: int = 5,
        fast_volume_window_minutes: int = 5,
        slow_volume_window_minutes: int = 30,
    ):
        self.local_window_minutes = local_window
        self.local_window = timedelta(minutes=local_window)
        self.velocity_window = timedelta(minutes=velocity_window_minutes)
        self.acceleration_window = timedelta(minutes=acceleration_window_minutes)
        self.fast_volume_window = timedelta(minutes=fast_volume_window_minutes)
        self.slow_volume_window = timedelta(minutes=slow_volume_window_minutes)
        self.vwap_slope_15m_window = timedelta(minutes=15)
        self.vwap_slope_30m_window = timedelta(minutes=30)
        self.event_window_30m = timedelta(minutes=30)
        self.max_history_window = max(
            self.local_window,
            self.velocity_window + self.acceleration_window,
            self.slow_volume_window,
            self.vwap_slope_30m_window,
            self.event_window_30m,
        ) + timedelta(minutes=5)
        self.price_history: deque[tuple[datetime, float]] = deque()
        self.vol_history: deque[tuple[datetime, float]] = deque()
        self.vel_history: deque[tuple[datetime, float]] = deque()
        self.local_amt_history: deque[tuple[datetime, float]] = deque()
        self.local_vol_history: deque[tuple[datetime, float]] = deque()
        self.local_price_history: deque[tuple[datetime, float]] = deque()
        self.vwap_history: deque[tuple[datetime, float]] = deque()
        self.new_high_history: deque[tuple[datetime, float]] = deque()
        self.new_low_history: deque[tuple[datetime, float]] = deque()
        self.cum_vol = 0.0
        self.cum_amt = 0.0
        self.vwap = 0.0
        self.prev_close = 0.0
        self.last_raw_vol = 0.0
        self.last_raw_amt = 0.0
        self.open_price = 0.0
        self.intraday_high = 0.0
        self.intraday_low = 0.0
        self.sample_count = 0
        self.below_vwap_count = 0
        self.opening_range_high = 0.0
        self.opening_range_low = 0.0
        self.consecutive_above_vwap = 0
        self.consecutive_below_vwap = 0
        self.last_snapshot: Optional[FactorSnapshot] = None

    @staticmethod
    def _prune(history: deque[tuple[datetime, float]], cutoff: datetime) -> None:
        while history and history[0][0] < cutoff:
            history.popleft()

    @staticmethod
    def _values_since(
        history: deque[tuple[datetime, float]],
        current_dt: datetime,
        window: timedelta,
    ) -> list[float]:
        cutoff = current_dt - window
        return [value for ts, value in history if ts > cutoff]

    @staticmethod
    def _sum_since(
        history: deque[tuple[datetime, float]],
        current_dt: datetime,
        window: timedelta,
    ) -> float:
        cutoff = current_dt - window
        return sum(value for ts, value in history if ts > cutoff)

    @staticmethod
    def _value_at_or_before(
        history: deque[tuple[datetime, float]],
        cutoff: datetime,
    ) -> Optional[float]:
        for ts, value in reversed(history):
            if ts <= cutoff:
                return value
        return None

    @staticmethod
    def _has_full_window(
        history: deque[tuple[datetime, float]],
        current_dt: datetime,
        window: timedelta,
    ) -> bool:
        cutoff = current_dt - window
        return bool(history and history[0][0] <= cutoff)

    @staticmethod
    def _trading_clock_dt(value: datetime) -> datetime:
        day_start = value.replace(hour=9, minute=30, second=0, microsecond=0)
        morning_end = value.replace(hour=11, minute=30, second=0, microsecond=0)
        afternoon_start = value.replace(hour=13, minute=0, second=0, microsecond=0)
        if value <= morning_end:
            traded_seconds = max(0.0, (value - day_start).total_seconds())
        elif value < afternoon_start:
            traded_seconds = (morning_end - day_start).total_seconds()
        else:
            traded_seconds = (
                (morning_end - day_start).total_seconds()
                + max(0.0, (value - afternoon_start).total_seconds())
            )
        return day_start + timedelta(seconds=traded_seconds)

    def _prune_histories(self, current_dt: datetime) -> None:
        cutoff = current_dt - self.max_history_window
        for history in (
            self.price_history,
            self.vol_history,
            self.vel_history,
            self.local_amt_history,
            self.local_vol_history,
            self.local_price_history,
            self.vwap_history,
            self.new_high_history,
            self.new_low_history,
        ):
            self._prune(history, cutoff)

    def reset(
        self,
        prev_close: float,
        current_price: float,
        open_price: float = 0.0,
        intraday_high: float = 0.0,
        intraday_low: float = 0.0,
    ) -> None:
        self.price_history.clear()
        self.vol_history.clear()
        self.vel_history.clear()
        self.local_amt_history.clear()
        self.local_vol_history.clear()
        self.local_price_history.clear()
        self.vwap_history.clear()
        self.new_high_history.clear()
        self.new_low_history.clear()
        self.cum_vol = 0.0
        self.cum_amt = 0.0
        self.vwap = current_price
        self.prev_close = prev_close if prev_close > 0 else current_price
        self.last_raw_vol = 0.0
        self.last_raw_amt = 0.0
        self.open_price = open_price if open_price > 0 else current_price
        self.intraday_high = max(current_price, intraday_high if intraday_high > 0 else current_price)
        self.intraday_low = min(current_price, intraday_low if intraday_low > 0 else current_price)
        self.sample_count = 0
        self.below_vwap_count = 0
        self.opening_range_high = current_price
        self.opening_range_low = current_price
        self.consecutive_above_vwap = 0
        self.consecutive_below_vwap = 0
        self.last_snapshot = None

    def update(self, tick: Dict[str, Any], is_new_day: bool) -> FactorSnapshot:
        current_price = float(tick.get("Close", tick.get("price", 0.0)) or 0.0)
        raw_vol = float(tick.get("Volume", tick.get("cum_volume", 0.0)) or 0.0)
        raw_amt = float(tick.get("Amount", tick.get("cum_amount", 0.0)) or 0.0)
        prev_close = float(tick.get("prev_close", 0.0) or 0.0)
        open_price = float(tick.get("open", 0.0) or 0.0)
        day_high = float(tick.get("high", tick.get("High", 0.0)) or 0.0)
        day_low = float(tick.get("low", tick.get("Low", 0.0)) or 0.0)
        dt = parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
        has_timestamp = dt is not None
        if dt is None:
            dt = datetime(1970, 1, 1, 9, 30) + timedelta(minutes=self.sample_count)
        time_str = dt.strftime("%H:%M:%S") if dt is not None else ""
        history_dt = self._trading_clock_dt(dt) if has_timestamp else dt

        if is_new_day:
            self.reset(prev_close, current_price, open_price, day_high, day_low)

        has_direct_vol = "tick_vol" in tick
        has_direct_amt = "tick_amt" in tick
        if has_direct_vol:
            delta_vol = max(0.0, float(tick.get("tick_vol", 0.0) or 0.0))
            delta_amt = max(0.0, float(tick.get("tick_amt", 0.0) or 0.0)) if has_direct_amt else 0.0
        else:
            if raw_vol >= self.last_raw_vol:
                delta_vol = raw_vol - self.last_raw_vol
            else:
                delta_vol = raw_vol
            if raw_amt >= self.last_raw_amt:
                delta_amt = raw_amt - self.last_raw_amt
            else:
                delta_amt = raw_amt

        self.last_raw_vol = raw_vol
        self.last_raw_amt = raw_amt

        if delta_amt <= 0 and delta_vol > 0:
            delta_amt = delta_vol * current_price

        if raw_vol > 0 and raw_amt > 0 and raw_vol >= self.cum_vol:
            self.cum_vol = raw_vol
            self.cum_amt = raw_amt
        else:
            self.cum_vol += delta_vol
            self.cum_amt += delta_amt

        self.vwap = self.cum_amt / self.cum_vol if self.cum_vol > 0 else current_price
        prev_intraday_high = self.intraday_high
        prev_intraday_low = self.intraday_low
        self.intraday_high = max(self.intraday_high, current_price, day_high if day_high > 0 else current_price)
        self.intraday_low = min(self.intraday_low, current_price, day_low if day_low > 0 else current_price)
        if time_str <= "10:00:00":
            self.opening_range_high = max(self.opening_range_high, current_price)
            self.opening_range_low = min(self.opening_range_low, current_price)
        new_high_event = 1.0 if self.sample_count > 0 and current_price >= prev_intraday_high else 0.0
        new_low_event = 1.0 if self.sample_count > 0 and current_price <= prev_intraday_low else 0.0
        self.new_high_history.append((history_dt, new_high_event))
        self.new_low_history.append((history_dt, new_low_event))
        self.sample_count += 1
        if current_price < self.vwap:
            self.below_vwap_count += 1
            self.consecutive_below_vwap += 1
            self.consecutive_above_vwap = 0
        elif current_price > self.vwap:
            self.consecutive_above_vwap += 1
            self.consecutive_below_vwap = 0

        self.price_history.append((history_dt, current_price))
        self.vol_history.append((history_dt, delta_vol))
        self.local_amt_history.append((history_dt, delta_amt))
        self.local_vol_history.append((history_dt, delta_vol))
        self.local_price_history.append((history_dt, current_price))
        self.vwap_history.append((history_dt, self.vwap))
        self._prune_histories(history_dt)

        local_amt = self._sum_since(self.local_amt_history, history_dt, self.local_window)
        local_vol = self._sum_since(self.local_vol_history, history_dt, self.local_window)
        if local_vol > 0:
            local_vwap = local_amt / local_vol
        else:
            local_prices_for_avg = self._values_since(self.local_price_history, history_dt, self.local_window)
            local_vwap = sum(local_prices_for_avg) / len(local_prices_for_avg) if local_prices_for_avg else current_price
        local_prices = self._values_since(self.local_price_history, history_dt, self.local_window)
        if not local_prices:
            local_prices = [current_price]
        local_mean = sum(local_prices) / len(local_prices)
        local_var = sum((p - local_mean) ** 2 for p in local_prices) / len(local_prices)
        local_price_std = math.sqrt(local_var)

        velocity = 0.0
        velocity_base = self._value_at_or_before(self.price_history, history_dt - self.velocity_window)
        if velocity_base is not None and velocity_base > 0:
            velocity = current_price / velocity_base - 1.0
        self.vel_history.append((history_dt, velocity))

        acceleration = 0.0
        acceleration_base = self._value_at_or_before(self.vel_history, history_dt - self.acceleration_window)
        if acceleration_base is not None:
            acceleration = velocity - acceleration_base

        vol_mom = 0.0
        if self._has_full_window(self.vol_history, history_dt, self.slow_volume_window):
            fast_vol = self._sum_since(self.vol_history, history_dt, self.fast_volume_window)
            slow_vol = self._sum_since(self.vol_history, history_dt, self.slow_volume_window)
            fast_minutes = self.fast_volume_window.total_seconds() / 60.0
            slow_minutes = self.slow_volume_window.total_seconds() / 60.0
            fast_ma = fast_vol / fast_minutes if fast_minutes > 0 else 0.0
            slow_ma = slow_vol / slow_minutes if slow_minutes > 0 else 0.0
            vol_mom = fast_ma / slow_ma if slow_ma > 0 else 0.0

        day_vwap_dev = current_price / self.vwap - 1.0 if self.vwap > 0 else 0.0
        local_vwap_dev = current_price / local_vwap - 1.0 if local_vwap > 0 else 0.0
        day_return = current_price / self.prev_close - 1.0 if self.prev_close > 0 else 0.0
        open_gap = self.open_price / self.prev_close - 1.0 if self.prev_close > 0 else 0.0
        open_return = current_price / self.open_price - 1.0 if self.open_price > 0 else 0.0
        high_return = self.intraday_high / self.open_price - 1.0 if self.open_price > 0 else 0.0
        pullback_from_high = current_price / self.intraday_high - 1.0 if self.intraday_high > 0 else 0.0
        day_range = self.intraday_high - self.intraday_low
        range_position = (current_price - self.intraday_low) / day_range if day_range > 0 else 0.5
        below_vwap_ratio = self.below_vwap_count / self.sample_count if self.sample_count > 0 else 0.0
        vwap_slope_15m = 0.0
        vwap_15m = self._value_at_or_before(self.vwap_history, history_dt - self.vwap_slope_15m_window)
        if vwap_15m is not None and vwap_15m > 0:
            vwap_slope_15m = self.vwap / vwap_15m - 1.0
        vwap_slope_30m = 0.0
        vwap_30m = self._value_at_or_before(self.vwap_history, history_dt - self.vwap_slope_30m_window)
        if vwap_30m is not None and vwap_30m > 0:
            vwap_slope_30m = self.vwap / vwap_30m - 1.0
        local_vwap_z = (current_price - local_vwap) / local_price_std if local_price_std > 0 else 0.0
        opening_range = self.opening_range_high - self.opening_range_low
        opening_range_position = (
            (current_price - self.opening_range_low) / opening_range if opening_range > 0 else 0.5
        )
        break_opening_high = 1.0 if time_str > "10:00:00" and current_price > self.opening_range_high else 0.0
        break_opening_low = 1.0 if time_str > "10:00:00" and current_price < self.opening_range_low else 0.0
        new_high_count_30m = self._sum_since(self.new_high_history, history_dt, self.event_window_30m)
        new_low_count_30m = self._sum_since(self.new_low_history, history_dt, self.event_window_30m)
        bid_depth = sum(float(tick.get(f"bv{i}", 0.0) or 0.0) for i in range(1, 6))
        ask_depth = sum(float(tick.get(f"sv{i}", 0.0) or 0.0) for i in range(1, 6))
        total_depth = bid_depth + ask_depth
        orderbook_imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        snapshot = FactorSnapshot(
            price=current_price,
            vwap=self.vwap,
            day_vwap_dev=day_vwap_dev,
            local_vwap=local_vwap,
            local_vwap_dev=local_vwap_dev,
            velocity=velocity,
            acceleration=acceleration,
            vol_mom=vol_mom,
            day_return=day_return,
            tick_vol=delta_vol,
            tick_amt=delta_amt,
            open_price=self.open_price,
            open_gap=open_gap,
            open_return=open_return,
            intraday_high=self.intraday_high,
            intraday_low=self.intraday_low,
            high_return=high_return,
            pullback_from_high=pullback_from_high,
            range_position=range_position,
            below_vwap_ratio=below_vwap_ratio,
            vwap_slope_15m=vwap_slope_15m,
            vwap_slope_30m=vwap_slope_30m,
            local_price_std=local_price_std,
            local_vwap_z=local_vwap_z,
            opening_range_high=self.opening_range_high,
            opening_range_low=self.opening_range_low,
            opening_range_position=opening_range_position,
            break_opening_high=break_opening_high,
            break_opening_low=break_opening_low,
            consecutive_above_vwap=float(self.consecutive_above_vwap),
            consecutive_below_vwap=float(self.consecutive_below_vwap),
            new_high_count_30m=new_high_count_30m,
            new_low_count_30m=new_low_count_30m,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            orderbook_imbalance=orderbook_imbalance,
        )
        self.last_snapshot = snapshot
        return snapshot

