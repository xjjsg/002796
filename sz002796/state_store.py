"""Runtime state persistence and position replay for the GUI.

Cash and shares are not trusted from manual configuration. On startup the store
replays the V6 backtest trade log plus any runtime trade log, then writes a
state snapshot only for strategy context such as cooldowns and regime state.
"""
import os
import csv
import json
from datetime import datetime
from typing import Any, Optional, Tuple, List, Dict

from .config import (
    INITIAL_CASH, INITIAL_SHARES, INITIAL_TARGET_PCT, INITIAL_CAPITAL,
    ANCHOR_PCT, LOT_SIZE, SYMBOL_CODE, BACKTEST_RECORD_DIR,
    BACKTEST_TRADE_LOG_FILE, parse_dt
)
from .position import PositionMode, TradeRecord
from .execution import calculate_trade_costs

class StrategyStateStore:
    def __init__(
        self,
        state_path: str,
        trade_log_path: str,
        seed_trade_log_path: str | None = BACKTEST_TRADE_LOG_FILE,
        seed_cash: float = INITIAL_CASH,
        seed_shares: int = INITIAL_SHARES,
        seed_target_pct: float = INITIAL_TARGET_PCT,
        seed_asset_base: float = INITIAL_CAPITAL,
    ):
        self.state_path = state_path
        self.trade_log_path = trade_log_path
        self.seed_trade_log_path = seed_trade_log_path
        self.seed_cash = seed_cash
        self.seed_shares = seed_shares
        self.seed_target_pct = seed_target_pct
        self.seed_asset_base = seed_asset_base
        self.ignored_state = None
        self.last_replay_info: dict = {"source": "none", "replayed_count": 0, "warnings": []}

    @staticmethod
    def _dt_to_text(value):
        if value is None:
            return None
        return value.isoformat()

    @staticmethod
    def _safe_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _trade_log_fieldnames() -> list[str]:
        return [
            "timestamp",
            "tick_time",
            "side",
            "price",
            "last_price",
            "shares",
            "amount",
            "commission",
            "stamp_tax",
            "position_shares",
            "cash_after",
            "asset_after",
            "position_pct_after",
            "target_pct",
            "mode",
            "day_trade_count",
            "reason",
            "detail",
            "day_vwap_dev",
            "local_vwap_dev",
            "velocity",
            "acceleration",
            "vol_mom",
            "day_return",
            "vwap",
            "local_vwap",
            "range_position",
            "orderbook_imbalance",
            "cross_buy_score",
            "cross_sell_score",
            "local_trim_score",
            "local_cover_score",
            "buy_timing_score",
            "sell_timing_score",
        ]

    @staticmethod
    def _read_csv_rows(path: str | None) -> list[dict]:
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def _read_trade_rows(self) -> list[dict]:
        return self._read_csv_rows(self.trade_log_path)

    def _seed_available(self) -> bool:
        return bool(self.seed_trade_log_path and os.path.exists(self.seed_trade_log_path))

    def _position_seed(self) -> tuple[float, int, float, float, str]:
        return self.seed_cash, self.seed_shares, self.seed_target_pct, self.seed_asset_base, "backtest_100w"

    @staticmethod
    def _trade_identity(row: dict) -> tuple[str, str, str, str, str, str]:
        return (
            str(row.get("timestamp") or row.get("time") or ""),
            str(row.get("side") or ""),
            str(row.get("price") or ""),
            str(row.get("shares") or ""),
            str(row.get("reason") or ""),
            str(row.get("detail") or ""),
        )

    def _read_position_rows(self) -> tuple[list[dict], dict]:
        seed_rows = self._read_csv_rows(self.seed_trade_log_path) if self._seed_available() else []
        runtime_rows = self._read_trade_rows()
        rows: list[dict] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        duplicate_count = 0

        for row in seed_rows + runtime_rows:
            key = self._trade_identity(row)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            rows.append(row)

        source = "none"
        if seed_rows and runtime_rows:
            source = "backtest+runtime"
        elif seed_rows:
            source = "backtest"
        elif runtime_rows:
            source = "runtime"

        return rows, {
            "source": source,
            "seed_rows": len(seed_rows),
            "runtime_rows": len(runtime_rows),
            "duplicate_rows": duplicate_count,
        }

    def _ensure_trade_log_schema(self, required_fields: list[str]) -> list[str]:
        if not os.path.exists(self.trade_log_path) or os.path.getsize(self.trade_log_path) == 0:
            return required_fields

        with open(self.trade_log_path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            old_fields = reader.fieldnames or []
            rows = list(reader)

        merged_fields = required_fields + [field for field in old_fields if field not in required_fields]
        if old_fields == merged_fields:
            return merged_fields

        tmp_path = self.trade_log_path + ".tmp"
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=merged_fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        os.replace(tmp_path, self.trade_log_path)
        return merged_fields

    @classmethod
    def _trade_sort_key(cls, item: tuple[int, dict]) -> tuple[datetime, int]:
        idx, row = item
        timestamp = parse_dt(row.get("timestamp") or row.get("time"))
        return timestamp or datetime.max, idx

    @classmethod
    def _apply_trade_row(
        cls,
        row: dict,
        cash: float,
        shares: int,
        last_target_pct: float,
        last_mode: PositionMode,
    ) -> tuple[float, int, float, PositionMode, TradeRecord | None, dict, str | None]:
        side = str(row.get("side", "") or "").upper()
        price = float(row.get("price", 0.0) or 0.0)
        requested_shares = int(float(row.get("shares", 0) or 0))
        traded_shares = int(requested_shares / LOT_SIZE) * LOT_SIZE
        timestamp = parse_dt(row.get("timestamp") or row.get("time")) or datetime.now()
        target_pct = cls._safe_float(row.get("target_pct"))
        if target_pct is None:
            target_pct = cls._safe_float(row.get("position_pct"))
        if target_pct is None:
            target_pct = last_target_pct

        mode_value = str(row.get("mode", "") or "")
        try:
            mode = PositionMode(mode_value) if mode_value else cls._mode_from_target_pct(target_pct)
        except ValueError:
            mode = cls._mode_from_target_pct(target_pct)

        reason = str(row.get("reason", "") or "")
        detail = str(row.get("detail", "") or "")
        warning = None

        if side not in {"BUY", "SELL"} or price <= 0 or traded_shares <= 0:
            return cash, shares, target_pct, mode, None, {}, f"ignored invalid trade row at {timestamp.isoformat()}"

        if side == "SELL" and traded_shares > shares:
            warning = f"sell shares capped from {traded_shares} to {shares} at {timestamp.isoformat()}"
            traded_shares = int(shares / LOT_SIZE) * LOT_SIZE
            if traded_shares <= 0:
                return cash, shares, target_pct, mode, None, {}, warning

        costs = calculate_trade_costs(side, price, traded_shares)
        if side == "BUY":
            cash -= costs.buy_cash_required
            shares += traded_shares
        else:
            cash += costs.sell_cash_received
            shares -= traded_shares

        record = TradeRecord(
            timestamp=timestamp,
            side=side,
            price=price,
            shares=traded_shares,
            position_shares=shares,
            cash_after=cash,
            target_pct=target_pct,
            mode=mode.value,
            reason=reason,
            detail=detail,
        )
        cost_row = {
            "shares": traded_shares,
            "amount": costs.amount,
            "commission": costs.commission,
            "stamp_tax": costs.stamp_tax,
            "position_shares": shares,
            "cash_after": cash,
            "target_pct": target_pct,
            "mode": mode.value,
        }
        return cash, shares, target_pct, mode, record, cost_row, warning

    @classmethod
    def _replay_trade_rows(
        cls,
        rows: list[dict],
        initial_cash: float = INITIAL_CASH,
        initial_shares: int = INITIAL_SHARES,
        initial_target_pct: float = INITIAL_TARGET_PCT,
    ) -> tuple[float, int, float, PositionMode, list[TradeRecord], list[str]]:
        cash = initial_cash
        shares = initial_shares
        target_pct = initial_target_pct
        mode = cls._mode_from_target_pct(target_pct)
        records: list[TradeRecord] = []
        warnings: list[str] = []

        for _, row in sorted(enumerate(rows), key=cls._trade_sort_key):
            cash, shares, target_pct, mode, record, _, warning = cls._apply_trade_row(
                row,
                cash,
                shares,
                target_pct,
                mode,
            )
            if warning:
                warnings.append(warning)
            if record is not None:
                records.append(record)
        return cash, shares, target_pct, mode, records, warnings

    @staticmethod
    def _mode_from_target_pct(target_pct: float) -> PositionMode:
        if target_pct < ANCHOR_PCT - 0.03:
            return PositionMode.DEFENSE
        if target_pct > ANCHOR_PCT + 0.03:
            return PositionMode.ATTACK
        return PositionMode.NEUTRAL

    @staticmethod
    def _trade_to_dict(
        trade: TradeRecord,
        strategy: Any | None = None,
        tick: dict | None = None,
    ) -> dict:
        row = {
            "timestamp": trade.timestamp.isoformat(),
            "side": trade.side,
            "price": trade.price,
            "shares": trade.shares,
            "position_shares": trade.position_shares,
            "cash_after": trade.cash_after,
            "target_pct": trade.target_pct,
            "mode": trade.mode,
            "reason": trade.reason,
            "detail": trade.detail,
        }
        if tick:
            row["tick_time"] = tick.get("server_time", "")
            row["last_price"] = tick.get("price", tick.get("Close", ""))

        if strategy is not None:
            current_price = float(row.get("last_price") or trade.price)
            row["asset_after"] = strategy.total_asset(current_price)
            row["position_pct_after"] = strategy.current_position_pct(current_price)
            row["day_trade_count"] = strategy.day_trade_count
            snapshot = strategy.factor_calc.last_snapshot
            if snapshot is not None:
                row.update(
                    {
                        "day_vwap_dev": snapshot.day_vwap_dev,
                        "local_vwap_dev": snapshot.local_vwap_dev,
                        "velocity": snapshot.velocity,
                        "acceleration": snapshot.acceleration,
                        "vol_mom": snapshot.vol_mom,
                        "day_return": snapshot.day_return,
                        "vwap": snapshot.vwap,
                        "local_vwap": snapshot.local_vwap,
                        "range_position": snapshot.range_position,
                        "orderbook_imbalance": snapshot.orderbook_imbalance,
                        "cross_buy_score": strategy._score_cross_buy(snapshot),
                        "cross_sell_score": strategy._score_cross_sell(snapshot),
                        "local_trim_score": strategy._score_local_trim(snapshot),
                        "local_cover_score": strategy._score_local_cover(snapshot),
                        "buy_timing_score": strategy._score_buy_timing(snapshot),
                        "sell_timing_score": strategy._score_sell_timing(snapshot),
                    }
                )
        return row

    @staticmethod
    def _trade_from_dict(row: dict) -> TradeRecord:
        timestamp = parse_dt(row.get("timestamp") or row.get("time")) or datetime.now()
        return TradeRecord(
            timestamp=timestamp,
            side=str(row.get("side", "")),
            price=float(row.get("price", 0.0) or 0.0),
            shares=int(row.get("shares", 0) or 0),
            position_shares=int(row.get("position_shares", 0) or 0),
            cash_after=float(row.get("cash_after", 0.0) or 0.0),
            target_pct=float(row.get("target_pct", 0.0) or 0.0),
            mode=str(row.get("mode", PositionMode.NEUTRAL.value)),
            reason=str(row.get("reason", "")),
            detail=str(row.get("detail", "")),
        )

    def reconcile_from_trade_log(self, strategy: Any, state: dict | None = None) -> dict:
        rows, source_info = self._read_position_rows()
        seed_cash, seed_shares, seed_target_pct, seed_asset_base, seed_name = self._position_seed()
        source = source_info["source"]

        if not rows:
            self.last_replay_info = {
                "source": "none",
                "position_seed": seed_name,
                "asset_base": seed_asset_base,
                "replayed_count": 0,
                "warnings": [],
            }
            return self.last_replay_info

        cash, shares, target_pct, mode, records, warnings = self._replay_trade_rows(
            rows,
            initial_cash=seed_cash,
            initial_shares=seed_shares,
            initial_target_pct=seed_target_pct,
        )
        if records:
            strategy.initial_capital = seed_asset_base
            strategy.cash = cash
            strategy.shares = shares
            strategy.target_pct = target_pct
            strategy.mode = mode
            strategy.trades = records
            strategy.local_base_target_pct = target_pct
            strategy.last_trade_dt = records[-1].timestamp
            last_trade_date = records[-1].timestamp.strftime("%Y-%m-%d")
            if not strategy.current_date:
                strategy.current_date = last_trade_date
            if strategy.current_date == last_trade_date:
                strategy.day_trade_count = sum(
                    1 for record in records if record.timestamp.strftime("%Y-%m-%d") == last_trade_date
                )

        self.last_replay_info = {
            "source": source,
            "position_seed": seed_name,
            "seed_cash": seed_cash,
            "seed_shares": seed_shares,
            "seed_target_pct": seed_target_pct,
            "asset_base": seed_asset_base,
            "seed_rows": source_info.get("seed_rows", 0),
            "runtime_rows": source_info.get("runtime_rows", 0),
            "duplicate_rows": source_info.get("duplicate_rows", 0),
            "replayed_count": len(records),
            "warnings": warnings,
            "cash": strategy.cash,
            "shares": strategy.shares,
            "target_pct": strategy.target_pct,
            "mode": strategy.mode.value,
        }
        return self.last_replay_info

    def _loaded_state_from_replay(self, strategy: Any, reason: str) -> dict | None:
        replay_info = self.reconcile_from_trade_log(strategy)
        if replay_info.get("replayed_count", 0) <= 0:
            return None
        return {
            "strategy_version": "v6",
            "symbol": SYMBOL_CODE,
            "saved_at": "-",
            "save_reason": reason,
            "cash": strategy.cash,
            "shares": strategy.shares,
            "target_pct": strategy.target_pct,
            "mode": strategy.mode.value,
            "position_replay": replay_info,
        }

    def load(self, strategy: Any) -> dict | None:
        self.ignored_state = None
        if not os.path.exists(self.state_path):
            return self._loaded_state_from_replay(strategy, "trade_log_replay")

        with open(self.state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        if state.get("symbol") != SYMBOL_CODE:
            raise ValueError(f"state symbol mismatch: {state.get('symbol')} != {SYMBOL_CODE}")

        if state.get("strategy_version") != "v6":
            self.ignored_state = dict(state)
            self.ignored_state["ignored_reason"] = "state strategy_version is not v6"
            return self._loaded_state_from_replay(strategy, "ignored_state_trade_log_replay")

        strategy.cash = float(state.get("cash", strategy.cash) or 0.0)
        strategy.shares = int(state.get("shares", strategy.shares) or 0)
        strategy.target_pct = float(state.get("target_pct", strategy.target_pct) or 0.0)
        try:
            strategy.mode = PositionMode(state.get("mode", strategy.mode.value))
        except ValueError:
            strategy.mode = strategy._mode_from_target(strategy.target_pct)

        strategy.current_date = state.get("current_date")
        strategy.day_trade_count = int(state.get("day_trade_count", 0) or 0)
        strategy.last_trade_dt = parse_dt(state.get("last_trade_dt"))
        strategy.local_base_target_pct = state.get("local_base_target_pct")
        pending_cross_buy = state.get("pending_cross_buy")
        strategy.pending_cross_buy = tuple(pending_cross_buy) if pending_cross_buy else None
        strategy.local_t_cycle = state.get("local_t_cycle")
        strategy.local_t_cycle_base_pct = state.get("local_t_cycle_base_pct")
        strategy.local_t_entry_price = state.get("local_t_entry_price")
        strategy.local_t_entry_shares = int(state.get("local_t_entry_shares", 0) or 0)
        strategy.main_flow_guard_date = state.get("main_flow_guard_date")
        strategy.main_flow_guard_floor_pct = state.get("main_flow_guard_floor_pct")
        if hasattr(strategy.market_regime, "load_state"):
            strategy.market_regime.load_state(state.get("market_regime"))
        if strategy.market_regime.last_decision is not None:
            strategy.regime_decision = strategy.market_regime.last_decision
        strategy.trades = [self._trade_from_dict(row) for row in state.get("trades", [])]

        replay_info = self.reconcile_from_trade_log(strategy, state)
        if replay_info.get("replayed_count", 0) > 0:
            state = dict(state)
            state["cash"] = strategy.cash
            state["shares"] = strategy.shares
            state["target_pct"] = strategy.target_pct
            state["mode"] = strategy.mode.value
            state["position_replay"] = replay_info

        return state

    def save(self, strategy: Any, tick: dict | None = None, reason: str = "snapshot") -> None:
        last_price = None
        last_tick_time = None
        if tick:
            last_price = tick.get("price", tick.get("Close"))
            tick_time = tick.get("Time")
            if isinstance(tick_time, datetime):
                last_tick_time = tick_time.isoformat()
            elif tick_time:
                last_tick_time = str(tick_time)

        state = {
            "version": 2,
            "strategy_version": "v6",
            "symbol": SYMBOL_CODE,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "save_reason": reason,
            "initial_capital": INITIAL_CAPITAL,
            "asset_base": strategy.initial_capital,
            "cash": strategy.cash,
            "shares": strategy.shares,
            "target_pct": strategy.target_pct,
            "mode": strategy.mode.value,
            "current_date": strategy.current_date,
            "day_trade_count": strategy.day_trade_count,
            "last_trade_dt": self._dt_to_text(strategy.last_trade_dt),
            "local_base_target_pct": strategy.local_base_target_pct,
            "pending_cross_buy": list(strategy.pending_cross_buy) if strategy.pending_cross_buy else None,
            "local_t_cycle": strategy.local_t_cycle,
            "local_t_cycle_base_pct": strategy.local_t_cycle_base_pct,
            "local_t_entry_price": strategy.local_t_entry_price,
            "local_t_entry_shares": strategy.local_t_entry_shares,
            "main_flow_guard_date": strategy.main_flow_guard_date,
            "main_flow_guard_floor_pct": strategy.main_flow_guard_floor_pct,
            "market_regime": strategy.market_regime.export_state()
            if hasattr(strategy.market_regime, "export_state")
            else None,
            "last_price": last_price,
            "last_tick_time": last_tick_time,
            "trades": [self._trade_to_dict(trade) for trade in strategy.trades],
            "position_replay": self.last_replay_info,
            "position_seed": {
                "seed_trade_log_path": self.seed_trade_log_path,
                "seed_cash": self.seed_cash,
                "seed_shares": self.seed_shares,
                "seed_target_pct": self.seed_target_pct,
                "seed_asset_base": self.seed_asset_base,
            },
        }

        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.state_path)

    def append_trade(
        self,
        trade: TradeRecord,
        strategy: Any | None = None,
        tick: dict | None = None,
    ) -> TradeRecord:
        required_fieldnames = self._trade_log_fieldnames()
        fieldnames = self._ensure_trade_log_schema(required_fieldnames)
        file_exists = os.path.exists(self.trade_log_path) and os.path.getsize(self.trade_log_path) > 0
        row = self._trade_to_dict(trade, strategy=strategy, tick=tick)
        prior_rows, _ = self._read_position_rows()
        seed_cash, seed_shares, seed_target_pct, _, seed_name = self._position_seed()
        replayed_position = self._replay_trade_rows(
            prior_rows,
            initial_cash=seed_cash,
            initial_shares=seed_shares,
            initial_target_pct=seed_target_pct,
        )
        cash, shares, target_pct, mode, corrected_trade, cost_row, warning = self._apply_trade_row(
            row,
            *replayed_position[:4],
        )
        if warning:
            self.last_replay_info = {
                "source": "runtime",
                "position_seed": seed_name,
                "replayed_count": len(replayed_position[4]),
                "warnings": [warning],
            }
        if corrected_trade is None:
            corrected_trade = trade
            cost_row = {}

        row.update(cost_row)
        row["position_shares"] = corrected_trade.position_shares
        row["cash_after"] = corrected_trade.cash_after
        if tick:
            current_price = float(tick.get("price", tick.get("Close", corrected_trade.price)) or corrected_trade.price)
            asset = corrected_trade.cash_after + corrected_trade.position_shares * current_price
            row["asset_after"] = asset
            row["position_pct_after"] = (
                corrected_trade.position_shares * current_price / asset if asset > 0 else 0.0
            )

        with open(self.trade_log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fieldnames})
        return corrected_trade

