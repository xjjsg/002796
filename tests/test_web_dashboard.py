"""Tests for the web dashboard serialization and route surface."""
from __future__ import annotations

import unittest
from datetime import datetime

from sz002796.dashboard import build_dashboard_snapshot, trade_row_to_payload
from sz002796.factors import FactorSnapshot
from sz002796.position import TradeRecord
from sz002796.regime import MarketRegime, MarketRegimeDecision
from sz002796.strategy_v6 import CombinedStrategyV6
from sz002796.web_server import create_app


class WebDashboardTests(unittest.TestCase):
    def test_trade_payload_exposes_structured_execution_details(self):
        payload = trade_row_to_payload(
            {
                "timestamp": "2026-06-25 14:32:18",
                "side": "BUY",
                "price": 42.36,
                "last_price": 42.36,
                "shares": 2000,
                "amount": 84720,
                "commission": 8.472,
                "stamp_tax": 0,
                "cash_after": 500000,
                "position_shares": 12000,
                "position_pct_after": 0.503,
                "target_pct": 0.50,
                "mode": "NEUTRAL",
                "reason": "局部 T 回补",
                "detail": "回补已确认",
            }
        )

        self.assertEqual(payload["status"], "FILLED")
        self.assertEqual(payload["statusLabel"], "已成交")
        self.assertEqual(payload["side"], "BUY")
        self.assertEqual(payload["amount"], 84720)
        self.assertLess(payload["positionBefore"], payload["positionAfter"])
        self.assertEqual(payload["reason"], "局部 T 回补")

    def test_dashboard_snapshot_contains_decision_signals_and_orderbook(self):
        strategy = CombinedStrategyV6(initial_capital=1_000_000)
        strategy.cash = 500_000
        strategy.shares = 10_000
        strategy.target_pct = 0.50
        strategy.regime_decision = MarketRegimeDecision(
            regime=MarketRegime.RANGE,
            tags=("test",),
            confidence=0.8,
            target_floor_pct=0.40,
            target_ceiling_pct=1.00,
            regime_score=0.1,
            detail="震荡区间",
        )
        strategy.factor_calc.last_snapshot = FactorSnapshot(
            price=50.0,
            vwap=49.5,
            day_vwap_dev=0.01,
            local_vwap=49.8,
            local_vwap_dev=0.004,
            velocity=0.002,
            acceleration=0.001,
            vol_mom=1.2,
            day_return=0.03,
            tick_vol=100,
            tick_amt=5000,
            orderbook_imbalance=0.12,
        )
        trade = TradeRecord(
            timestamp=datetime(2026, 6, 25, 14, 32, 18),
            side="SELL",
            price=50.0,
            shares=1000,
            position_shares=9000,
            cash_after=550_000,
            target_pct=0.45,
            mode="DEFENSE",
            reason="主力流出保护",
            detail="卖出信号确认",
        )
        tick = {
            "price": 50.0,
            "prev_close": 48.54,
            "server_time": "14:32:18",
            "sp1": 50.01,
            "sv1": 1200,
            "bp1": 49.99,
            "bv1": 1500,
        }

        snapshot = build_dashboard_snapshot(
            tick,
            strategy,
            trade,
            market_source="tencent",
            market_source_label="现有接口",
            requested_market_source="tencent",
        )

        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["decision"]["state"], "filled")
        self.assertEqual(snapshot["decision"]["action"], "SELL")
        self.assertEqual(len(snapshot["signals"]), 7)
        self.assertEqual(len(snapshot["factors"]), 10)
        self.assertEqual(snapshot["orderbook"]["asks"][-1]["price"], 50.01)
        self.assertEqual(snapshot["trade"]["reason"], "主力流出保护")

    def test_web_app_exposes_dashboard_routes(self):
        paths = {route.resource.canonical for route in create_app().router.routes()}
        self.assertIn("/api/bootstrap", paths)
        self.assertIn("/api/runtime/start", paths)
        self.assertIn("/api/runtime/stop", paths)
        self.assertIn("/api/trades", paths)
        self.assertIn("/ws", paths)


if __name__ == "__main__":
    unittest.main()
