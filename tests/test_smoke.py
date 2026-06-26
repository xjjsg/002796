"""Regression tests for the refactored V6 system.

These tests cover the behaviors that must not drift during cleanup: factor
windows, state replay, stale tick filtering, execution-cost rules, regime
guardrails, and independent backtest output generation.
"""
import csv
import asyncio
import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from sz002796.strategy_v6 import CombinedStrategyV6
from sz002796.config import INITIAL_CAPITAL
from sz002796.position import PositionMode, TradeRecord
from sz002796.state_store import StrategyStateStore
from sz002796.trade_records import TRADE_LOG_COLUMNS, read_trade_rows
from sz002796.tick_writer import TickDataWriter
from sz002796.market_data import load_market_data, row_to_tick
from sz002796.realtime_sources import (
    FallbackMarketDataSource,
    QmtMarketDataSource,
    RealtimeMarketSource,
    create_market_data_source,
    normalize_market_source_id,
    symbol_to_qmt_symbol,
)
from sz002796.regime import MarketRegime, MarketRegimeDecision, MarketRegimeEngine
from sz002796.backtest import (
    BENCHMARK_TARGET_PCT,
    INITIAL_STRATEGY_TARGET_PCT,
    V6BacktestExecutionStrategy,
    benchmark_all_in,
    benchmark_buy_and_hold,
    calculate_trade_costs,
    is_limit_blocked,
    run_backtest,
)
from sz002796.factors import FactorSnapshot, IntradayFactorCalc
from qmt.anti_overfit_validation import (
    LEGACY_COMPARE_VARIANTS,
    LEGACY_COMPARE_WINDOWS,
    MODULE_REASON_MAP,
    RunResult,
    VARIANTS,
    ValidationFold,
    build_parser,
    floor_refill_conflicts,
    _trim_incomplete_tail_days,
)


def make_tick(dt: datetime, price: float, idx: int) -> dict:
    return {
        "Time": dt,
        "Close": price,
        "price": price,
        "Volume": idx + 1,
        "Amount": (idx + 1) * price,
        "tick_vol": 1,
        "tick_amt": price,
        "prev_close": 10.0,
        "open": 10.0,
        "high": max(10.0, price),
        "low": min(10.0, price),
    }


def fixed_regime_decision(floor_pct: float = 0.40) -> MarketRegimeDecision:
    return MarketRegimeDecision(
        regime=MarketRegime.RANGE,
        tags=("test",),
        confidence=0.8,
        target_floor_pct=floor_pct,
        target_ceiling_pct=1.0,
        regime_score=0.0,
        detail="test",
        allow_cross_day=True,
        allow_local_t=True,
    )


def directional_regime_decision(regime: MarketRegime, floor_pct: float = 0.40) -> MarketRegimeDecision:
    if regime == MarketRegime.DOWNTREND:
        ceiling_pct = 0.60
        allow_cross_day = False
        allow_local_t = False
    elif regime == MarketRegime.UPTREND:
        ceiling_pct = 1.00
        allow_cross_day = False
        allow_local_t = False
        floor_pct = max(floor_pct, 0.95)
    else:
        ceiling_pct = 1.00
        allow_cross_day = True
        allow_local_t = True
    return MarketRegimeDecision(
        regime=regime,
        tags=("test",),
        confidence=0.8,
        target_floor_pct=floor_pct,
        target_ceiling_pct=ceiling_pct,
        regime_score=0.0,
        detail="test",
        allow_cross_day=allow_cross_day,
        allow_local_t=allow_local_t,
    )


class SmokeTests(unittest.TestCase):
    def test_realtime_source_selection_and_symbol_mapping(self):
        self.assertEqual(normalize_market_source_id("QMT"), "qmt")
        self.assertEqual(normalize_market_source_id("现有接口"), "tencent")
        self.assertEqual(symbol_to_qmt_symbol("sz002796"), "002796.SZ")
        self.assertEqual(create_market_data_source("现有接口", "sz002796").source_id, "tencent")

    def test_qmt_market_source_drains_to_latest_tick(self):
        class FakeFeed:
            def __init__(self):
                self.started = False
                self.stopped = False
                self.queue = [
                    {"Time": datetime(2026, 6, 10, 9, 30, 0), "server_time": "09:30:00", "price": 10.0},
                    {"Time": datetime(2026, 6, 10, 9, 30, 3), "server_time": "09:30:03", "price": 10.2},
                ]

            def start(self):
                self.started = True

            def wait_next(self, timeout=None):
                if self.queue:
                    return self.queue.pop(0)
                return None

            def stop(self):
                self.stopped = True

        async def run_source():
            feed = FakeFeed()
            source = QmtMarketDataSource("sz002796", feed=feed, queue_timeout_seconds=0)
            await source.start()
            tick = await source.fetch()
            await source.close()
            return feed, tick

        feed, tick = asyncio.run(run_source())

        self.assertTrue(feed.started)
        self.assertTrue(feed.stopped)
        self.assertEqual(tick["price"], 10.2)
        self.assertEqual(tick["market_source"], "qmt")
        self.assertEqual(tick["market_source_label"], "QMT")

    def test_qmt_market_source_falls_back_to_tencent_on_start_failure(self):
        class BrokenQmtSource(RealtimeMarketSource):
            source_id = "qmt"
            label = "QMT"

            async def start(self):
                raise RuntimeError("xtquant missing")

        class BackupSource(RealtimeMarketSource):
            source_id = "tencent"
            label = "现有接口"

            def __init__(self):
                self.started = False

            async def start(self):
                self.started = True

            async def fetch(self):
                return self._mark_tick(
                    {
                        "Time": datetime(2026, 6, 10, 9, 30, 3),
                        "server_time": "09:30:03",
                        "price": 10.3,
                    }
                )

        async def run_source():
            backup = BackupSource()
            source = FallbackMarketDataSource(BrokenQmtSource(), backup)
            await source.start()
            events = source.pop_status_events()
            tick = await source.fetch()
            return backup, events, tick

        backup, events, tick = asyncio.run(run_source())

        self.assertTrue(backup.started)
        self.assertIn("已自动切换到 现有接口", events[0])
        self.assertEqual(tick["market_source"], "tencent")
        self.assertTrue(tick["market_source_fallback"])
        self.assertEqual(tick["requested_market_source"], "qmt")

    def test_qmt_market_source_falls_back_to_tencent_on_fetch_exception(self):
        class BrokenQmtSource(RealtimeMarketSource):
            source_id = "qmt"
            label = "QMT"

            async def start(self):
                return None

            async def fetch(self):
                raise RuntimeError("qmt fetch failed")

        class BackupSource(RealtimeMarketSource):
            source_id = "tencent"
            label = "现有接口"

            async def start(self):
                return None

            async def fetch(self):
                return self._mark_tick(
                    {
                        "Time": datetime(2026, 6, 10, 9, 30, 6),
                        "server_time": "09:30:06",
                        "price": 10.4,
                    }
                )

        async def run_source():
            source = FallbackMarketDataSource(BrokenQmtSource(), BackupSource())
            await source.start()
            tick = await source.fetch()
            return source.pop_status_events(), tick

        events, tick = asyncio.run(run_source())

        self.assertIn("QMT 行情失败", events[0])
        self.assertEqual(tick["market_source"], "tencent")
        self.assertTrue(tick["market_source_fallback"])

    def test_qmt_market_source_falls_back_on_callback_error(self):
        class Stats:
            last_error = "callback broken"

        class FakeFeed:
            stats = Stats()

            def start(self):
                return 1

            def wait_next(self, timeout=None):
                return {
                    "Time": datetime(2026, 6, 10, 9, 30, 9),
                    "server_time": "09:30:09",
                    "price": 10.5,
                }

            def stop(self):
                return None

        class BackupSource(RealtimeMarketSource):
            source_id = "tencent"
            label = "现有接口"

            async def start(self):
                return None

            async def fetch(self):
                return self._mark_tick(
                    {
                        "Time": datetime(2026, 6, 10, 9, 30, 12),
                        "server_time": "09:30:12",
                        "price": 10.6,
                    }
                )

        async def run_source():
            primary = QmtMarketDataSource("sz002796", feed=FakeFeed(), queue_timeout_seconds=0)
            source = FallbackMarketDataSource(primary, BackupSource())
            await source.start()
            tick = await source.fetch()
            return source.pop_status_events(), tick

        events, tick = asyncio.run(run_source())

        self.assertIn("QMT 行情失败", events[0])
        self.assertEqual(tick["market_source"], "tencent")
        self.assertTrue(tick["market_source_fallback"])

    def test_qmt_market_source_falls_back_after_no_tick_timeout(self):
        now = {"value": 0.0}

        class EmptyQmtSource(RealtimeMarketSource):
            source_id = "qmt"
            label = "QMT"

            async def start(self):
                return None

            async def fetch(self):
                return None

        class BackupSource(RealtimeMarketSource):
            source_id = "tencent"
            label = "现有接口"

            async def start(self):
                return None

            async def fetch(self):
                return self._mark_tick(
                    {
                        "Time": datetime(2026, 6, 10, 9, 30, 15),
                        "server_time": "09:30:15",
                        "price": 10.7,
                    }
                )

        async def run_source():
            source = FallbackMarketDataSource(
                EmptyQmtSource(),
                BackupSource(),
                no_tick_timeout_seconds=30.0,
                clock=lambda: now["value"],
            )
            await source.start()
            self.assertIsNone(await source.fetch())
            now["value"] = 31.0
            tick = await source.fetch()
            return source.pop_status_events(), tick

        events, tick = asyncio.run(run_source())

        self.assertIn("无有效 tick", events[0])
        self.assertEqual(tick["market_source"], "tencent")
        self.assertTrue(tick["market_source_fallback"])

    def test_local_vwap_uses_time_window_for_minute_data(self):
        calc = IntradayFactorCalc(local_window=30)
        start = datetime(2026, 6, 1, 9, 30)
        snapshot = None
        for idx in range(40):
            price = 10.0 if idx < 10 else 20.0
            snapshot = calc.update(make_tick(start + timedelta(minutes=idx), price, idx), idx == 0)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot.local_vwap, 20.0, places=6)

    def test_local_vwap_uses_time_window_for_three_second_data(self):
        calc = IntradayFactorCalc(local_window=30)
        start = datetime(2026, 6, 1, 9, 30)
        snapshot = None
        for idx in range(800):
            elapsed = timedelta(seconds=idx * 3)
            price = 10.0 if elapsed < timedelta(minutes=10) else 20.0
            snapshot = calc.update(make_tick(start + elapsed, price, idx), idx == 0)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot.local_vwap, 20.0, places=6)

    def test_consecutive_vwap_duration_is_time_normalized(self):
        calc = IntradayFactorCalc(local_window=30)
        start = datetime(2026, 6, 1, 9, 30)
        snapshot = None
        for idx, price in enumerate([10.0, 11.0, 11.0, 11.0]):
            tick = make_tick(start + timedelta(minutes=idx), price, idx)
            tick["Volume"] = 0
            tick["Amount"] = 0
            snapshot = calc.update(tick, idx == 0)

        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot.consecutive_above_vwap, 3.0, places=6)
        self.assertAlmostEqual(snapshot.consecutive_below_vwap, 0.0, places=6)

    def test_intraday_range_ignores_external_high_low_fields(self):
        calc = IntradayFactorCalc(local_window=30)
        start = datetime(2026, 6, 1, 9, 30)
        first = make_tick(start, 10.0, 0)
        first["high"] = 99.0
        first["low"] = 1.0
        calc.update(first, True)

        second = make_tick(start + timedelta(minutes=1), 11.0, 1)
        second["high"] = 99.0
        second["low"] = 1.0
        snapshot = calc.update(second, False)

        self.assertAlmostEqual(snapshot.intraday_high, 11.0)
        self.assertAlmostEqual(snapshot.intraday_low, 10.0)
        self.assertAlmostEqual(snapshot.high_return, 0.10)
        self.assertAlmostEqual(snapshot.range_position, 1.0)

    def test_validation_attribution_knows_directional_local_t_reasons(self):
        self.assertEqual(MODULE_REASON_MAP["V6 local short entry"][0], "4_intraday_t")
        self.assertEqual(MODULE_REASON_MAP["V6 local short cover"][0], "4_intraday_t")
        self.assertEqual(MODULE_REASON_MAP["V6 local long entry"][0], "4_intraday_t")
        self.assertEqual(MODULE_REASON_MAP["V6 local long exit"][0], "4_intraday_t")

    def test_anti_overfit_variants_compare_current_to_legacy_t(self):
        self.assertIn("baseline", VARIANTS)
        self.assertIn("no_local_t", VARIANTS)
        self.assertEqual(VARIANTS["legacy_local_t"]["trend_local_t_mode"], "legacy")
        self.assertTrue(VARIANTS["legacy_refill_lock"]["protect_local_short_floor_refill"])
        self.assertNotIn("candidate_short_refill_lock", VARIANTS)
        default_variants = build_parser().get_default("variants")
        self.assertIn("legacy_local_t", default_variants)
        self.assertIn("legacy_refill_lock", default_variants)
        self.assertNotIn("candidate_short_refill_lock", default_variants)

    def test_legacy_compare_cli_is_lightweight_directional_vs_legacy(self):
        self.assertEqual(set(LEGACY_COMPARE_VARIANTS), {"baseline_directional", "legacy_local_t"})
        self.assertEqual(LEGACY_COMPARE_VARIANTS["legacy_local_t"]["trend_local_t_mode"], "legacy")
        self.assertIn("recent_fixed", LEGACY_COMPARE_WINDOWS)
        self.assertIn("rolling_latest_15", LEGACY_COMPARE_WINDOWS)
        args = build_parser().parse_args(["--legacy-compare-only"])
        self.assertTrue(args.legacy_compare_only)

    def test_anti_overfit_trims_only_trailing_incomplete_days(self):
        rows = []
        for i in range(99):
            rows.append(
                {
                    "date": "2026-06-17",
                    "dt": datetime(2026, 6, 17, 9, 30) + timedelta(minutes=i),
                    "price": 10.0,
                }
            )
        rows.append({"date": "2026-06-17", "dt": datetime(2026, 6, 17, 14, 55), "price": 10.0})
        rows.append({"date": "2026-06-18", "dt": datetime(2026, 6, 18, 9, 24, 57), "price": 10.0})
        rows.append({"date": "2026-06-18", "dt": datetime(2026, 6, 18, 9, 25, 0), "price": 10.0})

        trimmed, excluded = _trim_incomplete_tail_days(pd.DataFrame(rows))

        self.assertEqual(excluded, ["2026-06-18"])
        self.assertEqual(set(trimmed["date"]), {"2026-06-17"})

    def test_anti_overfit_keeps_complete_latest_day(self):
        rows = []
        for i in range(99):
            rows.append(
                {
                    "date": "2026-06-17",
                    "dt": datetime(2026, 6, 17, 9, 30) + timedelta(minutes=i),
                    "price": 10.0,
                }
            )
        rows.append({"date": "2026-06-17", "dt": datetime(2026, 6, 17, 14, 55), "price": 10.0})
        for i in range(99):
            rows.append(
                {
                    "date": "2026-06-18",
                    "dt": datetime(2026, 6, 18, 9, 30) + timedelta(minutes=i),
                    "price": 10.0,
                }
            )
        rows.append({"date": "2026-06-18", "dt": datetime(2026, 6, 18, 14, 55), "price": 10.0})

        trimmed, excluded = _trim_incomplete_tail_days(pd.DataFrame(rows))

        self.assertEqual(excluded, [])
        self.assertEqual(set(trimmed["date"]), {"2026-06-17", "2026-06-18"})

    def test_floor_refill_conflicts_counts_directional_short_entry(self):
        fold = ValidationFold("unit", "2026-06-01", "2026-06-01", "2026-06-01", "2026-06-01")
        result = RunResult(
            fold=fold,
            variant="unit",
            params={},
            slippage_ticks=1,
            equity=pd.DataFrame(),
            trades=pd.DataFrame(
                [
                    {
                        "time": "2026-06-01 10:00:00",
                        "date": "2026-06-01",
                        "reason": "V6 local short entry",
                        "amount": 1000.0,
                    },
                    {
                        "time": "2026-06-01 10:01:00",
                        "date": "2026-06-01",
                        "reason": "V6 floor refill",
                        "amount": 900.0,
                    },
                    {
                        "time": "2026-06-01 10:02:00",
                        "date": "2026-06-01",
                        "reason": "V6 local short cover",
                        "amount": 950.0,
                    },
                ]
            ),
            orderbook_fallback_count=0,
            limit_skip_count=0,
        )

        conflicts = floor_refill_conflicts(result)

        self.assertEqual(len(conflicts), 1)
        row = conflicts.iloc[0]
        self.assertEqual(int(row["local_short_entry_count"]), 1)
        self.assertEqual(int(row["open_short_then_floor_refill_count"]), 1)
        self.assertEqual(int(row["trim_then_floor_refill_count"]), 1)
        self.assertAlmostEqual(float(row["trim_then_floor_refill_amount"]), 900.0)

    def test_strategy_state_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        trade_path = tmp / "trades.csv"
        strategy = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        buy_costs = calculate_trade_costs("BUY", 48.2, 300)
        expected_cash = INITIAL_CAPITAL - buy_costs.buy_cash_required
        strategy.cash = expected_cash
        strategy.shares = 300
        strategy.target_pct = 0.82
        strategy.mode = PositionMode.ATTACK
        strategy.current_date = "2026-06-01"
        strategy.day_trade_count = 2
        strategy.last_trade_dt = datetime(2026, 6, 1, 14, 0)
        strategy.main_flow_guard_date = "2026-06-01"
        strategy.main_flow_guard_floor_pct = 0.40
        strategy.market_regime.update(make_tick(datetime(2026, 6, 1, 14, 0), 48.2, 1))
        strategy.trades.append(
            TradeRecord(
                datetime(2026, 6, 1, 14, 0),
                "BUY",
                48.2,
                300,
                300,
                expected_cash,
                0.82,
                PositionMode.ATTACK.value,
                "test",
                "detail",
            )
        )

        store = StrategyStateStore(str(state_path), str(trade_path), seed_trade_log_path=None)
        store.save(strategy, {"price": 48.2, "Time": datetime(2026, 6, 1, 14, 0)}, "test")
        store.append_trade(strategy.trades[-1], strategy=strategy, tick={"price": 48.2, "server_time": "14:00:00"})

        restored = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        loaded = store.load(restored)
        self.assertEqual(loaded["strategy_version"], "v6")
        self.assertEqual(restored.shares, 300)
        self.assertAlmostEqual(restored.cash, expected_cash)
        self.assertAlmostEqual(restored.target_pct, 0.82)
        self.assertEqual(restored.mode, PositionMode.ATTACK)
        self.assertEqual(restored.day_trade_count, 1)
        self.assertEqual(restored.main_flow_guard_date, "2026-06-01")
        self.assertAlmostEqual(restored.main_flow_guard_floor_pct, 0.40)
        self.assertIsNotNone(restored.market_regime.current_day)
        self.assertEqual(restored.market_regime.current_day.date, "2026-06-01")
        self.assertEqual(len(restored.trades), 1)
        self.assertTrue(trade_path.exists())
        header = trade_path.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
        self.assertEqual(header, TRADE_LOG_COLUMNS)
        rows = read_trade_rows(str(trade_path))
        self.assertEqual(rows[0]["source"], "runtime")
        self.assertEqual(rows[0]["timestamp"], "2026-06-01 14:00:00")
        self.assertEqual(loaded["position_replay"]["source"], "runtime")
        self.assertEqual(loaded["position_replay"]["replayed_count"], 1)

    def test_strategy_state_replays_trade_log_over_saved_cash_and_shares(self):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        trade_path = tmp / "trades.csv"
        store = StrategyStateStore(str(state_path), str(trade_path), seed_trade_log_path=None)

        drifted = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        drifted.cash = 1.0
        drifted.shares = 1
        drifted.target_pct = 0.01
        drifted.mode = PositionMode.DEFENSE
        store.save(drifted, reason="drifted_state")

        trade_path.write_text(
            "timestamp,side,price,shares,target_pct,mode,reason,detail\n"
            "2026-06-01T14:00:00,BUY,10.0,100,0.70,NEUTRAL,test,buy one lot\n",
            encoding="utf-8",
        )

        restored = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        loaded = store.load(restored)

        self.assertEqual(restored.shares, 100)
        self.assertAlmostEqual(restored.cash, 998995.0)
        self.assertAlmostEqual(restored.target_pct, 0.70)
        self.assertEqual(restored.mode, PositionMode.NEUTRAL)
        self.assertEqual(loaded["position_replay"]["source"], "runtime")
        self.assertEqual(loaded["position_replay"]["replayed_count"], 1)

    def test_strategy_state_can_seed_position_from_backtest_trades(self):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        runtime_trade_path = tmp / "runtime_trades.csv"
        backtest_trade_path = tmp / "backtest_trades.csv"
        backtest_trade_path.write_text(
            "time,side,price,shares,amount,commission,stamp_tax,cash_after,position_shares,asset,position_pct,reason,detail\n"
            "2026-01-05 10:34:00,BUY,28.35,24600,697410.0,69.741,0.0,302520.259,24600,999930.259,0.697459,initial 70% base position,seed=70pct\n"
            "2026-01-07 10:15:00,SELL,29.26,6200,181412.0,18.1412,90.706,483823.4118,18400,1022207.4118,0.526688,V6 cross-day reduce,test\n",
            encoding="utf-8",
        )

        store = StrategyStateStore(
            str(state_path),
            str(runtime_trade_path),
            seed_trade_log_path=str(backtest_trade_path),
            seed_cash=1_000_000.0,
            seed_shares=0,
            seed_target_pct=0.0,
        )
        restored = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        loaded = store.load(restored)

        self.assertEqual(restored.shares, 18400)
        self.assertAlmostEqual(restored.cash, 483823.4118)
        self.assertAlmostEqual(restored.target_pct, 0.526688)
        self.assertEqual(restored.mode, PositionMode.DEFENSE)
        self.assertEqual(loaded["position_replay"]["source"], "backtest")
        self.assertEqual(loaded["position_replay"]["seed_rows"], 2)
        self.assertEqual(loaded["position_replay"]["runtime_rows"], 0)

    def test_strategy_state_replays_unified_backtest_and_runtime_schema(self):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        runtime_trade_path = tmp / "runtime_trades.csv"
        backtest_trade_path = tmp / "backtest_trades.csv"
        with backtest_trade_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS)
            writer.writeheader()
            writer.writerow(
                {
                    "timestamp": "2026-01-05 10:34:00",
                    "source": "backtest",
                    "side": "BUY",
                    "price": 10.0,
                    "shares": 100,
                    "target_pct": 0.10,
                    "mode": "DEFENSE",
                    "reason": "seed",
                    "detail": "unified",
                }
            )
        with runtime_trade_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS)
            writer.writeheader()
            writer.writerow(
                {
                    "timestamp": "2026-01-05 14:00:00",
                    "source": "runtime",
                    "side": "SELL",
                    "price": 11.0,
                    "shares": 100,
                    "target_pct": 0.0,
                    "mode": "DEFENSE",
                    "reason": "runtime exit",
                    "detail": "unified",
                }
            )

        store = StrategyStateStore(
            str(state_path),
            str(runtime_trade_path),
            seed_trade_log_path=str(backtest_trade_path),
            seed_cash=1_000_000.0,
            seed_shares=0,
            seed_target_pct=0.0,
        )
        restored = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        loaded = store.load(restored)

        self.assertEqual(restored.shares, 0)
        self.assertAlmostEqual(restored.cash, 1_000_000.0 - 1005.0 + 1094.45)
        self.assertEqual(loaded["position_replay"]["source"], "backtest+runtime")
        self.assertEqual(loaded["position_replay"]["seed_rows"], 1)
        self.assertEqual(loaded["position_replay"]["runtime_rows"], 1)

    def test_mismatched_state_is_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        trade_path = tmp / "trades.csv"
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "symbol": "sz002796",
                    "initial_capital": 186133.6115,
                    "cash": 0.0,
                    "shares": 3700,
                    "target_pct": 1.0,
                    "mode": PositionMode.ATTACK.value,
                }
            ),
            encoding="utf-8",
        )

        strategy = CombinedStrategyV6(initial_capital=INITIAL_CAPITAL)
        store = StrategyStateStore(str(state_path), str(trade_path), seed_trade_log_path=None)
        loaded = store.load(strategy)

        self.assertIsNone(loaded)
        self.assertIsNotNone(store.ignored_state)
        self.assertEqual(strategy.shares, 0)
        self.assertAlmostEqual(strategy.cash, INITIAL_CAPITAL)

    def test_tick_writer_skips_stale_existing_server_time(self):
        tmp = Path(tempfile.mkdtemp())
        writer = TickDataWriter(str(tmp), "sz002796")
        today = datetime.now().strftime("%Y-%m-%d")
        existing = tmp / f"sz002796-{today}.csv"
        existing.write_text(
            "local_time_ms,server_time,price,open,high,low,prev_close,cum_volume,cum_amount,"
            "bp1,bv1,bp2,bv2,bp3,bv3,bp4,bv4,bp5,bv5,"
            "sp1,sv1,sp2,sv2,sp3,sv3,sp4,sv4,sp5,sv5,signal\n"
            "1,09:30:00,10,10,10,10,9,100,1000,"
            "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,HOLD\n",
            encoding="utf-8",
        )

        stale = {"server_time": "09:30:00", "price": 10, "cum_volume": 100, "cum_amount": 1000}
        fresh = {"server_time": "09:30:03", "price": 10.1, "cum_volume": 110, "cum_amount": 1110}
        try:
            self.assertFalse(writer.write(stale))
            self.assertTrue(writer.write(fresh))
        finally:
            if writer.file:
                writer.file.close()

        rows = existing.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertIn("09:30:03", rows[-1])

    def test_market_loader_recomputes_minute_tick_deltas_and_fills_orderbook(self):
        tmp = Path(tempfile.mkdtemp())
        csv_path = tmp / "sz002796-2026-01-05.csv"
        csv_path.write_text(
            "server_time,price,open,high,low,prev_close,cum_volume,cum_amount,tick_vol,tick_amt\n"
            "10:34:00,10,10,10,10,9,100,1000,-1,-1\n"
            "10:35:00,10.5,10,10.5,10,9,150,1525,-1,-1\n",
            encoding="utf-8",
        )

        bundle = load_market_data(data_dir=tmp, start_date="2026-01-05")

        self.assertEqual(list(bundle.frame["tick_vol"]), [100.0, 50.0])
        self.assertEqual(list(bundle.frame["tick_amt"]), [1000.0, 525.0])
        self.assertEqual(float(bundle.frame.iloc[0]["bp1"]), 0.0)
        self.assertFalse(bool(bundle.frame.iloc[0]["is_realtime"]))
        self.assertFalse(bool(bundle.frame.iloc[0]["is_tick_history"]))

    def test_market_loader_marks_dense_non_orderbook_files_as_tick_history(self):
        tmp = Path(tempfile.mkdtemp())
        csv_path = tmp / "sz002796-2026-01-05.csv"
        rows = ["server_time,price,open,high,low,prev_close,cum_volume,cum_amount,tick_vol,tick_amt\n"]
        start = datetime(2026, 1, 5, 9, 30)
        for idx in range(1001):
            dt = start + timedelta(seconds=idx)
            price = 10.0 + idx * 0.0001
            rows.append(
                f"{dt:%H:%M:%S},{price:.4f},10,10.2,9.9,9.8,{idx + 1},{(idx + 1) * price:.4f},1,{price:.4f}\n"
            )
        csv_path.write_text("".join(rows), encoding="utf-8")

        bundle = load_market_data(data_dir=tmp, start_date="2026-01-05")
        tick = row_to_tick(bundle.frame.iloc[0])

        self.assertTrue(bool(bundle.frame.iloc[0]["is_tick_history"]))
        self.assertTrue(tick["_is_tick_history"])
        self.assertFalse(tick["_is_realtime"])

    def test_minute_execution_price_uses_price(self):
        strategy = V6BacktestExecutionStrategy()
        tick = {"price": 10.0, "Close": 10.0, "_is_realtime": False, "sp1": 10.2, "bp1": 9.9}

        price, source, fallback = strategy.resolve_execution_price("BUY", tick)

        self.assertEqual(price, 10.0)
        self.assertEqual(source, "price")
        self.assertFalse(fallback)

    def test_realtime_execution_uses_sell_one_for_buy_and_buy_one_for_sell(self):
        strategy = V6BacktestExecutionStrategy()
        tick = {"price": 10.0, "_is_realtime": True, "sp1": 10.2, "bp1": 9.9}

        buy_price, buy_source, _ = strategy.resolve_execution_price("BUY", tick)
        sell_price, sell_source, _ = strategy.resolve_execution_price("SELL", tick)

        self.assertEqual(buy_price, 10.2)
        self.assertEqual(buy_source, "sp1")
        self.assertEqual(sell_price, 9.9)
        self.assertEqual(sell_source, "bp1")

    def test_realtime_missing_orderbook_falls_back_to_price(self):
        strategy = V6BacktestExecutionStrategy()
        tick = {"price": 10.0, "_is_realtime": True, "sp1": 0.0, "bp1": 0.0}

        price, source, fallback = strategy.resolve_execution_price("BUY", tick, count_fallback=True)

        self.assertEqual(price, 10.01)
        self.assertEqual(source, "price_fallback_slipped")
        self.assertTrue(fallback)
        self.assertEqual(strategy.orderbook_fallback_count, 1)

    def test_tick_history_without_orderbook_uses_conservative_slippage(self):
        strategy = V6BacktestExecutionStrategy()
        tick = {"price": 10.0, "_is_tick_history": True}

        buy_price, buy_source, buy_fallback = strategy.resolve_execution_price("BUY", tick, count_fallback=True)
        sell_price, sell_source, sell_fallback = strategy.resolve_execution_price("SELL", tick, count_fallback=True)

        self.assertEqual(buy_price, 10.01)
        self.assertEqual(buy_source, "tick_history_price_slipped")
        self.assertTrue(buy_fallback)
        self.assertEqual(sell_price, 9.99)
        self.assertEqual(sell_source, "tick_history_price_slipped")
        self.assertTrue(sell_fallback)
        self.assertEqual(strategy.orderbook_fallback_count, 2)

    def test_minimum_commission_is_applied(self):
        buy_costs = calculate_trade_costs("BUY", 10.0, 100)
        sell_costs = calculate_trade_costs("SELL", 10.0, 100)

        self.assertEqual(buy_costs.commission, 5.0)
        self.assertEqual(sell_costs.commission, 5.0)
        self.assertAlmostEqual(sell_costs.stamp_tax, 0.5)

    def test_limit_up_buy_and_limit_down_sell_are_blocked(self):
        self.assertTrue(is_limit_blocked("BUY", 11.0, 10.0))
        self.assertTrue(is_limit_blocked("SELL", 9.0, 10.0))

        strategy = V6BacktestExecutionStrategy()
        dt = datetime(2026, 1, 5, 14, 0)
        strategy._current_tick = {"price": 11.0, "sp1": 11.0, "prev_close": 10.0, "_is_realtime": True}
        self.assertIsNone(strategy._buy(11.0, 100, dt, 0.4, "test", "limit up"))
        self.assertEqual(strategy.limit_up_buy_skip_count, 1)

        strategy.shares = 100
        strategy._current_tick = {"price": 9.0, "bp1": 9.0, "prev_close": 10.0, "_is_realtime": True}
        self.assertIsNone(strategy._sell(9.0, 100, dt, 0.4, "test", "limit down"))
        self.assertEqual(strategy.limit_down_sell_skip_count, 1)

    def test_benchmark_buys_first_price_and_keeps_cash_remainder(self):
        benchmark = benchmark_all_in(1_000_000.0, 28.35, 50.0)

        self.assertEqual(benchmark.buy_shares, 35200)
        self.assertGreater(benchmark.cash_after_buy, 0.0)
        self.assertAlmostEqual(
            benchmark.cash_after_buy,
            1_000_000.0 - benchmark.buy_amount - benchmark.buy_commission,
            places=6,
        )

    def test_70pct_benchmark_buys_base_position_and_keeps_cash(self):
        benchmark = benchmark_buy_and_hold(1_000_000.0, 28.35, 50.0, target_pct=0.70)

        self.assertEqual(benchmark.buy_shares, 24600)
        self.assertGreater(benchmark.cash_after_buy, 300000.0)
        self.assertLess(benchmark.cash_after_buy, 303000.0)

    def test_cash_backtest_writes_outputs_without_runtime_state_inputs(self):
        tmp = Path(tempfile.mkdtemp())
        data_dir = tmp / "data"
        out_dir = tmp / "records"
        data_dir.mkdir()
        (data_dir / "sz002796-2026-01-05.csv").write_text(
            "server_time,price,open,high,low,prev_close,cum_volume,cum_amount,tick_vol,tick_amt\n"
            "10:34:00,10,10,10,10,10,100,1000,100,1000\n"
            "14:00:00,10.2,10,10.2,10,10,200,2020,100,1020\n",
            encoding="utf-8",
        )

        summary = run_backtest(start_date="2026-01-05", data_dir=data_dir, output_dir=out_dir)
        source = Path("sz002796/backtest.py").read_text(encoding="utf-8")

        self.assertTrue((out_dir / "trades.csv").exists())
        self.assertTrue((out_dir / "summary.json").exists())
        self.assertEqual(summary["data_rows"], 2)
        self.assertEqual(summary["initial_strategy_target_pct"], INITIAL_STRATEGY_TARGET_PCT)
        self.assertEqual(summary["benchmark_target_pct"], BENCHMARK_TARGET_PCT)
        self.assertTrue(summary["initial_seed_trade"])
        trades = (out_dir / "trades.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(trades[0].split(","), TRADE_LOG_COLUMNS)
        self.assertIn("initial 70% base position", trades[1])
        self.assertIn(",backtest,", trades[1])
        self.assertEqual(summary["known_data_quality_warnings"], [])
        for forbidden in ("strategy_state", "strategy_trades"):
            self.assertNotIn(forbidden, source)

    def test_market_regime_identifies_recent_trend_states(self):
        engine = MarketRegimeEngine()
        for day, price in enumerate([10.0, 10.6, 11.4, 12.2, 13.1], start=1):
            engine.update(
                {
                    "Time": datetime(2026, 1, day, 15, 0),
                    "price": price,
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "Volume": 1000.0,
                    "Amount": 1000.0 * price,
                }
            )

        uptrend = engine.update(
            {
                "Time": datetime(2026, 1, 6, 14, 0),
                "price": 14.2,
                "open": 13.2,
                "high": 14.3,
                "low": 13.1,
                "Volume": 1000.0,
                "Amount": 14200.0,
            }
        )

        self.assertEqual(uptrend.regime, MarketRegime.UPTREND)
        self.assertFalse(uptrend.allow_cross_day)
        self.assertGreaterEqual(uptrend.target_floor_pct, 0.9)

        for idx, price in enumerate([12.5, 11.8, 10.9, 10.2, 9.4], start=7):
            downtrend = engine.update(
                {
                    "Time": datetime(2026, 1, idx, 15, 0),
                    "price": price,
                    "open": price * 1.01,
                    "high": price * 1.02,
                    "low": price * 0.98,
                    "Volume": 1000.0,
                    "Amount": 1000.0 * price,
                }
            )

        self.assertEqual(downtrend.regime, MarketRegime.DOWNTREND)
        self.assertFalse(downtrend.allow_local_t)
        self.assertLess(downtrend.target_ceiling_pct, 0.7)

    def test_v6_clamps_target_to_state_band(self):
        strategy = CombinedStrategyV6()
        strategy.regime_decision = MarketRegimeDecision(
            regime=MarketRegime.UPTREND,
            tags=("above_ma5", "strong_ret5"),
            confidence=0.7,
            target_floor_pct=0.95,
            target_ceiling_pct=1.0,
            regime_score=0.9,
            detail="test",
            allow_cross_day=False,
            allow_local_t=False,
        )

        target, detail = strategy._apply_regime_target(0.40, "trim")

        self.assertAlmostEqual(target, 0.95)
        self.assertIn("regime=UPTREND", detail)

    def test_v6_main_flow_guard_blocks_same_day_floor_refill(self):
        strategy = CombinedStrategyV6()
        strategy.current_date = "2026-01-23"
        strategy.cash = 90000.0
        strategy.shares = 1000
        strategy.target_pct = 0.10
        strategy.main_flow_guard_date = "2026-01-23"
        strategy.main_flow_guard_floor_pct = 0.40
        strategy.regime_decision = MarketRegimeDecision(
            regime=MarketRegime.UPTREND,
            tags=("above_ma5", "strong_ret5"),
            confidence=0.8,
            target_floor_pct=0.95,
            target_ceiling_pct=1.0,
            regime_score=0.9,
            detail="test",
            allow_cross_day=False,
            allow_local_t=False,
        )

        calls = []

        def fail_if_floor_refill_called(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        strategy._align_to_target = fail_if_floor_refill_called
        record = strategy.on_tick(make_tick(datetime(2026, 1, 23, 13, 0), 10.0, 1))

        self.assertIsNone(record)
        self.assertEqual(calls, [])

    def test_local_short_refill_lock_can_be_disabled_in_legacy_mode(self):
        strategy = CombinedStrategyV6(trend_local_t_mode="legacy")
        strategy.current_date = "2026-06-01"
        strategy.cash = 8000.0
        strategy.shares = 300
        strategy.target_pct = 0.30
        strategy.local_base_target_pct = 0.70
        strategy.local_t_cycle = "short"
        strategy.local_t_entry_shares = 300
        strategy.last_trade_dt = datetime(2026, 6, 1, 12, 45)
        strategy.market_regime.update = lambda tick: fixed_regime_decision()

        calls = []

        def capture_align(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        strategy._align_to_target = capture_align
        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 13, 0), 10.0, 1))

        self.assertIsNone(record)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][3], "V6 floor refill")

    def test_directional_local_short_refill_lock_is_enabled_by_default(self):
        strategy = CombinedStrategyV6()
        strategy.current_date = "2026-06-01"
        strategy.cash = 8000.0
        strategy.shares = 300
        strategy.target_pct = 0.30
        strategy.local_base_target_pct = 0.70
        strategy.local_t_cycle = "short"
        strategy.local_t_entry_shares = 300
        strategy.last_trade_dt = datetime(2026, 6, 1, 12, 45)
        strategy.market_regime.update = lambda tick: fixed_regime_decision()

        calls = []

        def fail_if_align_called(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        strategy._align_to_target = fail_if_align_called
        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 13, 0), 10.0, 1))

        self.assertIsNone(record)
        self.assertEqual(calls, [])
        self.assertEqual(strategy.local_t_cycle, "short")

    def test_local_short_refill_lock_skips_refill_above_hard_floor(self):
        strategy = CombinedStrategyV6(protect_local_short_floor_refill=True)
        strategy.current_date = "2026-06-01"
        strategy.cash = 8000.0
        strategy.shares = 300
        strategy.target_pct = 0.30
        strategy.local_base_target_pct = 0.70
        strategy.local_t_cycle = "short"
        strategy.local_t_entry_shares = 300
        strategy.last_trade_dt = datetime(2026, 6, 1, 12, 45)
        strategy.market_regime.update = lambda tick: fixed_regime_decision()

        calls = []

        def fail_if_align_called(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        strategy._align_to_target = fail_if_align_called
        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 13, 0), 10.0, 1))

        self.assertIsNone(record)
        self.assertEqual(calls, [])
        self.assertEqual(strategy.local_t_cycle, "short")

    def test_local_short_refill_lock_allows_hard_floor_refill(self):
        strategy = CombinedStrategyV6(protect_local_short_floor_refill=True)
        strategy.current_date = "2026-06-01"
        strategy.cash = 10000.0
        strategy.shares = 100
        strategy.target_pct = 0.10
        strategy.local_base_target_pct = 0.70
        strategy.local_t_cycle = "short"
        strategy.local_t_entry_shares = 300
        strategy.last_trade_dt = datetime(2026, 6, 1, 12, 45)
        strategy.market_regime.update = lambda tick: fixed_regime_decision()

        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 13, 0), 10.0, 1))

        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "V6 local short hard-floor refill")
        self.assertEqual(record.side, "BUY")
        self.assertGreater(record.shares, 0)
        self.assertIn("short_refill_lock=1", record.detail)
        self.assertEqual(strategy.local_t_cycle, "short")
        self.assertEqual(strategy.day_trade_count, 1)
        self.assertEqual(strategy.last_trade_dt, datetime(2026, 6, 1, 13, 0))

    def test_directional_local_t_permissions_follow_regime(self):
        strategy = CombinedStrategyV6()

        strategy.regime_decision = directional_regime_decision(MarketRegime.DOWNTREND, floor_pct=0.0)
        self.assertEqual(strategy._local_t_permissions(), (True, True, False, True))

        strategy.regime_decision = directional_regime_decision(MarketRegime.UPTREND)
        self.assertEqual(strategy._local_t_permissions(), (False, True, True, True))

        strategy.regime_decision = directional_regime_decision(MarketRegime.RANGE)
        self.assertEqual(strategy._local_t_permissions(), (True, True, True, True))

    def test_downtrend_allows_short_entry_but_blocks_long_entry(self):
        strategy = CombinedStrategyV6(local_short_min_day_return=0.0)
        strategy.cash = 100000.0
        strategy.shares = 10000
        strategy.target_pct = 0.50
        strategy.local_base_target_pct = 0.50
        strategy.market_regime.update = lambda tick: directional_regime_decision(MarketRegime.DOWNTREND, floor_pct=0.0)
        strategy._score_cross_sell = lambda factors: 0.0
        strategy._score_cross_buy = lambda factors: 0.0
        strategy._score_local_trim = lambda factors: 1.0
        strategy._score_local_cover = lambda factors: 1.0
        strategy._score_sell_timing = lambda factors: 1.0
        strategy._score_buy_timing = lambda factors: 1.0

        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 10, 0), 10.0, 100))

        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "V6 local short entry")
        self.assertEqual(record.side, "SELL")
        self.assertEqual(strategy.local_t_cycle, "short")

        blocked = CombinedStrategyV6()
        blocked.cash = 100000.0
        blocked.shares = 1000
        blocked.target_pct = 0.40
        blocked.local_base_target_pct = 0.40
        blocked.market_regime.update = lambda tick: directional_regime_decision(MarketRegime.DOWNTREND, floor_pct=0.0)
        blocked._score_cross_sell = lambda factors: 0.0
        blocked._score_cross_buy = lambda factors: 0.0
        blocked._score_local_trim = lambda factors: 0.0
        blocked._score_local_cover = lambda factors: 1.0
        blocked._score_buy_timing = lambda factors: 1.0

        self.assertIsNone(blocked.on_tick(make_tick(datetime(2026, 6, 1, 10, 0), 10.0, 100)))
        self.assertIsNone(blocked.local_t_cycle)

    def test_local_short_entry_blocks_high_volume_opening_breakout(self):
        strategy = CombinedStrategyV6()
        strategy.regime_decision = directional_regime_decision(MarketRegime.RANGE)
        factors = FactorSnapshot(
            price=10.0,
            vwap=9.7,
            day_vwap_dev=0.03,
            local_vwap=9.75,
            local_vwap_dev=0.025,
            velocity=0.003,
            acceleration=-0.010,
            vol_mom=1.50,
            day_return=0.04,
            tick_vol=100.0,
            tick_amt=1000.0,
            high_return=0.06,
            opening_range_position=1.20,
            break_opening_high=1.0,
        )

        self.assertFalse(strategy._local_short_entry_signal(factors, 0.95))

        factors.vol_mom = 0.80
        self.assertTrue(strategy._local_short_entry_signal(factors, 0.95))

    def test_local_short_entry_requires_rebound_and_timing(self):
        strategy = CombinedStrategyV6()
        strategy.regime_decision = directional_regime_decision(MarketRegime.RANGE)
        factors = FactorSnapshot(
            price=10.0,
            vwap=9.7,
            day_vwap_dev=0.03,
            local_vwap=9.75,
            local_vwap_dev=0.025,
            velocity=0.003,
            acceleration=-0.010,
            vol_mom=0.80,
            day_return=0.005,
            tick_vol=100.0,
            tick_amt=1000.0,
            high_return=0.06,
        )

        self.assertFalse(strategy._local_short_entry_signal(factors, 0.95))

        factors.day_return = 0.04
        self.assertTrue(strategy._local_short_entry_signal(factors, 0.95))

        factors.day_vwap_dev = 0.010
        factors.local_vwap_dev = 0.004
        factors.high_return = 0.018
        factors.acceleration = 0.001
        self.assertFalse(strategy._local_short_entry_signal(factors, 0.95))

    def test_uptrend_blocks_short_entry_but_allows_long_entry(self):
        blocked = CombinedStrategyV6()
        blocked.cash = 1000.0
        blocked.shares = 10000
        blocked.target_pct = 0.95
        blocked.local_base_target_pct = 0.95
        blocked.market_regime.update = lambda tick: directional_regime_decision(MarketRegime.UPTREND)
        blocked._score_cross_sell = lambda factors: 0.0
        blocked._score_cross_buy = lambda factors: 0.0
        blocked._score_local_trim = lambda factors: 1.0
        blocked._score_sell_timing = lambda factors: 1.0

        self.assertIsNone(blocked.on_tick(make_tick(datetime(2026, 6, 1, 10, 0), 10.0, 100)))
        self.assertIsNone(blocked.local_t_cycle)

        strategy = CombinedStrategyV6()
        strategy.cash = 5500.0
        strategy.shares = 10000
        strategy.target_pct = 0.95
        strategy.local_base_target_pct = 0.95
        strategy.market_regime.update = lambda tick: directional_regime_decision(MarketRegime.UPTREND)
        strategy._score_cross_sell = lambda factors: 0.0
        strategy._score_cross_buy = lambda factors: 0.0
        strategy._score_local_trim = lambda factors: 0.0
        strategy._score_local_cover = lambda factors: 1.0
        strategy._score_buy_timing = lambda factors: 1.0

        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 10, 0), 10.0, 100))

        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "V6 local long entry")
        self.assertEqual(record.side, "BUY")
        self.assertEqual(strategy.local_t_cycle, "long")

    def test_local_short_cycle_profit_cover_precedes_floor_refill(self):
        strategy = CombinedStrategyV6()
        strategy.cash = 100000.0
        strategy.shares = 1000
        strategy.target_pct = 0.30
        strategy.local_base_target_pct = 0.40
        strategy.local_t_cycle = "short"
        strategy.local_t_entry_price = 10.0
        strategy.local_t_entry_shares = 300
        strategy.last_trade_dt = datetime(2026, 6, 1, 12, 59)
        strategy.market_regime.update = lambda tick: fixed_regime_decision()

        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 13, 0), 9.70, 100))

        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "V6 local short cover")
        self.assertEqual(record.side, "BUY")
        self.assertEqual(record.shares, 300)
        self.assertIsNone(strategy.local_t_cycle)
        self.assertIn("exit=profit", record.detail)

    def test_local_long_cycle_stop_exit_precedes_cooldown(self):
        strategy = CombinedStrategyV6()
        strategy.cash = 100000.0
        strategy.shares = 1000
        strategy.target_pct = 0.50
        strategy.local_base_target_pct = 0.40
        strategy.local_t_cycle = "long"
        strategy.local_t_entry_price = 10.0
        strategy.local_t_entry_shares = 300
        strategy.last_trade_dt = datetime(2026, 6, 1, 12, 59)
        strategy.market_regime.update = lambda tick: fixed_regime_decision()

        record = strategy.on_tick(make_tick(datetime(2026, 6, 1, 13, 0), 9.79, 100))

        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "V6 local long exit")
        self.assertEqual(record.side, "SELL")
        self.assertEqual(record.shares, 300)
        self.assertIsNone(strategy.local_t_cycle)
        self.assertIn("exit=stop", record.detail)

    def test_v6_backtest_writes_independent_outputs(self):
        tmp = Path(tempfile.mkdtemp())
        data_dir = tmp / "data"
        out_dir = tmp / "v6_records"
        data_dir.mkdir()
        (data_dir / "sz002796-2026-01-05.csv").write_text(
            "server_time,price,open,high,low,prev_close,cum_volume,cum_amount,tick_vol,tick_amt\n"
            "10:34:00,10,10,10,10,10,100,1000,100,1000\n"
            "14:00:00,10.2,10,10.2,10,10,200,2020,100,1020\n",
            encoding="utf-8",
        )

        summary = run_backtest(start_date="2026-01-05", data_dir=data_dir, output_dir=out_dir)

        self.assertEqual(summary["strategy_variant"], "CombinedStrategyV6")
        self.assertTrue(summary["initial_seed_trade"])
        self.assertTrue((out_dir / "trades.csv").exists())
        self.assertTrue((out_dir / "summary.json").exists())
        self.assertIn("regime_counts", summary)


if __name__ == "__main__":
    unittest.main()
