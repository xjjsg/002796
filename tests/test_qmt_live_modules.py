import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from qmt.anti_overfit_validation import _normalise_trade_frame
from qmt.live_data import RealtimeTickFeed
from qmt.run_live import (
    JsonlEventLogger,
    MARKET_CSV_COLUMNS,
    RealtimeMarketCsvWriter,
    V6LiveEngine,
    initial_base_order_request,
    is_after_final_close,
    is_tick_after_final_close,
    is_trading_datetime,
    next_trading_start,
    validate_live_account,
)
from qmt.update_local_data import _safe_backup_dir, build_updated_data
from qmt.trade_gateway import (
    AccountSnapshot,
    LiveRiskLimits,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
    QmtTradeGateway,
)
from sz002796.position import TradeRecord


class QmtLiveModuleTests(unittest.TestCase):
    def test_realtime_tick_feed_normalizes_callback_payload(self):
        feed = RealtimeTickFeed(symbol="002796.SZ", max_queue_size=10)
        feed._on_quote(
            {
                "002796.SZ": [
                    {
                        "time": 1780981200000,
                        "lastPrice": 41.66,
                        "open": 41.99,
                        "high": 41.99,
                        "low": 40.93,
                        "lastClose": 41.36,
                        "volume": 55660,
                        "amount": 230911779.0,
                        "askPrice": [41.66, 41.68, 41.69, 41.70, 41.71],
                        "bidPrice": [41.60, 41.58, 41.56, 41.55, 41.54],
                        "askVol": [2, 12, 8, 59, 1],
                        "bidVol": [30, 2, 4, 13, 61],
                    }
                ]
            }
        )

        tick = feed.wait_next(timeout=0.01)

        self.assertIsNotNone(tick)
        self.assertEqual(feed.stats.callback_count, 1)
        self.assertEqual(feed.stats.tick_count, 1)
        self.assertEqual(tick["price"], 41.66)
        self.assertEqual(tick["bp1"], 41.60)
        self.assertEqual(tick["sp1"], 41.66)
        self.assertEqual(tick["bv1"], 3000)
        self.assertEqual(tick["_is_realtime"], True)

    def test_trade_gateway_dry_run_does_not_require_connection(self):
        events = []
        gateway = QmtTradeGateway(
            account_id="real_account_for_test",
            live_orders_enabled=False,
            event_handler=lambda event, payload: events.append((event, payload)),
        )

        result = gateway.place_order(
            OrderRequest(side="BUY", symbol="002796.SZ", price=10.12, shares=300)
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.sent)
        self.assertTrue(result.dry_run)
        self.assertEqual(events[0][0], "order_dry_run")

    def test_trade_gateway_risk_blocks_oversized_order_before_dry_run(self):
        gateway = QmtTradeGateway(
            account_id="real_account_for_test",
            live_orders_enabled=False,
            risk_limits=LiveRiskLimits(max_order_value=1000.0, max_shares_per_order=10000),
        )

        result = gateway.place_order(
            OrderRequest(side="BUY", symbol="002796.SZ", price=10.0, shares=200)
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.sent)
        self.assertIn("exceeds max_order_value", result.message)

    def test_trade_gateway_can_explicitly_allow_test_account(self):
        gateway = QmtTradeGateway(
            account_id="testS",
            live_orders_enabled=False,
            allow_test_account=True,
        )

        self.assertTrue(gateway.allow_test_account)
        self.assertFalse(gateway.risk_limits.forbid_backtest_account)

    def test_anti_overfit_analysis_accepts_unified_trade_columns(self):
        trades = pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-10 09:30:00",
                    "side": "BUY",
                    "asset_after": 1_000_000.0,
                    "position_pct_after": 0.7,
                    "orderbook_fallback": False,
                }
            ]
        )

        normalized = _normalise_trade_frame(trades)

        self.assertEqual(normalized.loc[0, "time"], "2026-06-10 09:30:00")
        self.assertEqual(normalized.loc[0, "asset"], 1_000_000.0)
        self.assertEqual(normalized.loc[0, "position_pct"], 0.7)

    def test_update_local_data_backup_dir_avoids_existing_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data" / "sz002796"
            data_dir.mkdir(parents=True)
            first = _safe_backup_dir(data_dir)
            first.mkdir()
            second = _safe_backup_dir(data_dir)

            self.assertNotEqual(first, second)
            self.assertFalse(second.exists())

    def test_update_local_data_reports_empty_qmt_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "sz002796"
            staging_dir = root / "staging"
            data_dir.mkdir(parents=True)

            with patch("qmt.update_local_data._load_qmt_ticks", return_value=pd.DataFrame()):
                with self.assertRaisesRegex(RuntimeError, "no QMT tick data returned"):
                    build_updated_data(
                        symbol="002796.SZ",
                        start_time="20260105",
                        end_time="20260106",
                        data_dir=data_dir,
                        output_dir=staging_dir,
                    )

    def test_market_csv_writer_writes_existing_orderbook_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = RealtimeMarketCsvWriter(tmp)
            tick = {
                "Time": datetime(2026, 6, 10, 9, 30, 3),
                "_local_time_ms": 1781055003000,
                "price": 41.66,
                "open": 41.0,
                "high": 42.0,
                "low": 40.5,
                "prev_close": 41.36,
                "Volume": 100000,
                "Amount": 4166000.0,
                "sp1": 41.67,
                "sv1": 1000,
                "bp1": 41.65,
                "bv1": 1200,
            }

            path = writer.write_tick("002796.SZ", tick)

            with path.open(encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(path.name, "sz002796-2026-06-10.csv")
            self.assertEqual(rows[0].keys(), dict.fromkeys(MARKET_CSV_COLUMNS).keys())
            self.assertEqual(rows[0]["local_time_ms"], "1781055003000")
            self.assertEqual(rows[0]["server_time"], "09:30:03")
            self.assertEqual(rows[0]["price"], "41.66")
            self.assertEqual(rows[0]["sp1"], "41.67")
            self.assertEqual(rows[0]["bp1"], "41.65")
            self.assertEqual(rows[0]["signal"], "HOLD")

    def test_market_csv_writer_appends_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = RealtimeMarketCsvWriter(tmp)
            tick = {
                "Time": datetime(2026, 6, 10, 9, 30, 3),
                "_local_time_ms": 1781055003000,
                "price": 41.66,
                "open": 41.0,
                "high": 42.0,
                "low": 40.5,
                "prev_close": 41.36,
                "Volume": 100000,
                "Amount": 4166000.0,
            }
            writer.write_tick("002796.SZ", tick)
            tick2 = dict(tick)
            tick2["_local_time_ms"] = 1781055006000
            tick2["Time"] = datetime(2026, 6, 10, 9, 30, 6)
            tick2["price"] = 41.70
            path = writer.write_tick("002796.SZ", tick2)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0], ",".join(MARKET_CSV_COLUMNS))

    def test_market_csv_writer_spools_locked_file_and_flushes_later(self):
        class LockingWriter(RealtimeMarketCsvWriter):
            def __init__(self, data_dir, fallback_dir):
                super().__init__(data_dir, fallback_dir=fallback_dir)
                self.fail_main = True

            def _append_rows(self, path, rows):
                if self.fail_main and Path(path).parent == self.data_dir:
                    raise PermissionError(13, "Permission denied")
                return super()._append_rows(path, rows)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = LockingWriter(root / "data", root / "spool")
            tick = {
                "Time": datetime(2026, 6, 10, 13, 4, 18),
                "_local_time_ms": 1781067858000,
                "price": 40.54,
                "sp1": 40.57,
                "sv1": 700,
                "bp1": 40.54,
                "bv1": 400,
            }

            path = writer.write_tick("002796.SZ", tick)

            self.assertEqual(writer.write_error_count, 1)
            self.assertEqual(writer.pending_count(), 1)
            self.assertFalse(path.exists())
            self.assertTrue(writer.last_spool_path.exists())

            writer.fail_main = False
            tick2 = dict(tick)
            tick2["Time"] = datetime(2026, 6, 10, 13, 4, 19)
            tick2["_local_time_ms"] = 1781067859000
            tick2["price"] = 40.55
            writer.write_tick("002796.SZ", tick2)

            with path.open(encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(writer.pending_count(), 0)
            self.assertEqual([row["server_time"] for row in rows], ["13:04:18", "13:04:19"])

    def test_initial_base_order_uses_total_asset_and_ask1(self):
        snapshot = AccountSnapshot(account_id="99005544", cash=10_000_000.0, total_asset=10_000_000.0)
        request = initial_base_order_request("002796.SZ", snapshot, {"sp1": 41.67})

        self.assertIsNotNone(request)
        self.assertEqual(request.side, "BUY")
        self.assertEqual(request.price, 41.67)
        self.assertEqual(request.shares, 167900)

    def test_initial_base_order_waits_for_valid_ask1(self):
        snapshot = AccountSnapshot(account_id="99005544", cash=10_000_000.0, total_asset=10_000_000.0)

        self.assertIsNone(initial_base_order_request("002796.SZ", snapshot, {"sp1": 0.0}))

    def test_live_account_rejects_non_sim_account(self):
        with self.assertRaises(ValueError):
            validate_live_account("12345678", data_only=False)

    def test_live_account_allows_test_account_with_explicit_flag(self):
        validate_live_account("testS", data_only=False, allow_test_account=True)

    def test_data_only_allows_no_account_validation(self):
        validate_live_account("12345678", data_only=True)

    def test_trading_time_helpers_cover_daily_windows(self):
        self.assertFalse(is_trading_datetime(datetime(2026, 6, 10, 9, 24, 59)))
        self.assertTrue(is_trading_datetime(datetime(2026, 6, 10, 9, 25, 0)))
        self.assertFalse(is_trading_datetime(datetime(2026, 6, 10, 11, 45, 0)))
        self.assertTrue(is_trading_datetime(datetime(2026, 6, 10, 13, 0, 0)))
        self.assertTrue(is_after_final_close(datetime(2026, 6, 10, 15, 0, 1)))
        self.assertEqual(
            next_trading_start(datetime(2026, 6, 10, 11, 45, 0)),
            datetime(2026, 6, 10, 13, 0, 0),
        )

    def test_tick_close_helper_uses_qmt_tick_time(self):
        self.assertFalse(is_tick_after_final_close({"Time": datetime(2026, 6, 10, 9, 25, 0)}))
        self.assertTrue(is_tick_after_final_close({"Time": datetime(2026, 6, 10, 15, 0, 0)}))
        self.assertTrue(is_tick_after_final_close({"Time": datetime(2026, 6, 10, 15, 0, 1)}))

    def test_live_engine_resyncs_after_rejected_strategy_order(self):
        class SignalingStrategy:
            def on_tick(self, _tick):
                return TradeRecord(
                    timestamp=datetime(2026, 6, 10, 9, 30, 1),
                    side="BUY",
                    price=41.67,
                    shares=300,
                    position_shares=300,
                    cash_after=987499.0,
                    target_pct=0.7,
                    mode="ATTACK",
                    reason="unit-test signal",
                )

        class RejectingGateway:
            def __init__(self):
                self.sync_count = 0
                self.last_snapshot = AccountSnapshot(
                    account_id="99005544",
                    cash=1_000_000.0,
                    total_asset=1_000_000.0,
                    positions={
                        "002796.SZ": PositionSnapshot(
                            stock_code="002796.SZ",
                            volume=300,
                            can_use_volume=300,
                            market_value=12_501.0,
                        )
                    },
                )

            def place_order(self, request):
                return OrderResult(
                    ok=False,
                    sent=False,
                    dry_run=False,
                    side=request.side,
                    symbol=request.symbol,
                    price=request.price,
                    shares=request.shares,
                    message="unit-test reject",
                )

            def sync_strategy_state(self, _strategy, _symbol, mark_price):
                self.sync_count += 1
                return self.last_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            gateway = RejectingGateway()
            logger = JsonlEventLogger(output_dir=tmp, market_data_dir=tmp)
            engine = V6LiveEngine(
                symbol="002796.SZ",
                gateway=gateway,
                feed=RealtimeTickFeed(symbol="002796.SZ"),
                logger=logger,
                sync_interval_seconds=999999.0,
            )
            engine.strategy = SignalingStrategy()
            engine.initial_position_ready = True
            engine.last_sync_ts = 10**12

            engine._handle_tick(
                {
                    "Time": datetime(2026, 6, 10, 9, 30, 1),
                    "price": 41.66,
                    "sp1": 41.67,
                    "bp1": 41.65,
                    "prev_close": 40.0,
                }
            )

            events = [json.loads(line)["event"] for line in logger.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(gateway.sync_count, 1)
            self.assertIn("order_result", events)
            self.assertIn("account_sync_after_order_failure", events)

    def test_initial_position_order_exception_is_logged_without_raising(self):
        class RaisingGateway:
            def place_order(self, _request):
                raise RuntimeError("qmt order failed")

        with tempfile.TemporaryDirectory() as tmp:
            logger = JsonlEventLogger(output_dir=tmp, market_data_dir=tmp)
            engine = V6LiveEngine(
                symbol="002796.SZ",
                gateway=RaisingGateway(),
                feed=RealtimeTickFeed(symbol="002796.SZ"),
                logger=logger,
            )
            snapshot = AccountSnapshot(
                account_id="99005544",
                cash=10_000_000.0,
                total_asset=10_000_000.0,
            )

            engine._try_initial_position(
                {
                    "Time": datetime(2026, 6, 10, 9, 25, 0),
                    "sp1": 41.03,
                    "prev_close": 40.0,
                },
                snapshot,
            )

            events = [json.loads(line)["event"] for line in logger.path.read_text(encoding="utf-8").splitlines()]
            self.assertIn("initial_position_order", events)
            self.assertIn("order_exception", events)
            self.assertEqual(engine.order_count, 0)


if __name__ == "__main__":
    unittest.main()
