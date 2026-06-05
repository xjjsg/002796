"""Execution-side helpers shared by backtest and realtime persistence.

The strategy decides whether to trade; this module handles the exchange-facing
constraints: lot alignment, minimum commission, stamp duty, and limit-up/down
blocking. Backtest and GUI state replay both use these functions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .config import COMMISSION_RATE, LOT_SIZE, MIN_COMMISSION, STAMP_DUTY_RATE


@dataclass(frozen=True)
class TradeCosts:
    amount: float
    commission: float
    stamp_tax: float

    @property
    def buy_cash_required(self) -> float:
        return self.amount + self.commission

    @property
    def sell_cash_received(self) -> float:
        return self.amount - self.commission - self.stamp_tax


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_trade_costs(
    side: str,
    price: float,
    shares: int,
    commission_rate: float = COMMISSION_RATE,
    stamp_duty_rate: float = STAMP_DUTY_RATE,
    min_commission: float = MIN_COMMISSION,
) -> TradeCosts:
    shares = int(shares / LOT_SIZE) * LOT_SIZE
    amount = max(0.0, float(price) * shares)
    if amount <= 0.0:
        return TradeCosts(amount=0.0, commission=0.0, stamp_tax=0.0)
    commission = max(amount * commission_rate, min_commission)
    stamp_tax = amount * stamp_duty_rate if side.upper() == "SELL" else 0.0
    return TradeCosts(amount=amount, commission=commission, stamp_tax=stamp_tax)


def max_affordable_lot_shares(cash: float, price: float, min_commission: float = MIN_COMMISSION) -> int:
    if cash <= 0 or price <= 0:
        return 0
    shares = int((cash / price) / LOT_SIZE) * LOT_SIZE
    while shares > 0:
        costs = calculate_trade_costs("BUY", price, shares, min_commission=min_commission)
        if costs.buy_cash_required <= cash + 1e-6:
            return shares
        shares -= LOT_SIZE
    return 0


def is_limit_blocked(side: str, execution_price: float, prev_close: float) -> bool:
    if execution_price <= 0 or prev_close <= 0:
        return False
    side = side.upper()
    if side == "BUY":
        return execution_price >= round_price(prev_close * 1.10) - 1e-9
    if side == "SELL":
        return execution_price <= round_price(prev_close * 0.90) + 1e-9
    return False
