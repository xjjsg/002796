import glob
import os
import warnings
from typing import Any

import pandas as pd

from combined_strategy_v5 import ANCHOR_PCT, COMMISSION_RATE, CombinedStrategyV5
from data_quality import validate_market_frame


DATA_DIR = os.path.join("data", "sz002796")
INITIAL_CAPITAL = 500000.0
INITIAL_BUY_PRICE = 44.44
START_DATE = "2026-03-27"
LOCAL_T0_ENTER_SCORE = 0.70


def load_market_data(start_date: str = START_DATE) -> pd.DataFrame:
    frames = []
    pattern = os.path.join(DATA_DIR, "sz002796-*.csv")
    orderbook_cols = [f"{side}{level}" for side in ("bp", "bv", "sp", "sv") for level in range(1, 6)]

    for path in sorted(glob.glob(pattern)):
        trade_date = os.path.basename(path).replace("sz002796-", "").replace(".csv", "")
        if trade_date < start_date:
            continue

        df = pd.read_csv(path)
        if "server_time" not in df.columns or "price" not in df.columns:
            continue

        df["dt"] = pd.to_datetime(trade_date + " " + df["server_time"].astype(str), errors="coerce")
        sort_cols = ["dt"] + (["local_time_ms"] if "local_time_ms" in df.columns else [])
        df = df.sort_values(sort_cols)

        for col in ["open", "high", "low", "prev_close", "cum_volume", "cum_amount", *orderbook_cols]:
            if col not in df.columns:
                df[col] = 0.0

        if "tick_vol" not in df.columns:
            df["tick_vol"] = df["cum_volume"].diff().fillna(df["cum_volume"]).clip(lower=0.0)
        if "tick_amt" not in df.columns:
            df["tick_amt"] = df["cum_amount"].diff().fillna(df["cum_amount"]).clip(lower=0.0)

        df["date"] = trade_date
        day_frame = df[
                [
                    "dt",
                    "date",
                    "price",
                    "open",
                    "high",
                    "low",
                    "prev_close",
                    "cum_volume",
                    "cum_amount",
                    "tick_vol",
                    "tick_amt",
                    *orderbook_cols,
                ]
            ].dropna(subset=["dt", "price"])

        issues = validate_market_frame(day_frame, source=path)
        for issue in issues:
            if issue.severity == "critical":
                raise RuntimeError(issue.message)
            warnings.warn(issue.message, RuntimeWarning, stacklevel=2)

        frames.append(day_frame)

    if not frames:
        raise RuntimeError(f"No CSV data found under {DATA_DIR} from {start_date}")

    return pd.concat(frames, ignore_index=True).sort_values("dt").reset_index(drop=True)


def tick_from_row(row: Any) -> dict[str, Any]:
    tick = {
        "Time": row.dt,
        "Close": float(row.price),
        "Volume": float(row.cum_volume),
        "Amount": float(row.cum_amount),
        "tick_vol": float(row.tick_vol),
        "tick_amt": float(row.tick_amt),
        "prev_close": float(row.prev_close),
        "open": float(row.open),
        "high": float(row.high),
        "low": float(row.low),
    }
    for side in ("bp", "bv", "sp", "sv"):
        for level in range(1, 6):
            tick[f"{side}{level}"] = float(getattr(row, f"{side}{level}", 0.0) or 0.0)
    return tick


def benchmark_70pct(final_price: float) -> tuple[int, float, float]:
    shares = int((INITIAL_CAPITAL * ANCHOR_PCT) / INITIAL_BUY_PRICE / 100) * 100
    cash = INITIAL_CAPITAL - shares * INITIAL_BUY_PRICE * (1.0 + COMMISSION_RATE)
    return shares, cash, cash + shares * final_price


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def run_backtest(show_trades: bool = True) -> None:
    data = load_market_data()
    strategy = CombinedStrategyV5(
        initial_capital=INITIAL_CAPITAL,
        local_enter_score=LOCAL_T0_ENTER_SCORE,
        verbose=False,
    )
    strategy.initialize_position(
        price=INITIAL_BUY_PRICE,
        timestamp=pd.Timestamp(f"{START_DATE} 09:30:00"),
        target_pct=ANCHOR_PCT,
        reason="V5 initial 70pct position",
    )

    equity_curve: list[float] = []
    position_curve: list[float] = []

    for row in data.itertuples(index=False):
        strategy.on_tick(tick_from_row(row))
        price = float(row.price)
        equity_curve.append(strategy.total_asset(price))
        position_curve.append(strategy.current_position_pct(price))

    final_price = float(data.iloc[-1]["price"])
    final_asset = strategy.total_asset(final_price)
    strategy_return = final_asset / INITIAL_CAPITAL - 1.0
    strategy_mdd = max_drawdown(equity_curve)

    bench_shares, bench_cash, bench_asset = benchmark_70pct(final_price)
    bench_return = bench_asset / INITIAL_CAPITAL - 1.0
    turnover = sum(t.price * t.shares for t in strategy.trades[1:]) / INITIAL_CAPITAL

    print("\n================ V5 backtest ================")
    print(f"Data: {data.iloc[0]['dt']} -> {data.iloc[-1]['dt']} | rows: {len(data)}")
    print(f"Initial capital: {INITIAL_CAPITAL:,.2f}")
    print(f"Initial buy: {INITIAL_BUY_PRICE:.2f} x {strategy.trades[0].shares} shares")
    print(f"Final price: {final_price:.2f}")
    print("-" * 58)
    print(f"V5 final asset: {final_asset:,.2f}")
    print(f"V5 return: {strategy_return * 100:.2f}%")
    print(f"V5 max drawdown: {strategy_mdd * 100:.2f}%")
    print(f"V5 final shares: {strategy.shares}")
    print(f"V5 final cash: {strategy.cash:,.2f}")
    print(f"V5 trade count: {max(0, len(strategy.trades) - 1)}")
    print(f"V5 turnover: {turnover:.2f}")
    print("-" * 58)
    print(f"70pct benchmark shares: {bench_shares}")
    print(f"70pct benchmark cash: {bench_cash:,.2f}")
    print(f"70pct benchmark asset: {bench_asset:,.2f}")
    print(f"70pct benchmark return: {bench_return * 100:.2f}%")
    print(f"V5 relative alpha: {(strategy_return - bench_return) * 100:.2f}%")
    print("=" * 58)

    if show_trades:
        print("\nTrades:")
        for trade in strategy.trades:
            print(strategy._format_trade(trade))


if __name__ == "__main__":
    run_backtest(show_trades=True)
