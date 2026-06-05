"""Regression tests for the refactored V6 system.

These tests cover the behaviors that must not drift during cleanup: factor
windows, state replay, stale tick filtering, execution-cost rules, regime
guardrails, and independent backtest output generation.
"""
import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from sz002796.strategy_v6 import CombinedStrategyV6
from sz002796.config import INITIAL_CAPITAL
from sz002796.position import PositionMode, TradeRecord
from sz002796.state_store import StrategyStateStore
from sz002796.tick_writer import TickDataWriter
from sz002796.market_data import load_market_data
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
from sz002796.factors import IntradayFactorCalc


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


class SmokeTests(unittest.TestCase):
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

        self.assertEqual(price, 10.0)
        self.assertEqual(source, "price_fallback")
        self.assertTrue(fallback)
        self.assertEqual(strategy.orderbook_fallback_count, 1)

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
        self.assertIn("initial 70% base position", trades[1])
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
