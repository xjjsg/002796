"""Market regime module v4 – Mean-Reversion & Volatility Based.

Designed specifically for 002796 (a high volatility, mean-reverting stock).
Abandons MA-alignment (which was too lagging and missed rebounds).
Instead, uses price-action anomalies and volume to define 4 states:
1. OVERSOLD_BOUNCE: Price broke recent lows + below VWAP. High probability of bounce.
   Action: Floor 60%, Ceiling 100%. Protect against panic selling.
2. HIGH_VOLUME_TREND: Volume > 1.33x MA5_Vol + above VWAP. Real money pushing.
   Action: Floor 50%, Ceiling 100%. Allow strategy to ride the wave.
3. EXHAUSTION: High (above MA5) but making lower highs OR breaking VWAP while making higher lows.
   Action: Floor 30%, Ceiling 70%. Force profit taking before mean-reversion.
4. RANGE: Normal oscillation.
   Action: Floor 40%, Ceiling 100%. V5 operates freely.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class MarketRegime(str, Enum):
    OVERSOLD_BOUNCE = "OVERSOLD_BOUNCE"
    HIGH_VOLUME_TREND = "HIGH_VOLUME_TREND"
    EXHAUSTION = "EXHAUSTION"
    RANGE = "RANGE"


@dataclass(frozen=True)
class MarketRegimeDecision:
    regime: MarketRegime
    tags: tuple[str, ...]
    confidence: float
    target_floor_pct: float
    target_ceiling_pct: float
    regime_score: float  # continuous measure of state intensity
    detail: str


@dataclass
class _DailySummary:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    vwap: float


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class MarketRegimeEngine:
    def __init__(
        self,
        bounce_floor: float = 0.60,
        bounce_ceiling: float = 1.00,
        trend_floor: float = 0.50,
        trend_ceiling: float = 1.00,
        exhaustion_floor: float = 0.30,
        exhaustion_ceiling: float = 0.70,
        range_floor: float = 0.40,
        range_ceiling: float = 1.00,
        min_history: int = 5,
    ):
        self.bounce_floor = bounce_floor
        self.bounce_ceiling = bounce_ceiling
        self.trend_floor = trend_floor
        self.trend_ceiling = trend_ceiling
        self.exhaustion_floor = exhaustion_floor
        self.exhaustion_ceiling = exhaustion_ceiling
        self.range_floor = range_floor
        self.range_ceiling = range_ceiling
        self.min_history = min_history
        self.completed_days: list[_DailySummary] = []
        self.current_day: _DailySummary | None = None
        self.last_decision: MarketRegimeDecision | None = None

    def update(self, tick: dict[str, Any]) -> MarketRegimeDecision:
        dt = _parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
        date_str = dt.strftime("%Y-%m-%d") if dt else str(tick.get("date", ""))
        price = float(tick.get("price", tick.get("Close", 0.0)) or 0.0)
        open_price = float(tick.get("open", price) or price)
        high = float(tick.get("high", price) or price)
        low = float(tick.get("low", price) or price)
        volume = float(tick.get("Volume", tick.get("cum_volume", 0.0)) or 0.0)
        amount = float(tick.get("Amount", tick.get("cum_amount", 0.0)) or 0.0)
        vwap = amount / volume if volume > 0 and amount > 0 else price

        if self.current_day is not None and self.current_day.date != date_str:
            self.completed_days.append(self.current_day)
            self.current_day = None

        if self.current_day is None:
            self.current_day = _DailySummary(
                date=date_str,
                open=open_price if open_price > 0 else price,
                high=max(high, price),
                low=min(low, price) if low > 0 else price,
                close=price,
                volume=volume,
                amount=amount,
                vwap=vwap,
            )
        else:
            self.current_day.high = max(self.current_day.high, high, price)
            self.current_day.low = min(self.current_day.low, low, price) if low > 0 else min(self.current_day.low, price)
            self.current_day.close = price
            self.current_day.volume = volume
            self.current_day.amount = amount
            self.current_day.vwap = vwap

        decision = self._classify(self.current_day, dt)
        self.last_decision = decision
        return decision

    def _classify(self, day: _DailySummary, dt: datetime | None) -> MarketRegimeDecision:
        history = self.completed_days
        price = day.close
        tags: list[str] = []

        above_vwap = day.vwap > 0 and price >= day.vwap
        tags.append("above_vwap" if above_vwap else "below_vwap")

        prev5 = history[-5:]
        prev20 = history[-20:]
        ma5 = _mean([d.close for d in prev5]) if prev5 else price
        recent_low = min((d.low for d in prev5), default=price)
        recent_high = max((d.high for d in prev5), default=price)
        major_low = min((d.low for d in prev20), default=price)
        
        avg_volume = _mean([d.volume for d in prev5]) if prev5 else 0.0
        
        # Project intraday volume to full day (240 minutes)
        minutes_passed = 240
        if dt:
            h, m = dt.hour, dt.minute
            if h == 9 and m >= 30: minutes_passed = m - 30
            elif h == 10: minutes_passed = 30 + m
            elif h == 11 and m <= 30: minutes_passed = 90 + m
            elif h >= 13 and h < 15: minutes_passed = 120 + (h - 13) * 60 + m
            elif h == 15: minutes_passed = 240
            minutes_passed = max(1, min(240, minutes_passed))
            
        projected_volume = day.volume * (240.0 / minutes_passed) if minutes_passed < 240 else day.volume
        volume_ratio = projected_volume / avg_volume if avg_volume > 0 else 0.0
        
        is_high_volume = volume_ratio >= 1.50
        if is_high_volume:
            tags.append("high_volume")
            
        is_break_low = False
        if price > 0 and recent_low > 0 and price < recent_low:
            is_break_low = True
            tags.append("break_recent_low")
        if price > 0 and major_low > 0 and price < major_low:
            is_break_low = True
            tags.append("break_major_low")

        is_lower_highs = False
        if recent_high > 0 and prev5:
            major_high = max((d.high for d in prev20), default=price)
            # Only trigger lower_highs if we are close to MA5 but significantly below major high
            if recent_high < major_high * 0.90 and price < ma5 * 1.02:
                is_lower_highs = True
                tags.append("lower_highs")
                
        is_higher_lows = False
        if recent_low > 0 and prev5:
            if recent_low > major_low * 1.04:
                is_higher_lows = True
                tags.append("higher_lows")

        if ma5 > 0 and price >= ma5:
            tags.append("above_ma5")
        elif ma5 > 0:
            tags.append("below_ma5")

        # ── classification ───────────────────────────────────────────
        if len(history) < self.min_history:
            return self._make_decision(MarketRegime.RANGE, tags, 0.5, self.range_floor, self.range_ceiling, "warmup")

        regime = MarketRegime.RANGE
        floor_pct = self.range_floor
        ceiling_pct = self.range_ceiling
        score = 0.5

        if is_break_low and not above_vwap:
            regime = MarketRegime.OVERSOLD_BOUNCE
            floor_pct = self.bounce_floor
            ceiling_pct = self.bounce_ceiling
            score = 1.0
        elif is_high_volume and above_vwap:
            regime = MarketRegime.HIGH_VOLUME_TREND
            floor_pct = self.trend_floor
            ceiling_pct = self.trend_ceiling
            score = min(1.0, 0.6 + volume_ratio * 0.1)
        elif (price >= ma5 and is_lower_highs) or (is_higher_lows and not above_vwap):
            regime = MarketRegime.EXHAUSTION
            floor_pct = self.exhaustion_floor
            ceiling_pct = self.exhaustion_ceiling
            score = 0.0
            
        # Confidence based on volatility/deviation
        day_dev = abs(price / day.vwap - 1.0) if day.vwap > 0 else 0.0
        confidence = max(0.4, min(0.9, 0.4 + day_dev * 10))

        return self._make_decision(
            regime, tags, score, floor_pct, ceiling_pct,
            f"regime={regime.value} px={price:.2f} vol_ratio={volume_ratio:.2f} score={score:.2f}"
        )

    def _make_decision(
        self,
        regime: MarketRegime,
        tags: list[str],
        regime_score: float,
        floor_pct: float,
        ceiling_pct: float,
        detail: str,
    ) -> MarketRegimeDecision:
        return MarketRegimeDecision(
            regime=regime,
            tags=tuple(tags),
            confidence=0.8,
            target_floor_pct=round(floor_pct, 4),
            target_ceiling_pct=round(ceiling_pct, 4),
            regime_score=round(regime_score, 4),
            detail=detail,
        )
