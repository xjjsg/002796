# QMT 集成说明

`qmt/` 目录负责 miniQMT 相关能力：连接检查、实时盘口检测、QMT tick 数据同步、项目侧 tick 回测、实时行情同步，以及受控的模拟账户下单。这里不会修改 `CombinedStrategyV6` 的策略算法。

默认目标：

- 标的：`002796.SZ`
- 模拟账户：`99005544`
- 回测起点：`20260105`
- 当前常用数据终点：`20260618`

## 1. 检查 miniQMT 连接

先启动并登录 miniQMT，然后运行：

```powershell
python -m qmt.check_login
```

指定账户：

```powershell
python -m qmt.check_login --account-id 99005544
```

该命令只检查 xtquant 路径、行情服务和账户查询，不会发送订单。

miniQMT 的 `xtquant` 原生模块与 Python 版本绑定。如果检查结果显示 native module 不匹配，请用 miniQMT 目录中支持的 Python 版本运行，例如 `cp311` 对应 Python 3.11。

## 2. 检查实时五档盘口

默认在 `13:00:00` 附近采样 60 秒：

```powershell
python -m qmt.check_realtime_orderbook
```

立即测试：

```powershell
python -m qmt.check_realtime_orderbook --start-at 00:00 --duration-seconds 20
```

报告写入：

```text
qmt/analysis/realtime_orderbook/
```

`qmt/analysis/` 是生成目录，已加入忽略规则。

## 3. 用 QMT tick 更新本地行情

更新并应用到 `data/sz002796/`：

```powershell
python -m qmt.update_local_data --apply --end-time 20260618150000
```

默认行为：

- QMT 历史 tick 替换已有的分钟级 CSV 日期。
- 本地五档盘口日保留，因为 QMT 历史 tick 通常不含五档盘口。
- 本地 QMT 覆盖范围外的日期保留。
- 应用前会生成 `data/sz002796_backup_<timestamp>/` 备份目录。

可选参数：

- `--download`：先要求 miniQMT 刷新历史 tick。
- `--replace-orderbook-days`：强制用无盘口的 QMT 历史 tick 替换本地盘口日，默认不建议。

## 4. 项目侧 tick 回测

```powershell
python -m qmt.tick_backtest --start-time 20260105 --end-time 20260618
```

需要刷新历史数据时：

```powershell
python -m qmt.tick_backtest --start-time 20260105 --end-time 20260618 --download
```

输出目录：

```text
qmt/backtest_records/tick_v6_<start>_to_<end>/
```

该回测在项目侧完成模拟成交，不调用 `xttrader.order_stock`。`qmt/backtest_records/` 是生成目录，已加入忽略规则。

## 5. 实时行情同步和受控下单

数据同步测试，不连接下单路径：

```powershell
python -m qmt.run_live --data-only --duration-seconds 300
```

使用模拟账户 `99005544` 运行受控下单路径：

```powershell
python -m qmt.run_live
```

放宽模拟账户软限制：

```powershell
python -m qmt.run_live `
  --account-id 99005544 `
  --max-order-value 0 `
  --max-shares-per-order 0 `
  --min-order-interval-seconds 0
```

硬限制仍然生效：账户 id、整手、买一/卖一、可用资金、可卖股数和涨跌停保护。

实时 tick 会追加到：

```text
data/sz002796/sz002796-YYYY-MM-DD.csv
```

如果 CSV 被 Excel/WPS 锁住，数据会临时写入当前 live session 的 `market_csv_spool/`，后续再回灌到根数据目录。

事件日志写入：

```text
qmt/live_records/live_v6_<timestamp>/events.jsonl
```

`qmt/live_records/` 是运行生成目录，已加入忽略规则。

## 6. 配置项

环境变量：

| 变量 | 默认值 |
|------|--------|
| `QMT_INSTALL_DIR` | `D:\国金QMT交易端模拟` |
| `MINI_QMT_PATH` | `<QMT_INSTALL_DIR>\userdata_mini` |
| `XTQUANT_SITE_PACKAGES` | `<QMT_INSTALL_DIR>\bin.x64\Lib\site-packages` |
| `QMT_TARGET_SYMBOL` | `002796.SZ` |
| `QMT_SIM_ACCOUNT` | `99005544` |
| `QMT_ACCOUNT_ID` | 空时使用 `QMT_SIM_ACCOUNT` |
| `QMT_BACKTEST_ACCOUNT` | `testS` |
| `QMT_BACKTEST_START_TIME` | `20260105` |
| `QMT_BACKTEST_END_TIME` | 空，表示 latest |
| `QMT_BACKTEST_OUTPUT_ROOT` | `qmt/backtest_records` |

旧的 `new/config.py` 兼容读取已经移除。当前配置只来自环境变量和本文件默认值。

## 7. 模块索引

| 文件 | 说明 |
|------|------|
| `adapter.py` | 将 QMT tick 归一化为策略 tick。 |
| `check_login.py` | 检查 miniQMT、xtquant 和账户连接。 |
| `check_realtime_orderbook.py` | 检查实时五档盘口可用性。 |
| `compare_local_qmt.py` | 对比本地 CSV 与 QMT tick。 |
| `update_local_data.py` | 用 QMT tick 更新本地行情 CSV。 |
| `tick_backtest.py` | 项目侧 QMT tick 回测。 |
| `live_data.py` | 实时 tick 订阅。 |
| `trade_gateway.py` | 受控 QMT 下单网关。 |
| `run_live.py` | 实时行情、账户同步、策略信号和下单整合。 |
| `anti_overfit_validation.py` | 防过拟合验证与模块归因。 |
| `stability_analysis.py` | 因子和参数稳定性分析。 |
