"""Shared realtime strategy worker used by desktop and web frontends."""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import traceback
from typing import Any

from .config import (
    BACKTEST_TRADE_LOG_FILE,
    DATA_DIR,
    FETCH_INTERVAL,
    INITIAL_CAPITAL,
    INITIAL_CASH,
    INITIAL_SHARES,
    INITIAL_TARGET_PCT,
    LOCAL_T0_ENTER_SCORE,
    STATE_FILE,
    STATE_SAVE_INTERVAL,
    SYMBOL_CODE,
    SYMBOL_NAME,
    TRADE_LOG_FILE,
    get_next_window,
    is_trading_time,
    seconds_until,
)
from .dashboard import build_dashboard_snapshot, build_pause_snapshot
from .data_quality import RealtimeDataQualityMonitor, has_critical_issue
from .realtime_sources import create_market_data_source, market_source_label, normalize_market_source_id
from .state_store import StrategyStateStore
from .strategy_v6 import CombinedStrategyV6
from .tick_writer import TickDataWriter


def _position_summary(shares: int, cash: float, price: float | None) -> str:
    if price and price > 0:
        asset = shares * price + cash
        pct = shares * price / asset if asset > 0 else 0.0
        return f"{shares} 股 | 现金 {cash:,.2f} | 最新价 {price:.2f} | 资产 {asset:,.2f} | 仓位 {pct * 100:.1f}%"
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
            + _position_summary(
                int(loaded_state.get("shares", 0) or 0),
                float(loaded_state.get("cash", 0.0) or 0.0),
                latest_price,
            )
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


async def _sleep_or_stop(stop_event: threading.Event | None, seconds: float) -> bool:
    seconds = max(0.0, float(seconds))
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    return await asyncio.to_thread(stop_event.wait, seconds)


def worker_thread(
    update_queue: queue.Queue,
    log_queue: queue.Queue,
    market_source_id: str = "tencent",
    stop_event: threading.Event | None = None,
) -> None:
    async def _async_main() -> None:
        if not os.path.exists(BACKTEST_TRADE_LOG_FILE):
            log_queue.put(f"[!] 缺少回测仓位流水，运行引擎不启动: {os.path.abspath(BACKTEST_TRADE_LOG_FILE)}")
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
                f"[STATE] 已恢复 {strategy.shares} 股 | 目标 {strategy.target_pct * 100:.1f}% | "
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
            while stop_event is None or not stop_event.is_set():
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
                                "dashboard": build_pause_snapshot(
                                    win_name=win_name,
                                    wait_s=wait_s,
                                    market_source=current_source_id(),
                                    market_source_label=current_source_label(),
                                    requested_market_source=selected_source_id,
                                ),
                            }
                        )
                        if await _sleep_or_stop(stop_event, min(30, max(1, wait_s - 5))):
                            break
                        continue

                    start_ts = time.time()
                    tick = await market_source.fetch()
                    flush_source_events()
                    if tick is None:
                        elapsed = time.time() - start_ts
                        if await _sleep_or_stop(stop_event, max(0, FETCH_INTERVAL - elapsed)):
                            break
                        continue

                    consecutive_errors = 0
                    dq_issues = quality_monitor.check(tick)
                    for issue in dq_issues:
                        log_queue.put(f"[DQ:{issue.severity.upper()}] {issue.message}")
                    if has_critical_issue(dq_issues):
                        writer.write(tick, "DQ_SKIP")
                        state_store.save(strategy, tick, reason="data_quality_skip")
                        elapsed = time.time() - start_ts
                        if await _sleep_or_stop(stop_event, max(0, FETCH_INTERVAL - elapsed)):
                            break
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
                            "dashboard": build_dashboard_snapshot(
                                tick,
                                strategy,
                                trade_record,
                                status="RUNNING",
                                market_source=current_source_id(),
                                market_source_label=current_source_label(),
                                requested_market_source=selected_source_id,
                            ),
                        }
                    )

                    elapsed = time.time() - start_ts
                    if await _sleep_or_stop(stop_event, max(0, FETCH_INTERVAL - elapsed)):
                        break

                except Exception as exc:
                    consecutive_errors += 1
                    flush_source_events()
                    log_queue.put(f"[ERROR] 主循环错误 #{consecutive_errors}: {exc}")
                    traceback.print_exc()
                    if await _sleep_or_stop(stop_event, 10 if consecutive_errors > 5 else 2):
                        break
        finally:
            try:
                state_store.save(strategy, reason="worker_stop")
            except Exception as exc:
                log_queue.put(f"[WARN] 停止时保存状态失败: {exc}")
            await market_source.close()
            log_queue.put("[STATE] 运行引擎已停止")

    asyncio.run(_async_main())
