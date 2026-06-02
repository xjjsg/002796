import argparse
import json
import os
from datetime import datetime

import pandas as pd

from combined_strategy_v5 import CombinedStrategyV5
from run_v5_backtest import load_market_data, max_drawdown, tick_from_row
from strategy_core import PositionMode, TradeRecord


DATA_DIR = os.path.join("data", "sz002796")
CONFIG_FILE = os.path.join(DATA_DIR, "live_config.json")
LOCAL_T0_ENTER_SCORE = 0.70


def load_live_config(path: str = CONFIG_FILE) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for key in ("shares", "cash", "cost_price"):
        if key not in config:
            raise ValueError(f"Missing live config field: {key}")
    return config


def seed_live_position(strategy: CombinedStrategyV5, config: dict, timestamp: datetime) -> None:
    shares = int(config["shares"])
    cash = float(config["cash"])
    cost_price = float(config["cost_price"])
    initial_cost = shares * cost_price + cash
    target_pct = shares * cost_price / initial_cost if initial_cost > 0 else 0.0

    strategy.cash = cash
    strategy.shares = shares
    strategy.target_pct = target_pct
    strategy.mode = strategy._mode_from_target(target_pct)
    strategy.trades.append(
        TradeRecord(
            timestamp=timestamp,
            side="SEED",
            price=cost_price,
            shares=shares,
            position_shares=shares,
            cash_after=cash,
            target_pct=target_pct,
            mode=strategy.mode.value,
            reason="live config seed",
            detail=f"cash={cash:.2f}",
        )
    )


def run_live_review(start_date: str, show_trades: bool = False) -> None:
    config = load_live_config()
    initial_shares = int(config["shares"])
    initial_cash = float(config["cash"])
    cost_price = float(config["cost_price"])
    initial_cost = initial_shares * cost_price + initial_cash

    data = load_market_data(start_date)
    strategy = CombinedStrategyV5(
        initial_capital=initial_cost,
        local_enter_score=LOCAL_T0_ENTER_SCORE,
        verbose=False,
    )
    seed_live_position(strategy, config, pd.Timestamp(data.iloc[0]["dt"]).to_pydatetime())

    equity_curve: list[float] = []
    benchmark_curve: list[float] = []

    for row in data.itertuples(index=False):
        price = float(row.price)
        strategy.on_tick(tick_from_row(row))
        equity_curve.append(strategy.total_asset(price))
        benchmark_curve.append(initial_cash + initial_shares * price)

    final_price = float(data.iloc[-1]["price"])
    final_asset = strategy.total_asset(final_price)
    benchmark_asset = initial_cash + initial_shares * final_price
    strategy_return = final_asset / initial_cost - 1.0
    benchmark_return = benchmark_asset / initial_cost - 1.0
    turnover = sum(t.price * t.shares for t in strategy.trades[1:]) / initial_cost

    print("\n================ V5 live-position review ================")
    print(f"Config: {CONFIG_FILE}")
    print(f"Start: {data.iloc[0]['dt']} | End: {data.iloc[-1]['dt']} | rows: {len(data)}")
    print(f"Seed: {initial_shares} shares @ cost {cost_price:.3f} | cash {initial_cash:,.2f}")
    print(f"Initial cost basis: {initial_cost:,.2f} | final price: {final_price:.2f}")
    print("-" * 62)
    print(f"V5 final asset: {final_asset:,.2f}")
    print(f"V5 return: {strategy_return * 100:.2f}%")
    print(f"V5 max drawdown: {max_drawdown(equity_curve) * 100:.2f}%")
    print(f"V5 final shares: {strategy.shares}")
    print(f"V5 final cash: {strategy.cash:,.2f}")
    print(f"V5 strategy trade count: {max(0, len(strategy.trades) - 1)}")
    print(f"V5 turnover: {turnover:.2f}")
    print("-" * 62)
    print(f"Hold benchmark asset: {benchmark_asset:,.2f}")
    print(f"Hold benchmark return: {benchmark_return * 100:.2f}%")
    print(f"Hold benchmark max drawdown: {max_drawdown(benchmark_curve) * 100:.2f}%")
    print(f"V5 relative alpha: {(strategy_return - benchmark_return) * 100:.2f}%")
    print("=" * 62)

    if show_trades:
        print("\nTrades after seed:")
        for trade in strategy.trades[1:]:
            print(strategy._format_trade(trade))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review V5 against current live-position config.")
    parser.add_argument("--start-date", default="2026-06-01", help="first trade date to replay, YYYY-MM-DD")
    parser.add_argument("--show-trades", action="store_true", help="print all strategy trades")
    args = parser.parse_args()
    run_live_review(args.start_date, show_trades=args.show_trades)
