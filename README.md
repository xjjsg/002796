# 002796.SZ V5 策略监控

这是针对 `002796.SZ 世嘉科技` 的 V5 策略项目。当前代码分为实时监控与历史回测两条路径：实时路径读取实盘配置和状态，历史回测路径只读取历史行情 CSV。

## 文件结构

- `strategy_core.py`：因子计算、仓位模型、基础交易执行逻辑。
- `combined_strategy_v5.py`：V5 策略，包含跨日仓位调整和局部 T 逻辑。
- `gui_realtime_002796.py`：实时行情拉取、策略运行、GUI 展示、状态持久化。
- `market_data.py`：统一历史行情加载器，兼容 10 列分钟线和 30 列高频盘口线。
- `run_cash_backtest.py`：100 万现金起步的独立历史回测入口。
- `data_quality.py`：历史 CSV 和实时 tick 数据质量检查。
- `tests/test_smoke.py`：核心回归测试。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 实时监控

实时 GUI 使用本地实盘配置和状态文件：

```text
data/sz002796/live_config.json
data/sz002796/sz002796_strategy_state.json
data/sz002796/sz002796_strategy_trades.csv
```

如果没有实盘配置，可以从示例复制：

```powershell
Copy-Item data\sz002796\live_config.example.json data\sz002796\live_config.json
```

启动实时 GUI：

```powershell
python gui_realtime_002796.py
```

## 现金起步回测

新回测入口不会读取实盘配置、状态或实盘成交流水，只使用 `data/sz002796/sz002796-*.csv` 历史行情。

默认区间从 `2026-01-05` 到当前最新 CSV：

```powershell
python run_cash_backtest.py
```

输出目录：

```text
backtest_records/cash_100w_2026-01-05_to_latest/
```

输出文件：

- `trades.csv`：每笔策略交易，包含成交价、股数、成交额、佣金、印花税、成交后现金、持股、资产、仓位、原因和明细。
- `summary.json`：策略与 100 万全仓持有基准的最终资产、收益、alpha、最大回撤、交易次数、换手率、盘口 fallback、涨跌停跳过和数据质量告警。

## 数据格式

历史行情加载器兼容两种 CSV：

- 10 列分钟线：`server_time, price, open, high, low, prev_close, cum_volume, cum_amount, tick_vol, tick_amt`
- 30 列高频线：`local_time_ms, server_time, price, open, high, low, prev_close, cum_volume, cum_amount, bp1..bp5/bv1..bv5/sp1..sp5/sv1..sv5, signal`

加载时会按 `dt + local_time_ms` 排序，并始终根据 `cum_volume/cum_amount` 重算 `tick_vol/tick_amt`。分钟线缺失的盘口字段会填 0。

## 测试

```powershell
python -m unittest discover -s tests
```

测试覆盖：

- 分钟线和 3 秒线的 30 分钟因子窗口；
- 实盘状态保存和恢复；
- 实时写入器跳过旧行情快照；
- 历史 loader 重算增量并填充盘口字段；
- 回测成交价、最低佣金、涨跌停拦截、基准建仓和输出落盘。
