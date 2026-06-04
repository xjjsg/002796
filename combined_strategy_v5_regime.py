from __future__ import annotations

from typing import Any, Dict, Optional

from market_regime import MarketRegimeDecision, MarketRegimeEngine
from strategy_core import (
    ANCHOR_PCT,
    CEIL_PCT,
    FLOOR_PCT,
    COMMISSION_RATE,
    STAMP_DUTY_RATE,
    BaseStrategy,
    FactorSnapshot,
    TradeRecord,
    _clamp,
    _parse_dt,
)


class CombinedStrategyV5Regime(BaseStrategy):
    """
    V5 with score-based regime guardrails.

    The regime module only controls the position band (floor/ceiling).
    V5's own cross-day and local-T signals decide targets within that band.
    No signal multipliers — the regime module is a guardrail, not a direction caller.
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
        max_day_trades: int = 3,
        last_signal_time: str = "14:45:00",
        cross_start_time: str = "10:00:00",
        cross_buy_execute_time: str = "14:00:00",
        cross_enter_score: float = 0.25,
        cross_target_sensitivity: float = 0.38,
        local_enter_score: float = 0.70,
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

    def _score_cross_sell(self, f: FactorSnapshot) -> float:
        return self._score_macro_sell(f)

    def _score_cross_buy(self, f: FactorSnapshot) -> float:
        return self._score_macro_buy(f)

    def _cross_target(self, direction: str, score: float) -> float:
        signed = -1.0 if direction == "sell" else 1.0
        target = self.anchor_pct + signed * self.cross_target_sensitivity * _clamp(score)
        return _clamp(target, self.floor_pct, self.ceil_pct)

    def _base_for_local_t(self) -> float:
        if self.local_base_target_pct is None:
            self.local_base_target_pct = _clamp(self.target_pct, self.floor_pct, self.ceil_pct)
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
            trim_step = self.local_trim_step_base + self.local_trim_step_slope * _clamp(score)
            lower = max(self.floor_pct, base - self.local_trim_band)
            return max(self.target_pct - trim_step, lower)
        raw_target = super()._target_for_local_t(direction, score)
        upper = min(self.ceil_pct, base + self.local_cover_band)
        return min(raw_target, upper)

    # ── regime guardrail methods ─────────────────────────────────────

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
        """Clamp V5's target to the regime's dynamic band."""
        decision = self.regime_decision
        if not self.enable_market_regime or decision is None:
            return _clamp(target_pct, self.floor_pct, self.ceil_pct), detail
        raw_target = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        adjusted_target = _clamp(raw_target, decision.target_floor_pct, decision.target_ceiling_pct)
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

        This is a simple guardrail — reduce if too high, restore if too low.
        Uses regime_rebalance_margin to avoid churning near band edges.
        """
        decision = self.regime_decision
        if not self.enable_market_regime or decision is None or self.shares <= 0:
            return None
        if not self._can_signal(dt, time_str, start_time="10:00:00"):
            return None

        current_pct = self.current_position_pct(current_price)
        margin = self.regime_rebalance_margin

        # If position significantly above ceiling → reduce to ceiling
        if current_pct > decision.target_ceiling_pct + margin:
            record = super()._align_to_target(
                current_price,
                decision.target_ceiling_pct,
                dt,
                "V5R regime cap reduce",
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

        # If position significantly below floor → restore to floor (only after 14:00)
        if (
            current_pct < decision.target_floor_pct - margin
            and time_str >= self.cross_buy_execute_time
        ):
            record = super()._align_to_target(
                current_price,
                decision.target_floor_pct,
                dt,
                "V5R regime floor restore",
                decision.detail + self._regime_suffix(),
                force_floor=True,
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

        return None

    # ── scoring & details ────────────────────────────────────────────

    def _score_buy_timing(self, f: FactorSnapshot) -> float:
        day_cheap = _clamp((-f.day_vwap_dev - 0.002) / 0.022)
        local_cheap = _clamp((-f.local_vwap_dev - 0.002) / 0.014)
        rebound = _clamp(f.acceleration / 0.008)
        volume = _clamp((f.vol_mom - 1.0) / 1.8)
        return _clamp(0.36 * day_cheap + 0.24 * local_cheap + 0.24 * rebound + 0.16 * volume)

    def _score_sell_timing(self, f: FactorSnapshot) -> float:
        day_rich = _clamp((f.day_vwap_dev - 0.010) / 0.030)
        momentum_fade = _clamp((-f.acceleration) / 0.008)
        local_rich = _clamp((f.local_vwap_dev - 0.004) / 0.018)
        high_return = _clamp((f.high_return - 0.018) / 0.050)
        return _clamp(0.36 * day_rich + 0.28 * momentum_fade + 0.20 * local_rich + 0.16 * high_return)

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
        return True

    # ── main tick handler ────────────────────────────────────────────

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

        is_new_day = self._reset_new_day_state(date_str)
        factors = self.factor_calc.update(tick, is_new_day)
        self.regime_decision = self.market_regime.update(tick) if self.enable_market_regime else None

        if self.shares > 0 and self.current_position_pct(current_price) < self.floor_pct - 0.005:
            record = self._align_to_target(
                current_price,
                self.floor_pct,
                dt,
                "V5 floor refill",
                self._detail(factors, "floor", 1.0),
                force_floor=True,
            )
            if record:
                return record

        # Regime guardrail: rebalance if position is significantly outside band
        regime_record = self._regime_rebalance(current_price, dt, time_str)
        if regime_record:
            return regime_record

        if not self._can_signal(dt, time_str, start_time=self.cross_start_time):
            return None

        # V5 signals — NO multipliers, regime only constrains the final target
        cross_sell = self._score_cross_sell(factors) if self.enable_cross_day else 0.0
        cross_buy = self._score_cross_buy(factors) if self.enable_cross_day else 0.0
        local_trim = self._score_local_trim(factors) if self.enable_local_t else 0.0
        local_cover = self._score_local_cover(factors) if self.enable_local_t else 0.0

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
                    "V5 cross-day reduce",
                    self._cross_detail(factors, "sell", cross_sell, target),
                )
        elif self.pending_cross_buy is not None and time_str >= self.cross_buy_execute_time:
            pending_score, pending_target = self.pending_cross_buy
            if pending_target > self.target_pct + self.min_target_move:
                record = self._align_to_target(
                    current_price,
                    pending_target,
                    dt,
                    "V5 cross-day add",
                    self._cross_detail(factors, "buy", pending_score, pending_target) + " exec=delayed_buy",
                )
                if record:
                    self.pending_cross_buy = None
        elif (
            self.enable_local_t
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
            reason = "V5 local long profit exit"
            if entry_ret <= -self.local_buy_first_stop:
                reason = "V5 local long stop exit"
            elif time_str >= self.local_flat_time:
                reason = "V5 local long time exit"
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
                reason = "V5 local long trim exit"
                detail = self._local_cycle_detail(factors, "long_trim", local_trim, current_price)
                record = self._close_local_cycle_to_base(current_price, dt, reason, detail)
                if record:
                    next_local_cycle = None
                    next_local_entry_price = None
            else:
                target = self._target_for_local_t("trim", local_trim)
                reason = "V5 local trim"
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
                reason = "V5 local short cover"
                detail = self._local_cycle_detail(factors, "short_cover", local_cover, current_price)
                if target >= base - self.min_target_move:
                    next_local_cycle = None
                    next_local_entry_price = None
            else:
                target = raw_target
                reason = "V5 local long entry"
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
                if next_local_cycle and record.reason in {"V5 local trim", "V5 local long entry"}:
                    self.local_t_entry_shares = record.shares
                elif next_local_cycle is None:
                    self.local_t_entry_shares = 0
            self.last_trade_dt = dt
            self.day_trade_count += 1
        return record
