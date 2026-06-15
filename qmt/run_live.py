"""Run V6 with realtime miniQMT data and guarded live-order support."""
from __future__ import annotations

import argparse
import csv
import json
import signal
import time
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sz002796.backtest import V6BacktestExecutionStrategy
from sz002796.config import DATA_DIR, INITIAL_CAPITAL, INITIAL_STRATEGY_TARGET_PCT, LOT_SIZE, SYMBOL_CODE
from sz002796.execution import is_limit_blocked, max_affordable_lot_shares

from qmt.config import OUTPUT_ROOT, QMT_BACKTEST_ACCOUNT, QMT_SIM_ACCOUNT, TARGET_SYMBOL
from qmt.live_data import RealtimeTickFeed
from qmt.trade_gateway import AccountSnapshot, LiveRiskLimits, OrderRequest, QmtTradeGateway


MARKET_CSV_COLUMNS = [
    "local_time_ms",
    "server_time",
    "price",
    "open",
    "high",
    "low",
    "prev_close",
    "cum_volume",
    "cum_amount",
    "bp1",
    "bv1",
    "bp2",
    "bv2",
    "bp3",
    "bv3",
    "bp4",
    "bv4",
    "bp5",
    "bv5",
    "sp1",
    "sv1",
    "sp2",
    "sv2",
    "sp3",
    "sv3",
    "sp4",
    "sv4",
    "sp5",
    "sv5",
    "signal",
]

TRADING_WINDOWS = (
    (dt_time(9, 25), dt_time(11, 30)),
    (dt_time(13, 0), dt_time(15, 0)),
)


def is_trading_datetime(value: datetime | None = None) -> bool:
    now = value or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return any(start <= current <= end for start, end in TRADING_WINDOWS)


def is_after_final_close(value: datetime | None = None) -> bool:
    now = value or datetime.now()
    return now.weekday() < 5 and now.time() > TRADING_WINDOWS[-1][1]


def is_tick_after_final_close(tick: dict[str, Any]) -> bool:
    dt = tick.get("Time")
    if dt is None:
        return False
    return dt.weekday() < 5 and dt.time() >= TRADING_WINDOWS[-1][1]


def next_trading_start(value: datetime | None = None) -> datetime:
    now = value or datetime.now()
    if now.weekday() >= 5:
        days = 7 - now.weekday()
        target = now + timedelta(days=days)
        return target.replace(hour=9, minute=25, second=0, microsecond=0)
    current = now.time()
    for start, _ in TRADING_WINDOWS:
        if current < start:
            return now.replace(hour=start.hour, minute=start.minute, second=start.second, microsecond=0)
    days = 3 if now.weekday() == 4 else 1
    target = now + timedelta(days=days)
    return target.replace(hour=9, minute=25, second=0, microsecond=0)


def has_top_of_book(tick: dict[str, Any]) -> bool:
    return float(tick.get("bp1", 0.0) or 0.0) > 0 and float(tick.get("sp1", 0.0) or 0.0) > 0


def initial_base_order_request(
    symbol: str,
    snapshot: AccountSnapshot,
    tick: dict[str, Any],
    target_pct: float = INITIAL_STRATEGY_TARGET_PCT,
) -> OrderRequest | None:
    ask_price = float(tick.get("sp1", 0.0) or 0.0)
    if ask_price <= 0:
        return None
    target_asset = float(snapshot.total_asset or 0.0) * target_pct
    target_shares = int(target_asset / ask_price / LOT_SIZE) * LOT_SIZE
    affordable = max_affordable_lot_shares(float(snapshot.cash or 0.0), ask_price)
    shares = min(target_shares, affordable)
    shares = int(shares / LOT_SIZE) * LOT_SIZE
    if shares <= 0:
        return None
    return OrderRequest(
        side="BUY",
        symbol=symbol,
        price=ask_price,
        shares=shares,
        strategy_name="v6_initial_base",
        remark="002796_v6_initial_70pct",
    )


def validate_live_account(account_id: str, data_only: bool, allow_test_account: bool = False) -> None:
    if data_only:
        return
    if account_id == QMT_BACKTEST_ACCOUNT:
        if allow_test_account:
            return
        raise ValueError("order_stock with testS requires --allow-test-account")
    if account_id != QMT_SIM_ACCOUNT:
        raise ValueError(f"real order_stock mode only allows account {QMT_SIM_ACCOUNT}")


class JsonlEventLogger:
    def __init__(self, output_dir: str | Path | None = None, market_data_dir: str | Path = DATA_DIR):
        root = Path(output_dir) if output_dir else OUTPUT_ROOT.parent / "live_records"
        session = datetime.now().strftime("live_v6_%Y%m%d_%H%M%S")
        self.output_dir = root / session
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.output_dir / "events.jsonl"
        self.market_writer = RealtimeMarketCsvWriter(
            market_data_dir,
            fallback_dir=self.output_dir / "market_csv_spool",
        )

    def write(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event_type,
            "payload": _jsonable(payload),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_tick(self, symbol: str, tick: dict[str, Any], signal_text: str = "HOLD") -> Path:
        return self.market_writer.write_tick(symbol, tick, signal_text=signal_text)

    @property
    def market_csv_path(self) -> Path | None:
        return self.market_writer.last_path

    @property
    def market_csv_spool_path(self) -> Path | None:
        return self.market_writer.last_spool_path


class RealtimeMarketCsvWriter:
    def __init__(self, data_dir: str | Path = DATA_DIR, fallback_dir: str | Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.seen_keys: set[tuple[int, str, float]] = set()
        self.last_path: Path | None = None
        self.fallback_dir = Path(fallback_dir) if fallback_dir else None
        if self.fallback_dir is not None:
            self.fallback_dir.mkdir(parents=True, exist_ok=True)
        self.last_spool_path: Path | None = None
        self.last_error: str | None = None
        self.write_error_count = 0
        self.pending_rows: dict[Path, list[dict[str, Any]]] = {}

    def write_tick(self, symbol: str, tick: dict[str, Any], signal_text: str = "HOLD") -> Path:
        row, path, key = self._row_for_tick(tick, signal_text)
        self.last_path = path
        if key in self.seen_keys:
            return path
        try:
            self._flush_pending_path(path)
            self._append_rows(path, [row])
            self.last_error = None
        except OSError as exc:
            self.write_error_count += 1
            self.last_error = repr(exc)
            self.pending_rows.setdefault(path, []).append(row)
            self._append_spool_row(path.name, row)
        self.seen_keys.add(key)
        return path

    def pending_count(self) -> int:
        return sum(len(rows) for rows in self.pending_rows.values())

    def flush_pending(self) -> None:
        for path in list(self.pending_rows.keys()):
            self._flush_pending_path(path)

    def _row_for_tick(
        self,
        tick: dict[str, Any],
        signal_text: str,
    ) -> tuple[dict[str, Any], Path, tuple[int, str, float]]:
        dt = tick.get("Time")
        now = datetime.now()
        date_text = dt.strftime("%Y-%m-%d") if dt is not None else now.strftime("%Y-%m-%d")
        local_time_ms = int(tick.get("_local_time_ms") or time.time() * 1000)
        server_time = dt.strftime("%H:%M:%S") if dt is not None else ""
        price = float(tick.get("price", 0.0) or 0.0)
        key = (local_time_ms, server_time, price)
        path = self.data_dir / f"{SYMBOL_CODE}-{date_text}.csv"
        row = {
            "local_time_ms": local_time_ms,
            "server_time": server_time,
            "price": price,
            "open": float(tick.get("open", 0.0) or 0.0),
            "high": float(tick.get("high", 0.0) or 0.0),
            "low": float(tick.get("low", 0.0) or 0.0),
            "prev_close": float(tick.get("prev_close", 0.0) or 0.0),
            "cum_volume": float(tick.get("Volume", tick.get("cum_volume", 0.0)) or 0.0),
            "cum_amount": float(tick.get("Amount", tick.get("cum_amount", 0.0)) or 0.0),
            "signal": signal_text or "HOLD",
        }
        for level in range(1, 6):
            row[f"bp{level}"] = float(tick.get(f"bp{level}", 0.0) or 0.0)
            row[f"bv{level}"] = float(tick.get(f"bv{level}", 0.0) or 0.0)
            row[f"sp{level}"] = float(tick.get(f"sp{level}", 0.0) or 0.0)
            row[f"sv{level}"] = float(tick.get(f"sv{level}", 0.0) or 0.0)
        return row, path, key

    def _flush_pending_path(self, path: Path) -> None:
        rows = self.pending_rows.get(path)
        if not rows:
            return
        self._append_rows(path, rows)
        self.pending_rows.pop(path, None)

    def _append_spool_row(self, file_name: str, row: dict[str, Any]) -> None:
        if self.fallback_dir is None:
            raise PermissionError(self.last_error or "market CSV write failed")
        spool_path = self.fallback_dir / file_name
        self._append_rows(spool_path, [row])
        self.last_spool_path = spool_path

    def _append_rows(self, path: Path, rows: list[dict[str, Any]]) -> None:
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MARKET_CSV_COLUMNS)
            if needs_header:
                writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in MARKET_CSV_COLUMNS})


class V6LiveEngine:
    def __init__(
        self,
        symbol: str,
        gateway: QmtTradeGateway | None,
        feed: RealtimeTickFeed,
        logger: JsonlEventLogger,
        sync_interval_seconds: float = 30.0,
        pending_timeout_seconds: float = 30.0,
        print_every_ticks: int = 200,
        record_ticks: bool = True,
        data_only: bool = False,
    ):
        self.symbol = symbol
        self.gateway = gateway
        self.feed = feed
        self.logger = logger
        self.sync_interval_seconds = sync_interval_seconds
        self.pending_timeout_seconds = pending_timeout_seconds
        self.print_every_ticks = print_every_ticks
        self.record_ticks = record_ticks
        self.data_only = data_only
        self.strategy = V6BacktestExecutionStrategy(initial_capital=INITIAL_CAPITAL)
        self.strategy._position_built = True
        self.strategy.enable_local_t = self.strategy._normal_enable_local_t
        self.last_sync_ts = 0.0
        self.pending_order_id: int | None = None
        self.pending_until_ts = 0.0
        self.initial_position_ready = False
        self.tick_count = 0
        self.signal_count = 0
        self.order_count = 0
        self.market_write_errors_reported = 0
        self._stop = False
        self._services_started = False

    def stop(self) -> None:
        self._stop = True

    def run(self, duration_seconds: int = 0) -> dict[str, Any]:
        deadline = time.time() + duration_seconds if duration_seconds > 0 else None
        summary: dict[str, Any] | None = None
        error: Exception | None = None
        try:
            self._start_services()
            while not self._stop:
                if deadline is not None and time.time() >= deadline:
                    break
                tick = self.feed.wait_next(timeout=1.0)
                if tick is None:
                    continue
                self._handle_tick(tick)
                if is_tick_after_final_close(tick):
                    print("[qmt live] final QMT tick reached 15:00; stopping for today", flush=True)
                    break
        except Exception as exc:
            error = exc
            self.logger.write("live_engine_exception", {"error": repr(exc)})
        finally:
            self._stop_services()
            self._flush_market_pending()
            summary = {
                "symbol": self.symbol,
                "data_only": self.data_only,
                "tick_count": self.tick_count,
                "signal_count": self.signal_count,
                "order_count": self.order_count,
                "feed_stats": self.feed.stats.as_dict(),
                "events": str(self.logger.path),
                "market_csv": str(self.logger.market_csv_path) if self.logger.market_csv_path else None,
                "market_csv_spool": str(self.logger.market_csv_spool_path) if self.logger.market_csv_spool_path else None,
                "market_csv_write_errors": self.logger.market_writer.write_error_count,
                "market_csv_pending_rows": self.logger.market_writer.pending_count(),
            }
            self.logger.write("live_engine_stopped", summary)
            print("[qmt live] stopped; events=%s market_csv=%s" % (self.logger.path, summary["market_csv"]), flush=True)
        if error is not None:
            raise error
        return summary

    def _start_services(self) -> None:
        try:
            if self.gateway is not None:
                info = self.gateway.connect()
                self.logger.write("trade_gateway_connected", info)
            seq = self.feed.start()
            self.logger.write("market_data_subscribed", {"symbol": self.symbol, "seq": seq})
            self._services_started = True
        except Exception:
            if self.gateway is not None:
                self.gateway.disconnect()
            raise
        print("[qmt live] subscribed %s tick seq=%s" % (self.symbol, seq), flush=True)
        if self.gateway is None:
            print("[qmt live] data-only mode; no account query and no order path", flush=True)
        elif self.gateway.live_orders_enabled:
            print("[qmt live] LIVE ORDER MODE ENABLED account=%s" % self.gateway.account_id, flush=True)
        else:
            print("[qmt live] trade gateway connected with live_orders_enabled=False; order_stock will not be called", flush=True)

    def _stop_services(self) -> None:
        if not self._services_started:
            return
        self.feed.stop()
        if self.gateway is not None:
            self.gateway.disconnect()
        self._services_started = False

    def _handle_tick(self, tick: dict[str, Any]) -> None:
        self.tick_count += 1
        if self.record_ticks:
            self._write_market_tick(tick)
        price = float(tick.get("price", 0.0) or 0.0)
        if self.print_every_ticks > 0 and self.tick_count % self.print_every_ticks == 0:
            print(
                "[qmt live] ticks=%s price=%.2f bp1=%.2f sp1=%.2f signals=%s orders=%s"
                % (
                    self.tick_count,
                    price,
                    float(tick.get("bp1", 0.0) or 0.0),
                    float(tick.get("sp1", 0.0) or 0.0),
                    self.signal_count,
                    self.order_count,
                ),
                flush=True,
            )
        if self.data_only or self.gateway is None:
            return

        now = time.time()
        if self.pending_order_id is not None and now < self.pending_until_ts:
            return
        snapshot = self.gateway.last_snapshot
        if self.pending_order_id is not None and now >= self.pending_until_ts:
            self.logger.write("pending_order_timeout", {"order_id": self.pending_order_id})
            self.pending_order_id = None
            snapshot = self._sync_account(price)
            if snapshot.position(self.symbol).volume > 0:
                self.initial_position_ready = True
        elif now - self.last_sync_ts >= self.sync_interval_seconds:
            snapshot = self._sync_account(price)
        elif snapshot is None:
            snapshot = self._sync_account(price)

        if not self.initial_position_ready:
            position = snapshot.position(self.symbol)
            if position.volume > 0:
                self.initial_position_ready = True
                self.logger.write("initial_position_existing", position.__dict__)
            else:
                self._try_initial_position(tick, snapshot)
                return

        if not has_top_of_book(tick):
            self.logger.write(
                "orderbook_invalid_skip_strategy",
                {
                    "time": tick["Time"].isoformat(sep=" ", timespec="seconds") if tick.get("Time") else None,
                    "price": price,
                    "bp1": tick.get("bp1", 0.0),
                    "sp1": tick.get("sp1", 0.0),
                },
            )
            return

        record = self.strategy.on_tick(tick)
        if record is None:
            return

        self.signal_count += 1
        payload = {
            "time": record.timestamp.isoformat(sep=" ", timespec="seconds"),
            "side": record.side,
            "price": record.price,
            "shares": record.shares,
            "target_pct": record.target_pct,
            "reason": record.reason,
            "detail": record.detail,
        }
        self.logger.write("strategy_signal", payload)
        print(
            "[qmt live] signal %s %s @ %.2f target=%.1f%% reason=%s"
            % (record.side, record.shares, record.price, record.target_pct * 100.0, record.reason),
            flush=True,
        )

        request = OrderRequest(
            side=record.side,
            symbol=self.symbol,
            price=record.price,
            shares=record.shares,
            strategy_name="v6_live",
            remark="002796_v6_live",
        )
        try:
            result = self.gateway.place_order(request)
        except Exception as exc:
            self.logger.write(
                "order_exception",
                {
                    "phase": "strategy_signal",
                    "error": repr(exc),
                    "request": request.__dict__,
                },
            )
            print("[qmt live] order exception; continuing: %s" % repr(exc), flush=True)
            self._sync_after_failed_order(price, "strategy_signal", repr(exc))
            return
        self.logger.write("order_result", result.__dict__)
        if result.ok:
            self.order_count += 1
        else:
            self._sync_after_failed_order(price, "strategy_signal", result.message)
        if result.sent and result.order_id:
            self.pending_order_id = result.order_id
            self.pending_until_ts = time.time() + self.pending_timeout_seconds
        print("[qmt live] order_result ok=%s sent=%s dry_run=%s message=%s" % (
            result.ok,
            result.sent,
            result.dry_run,
            result.message,
        ), flush=True)

    def _write_market_tick(self, tick: dict[str, Any]) -> None:
        try:
            self.logger.write_tick(self.symbol, tick)
        except Exception as exc:
            self.logger.write(
                "market_csv_write_error",
                {
                    "error": repr(exc),
                    "market_csv": str(self.logger.market_csv_path) if self.logger.market_csv_path else None,
                    "spool": str(self.logger.market_csv_spool_path) if self.logger.market_csv_spool_path else None,
                    "pending_rows": self.logger.market_writer.pending_count(),
                },
            )
            print("[qmt live] market CSV write failed; continuing: %s" % repr(exc), flush=True)
            return
        writer = self.logger.market_writer
        if writer.write_error_count > self.market_write_errors_reported:
            self.market_write_errors_reported = writer.write_error_count
            self.logger.write(
                "market_csv_write_error",
                {
                    "error": writer.last_error,
                    "market_csv": str(writer.last_path) if writer.last_path else None,
                    "spool": str(writer.last_spool_path) if writer.last_spool_path else None,
                    "pending_rows": writer.pending_count(),
                    "message": "main CSV is locked or unavailable; tick was spooled and runner continues",
                },
            )
            print(
                "[qmt live] market CSV locked; spooling ticks and continuing: %s" % writer.last_error,
                flush=True,
            )

    def _flush_market_pending(self) -> None:
        if not self.record_ticks:
            return
        pending_before = self.logger.market_writer.pending_count()
        if pending_before <= 0:
            return
        try:
            self.logger.market_writer.flush_pending()
        except Exception as exc:
            self.logger.write(
                "market_csv_flush_error",
                {
                    "error": repr(exc),
                    "pending_rows": self.logger.market_writer.pending_count(),
                    "spool": str(self.logger.market_csv_spool_path) if self.logger.market_csv_spool_path else None,
                },
            )
            print("[qmt live] market CSV pending flush failed: %s" % repr(exc), flush=True)
            return
        self.logger.write(
            "market_csv_flush_ok",
            {
                "flushed_rows": pending_before,
                "market_csv": str(self.logger.market_csv_path) if self.logger.market_csv_path else None,
            },
        )

    def _sync_account(self, price: float) -> AccountSnapshot:
        if self.gateway is None:
            raise RuntimeError("cannot sync account without trade gateway")
        snapshot = self.gateway.sync_strategy_state(self.strategy, self.symbol, mark_price=price)
        self.last_sync_ts = time.time()
        position = snapshot.position(self.symbol)
        print(
            "[qmt live] account sync cash=%.2f shares=%s can_use=%s total=%.2f"
            % (snapshot.cash, position.volume, position.can_use_volume, snapshot.total_asset),
            flush=True,
        )
        return snapshot

    def _sync_after_failed_order(self, price: float, phase: str, reason: str) -> None:
        if self.gateway is None or not hasattr(self.gateway, "sync_strategy_state"):
            return
        try:
            snapshot = self._sync_account(price)
        except Exception as exc:
            self.logger.write(
                "account_sync_exception",
                {
                    "phase": phase,
                    "after_order_failure": True,
                    "order_failure_reason": reason,
                    "error": repr(exc),
                },
            )
            return
        self.logger.write(
            "account_sync_after_order_failure",
            {
                "phase": phase,
                "order_failure_reason": reason,
                "cash": snapshot.cash,
                "total_asset": snapshot.total_asset,
                "position_volume": snapshot.position(self.symbol).volume,
            },
        )

    def _try_initial_position(self, tick: dict[str, Any], snapshot: AccountSnapshot) -> None:
        request = initial_base_order_request(self.symbol, snapshot, tick)
        if request is None:
            self.logger.write(
                "initial_position_wait",
                {
                    "reason": "invalid_ask_or_no_cash",
                    "total_asset": snapshot.total_asset,
                    "cash": snapshot.cash,
                    "sp1": tick.get("sp1", 0.0),
                },
            )
            return
        if is_limit_blocked("BUY", request.price, float(tick.get("prev_close", 0.0) or 0.0)):
            self.logger.write(
                "initial_position_wait",
                {
                    "reason": "limit_up_blocked",
                    "price": request.price,
                    "prev_close": tick.get("prev_close", 0.0),
                },
            )
            return
        self.logger.write(
            "initial_position_order",
            {
                "side": request.side,
                "symbol": request.symbol,
                "price": request.price,
                "shares": request.shares,
                "target_pct": INITIAL_STRATEGY_TARGET_PCT,
                "total_asset": snapshot.total_asset,
            },
        )
        try:
            result = self.gateway.place_order(request)
        except Exception as exc:
            self.logger.write(
                "order_exception",
                {
                    "phase": "initial_position",
                    "error": repr(exc),
                    "request": request.__dict__,
                },
            )
            print("[qmt live] initial_position order exception; continuing: %s" % repr(exc), flush=True)
            return
        self.logger.write("order_result", result.__dict__)
        if result.ok:
            self.order_count += 1
        if result.sent and result.order_id:
            self.pending_order_id = result.order_id
            self.pending_until_ts = time.time() + self.pending_timeout_seconds
        print(
            "[qmt live] initial_position order ok=%s sent=%s shares=%s price=%.2f message=%s"
            % (result.ok, result.sent, request.shares, request.price, result.message),
            flush=True,
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V6 with realtime QMT tick data and guarded live orders.")
    parser.add_argument("--symbol", default=TARGET_SYMBOL)
    parser.add_argument("--account-id", default=QMT_SIM_ACCOUNT)
    parser.add_argument("--duration-seconds", type=int, default=0, help="0 means run until Ctrl+C")
    parser.add_argument("--data-only", action="store_true", help="subscribe ticks only; do not connect trade account")
    parser.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-test-account",
        action="store_true",
        help="allow account testS when it is your external miniQMT simulated trading account",
    )
    parser.add_argument("--max-order-value", type=float, default=0.0)
    parser.add_argument("--max-shares-per-order", type=int, default=0)
    parser.add_argument("--min-order-interval-seconds", type=float, default=0.0)
    parser.add_argument("--sync-interval-seconds", type=float, default=30.0)
    parser.add_argument("--pending-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--print-every-ticks", type=int, default=200)
    parser.add_argument("--no-record-ticks", action="store_true", help="do not write realtime market CSV")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    live_orders_enabled = not args.data_only
    if live_orders_enabled:
        try:
            validate_live_account(args.account_id, data_only=args.data_only, allow_test_account=args.allow_test_account)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    logger = JsonlEventLogger(args.output_dir or None)
    gateway = None
    if not args.data_only:
        gateway = QmtTradeGateway(
            account_id=args.account_id,
            symbol=args.symbol,
            live_orders_enabled=live_orders_enabled,
            allow_test_account=args.allow_test_account,
            risk_limits=LiveRiskLimits(
                max_order_value=args.max_order_value,
                max_shares_per_order=args.max_shares_per_order,
                min_order_interval_seconds=args.min_order_interval_seconds,
            ),
            event_handler=logger.write,
        )
    feed = RealtimeTickFeed(symbol=args.symbol)
    engine = V6LiveEngine(
        symbol=args.symbol,
        gateway=gateway,
        feed=feed,
        logger=logger,
        sync_interval_seconds=args.sync_interval_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
        print_every_ticks=args.print_every_ticks,
        record_ticks=not args.no_record_ticks,
        data_only=args.data_only,
    )

    def _stop(_signum, _frame):
        engine.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    summary = engine.run(duration_seconds=args.duration_seconds)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
