"""Run V6 tick backtests from miniQMT xtdata history."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sz002796.backtest import TRADE_COLUMNS, V6BacktestExecutionStrategy, max_drawdown
from sz002796.config import INITIAL_CAPITAL, INITIAL_STRATEGY_TARGET_PCT, LOT_SIZE
from sz002796.execution import max_affordable_lot_shares

if __package__ in (None, ""):
    from qmt.adapter import TICK_FIELDS, iter_qmt_strategy_ticks
    from qmt.config import END_TIME, START_TIME, TARGET_SYMBOL, default_output_dir
    from qmt.xtquant_env import import_xtdata
else:
    from .adapter import TICK_FIELDS, iter_qmt_strategy_ticks
    from .config import END_TIME, START_TIME, TARGET_SYMBOL, default_output_dir
    from .xtquant_env import import_xtdata


def seed_initial_position_from_tick(
    strategy: V6BacktestExecutionStrategy,
    tick: dict[str, Any],
    target_pct: float = INITIAL_STRATEGY_TARGET_PCT,
):
    price = float(tick.get("price", 0.0) or 0.0)
    if price <= 0:
        return None
    strategy._current_tick = tick
    target_shares = int((strategy.initial_capital * target_pct) / price / LOT_SIZE) * LOT_SIZE
    max_shares = max_affordable_lot_shares(strategy.cash, price, strategy.min_commission)
    record = strategy._buy(
        price,
        min(target_shares, max_shares),
        tick["Time"],
        target_pct,
        "initial 70% base position",
        "seed=70pct qmt_tick",
    )
    if record:
        strategy._position_built = True
        strategy.enable_local_t = strategy._normal_enable_local_t
        strategy.local_base_target_pct = record.target_pct
    return record


def _write_outputs(output_dir: Path, trade_records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(trade_records, columns=TRADE_COLUMNS).to_csv(
        output_dir / "trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _log(message: str, verbose: bool = True) -> None:
    if verbose:
        print(message, flush=True)


def _row_count(data: Any) -> int | str:
    try:
        return len(data)
    except Exception:
        return "unknown"


def run_qmt_tick_backtest(
    symbol: str = TARGET_SYMBOL,
    start_time: str = START_TIME,
    end_time: str = END_TIME,
    output_dir: str | Path | None = None,
    download: bool = False,
    verbose: bool = True,
    progress_interval: int = 50_000,
) -> dict[str, Any]:
    _log(f"[qmt tick] loading xtdata symbol={symbol} start={start_time or 'latest'} end={end_time or 'latest'}", verbose)
    xtdata = import_xtdata()
    if download:
        _log("[qmt tick] downloading history data from miniQMT; this can be slow and has no QMT-side progress bar", verbose)
        xtdata.download_history_data2(
            [symbol],
            period="tick",
            start_time=start_time,
            end_time=end_time,
        )
        _log("[qmt tick] download request finished", verbose)
    else:
        _log("[qmt tick] skip download; reading already cached miniQMT tick data", verbose)
    _log("[qmt tick] requesting tick frame from xtdata.get_market_data_ex ...", verbose)
    data_dict = xtdata.get_market_data_ex(
        TICK_FIELDS,
        [symbol],
        period="tick",
        start_time=start_time,
        end_time=end_time,
    )
    if not data_dict or symbol not in data_dict:
        raise RuntimeError(f"no QMT tick data returned for {symbol}")
    _log(f"[qmt tick] tick frame received rows={_row_count(data_dict[symbol])}", verbose)

    strategy = V6BacktestExecutionStrategy(initial_capital=INITIAL_CAPITAL)
    equity_curve: list[float] = []
    regime_counts: dict[str, int] = {}
    first_tick: dict[str, Any] | None = None
    last_tick: dict[str, Any] | None = None
    row_count = 0
    seed_record = None

    for tick in iter_qmt_strategy_ticks(data_dict[symbol]):
        row_count += 1
        if progress_interval > 0 and row_count % progress_interval == 0:
            _log(f"[qmt tick] processed rows={row_count} trades={len(strategy.execution_records)}", verbose)
        if first_tick is None:
            first_tick = tick
            seed_record = seed_initial_position_from_tick(strategy, tick)
        strategy.on_tick(tick)
        if strategy.regime_decision is not None:
            regime = strategy.regime_decision.regime.value
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
        mark_price = float(tick["price"])
        equity_curve.append(strategy.total_asset(mark_price))
        last_tick = tick

    if first_tick is None or last_tick is None or not equity_curve:
        raise RuntimeError(f"QMT tick data for {symbol} had no usable rows")

    final_price = float(last_tick["price"])
    final_asset = equity_curve[-1]
    strategy_return = final_asset / INITIAL_CAPITAL - 1.0
    trade_amount = sum(float(record["amount"]) for record in strategy.execution_records)
    output_path = Path(output_dir) if output_dir else default_output_dir(start_time, end_time)
    summary = {
        "symbol": symbol,
        "period": "tick",
        "start_time": first_tick["Time"].strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": last_tick["Time"].strftime("%Y-%m-%d %H:%M:%S"),
        "requested_start_time": start_time,
        "requested_end_time": end_time,
        "data_rows": row_count,
        "initial_capital": INITIAL_CAPITAL,
        "initial_strategy_target_pct": INITIAL_STRATEGY_TARGET_PCT,
        "initial_seed_trade": seed_record is not None,
        "strategy_variant": "CombinedStrategyV6",
        "strategy_final_asset": round(final_asset, 4),
        "strategy_return": round(strategy_return, 8),
        "max_drawdown": round(max_drawdown(equity_curve), 8),
        "trade_count": len(strategy.execution_records),
        "turnover": round(trade_amount / INITIAL_CAPITAL, 8),
        "final_cash": round(strategy.cash, 4),
        "final_shares": int(strategy.shares),
        "final_price": round(final_price, 4),
        "final_position_pct": round(strategy.current_position_pct(final_price), 8),
        "regime_counts": regime_counts,
        "orderbook_fallback_count": strategy.orderbook_fallback_count,
        "limit_skip_count": strategy.limit_up_buy_skip_count + strategy.limit_down_sell_skip_count,
        "outputs": {
            "trades_csv": str(output_path / "trades.csv"),
            "summary_json": str(output_path / "summary.json"),
        },
    }
    _log(f"[qmt tick] writing outputs to {output_path}", verbose)
    _write_outputs(output_path, strategy.execution_records, summary)
    _log("[qmt tick] finished", verbose)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V6 tick backtest with miniQMT xtdata.")
    parser.add_argument("--symbol", default=TARGET_SYMBOL)
    parser.add_argument("--start-time", default=START_TIME)
    parser.add_argument("--end-time", default=END_TIME)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--download", action="store_true", help="download QMT tick history before reading cached data")
    parser.add_argument("--no-download", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--progress-interval", type=int, default=50_000)
    args = parser.parse_args()
    download = bool(args.download and not args.no_download)

    summary = run_qmt_tick_backtest(
        symbol=args.symbol,
        start_time=args.start_time,
        end_time=args.end_time,
        output_dir=args.output_dir or None,
        download=download,
        verbose=not args.quiet,
        progress_interval=args.progress_interval,
    )
    print(f"rows={summary['data_rows']} trades={summary['trade_count']}")
    print(f"strategy_final_asset={summary['strategy_final_asset']:.2f}")
    print(f"strategy_return={summary['strategy_return']:.4%}")
    print(f"trades={summary['outputs']['trades_csv']}")
    print(f"summary={summary['outputs']['summary_json']}")


if __name__ == "__main__":
    main()
