import pandas as pd

from combined_strategy_v5 import COMMISSION_RATE, CombinedStrategyV5
from run_v5_backtest import load_market_data, max_drawdown, tick_from_row
from strategy_core import PositionMode, TradeRecord


STRATEGY_ENABLE_DATE = "2026-04-18"
LOCAL_T0_ENTER_SCORE = 0.70

BUY_1_DATE = "2026-04-14 09:30:00"
BUY_1_PRICE = 49.95
BUY_1_SHARES = 1700

BUY_2_DATE = "2026-04-17 09:30:00"
BUY_2_PRICE = 50.60
BUY_2_SHARES = 2000

INITIAL_SHARES = BUY_1_SHARES + BUY_2_SHARES
INITIAL_COST = (
    BUY_1_SHARES * BUY_1_PRICE * (1.0 + COMMISSION_RATE)
    + BUY_2_SHARES * BUY_2_PRICE * (1.0 + COMMISSION_RATE)
)


def seed_real_position(strategy: CombinedStrategyV5) -> None:
    strategy.cash = 0.0
    strategy.shares = INITIAL_SHARES
    strategy.target_pct = 1.0
    strategy.mode = PositionMode.ATTACK
    strategy.trades.append(
        TradeRecord(
            timestamp=pd.Timestamp(BUY_1_DATE).to_pydatetime(),
            side="BUY",
            price=BUY_1_PRICE,
            shares=BUY_1_SHARES,
            position_shares=BUY_1_SHARES,
            cash_after=BUY_2_SHARES * BUY_2_PRICE * (1.0 + COMMISSION_RATE),
            target_pct=1.0,
            mode=PositionMode.ATTACK.value,
            reason="real entry 1",
            detail="strategy disabled",
        )
    )
    strategy.trades.append(
        TradeRecord(
            timestamp=pd.Timestamp(BUY_2_DATE).to_pydatetime(),
            side="BUY",
            price=BUY_2_PRICE,
            shares=BUY_2_SHARES,
            position_shares=INITIAL_SHARES,
            cash_after=0.0,
            target_pct=1.0,
            mode=PositionMode.ATTACK.value,
            reason="real entry 2",
            detail="strategy disabled",
        )
    )


def run_backtest(show_trades: bool = True) -> None:
    data = load_market_data(STRATEGY_ENABLE_DATE)
    strategy = CombinedStrategyV5(
        initial_capital=INITIAL_COST,
        local_enter_score=LOCAL_T0_ENTER_SCORE,
        verbose=False,
    )
    seed_real_position(strategy)

    equity_curve: list[float] = []
    benchmark_curve: list[float] = []

    for row in data.itertuples(index=False):
        price = float(row.price)
        strategy.on_tick(tick_from_row(row))
        equity_curve.append(strategy.total_asset(price))
        benchmark_curve.append(INITIAL_SHARES * price)

    final_price = float(data.iloc[-1]["price"])
    final_asset = strategy.total_asset(final_price)
    strategy_return = final_asset / INITIAL_COST - 1.0
    strategy_mdd = max_drawdown(equity_curve)

    benchmark_asset = INITIAL_SHARES * final_price
    benchmark_return = benchmark_asset / INITIAL_COST - 1.0
    benchmark_mdd = max_drawdown(benchmark_curve)

    strategy_trade_count = max(0, len(strategy.trades) - 2)
    turnover = sum(t.price * t.shares for t in strategy.trades[2:]) / INITIAL_COST

    print("\n================ V5 real-entry handoff backtest ================")
    print(f"Real entry 1: {BUY_1_DATE} | {BUY_1_PRICE:.2f} | {BUY_1_SHARES} shares")
    print(f"Real entry 2: {BUY_2_DATE} | {BUY_2_PRICE:.2f} | {BUY_2_SHARES} shares")
    print(f"Initial shares: {INITIAL_SHARES} | initial cost: {INITIAL_COST:,.2f}")
    print(f"Strategy enabled from: {STRATEGY_ENABLE_DATE} | first row: {data.iloc[0]['dt']}")
    print(f"End: {data.iloc[-1]['dt']} | final price: {final_price:.2f}")
    print("-" * 62)
    print(f"V5 final asset: {final_asset:,.2f}")
    print(f"V5 return: {strategy_return * 100:.2f}%")
    print(f"V5 max drawdown: {strategy_mdd * 100:.2f}%")
    print(f"V5 final shares: {strategy.shares}")
    print(f"V5 final cash: {strategy.cash:,.2f}")
    print(f"V5 strategy trade count: {strategy_trade_count}")
    print(f"V5 turnover: {turnover:.2f}")
    print("-" * 62)
    print(f"Hold benchmark asset: {benchmark_asset:,.2f}")
    print(f"Hold benchmark return: {benchmark_return * 100:.2f}%")
    print(f"Hold benchmark max drawdown: {benchmark_mdd * 100:.2f}%")
    print(f"V5 relative alpha: {(strategy_return - benchmark_return) * 100:.2f}%")
    print("=" * 62)

    if show_trades:
        print("\nTrades after handoff:")
        for trade in strategy.trades[2:]:
            print(strategy._format_trade(trade))


if __name__ == "__main__":
    run_backtest(show_trades=True)
