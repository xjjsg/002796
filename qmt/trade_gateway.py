"""miniQMT trade gateway with explicit live-order guards."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sz002796.config import LOT_SIZE

from .config import ACCOUNT_ID, MINI_QMT_PATH, QMT_BACKTEST_ACCOUNT, TARGET_SYMBOL
from .xtquant_env import ensure_xtquant_path, import_xttrader_modules, xtquant_compatibility


class LiveOrderRejected(RuntimeError):
    """Raised when a live order is blocked by local safety checks."""


@dataclass(frozen=True)
class PositionSnapshot:
    stock_code: str
    volume: int = 0
    can_use_volume: int = 0
    open_price: float = 0.0
    market_value: float = 0.0
    frozen_volume: int = 0
    on_road_volume: int = 0
    yesterday_volume: int = 0


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    cash: float = 0.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    total_asset: float = 0.0
    positions: dict[str, PositionSnapshot] = field(default_factory=dict)

    def position(self, symbol: str) -> PositionSnapshot:
        return self.positions.get(symbol, PositionSnapshot(stock_code=symbol))


@dataclass(frozen=True)
class OrderRequest:
    side: str
    symbol: str
    price: float
    shares: int
    strategy_name: str = "v6_live"
    remark: str = "002796_v6"
    price_type_name: str = "FIX_PRICE"


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    sent: bool
    dry_run: bool
    side: str
    symbol: str
    price: float
    shares: int
    order_id: int | None = None
    message: str = ""


@dataclass(frozen=True)
class LiveRiskLimits:
    max_order_value: float = 100000.0
    max_shares_per_order: int = 20000
    min_order_interval_seconds: float = 30.0
    forbid_backtest_account: bool = True


def _float_attr(obj: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(obj, name, default) or 0.0)
    except (TypeError, ValueError):
        return default


def _int_attr(obj: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(obj, name, default) or 0)
    except (TypeError, ValueError):
        return default


def position_from_qmt(obj: Any) -> PositionSnapshot:
    return PositionSnapshot(
        stock_code=str(getattr(obj, "stock_code", "") or ""),
        volume=_int_attr(obj, "volume"),
        can_use_volume=_int_attr(obj, "can_use_volume"),
        open_price=_float_attr(obj, "open_price"),
        market_value=_float_attr(obj, "market_value"),
        frozen_volume=_int_attr(obj, "frozen_volume"),
        on_road_volume=_int_attr(obj, "on_road_volume"),
        yesterday_volume=_int_attr(obj, "yesterday_volume"),
    )


class QmtTradeGateway:
    """Connection and order wrapper for miniQMT external xttrader."""

    def __init__(
        self,
        account_id: str = ACCOUNT_ID,
        symbol: str = TARGET_SYMBOL,
        live_orders_enabled: bool = False,
        allow_test_account: bool = False,
        risk_limits: LiveRiskLimits | None = None,
        event_handler: Callable[[str, dict[str, Any]], None] | None = None,
        session_id: int | None = None,
    ):
        self.account_id = account_id
        self.symbol = symbol
        self.live_orders_enabled = bool(live_orders_enabled)
        self.allow_test_account = bool(allow_test_account)
        self.risk_limits = risk_limits or LiveRiskLimits()
        if self.allow_test_account:
            self.risk_limits = LiveRiskLimits(
                max_order_value=self.risk_limits.max_order_value,
                max_shares_per_order=self.risk_limits.max_shares_per_order,
                min_order_interval_seconds=self.risk_limits.min_order_interval_seconds,
                forbid_backtest_account=False,
            )
        self.event_handler = event_handler
        self.session_id = session_id or random.randint(100000, 999999)
        self.trader: Any | None = None
        self.account: Any | None = None
        self.xttype: Any | None = None
        self.connected = False
        self.subscribed = False
        self.last_order_time = 0.0
        self.last_snapshot: AccountSnapshot | None = None

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_handler is not None:
            self.event_handler(event_type, payload)

    def connect(self) -> dict[str, Any]:
        site_path = ensure_xtquant_path()
        compatibility = xtquant_compatibility(site_path)
        if not compatibility["matching_native_module"]:
            raise RuntimeError(
                "current Python cannot load miniQMT xtquant native module; "
                f"python={compatibility['python_version']} expected={compatibility['expected_native_tag']}"
            )
        if not self.account_id:
            raise RuntimeError("live gateway requires a QMT stock account id")
        if self.account_id == QMT_BACKTEST_ACCOUNT and not self.allow_test_account:
            raise RuntimeError(
                "account testS is treated as the miniQMT model-backtest account by default; "
                "set allow_test_account=True only if testS is your external simulated trading account"
            )

        xttrader, xttype = import_xttrader_modules(site_path)
        self.xttype = xttype
        gateway = self

        class _Callback(xttrader.XtQuantTraderCallback):
            def on_connected(self):
                gateway.emit("connected", {"account_id": gateway.account_id})

            def on_disconnected(self):
                gateway.connected = False
                gateway.emit("disconnected", {"account_id": gateway.account_id})

            def on_stock_order(self, order):
                gateway.emit("stock_order", _object_public_dict(order))

            def on_stock_trade(self, trade):
                gateway.emit("stock_trade", _object_public_dict(trade))

            def on_order_error(self, order_error):
                gateway.emit("order_error", _object_public_dict(order_error))

            def on_cancel_error(self, cancel_error):
                gateway.emit("cancel_error", _object_public_dict(cancel_error))

            def on_order_stock_async_response(self, response):
                gateway.emit("order_async_response", _object_public_dict(response))

        self.trader = xttrader.XtQuantTrader(str(MINI_QMT_PATH), self.session_id, callback=_Callback())
        self.account = xttype.StockAccount(self.account_id)
        self.trader.start()
        connect_result = self.trader.connect()
        self.connected = connect_result == 0
        subscribe_result = self.trader.subscribe(self.account) if self.connected else None
        self.subscribed = subscribe_result == 0
        info = {
            "account_id": self.account_id,
            "session_id": self.session_id,
            "connect_result": connect_result,
            "subscribe_result": subscribe_result,
            "connected": self.connected,
            "subscribed": self.subscribed,
            "live_orders_enabled": self.live_orders_enabled,
            "allow_test_account": self.allow_test_account,
        }
        self.emit("trade_gateway_connected", info)
        if not self.connected or not self.subscribed:
            try:
                self.disconnect()
            finally:
                raise RuntimeError(f"miniQMT trade connection failed: {info}")
        return info

    def disconnect(self) -> None:
        if self.trader is None:
            return
        try:
            if self.subscribed and self.account is not None:
                self.trader.unsubscribe(self.account)
        except Exception as exc:
            self.emit("unsubscribe_error", {"error": repr(exc)})
        try:
            self.trader.stop()
        except Exception as exc:
            self.emit("trade_gateway_stop_error", {"error": repr(exc)})
        finally:
            self.connected = False
            self.subscribed = False

    def query_snapshot(self) -> AccountSnapshot:
        if self.trader is None or self.account is None:
            raise RuntimeError("trade gateway is not connected")
        asset = self.trader.query_stock_asset(self.account)
        positions = self.trader.query_stock_positions(self.account) or []
        position_map = {
            pos.stock_code: position_from_qmt(pos)
            for pos in positions
            if getattr(pos, "stock_code", None)
        }
        snapshot = AccountSnapshot(
            account_id=self.account_id,
            cash=_float_attr(asset, "cash"),
            frozen_cash=_float_attr(asset, "frozen_cash"),
            market_value=_float_attr(asset, "market_value"),
            total_asset=_float_attr(asset, "total_asset"),
            positions=position_map,
        )
        self.last_snapshot = snapshot
        self.emit(
            "account_snapshot",
            {
                "account_id": snapshot.account_id,
                "cash": snapshot.cash,
                "frozen_cash": snapshot.frozen_cash,
                "market_value": snapshot.market_value,
                "total_asset": snapshot.total_asset,
                "target_position": snapshot.position(self.symbol).__dict__,
            },
        )
        return snapshot

    def sync_strategy_state(self, strategy: Any, symbol: str | None = None, mark_price: float | None = None) -> AccountSnapshot:
        symbol = symbol or self.symbol
        snapshot = self.query_snapshot()
        position = snapshot.position(symbol)
        strategy.cash = snapshot.cash
        strategy.shares = position.volume
        total_asset = snapshot.total_asset
        if total_asset <= 0 and mark_price and mark_price > 0:
            total_asset = snapshot.cash + position.volume * mark_price
        if total_asset > 0:
            strategy.initial_capital = total_asset
        if mark_price and mark_price > 0:
            target_pct = strategy.current_position_pct(mark_price)
            strategy.target_pct = target_pct
            if hasattr(strategy, "local_base_target_pct"):
                strategy.local_base_target_pct = target_pct
        if hasattr(strategy, "_position_built"):
            strategy._position_built = True
            strategy.enable_local_t = getattr(strategy, "_normal_enable_local_t", strategy.enable_local_t)
        return snapshot

    def place_order(self, request: OrderRequest) -> OrderResult:
        side = request.side.upper()
        shares = int(request.shares / LOT_SIZE) * LOT_SIZE
        checked = OrderRequest(
            side=side,
            symbol=request.symbol,
            price=round(float(request.price), 2),
            shares=shares,
            strategy_name=request.strategy_name,
            remark=request.remark,
            price_type_name=request.price_type_name,
        )
        blocked = self._validate_order(checked)
        if blocked:
            return OrderResult(
                ok=False,
                sent=False,
                dry_run=not self.live_orders_enabled,
                side=checked.side,
                symbol=checked.symbol,
                price=checked.price,
                shares=checked.shares,
                message=blocked,
            )
        if not self.live_orders_enabled:
            result = OrderResult(
                ok=True,
                sent=False,
                dry_run=True,
                side=checked.side,
                symbol=checked.symbol,
                price=checked.price,
                shares=checked.shares,
                message="dry-run: live_orders_enabled is false; order_stock not called",
            )
            self.emit("order_dry_run", result.__dict__)
            return result

        if self.trader is None or self.account is None or self.xttype is None:
            raise RuntimeError("trade gateway is not connected")
        order_type = self.xttype.STOCK_BUY if checked.side == "BUY" else self.xttype.STOCK_SELL
        price_type = getattr(self.xttype, checked.price_type_name)
        order_id = self.trader.order_stock(
            self.account,
            checked.symbol,
            order_type,
            checked.shares,
            price_type,
            checked.price,
            checked.strategy_name,
            checked.remark,
        )
        self.last_order_time = time.time()
        result = OrderResult(
            ok=bool(order_id and int(order_id) > 0),
            sent=True,
            dry_run=False,
            side=checked.side,
            symbol=checked.symbol,
            price=checked.price,
            shares=checked.shares,
            order_id=int(order_id) if order_id is not None else None,
            message="order_stock returned order_id=%s" % order_id,
        )
        self.emit("order_sent", result.__dict__)
        return result

    def cancel_order(self, order_id: int) -> int:
        if self.trader is None or self.account is None:
            raise RuntimeError("trade gateway is not connected")
        result = int(self.trader.cancel_order_stock(self.account, int(order_id)))
        self.emit("cancel_order", {"order_id": int(order_id), "result": result})
        return result

    def _validate_order(self, request: OrderRequest) -> str:
        if request.side not in {"BUY", "SELL"}:
            return "side must be BUY or SELL"
        if request.price <= 0:
            return "price must be positive"
        if request.shares <= 0 or request.shares % LOT_SIZE != 0:
            return "shares must be a positive board-lot quantity"
        order_value = request.price * request.shares
        if self.risk_limits.max_order_value > 0 and order_value > self.risk_limits.max_order_value:
            return "order value %.2f exceeds max_order_value %.2f" % (
                order_value,
                self.risk_limits.max_order_value,
            )
        if self.risk_limits.max_shares_per_order > 0 and request.shares > self.risk_limits.max_shares_per_order:
            return "shares %s exceeds max_shares_per_order %s" % (
                request.shares,
                self.risk_limits.max_shares_per_order,
            )
        elapsed = time.time() - self.last_order_time
        if self.live_orders_enabled and elapsed < self.risk_limits.min_order_interval_seconds:
            return "blocked by min_order_interval_seconds %.1f" % self.risk_limits.min_order_interval_seconds
        if self.live_orders_enabled and self.risk_limits.forbid_backtest_account and self.account_id == QMT_BACKTEST_ACCOUNT:
            return "refusing to send live order through backtest account testS"
        if self.live_orders_enabled:
            snapshot = self.query_snapshot()
            if request.side == "BUY" and snapshot.cash < order_value:
                return "cash %.2f is below order value %.2f" % (snapshot.cash, order_value)
            if request.side == "SELL":
                can_use = snapshot.position(request.symbol).can_use_volume
                if can_use < request.shares:
                    return "can_use_volume %s is below sell shares %s" % (can_use, request.shares)
        return ""


def _object_public_dict(obj: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        result[name] = value
    return result
