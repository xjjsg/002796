"""CustomTkinter realtime monitor for the V6 strategy.

The GUI shows live price, factors, orderbook, and the strategy's simulated
position. Its worker thread restores the 2026-01-05 1,000,000-cash V6 backtest
account from trade logs, then appends only verified realtime ticks and trades.
"""
import asyncio
import os
import threading
import queue
import time
from datetime import datetime
import tkinter as tk
import customtkinter as ctk

from .config import (
    INITIAL_CASH, INITIAL_SHARES, INITIAL_TARGET_PCT, INITIAL_CAPITAL,
    FETCH_INTERVAL, STATE_SAVE_INTERVAL, DATA_DIR, STATE_FILE, TRADE_LOG_FILE,
    BACKTEST_TRADE_LOG_FILE, LOCAL_T0_ENTER_SCORE, GUI_MARKET_SOURCE,
    SYMBOL_CODE, SYMBOL_NAME, is_trading_time, get_next_window, seconds_until
)
from .state_store import StrategyStateStore
from .tick_writer import TickDataWriter
from .realtime_sources import (
    MARKET_SOURCE_OPTIONS,
    create_market_data_source,
    market_source_label,
    normalize_market_source_id,
)
from .strategy_v6 import CombinedStrategyV6
from .position import PositionMode
from .data_quality import RealtimeDataQualityMonitor, has_critical_issue


class TickChartBuffer:
    def __init__(self, max_points: int = 300):
        self.max_points = max(2, int(max_points))
        self.points: list[tuple[str, float]] = []

    def append(self, tick: dict) -> None:
        price = float(tick.get("price", tick.get("Close", 0.0)) or 0.0)
        if price <= 0:
            return
        label = str(tick.get("server_time") or "")
        if not label:
            value = tick.get("Time") or tick.get("dt")
            label = value.strftime("%H:%M:%S") if isinstance(value, datetime) else str(value or "")
        self.points.append((label, price))
        if len(self.points) > self.max_points:
            self.points = self.points[-self.max_points :]

    def price_bounds(self) -> tuple[float, float]:
        if not self.points:
            return 0.0, 1.0
        prices = [price for _, price in self.points]
        low = min(prices)
        high = max(prices)
        if abs(high - low) < 1e-9:
            pad = max(abs(high) * 0.001, 0.01)
            return low - pad, high + pad
        pad = max((high - low) * 0.08, 0.01)
        return low - pad, high + pad

    def scaled_points(self, width: int, height: int, pad: int = 24) -> list[tuple[float, float]]:
        if not self.points:
            return []
        width = max(int(width), pad * 2 + 1)
        height = max(int(height), pad * 2 + 1)
        low, high = self.price_bounds()
        span = high - low if high > low else 1.0
        plot_w = max(width - pad * 2, 1)
        plot_h = max(height - pad * 2, 1)
        count = len(self.points)
        result: list[tuple[float, float]] = []
        for idx, (_, price) in enumerate(self.points):
            x = pad + (plot_w if count == 1 else plot_w * idx / (count - 1))
            y = pad + (high - price) / span * plot_h
            result.append((x, y))
        return result

def _position_summary(shares: int, cash: float, price: float | None) -> str:
    if price and price > 0:
        asset = shares * price + cash
        pct = shares * price / asset if asset > 0 else 0.0
        return f"{shares} 股 | 现金 {cash:,.2f} | 最新价 {price:.2f} | 资产 {asset:,.2f} | 仓位 {pct*100:.1f}%"
    return f"{shares} 股 | 现金 {cash:,.2f} | 最新价 -- | 仓位 --"

def log_position_reconciliation(
    log_queue: queue.Queue,
    strategy: CombinedStrategyV6,
    loaded_state: dict | None,
    latest_tick: dict | None,
) -> None:
    latest_price = None
    if latest_tick:
        latest_price = float(latest_tick.get("price", latest_tick.get("Close", 0.0)) or 0.0)
    log_queue.put("[CHECK] 持仓核对")
    log_queue.put(
        "[CHECK] 账户起点: "
        + _position_summary(INITIAL_SHARES, INITIAL_CASH, latest_price)
        + " | 2026-01-05 起始 100 万现金，首笔按回测买入 70% 基准仓位"
    )
    if loaded_state:
        log_queue.put(
            "[CHECK] 状态: "
            + _position_summary(int(loaded_state.get("shares", 0) or 0), float(loaded_state.get("cash", 0.0) or 0.0), latest_price)
            + f" | 保存时间 {loaded_state.get('saved_at', '-')} | 原因 {loaded_state.get('save_reason', '-')}"
        )
        replay = loaded_state.get("position_replay") or {}
        if replay.get("replayed_count", 0):
            log_queue.put(
                f"[CHECK] 交易流水重放: {replay.get('source', '-')} | "
                f"{replay.get('replayed_count', 0)} 笔 | 现金/持股已按回测成本模型重算"
            )
            log_queue.put(
                f"[CHECK] 仓位种子: {replay.get('position_seed', '-')} | "
                f"回测 {replay.get('seed_rows', 0)} 笔 | 运行增量 {replay.get('runtime_rows', 0)} 笔"
            )
            for warning in replay.get("warnings", [])[:3]:
                log_queue.put(f"[CHECK:WARN] {warning}")
    else:
        log_queue.put("[CHECK] 状态: 未找到可用回测流水，未恢复仓位")
    log_queue.put("[CHECK] 运行: " + _position_summary(strategy.shares, strategy.cash, latest_price))


def worker_thread(update_queue: queue.Queue, log_queue: queue.Queue, market_source_id: str = "tencent"):
    async def _async_main():
        if not os.path.exists(BACKTEST_TRADE_LOG_FILE):
            log_queue.put(f"[!] 缺少回测仓位流水，GUI 不启动: {os.path.abspath(BACKTEST_TRADE_LOG_FILE)}")
            log_queue.put("[!] 请先运行 python run_backtest.py 生成 2026-01-05 起步的 100 万模拟账户流水")
            return

        selected_source_id = normalize_market_source_id(market_source_id)
        requested_source_label = market_source_label(selected_source_id)
        writer = TickDataWriter(DATA_DIR, SYMBOL_CODE)
        state_store = StrategyStateStore(
            STATE_FILE,
            TRADE_LOG_FILE,
            seed_trade_log_path=BACKTEST_TRADE_LOG_FILE,
            seed_cash=INITIAL_CASH,
            seed_shares=INITIAL_SHARES,
            seed_target_pct=INITIAL_TARGET_PCT,
            seed_asset_base=INITIAL_CAPITAL,
        )
        quality_monitor = RealtimeDataQualityMonitor()
        strategy = CombinedStrategyV6(
            initial_capital=INITIAL_CAPITAL,
            local_enter_score=LOCAL_T0_ENTER_SCORE,
        )

        ignored_state = None
        try:
            loaded_state = state_store.load(strategy)
            ignored_state = state_store.ignored_state
        except Exception as exc:
            log_queue.put(f"[!] 状态恢复失败，已停止启动: {exc}")
            log_queue.put(f"[!] 请检查或移走状态文件: {os.path.abspath(STATE_FILE)}")
            return

        if loaded_state:
            log_queue.put(
                f"[STATE] 已恢复 {strategy.shares} 股 | 目标 {strategy.target_pct*100:.1f}% | "
                f"现金 {strategy.cash:,.2f} | 保存时间 {loaded_state.get('saved_at', '-')}"
            )
        else:
            if ignored_state:
                log_queue.put(
                    f"[STATE] 已忽略旧状态 {ignored_state.get('shares', '-')} 股 | "
                    f"现金 {float(ignored_state.get('cash', 0.0) or 0.0):,.2f} | "
                    f"保存时间 {ignored_state.get('saved_at', '-')}"
                )
            log_queue.put("[!] 未能从回测流水恢复仓位，已停止启动")
            return

        log_queue.put(f"初始化完成 | 标的: {SYMBOL_CODE} {SYMBOL_NAME}")
        log_queue.put(f"数据目录: {os.path.abspath(DATA_DIR)}")
        log_queue.put(f"状态文件: {os.path.abspath(STATE_FILE)}")
        log_queue.put(f"实时交易流水: {os.path.abspath(TRADE_LOG_FILE)}")
        log_queue.put(f"回测仓位种子: {os.path.abspath(BACKTEST_TRADE_LOG_FILE)}")
        log_queue.put("策略引擎: CombinedStrategyV6")
        log_queue.put(f"行情源: {requested_source_label}")
        log_queue.put(f"局部 T 阈值: {LOCAL_T0_ENTER_SCORE:.2f}")
        log_queue.put(
            f"当前持仓: {strategy.shares} 股 | 现金 {strategy.cash:,.2f} | "
            f"资产基准 {strategy.initial_capital:,.2f}"
        )

        market_source = create_market_data_source(selected_source_id, SYMBOL_CODE)

        def current_source_id() -> str:
            return getattr(market_source, "active_source_id", selected_source_id)

        def current_source_label() -> str:
            return getattr(market_source, "active_label", requested_source_label)

        def flush_source_events() -> None:
            for event in market_source.pop_status_events():
                log_queue.put(event)

        try:
            await market_source.start()
            flush_source_events()
        except Exception as exc:
            log_queue.put(f"[!] 行情源启动失败 ({requested_source_label}): {exc}")
            return

        try:
            log_queue.put(f"正在测试数据连接 ({current_source_label()})...")
            test_tick = await market_source.fetch_initial_tick()
            flush_source_events()
            if test_tick:
                log_queue.put(f"[OK] {current_source_label()} 连接成功，当前价 {test_tick['price']:.2f}")
                await market_source.reset_stale_guard()
            else:
                log_queue.put(f"[!] {current_source_label()} 首次连接未返回新行情")
                await market_source.reset_stale_guard()
            log_position_reconciliation(log_queue, strategy, loaded_state or ignored_state, test_tick)

            consecutive_errors = 0
            last_state_save_ts = 0.0
            while True:
                try:
                    if not is_trading_time():
                        next_win, win_name = get_next_window()
                        wait_s = seconds_until(next_win) if next_win else 1.0
                        now_ts = time.time()
                        if now_ts - last_state_save_ts >= STATE_SAVE_INTERVAL:
                            state_store.save(strategy, reason="pause_snapshot")
                            last_state_save_ts = now_ts
                        update_queue.put(
                            {
                                "status": "PAUSE",
                                "win_name": win_name,
                                "wait_s": wait_s,
                                "market_source": current_source_id(),
                                "market_source_label": current_source_label(),
                                "requested_market_source": selected_source_id,
                            }
                        )
                        await asyncio.sleep(min(30, max(1, wait_s - 5)))
                        continue

                    start_ts = time.time()
                    tick = await market_source.fetch()
                    flush_source_events()
                    if tick is None:
                        elapsed = time.time() - start_ts
                        await asyncio.sleep(max(0, FETCH_INTERVAL - elapsed))
                        continue

                    consecutive_errors = 0
                    dq_issues = quality_monitor.check(tick)
                    for issue in dq_issues:
                        log_queue.put(f"[DQ:{issue.severity.upper()}] {issue.message}")
                    if has_critical_issue(dq_issues):
                        writer.write(tick, "DQ_SKIP")
                        state_store.save(strategy, tick, reason="data_quality_skip")
                        elapsed = time.time() - start_ts
                        await asyncio.sleep(max(0, FETCH_INTERVAL - elapsed))
                        continue

                    trade_record = strategy.on_tick(tick)
                    signal_str = "HOLD"
                    if trade_record:
                        signal_str = trade_record.side
                        log_queue.put(
                            f"[{trade_record.side}] @ {trade_record.price:.2f} | "
                            f"{trade_record.reason} | {trade_record.detail}"
                        )
                        trade_record = state_store.append_trade(trade_record, strategy=strategy, tick=tick)
                        state_store.reconcile_from_trade_log(strategy)
                        if strategy.trades:
                            trade_record = strategy.trades[-1]
                        state_store.save(strategy, tick, reason="trade")
                        last_state_save_ts = time.time()
                    elif time.time() - last_state_save_ts >= STATE_SAVE_INTERVAL:
                        state_store.save(strategy, tick, reason="heartbeat")
                        last_state_save_ts = time.time()

                    writer.write(tick, signal_str)
                    update_queue.put(
                        {
                            "status": "RUNNING",
                            "tick": tick,
                            "strategy": strategy,
                            "trade_record": trade_record,
                            "market_source": current_source_id(),
                            "market_source_label": current_source_label(),
                            "requested_market_source": selected_source_id,
                        }
                    )

                    elapsed = time.time() - start_ts
                    await asyncio.sleep(max(0, FETCH_INTERVAL - elapsed))

                except Exception as exc:
                    consecutive_errors += 1
                    flush_source_events()
                    log_queue.put(f"[ERROR] 主循环错误 #{consecutive_errors}: {exc}")
                    import traceback

                    traceback.print_exc()
                    await asyncio.sleep(10 if consecutive_errors > 5 else 2)
        finally:
            await market_source.close()

    asyncio.run(_async_main())


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("002796.SZ 世嘉科技 - V6 策略监控终端")
        self.geometry("1280x820")
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.update_queue = queue.Queue()
        self.log_queue = queue.Queue()
        default_source_id = normalize_market_source_id(GUI_MARKET_SOURCE)
        self.market_source_var = ctk.StringVar(value=market_source_label(default_source_id))
        self.worker = None
        self.worker_running = False
        self.tick_chart = TickChartBuffer(max_points=300)
        self.previous_factor_values: dict[str, float] = {}
        
        self._setup_ui()
        self.log_queue.put("请选择行情源后点击启动")
        
        self.after(50, self.process_queues)

    def _add_score_row(self, parent, row: int, key: str, title: str, direction: str) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, padx=15, pady=4, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        label = ctk.CTkLabel(frame, text=title, width=92, anchor="w", font=ctk.CTkFont(size=13))
        label.grid(row=0, column=0, sticky="w")
        bar = ctk.CTkProgressBar(frame, height=9)
        bar.grid(row=0, column=1, padx=10, sticky="ew")
        bar.set(0)
        value = ctk.CTkLabel(frame, text="等待", width=118, anchor="e", font=ctk.CTkFont(size=12))
        value.grid(row=0, column=2, sticky="e")
        self.score_rows[key] = {"bar": bar, "value": value, "direction": direction}

    def _add_factor_row(self, parent, row: int, key: str, title: str) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, padx=15, pady=2, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=title, anchor="w", font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="w")
        value = ctk.CTkLabel(frame, text="等待", width=118, anchor="e", font=ctk.CTkFont(size=12))
        value.grid(row=0, column=1, sticky="e")
        delta = ctk.CTkLabel(frame, text="未成型", width=92, anchor="e", font=ctk.CTkFont(size=12), text_color="gray")
        delta.grid(row=0, column=2, padx=(10, 0), sticky="e")
        self.factor_labels[key] = {"value": value, "delta": delta}

    @staticmethod
    def _pct(value: float | None, digits: int = 2, signed: bool = True) -> str:
        if value is None:
            return "--"
        sign = "+" if signed else ""
        return f"{value * 100:{sign}.{digits}f}%"

    @staticmethod
    def _num(value: float | None, digits: int = 2) -> str:
        if value is None:
            return "--"
        return f"{value:.{digits}f}"

    @staticmethod
    def _score_color(value: float, threshold: float, direction: str) -> str:
        if value >= threshold:
            return "#D85B5B" if direction == "BUY" else "#2AA876"
        if value >= threshold * 0.75:
            return "#C2A04A"
        return "#3F5F7D"

    def _set_score(self, key: str, value: float, threshold: float) -> None:
        row = self.score_rows[key]
        bar = row["bar"]
        label = row["value"]
        direction = row["direction"]
        clipped = max(0.0, min(1.0, float(value or 0.0)))
        bar.set(clipped)
        bar.configure(progress_color=self._score_color(clipped, threshold, direction))
        label_text, label_color = self._score_state_text(key, clipped, threshold)
        label.configure(text=label_text, text_color=label_color)

    @staticmethod
    def _score_state_text(key: str, value: float, threshold: float) -> tuple[str, str]:
        strong = value >= threshold
        warming = value >= threshold * 0.75
        forming = value >= max(0.18, threshold * 0.45)
        specs = {
            "cross_buy": ("加仓触发", "偏多酝酿", "低位修复", "无加仓倾向", "#D85B5B"),
            "cross_sell": ("减仓触发", "偏空酝酿", "高位转弱", "无减仓倾向", "#2AA876"),
            "local_trim": ("短线减仓", "冲高警惕", "局部偏热", "无T减仓", "#2AA876"),
            "local_cover": ("短线回补", "回补酝酿", "低位修复", "无T回补", "#D85B5B"),
            "main_flow": ("防守触发", "流出警戒", "承压观察", "未见流出", "#2AA876"),
            "buy_timing": ("买点确认", "买点接近", "修复观察", "买点未到", "#D85B5B"),
            "sell_timing": ("卖点确认", "卖点接近", "衰减观察", "卖点未到", "#2AA876"),
        }
        active, near, mild, quiet, active_color = specs.get(
            key, ("信号触发", "接近触发", "开始成型", "未成型", "#C2A04A")
        )
        if strong:
            return active, active_color
        if warming:
            return near, "#C2A04A"
        if forming:
            return mild, "#8C98A4"
        return quiet, "gray"

    @staticmethod
    def _safe_score(strategy: CombinedStrategyV6, method_name: str, snapshot) -> float:
        if snapshot is None:
            return 0.0
        try:
            return float(getattr(strategy, method_name)(snapshot) or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _leading_signal_text(key: str) -> str:
        labels = {
            "cross_buy": "跨日加仓倾向",
            "cross_sell": "跨日减仓倾向",
            "local_trim": "短线减仓倾向",
            "local_cover": "短线回补倾向",
            "main_flow": "主力流出防守",
        }
        return labels.get(key, "暂无主导倾向")

    @staticmethod
    def _factor_delta_text(key: str, delta: float | None) -> tuple[str, str]:
        if delta is None:
            return "等待", "gray"
        if abs(delta) < 1e-12:
            return "持平", "gray"
        up_text = "增强"
        down_text = "减弱"
        if key in {"pullback_from_high", "below_vwap_ratio"}:
            up_text, down_text = "压力增加", "压力减轻"
        if key == "orderbook_imbalance":
            up_text, down_text = "买盘增强", "卖压增强"
        return (up_text, "#D85B5B") if delta > 0 else (down_text, "#2AA876")

    def _update_factor_rows(self, values: dict[str, str], raw_values: dict[str, float | None]) -> None:
        for key, row in self.factor_labels.items():
            prediction_text, prediction_color = self._factor_prediction_text(key, raw_values.get(key))
            row["value"].configure(text=prediction_text, text_color=prediction_color)
            current = raw_values.get(key)
            previous = self.previous_factor_values.get(key)
            delta = current - previous if current is not None and previous is not None else None
            delta_text, delta_color = self._factor_delta_text(key, delta)
            row["delta"].configure(text=delta_text, text_color=delta_color)
        self.previous_factor_values = {
            key: value for key, value in raw_values.items() if value is not None
        }

    @staticmethod
    def _factor_prediction_text(key: str, value: float | None) -> tuple[str, str]:
        if value is None:
            return "等待", "gray"
        red = "#D85B5B"
        green = "#2AA876"
        amber = "#C2A04A"
        gray = "#8C98A4"
        if key == "day_return":
            if value > 0.025:
                return "日内强势", red
            if value < -0.025:
                return "日内弱势", green
            return "震荡中性", gray
        if key == "day_vwap_dev":
            if value > 0.018:
                return "高位偏热", amber
            if value < -0.006:
                return "低位修复", red
            return "均值附近", gray
        if key == "local_vwap_dev":
            if value > 0.006:
                return "短线冲高", amber
            if value < -0.004:
                return "短线低吸", red
            return "短线均衡", gray
        if key == "velocity":
            if value > 0.004:
                return "动能上行", red
            if value < -0.004:
                return "动能下行", green
            return "动能平缓", gray
        if key == "acceleration":
            if value > 0.002:
                return "趋势加速", red
            if value < -0.002:
                return "动能衰减", green
            return "加速不明", gray
        if key == "vol_mom":
            if value > 1.8:
                return "放量确认", red
            if value < 0.7:
                return "量能不足", green
            return "量能正常", gray
        if key == "range_position":
            if value > 0.75:
                return "靠近日高", amber
            if value < 0.25:
                return "靠近日低", green
            return "区间中部", gray
        if key == "pullback_from_high":
            if value < -0.035:
                return "回撤加深", green
            if value < -0.015:
                return "高位回落", amber
            return "贴近日高", red
        if key == "below_vwap_ratio":
            if value > 0.65:
                return "水下偏弱", green
            if value < 0.25:
                return "水上偏强", red
            return "VWAP拉锯", gray
        if key == "orderbook_imbalance":
            if value > 0.18:
                return "买盘占优", red
            if value < -0.18:
                return "卖压占优", green
            return "盘口均衡", gray
        return "观察", gray

    def _render_tick_chart(self) -> None:
        canvas = self.canvas_tick_chart
        width = max(canvas.winfo_width(), 360)
        height = max(canvas.winfo_height(), 180)
        pad = 28
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#101418", outline="")
        for idx in range(4):
            y = pad + (height - pad * 2) * idx / 3
            canvas.create_line(pad, y, width - pad, y, fill="#26313A")
        if not self.tick_chart.points:
            canvas.create_text(width / 2, height / 2, text="等待 tick", fill="#68727D", font=("Consolas", 12))
            return

        low, high = self.tick_chart.price_bounds()
        scaled = self.tick_chart.scaled_points(width, height, pad)
        if len(scaled) >= 2:
            flat = [coord for point in scaled for coord in point]
            canvas.create_line(*flat, fill="#4EA7FF", width=2)
        else:
            x, y = scaled[0]
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#4EA7FF", outline="")

        latest_label, latest_price = self.tick_chart.points[-1]
        latest_x, latest_y = scaled[-1]
        canvas.create_line(pad, latest_y, width - pad, latest_y, fill="#4EA7FF", dash=(4, 3))
        canvas.create_text(pad, 14, text=f"{high:.2f}", fill="#8C98A4", anchor="w", font=("Consolas", 10))
        canvas.create_text(pad, height - 12, text=f"{low:.2f}", fill="#8C98A4", anchor="w", font=("Consolas", 10))
        canvas.create_text(width - pad, 14, text=f"{latest_price:.2f}", fill="#DDE6EE", anchor="e", font=("Consolas", 12, "bold"))
        canvas.create_text(width - pad, height - 12, text=latest_label, fill="#8C98A4", anchor="e", font=("Consolas", 10))
        canvas.create_oval(latest_x - 3, latest_y - 3, latest_x + 3, latest_y + 3, fill="#DDE6EE", outline="")
        
    def _setup_ui(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=4)
        self.grid_columnconfigure(2, weight=3)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=6)
        self.grid_rowconfigure(2, weight=3)
        
        # --- Header ---
        self.frame_top = ctk.CTkFrame(self, fg_color="#1E1E1E")
        self.frame_top.grid(row=0, column=0, columnspan=3, padx=10, pady=10, sticky="ew")
        
        self.lbl_title = ctk.CTkLabel(self.frame_top, text="世嘉科技 (002796.SZ)", font=ctk.CTkFont(size=28, weight="bold"))
        self.lbl_title.pack(side="left", padx=20)
        
        self.lbl_price = ctk.CTkLabel(self.frame_top, text="--.--", font=ctk.CTkFont(size=40, weight="bold"), text_color="white")
        self.lbl_price.pack(side="left", padx=20)
        
        self.lbl_pct = ctk.CTkLabel(self.frame_top, text="+0.00%", font=ctk.CTkFont(size=20))
        self.lbl_pct.pack(side="left", padx=10)
        
        self.frame_source = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_source.pack(side="right", padx=(0, 10))

        self.lbl_source = ctk.CTkLabel(self.frame_source, text="行情源", font=ctk.CTkFont(size=12), text_color="gray")
        self.lbl_source.pack(side="left", padx=(0, 8))

        source_labels = [option.label for option in MARKET_SOURCE_OPTIONS.values()]
        self.source_selector = ctk.CTkSegmentedButton(
            self.frame_source,
            values=source_labels,
            variable=self.market_source_var,
            width=150,
        )
        self.source_selector.pack(side="left", padx=(0, 8))
        self.source_selector.set(self.market_source_var.get())

        self.btn_start = ctk.CTkButton(self.frame_source, text="启动", width=70, command=self.start_worker)
        self.btn_start.pack(side="left")

        self.lbl_sys_status = ctk.CTkLabel(self.frame_top, text="待启动", font=ctk.CTkFont(size=16), text_color="gray")
        self.lbl_sys_status.pack(side="right", padx=20)
        
        # --- Position Panel ---
        self.frame_position = ctk.CTkFrame(self, corner_radius=6)
        self.frame_position.grid(row=1, column=0, padx=(10, 5), pady=(0, 10), sticky="nsew")
        self.frame_position.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.frame_position, text="账户与仓位", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=15, pady=(14, 8), sticky="w"
        )
        self.lbl_pos_details = ctk.CTkLabel(self.frame_position, text="持股 -- | 仓位 -- | 现金 --", font=ctk.CTkFont(size=14))
        self.lbl_pos_details.grid(row=1, column=0, padx=15, pady=4, sticky="w")
        self.lbl_pnl = ctk.CTkLabel(self.frame_position, text="权益 -- | 盈亏 --", font=ctk.CTkFont(size=16, weight="bold"))
        self.lbl_pnl.grid(row=2, column=0, padx=15, pady=4, sticky="w")
        self.lbl_position_bar = ctk.CTkLabel(self.frame_position, text="实际仓位 -- / 目标 --", font=ctk.CTkFont(size=12), text_color="gray")
        self.lbl_position_bar.grid(row=3, column=0, padx=15, pady=(12, 2), sticky="w")
        self.pb_position = ctk.CTkProgressBar(self.frame_position, height=10)
        self.pb_position.grid(row=4, column=0, padx=15, pady=(0, 10), sticky="ew")
        self.pb_position.set(0)
        self.lbl_mode = ctk.CTkLabel(self.frame_position, text="模式 --", font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_mode.grid(row=5, column=0, padx=15, pady=4, sticky="w")
        self.lbl_trade_counter = ctk.CTkLabel(self.frame_position, text="今日交易 -- / -- | 上次 --", font=ctk.CTkFont(size=13))
        self.lbl_trade_counter.grid(row=6, column=0, padx=15, pady=4, sticky="w")
        self.lbl_local_cycle = ctk.CTkLabel(self.frame_position, text="本地T 周期 --", font=ctk.CTkFont(size=13))
        self.lbl_local_cycle.grid(row=7, column=0, padx=15, pady=4, sticky="w")

        ctk.CTkFrame(self.frame_position, height=1, fg_color="#333333").grid(row=8, column=0, sticky="ew", padx=15, pady=12)
        ctk.CTkLabel(self.frame_position, text="市场状态约束", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=9, column=0, padx=15, pady=(0, 6), sticky="w"
        )
        self.lbl_regime = ctk.CTkLabel(self.frame_position, text="Regime --", font=ctk.CTkFont(size=13), justify="left")
        self.lbl_regime.grid(row=10, column=0, padx=15, pady=3, sticky="w")
        self.lbl_band = ctk.CTkLabel(self.frame_position, text="允许仓位 --", font=ctk.CTkFont(size=13))
        self.lbl_band.grid(row=11, column=0, padx=15, pady=3, sticky="w")
        self.lbl_feed_detail = ctk.CTkLabel(self.frame_position, text="行情源 --", font=ctk.CTkFont(size=12), text_color="gray")
        self.lbl_feed_detail.grid(row=12, column=0, padx=15, pady=(12, 4), sticky="w")
        self.lbl_tick_detail = ctk.CTkLabel(self.frame_position, text="Tick --", font=ctk.CTkFont(size=12), text_color="gray")
        self.lbl_tick_detail.grid(row=13, column=0, padx=15, pady=3, sticky="w")

        # --- Center Panel: tick chart + factors ---
        self.frame_center = ctk.CTkFrame(self, corner_radius=6)
        self.frame_center.grid(row=1, column=1, padx=5, pady=(0, 10), sticky="nsew")
        self.frame_center.grid_columnconfigure(0, weight=1)
        self.frame_center.grid_rowconfigure(0, weight=3)
        self.frame_center.grid_rowconfigure(1, weight=4)

        self.frame_chart = ctk.CTkFrame(self.frame_center, fg_color="transparent")
        self.frame_chart.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="nsew")
        self.frame_chart.grid_columnconfigure(0, weight=1)
        self.frame_chart.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self.frame_chart, text="Tick 价格折线", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=3, pady=(0, 8), sticky="w"
        )
        self.canvas_tick_chart = tk.Canvas(
            self.frame_chart,
            height=210,
            bg="#101418",
            highlightthickness=0,
            bd=0,
        )
        self.canvas_tick_chart.grid(row=1, column=0, sticky="nsew")
        self.canvas_tick_chart.bind("<Configure>", lambda _event: self._render_tick_chart())

        self.frame_factors = ctk.CTkFrame(self.frame_center, fg_color="transparent")
        self.frame_factors.grid(row=1, column=0, padx=12, pady=(6, 12), sticky="nsew")
        self.frame_factors.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.frame_factors, text="核心因子预判", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=3, pady=(0, 8), sticky="w"
        )
        self.factor_labels = {}
        factor_specs = [
            ("day_return", "日涨跌"),
            ("day_vwap_dev", "日VWAP偏离"),
            ("local_vwap_dev", "30m VWAP偏离"),
            ("velocity", "5m动量"),
            ("acceleration", "动量加速度"),
            ("vol_mom", "量能动量"),
            ("range_position", "日内区间位置"),
            ("pullback_from_high", "距日高回撤"),
            ("below_vwap_ratio", "低于VWAP时长"),
            ("orderbook_imbalance", "盘口不平衡"),
        ]
        for idx, (key, title) in enumerate(factor_specs, start=1):
            self._add_factor_row(self.frame_factors, idx, key, title)

        # --- Right Panel: decision stack + orderbook ---
        self.frame_right = ctk.CTkFrame(self, corner_radius=6)
        self.frame_right.grid(row=1, column=2, padx=(5, 10), pady=(0, 10), sticky="nsew")
        self.frame_right.grid_columnconfigure(0, weight=1)
        self.frame_right.grid_rowconfigure(0, weight=4)
        self.frame_right.grid_rowconfigure(1, weight=3)

        self.frame_decision = ctk.CTkFrame(self.frame_right, fg_color="transparent")
        self.frame_decision.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="nsew")
        self.frame_decision.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.frame_decision, text="V6 趋势决策", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=3, pady=(0, 8), sticky="w"
        )
        self.lbl_signal = ctk.CTkLabel(
            self.frame_decision,
            text="WAIT | 尚无信号",
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#333333",
            corner_radius=5,
            height=34,
        )
        self.lbl_signal.grid(row=1, column=0, padx=3, pady=(0, 12), sticky="ew")

        self.score_rows = {}
        score_specs = [
            ("cross_buy", "跨日加仓", "BUY"),
            ("cross_sell", "跨日减仓", "SELL"),
            ("local_trim", "局部T 减仓", "SELL"),
            ("local_cover", "局部T 回补", "BUY"),
            ("main_flow", "主力流出保护", "SELL"),
            ("buy_timing", "买点确认", "BUY"),
            ("sell_timing", "卖点确认", "SELL"),
        ]
        for idx, spec in enumerate(score_specs, start=2):
            self._add_score_row(self.frame_decision, idx, *spec)

        self.lbl_score_note = ctk.CTkLabel(self.frame_decision, text="进度条表示信号成型强弱", font=ctk.CTkFont(size=12), text_color="gray")
        self.lbl_score_note.grid(row=9, column=0, padx=15, pady=(8, 0), sticky="w")

        self.frame_orderbook = ctk.CTkFrame(self.frame_right, fg_color="transparent")
        self.frame_orderbook.grid(row=1, column=0, padx=12, pady=(6, 12), sticky="nsew")
        self.frame_orderbook.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.frame_orderbook, text="买卖五档盘口", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, padx=3, pady=(0, 8), sticky="w"
        )
        self.ob_labels = []
        for idx, level in enumerate(range(5, 0, -1), start=1):
            lbl = ctk.CTkLabel(
                self.frame_orderbook,
                text=f"Ask{level}  --     --",
                font=ctk.CTkFont(family="Consolas", size=13),
                text_color="#2AA876",
            )
            lbl.grid(row=idx, column=0, padx=24, pady=1, sticky="w")
            self.ob_labels.append(lbl)
        ctk.CTkFrame(self.frame_orderbook, height=1, fg_color="#333333").grid(row=6, column=0, sticky="ew", padx=24, pady=4)
        for idx, level in enumerate(range(1, 6), start=7):
            lbl = ctk.CTkLabel(
                self.frame_orderbook,
                text=f"Bid{level}  --     --",
                font=ctk.CTkFont(family="Consolas", size=13),
                text_color="#D85B5B",
            )
            lbl.grid(row=idx, column=0, padx=24, pady=1, sticky="w")
            self.ob_labels.append(lbl)
            
        # --- Bottom Panel ---
        self.frame_bottom = ctk.CTkFrame(self)
        self.frame_bottom.grid(row=2, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="nsew")
        
        self.txt_log = ctk.CTkTextbox(self.frame_bottom, font=ctk.CTkFont(family="Consolas", size=13), fg_color="#121212", text_color="#A0A0A0")
        self.txt_log.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt_log.insert("end", ">>> 系统待启动...\n")
        self.txt_log.configure(state="disabled")

    def start_worker(self):
        if self.worker is not None and self.worker.is_alive():
            return
        source_id = normalize_market_source_id(self.market_source_var.get())
        source_label = market_source_label(source_id)
        self.worker_running = True
        self.btn_start.configure(state="disabled", text="运行中")
        self.source_selector.configure(state="disabled")
        self.lbl_sys_status.configure(text=f"启动中: {source_label}", text_color="gray")
        self.worker = threading.Thread(
            target=worker_thread,
            args=(self.update_queue, self.log_queue, source_id),
            daemon=True,
        )
        self.worker.start()
        self.log(f"行情源选择: {source_label}")

    def log(self, msg: str):
        self.txt_log.configure(state="normal")
        now = datetime.now().strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{now}] {msg}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        
    def process_queues(self):
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log(msg)
            except queue.Empty:
                break
                
        latest_data = None
        while not self.update_queue.empty():
            try:
                latest_data = self.update_queue.get_nowait()
            except queue.Empty:
                break
                
        if latest_data:
            self._update_dashboard(latest_data)

        if self.worker_running and self.worker is not None and not self.worker.is_alive():
            self.worker_running = False
            self.btn_start.configure(state="normal", text="启动")
            self.source_selector.configure(state="normal")
            self.lbl_sys_status.configure(text="已停止", text_color="orange")
            
        self.after(100, self.process_queues)

    def _update_dashboard(self, data: dict):
        if data["status"] == "PAUSE":
            source_label = data.get("market_source_label", self.market_source_var.get())
            self.lbl_sys_status.configure(text=f"{source_label} | 非交易时段 ({data['win_name']})", text_color="yellow")
            return
            
        tick = data["tick"]
        strategy: CombinedStrategyV6 = data["strategy"]
        trade_record = data["trade_record"]
        calc = strategy.factor_calc
        
        current_price = tick["price"]
        
        snapshot = calc.last_snapshot
        if snapshot is not None:
            day_vwap_dev = snapshot.day_vwap_dev
            local_vwap_dev = snapshot.local_vwap_dev
            velocity = snapshot.velocity
            acceleration = snapshot.acceleration
            day_return = snapshot.day_return
        else:
            day_vwap_dev = current_price / calc.vwap - 1.0 if calc.vwap > 0 else 0.0
            local_vwap_dev = 0.0
            velocity = 0.0
            acceleration = 0.0
            day_return = current_price / calc.prev_close - 1.0 if calc.prev_close > 0 else 0.0

        source_label = data.get("market_source_label") or tick.get("market_source_label") or self.market_source_var.get()
        fallback_note = " | QMT->现有接口" if tick.get("market_source_fallback") else ""
        self.lbl_sys_status.configure(text=f"{source_label}{fallback_note} | {tick['server_time']}", text_color="gray")
        self.tick_chart.append(tick)
        self._render_tick_chart()
        
        price_color = "#D85B5B" if day_return > 0 else ("#2AA876" if day_return < 0 else "white")
        self.lbl_price.configure(text=f"{current_price:.2f}", text_color=price_color)
        self.lbl_pct.configure(text=f"{day_return*100:+.2f}%", text_color=price_color)
        
        pos_pct = strategy.current_position_pct(current_price) * 100
        equity = strategy.total_asset(current_price)
        asset_base = float(getattr(strategy, "initial_capital", INITIAL_CAPITAL) or INITIAL_CAPITAL)
        pnl = equity - asset_base
        pnl_pct = pnl / asset_base * 100 if asset_base > 0 else 0.0
        
        pnl_color = "#D85B5B" if pnl > 0 else "#2AA876"
        
        self.lbl_pos_details.configure(text=f"持股 {strategy.shares:,} | 仓位 {pos_pct:.1f}% | 现金 {strategy.cash:,.0f}")
        self.lbl_pnl.configure(text=f"总权益: {equity:,.0f}  |  总盈亏: {pnl:+,.0f} ({pnl_pct:+.2f}%)", text_color=pnl_color)
        self.lbl_position_bar.configure(text=f"实际仓位 {pos_pct:.1f}% / 目标 {strategy.target_pct*100:.1f}%")
        self.pb_position.set(max(0.0, min(1.0, pos_pct / 100.0)))
        self.pb_position.configure(progress_color="#D85B5B" if pos_pct >= strategy.target_pct * 100 else "#3F5F7D")

        mode_text = strategy.mode.value
        mode_color = "#D85B5B" if strategy.mode == PositionMode.ATTACK else ("#2AA876" if strategy.mode == PositionMode.DEFENSE else "white")
        self.lbl_mode.configure(text=f"模式 {mode_text} | 目标 {strategy.target_pct*100:.1f}%", text_color=mode_color)
        last_trade_dt = getattr(strategy, "last_trade_dt", None)
        last_trade_text = last_trade_dt.strftime("%H:%M:%S") if isinstance(last_trade_dt, datetime) else "--"
        self.lbl_trade_counter.configure(
            text=f"今日交易 {getattr(strategy, 'day_trade_count', 0)} / {getattr(strategy, 'max_day_trades', 0)} | 上次 {last_trade_text}"
        )
        local_cycle = getattr(strategy, "local_t_cycle", None) or "none"
        local_base = getattr(strategy, "local_base_target_pct", None)
        local_entry = getattr(strategy, "local_t_entry_price", None)
        local_shares = getattr(strategy, "local_t_entry_shares", 0)
        local_base_text = f"{local_base*100:.1f}%" if local_base is not None else "--"
        local_entry_text = f"{local_entry:.2f}" if local_entry else "--"
        self.lbl_local_cycle.configure(
            text=f"本地T {local_cycle} | base {local_base_text} | entry {local_entry_text} | {local_shares}股"
        )

        decision = getattr(strategy, "regime_decision", None)
        if decision is not None:
            self.lbl_regime.configure(
                text=f"{decision.regime.value} | score {decision.regime_score:.2f} | {decision.detail[:46]}"
            )
            self.lbl_band.configure(
                text=f"允许仓位 {decision.target_floor_pct*100:.0f}-{decision.target_ceiling_pct*100:.0f}% | floor {strategy._active_regime_floor_pct()*100:.0f}%"
            )
        else:
            self.lbl_regime.configure(text="Regime --")
            self.lbl_band.configure(text=f"允许仓位 {strategy.floor_pct*100:.0f}-{strategy.ceil_pct*100:.0f}%")
        tick_vol = float(tick.get("tick_vol", 0.0) or 0.0)
        self.lbl_feed_detail.configure(text=f"行情源 {source_label}{fallback_note}")
        self.lbl_tick_detail.configure(text=f"Tick {tick.get('server_time', '--')} | 成交量 {tick_vol:,.0f} | 价 {current_price:.2f}")

        cross_buy = self._safe_score(strategy, "_score_cross_buy", snapshot)
        cross_sell = self._safe_score(strategy, "_score_cross_sell", snapshot)
        local_trim = self._safe_score(strategy, "_score_local_trim", snapshot)
        local_cover = self._safe_score(strategy, "_score_local_cover", snapshot)
        main_flow = self._safe_score(strategy, "_score_main_flow_distribution", snapshot)
        buy_timing = self._safe_score(strategy, "_score_buy_timing", snapshot)
        sell_timing = self._safe_score(strategy, "_score_sell_timing", snapshot)
        self._set_score("cross_buy", cross_buy, getattr(strategy, "cross_enter_score", 0.25))
        self._set_score("cross_sell", cross_sell, getattr(strategy, "cross_enter_score", 0.25))
        self._set_score("local_trim", local_trim, getattr(strategy, "local_enter_score", LOCAL_T0_ENTER_SCORE))
        self._set_score("local_cover", local_cover, getattr(strategy, "local_cover_enter_score", 0.85))
        self._set_score("main_flow", main_flow, getattr(strategy, "main_flow_guard_score", 0.50))
        self._set_score("buy_timing", buy_timing, 0.50)
        self._set_score("sell_timing", sell_timing, 0.50)
        self.lbl_score_note.configure(text="进度条越长，模块判断越接近真实动作")

        if snapshot is not None:
            factor_raw_values = {
                "day_return": snapshot.day_return,
                "day_vwap_dev": snapshot.day_vwap_dev,
                "local_vwap_dev": snapshot.local_vwap_dev,
                "velocity": snapshot.velocity,
                "acceleration": snapshot.acceleration,
                "vol_mom": snapshot.vol_mom,
                "range_position": snapshot.range_position,
                "pullback_from_high": snapshot.pullback_from_high,
                "below_vwap_ratio": snapshot.below_vwap_ratio,
                "orderbook_imbalance": snapshot.orderbook_imbalance,
            }
            factor_values = {
                "day_return": self._pct(snapshot.day_return),
                "day_vwap_dev": self._pct(snapshot.day_vwap_dev, 3),
                "local_vwap_dev": self._pct(snapshot.local_vwap_dev, 3),
                "velocity": self._pct(snapshot.velocity, 3),
                "acceleration": self._pct(snapshot.acceleration, 4),
                "vol_mom": self._num(snapshot.vol_mom, 2) + "x",
                "range_position": self._pct(snapshot.range_position, 1, signed=False),
                "pullback_from_high": self._pct(snapshot.pullback_from_high, 2),
                "below_vwap_ratio": self._pct(snapshot.below_vwap_ratio, 1, signed=False),
                "orderbook_imbalance": self._pct(snapshot.orderbook_imbalance, 1),
            }
        else:
            factor_raw_values = {key: None for key in self.factor_labels}
            factor_values = {key: "--" for key in self.factor_labels}
        self._update_factor_rows(factor_values, factor_raw_values)

        if trade_record:
            if trade_record.side == "BUY":
                self.lbl_signal.configure(text=f"BUY | {trade_record.reason}", text_color="white", fg_color="#D85B5B")
            elif trade_record.side == "SELL":
                self.lbl_signal.configure(text=f"SELL | {trade_record.reason}", text_color="white", fg_color="#2AA876")
        else:
            min_buy_cash = current_price * 100 * (1.0 + strategy.commission_rate)
            if strategy.target_pct >= strategy.ceil_pct - 1e-6 or strategy.cash < min_buy_cash:
                self.lbl_signal.configure(text="HOLD | 满仓侧重卖出信号", text_color="gray", fg_color="#333333")
            else:
                leading = max(
                    ("cross_buy", cross_buy),
                    ("cross_sell", cross_sell),
                    ("local_trim", local_trim),
                    ("local_cover", local_cover),
                    ("main_flow", main_flow),
                    key=lambda item: item[1],
                )
                self.lbl_signal.configure(
                    text=f"HOLD | {self._leading_signal_text(leading[0])}",
                    text_color="gray",
                    fg_color="#333333",
                )
            
        asks = [
            (tick.get("sp5", 0.0), tick.get("sv5", 0)), (tick.get("sp4", 0.0), tick.get("sv4", 0)),
            (tick.get("sp3", 0.0), tick.get("sv3", 0)), (tick.get("sp2", 0.0), tick.get("sv2", 0)),
            (tick.get("sp1", 0.0), tick.get("sv1", 0))
        ]
        bids = [
            (tick.get("bp1", 0.0), tick.get("bv1", 0)), (tick.get("bp2", 0.0), tick.get("bv2", 0)),
            (tick.get("bp3", 0.0), tick.get("bv3", 0)), (tick.get("bp4", 0.0), tick.get("bv4", 0)),
            (tick.get("bp5", 0.0), tick.get("bv5", 0))
        ]
        
        for i, (p, v) in enumerate(asks):
            self.ob_labels[i].configure(text=f"Ask{5-i}  {float(p or 0):>7.2f}  {int(float(v or 0)):>8d}")
            
        for i, (p, v) in enumerate(bids):
            self.ob_labels[i+5].configure(text=f"Bid{i+1}  {float(p or 0):>7.2f}  {int(float(v or 0)):>8d}")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
