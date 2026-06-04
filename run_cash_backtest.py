from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

from combined_strategy_v5 import CombinedStrategyV5
from market_data import DATA_DIR, MarketDataBundle, load_market_data, row_to_tick
from strategy_core import (
    COMMISSION_RATE,
    LOT_SIZE,
    STAMP_DUTY_RATE,
    PositionMode,
    TradeRecord,
    _clamp,
)


INITIAL_CAPITAL = 1_000_000.0
START_DATE = "2026-01-05"
MIN_COMMISSION = 5.0
OUTPUT_DIR = Path(__file__).resolve().parent / "backtest_records" / "cash_100w_2026-01-05_to_latest"

TRADE_COLUMNS = [
    "time",
    "side",
    "price",
    "shares",
    "amount",
    "commission",
    "stamp_tax",
    "cash_after",
    "position_shares",
    "asset",
    "position_pct",
    "reason",
    "detail",
    "execution_source",
    "orderbook_fallback",
]


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


@dataclass(frozen=True)
class BenchmarkPosition:
    buy_price: float
    buy_shares: int
    buy_amount: float
    buy_commission: float
    cash_after_buy: float
    final_asset: float


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


def max_drawdown(values: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            max_dd = max(max_dd, 1.0 - value / peak if value > 0 else 0.0)
    return max_dd


def benchmark_all_in(initial_cash: float, buy_price: float, final_price: float) -> BenchmarkPosition:
    shares = max_affordable_lot_shares(initial_cash, buy_price)
    costs = calculate_trade_costs("BUY", buy_price, shares)
    cash_after_buy = initial_cash - costs.buy_cash_required
    final_asset = cash_after_buy + shares * final_price
    return BenchmarkPosition(
        buy_price=buy_price,
        buy_shares=shares,
        buy_amount=costs.amount,
        buy_commission=costs.commission,
        cash_after_buy=cash_after_buy,
        final_asset=final_asset,
    )


class BacktestExecutionStrategy(CombinedStrategyV5):
    def __init__(self, initial_capital: float = INITIAL_CAPITAL, min_commission: float = MIN_COMMISSION, **kwargs: Any):
        super().__init__(initial_capital=initial_capital, **kwargs)
        self.cash = initial_capital
        self.shares = 0
        self.target_pct = 0.0
        self.mode = PositionMode.DEFENSE
        self.local_base_target_pct = 0.0
        self.min_commission = min_commission
        self.execution_records: list[dict[str, Any]] = []
        self.orderbook_fallback_count = 0
        self.limit_up_buy_skip_count = 0
        self.limit_down_sell_skip_count = 0
        self._current_tick: dict[str, Any] | None = None
        self._normal_enable_local_t = self.enable_local_t
        self._position_built = False

    def on_tick(self, tick: dict[str, Any]) -> TradeRecord | None:
        self._current_tick = tick
        if not self._position_built:
            self.enable_local_t = False
        try:
            record = super().on_tick(tick)
        finally:
            if not self._position_built:
                self.enable_local_t = self._normal_enable_local_t
        if record and record.side == "BUY" and not self._position_built:
            self._position_built = True
            self.enable_local_t = self._normal_enable_local_t
            self.local_base_target_pct = record.target_pct
        return record

    def resolve_execution_price(
        self,
        side: str,
        tick: dict[str, Any] | None = None,
        fallback_price: float | None = None,
        count_fallback: bool = False,
    ) -> tuple[float, str, bool]:
        tick = tick or self._current_tick or {}
        mark_price = float(tick.get("price", tick.get("Close", fallback_price or 0.0)) or fallback_price or 0.0)
        if bool(tick.get("_is_realtime", False)):
            field = "sp1" if side.upper() == "BUY" else "bp1"
            quote_price = float(tick.get(field, 0.0) or 0.0)
            if quote_price > 0:
                return quote_price, field, False
            if count_fallback:
                self.orderbook_fallback_count += 1
            return mark_price, "price_fallback", True
        return mark_price, "price", False

    def _mark_price(self, fallback_price: float) -> float:
        tick = self._current_tick or {}
        return float(tick.get("price", tick.get("Close", fallback_price)) or fallback_price)

    def _prev_close(self) -> float:
        tick = self._current_tick or {}
        return float(tick.get("prev_close", 0.0) or 0.0)

    def _align_to_target(
        self,
        current_price: float,
        target_pct: float,
        dt: Any,
        reason: str,
        detail: str = "",
        force_floor: bool = False,
    ) -> TradeRecord | None:
        target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        total = self.total_asset(current_price)
        target_shares = int((total * target_pct) / current_price / LOT_SIZE) * LOT_SIZE
        diff = target_shares - self.shares
        min_shares = LOT_SIZE if force_floor else self.min_trade_lots * LOT_SIZE

        if diff >= min_shares:
            buy_price, _, _ = self.resolve_execution_price("BUY", fallback_price=current_price)
            max_affordable = max_affordable_lot_shares(self.cash, buy_price, self.min_commission)
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
        dt: Any,
        target_pct: float,
        reason: str,
        detail: str,
    ) -> TradeRecord | None:
        exec_price, source, fallback = self.resolve_execution_price("BUY", fallback_price=price, count_fallback=True)
        if is_limit_blocked("BUY", exec_price, self._prev_close()):
            self.limit_up_buy_skip_count += 1
            return None

        shares = min(int(shares / LOT_SIZE) * LOT_SIZE, max_affordable_lot_shares(self.cash, exec_price, self.min_commission))
        if shares <= 0:
            return None
        costs = calculate_trade_costs("BUY", exec_price, shares, self.commission_rate, self.stamp_duty_rate, self.min_commission)
        if costs.buy_cash_required > self.cash + 1e-6:
            return None

        self.cash -= costs.buy_cash_required
        self.shares += shares
        self.target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        self.mode = self._mode_from_target(self.target_pct)
        record = TradeRecord(dt, "BUY", exec_price, shares, self.shares, self.cash, self.target_pct, self.mode.value, reason, detail)
        self.trades.append(record)
        self._append_execution_record(record, costs, source, fallback)
        if self.verbose:
            print(self._format_trade(record))
        return record

    def _sell(
        self,
        price: float,
        shares: int,
        dt: Any,
        target_pct: float,
        reason: str,
        detail: str,
    ) -> TradeRecord | None:
        exec_price, source, fallback = self.resolve_execution_price("SELL", fallback_price=price, count_fallback=True)
        if is_limit_blocked("SELL", exec_price, self._prev_close()):
            self.limit_down_sell_skip_count += 1
            return None

        shares = int(min(shares, self.shares) / LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            return None
        costs = calculate_trade_costs("SELL", exec_price, shares, self.commission_rate, self.stamp_duty_rate, self.min_commission)
        self.cash += costs.sell_cash_received
        self.shares -= shares
        self.target_pct = _clamp(target_pct, self.floor_pct, self.ceil_pct)
        self.mode = self._mode_from_target(self.target_pct)
        record = TradeRecord(dt, "SELL", exec_price, shares, self.shares, self.cash, self.target_pct, self.mode.value, reason, detail)
        self.trades.append(record)
        self._append_execution_record(record, costs, source, fallback)
        if self.verbose:
            print(self._format_trade(record))
        return record

    def _append_execution_record(
        self,
        record: TradeRecord,
        costs: TradeCosts,
        execution_source: str,
        orderbook_fallback: bool,
    ) -> None:
        mark_price = self._mark_price(record.price)
        asset = self.total_asset(mark_price)
        position_pct = self.current_position_pct(mark_price)
        self.execution_records.append(
            {
                "time": record.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "side": record.side,
                "price": round(record.price, 4),
                "shares": record.shares,
                "amount": round(costs.amount, 4),
                "commission": round(costs.commission, 4),
                "stamp_tax": round(costs.stamp_tax, 4),
                "cash_after": round(record.cash_after, 4),
                "position_shares": record.position_shares,
                "asset": round(asset, 4),
                "position_pct": round(position_pct, 6),
                "reason": record.reason,
                "detail": record.detail,
                "execution_source": execution_source,
                "orderbook_fallback": orderbook_fallback,
            }
        )


def _timestamp_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _write_outputs(
    output_dir: Path,
    trade_records: list[dict[str, Any]],
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "trades.csv"
    summary_path = output_dir / "summary.json"
    pd.DataFrame(trade_records, columns=TRADE_COLUMNS).to_csv(trades_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return trades_path, summary_path


def run_backtest(
    start_date: str = START_DATE,
    end_date: str | None = None,
    data_dir: str | Path = DATA_DIR,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict[str, Any]:
    bundle: MarketDataBundle = load_market_data(start_date=start_date, end_date=end_date, data_dir=data_dir)
    df = bundle.frame
    first_row = df.iloc[0]
    last_row = df.iloc[-1]
    first_price = float(first_row["price"])
    last_price = float(last_row["price"])

    strategy = BacktestExecutionStrategy(initial_capital=INITIAL_CAPITAL)
    benchmark = benchmark_all_in(INITIAL_CAPITAL, first_price, last_price)

    equity_curve: list[float] = []
    benchmark_curve: list[float] = []
    for _, row in df.iterrows():
        tick = row_to_tick(row)
        strategy.on_tick(tick)
        mark_price = float(row["price"])
        equity_curve.append(strategy.total_asset(mark_price))
        benchmark_curve.append(benchmark.cash_after_buy + benchmark.buy_shares * mark_price)

    strategy_final_asset = equity_curve[-1]
    benchmark_final_asset = benchmark_curve[-1]
    strategy_return = strategy_final_asset / INITIAL_CAPITAL - 1.0
    benchmark_return = benchmark_final_asset / INITIAL_CAPITAL - 1.0
    trade_amount = sum(float(record["amount"]) for record in strategy.execution_records)
    final_position_pct = strategy.current_position_pct(last_price)
    output_dir = Path(output_dir)

    summary = {
        "start_time": _timestamp_text(first_row["dt"]),
        "end_time": _timestamp_text(last_row["dt"]),
        "start_date": start_date,
        "end_date": end_date or str(last_row["date"]),
        "data_rows": int(len(df)),
        "data_files": len(bundle.files),
        "strategy_final_asset": round(strategy_final_asset, 4),
        "benchmark_final_asset": round(benchmark_final_asset, 4),
        "strategy_return": round(strategy_return, 8),
        "benchmark_return": round(benchmark_return, 8),
        "alpha": round(strategy_return - benchmark_return, 8),
        "max_drawdown": round(max_drawdown(equity_curve), 8),
        "benchmark_max_drawdown": round(max_drawdown(benchmark_curve), 8),
        "trade_count": len(strategy.execution_records),
        "turnover": round(trade_amount / INITIAL_CAPITAL, 8),
        "final_cash": round(strategy.cash, 4),
        "final_shares": int(strategy.shares),
        "final_position_pct": round(final_position_pct, 8),
        "orderbook_fallback_count": strategy.orderbook_fallback_count,
        "limit_skip_count": strategy.limit_up_buy_skip_count + strategy.limit_down_sell_skip_count,
        "limit_up_buy_skip_count": strategy.limit_up_buy_skip_count,
        "limit_down_sell_skip_count": strategy.limit_down_sell_skip_count,
        "benchmark": {
            "buy_time": _timestamp_text(first_row["dt"]),
            "buy_price": benchmark.buy_price,
            "buy_shares": benchmark.buy_shares,
            "buy_amount": round(benchmark.buy_amount, 4),
            "buy_commission": round(benchmark.buy_commission, 4),
            "cash_after_buy": round(benchmark.cash_after_buy, 4),
        },
        "known_data_quality_warnings": bundle.warnings,
        "outputs": {
            "trades_csv": str(output_dir / "trades.csv"),
            "summary_json": str(output_dir / "summary.json"),
        },
    }
    trades_path, summary_path = _write_outputs(output_dir, strategy.execution_records, summary)
    summary["outputs"] = {"trades_csv": str(trades_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run cash-start V5 backtest for sz002796.")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    summary = run_backtest(
        start_date=args.start_date,
        end_date=args.end_date,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
    )
    print(f"rows={summary['data_rows']} trades={summary['trade_count']}")
    print(f"strategy_final_asset={summary['strategy_final_asset']:.2f}")
    print(f"benchmark_final_asset={summary['benchmark_final_asset']:.2f}")
    print(f"alpha={summary['alpha']:.4%}")
    print(f"trades={summary['outputs']['trades_csv']}")
    print(f"summary={summary['outputs']['summary_json']}")


if __name__ == "__main__":
    main()
