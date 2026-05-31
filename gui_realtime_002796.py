# -*- coding: utf-8 -*-
"""
世嘉科技 (002796.SZ) 实时策略监控系统 - GUI 可视化版
Realtime monitor. Strategy engine: combined_strategy_v5.py
"""
import sys
import os
import time
import asyncio
import threading
import queue
import csv
from datetime import datetime, timedelta
import customtkinter as ctk

from combined_strategy_v5 import CombinedStrategyV5
from strategy_core import PositionMode

SYMBOL_CODE = "sz002796"
SYMBOL_NAME = "世嘉科技"
DATA_DIR = os.path.join("data", "sz002796")
FETCH_INTERVAL = 3.0  # 3 seconds
LOCAL_T0_ENTER_SCORE = 0.70

# 真实持仓参数
BUY_1_PRICE = 49.95
BUY_1_SHARES = 1700
BUY_2_PRICE = 50.60
BUY_2_SHARES = 2000
INITIAL_SHARES = BUY_1_SHARES + BUY_2_SHARES
COMMISSION_RATE = 0.0001
INITIAL_COST = (
    BUY_1_SHARES * BUY_1_PRICE * (1.0 + COMMISSION_RATE)
    + BUY_2_SHARES * BUY_2_PRICE * (1.0 + COMMISSION_RATE)
)

os.makedirs(DATA_DIR, exist_ok=True)

class TickDataWriter:
    def __init__(self, data_dir: str, symbol: str):
        self.data_dir = data_dir
        self.symbol = symbol
        self.current_date_str = ""
        self.file = None
        self.csv_writer = None
        
    def _get_filename(self, date_str: str) -> str:
        return os.path.join(self.data_dir, f"{self.symbol}-{date_str}.csv")
        
    def write(self, tick: dict, signal: str = "HOLD"):
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        
        if self.current_date_str != date_str:
            if self.file:
                self.file.close()
            self.current_date_str = date_str
            filepath = self._get_filename(date_str)
            file_exists = os.path.exists(filepath)
            self.file = open(filepath, 'a', newline='', encoding='utf-8')
            
            self.header = [
                "local_time_ms", "server_time", "price", "open", "high", "low", "prev_close",
                "cum_volume", "cum_amount", "bp1", "bv1", "bp2", "bv2", "bp3", "bv3", "bp4", "bv4", "bp5", "bv5",
                "sp1", "sv1", "sp2", "sv2", "sp3", "sv3", "sp4", "sv4", "sp5", "sv5", "signal"
            ]
            self.csv_writer = csv.DictWriter(self.file, fieldnames=self.header, extrasaction='ignore')
            if not file_exists:
                self.csv_writer.writeheader()
                
        row = {
            "local_time_ms": int(time.time() * 1000),
            "server_time": tick.get("server_time", ""),
            "price": tick.get("price", ""),
            "open": tick.get("open", ""),
            "high": tick.get("high", ""),
            "low": tick.get("low", ""),
            "prev_close": tick.get("prev_close", ""),
            "cum_volume": tick.get("cum_volume", ""),
            "cum_amount": tick.get("cum_amount", ""),
            "signal": signal
        }
        
        for k in ["bp1", "bv1", "bp2", "bv2", "bp3", "bv3", "bp4", "bv4", "bp5", "bv5",
                  "sp1", "sv1", "sp2", "sv2", "sp3", "sv3", "sp4", "sv4", "sp5", "sv5"]:
            row[k] = tick.get(k, "")
            
        self.csv_writer.writerow(row)
        self.file.flush()

def is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    if (datetime.strptime("09:25:00", "%H:%M:%S").time() <= t <= datetime.strptime("11:30:00", "%H:%M:%S").time()) or \
       (datetime.strptime("13:00:00", "%H:%M:%S").time() <= t <= datetime.strptime("15:00:00", "%H:%M:%S").time()):
        return True
    return False

def get_next_window() -> tuple[datetime, str]:
    now = datetime.now()
    if now.weekday() >= 5:
        days_ahead = 7 - now.weekday()
        next_open = datetime(now.year, now.month, now.day, 9, 25, 0) + timedelta(days=days_ahead)
        return next_open, "下周一开盘"
        
    t = now.time()
    if t < datetime.strptime("09:25:00", "%H:%M:%S").time():
        return datetime(now.year, now.month, now.day, 9, 25, 0), "早盘集合竞价"
    elif datetime.strptime("11:30:00", "%H:%M:%S").time() < t < datetime.strptime("13:00:00", "%H:%M:%S").time():
        return datetime(now.year, now.month, now.day, 13, 0, 0), "午后开盘"
    else:
        days_ahead = 3 if now.weekday() == 4 else 1
        next_open = datetime(now.year, now.month, now.day, 9, 25, 0) + timedelta(days=days_ahead)
        return next_open, "明日开盘"

def seconds_until(dt: datetime) -> float:
    return (dt - datetime.now()).total_seconds()

class TencentFetcher:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.url = f"http://qt.gtimg.cn/q={symbol}"
        self.last_server_ts = None
        
    async def fetch(self, session) -> dict:
        try:
            async with session.get(self.url, timeout=5) as resp:
                text = await resp.text()
                if not text or len(text) < 50:
                    return None
                    
                parts = text.split("~")
                if len(parts) < 40:
                    return None
                    
                server_time_str = parts[30]
                if self.last_server_ts == server_time_str:
                    return None
                self.last_server_ts = server_time_str
                
                dt = datetime.strptime(server_time_str, "%Y%m%d%H%M%S")
                
                tick = {
                    "Time": dt,
                    "server_time": dt.strftime("%H:%M:%S"),
                    "price": float(parts[3]),
                    "prev_close": float(parts[4]),
                    "open": float(parts[5]),
                    "cum_volume": float(parts[6]) * 100,
                    "cum_amount": float(parts[37]) * 10000,
                    "high": float(parts[33]),
                    "low": float(parts[34]),
                }
                
                for i in range(5):
                    tick[f"bp{i+1}"] = float(parts[9 + i*2])
                    tick[f"bv{i+1}"] = int(parts[10 + i*2]) * 100
                    tick[f"sp{i+1}"] = float(parts[19 + i*2])
                    tick[f"sv{i+1}"] = int(parts[20 + i*2]) * 100
                    
                tick["Close"] = tick["price"]
                tick["Volume"] = tick["cum_volume"]
                tick["Amount"] = tick["cum_amount"]
                
                return tick
        except Exception as e:
            return None


def worker_thread(update_queue: queue.Queue, log_queue: queue.Queue):
    async def _async_main():
        fetcher = TencentFetcher(SYMBOL_CODE)
        writer = TickDataWriter(DATA_DIR, SYMBOL_CODE)
        
        strategy = CombinedStrategyV5(
            initial_capital=INITIAL_COST,
            local_enter_score=LOCAL_T0_ENTER_SCORE,
        )
        strategy.cash = 0.0
        strategy.shares = INITIAL_SHARES
        strategy.target_pct = 1.0
        strategy.mode = PositionMode.ATTACK
        
        log_queue.put(f"初始化完成 | 标的: {SYMBOL_CODE} {SYMBOL_NAME}")
        log_queue.put(f"数据存储: {os.path.abspath(DATA_DIR)}\\")
        log_queue.put(f"策略引擎: CombinedStrategyV5 (cross-day + local T)")
        log_queue.put(f"局部T阈值: {LOCAL_T0_ENTER_SCORE:.2f}")
        log_queue.put(f"接管持仓: {INITIAL_SHARES}股 (成本 ~{INITIAL_COST:,.2f}元)")
        
        try:
            import aiohttp
        except ImportError:
            log_queue.put("[!] 错误: 缺少 aiohttp 库")
            return
            
        log_queue.put("正在测试数据连接...")
        
        async with aiohttp.ClientSession() as session:
            test_tick = await fetcher.fetch(session)
            if test_tick:
                log_queue.put(f"[OK] 连接成功! 当前价: {test_tick['price']:.2f}")
                fetcher.last_server_ts = None
            else:
                log_queue.put("[!] 首次连接未返回新数据 (可能非交易时间或未更新)")
                fetcher.last_server_ts = None
                
            consecutive_errors = 0
            while True:
                try:
                    if not is_trading_time():
                        next_win, win_name = get_next_window()
                        if next_win:
                            wait_s = seconds_until(next_win)
                            if wait_s % 30 < FETCH_INTERVAL:  
                                log_queue.put(f"[PAUSE] 非交易时间, 等待 {win_name}")
                            update_queue.put({
                                "status": "PAUSE",
                                "win_name": win_name,
                                "wait_s": wait_s
                            })
                            await asyncio.sleep(min(30, max(1, wait_s - 5)))
                        else:
                            await asyncio.sleep(1)
                        continue
                        
                    start_ts = time.time()
                    
                    tick = await fetcher.fetch(session)
                    if tick is None:
                        elapsed = time.time() - start_ts
                        await asyncio.sleep(max(0, FETCH_INTERVAL - elapsed))
                        continue
                        
                    consecutive_errors = 0
                    
                    trade_record = strategy.on_tick(tick)
                    
                    signal_str = "HOLD"
                    if trade_record:
                        signal_str = trade_record.side
                        log_queue.put(f"[{trade_record.side}] @ {trade_record.price:.2f} | {trade_record.reason} | {trade_record.detail}")
                        
                    writer.write(tick, signal_str)
                    
                    update_queue.put({
                        "status": "RUNNING",
                        "tick": tick,
                        "strategy": strategy,
                        "trade_record": trade_record
                    })
                    
                    elapsed = time.time() - start_ts
                    await asyncio.sleep(max(0, FETCH_INTERVAL - elapsed))
                    
                except Exception as e:
                    consecutive_errors += 1
                    log_queue.put(f"[ERROR] 主循环错误 #{consecutive_errors}: {e}")
                    import traceback
                    traceback.print_exc()
                    if consecutive_errors > 5:
                        await asyncio.sleep(10)
                    else:
                        await asyncio.sleep(2)
                        
    asyncio.run(_async_main())


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("002796.SZ 世嘉科技 - 实时策略监控终端 (V5)")
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
        
        self.lbl_pos_details = ctk.CTkLabel(self.frame_left, text="持股: -- 股  |  仓位: --  |  现金: --", font=ctk.CTkFont(size=14))
        self.lbl_pos_details.grid(row=1, column=0, padx=15, pady=5, sticky="w")
        
        self.lbl_pnl = ctk.CTkLabel(self.frame_left, text="总盈亏: -- (--%)", font=ctk.CTkFont(size=16))
        self.lbl_pnl.grid(row=2, column=0, padx=15, pady=5, sticky="w")
        
        self.lbl_mode = ctk.CTkLabel(self.frame_left, text="当前模式: --", font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_mode.grid(row=3, column=0, padx=15, pady=5, sticky="w")
        
        ctk.CTkFrame(self.frame_left, height=2, fg_color="#333333").grid(row=4, column=0, sticky="ew", padx=10, pady=15)
        
        lbl_fac_title = ctk.CTkLabel(self.frame_left, text="策略高阶因子 (V5 Alpha)", font=ctk.CTkFont(size=18, weight="bold"))
        lbl_fac_title.grid(row=5, column=0, padx=15, pady=(5, 10), sticky="w")
        
        self.lbl_day_vwap = ctk.CTkLabel(self.frame_left, text="日 VWAP 乖离: --%", font=ctk.CTkFont(size=14))
        self.lbl_day_vwap.grid(row=6, column=0, padx=15, pady=2, sticky="w")
        self.pb_day_vwap = ctk.CTkProgressBar(self.frame_left, width=300)
        self.pb_day_vwap.grid(row=7, column=0, padx=15, pady=(0, 10), sticky="w")
        self.pb_day_vwap.set(0.5)
        
        self.lbl_local_vwap = ctk.CTkLabel(self.frame_left, text="局部 VWAP 乖离 (30m): --%", font=ctk.CTkFont(size=14))
        self.lbl_local_vwap.grid(row=8, column=0, padx=15, pady=2, sticky="w")
        self.pb_local_vwap = ctk.CTkProgressBar(self.frame_left, width=300)
        self.pb_local_vwap.grid(row=9, column=0, padx=15, pady=(0, 10), sticky="w")
        self.pb_local_vwap.set(0.5)
        
        self.lbl_vel = ctk.CTkLabel(self.frame_left, text="动量 (Velocity): --%", font=ctk.CTkFont(size=14))
        self.lbl_vel.grid(row=10, column=0, padx=15, pady=2, sticky="w")
        
        self.lbl_acc = ctk.CTkLabel(self.frame_left, text="加速度 (Acceleration): --%", font=ctk.CTkFont(size=14))
        self.lbl_acc.grid(row=11, column=0, padx=15, pady=2, sticky="w")
        
        self.lbl_signal = ctk.CTkLabel(self.frame_left, text="等待信号...", font=ctk.CTkFont(size=16, weight="bold"), fg_color="#333333", corner_radius=5)
        self.lbl_signal.grid(row=12, column=0, padx=15, pady=20, sticky="ew")
        
        # --- Right Panel ---
        self.frame_right = ctk.CTkFrame(self)
        self.frame_right.grid(row=1, column=1, padx=(5, 10), pady=(0, 10), sticky="nsew")
        
        lbl_ob_title = ctk.CTkLabel(self.frame_right, text="买卖五档盘口", font=ctk.CTkFont(size=18, weight="bold"))
        lbl_ob_title.pack(pady=(15, 10))
        
        self.ob_labels = []
        for i in range(5, 0, -1):
            lbl = ctk.CTkLabel(self.frame_right, text=f"卖{i}  --  --", font=ctk.CTkFont(family="Consolas", size=14), text_color="#00FF7F")
            lbl.pack(anchor="w", padx=30, pady=2)
            self.ob_labels.append(lbl)
            
        ctk.CTkFrame(self.frame_right, height=2, fg_color="#333333").pack(fill="x", padx=20, pady=10)
        
        for i in range(1, 6):
            lbl = ctk.CTkLabel(self.frame_right, text=f"买{i}  --  --", font=ctk.CTkFont(family="Consolas", size=14), text_color="#FF4500")
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
            self.lbl_sys_status.configure(text=f"非交易时间 ({data['win_name']})", text_color="yellow")
            return
            
        tick = data["tick"]
        strategy: CombinedStrategyV5 = data["strategy"]
        trade_record = data["trade_record"]
        calc = strategy.factor_calc
        
        current_price = tick["price"]
        
        # Calculate derived values from the strategy factor state.
        day_vwap_dev = current_price / calc.vwap - 1.0 if calc.vwap > 0 else 0.0
        
        local_amt = sum(calc.local_amt_history)
        local_vol = sum(calc.local_vol_history)
        local_vwap = local_amt / local_vol if local_vol > 0 else (sum(calc.local_price_history) / len(calc.local_price_history) if calc.local_price_history else current_price)
        local_vwap_dev = current_price / local_vwap - 1.0 if local_vwap > 0 else 0.0
        
        velocity = calc.vel_history[-1] if calc.vel_history else 0.0
        acceleration = (calc.vel_history[-1] - calc.vel_history[-6]) if len(calc.vel_history) >= 6 else 0.0
        day_return = current_price / calc.prev_close - 1.0 if calc.prev_close > 0 else 0.0

        # Header
        self.lbl_sys_status.configure(text=f"刷新时间: {tick['server_time']}", text_color="gray")
        
        price_color = "#FF4500" if day_return > 0 else ("#00FF7F" if day_return < 0 else "white")
        self.lbl_price.configure(text=f"{current_price:.2f}", text_color=price_color)
        self.lbl_pct.configure(text=f"{day_return*100:+.2f}%", text_color=price_color)
        
        # Position Info
        pos_pct = strategy.current_position_pct(current_price) * 100
        equity = strategy.total_asset(current_price)
        pnl = equity - INITIAL_COST
        pnl_pct = pnl / INITIAL_COST * 100
        
        pnl_color = "#FF4500" if pnl > 0 else "#00FF7F"
        
        self.lbl_pos_details.configure(text=f"持股: {strategy.shares} 股  |  仓位: {pos_pct:.1f}% (目标 {strategy.target_pct*100:.0f}%)  |  现金: {strategy.cash:,.0f}")
        self.lbl_pnl.configure(text=f"总权益: {equity:,.0f}  |  总盈亏: {pnl:+,.0f} ({pnl_pct:+.2f}%)", text_color=pnl_color)
        
        mode_text = strategy.mode.value
        mode_color = "#FF4500" if strategy.mode == PositionMode.ATTACK else ("#00FF7F" if strategy.mode == PositionMode.DEFENSE else "white")
        self.lbl_mode.configure(text=f"当前模式: {mode_text}", text_color=mode_color)
        
        # Factors
        # Day VWAP dev [-3%, 3%] mapping to [0, 1]
        d_dev_pct = day_vwap_dev * 100
        d_dev_ratio = max(0, min(1, (d_dev_pct + 3) / 6))
        d_dev_color = "#FF4500" if d_dev_pct > 1.8 else ("#00FF7F" if d_dev_pct < -0.4 else "#1f538d")
        self.lbl_day_vwap.configure(text=f"日 VWAP 乖离率: {d_dev_pct:+.3f}%")
        self.pb_day_vwap.set(d_dev_ratio)
        self.pb_day_vwap.configure(progress_color=d_dev_color)
        
        # Local VWAP dev [-2%, 2%] mapping to [0, 1]
        l_dev_pct = local_vwap_dev * 100
        l_dev_ratio = max(0, min(1, (l_dev_pct + 2) / 4))
        l_dev_color = "#FF4500" if l_dev_pct > 0.6 else ("#00FF7F" if l_dev_pct < -0.4 else "#1f538d")
        self.lbl_local_vwap.configure(text=f"局部 VWAP 乖离 (30m): {l_dev_pct:+.3f}%")
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
                self.lbl_signal.configure(text=f"▶ 买入信号 ({trade_record.reason})", text_color="white", fg_color="#28a745")
            elif trade_record.side == "SELL":
                self.lbl_signal.configure(text=f"▶ 卖出信号 ({trade_record.reason})", text_color="white", fg_color="#dc3545")
        else:
            signal_text = self.lbl_signal.cget("text")
            if signal_text.startswith("等待信号") or "观察中" in signal_text:
                min_buy_cash = current_price * 100 * (1.0 + strategy.commission_rate)
                if strategy.target_pct >= strategy.ceil_pct - 1e-6 or strategy.cash < min_buy_cash:
                    self.lbl_signal.configure(text="满仓观察 | 等待卖出或T信号", text_color="gray", fg_color="#333333")
                else:
                    self.lbl_signal.configure(text=f"观察中 | 距离宏观买入: {abs(d_dev_pct - (-0.4)):.2f}%", text_color="gray", fg_color="#333333")
            
        # Orderbook
        asks = [
            (tick["sp5"], tick["sv5"]), (tick["sp4"], tick["sv4"]), (tick["sp3"], tick["sv3"]), (tick["sp2"], tick["sv2"]), (tick["sp1"], tick["sv1"])
        ]
        bids = [
            (tick["bp1"], tick["bv1"]), (tick["bp2"], tick["bv2"]), (tick["bp3"], tick["bv3"]), (tick["bp4"], tick["bv4"]), (tick["bp5"], tick["bv5"])
        ]
        
        for i, (p, v) in enumerate(asks):
            self.ob_labels[i].configure(text=f"卖{5-i}  {p:>7.2f}  {v:>8d}")
            
        for i, (p, v) in enumerate(bids):
            self.ob_labels[i+5].configure(text=f"买{i+1}  {p:>7.2f}  {v:>8d}")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    
    app = App()
    app.mainloop()
