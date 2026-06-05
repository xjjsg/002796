"""Base position engine and shared trade record types.

Versioned strategies inherit from BaseStrategy for cash/share accounting,
target-position alignment, scoring helpers, and lot-aware buy/sell execution.
Concrete strategy modules own the actual signal ordering.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, List, Optional

from .config import (
    LOT_SIZE, COMMISSION_RATE, STAMP_DUTY_RATE, 
    FLOOR_PCT, CEIL_PCT, ANCHOR_PCT, clamp, parse_dt
)
from .factors import FactorSnapshot, IntradayFactorCalc
from .execution import calculate_trade_costs

class PositionMode(Enum):
    NEUTRAL = "NEUTRAL"
    DEFENSE = "DEFENSE"
    ATTACK = "ATTACK"

from dataclasses import dataclass
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

class BaseStrategy:
    """
    Shared position, factor, and execution engine used by versioned strategies.
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
        dt = parse_dt(timestamp)
        if dt is None or price <= 0:
            return None
        target_pct = clamp(target_pct, self.floor_pct, self.ceil_pct)
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
        dev_score = clamp((f.day_vwap_dev - 0.018) / 0.032)
        acc_score = clamp((-f.acceleration) / 0.008)
        vel_score = clamp(f.velocity / 0.008)
        return clamp(0.45 * dev_score + 0.30 * acc_score + 0.25 * vel_score)

    def _score_macro_buy(self, f: FactorSnapshot) -> float:
        if not (f.day_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0 and f.vol_mom > 1.8):
            return 0.0
        dev_score = clamp((-f.day_vwap_dev - 0.004) / 0.020)
        acc_score = clamp(f.acceleration / 0.008)
        vel_score = clamp(f.velocity / 0.008)
        vol_score = clamp((f.vol_mom - 1.8) / 1.8)
        return clamp(0.35 * dev_score + 0.25 * acc_score + 0.15 * vel_score + 0.25 * vol_score)

    def _score_local_trim(self, f: FactorSnapshot) -> float:
        if not (f.local_vwap_dev > 0.006 and f.acceleration < 0):
            return 0.0
        dev_score = clamp((f.local_vwap_dev - 0.006) / 0.018)
        acc_score = clamp((-f.acceleration) / 0.006)
        return clamp(0.60 * dev_score + 0.40 * acc_score)

    def _score_local_cover(self, f: FactorSnapshot) -> float:
        if not (f.local_vwap_dev < -0.004 and f.velocity > 0 and f.acceleration > 0):
            return 0.0
        dev_score = clamp((-f.local_vwap_dev - 0.004) / 0.016)
        acc_score = clamp(f.acceleration / 0.006)
        vel_score = clamp(f.velocity / 0.006)
        return clamp(0.55 * dev_score + 0.30 * acc_score + 0.15 * vel_score)

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
        step = 0.06 + 0.10 * clamp(score)
        if direction == "trim":
            return self.target_pct - step
        return self.target_pct + step

    def _align_to_target(
        self,
        current_price: float,
        target_pct: float,
        dt: datetime,
        reason: str,
        detail: str = "",
        force_floor: bool = False,
    ) -> Optional[TradeRecord]:
        target_pct = clamp(target_pct, self.floor_pct, self.ceil_pct)
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
        costs = calculate_trade_costs("BUY", price, shares)
        cost = costs.buy_cash_required
        if cost > self.cash + 1e-6:
            return None
        self.cash -= cost
        self.shares += shares
        self.target_pct = clamp(target_pct, self.floor_pct, self.ceil_pct)
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
        costs = calculate_trade_costs("SELL", price, shares)
        revenue = costs.sell_cash_received
        self.cash += revenue
        self.shares -= shares
        self.target_pct = clamp(target_pct, self.floor_pct, self.ceil_pct)
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

