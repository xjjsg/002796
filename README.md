# 002796.SZ V5 策略监控

这是一个针对 `002796.SZ 世嘉科技` 的长期持有增强策略项目。核心目标是在保留底仓的前提下，通过跨日仓位调节和日内局部 T 获取相对持有基准的 alpha。

## 文件结构

- `strategy_core.py`：因子计算、仓位/交易执行、基础策略。
- `combined_strategy_v5.py`：V5 策略，包含跨日加减仓和局部 T 逻辑。
- `gui_realtime_002796.py`：实时行情拉取、策略运行、GUI 展示、状态持久化。
- `run_v5_backtest.py`：完整历史回测。
- `run_v5_real_entry_backtest.py`：历史真实接管回测。
- `run_live_review.py`：按当前实盘配置复盘策略相对固定持有的 alpha。
- `data_quality.py`：历史 CSV 和实时 tick 数据质量检查。
- `tests/test_smoke.py`：最小回归测试。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 实盘配置

真实持仓配置放在：

```text
data/sz002796/live_config.json
```

如果没有该文件，复制 example：

```powershell
Copy-Item data\sz002796\live_config.example.json data\sz002796\live_config.json
```

当前配置字段：

```json
{
  "symbol": "sz002796",
  "name": "世嘉科技",
  "shares": 3000,
  "cash": 10000.0,
  "cost_price": 44.144,
  "updated_at": "2026-06-02"
}
```

## 状态持久化

实时程序会自动维护：

- `data/sz002796/sz002796_strategy_state.json`：现金、持股、目标仓位、当日交易次数、上一笔交易时间、本地 T 状态、交易记录。
- `data/sz002796/sz002796_strategy_trades.csv`：每笔成交的扩展流水，包含因子、score、成交后资产和仓位。

启动优先读取状态文件。没有状态文件时，才按 `live_config.json` 接管初始持仓。启动日志会同时显示配置、状态文件和最新行情折算仓位，便于核对。

## 运行

实时 GUI：

```powershell
python gui_realtime_002796.py
```

完整回测：

```powershell
python run_v5_backtest.py
```

当前实盘配置复盘：

```powershell
python run_live_review.py --start-date 2026-06-01 --show-trades
```

## 数据质量

历史数据加载时会检查：

- 必需列是否存在；
- 时间戳是否有效；
- 价格是否为正；
- 重复时间戳；
- 累计成交量/成交额倒退；
- 相邻样本价格异常跳变。

实时 tick 如果出现严重异常，会记录 `[DQ:CRITICAL]` 并跳过该 tick，不送入策略。

## 测试

```powershell
python -m unittest discover -s tests
```

测试覆盖：

- 分钟级和 3s 级数据下 30 分钟窗口语义一致；
- 策略状态保存/恢复；
- 同一份 CSV 回测交易记录可复现。
