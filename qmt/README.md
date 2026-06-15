# QMT V6 Tick Backtest

This folder adapts the main `sz002796` V6 strategy to miniQMT. It does not
modify the strategy algorithm. Project-side tick backtests, miniQMT built-in
model backtests, realtime data sync, and guarded live-order execution live here.

## 1. Check miniQMT connectivity

Start and log in to miniQMT first, then run:

```powershell
python -m qmt.check_login
```

This checks the bundled `xtquant` path, the market-data service, and the
default simulated trading account `99005544`. To check another account, pass
an account id:

```powershell
python -m qmt.check_login --account-id 99005544
```

The account check only queries connection/account assets. It does not send
orders.

By default, project-side QMT account checks, realtime sync, and simulated
`order_stock` calls use account `99005544`.

The name `testS` is kept only for miniQMT built-in model-backtest compatibility.
If `testS` is configured in your miniQMT as an external simulated trading
account and you want to verify it through `xttrader`, force the account query:

```powershell
python -m qmt.check_login --account-id testS --force-account-query
```

miniQMT's `xtquant` package contains native `.pyd` files tied to specific
Python versions. If `qmt.check_login` reports `matching_native_module=false`,
run the command with a Python version matching one of the listed native module
tags, for example Python 3.11 for `cp311`.

## 2. Check realtime orderbook at 13:00

To verify whether live tick quotes include bid/ask orderbook data, keep miniQMT
open and run this before the afternoon session:

```powershell
python -m qmt.check_realtime_orderbook
```

The default start time is `13:00:00` and the default sampling duration is 60
seconds. `--start-at 1.30` is treated as `13:30` if you want to override it.

For immediate testing:

```powershell
python -m qmt.check_realtime_orderbook --start-at 00:00 --duration-seconds 20
```

Reports are written under:

```text
qmt/analysis/realtime_orderbook/
```

## 3. Overwrite local market data with QMT ticks

`qmt.update_local_data` builds a staging data set, backs up the current
`data/sz002796` directory, and then replaces only market files named
`sz002796-YYYY-MM-DD.csv`.

```powershell
python -m qmt.update_local_data --apply --end-time 20260608150000
```

Default behavior:

- QMT historical tick data replaces local minute CSV days where QMT history is
  available.
- Local realtime five-level orderbook files are preserved, because miniQMT
  historical tick data has no bid/ask depth.
- Local days outside QMT history are preserved.
- A full backup is created at `data/sz002796_backup_<timestamp>/`.

Use `--download` to ask miniQMT to download tick history before reading it.
Use `--replace-orderbook-days` only if you intentionally want to replace local
five-level orderbook days with QMT historical tick rows that have no orderbook.

Reports and staging files are written under:

```text
qmt/analysis/update_local_data_<timestamp>/
```

## 4. Run project-side tick backtest

```powershell
python -m qmt.tick_backtest --start-time 20260105 --end-time 20260608
```

The script reads already cached miniQMT tick data by default. Add `--download`
only when you need miniQMT to refresh historical tick data first:

```powershell
python -m qmt.tick_backtest --start-time 20260105 --end-time 20260608 --download
```

Default output:

```text
qmt/backtest_records/tick_v6_<start>_to_<end>/
```

The output includes `trades.csv` and `summary.json`. The backtest uses QMT tick
history, normalizes it into the V6 strategy tick shape, seeds the current 70%
base position, and performs local simulated execution. It does not call
`xttrader.order_stock`.

## 5. miniQMT model backtest note

The active QMT route now uses external miniQMT account `99005544` for account
checks, realtime sync, and simulated `order_stock` testing.

The older copy-into-miniQMT built-in script route is not the active path right
now. For project-side research and full-module reuse, run
`python -m qmt.tick_backtest` with a Python version that matches miniQMT's
`xtquant` native module.

## 6. Realtime data sync and guarded live orders

The live path is split into:

- `qmt/live_data.py`: subscribe to realtime tick data and normalize it into the
  same V6 tick shape used by backtests.
- `qmt/trade_gateway.py`: connect to a real miniQMT stock account, query cash
  and positions, and send guarded `xttrader.order_stock` orders.
- `qmt/run_live.py`: wire realtime ticks, account sync, V6 signals, and order
  placement together.

Recommended sequence:

```powershell
# 1) Daily realtime orderbook CSV sync test. No account connection, no order path.
python -m qmt.run_live --data-only --duration-seconds 300

# 2) Real simulated-account order_stock mode through account 99005544.
python -m qmt.run_live
```

The runner subscribes immediately after startup. It does not repeatedly start
and stop the feed based on the local Windows/Python clock, because miniQMT quote
timestamps are the authoritative market clock. During non-trading time it may
simply wait without receiving ticks. After it receives a QMT tick later than
or equal to 15:00:00, it stops for the day.

The runner uses account `99005544` for real simulated-account `order_stock`.
Soft single-order value/share limits are disabled by default because this is a
simulated account. Hard checks still apply: account id, board lots, valid
bid/ask, available cash, available sellable shares, and limit-up/down protection.

```powershell
python -m qmt.run_live `
  --account-id 99005544 `
  --max-order-value 0 `
  --max-shares-per-order 0 `
  --min-order-interval-seconds 0
```

Live startup syncs the strategy from the real account's cash and current
position. If account `99005544` has no `002796.SZ` position, the first valid
ask-one tick buys a 70% initial base position using `FIX_PRICE` at `sp1`.

Normalized realtime ticks are appended to the root data directory in the same
CSV format as existing orderbook files:

```text
data/sz002796/sz002796-YYYY-MM-DD.csv
```

If the daily CSV is locked by Excel/WPS or another process, the runner keeps
running and temporarily spools ticks under the current live session's
`market_csv_spool/` directory. It retries the root CSV on later ticks and
flushes pending rows back to `data/sz002796` once the file becomes writable.

Events, account syncs, strategy signals, and order results are still written as
JSON lines under:

```text
qmt/live_records/live_v6_<timestamp>/events.jsonl
```

## Configuration

Environment variables:

- `QMT_INSTALL_DIR`: default `D:\国金QMT交易端模拟`
- `MINI_QMT_PATH`: default `D:\国金QMT交易端模拟\userdata_mini`
- `XTQUANT_SITE_PACKAGES`: default `D:\国金QMT交易端模拟\bin.x64\Lib\site-packages`
- `QMT_TARGET_SYMBOL`: default `002796.SZ`
- `QMT_SIM_ACCOUNT`: default `99005544`
- `QMT_ACCOUNT_ID`: optional override for `qmt.check_login`; `qmt.run_live` uses `QMT_SIM_ACCOUNT`
- `QMT_BACKTEST_ACCOUNT`: default `testS`
- `QMT_BACKTEST_START_TIME`: default `20260105`
- `QMT_BACKTEST_END_TIME`: default empty, meaning latest available
