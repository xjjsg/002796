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
import customtkinter as ctk

from .config import (
    INITIAL_CASH, INITIAL_SHARES, INITIAL_TARGET_PCT, INITIAL_CAPITAL,
    FETCH_INTERVAL, STATE_SAVE_INTERVAL, DATA_DIR, STATE_FILE, TRADE_LOG_FILE,
    BACKTEST_TRADE_LOG_FILE, LOCAL_T0_ENTER_SCORE,
    SYMBOL_CODE, SYMBOL_NAME, is_trading_time, get_next_window, seconds_until
)
from .state_store import StrategyStateStore
from .tick_writer import TickDataWriter
from .fetcher import TencentFetcher
from .strategy_v6 import CombinedStrategyV6
from .position import PositionMode
from .data_quality import RealtimeDataQualityMonitor, has_critical_issue

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


def worker_thread(update_queue: queue.Queue, log_queue: queue.Queue):
    async def _async_main():
        if not os.path.exists(BACKTEST_TRADE_LOG_FILE):
            log_queue.put(f"[!] 缺少回测仓位流水，GUI 不启动: {os.path.abspath(BACKTEST_TRADE_LOG_FILE)}")
            log_queue.put("[!] 请先运行 python run_backtest.py 生成 2026-01-05 起步的 100 万模拟账户流水")
            return

        fetcher = TencentFetcher(SYMBOL_CODE)
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
        log_queue.put(f"局部 T 阈值: {LOCAL_T0_ENTER_SCORE:.2f}")
        log_queue.put(
            f"当前持仓: {strategy.shares} 股 | 现金 {strategy.cash:,.2f} | "
            f"资产基准 {strategy.initial_capital:,.2f}"
        )

        try:
            import aiohttp
        except ImportError:
            log_queue.put("[!] 缺少 aiohttp 依赖")
            return

        log_queue.put("正在测试数据连接...")
        async with aiohttp.ClientSession() as session:
            test_tick = await fetcher.fetch(session)
            if test_tick:
                log_queue.put(f"[OK] 连接成功，当前价 {test_tick['price']:.2f}")
                fetcher.last_server_ts = None
            else:
                log_queue.put("[!] 首次连接未返回新行情")
                fetcher.last_server_ts = None
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
                            }
                        )
                        await asyncio.sleep(min(30, max(1, wait_s - 5)))
                        continue

                    start_ts = time.time()
                    tick = await fetcher.fetch(session)
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
                        }
                    )

                    elapsed = time.time() - start_ts
                    await asyncio.sleep(max(0, FETCH_INTERVAL - elapsed))

                except Exception as exc:
                    consecutive_errors += 1
                    log_queue.put(f"[ERROR] 主循环错误 #{consecutive_errors}: {exc}")
                    import traceback

                    traceback.print_exc()
                    await asyncio.sleep(10 if consecutive_errors > 5 else 2)

    asyncio.run(_async_main())


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("002796.SZ 世嘉科技 - 实时策略监控终端 (V6)")
        self.geometry("1100x750")
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.update_queue = queue.Queue()
        self.log_queue = queue.Queue()
        
        self._setup_ui()
        
        self.worker = threading.Thread(target=worker_thread, args=(self.update_queue, self.log_queue), daemon=True)
        self.worker.start()
        
        self.after(50, self.process_queues)
        
    def _setup_ui(self):
        self.grid_columnconfigure(0, weight=6)
        self.grid_columnconfigure(1, weight=4)
        self.grid_rowconfigure(0, weight=2)
        self.grid_rowconfigure(1, weight=5)
        self.grid_rowconfigure(2, weight=3)
        
        # --- Header ---
        self.frame_top = ctk.CTkFrame(self, fg_color="#1E1E1E")
        self.frame_top.grid(row=0, column=0, columnspan=2, padx=10, pady=10, sticky="nsew")
        
        self.lbl_title = ctk.CTkLabel(self.frame_top, text="世嘉科技 (002796.SZ)", font=ctk.CTkFont(size=28, weight="bold"))
        self.lbl_title.pack(side="left", padx=20)
        
        self.lbl_price = ctk.CTkLabel(self.frame_top, text="--.--", font=ctk.CTkFont(size=40, weight="bold"), text_color="white")
        self.lbl_price.pack(side="left", padx=20)
        
        self.lbl_pct = ctk.CTkLabel(self.frame_top, text="+0.00%", font=ctk.CTkFont(size=20))
        self.lbl_pct.pack(side="left", padx=10)
        
        self.lbl_sys_status = ctk.CTkLabel(self.frame_top, text="启动中...", font=ctk.CTkFont(size=16), text_color="gray")
        self.lbl_sys_status.pack(side="right", padx=20)
        
        # --- Left Panel ---
        self.frame_left = ctk.CTkFrame(self)
        self.frame_left.grid(row=1, column=0, padx=(10, 5), pady=(0, 10), sticky="nsew")
        
        lbl_pos_title = ctk.CTkLabel(self.frame_left, text="持仓信息 (Holdings)", font=ctk.CTkFont(size=18, weight="bold"))
        lbl_pos_title.grid(row=0, column=0, padx=15, pady=(15, 5), sticky="w")
        
        self.lbl_pos_details = ctk.CTkLabel(self.frame_left, text="持股: -- 股 |  仓位: --  |  现金: --", font=ctk.CTkFont(size=14))
        self.lbl_pos_details.grid(row=1, column=0, padx=15, pady=5, sticky="w")
        
        self.lbl_pnl = ctk.CTkLabel(self.frame_left, text="总盈亏: -- (--%)", font=ctk.CTkFont(size=16))
        self.lbl_pnl.grid(row=2, column=0, padx=15, pady=5, sticky="w")
        
        self.lbl_mode = ctk.CTkLabel(self.frame_left, text="当前模式: --", font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_mode.grid(row=3, column=0, padx=15, pady=5, sticky="w")
        
        ctk.CTkFrame(self.frame_left, height=2, fg_color="#333333").grid(row=4, column=0, sticky="ew", padx=10, pady=15)
        
        lbl_fac_title = ctk.CTkLabel(self.frame_left, text="策略高阶因子 (V6 Alpha)", font=ctk.CTkFont(size=18, weight="bold"))
        lbl_fac_title.grid(row=5, column=0, padx=15, pady=(5, 10), sticky="w")
        
        self.lbl_day_vwap = ctk.CTkLabel(self.frame_left, text="日 VWAP 偏离: --%", font=ctk.CTkFont(size=14))
        self.lbl_day_vwap.grid(row=6, column=0, padx=15, pady=2, sticky="w")
        self.pb_day_vwap = ctk.CTkProgressBar(self.frame_left, width=300)
        self.pb_day_vwap.grid(row=7, column=0, padx=15, pady=(0, 10), sticky="w")
        self.pb_day_vwap.set(0.5)
        
        self.lbl_local_vwap = ctk.CTkLabel(self.frame_left, text="局部 VWAP 偏离 (30m): --%", font=ctk.CTkFont(size=14))
        self.lbl_local_vwap.grid(row=8, column=0, padx=15, pady=2, sticky="w")
        self.pb_local_vwap = ctk.CTkProgressBar(self.frame_left, width=300)
        self.pb_local_vwap.grid(row=9, column=0, padx=15, pady=(0, 10), sticky="w")
        self.pb_local_vwap.set(0.5)
        
        self.lbl_vel = ctk.CTkLabel(self.frame_left, text="动量 (Velocity): --%", font=ctk.CTkFont(size=14))
        self.lbl_vel.grid(row=10, column=0, padx=15, pady=2, sticky="w")
        
        self.lbl_acc = ctk.CTkLabel(self.frame_left, text="加速度 (Acceleration): --%", font=ctk.CTkFont(size=14))
        self.lbl_acc.grid(row=11, column=0, padx=15, pady=2, sticky="w")
        
        self.lbl_signal = ctk.CTkLabel(self.frame_left, text="Waiting for signal...", font=ctk.CTkFont(size=16, weight="bold"), fg_color="#333333", corner_radius=5)
        self.lbl_signal.grid(row=12, column=0, padx=15, pady=20, sticky="ew")
        
        # --- Right Panel ---
        self.frame_right = ctk.CTkFrame(self)
        self.frame_right.grid(row=1, column=1, padx=(5, 10), pady=(0, 10), sticky="nsew")
        
        lbl_ob_title = ctk.CTkLabel(self.frame_right, text="买卖五档盘口", font=ctk.CTkFont(size=18, weight="bold"))
        lbl_ob_title.pack(pady=(15, 10))
        
        self.ob_labels = []
        for i in range(5, 0, -1):
            lbl = ctk.CTkLabel(self.frame_right, text=f"Ask{i}  --  --", font=ctk.CTkFont(family="Consolas", size=14), text_color="#00FF7F")
            lbl.pack(anchor="w", padx=30, pady=2)
            self.ob_labels.append(lbl)
            
        ctk.CTkFrame(self.frame_right, height=2, fg_color="#333333").pack(fill="x", padx=20, pady=10)
        
        for i in range(1, 6):
            lbl = ctk.CTkLabel(self.frame_right, text=f"Bid{i}  --  --", font=ctk.CTkFont(family="Consolas", size=14), text_color="#FF4500")
            lbl.pack(anchor="w", padx=30, pady=2)
            self.ob_labels.append(lbl)
            
        # --- Bottom Panel ---
        self.frame_bottom = ctk.CTkFrame(self)
        self.frame_bottom.grid(row=2, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="nsew")
        
        self.txt_log = ctk.CTkTextbox(self.frame_bottom, font=ctk.CTkFont(family="Consolas", size=13), fg_color="#121212", text_color="#A0A0A0")
        self.txt_log.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt_log.insert("end", ">>> 系统启动中...\n")
        self.txt_log.configure(state="disabled")

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
            
        self.after(100, self.process_queues)

    def _update_dashboard(self, data: dict):
        if data["status"] == "PAUSE":
            self.lbl_sys_status.configure(text=f"非交易时段 ({data['win_name']})", text_color="yellow")
            return
            
        tick = data["tick"]
        strategy: CombinedStrategyV6 = data["strategy"]
        trade_record = data["trade_record"]
        calc = strategy.factor_calc
        
        current_price = tick["price"]
        
        # Use the exact factor snapshot produced by the strategy engine.
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

        # Header
        self.lbl_sys_status.configure(text=f"刷新时间: {tick['server_time']}", text_color="gray")
        
        price_color = "#FF4500" if day_return > 0 else ("#00FF7F" if day_return < 0 else "white")
        self.lbl_price.configure(text=f"{current_price:.2f}", text_color=price_color)
        self.lbl_pct.configure(text=f"{day_return*100:+.2f}%", text_color=price_color)
        
        # Position Info
        pos_pct = strategy.current_position_pct(current_price) * 100
        equity = strategy.total_asset(current_price)
        asset_base = float(getattr(strategy, "initial_capital", INITIAL_CAPITAL) or INITIAL_CAPITAL)
        pnl = equity - asset_base
        pnl_pct = pnl / asset_base * 100 if asset_base > 0 else 0.0
        
        pnl_color = "#FF4500" if pnl > 0 else "#00FF7F"
        
        self.lbl_pos_details.configure(text=f"持股: {strategy.shares} 股 |  仓位: {pos_pct:.1f}% (目标 {strategy.target_pct*100:.0f}%)  |  现金: {strategy.cash:,.0f}")
        self.lbl_pnl.configure(text=f"总权益: {equity:,.0f}  |  总盈亏: {pnl:+,.0f} ({pnl_pct:+.2f}%)", text_color=pnl_color)
        
        mode_text = strategy.mode.value
        mode_color = "#FF4500" if strategy.mode == PositionMode.ATTACK else ("#00FF7F" if strategy.mode == PositionMode.DEFENSE else "white")
        self.lbl_mode.configure(text=f"当前模式: {mode_text}", text_color=mode_color)
        
        # Factors
        # Day VWAP dev [-3%, 3%] mapping to [0, 1]
        d_dev_pct = day_vwap_dev * 100
        d_dev_ratio = max(0, min(1, (d_dev_pct + 3) / 6))
        d_dev_color = "#FF4500" if d_dev_pct > 1.8 else ("#00FF7F" if d_dev_pct < -0.4 else "#1f538d")
        self.lbl_day_vwap.configure(text=f"日 VWAP 偏离率: {d_dev_pct:+.3f}%")
        self.pb_day_vwap.set(d_dev_ratio)
        self.pb_day_vwap.configure(progress_color=d_dev_color)
        
        # Local VWAP dev [-2%, 2%] mapping to [0, 1]
        l_dev_pct = local_vwap_dev * 100
        l_dev_ratio = max(0, min(1, (l_dev_pct + 2) / 4))
        l_dev_color = "#FF4500" if l_dev_pct > 0.6 else ("#00FF7F" if l_dev_pct < -0.4 else "#1f538d")
        self.lbl_local_vwap.configure(text=f"局部 VWAP 偏离 (30m): {l_dev_pct:+.3f}%")
        self.pb_local_vwap.set(l_dev_ratio)
        self.pb_local_vwap.configure(progress_color=l_dev_color)
        
        vel_pct = velocity * 100
        vel_color = "#FF4500" if vel_pct > 0 else "#00FF7F"
        self.lbl_vel.configure(text=f"动量 (Velocity): {vel_pct:+.3f}%", text_color=vel_color)
        
        acc_pct = acceleration * 100
        acc_color = "#FF4500" if acc_pct > 0 else ("#00FF7F" if acc_pct < 0 else "white")
        self.lbl_acc.configure(text=f"加速度 (Acceleration): {acc_pct:+.4f}%", text_color=acc_color)
        
        # Signal
        if trade_record:
            if trade_record.side == "BUY":
                self.lbl_signal.configure(text=f"BUY signal ({trade_record.reason})", text_color="white", fg_color="#28a745")
            elif trade_record.side == "SELL":
                self.lbl_signal.configure(text=f"SELL signal ({trade_record.reason})", text_color="white", fg_color="#dc3545")
        else:
            signal_text = self.lbl_signal.cget("text")
            if signal_text.startswith("Waiting") or "Watching" in signal_text:
                min_buy_cash = current_price * 100 * (1.0 + strategy.commission_rate)
                if strategy.target_pct >= strategy.ceil_pct - 1e-6 or strategy.cash < min_buy_cash:
                    self.lbl_signal.configure(text="Full-position watch | waiting for sell signal", text_color="gray", fg_color="#333333")
                else:
                    self.lbl_signal.configure(text=f"Watching | macro-buy distance: {abs(d_dev_pct - (-0.4)):.2f}%", text_color="gray", fg_color="#333333")
            
        # Orderbook
        asks = [
            (tick["sp5"], tick["sv5"]), (tick["sp4"], tick["sv4"]), (tick["sp3"], tick["sv3"]), (tick["sp2"], tick["sv2"]), (tick["sp1"], tick["sv1"])
        ]
        bids = [
            (tick["bp1"], tick["bv1"]), (tick["bp2"], tick["bv2"]), (tick["bp3"], tick["bv3"]), (tick["bp4"], tick["bv4"]), (tick["bp5"], tick["bv5"])
        ]
        
        for i, (p, v) in enumerate(asks):
            self.ob_labels[i].configure(text=f"Ask{5-i}  {p:>7.2f}  {v:>8d}")
            
        for i, (p, v) in enumerate(bids):
            self.ob_labels[i+5].configure(text=f"Bid{i+1}  {p:>7.2f}  {v:>8d}")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
