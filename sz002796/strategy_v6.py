"""Combined V6 strategy logic.

V6 combines three layers:
- a coarse market-regime guardrail that sets allowed position bands,
- cross-day target adjustment for slower inventory changes,
- local intraday T logic for range-bound alpha.

The regime module constrains exposure; it does not directly generate trades.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .regime import MarketRegime, MarketRegimeDecision, MarketRegimeEngine
from .config import ANCHOR_PCT, CEIL_PCT, COMMISSION_RATE, STAMP_DUTY_RATE, clamp, parse_dt
from .position import BaseStrategy, TradeRecord
from .factors import FactorSnapshot


class CombinedStrategyV6(BaseStrategy):
    """
    V6 with score-based regime guardrails.

    The regime module only controls the position band (floor/ceiling).
    V6's own cross-day and local-T signals decide targets within that band.
    No signal multipliers: the regime module is a guardrail, not a direction caller.
    """

    def __init__(
        self,
        initial_capital: float = 500000.0,
        anchor_pct: float = ANCHOR_PCT,
        floor_pct: float = 0.0,
        ceil_pct: float = CEIL_PCT,
        commission_rate: float = COMMISSION_RATE,
        stamp_duty_rate: float = STAMP_DUTY_RATE,
        min_trade_lots: int = 3,
        cooldown_minutes: int = 40,
        max_day_trades: int = 3,
        last_signal_time: str = "14:45:00",
        cross_start_time: str = "10:00:00",
        cross_buy_execute_time: str = "14:00:00",
        cross_enter_score: float = 0.25,
        cross_target_sensitivity: float = 0.38,
        local_enter_score: float = 0.80,
        local_cover_enter_score: float = 0.85,
        local_trim_band: float = 0.18,
        local_cover_band: float = 0.06,
        local_trim_step_base: float = 0.06,
        local_trim_step_slope: float = 0.12,
        local_buy_first_profit: float = 0.012,
        local_buy_first_stop: float = 0.020,
        local_flat_time: str = "14:40:00",
        min_target_move: float = 0.04,
        enable_cross_day: bool = True,
        enable_local_t: bool = True,
        enable_market_regime: bool = True,
        market_regime: Optional[MarketRegimeEngine] = None,
        # Band guardrail: only rebalance if position is this far outside the band
        regime_rebalance_margin: float = 0.03,
        enable_main_flow_guard: bool = True,
        main_flow_guard_start_time: str = "10:30:00",
        main_flow_guard_score: float = 0.50,
        main_flow_guard_target_pct: float = 0.40,
        protect_local_short_floor_refill: bool = False,
        verbose: bool = False,
    ):
        super().__init__(
            initial_capital=initial_capital,
            anchor_pct=anchor_pct,
            floor_pct=floor_pct,
            ceil_pct=ceil_pct,
            commission_rate=commission_rate,
            stamp_duty_rate=stamp_duty_rate,
            min_trade_lots=min_trade_lots,
            cooldown_minutes=cooldown_minutes,
            max_day_trades=max_day_trades,
            last_signal_time=last_signal_time,
            enable_intraday_regime=False,
            enable_risk_t=False,
            enable_pump_dump_guard=False,
            macro_enter_score=cross_enter_score,
            local_enter_score=local_enter_score,
            min_target_move=min_target_move,
            verbose=verbose,
        )
        self.cross_start_time = cross_start_time
        self.cross_buy_execute_time = cross_buy_execute_time
        self.cross_enter_score = cross_enter_score
        self.cross_target_sensitivity = cross_target_sensitivity
        self.enable_cross_day = enable_cross_day
        self.enable_local_t = enable_local_t
        self.local_cover_enter_score = local_cover_enter_score
        self.local_trim_band = local_trim_band
        self.local_cover_band = local_cover_band
        self.local_trim_step_base = local_trim_step_base
        self.local_trim_step_slope = local_trim_step_slope
        self.local_buy_first_profit = local_buy_first_profit
        self.local_buy_first_stop = local_buy_first_stop
        self.local_flat_time = local_flat_time
        self.local_base_target_pct: Optional[float] = None
        self.pending_cross_buy: Optional[tuple[float, float]] = None
        self.local_t_cycle: Optional[str] = None
        self.local_t_cycle_base_pct: Optional[float] = None
        self.local_t_entry_price: Optional[float] = None
        self.local_t_entry_shares: int = 0
        self.enable_market_regime = enable_market_regime
        self.market_regime = market_regime or MarketRegimeEngine()
        self.regime_decision: Optional[MarketRegimeDecision] = None
        self.regime_rebalance_margin = regime_rebalance_margin
        self.enable_main_flow_guard = enable_main_flow_guard
        self.main_flow_guard_start_time = main_flow_guard_start_time
        self.main_flow_guard_score = main_flow_guard_score
        self.main_flow_guard_target_pct = main_flow_guard_target_pct
        self.protect_local_short_floor_refill = protect_local_short_floor_refill
        self.main_flow_guard_date: Optional[str] = None
        self.main_flow_guard_floor_pct: Optional[float] = None

    def _main_flow_guard_active_today(self) -> bool:
        return self.main_flow_guard_date == self.current_date and self.main_flow_guard_floor_pct is not None

    def _active_regime_floor_pct(self) -> float:
        if self._main_flow_guard_active_today():
            return self.main_flow_guard_floor_pct
        if self.enable_market_regime and self.regime_decision is not None:
            return self.regime_decision.target_floor_pct
        return self.floor_pct

    def _local_short_hard_floor_pct(self, active_floor: float) -> float:
        return clamp(max(self.floor_pct, active_floor - self.local_trim_band), self.floor_pct, self.ceil_pct)

    def _protected_floor_refill_target(
        self,
        current_pct: float,
        active_floor: float,
    ) -> tuple[Optional[float], bool]:
        if not (
            self.protect_local_short_floor_refill
            and self.local_t_cycle == "short"
            and self.local_t_entry_shares > 0
        ):
            return active_floor, False
        hard_floor = self._local_short_hard_floor_pct(active_floor)
        if current_pct >= hard_floor - 0.005:
            return None, True
        return hard_floor, True

    def _allow_cross_day_signals(self) -> bool:
        decision = self.regime_decision
        return self.enable_cross_day and (decision is None or decision.allow_cross_day)

    def _allow_local_t_signals(self) -> bool:
        decision = self.regime_decision
        return self.enable_local_t and (decision is None or decision.allow_local_t)

    def _score_main_flow_distribution(self, f: FactorSnapshot) -> float:
        drop = clamp((-f.day_return - 0.025) / 0.060)
        vwap_break = clamp((-f.day_vwap_dev - 0.012) / 0.035)
        high_pullback = clamp((-f.pullback_from_high - 0.025) / 0.055)
        low_range = clamp((0.35 - f.range_position) / 0.35)
        volume_push = clamp((f.vol_mom - 1.2) / 2.0)
        return clamp(
            0.30 * drop
            + 0.25 * vwap_break
            + 0.25 * high_pullback
            + 0.10 * low_range
            + 0.10 * volume_push
        )

    def _main_flow_guard_rebalance(
        self,
        factors: FactorSnapshot,
        current_price: float,
        dt: Any,
        time_str: str,
    ) -> Optional[TradeRecord]:
        decision = self.regime_decision
        if (
            not self.enable_main_flow_guard
            or decision is None
            or decision.regime != MarketRegime.UPTREND
            or self.shares <= 0
            or time_str < self.main_flow_guard_start_time
            or self.main_flow_guard_date == self.current_date
        ):
            return None
        score = self._score_main_flow_distribution(factors)
        if score < self.main_flow_guard_score or factors.day_return > -0.035:
            return None
        target_pct = clamp(self.main_flow_guard_target_pct, self.floor_pct, self.ceil_pct)
        if self.current_position_pct(current_price) <= target_pct + self.regime_rebalance_margin:
            self.main_flow_guard_date = self.current_date
            self.main_flow_guard_floor_pct = target_pct
            return None
        record = super()._align_to_target(
            current_price,
            target_pct,
            dt,
            "V6 main flow guard reduce",
            (
                f"main_flow={score:.2f} "
                f"day_ret={factors.day_return*100:.2f}% "
                f"day_dev={factors.day_vwap_dev*100:.2f}% "
                f"pullback={-factors.pullback_from_high*100:.2f}% "
                f"range_pos={factors.range_position:.2f} "
                f"vol={factors.vol_mom:.1f}x"
                + self._regime_suffix()
            ),
        )
        self.main_flow_guard_date = self.current_date
        self.main_flow_guard_floor_pct = target_pct
        if record:
            self.local_base_target_pct = record.target_pct
            self.local_t_cycle = None
            self.local_t_cycle_base_pct = None
            self.local_t_entry_price = None
            self.local_t_entry_shares = 0
            self.last_trade_dt = dt
            self.day_trade_count += 1
        return record

    def _score_cross_sell(self, f: FactorSnapshot) -> float:
        return self._score_macro_sell(f)

    def _score_cross_buy(self, f: FactorSnapshot) -> float:
        return self._score_macro_buy(f)

    def _cross_target(self, direction: str, score: float) -> float:
        signed = -1.0 if direction == "sell" else 1.0
        target = self.anchor_pct + signed * self.cross_target_sensitivity * clamp(score)
        return clamp(target, self.floor_pct, self.ceil_pct)

    def _base_for_local_t(self) -> float:
        if self.local_base_target_pct is None:
            self.local_base_target_pct = clamp(self.target_pct, self.floor_pct, self.ceil_pct)
        return self.local_base_target_pct

    def _score_local_cover(self, f: FactorSnapshot) -> float:
        base = self._base_for_local_t()
        cover_cap = min(self.ceil_pct, base + self.local_cover_band)
        if self.target_pct >= cover_cap - self.min_target_move:
            return 0.0
        score = super()._score_local_cover(f)
        return score if score >= self.local_cover_enter_score else 0.0

    def _target_for_local_t(self, direction: str, score: float) -> float:
        base = self._base_for_local_t()
        if direction == "trim":
            trim_step = self.local_trim_step_base + self.local_trim_step_slope * clamp(score)
            lower = max(self.floor_pct, base - self.local_trim_band)
            return max(self.target_pct - trim_step, lower)
        raw_target = super()._target_for_local_t(direction, score)
        upper = min(self.ceil_pct, base + self.local_cover_band)
        return min(raw_target, upper)

    # Regime guardrail methods

    def _regime_suffix(self) -> str:
        decision = self.regime_decision
        if decision is None:
            return ""
        tags = ",".join(decision.tags[:4])
        return (
            f" regime={decision.regime.value}"
            f" band={decision.target_floor_pct*100:.0f}-{decision.target_ceiling_pct*100:.0f}%"
            f" score={decision.regime_score:.2f}"
            f" tags={tags}"
        )

    def _apply_regime_target(self, target_pct: float, detail: str) -> tuple[float, str]:
        """Clamp V6's target to the regime's dynamic band."""
        decision = self.regime_decision
        if not self.enable_market_regime or decision is None:
            return clamp(target_pct, self.floor_pct, self.ceil_pct), detail
        raw_target = clamp(target_pct, self.floor_pct, self.ceil_pct)
        adjusted_target = clamp(raw_target, decision.target_floor_pct, decision.target_ceiling_pct)
        suffix = self._regime_suffix()
        if abs(adjusted_target - raw_target) > 1e-9:
            suffix += f" raw_target={raw_target*100:.1f}%"
        return adjusted_target, (detail + suffix).strip()

    def _align_to_target(
        self,
        current_price: float,
        target_pct: float,
        dt: Any,
        reason: str,
        detail: str = "",
        force_floor: bool = False,
    ) -> Optional[TradeRecord]:
        target_pct, detail = self._apply_regime_target(target_pct, detail)
        return super()._align_to_target(current_price, target_pct, dt, reason, detail, force_floor=force_floor)

    def _regime_rebalance(
        self,
        current_price: float,
        dt: Any,
        time_str: str,
    ) -> Optional[TradeRecord]:
        """Rebalance if position is significantly outside the regime band.

        This is a simple guardrail: reduce if too high, restore if too low.
        Uses regime_rebalance_margin to avoid churning near band edges.
        """
        decision = self.regime_decision
        if not self.enable_market_regime or decision is None or self.shares <= 0:
            return None
        if not self._can_signal(dt, time_str, start_time="10:00:00"):
            return None

        current_pct = self.current_position_pct(current_price)
        margin = self.regime_rebalance_margin

        # If position significantly above ceiling 鈫?reduce to ceiling
        if current_pct > decision.target_ceiling_pct + margin:
            record = super()._align_to_target(
                current_price,
                decision.target_ceiling_pct,
                dt,
                "V6 regime cap reduce",
                decision.detail + self._regime_suffix(),
            )
            if record:
                self.local_base_target_pct = record.target_pct
                self.local_t_cycle = None
                self.local_t_cycle_base_pct = None
                self.local_t_entry_price = None
                self.local_t_entry_shares = 0
                self.last_trade_dt = dt
                self.day_trade_count += 1
            return record

        # If position significantly below floor 鈫?restore to floor (only after 14:00)
        if self._main_flow_guard_active_today():
            return None

        floor_target = self._active_regime_floor_pct()
        floor_target, protected_short = self._protected_floor_refill_target(current_pct, floor_target)
        if floor_target is None:
            return None

        restore_start_time = "10:00:00" if decision.regime == MarketRegime.UPTREND else self.cross_buy_execute_time
        if current_pct < floor_target - margin and time_str >= restore_start_time:
            reason = "V6 local short hard-floor restore" if protected_short else "V6 regime floor restore"
            detail = decision.detail + self._regime_suffix()
            if protected_short:
                detail = (
                    detail
                    + f" short_refill_lock=1 hard_floor={floor_target*100:.1f}%"
                ).strip()
            record = super()._align_to_target(
                current_price,
                floor_target,
                dt,
                reason,
                detail,
                force_floor=True,
            )
            if record:
                self.local_base_target_pct = record.target_pct
                if not protected_short:
                    self.local_t_cycle = None
                    self.local_t_cycle_base_pct = None
                    self.local_t_entry_price = None
                    self.local_t_entry_shares = 0
                self.last_trade_dt = dt
                self.day_trade_count += 1
            return record

        return None

    # Scoring and details

    def _score_buy_timing(self, f: FactorSnapshot) -> float:
        day_cheap = clamp((-f.day_vwap_dev - 0.002) / 0.022)
        local_cheap = clamp((-f.local_vwap_dev - 0.002) / 0.014)
        rebound = clamp(f.acceleration / 0.008)
        volume = clamp((f.vol_mom - 1.0) / 1.8)
        return clamp(0.36 * day_cheap + 0.24 * local_cheap + 0.24 * rebound + 0.16 * volume)

    def _score_sell_timing(self, f: FactorSnapshot) -> float:
        day_rich = clamp((f.day_vwap_dev - 0.010) / 0.030)
        momentum_fade = clamp((-f.acceleration) / 0.008)
        local_rich = clamp((f.local_vwap_dev - 0.004) / 0.018)
        high_return = clamp((f.high_return - 0.018) / 0.050)
        return clamp(0.36 * day_rich + 0.28 * momentum_fade + 0.20 * local_rich + 0.16 * high_return)

    def _cross_detail(self, f: FactorSnapshot, side: str, score: float, target: float) -> str:
        timing = self._score_sell_timing(f) if side == "sell" else self._score_buy_timing(f)
        return (
            f"cross_{side}={score:.2f} "
            f"target={target*100:.1f}% "
            f"timing={timing:.2f} "
            f"day_dev={f.day_vwap_dev*100:.2f}% "
            f"local_dev={f.local_vwap_dev*100:.2f}% "
            f"vel={f.velocity*100:.2f}% "
            f"acc={f.acceleration*100:.2f}% "
            f"vol={f.vol_mom:.1f}x"
        )

    def _local_cycle_detail(self, f: FactorSnapshot, action: str, score: float, current_price: float) -> str:
        entry_ret = 0.0
        if self.local_t_entry_price and self.local_t_entry_price > 0:
            entry_ret = current_price / self.local_t_entry_price - 1.0
        base = self._base_for_local_t()
        return (
            self._detail(f, action, score)
            + f" base={base*100:.1f}%"
            + f" entry_ret={entry_ret*100:.2f}%"
            + f" cycle={self.local_t_cycle or 'none'}"
        )

    def _close_local_cycle_to_base(
        self,
        current_price: float,
        dt: Any,
        reason: str,
        detail: str,
    ) -> Optional[TradeRecord]:
        base = self._base_for_local_t()
        shares = int(self.local_t_entry_shares / 100) * 100
        if shares <= 0:
            return self._align_to_target(current_price, base, dt, reason, detail, force_floor=True)
        if self.local_t_cycle == "long":
            return self._sell(current_price, min(shares, self.shares), dt, base, reason, detail)
        if self.local_t_cycle == "short":
            return self._buy(current_price, shares, dt, base, reason, detail)
        return None

    def _reset_new_day_state(self, date_str: str) -> bool:
        if self.current_date == date_str:
            return False
        self.current_date = date_str
        self.day_trade_count = 0
        self._base_for_local_t()
        self.pending_cross_buy = None
        self.main_flow_guard_floor_pct = None
        return True

    # Main tick handler

    def on_tick(self, tick: Dict[str, Any]) -> Optional[TradeRecord]:
        dt = parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
        if dt is None:
            return None
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
        if time_str < "09:30:00" or time_str > "15:00:00":
            return None

        current_price = float(tick.get("Close", tick.get("price", 0.0)) or 0.0)
        if current_price <= 0:
            return None

        is_new_day = self._reset_new_day_state(date_str)
        factors = self.factor_calc.update(tick, is_new_day)
        self.regime_decision = self.market_regime.update(tick) if self.enable_market_regime else None
        allow_cross_day = self._allow_cross_day_signals()
        allow_local_t = self._allow_local_t_signals()
        if not allow_cross_day:
            self.pending_cross_buy = None
        if not allow_local_t:
            self.local_t_cycle = None
            self.local_t_cycle_base_pct = None
            self.local_t_entry_price = None
            self.local_t_entry_shares = 0

        flow_guard_record = self._main_flow_guard_rebalance(factors, current_price, dt, time_str)
        if flow_guard_record:
            return flow_guard_record

        active_floor = self._active_regime_floor_pct()
        current_pct = self.current_position_pct(current_price)
        floor_refill_target, protected_short = self._protected_floor_refill_target(current_pct, active_floor)
        if (
            self.shares > 0
            and not self._main_flow_guard_active_today()
            and floor_refill_target is not None
            and current_pct < floor_refill_target - 0.005
        ):
            reason = "V6 local short hard-floor refill" if protected_short else "V6 floor refill"
            detail = self._detail(factors, "floor", 1.0)
            if protected_short:
                detail = (
                    detail
                    + f" short_refill_lock=1 active_floor={active_floor*100:.1f}%"
                    + f" hard_floor={floor_refill_target*100:.1f}%"
                )
            align_to_target = super()._align_to_target if protected_short else self._align_to_target
            record = align_to_target(
                current_price,
                floor_refill_target,
                dt,
                reason,
                detail,
                force_floor=True,
            )
            if record:
                self.last_trade_dt = dt
                self.day_trade_count += 1
                return record

        # Regime guardrail: rebalance if position is significantly outside band
        regime_record = self._regime_rebalance(current_price, dt, time_str)
        if regime_record:
            return regime_record

        if not self._can_signal(dt, time_str, start_time=self.cross_start_time):
            return None

        # V6 signals: no multipliers, regime only constrains the final target
        cross_sell = self._score_cross_sell(factors) if allow_cross_day else 0.0
        cross_buy = self._score_cross_buy(factors) if allow_cross_day else 0.0
        local_trim = self._score_local_trim(factors) if allow_local_t else 0.0
        local_cover = self._score_local_cover(factors) if allow_local_t else 0.0

        record: Optional[TradeRecord] = None
        next_local_cycle: Optional[str] = self.local_t_cycle
        next_local_entry_price: Optional[float] = self.local_t_entry_price
        if cross_buy >= self.cross_enter_score and cross_buy >= cross_sell:
            target = self._cross_target("buy", cross_buy)
            if target > self.target_pct + self.min_target_move:
                self.pending_cross_buy = (cross_buy, target)

        if cross_sell >= self.cross_enter_score and cross_sell >= cross_buy:
            target = self._cross_target("sell", cross_sell)
            if target < self.target_pct - self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    "V6 cross-day reduce",
                    self._cross_detail(factors, "sell", cross_sell, target),
                )
        elif self.pending_cross_buy is not None and time_str >= self.cross_buy_execute_time:
            pending_score, pending_target = self.pending_cross_buy
            if pending_target > self.target_pct + self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    pending_target,
                    dt,
                    "V6 cross-day add",
                    self._cross_detail(factors, "buy", pending_score, pending_target) + " exec=delayed_buy",
                )
                if record:
                    self.pending_cross_buy = None
        elif (
            allow_local_t
            and self.local_t_cycle == "long"
            and self.local_t_entry_price is not None
            and self.target_pct > self._base_for_local_t() + self.min_target_move
            and (
                current_price / self.local_t_entry_price - 1.0 >= self.local_buy_first_profit
                or current_price / self.local_t_entry_price - 1.0 <= -self.local_buy_first_stop
                or time_str >= self.local_flat_time
            )
        ):
            entry_ret = current_price / self.local_t_entry_price - 1.0
            reason = "V6 local long profit exit"
            if entry_ret <= -self.local_buy_first_stop:
                reason = "V6 local long stop exit"
            elif time_str >= self.local_flat_time:
                reason = "V6 local long time exit"
            record = self._close_local_cycle_to_base(
                current_price,
                dt,
                reason,
                self._local_cycle_detail(factors, "long_exit", 1.0, current_price),
            )
            if record:
                next_local_cycle = None
                next_local_entry_price = None
        elif local_trim >= self.local_enter_score:
            base = self._base_for_local_t()
            if self.local_t_cycle == "long" and self.target_pct > base + self.min_target_move:
                reason = "V6 local long trim exit"
                detail = self._local_cycle_detail(factors, "long_trim", local_trim, current_price)
                record = self._close_local_cycle_to_base(current_price, dt, reason, detail)
                if record:
                    next_local_cycle = None
                    next_local_entry_price = None
            else:
                target = self._target_for_local_t("trim", local_trim)
                reason = "V6 local trim"
                detail = self._detail(factors, "trim", local_trim)
                if target < base - self.min_target_move:
                    next_local_cycle = "short"
                    next_local_entry_price = current_price
                record = self._align_to_target(
                    current_price,
                    target,
                    dt,
                    reason,
                    detail,
                )
        elif local_cover >= self.local_enter_score:
            base = self._base_for_local_t()
            raw_target = self._target_for_local_t("cover", local_cover)
            if self.target_pct < base - self.min_target_move:
                target = min(raw_target, base)
                reason = "V6 local short cover"
                detail = self._local_cycle_detail(factors, "short_cover", local_cover, current_price)
                if target >= base - self.min_target_move:
                    next_local_cycle = None
                    next_local_entry_price = None
            else:
                target = raw_target
                reason = "V6 local long entry"
                detail = self._detail(factors, "long_entry", local_cover)
                if target > base + self.min_target_move:
                    next_local_cycle = "long"
                    next_local_entry_price = current_price
            record = self._align_to_target(
                current_price,
                target,
                dt,
                reason,
                detail,
            )

        if record:
            if "cross-day" in record.reason or "regime floor restore" in record.reason:
                self.local_base_target_pct = record.target_pct
                self.local_t_cycle = None
                self.local_t_cycle_base_pct = None
                self.local_t_entry_price = None
                self.local_t_entry_shares = 0
            else:
                self.local_t_cycle = next_local_cycle
                self.local_t_cycle_base_pct = self._base_for_local_t() if next_local_cycle else None
                self.local_t_entry_price = next_local_entry_price
                if next_local_cycle and record.reason in {"V6 local trim", "V6 local long entry"}:
                    self.local_t_entry_shares = record.shares
                elif next_local_cycle is None:
                    self.local_t_entry_shares = 0
            self.last_trade_dt = dt
            self.day_trade_count += 1
        return record

