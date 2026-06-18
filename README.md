# 002796.SZ V6 策略项目

本项目维护 `002796.SZ` 的 `CombinedStrategyV6` 策略、历史回测、实时监控和 QMT 数据接入。当前版本的核心定位是：以趋势仓位护栏控制风险，以跨日调仓处理慢变量，以方向化日内 T 捕捉可确认的日内波动。

当前本地数据样本已包含至 `2026-06-18`。默认回测从 `2026-01-05` 开始，初始资金 `1,000,000`，初始策略仓位 `70%`。

## 当前策略结构

`CombinedStrategyV6` 由五个主要模块组成：

| 模块 | 作用 |
|------|------|
| 市场趋势识别 | 将市场状态分为 `UPTREND`、`DOWNTREND`、`RANGE`，生成仓位上下限。 |
| 跨日调仓 | 处理慢速加减仓，买入信号延迟到下午执行，避免早盘追入。 |
| 方向化日内 T | `DOWNTREND` 偏先卖后买，`UPTREND` 偏先买后卖，`RANGE` 允许双向 T。 |
| 仓位修正 | 维护趋势仓和底仓，short T 期间只触发 hard floor 保护。 |
| 主力流出防守 | 上涨趋势中出现分布式杀跌时降至防守仓。 |

当前默认参数保持偏稳健：

- `local_enter_score = 0.80`
- `local_cover_enter_score = 0.85`
- `max_local_t_cycles_per_day = 1`
- `min_target_move = 0.04`
- directional T 默认 `14:35:00` 强制闭环

## 目录结构

| 路径 | 说明 |
|------|------|
| `run_gui.py` | 启动实时 GUI 监控。 |
| `run_backtest.py` | 启动默认历史回测。 |
| `sz002796/strategy_v6.py` | 当前 V6 策略状态机。 |
| `sz002796/backtest.py` | 本地 CSV 回测引擎和模拟成交逻辑。 |
| `sz002796/factors.py` | 日内 VWAP、动量、量能和盘口因子。 |
| `sz002796/regime.py` | 趋势/震荡状态识别。 |
| `sz002796/state_store.py` | GUI 策略状态恢复与保存。 |
| `sz002796/realtime_sources.py` | Tencent 与 QMT 实时行情源适配。 |
| `qmt/` | miniQMT 数据、回测、实时同步和受控下单接口。 |
| `data/sz002796/` | 本地行情 CSV。 |
| `backtest_records/` | 默认 V6 回测记录。 |
| `tests/` | 单元和回归测试。 |

打包生成物不属于源码仓库。`build/`、`dist/`、`*.spec`、`__pycache__/`、QMT 分析输出和本地运行态文件都被忽略，可以随时重新生成。

## 实时 GUI

启动：

```powershell
python run_gui.py
```

GUI 以 V6 模拟账户为仓位来源，会先回放：

```text
backtest_records/v6_seed70_100w_2026-01-05_to_latest/trades.csv
```

运行时生成的本地状态文件会被忽略：

```text
data/sz002796/sz002796_v6_strategy_state.json
data/sz002796/sz002796_v6_strategy_trades.csv
```

行情源可选：

- `tencent`：现有 Tencent 行情接口。
- `qmt`：miniQMT 实时 tick 订阅，默认只作为行情源，不直接下单。

设置默认行情源：

```powershell
$env:GUI_MARKET_SOURCE="qmt"; python run_gui.py
```

## 历史回测

默认回测只读取本地 CSV：

```powershell
python run_backtest.py
```

输出目录：

```text
backtest_records/v6_seed70_100w_2026-01-05_to_latest/
```

主要输出：

- `trades.csv`：逐笔交易、成交价、股数、金额、费用、现金、仓位和原因。
- `summary.json`：最终资产、基准比较、alpha、回撤、交易次数、换手率、盘口 fallback、涨跌停跳过次数、regime 统计和数据质量提示。

## 防过拟合验证

验证入口：

```powershell
python qmt/anti_overfit_validation.py
```

轻量对比当前 directional T 和 legacy T：

```powershell
python qmt/anti_overfit_validation.py --legacy-compare-only
```

验证脚本会：

- 使用完整交易日样本，避免把半天或残缺 CSV 当作最新窗口。
- 扫描生产策略代码中的明显未来视野风险。
- 分月、分滑点、分数据源对比候选参数。
- 输出模块 alpha 贡献、local T 贡献和 floor refill 冲突。

## QMT

QMT 相关命令见 [qmt/README.md](qmt/README.md)。

常用入口：

```powershell
python -m qmt.check_login
python -m qmt.check_realtime_orderbook
python -m qmt.update_local_data --apply --end-time 20260618150000
python -m qmt.tick_backtest --start-time 20260105 --end-time 20260618
python -m qmt.run_live --data-only --duration-seconds 300
```

## 测试

```powershell
python -m unittest discover -s tests
```

当前测试覆盖：

- 因子窗口和 3 秒 tick 数据
- V6 状态保存/恢复
- 实时行情源 fallback
- 本地 CSV loader 和盘口字段填充
- 模拟成交、最低佣金和涨跌停限制
- regime、main-flow guard 和 directional local T 状态机
- QMT adapter、实时数据、受控下单和数据更新
- 防过拟合验证辅助函数

## 清理规则

仓库保留源码、测试、行情数据和回测记录。以下内容视为可再生成，不提交：

- `build/`
- `dist/`
- `*.spec`
- `__pycache__/`
- `.pytest_cache/`
- `qmt/analysis/`
- `qmt/backtest_records/`
- `qmt/live_records/`
- `data/*_backup_*/`
- `data/*/*_strategy_state.json`
- `data/*/*_strategy_trades.csv`
