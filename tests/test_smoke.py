import tempfile
import unittest
import os
import json
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("LIVE_CONFIG_FILE", str(Path("data/sz002796/live_config.example.json")))

from combined_strategy_v5 import CombinedStrategyV5
from combined_strategy_v5_regime import CombinedStrategyV5Regime
from gui_realtime_002796 import INITIAL_COST, PositionMode, StrategyStateStore, TickDataWriter
from market_data import load_market_data
from market_regime import MarketRegime, MarketRegimeDecision, MarketRegimeEngine
from run_cash_backtest import (
    BacktestExecutionStrategy,
    BENCHMARK_TARGET_PCT,
    INITIAL_STRATEGY_TARGET_PCT,
    benchmark_all_in,
    benchmark_buy_and_hold,
    calculate_trade_costs,
    is_limit_blocked,
    run_backtest,
)
from run_regime_backtest import run_backtest as run_regime_backtest
from strategy_core import IntradayFactorCalc, TradeRecord


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
        strategy = CombinedStrategyV5(initial_capital=INITIAL_COST)
        strategy.cash = 1234.5
        strategy.shares = 3600
        strategy.target_pct = 0.82
        strategy.mode = PositionMode.ATTACK
        strategy.current_date = "2026-06-01"
        strategy.day_trade_count = 2
        strategy.last_trade_dt = datetime(2026, 6, 1, 14, 0)
        strategy.trades.append(
            TradeRecord(
                datetime(2026, 6, 1, 14, 0),
                "BUY",
                48.2,
                300,
                3600,
                1234.5,
                0.82,
                PositionMode.ATTACK.value,
                "test",
                "detail",
            )
        )

        store = StrategyStateStore(str(state_path), str(trade_path))
        store.save(strategy, {"price": 48.2, "Time": datetime(2026, 6, 1, 14, 0)}, "test")
        store.append_trade(strategy.trades[-1], strategy=strategy, tick={"price": 48.2, "server_time": "14:00:00"})

        restored = CombinedStrategyV5(initial_capital=INITIAL_COST)
        store.load(restored)
        self.assertEqual(restored.shares, 3600)
        self.assertAlmostEqual(restored.cash, 1234.5)
        self.assertAlmostEqual(restored.target_pct, 0.82)
        self.assertEqual(restored.mode, PositionMode.ATTACK)
        self.assertEqual(restored.day_trade_count, 2)
        self.assertEqual(len(restored.trades), 1)
        self.assertTrue(trade_path.exists())

    def test_mismatched_state_is_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        trade_path = tmp / "trades.csv"
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "symbol": "sz002796",
                    "initial_cost": 186133.6115,
                    "cash": 0.0,
                    "shares": 3700,
                    "target_pct": 1.0,
                    "mode": PositionMode.ATTACK.value,
                }
            ),
            encoding="utf-8",
        )

        strategy = CombinedStrategyV5(initial_capital=INITIAL_COST)
        store = StrategyStateStore(str(state_path), str(trade_path))
        loaded = store.load(strategy)

        self.assertIsNone(loaded)
        self.assertIsNotNone(store.ignored_state)
        self.assertEqual(strategy.shares, 0)
        self.assertAlmostEqual(strategy.cash, INITIAL_COST)

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
        strategy = BacktestExecutionStrategy()
        tick = {"price": 10.0, "Close": 10.0, "_is_realtime": False, "sp1": 10.2, "bp1": 9.9}

        price, source, fallback = strategy.resolve_execution_price("BUY", tick)

        self.assertEqual(price, 10.0)
        self.assertEqual(source, "price")
        self.assertFalse(fallback)

    def test_realtime_execution_uses_sell_one_for_buy_and_buy_one_for_sell(self):
        strategy = BacktestExecutionStrategy()
        tick = {"price": 10.0, "_is_realtime": True, "sp1": 10.2, "bp1": 9.9}

        buy_price, buy_source, _ = strategy.resolve_execution_price("BUY", tick)
        sell_price, sell_source, _ = strategy.resolve_execution_price("SELL", tick)

        self.assertEqual(buy_price, 10.2)
        self.assertEqual(buy_source, "sp1")
        self.assertEqual(sell_price, 9.9)
        self.assertEqual(sell_source, "bp1")

    def test_realtime_missing_orderbook_falls_back_to_price(self):
        strategy = BacktestExecutionStrategy()
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

        strategy = BacktestExecutionStrategy()
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

    def test_cash_backtest_writes_outputs_without_live_state_inputs(self):
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
        source = Path("run_cash_backtest.py").read_text(encoding="utf-8")

        self.assertTrue((out_dir / "trades.csv").exists())
        self.assertTrue((out_dir / "summary.json").exists())
        self.assertEqual(summary["data_rows"], 2)
        self.assertEqual(summary["initial_strategy_target_pct"], INITIAL_STRATEGY_TARGET_PCT)
        self.assertEqual(summary["benchmark_target_pct"], BENCHMARK_TARGET_PCT)
        self.assertTrue(summary["initial_seed_trade"])
        trades = (out_dir / "trades.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertIn("initial 70% base position", trades[1])
        self.assertEqual(summary["known_data_quality_warnings"], [])
        for forbidden in ("live_config.json", "strategy_state", "strategy_trades"):
            self.assertNotIn(forbidden, source)

    def test_market_regime_flags_breakdown_below_recent_low(self):
        engine = MarketRegimeEngine()
        for day in range(1, 6):
            engine.update(
                {
                    "Time": datetime(2026, 1, day, 15, 0),
                    "price": 10.0,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.0,
                    "Volume": 1000.0,
                    "Amount": 10000.0,
                }
            )

        decision = engine.update(
            {
                "Time": datetime(2026, 1, 6, 14, 0),
                "price": 8.8,
                "open": 10.0,
                "high": 10.0,
                "low": 8.8,
                "Volume": 1000.0,
                "Amount": 9200.0,
            }
        )

        self.assertEqual(decision.regime, MarketRegime.OVERSOLD_BOUNCE)
        # OVERSOLD_BOUNCE: score is 1.0 (strong intensity)
        self.assertEqual(decision.regime_score, 1.0)
        # Bands should reflect bounce (e.g. 0.60 floor, 1.00 ceiling)
        self.assertGreaterEqual(decision.target_ceiling_pct, 1.0)

    def test_regime_copy_clamps_target_to_state_band(self):
        strategy = CombinedStrategyV5Regime()
        strategy.regime_decision = MarketRegimeDecision(
            regime=MarketRegime.HIGH_VOLUME_TREND,
            tags=("above_vwap", "high_volume"),
            confidence=0.7,
            target_floor_pct=0.50,
            target_ceiling_pct=1.0,
            regime_score=0.9,
            detail="test",
        )

        target, detail = strategy._apply_regime_target(0.40, "trim")

        self.assertAlmostEqual(target, 0.50)
        self.assertIn("regime=HIGH_VOLUME_TREND", detail)

    def test_regime_backtest_writes_independent_outputs(self):
        tmp = Path(tempfile.mkdtemp())
        data_dir = tmp / "data"
        out_dir = tmp / "regime_records"
        data_dir.mkdir()
        (data_dir / "sz002796-2026-01-05.csv").write_text(
            "server_time,price,open,high,low,prev_close,cum_volume,cum_amount,tick_vol,tick_amt\n"
            "10:34:00,10,10,10,10,10,100,1000,100,1000\n"
            "14:00:00,10.2,10,10.2,10,10,200,2020,100,1020\n",
            encoding="utf-8",
        )

        summary = run_regime_backtest(start_date="2026-01-05", data_dir=data_dir, output_dir=out_dir)

        self.assertEqual(summary["strategy_variant"], "CombinedStrategyV5Regime")
        self.assertTrue(summary["initial_seed_trade"])
        self.assertTrue((out_dir / "trades.csv").exists())
        self.assertTrue((out_dir / "summary.json").exists())
        self.assertIn("regime_counts", summary)


if __name__ == "__main__":
    unittest.main()
