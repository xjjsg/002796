# 002796.SZ V6 Strategy Monitor

This project runs the V6 strategy for `002796.SZ` with two paths:

- realtime GUI monitoring and state persistence
- historical backtesting from local CSV market data only

## File Structure

### Entry Scripts

| File | Description |
|------|-------------|
| `run_gui.py` | **Main entry** — Starts the realtime GUI monitoring system. |
| `run_backtest.py` | Historical backtest runner: ¥1,000,000 cash, 70% initial position, outputs trades + summary. |

### `sz002796` Core Package

The project has been modularized into the `sz002796` package for better maintainability:

| File | Description |
|------|-------------|
| `sz002796/config.py` | Global constants, trading calendar tools, and utility functions (`parse_dt`, `clamp`). |
| `sz002796/factors.py` | Intraday VWAP factors calculation with real-time rolling windows (`IntradayFactorCalc`). |
| `sz002796/position.py` | Base strategy logic, trade records, and position tracking (`BaseStrategy`, `TradeRecord`). |
| `sz002796/regime.py` | Coarse market-state classifier (`UPTREND` / `DOWNTREND` / `RANGE`). |
| `sz002796/strategy_v6.py` | Current V6 strategy: cross-day adjustment, local T logic, regime guardrails, main-flow guard. |
| `sz002796/market_data.py` | Historical CSV loader for both 10-column minute and 30-column realtime orderbook formats. |
| `sz002796/data_quality.py` | Realtime tick and historical CSV quality checks. |
| `sz002796/execution.py` | Trade cost model (commission, stamp duty) and limit-up/limit-down blocking. |
| `sz002796/state_store.py` | Realtime trade state serialization, persistence, and reconciliation logic. |
| `sz002796/tick_writer.py` | Writes realtime ticks into daily CSV files. |
| `sz002796/fetcher.py` | Realtime asynchronous data fetching from Tencent API. |
| `sz002796/gui.py` | Graphical User Interface elements built with `customtkinter`. |
| `sz002796/backtest.py` | Backtest runner logic. |

### Configuration

| File | Description |
|------|-------------|
| `requirements.txt` | Python dependencies with version constraints. |
| `世嘉科技策略监控.spec` | PyInstaller spec for packaging the GUI into a standalone `.exe`. |
| `.gitignore` | Git ignore rules (caches, state files, build outputs, editor noise). |

### Tests

| File | Description |
|------|-------------|
| `tests/test_smoke.py` | 20 regression tests covering factors, state, trades, execution, regime, and backtest output. |

## Realtime Files

The GUI position source is the V6 simulated account only:

```text
backtest_records/v6_seed70_100w_2026-01-05_to_latest/trades.csv
```

That account starts on `2026-01-05` with `1,000,000` cash and `0` shares, buys the initial 70% base position in the backtest, then replays every V6 trade through the latest local CSV data. There is no manual-position configuration.

The V6 runtime writes GUI state and incremental runtime trades to V6-specific files:

```text
data/sz002796/sz002796_v6_strategy_state.json
data/sz002796/sz002796_v6_strategy_trades.csv
```

State files preserve strategy context such as regime and cooldown fields. Cash and shares are recalculated from the simulated-account trade replay each time.

Start the GUI:

```powershell
python run_gui.py
```

## Backtest

The historical backtest only reads local CSV market data from `data/sz002796/sz002796-*.csv`.
It does not read GUI state or GUI runtime trade logs.

Run the default V6 backtest:

```powershell
python run_backtest.py
```

Default output:

```text
backtest_records/v6_seed70_100w_2026-01-05_to_latest/
```

The output includes:

- `trades.csv`: every strategy trade with execution price, shares, amount, costs, cash, position, reason, and detail.
- `summary.json`: final asset, benchmark comparison, alpha, drawdown, trade count, turnover, orderbook fallback counts, limit-skip counts, regime counts, and data-quality warnings.

## Data Format

Supported historical CSV formats:

- 10-column minute data:
  `server_time, price, open, high, low, prev_close, cum_volume, cum_amount, tick_vol, tick_amt`
- 30-column realtime/orderbook data:
  `local_time_ms, server_time, price, open, high, low, prev_close, cum_volume, cum_amount, bp1..bp5/bv1..bv5/sp1..sp5/sv1..sv5, signal`

The loader sorts by `dt + local_time_ms`, fills missing orderbook fields with `0`, and always recomputes `tick_vol/tick_amt` from cumulative volume and amount.

## Tests

```powershell
python -m unittest discover -s tests
```

The tests cover:

- factor windows for minute and 3-second data
- V6 realtime state save/restore
- stale realtime tick skipping
- loader delta recomputation and orderbook field filling
- execution price rules, minimum commission, and limit blocking
- benchmark construction
- V6 regime and main-flow guard behavior
- V6 backtest output generation
