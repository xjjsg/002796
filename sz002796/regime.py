"""Recent market state classifier for the regime-aware strategy.

The regime layer is intentionally coarse.  It answers one question only:
is the recent market in a one-way uptrend, a one-way downtrend, or a range?

Trading interpretation:
- UPTREND: stay heavily invested; T/cross-day trims are mostly noise.
- DOWNTREND: keep risk low; bargain-hunting buys are mostly noise.
- RANGE: hand control back to the base strategy; intraday T and cross-day adjustment matter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class MarketRegime(str, Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGE = "RANGE"


@dataclass(frozen=True)
class MarketRegimeDecision:
    regime: MarketRegime
    tags: tuple[str, ...]
    confidence: float
    target_floor_pct: float
    target_ceiling_pct: float
    regime_score: float
    detail: str
    allow_cross_day: bool = True
    allow_local_t: bool = True


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


def parse_dt(value: Any) -> datetime | None:
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


def _signed_clamp(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return max(-1.0, min(1.0, value / scale))


class MarketRegimeEngine:
    def __init__(
        self,
        uptrend_floor: float = 0.95,
        uptrend_ceiling: float = 1.00,
        downtrend_floor: float = 0.00,
        downtrend_ceiling: float = 0.60,
        range_floor: float = 0.40,
        range_ceiling: float = 1.00,
        min_history: int = 5,
        uptrend_ret5: float = 0.08,
        uptrend_ret10: float = 0.12,
        uptrend_max_drawdown: float = 0.10,
        downtrend_drawdown: float = 0.12,
        downtrend_ret5: float = -0.035,
        downtrend_ret10: float = -0.10,
        downtrend_exit_ret5: float = 0.08,
    ):
        self.uptrend_floor = uptrend_floor
        self.uptrend_ceiling = uptrend_ceiling
        self.downtrend_floor = downtrend_floor
        self.downtrend_ceiling = downtrend_ceiling
        self.range_floor = range_floor
        self.range_ceiling = range_ceiling
        self.min_history = min_history
        self.uptrend_ret5 = uptrend_ret5
        self.uptrend_ret10 = uptrend_ret10
        self.uptrend_max_drawdown = uptrend_max_drawdown
        self.downtrend_drawdown = downtrend_drawdown
        self.downtrend_ret5 = downtrend_ret5
        self.downtrend_ret10 = downtrend_ret10
        self.downtrend_exit_ret5 = downtrend_exit_ret5
        self.completed_days: list[_DailySummary] = []
        self.current_day: _DailySummary | None = None
        self.last_decision: MarketRegimeDecision | None = None
        self._last_classified_asof: str | None = None

    @staticmethod
    def _decision_to_dict(decision: MarketRegimeDecision | None) -> dict[str, Any] | None:
        if decision is None:
            return None
        return {
            "regime": decision.regime.value,
            "tags": list(decision.tags),
            "confidence": decision.confidence,
            "target_floor_pct": decision.target_floor_pct,
            "target_ceiling_pct": decision.target_ceiling_pct,
            "regime_score": decision.regime_score,
            "detail": decision.detail,
            "allow_cross_day": decision.allow_cross_day,
            "allow_local_t": decision.allow_local_t,
        }

    @staticmethod
    def _decision_from_dict(data: dict[str, Any] | None) -> MarketRegimeDecision | None:
        if not isinstance(data, dict):
            return None
        try:
            regime = MarketRegime(data.get("regime", MarketRegime.RANGE.value))
        except ValueError:
            regime = MarketRegime.RANGE
        return MarketRegimeDecision(
            regime=regime,
            tags=tuple(data.get("tags", ()) or ()),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            target_floor_pct=float(data.get("target_floor_pct", 0.0) or 0.0),
            target_ceiling_pct=float(data.get("target_ceiling_pct", 1.0) or 1.0),
            regime_score=float(data.get("regime_score", 0.0) or 0.0),
            detail=str(data.get("detail", "")),
            allow_cross_day=bool(data.get("allow_cross_day", True)),
            allow_local_t=bool(data.get("allow_local_t", True)),
        )

    @staticmethod
    def _daily_from_dict(data: dict[str, Any] | None) -> _DailySummary | None:
        if not isinstance(data, dict):
            return None
        try:
            return _DailySummary(
                date=str(data.get("date", "")),
                open=float(data.get("open", 0.0) or 0.0),
                high=float(data.get("high", 0.0) or 0.0),
                low=float(data.get("low", 0.0) or 0.0),
                close=float(data.get("close", 0.0) or 0.0),
                volume=float(data.get("volume", 0.0) or 0.0),
                amount=float(data.get("amount", 0.0) or 0.0),
                vwap=float(data.get("vwap", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            return None

    def export_state(self) -> dict[str, Any]:
        return {
            "completed_days": [asdict(day) for day in self.completed_days],
            "current_day": asdict(self.current_day) if self.current_day is not None else None,
            "last_decision": self._decision_to_dict(self.last_decision),
            "last_classified_asof": self._last_classified_asof,
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        if not isinstance(state, dict):
            return
        completed_days: list[_DailySummary] = []
        for row in state.get("completed_days", []) or []:
            day = self._daily_from_dict(row)
            if day is not None and day.date:
                completed_days.append(day)
        self.completed_days = completed_days
        self.current_day = self._daily_from_dict(state.get("current_day"))
        self.last_decision = self._decision_from_dict(state.get("last_decision"))
        classified_asof = state.get("last_classified_asof")
        self._last_classified_asof = str(classified_asof) if classified_asof else None

    def update(self, tick: dict[str, Any]) -> MarketRegimeDecision:
        dt = parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
        date_str = dt.strftime("%Y-%m-%d") if dt else str(tick.get("date", ""))
        price = float(tick.get("price", tick.get("Close", 0.0)) or 0.0)
        open_price = float(tick.get("open", price) or price)
        high = float(tick.get("high", tick.get("High", price)) or price)
        low = float(tick.get("low", tick.get("Low", price)) or price)
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

        asof_date = (
            self.completed_days[-1].date
            if len(self.completed_days) >= self.min_history
            else f"warmup:{self.current_day.date}"
        )
        if self.last_decision is not None and self._last_classified_asof == asof_date:
            return self.last_decision

        decision = self._classify(self.current_day)
        self._last_classified_asof = asof_date
        self.last_decision = decision
        return decision

    def _classify(self, day: _DailySummary) -> MarketRegimeDecision:
        # Use only completed sessions for the actionable state.  The current
        # intraday price is deliberately excluded so the regime does not churn
        # between RANGE and trend states inside one trading day.
        days = self.completed_days
        tags: list[str] = []
        if len(days) < self.min_history:
            return self._make_decision(
                MarketRegime.RANGE,
                ["warmup"],
                0.0,
                self.range_floor,
                self.range_ceiling,
                "warmup",
                True,
                True,
            )

        closes = [d.close for d in days]
        highs = [d.high for d in days]
        lows = [d.low for d in days]
        price = closes[-1]
        ma5 = _mean(closes[-5:])
        ma10 = _mean(closes[-10:]) if len(closes) >= 10 else _mean(closes)
        high20 = max(highs[-20:])
        low20 = min(lows[-20:])
        high10 = max(highs[-10:])
        low10 = min(lows[-10:])
        ret5 = price / closes[-6] - 1.0 if len(closes) >= 6 and closes[-6] > 0 else price / closes[0] - 1.0
        ret10 = price / closes[-11] - 1.0 if len(closes) >= 11 and closes[-11] > 0 else price / closes[0] - 1.0
        dd20 = price / high20 - 1.0 if high20 > 0 else 0.0
        range20 = high20 / low20 - 1.0 if low20 > 0 else 0.0
        recent_changes = [closes[i] / closes[i - 1] - 1.0 for i in range(max(1, len(closes) - 5), len(closes))]
        down_days_5 = sum(1 for value in recent_changes if value < -0.005)
        up_days_5 = sum(1 for value in recent_changes if value > 0.005)

        if price >= ma5:
            tags.append("above_ma5")
        else:
            tags.append("below_ma5")
        if price >= ma10:
            tags.append("above_ma10")
        else:
            tags.append("below_ma10")
        if abs(dd20) <= 0.03:
            tags.append("near_20d_high")
        if price <= low10 * 1.03:
            tags.append("near_10d_low")
        if ret5 >= self.uptrend_ret5:
            tags.append("strong_ret5")
        elif ret5 <= self.downtrend_ret5:
            tags.append("weak_ret5")
        if ret10 >= self.uptrend_ret10:
            tags.append("strong_ret10")
        elif ret10 <= self.downtrend_ret10:
            tags.append("weak_ret10")
        if down_days_5 >= 3:
            tags.append("persistent_down")
        if up_days_5 >= 3:
            tags.append("persistent_up")
        two_sided_chop = up_days_5 >= 2 and down_days_5 >= 2 and price > low20 * 1.03
        if two_sided_chop:
            tags.append("two_sided_chop")

        up_score = (
            0.40 * _signed_clamp(ret5, 0.16)
            + 0.35 * _signed_clamp(ret10, 0.28)
            + 0.25 * _signed_clamp((price / ma10 - 1.0) if ma10 > 0 else 0.0, 0.12)
        )
        down_score = (
            0.45 * _signed_clamp(-dd20, 0.22)
            + 0.30 * _signed_clamp(-ret5, 0.16)
            + 0.25 * _signed_clamp(-ret10, 0.25)
        )

        raw_uptrend = (
            price >= ma5
            and price >= ma10
            and dd20 >= -self.uptrend_max_drawdown
            and (ret5 >= self.uptrend_ret5 or ret10 >= self.uptrend_ret10 or price >= high10 * 0.97)
        )
        raw_downtrend = (
            dd20 <= -self.downtrend_drawdown
            and price < ma5
            and down_days_5 >= 3
            and (ret5 <= self.downtrend_ret5 or ret10 <= self.downtrend_ret10 or price <= low10 * 1.03)
        )
        uptrend_break = (
            dd20 <= -0.07
            and price < ma5
            and (ret5 < 0.0 or down_days_5 >= 2)
        )

        previous = self.last_decision.regime if self.last_decision is not None else MarketRegime.RANGE
        regime = MarketRegime.RANGE
        if previous == MarketRegime.UPTREND:
            if uptrend_break or raw_downtrend:
                regime = MarketRegime.DOWNTREND
            elif dd20 <= -self.uptrend_max_drawdown and price < ma5 and ret5 < 0:
                regime = MarketRegime.RANGE
            else:
                regime = MarketRegime.UPTREND
        elif previous == MarketRegime.DOWNTREND:
            if raw_uptrend and ret10 >= self.uptrend_ret10:
                regime = MarketRegime.UPTREND
            elif two_sided_chop or ret5 >= self.downtrend_exit_ret5 or (price >= ma5 and price >= ma10):
                regime = MarketRegime.RANGE
            else:
                regime = MarketRegime.DOWNTREND
        else:
            if raw_uptrend:
                regime = MarketRegime.UPTREND
            elif raw_downtrend:
                regime = MarketRegime.DOWNTREND

        if range20 <= 0.10 and abs(ret10) <= 0.08:
            tags.append("compressed_range")
            if regime != MarketRegime.DOWNTREND:
                regime = MarketRegime.RANGE

        if regime == MarketRegime.UPTREND:
            score = max(0.25, min(1.0, up_score))
            return self._make_decision(
                regime,
                tags,
                score,
                self.uptrend_floor,
                self.uptrend_ceiling,
                (
                    f"regime=UPTREND px={price:.2f} ret5={ret5*100:.2f}% ret10={ret10*100:.2f}% "
                    f"dd20={dd20*100:.2f}% ma5={ma5:.2f} ma10={ma10:.2f}"
                ),
                False,
                False,
            )
        if regime == MarketRegime.DOWNTREND:
            score = -max(0.25, min(1.0, down_score))
            return self._make_decision(
                regime,
                tags,
                score,
                self.downtrend_floor,
                self.downtrend_ceiling,
                (
                    f"regime=DOWNTREND px={price:.2f} ret5={ret5*100:.2f}% ret10={ret10*100:.2f}% "
                    f"dd20={dd20*100:.2f}% ma5={ma5:.2f} ma10={ma10:.2f}"
                ),
                False,
                False,
            )

        confidence = 0.55 + min(0.25, max(0.0, 0.12 - abs(ret10)) / 0.12 * 0.25)
        return self._make_decision(
            MarketRegime.RANGE,
            tags,
            0.0,
            self.range_floor,
            self.range_ceiling,
            (
                f"regime=RANGE px={price:.2f} ret5={ret5*100:.2f}% ret10={ret10*100:.2f}% "
                f"dd20={dd20*100:.2f}% range20={range20*100:.2f}%"
            ),
            True,
            True,
            confidence=confidence,
        )

    def _make_decision(
        self,
        regime: MarketRegime,
        tags: list[str],
        regime_score: float,
        floor_pct: float,
        ceiling_pct: float,
        detail: str,
        allow_cross_day: bool,
        allow_local_t: bool,
        confidence: float | None = None,
    ) -> MarketRegimeDecision:
        if confidence is None:
            confidence = 0.80 if regime != MarketRegime.RANGE else 0.65
        return MarketRegimeDecision(
            regime=regime,
            tags=tuple(tags),
            confidence=round(confidence, 4),
            target_floor_pct=round(floor_pct, 4),
            target_ceiling_pct=round(ceiling_pct, 4),
            regime_score=round(regime_score, 4),
            detail=detail,
            allow_cross_day=allow_cross_day,
            allow_local_t=allow_local_t,
        )
