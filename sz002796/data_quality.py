"""Data-quality checks for historical frames and realtime ticks.

Historical validation reports loader warnings without mutating data. The
realtime monitor keeps only enough state to reject clearly bad feed snapshots,
such as non-positive prices or cumulative volume/amount backsteps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from .config import PRICE_JUMP_THRESHOLD


@dataclass(frozen=True)
class DataQualityIssue:
    severity: str
    message: str


def _issue(severity: str, message: str) -> DataQualityIssue:
    return DataQualityIssue(severity=severity, message=message)


def validate_market_frame(df: pd.DataFrame, source: str = "") -> list[DataQualityIssue]:
    issues: list[DataQualityIssue] = []
    required = {"dt", "price", "cum_volume", "cum_amount"}
    missing = sorted(required - set(df.columns))
    if missing:
        return [_issue("critical", f"{source}: missing columns {missing}")]

    if df.empty:
        return [_issue("critical", f"{source}: empty data frame")]

    null_dt = int(df["dt"].isna().sum())
    if null_dt:
        issues.append(_issue("critical", f"{source}: {null_dt} rows have invalid timestamps"))

    bad_price = int((df["price"].fillna(0.0) <= 0).sum())
    if bad_price:
        issues.append(_issue("critical", f"{source}: {bad_price} rows have non-positive price"))

    duplicated_dt = int(df["dt"].duplicated().sum())
    if duplicated_dt:
        issues.append(_issue("warning", f"{source}: {duplicated_dt} duplicated timestamps"))

    sorted_df = df.sort_values("dt")
    if "cum_volume" in sorted_df:
        volume_backsteps = int((sorted_df["cum_volume"].diff().fillna(0.0) < 0).sum())
        if volume_backsteps:
            issues.append(_issue("warning", f"{source}: {volume_backsteps} cumulative volume backsteps"))

    if "cum_amount" in sorted_df:
        amount_backsteps = int((sorted_df["cum_amount"].diff().fillna(0.0) < 0).sum())
        if amount_backsteps:
            issues.append(_issue("warning", f"{source}: {amount_backsteps} cumulative amount backsteps"))

    pct_change = sorted_df["price"].pct_change().abs()
    jumps = int((pct_change > PRICE_JUMP_THRESHOLD).sum())
    if jumps:
        threshold_pct = PRICE_JUMP_THRESHOLD * 100
        issues.append(_issue("warning", f"{source}: {jumps} price jumps above {threshold_pct:.0f}% between samples"))

    return issues


class RealtimeDataQualityMonitor:
    def __init__(self, max_price_jump_pct: float = PRICE_JUMP_THRESHOLD):
        self.max_price_jump_pct = max_price_jump_pct
        self.last_dt: datetime | None = None
        self.last_price: float | None = None
        self.last_cum_volume: float | None = None
        self.last_cum_amount: float | None = None

    def check(self, tick: dict[str, Any]) -> list[DataQualityIssue]:
        issues: list[DataQualityIssue] = []
        dt = tick.get("Time")
        price = float(tick.get("price", tick.get("Close", 0.0)) or 0.0)
        cum_volume = float(tick.get("cum_volume", tick.get("Volume", 0.0)) or 0.0)
        cum_amount = float(tick.get("cum_amount", tick.get("Amount", 0.0)) or 0.0)

        if not isinstance(dt, datetime):
            issues.append(_issue("critical", "tick timestamp is missing or invalid"))
        if price <= 0:
            issues.append(_issue("critical", f"non-positive tick price: {price}"))
        if cum_volume < 0:
            issues.append(_issue("critical", f"negative cumulative volume: {cum_volume}"))
        if cum_amount < 0:
            issues.append(_issue("critical", f"negative cumulative amount: {cum_amount}"))

        if self.last_dt is not None and isinstance(dt, datetime):
            if dt <= self.last_dt:
                issues.append(_issue("warning", f"non-increasing tick time: {dt} <= {self.last_dt}"))
            elif dt.date() == self.last_dt.date():
                if self.last_cum_volume is not None and cum_volume < self.last_cum_volume:
                    issues.append(_issue("critical", f"cumulative volume backstep: {cum_volume} < {self.last_cum_volume}"))
                if self.last_cum_amount is not None and cum_amount < self.last_cum_amount:
                    issues.append(_issue("critical", f"cumulative amount backstep: {cum_amount} < {self.last_cum_amount}"))

        if self.last_price and price > 0:
            jump = abs(price / self.last_price - 1.0)
            if jump > self.max_price_jump_pct:
                issues.append(_issue("warning", f"price jump {jump*100:.2f}% from {self.last_price:.2f} to {price:.2f}"))

        if not any(issue.severity == "critical" for issue in issues):
            self.last_dt = dt if isinstance(dt, datetime) else self.last_dt
            self.last_price = price if price > 0 else self.last_price
            self.last_cum_volume = cum_volume
            self.last_cum_amount = cum_amount

        return issues


def has_critical_issue(issues: list[DataQualityIssue]) -> bool:
    return any(issue.severity == "critical" for issue in issues)
