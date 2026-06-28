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
| `run_web.py` | 启动 aiohttp + React 实时策略工作台。 |
| `run_gui.py` | 兼容启动别名，转到新的 Web 工作台。 |
| `run_backtest.py` | 启动默认历史回测。 |
| `frontend/` | React/Vite 前端源码。 |
| `sz002796/web_server.py` | HTTP、WebSocket 和静态资源服务。 |
| `sz002796/web_runtime.py` | 可启停运行时、事件广播和工作台读模型。 |
| `sz002796/live_engine.py` | 桌面端与 Web 端共用的实时策略线程。 |
| `sz002796/dashboard.py` | 策略对象到稳定 JSON 数据协议的转换层。 |
| `sz002796/strategy_v6.py` | 当前 V6 策略状态机。 |
| `sz002796/backtest.py` | 本地 CSV 回测引擎和模拟成交逻辑。 |
| `sz002796/factors.py` | 日内 VWAP、动量、量能和盘口因子。 |
| `sz002796/regime.py` | 趋势/震荡状态识别。 |
| `sz002796/state_store.py` | Web 运行时策略状态恢复与保存。 |
| `sz002796/realtime_sources.py` | Tencent 与 QMT 实时行情源适配。 |
| `qmt/` | miniQMT 数据、回测、实时同步和受控下单接口。 |
| `data/sz002796/` | 本地行情 CSV。 |
| `backtest_records/` | 默认 V6 回测记录。 |
| `tests/` | 单元和回归测试。 |

打包生成物不属于源码仓库。`build/`、`dist/`、`*.spec`、`__pycache__/`、QMT 分析输出和本地运行态文件都被忽略，可以随时重新生成。

## Web 策略工作台

启动：

```powershell
python run_web.py
```

默认访问地址：

```text
http://127.0.0.1:8796
```

关闭自动打开浏览器：

```powershell
python run_web.py --no-browser
```

工作台包括：

- 实时监控：价格、VWAP、仓位、策略决策、五档盘口和信号强度。
- 结构化交易：成交通知、仓位变化、金额、状态、触发原因和策略详情。
- 回测分析：收益、基准、最大回撤、换手率与运行摘要。
- 交易记录：按方向和原因筛选完整流水。
- 数据管理：本地数据覆盖、体积、运行文件和质量提示。
- 系统设置：行情源、运行模式和服务架构概览。

系统日志不再作为默认主界面。底部默认展示交易记录，原始日志放在独立标签页中。

前端生产资源已生成在 `sz002796/web_assets/`。修改 `frontend/` 后重新构建：

```powershell
cd frontend
pnpm install
pnpm build
```

开发模式可使用 Vite：

```powershell
pnpm dev
```

## 兼容启动入口

启动：

```powershell
python run_gui.py
```

该命令保留给旧脚本和快捷方式，实际启动的仍是 Web 工作台。运行引擎以 V6 模拟账户为仓位来源，并先回放：

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
$env:WEB_MARKET_SOURCE="qmt"; python run_web.py
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
