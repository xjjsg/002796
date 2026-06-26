"""Runtime state persistence and position replay for the web runtime.

Cash and shares are not trusted from manual configuration. On startup the store
replays the V6 backtest trade log plus any runtime trade log, then writes a
state snapshot only for strategy context such as cooldowns and regime state.
"""
import os
import csv
import json
from datetime import datetime
from typing import Any

from .config import (
    INITIAL_CASH, INITIAL_SHARES, INITIAL_TARGET_PCT, INITIAL_CAPITAL,
    ANCHOR_PCT, LOT_SIZE, SYMBOL_CODE, BACKTEST_RECORD_DIR,
    BACKTEST_TRADE_LOG_FILE, parse_dt
)
from .position import PositionMode, TradeRecord
from .trade_records import (
    TRADE_LOG_COLUMNS,
    apply_trade_row,
    ensure_trade_log_schema,
    mode_from_target_pct,
    read_trade_rows,
    replay_trade_rows,
    trade_from_dict,
    trade_identity,
    trade_to_dict,
)

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
        return TRADE_LOG_COLUMNS

    @staticmethod
    def _read_csv_rows(path: str | None) -> list[dict]:
        return read_trade_rows(path)

    def _read_trade_rows(self) -> list[dict]:
        return self._read_csv_rows(self.trade_log_path)

    def _seed_available(self) -> bool:
        return bool(self.seed_trade_log_path and os.path.exists(self.seed_trade_log_path))

    def _position_seed(self) -> tuple[float, int, float, float, str]:
        return self.seed_cash, self.seed_shares, self.seed_target_pct, self.seed_asset_base, "backtest_100w"

    @staticmethod
    def _trade_identity(row: dict) -> tuple[str, str, str, str, str, str]:
        return trade_identity(row)

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
        return ensure_trade_log_schema(self.trade_log_path, required_fields)

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
        return apply_trade_row(row, cash, shares, last_target_pct, last_mode)

    @classmethod
    def _replay_trade_rows(
        cls,
        rows: list[dict],
        initial_cash: float = INITIAL_CASH,
        initial_shares: int = INITIAL_SHARES,
        initial_target_pct: float = INITIAL_TARGET_PCT,
    ) -> tuple[float, int, float, PositionMode, list[TradeRecord], list[str]]:
        return replay_trade_rows(rows, initial_cash, initial_shares, initial_target_pct)

    @staticmethod
    def _mode_from_target_pct(target_pct: float) -> PositionMode:
        return mode_from_target_pct(target_pct)

    @staticmethod
    def _trade_to_dict(
        trade: TradeRecord,
        strategy: Any | None = None,
        tick: dict | None = None,
    ) -> dict:
        return trade_to_dict(trade, strategy=strategy, tick=tick, source="runtime")

    @staticmethod
    def _trade_from_dict(row: dict) -> TradeRecord:
        return trade_from_dict(row)

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

