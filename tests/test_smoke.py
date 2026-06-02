import tempfile
import unittest
import os
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

os.environ.setdefault("LIVE_CONFIG_FILE", str(Path("data/sz002796/live_config.example.json")))

from combined_strategy_v5 import ANCHOR_PCT, CombinedStrategyV5
from gui_realtime_002796 import INITIAL_COST, PositionMode, StrategyStateStore
from run_v5_backtest import INITIAL_BUY_PRICE, load_market_data, tick_from_row
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

    def test_backtest_is_deterministic_for_same_data(self):
        def run_once():
            data = load_market_data("2026-05-28")
            strategy = CombinedStrategyV5(initial_capital=500000.0, local_enter_score=0.70)
            strategy.initialize_position(
                price=INITIAL_BUY_PRICE,
                timestamp=pd.Timestamp("2026-05-28 09:30:00"),
                target_pct=ANCHOR_PCT,
                reason="test seed",
            )
            for row in data.itertuples(index=False):
                strategy.on_tick(tick_from_row(row))
            return [
                (
                    trade.timestamp.isoformat(),
                    trade.side,
                    round(trade.price, 4),
                    trade.shares,
                    round(trade.target_pct, 6),
                    trade.reason,
                )
                for trade in strategy.trades
            ]

        self.assertEqual(run_once(), run_once())


if __name__ == "__main__":
    unittest.main()
