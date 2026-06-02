from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


LOT_SIZE = 100
COMMISSION_RATE = 0.0001
STAMP_DUTY_RATE = 0.0005

FLOOR_PCT = 0.40
CEIL_PCT = 1.00
ANCHOR_PCT = 0.70


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
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


class PositionMode(Enum):
    NEUTRAL = "NEUTRAL"
    DEFENSE = "DEFENSE"
    ATTACK = "ATTACK"


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


@dataclass
class TradeRecord:
    timestamp: datetime
    side: str
    price: float
    shares: int
    position_shares: int
    cash_after: float
    target_pct: float
    mode: str
    reason: str
    detail: str = ""


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
        dt = _parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
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


class BaseStrategy:
    """
    Shared position, factor, and execution engine used by the V5 strategy.
    It keeps a 70% anchor with a 40%-100% target range and local VWAP T logic.
    """

    def __init__(
        self,
        initial_capital: float = 500000.0,
        anchor_pct: float = ANCHOR_PCT,
        floor_pct: float = FLOOR_PCT,
        ceil_pct: float = CEIL_PCT,
        commission_rate: float = COMMISSION_RATE,
        stamp_duty_rate: float = STAMP_DUTY_RATE,
        min_trade_lots: int = 3,
        cooldown_minutes: int = 40,
        max_day_trades: int = 5,
        last_signal_time: str = "14:45:00",
        enable_intraday_regime: bool = False,
        regime_start_time: str = "10:00:00",
        pressure_enter_score: float = 0.75,
        pressure_exit_score: float = 0.35,
        pressure_restore_score: float = 0.55,
        pressure_min_target_pct: float = 0.95,
        enable_pump_dump_guard: bool = False,
        pump_guard_start_time: str = "09:35:00",
        pump_guard_peak_ret: float = 0.03,
        pump_guard_pullback: float = 0.015,
        pump_guard_step: float = 0.20,
        pump_guard_min_target_pct: float = 0.85,
        pump_guard_cover_vwap_dev: float = -0.010,
        enable_risk_t: bool = False,
        risk_t_start_time: str = "09:35:00",
        macro_enter_score: float = 0.30,
        local_enter_score: float = 0.70,
        risk_trim_enter_score: float = 0.65,
        risk_cover_enter_score: float = 0.55,
        risk_trim_min_target_pct: float = 0.95,
        min_target_move: float = 0.02,
        verbose: bool = False,
    ):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.shares = 0
        self.anchor_pct = anchor_pct
        self.floor_pct = floor_pct
        self.ceil_pct = ceil_pct
        self.commission_rate = commission_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.min_trade_lots = min_trade_lots
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.max_day_trades = max_day_trades
        self.last_signal_time = last_signal_time
        self.enable_intraday_regime = enable_intraday_regime
        self.regime_start_time = regime_start_time
        self.pressure_enter_score = pressure_enter_score
        self.pressure_exit_score = pressure_exit_score
        self.pressure_restore_score = pressure_restore_score
        self.pressure_min_target_pct = pressure_min_target_pct
        self.enable_pump_dump_guard = enable_pump_dump_guard
        self.pump_guard_start_time = pump_guard_start_time
        self.pump_guard_peak_ret = pump_guard_peak_ret
        self.pump_guard_pullback = pump_guard_pullback
        self.pump_guard_step = pump_guard_step
        self.pump_guard_min_target_pct = pump_guard_min_target_pct
        self.pump_guard_cover_vwap_dev = pump_guard_cover_vwap_dev
        self.enable_risk_t = enable_risk_t
        self.risk_t_start_time = risk_t_start_time
        self.macro_enter_score = macro_enter_score
        self.local_enter_score = local_enter_score
        self.risk_trim_enter_score = risk_trim_enter_score
        self.risk_cover_enter_score = risk_cover_enter_score
        self.risk_trim_min_target_pct = risk_trim_min_target_pct
        self.min_target_move = min_target_move
        self.verbose = verbose

        self.factor_calc = IntradayFactorCalc(local_window=30)
        self.current_date: Optional[str] = None
        self.last_trade_dt: Optional[datetime] = None
        self.day_trade_count = 0
        self.target_pct = anchor_pct
        self.mode = PositionMode.NEUTRAL
        self.risk_restore_target_pct: Optional[float] = None
        self.pump_guard_restore_target_pct: Optional[float] = None
        self.pump_guard_used = False
        self.regime_state = "NEUTRAL"
        self.regime_score = 0.0
        self.regime_cap_pct = ceil_pct
        self.regime_restore_target_pct: Optional[float] = None
        self.trades: List[TradeRecord] = []

    def initialize_position(
        self,
        price: float,
        timestamp: Any,
        target_pct: float = ANCHOR_PCT,
        reason: str = "初始70%建仓",
    ) -> Optional[TradeRecord]:
        dt = _parse_dt(timestamp)
        if dt is None or price <= 0:
            return None
        target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        target_shares = int((self.initial_capital * target_pct) / price / LOT_SIZE) * LOT_SIZE
        return self._buy(price, target_shares, dt, target_pct, reason, "initial")

    def total_asset(self, current_price: float) -> float:
        return self.cash + self.shares * current_price

    def current_position_pct(self, current_price: float) -> float:
        total = self.total_asset(current_price)
        return (self.shares * current_price) / total if total > 0 else 0.0

    def _can_signal(self, dt: datetime, time_str: str, start_time: str = "10:00:00") -> bool:
        if time_str < start_time or time_str > self.last_signal_time:
            return False
        if self.day_trade_count >= self.max_day_trades:
            return False
        if self.last_trade_dt is not None and dt - self.last_trade_dt < self.cooldown:
            return False
        return True

    def _mode_from_target(self, target_pct: float) -> PositionMode:
        if target_pct < self.anchor_pct - 0.03:
            return PositionMode.DEFENSE
        if target_pct > self.anchor_pct + 0.03:
            return PositionMode.ATTACK
        return PositionMode.NEUTRAL

    def _score_macro_sell(self, f: FactorSnapshot) -> float:
        if not (f.day_return < 0.08 and f.day_vwap_dev > 0.018 and f.velocity > 0 and f.acceleration < 0):
            return 0.0
        dev_score = _clamp((f.day_vwap_dev - 0.018) / 0.032)
        acc_score = _clamp((-f.acceleration) / 0.008)
        vel_score = _clamp(f.velocity / 0.008)
        return _clamp(0.45 * dev_score + 0.30 * acc_score + 0.25 * vel_score)

    def _score_macro_buy(self, f: FactorSnapshot) -> float:
        if not (f.day_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0 and f.vol_mom > 1.8):
            return 0.0
        dev_score = _clamp((-f.day_vwap_dev - 0.004) / 0.020)
        acc_score = _clamp(f.acceleration / 0.008)
        vel_score = _clamp(f.velocity / 0.008)
        vol_score = _clamp((f.vol_mom - 1.8) / 1.8)
        return _clamp(0.35 * dev_score + 0.25 * acc_score + 0.15 * vel_score + 0.25 * vol_score)

    def _score_local_trim(self, f: FactorSnapshot) -> float:
        if not (f.local_vwap_dev > 0.006 and f.acceleration < 0):
            return 0.0
        dev_score = _clamp((f.local_vwap_dev - 0.006) / 0.018)
        acc_score = _clamp((-f.acceleration) / 0.006)
        return _clamp(0.60 * dev_score + 0.40 * acc_score)

    def _score_local_cover(self, f: FactorSnapshot) -> float:
        if not (f.local_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0):
            return 0.0
        dev_score = _clamp((-f.local_vwap_dev - 0.004) / 0.016)
        acc_score = _clamp(f.acceleration / 0.006)
        vel_score = _clamp(f.velocity / 0.006)
        return _clamp(0.55 * dev_score + 0.30 * acc_score + 0.15 * vel_score)

    def _score_risk_trim(self, f: FactorSnapshot, allow_weak_break: bool = True) -> float:
        failed_rally = 0.0
        if f.local_vwap_dev > 0.006 and f.acceleration < 0:
            local_dev_score = _clamp((f.local_vwap_dev - 0.006) / 0.014)
            fade_score = _clamp((-f.acceleration) / 0.008)
            weak_day_bonus = _clamp((-f.day_vwap_dev) / 0.020) if f.day_vwap_dev < 0 else 0.0
            failed_rally = _clamp(0.50 * local_dev_score + 0.45 * fade_score + 0.05 * weak_day_bonus)

        weak_break = 0.0
        if allow_weak_break and f.day_vwap_dev < -0.008 and f.day_return < -0.002 and (
            f.velocity < 0 or f.local_vwap_dev < -0.008
        ):
            day_vwap_score = _clamp((-f.day_vwap_dev - 0.008) / 0.020)
            loss_score = _clamp((-f.day_return - 0.002) / 0.030)
            local_weak_score = _clamp((-f.local_vwap_dev) / 0.012) if f.local_vwap_dev < 0 else 0.0
            volume_score = _clamp((f.vol_mom - 1.0) / 1.5)
            weak_break = _clamp(
                0.40 * day_vwap_score
                + 0.30 * loss_score
                + 0.20 * local_weak_score
                + 0.10 * volume_score
            )

        return max(failed_rally, weak_break)

    def _score_risk_cover(self, f: FactorSnapshot) -> float:
        if not (f.day_vwap_dev < -0.010 and f.local_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0):
            return 0.0
        day_vwap_score = _clamp((-f.day_vwap_dev - 0.010) / 0.030)
        local_dev_score = _clamp((-f.local_vwap_dev - 0.004) / 0.020)
        acc_score = _clamp(f.acceleration / 0.006)
        vel_score = _clamp(f.velocity / 0.006)
        return _clamp(0.35 * day_vwap_score + 0.35 * local_dev_score + 0.20 * acc_score + 0.10 * vel_score)

    def _score_pressure_regime(self, f: FactorSnapshot) -> float:
        vwap_pressure = _clamp((-f.day_vwap_dev - 0.004) / 0.020)
        below_vwap = _clamp((f.below_vwap_ratio - 0.50) / 0.35)
        open_loss = _clamp((-f.open_return - 0.004) / 0.030)
        pullback = _clamp((-f.pullback_from_high - 0.012) / 0.035)
        low_range = _clamp((0.45 - f.range_position) / 0.45)
        failed_gap = 0.0
        if f.open_gap > 0.005 and f.open_return < -0.003 and f.pullback_from_high < -0.012:
            failed_gap = _clamp((f.open_gap - 0.005) / 0.045)
        failed_rally = 0.0
        if f.high_return > 0.008 and f.pullback_from_high < -0.012 and f.day_vwap_dev < 0:
            failed_rally = _clamp((-f.pullback_from_high - 0.012) / 0.035)

        trend_pressure = _clamp(
            0.30 * vwap_pressure
            + 0.25 * below_vwap
            + 0.25 * open_loss
            + 0.20 * low_range
        )
        distribution_pressure = _clamp(
            0.35 * pullback
            + 0.25 * vwap_pressure
            + 0.20 * below_vwap
            + 0.10 * failed_gap
            + 0.10 * failed_rally
        )
        return max(trend_pressure, distribution_pressure)

    def _score_pump_dump_guard(self, f: FactorSnapshot) -> float:
        pullback = -f.pullback_from_high
        if f.high_return < self.pump_guard_peak_ret or pullback < self.pump_guard_pullback:
            return 0.0
        peak_score = _clamp((f.high_return - self.pump_guard_peak_ret) / 0.050)
        pullback_score = _clamp((pullback - self.pump_guard_pullback) / 0.035)
        weak_close_score = _clamp((-f.day_vwap_dev) / 0.018) if f.day_vwap_dev < 0 else 0.0
        return _clamp(0.50 + 0.25 * peak_score + 0.20 * pullback_score + 0.05 * weak_close_score)

    def _cap_for_pressure(self, score: float) -> float:
        return _clamp(1.0 - 0.24 * _clamp(score), self.anchor_pct, self.ceil_pct)

    def _update_intraday_regime(self, f: FactorSnapshot, time_str: str) -> None:
        if not self.enable_intraday_regime or time_str < self.regime_start_time:
            self.regime_score = 0.0
            return

        score = self._score_pressure_regime(f)
        self.regime_score = score

        if self.regime_state == "PRESSURE":
            recovery = (
                score <= self.pressure_exit_score
                or (f.day_vwap_dev > -0.002 and f.local_vwap_dev > -0.001 and f.velocity >= 0)
            )
            if recovery:
                self.regime_state = "NEUTRAL"
                self.regime_cap_pct = self.ceil_pct
                return
            self.regime_cap_pct = min(self.regime_cap_pct, self._cap_for_pressure(score))
        elif score >= self.pressure_enter_score:
            self.regime_state = "PRESSURE"
            self.regime_cap_pct = self._cap_for_pressure(score)
            self.regime_restore_target_pct = max(self.regime_restore_target_pct or 0.0, self.target_pct)

    def _detail(self, f: FactorSnapshot, score_name: str, score: float) -> str:
        return (
            f"{score_name}={score:.2f} "
            f"day_dev={f.day_vwap_dev*100:.2f}% "
            f"local_dev={f.local_vwap_dev*100:.2f}% "
            f"vel={f.velocity*100:.2f}% "
            f"acc={f.acceleration*100:.2f}% "
            f"vol={f.vol_mom:.1f}x"
        )

    def _target_for_local_t(self, direction: str, score: float) -> float:
        # 局部 T 每次移动 6%~16%，由局部偏离程度决定。
        step = 0.06 + 0.10 * _clamp(score)
        if direction == "trim":
            return self.target_pct - step
        return self.target_pct + step

    def _target_for_risk_t(self, direction: str, score: float) -> float:
        step = 0.08 + 0.16 * _clamp(score)
        if direction == "trim":
            return max(self.anchor_pct, self.target_pct - step)
        return min(self.ceil_pct, self.target_pct + step)

    def _target_for_pump_guard(self) -> float:
        return max(self.anchor_pct, self.target_pct - self.pump_guard_step)

    def _pump_guard_detail(self, f: FactorSnapshot, score: float) -> str:
        return (
            f"pump_guard={score:.2f} "
            f"high={f.high_return*100:.2f}% "
            f"pullback={-f.pullback_from_high*100:.2f}% "
            f"day_dev={f.day_vwap_dev*100:.2f}% "
            f"open_ret={f.open_return*100:.2f}%"
        )

    def _align_to_target(
        self,
        current_price: float,
        target_pct: float,
        dt: datetime,
        reason: str,
        detail: str = "",
        force_floor: bool = False,
    ) -> Optional[TradeRecord]:
        target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        total = self.total_asset(current_price)
        target_shares = int((total * target_pct) / current_price / LOT_SIZE) * LOT_SIZE
        diff = target_shares - self.shares
        min_shares = LOT_SIZE if force_floor else self.min_trade_lots * LOT_SIZE

        if diff >= min_shares:
            max_affordable = int((self.cash / (current_price * (1.0 + self.commission_rate))) / LOT_SIZE) * LOT_SIZE
            buy_shares = min(diff, max_affordable)
            if buy_shares >= min_shares:
                return self._buy(current_price, buy_shares, dt, target_pct, reason, detail)
        elif diff <= -min_shares:
            sell_shares = min(-diff, self.shares)
            sell_shares = int(sell_shares / LOT_SIZE) * LOT_SIZE
            if sell_shares >= min_shares:
                return self._sell(current_price, sell_shares, dt, target_pct, reason, detail)
        return None

    def _buy(
        self,
        price: float,
        shares: int,
        dt: datetime,
        target_pct: float,
        reason: str,
        detail: str,
    ) -> Optional[TradeRecord]:
        shares = int(shares / LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            return None
        cost = shares * price * (1.0 + self.commission_rate)
        if cost > self.cash + 1e-6:
            return None
        self.cash -= cost
        self.shares += shares
        self.target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        self.mode = self._mode_from_target(self.target_pct)
        record = TradeRecord(dt, "BUY", price, shares, self.shares, self.cash, self.target_pct, self.mode.value, reason, detail)
        self.trades.append(record)
        if self.verbose:
            print(self._format_trade(record))
        return record

    def _sell(
        self,
        price: float,
        shares: int,
        dt: datetime,
        target_pct: float,
        reason: str,
        detail: str,
    ) -> Optional[TradeRecord]:
        shares = int(min(shares, self.shares) / LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            return None
        revenue = shares * price * (1.0 - self.commission_rate - self.stamp_duty_rate)
        self.cash += revenue
        self.shares -= shares
        self.target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        self.mode = self._mode_from_target(self.target_pct)
        record = TradeRecord(dt, "SELL", price, shares, self.shares, self.cash, self.target_pct, self.mode.value, reason, detail)
        self.trades.append(record)
        if self.verbose:
            print(self._format_trade(record))
        return record

    def _format_trade(self, record: TradeRecord) -> str:
        return (
            f"[{record.timestamp:%Y-%m-%d %H:%M:%S}] "
            f"{record.side} {record.shares} @ {record.price:.2f} "
            f"target={record.target_pct*100:.1f}% "
            f"pos={record.position_shares} cash={record.cash_after:.2f} "
            f"{record.reason} {record.detail}"
        )

    def on_tick(self, tick: Dict[str, Any]) -> Optional[TradeRecord]:
        dt = _parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
        if dt is None:
            return None
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
        if time_str < "09:30:00" or time_str > "15:00:00":
            return None

        current_price = float(tick.get("Close", tick.get("price", 0.0)) or 0.0)
        if current_price <= 0:
            return None

        is_new_day = False
        if self.current_date != date_str:
            self.current_date = date_str
            self.day_trade_count = 0
            self.regime_state = "NEUTRAL"
            self.regime_score = 0.0
            self.regime_cap_pct = self.ceil_pct
            self.regime_restore_target_pct = None
            self.risk_restore_target_pct = None
            self.pump_guard_restore_target_pct = None
            self.pump_guard_used = False
            is_new_day = True

        factors = self.factor_calc.update(tick, is_new_day)
        self._update_intraday_regime(factors, time_str)

        # 硬下限：价格波动导致仓位跌破 40% 时，优先补到 40%。
        if self.shares > 0 and self.current_position_pct(current_price) < self.floor_pct - 0.005:
            record = self._align_to_target(
                current_price,
                self.floor_pct,
                dt,
                "仓位下限补足",
                self._detail(factors, "floor", 1.0),
                force_floor=True,
            )
            if record:
                return record

        can_main_signal = self._can_signal(dt, time_str)
        can_risk_t_signal = self.enable_risk_t and self._can_signal(dt, time_str, start_time=self.risk_t_start_time)
        can_pump_guard_signal = self.enable_pump_dump_guard and self._can_signal(
            dt,
            time_str,
            start_time=self.pump_guard_start_time,
        )
        if not can_main_signal and not can_risk_t_signal and not can_pump_guard_signal:
            return None

        macro_sell = self._score_macro_sell(factors) if can_main_signal else 0.0
        macro_buy = self._score_macro_buy(factors) if can_main_signal else 0.0
        local_trim = self._score_local_trim(factors) if can_main_signal else 0.0
        local_cover = self._score_local_cover(factors) if can_main_signal else 0.0
        allow_weak_break = time_str >= "10:00:00"
        risk_trim = self._score_risk_trim(factors, allow_weak_break=allow_weak_break) if can_risk_t_signal else 0.0
        risk_cover = self._score_risk_cover(factors) if can_risk_t_signal else 0.0
        pump_guard = self._score_pump_dump_guard(factors) if can_pump_guard_signal else 0.0
        regime_pressure = self.regime_state == "PRESSURE" and can_main_signal
        regime_restore = self.regime_restore_target_pct is not None and can_main_signal

        record: Optional[TradeRecord] = None
        if (
            regime_pressure
            and self.target_pct >= self.pressure_min_target_pct
            and self.target_pct > self.regime_cap_pct + self.min_target_move
        ):
            target = self.regime_cap_pct
            record = self._align_to_target(
                current_price,
                target,
                dt,
                "盘面压制降仓",
                self._detail(factors, "pressure", self.regime_score),
            )
        elif (
            can_pump_guard_signal
            and not self.pump_guard_used
            and self.pump_guard_restore_target_pct is None
            and pump_guard > 0.0
            and self.target_pct >= self.pump_guard_min_target_pct
        ):
            restore_target = self.target_pct
            target = self._target_for_pump_guard()
            if target < self.target_pct - self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "冲高回落防守",
                    self._pump_guard_detail(factors, pump_guard),
                )
                if record:
                    self.pump_guard_restore_target_pct = max(
                        self.pump_guard_restore_target_pct or 0.0,
                        restore_target,
                    )
                    self.pump_guard_used = True
        elif can_main_signal and macro_sell >= self.macro_enter_score and macro_sell >= macro_buy:
            target = self.anchor_pct - 0.42 * macro_sell
            if target < self.target_pct - self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "模式高抛降仓",
                    self._detail(factors, "sell", macro_sell),
                )
        elif can_main_signal and macro_buy >= self.macro_enter_score and macro_buy >= macro_sell:
            target = self.anchor_pct + 0.42 * macro_buy
            if self.regime_state == "PRESSURE":
                target = min(target, self.regime_cap_pct)
            if target > self.target_pct + self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "模式低吸加仓",
                    self._detail(factors, "buy", macro_buy),
                )
        elif (
            can_risk_t_signal
            and risk_trim >= self.risk_trim_enter_score
            and self.target_pct >= self.risk_trim_min_target_pct
        ):
            restore_target = self.target_pct
            target = self._target_for_risk_t("trim", risk_trim)
            if target < self.target_pct - self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "日内风险减仓",
                    self._detail(factors, "risk_trim", risk_trim),
                )
                if record:
                    self.risk_restore_target_pct = max(self.risk_restore_target_pct or 0.0, restore_target)
        elif (
            can_risk_t_signal
            and self.risk_restore_target_pct is not None
            and macro_sell <= 0.0
            and macro_buy <= 0.0
            and risk_cover >= self.risk_cover_enter_score
        ):
            target = min(self.risk_restore_target_pct, self._target_for_risk_t("cover", risk_cover))
            if target > self.target_pct + self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "日内低位回补",
                    self._detail(factors, "risk_cover", risk_cover),
                )
                if record and self.target_pct >= self.risk_restore_target_pct - self.min_target_move:
                    self.risk_restore_target_pct = None
        elif (
            can_pump_guard_signal
            and self.pump_guard_restore_target_pct is not None
            and macro_sell <= 0.0
            and macro_buy <= 0.0
            and risk_trim <= 0.0
            and factors.day_vwap_dev <= self.pump_guard_cover_vwap_dev
            and factors.local_vwap_dev <= -0.004
            and factors.velocity > 0
            and factors.acceleration > 0
        ):
            target = min(self.pump_guard_restore_target_pct, self.ceil_pct)
            if self.regime_state == "PRESSURE":
                target = min(target, self.regime_cap_pct)
            if target > self.target_pct + self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "冲高回落回补",
                    self._pump_guard_detail(factors, pump_guard),
                )
                if record and self.target_pct >= self.pump_guard_restore_target_pct - self.min_target_move:
                    self.pump_guard_restore_target_pct = None
        elif (
            regime_restore
            and self.regime_state == "NEUTRAL"
            and macro_sell <= 0.0
            and macro_buy <= 0.0
            and self._score_risk_cover(factors) >= self.pressure_restore_score
        ):
            target = min(self.regime_restore_target_pct, self._target_for_risk_t("cover", self._score_risk_cover(factors)))
            if target > self.target_pct + self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "压盘解除回补",
                    self._detail(factors, "restore", self._score_risk_cover(factors)),
                )
                if record and self.target_pct >= self.regime_restore_target_pct - self.min_target_move:
                    self.regime_restore_target_pct = None
        elif can_main_signal and local_trim >= self.local_enter_score:
            target = self._target_for_local_t("trim", local_trim)
            record = self._align_to_target(
                current_price,
                target,
                dt,
                "局部止盈T",
                self._detail(factors, "trim", local_trim),
            )
        elif can_main_signal and local_cover >= self.local_enter_score:
            target = self._target_for_local_t("cover", local_cover)
            if self.regime_state == "PRESSURE":
                target = min(target, self.regime_cap_pct)
            record = self._align_to_target(
                current_price,
                target,
                dt,
                "局部回补T",
                self._detail(factors, "cover", local_cover),
            )

        if record:
            self.last_trade_dt = dt
            self.day_trade_count += 1
        return record
