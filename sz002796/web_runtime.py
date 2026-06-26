"""In-process runtime and read models for the aiohttp dashboard server."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import (
    BACKTEST_RECORD_DIR,
    BACKTEST_TRADE_LOG_FILE,
    DATA_DIR,
    INITIAL_CAPITAL,
    SYMBOL_CODE,
    SYMBOL_NAME,
    TRADE_LOG_FILE,
    WEB_MARKET_SOURCE,
    parse_dt,
)
from .dashboard import trade_row_to_payload
from .live_engine import worker_thread
from .realtime_sources import MARKET_SOURCE_OPTIONS, normalize_market_source_id
from .trade_records import read_trade_rows, trade_identity


DATA_FILE_RE = re.compile(r"^sz002796-(\d{4}-\d{2}-\d{2})\.csv$")


def load_backtest_summary() -> dict[str, Any]:
    path = Path(BACKTEST_RECORD_DIR) / "summary.json"
    if not path.exists():
        return {"available": False, "path": str(path)}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["available"] = True
    data["path"] = str(path)
    return data


def load_trade_history(limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for path, source in (
        (BACKTEST_TRADE_LOG_FILE, "backtest"),
        (TRADE_LOG_FILE, "runtime"),
    ):
        for original in read_trade_rows(path):
            row = dict(original)
            row["source"] = row.get("source") or source
            key = trade_identity(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    rows.sort(key=lambda item: parse_dt(item.get("timestamp")) or datetime.min, reverse=True)
    return [trade_row_to_payload(row) for row in rows[: max(1, int(limit))]]


def load_data_status() -> dict[str, Any]:
    root = Path(DATA_DIR)
    files: list[tuple[str, Path]] = []
    if root.exists():
        for path in root.iterdir():
            match = DATA_FILE_RE.match(path.name)
            if match and path.is_file():
                files.append((match.group(1), path))
    files.sort(key=lambda item: item[0])
    summary = load_backtest_summary()
    return {
        "directory": str(root),
        "fileCount": len(files),
        "firstDate": files[0][0] if files else "",
        "lastDate": files[-1][0] if files else "",
        "totalBytes": sum(path.stat().st_size for _, path in files),
        "latestModifiedAt": (
            datetime.fromtimestamp(max(path.stat().st_mtime for _, path in files)).isoformat(timespec="seconds")
            if files
            else ""
        ),
        "knownWarnings": summary.get("known_data_quality_warnings", []),
        "runtimeStateExists": (root / f"{SYMBOL_CODE}_v6_strategy_state.json").exists(),
        "runtimeTradesExists": Path(TRADE_LOG_FILE).exists(),
    }


def build_idle_snapshot() -> dict[str, Any]:
    summary = load_backtest_summary()
    trades = load_trade_history(limit=1)
    latest_trade = trades[0] if trades else None
    price = float(latest_trade.get("price", 0.0) if latest_trade else 0.0)
    if latest_trade:
        cash = float(latest_trade.get("cashAfter", 0.0) or 0.0)
        shares = int(latest_trade.get("positionShares", 0) or 0)
        equity = cash + shares * price
        position_pct = float(latest_trade.get("positionAfter", 0.0) or 0.0)
    else:
        equity = float(summary.get("strategy_final_asset", INITIAL_CAPITAL) or INITIAL_CAPITAL)
        cash = float(summary.get("final_cash", INITIAL_CAPITAL) or INITIAL_CAPITAL)
        shares = int(summary.get("final_shares", 0) or 0)
        position_pct = float(summary.get("final_position_pct", 0.0) or 0.0)
    return {
        "type": "snapshot",
        "status": "IDLE",
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "symbol": {"code": "002796.SZ", "sourceCode": SYMBOL_CODE, "name": SYMBOL_NAME},
        "feed": {
            "requestedSource": normalize_market_source_id(WEB_MARKET_SOURCE),
            "activeSource": "",
            "label": "未启动",
            "fallback": False,
            "lastTick": "",
        },
        "quote": {
            "price": price,
            "changePct": 0.0,
            "prevClose": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "tickVolume": 0.0,
            "vwap": 0.0,
            "localVwap": 0.0,
        },
        "account": {
            "shares": shares,
            "cash": cash,
            "equity": equity,
            "pnl": equity - INITIAL_CAPITAL,
            "pnlPct": equity / INITIAL_CAPITAL - 1.0,
            "positionPct": position_pct,
            "targetPct": float(latest_trade.get("targetPct", position_pct) if latest_trade else position_pct),
            "floorPct": 0.40,
            "ceilingPct": 1.00,
            "mode": str(latest_trade.get("mode", "NEUTRAL") if latest_trade else "NEUTRAL"),
            "dayTradeCount": 0,
            "maxDayTrades": 5,
            "lastTradeTime": str(latest_trade.get("timestamp", "") if latest_trade else ""),
            "localCycle": "none",
            "localBasePct": None,
            "localEntryPrice": None,
            "localEntryShares": 0,
        },
        "regime": {
            "name": "UNKNOWN",
            "score": 0.0,
            "confidence": 0.0,
            "floorPct": 0.40,
            "ceilingPct": 1.00,
            "detail": "启动行情后显示实时市场状态。",
            "tags": [],
            "allowCrossDay": True,
            "allowLocalT": True,
        },
        "decision": {
            "action": "HOLD",
            "state": "idle",
            "headline": "等待启动",
            "reason": "当前展示最近一次回测账户快照。",
            "detail": "选择行情源并启动后，实时决策将通过 WebSocket 更新。",
            "leadingSignal": None,
            "restrictions": [],
        },
        "signals": [],
        "factors": [],
        "orderbook": {"asks": [], "bids": [], "imbalance": 0.0},
        "trade": None,
        "chart": [],
    }


class DashboardRuntime:
    def __init__(self) -> None:
        self.update_queue: queue.Queue = queue.Queue()
        self.log_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.source_id = normalize_market_source_id(WEB_MARKET_SOURCE)
        self.snapshot = build_idle_snapshot()
        self.logs: deque[dict[str, Any]] = deque(maxlen=500)
        self.trades: deque[dict[str, Any]] = deque(load_trade_history(limit=200), maxlen=200)
        self.chart: deque[dict[str, Any]] = deque(maxlen=360)
        self.clients: set[web.WebSocketResponse] = set()
        self._pump_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(self._pump(), name="dashboard-runtime-pump")

    async def close(self) -> None:
        await self.stop_worker(wait=True)
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        for client in list(self.clients):
            await client.close()
        self.clients.clear()

    def is_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    async def start_worker(self, source_id: str | None = None) -> dict[str, Any]:
        if self.is_running():
            return self.runtime_status()
        self.source_id = normalize_market_source_id(source_id or self.source_id)
        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=worker_thread,
            args=(self.update_queue, self.log_queue, self.source_id, self.stop_event),
            daemon=True,
            name="v6-live-engine",
        )
        self.snapshot = dict(self.snapshot)
        self.snapshot["status"] = "STARTING"
        self.snapshot["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        self.snapshot.setdefault("feed", {})["requestedSource"] = self.source_id
        self.worker.start()
        await self.broadcast({"type": "runtime", **self.runtime_status()})
        return self.runtime_status()

    async def stop_worker(self, *, wait: bool = False) -> dict[str, Any]:
        if self.stop_event is not None:
            self.stop_event.set()
        if self.is_running():
            self.snapshot = dict(self.snapshot)
            self.snapshot["status"] = "STOPPING"
            await self.broadcast({"type": "runtime", **self.runtime_status()})
        if wait and self.worker is not None:
            await asyncio.to_thread(self.worker.join, 35)
        return self.runtime_status()

    def runtime_status(self) -> dict[str, Any]:
        return {
            "status": self.snapshot.get("status", "IDLE"),
            "running": self.is_running(),
            "source": self.source_id,
            "workerAlive": self.is_running(),
        }

    def bootstrap(self) -> dict[str, Any]:
        snapshot = dict(self.snapshot)
        snapshot["chart"] = list(self.chart)
        return {
            "runtime": self.runtime_status(),
            "snapshot": snapshot,
            "trades": list(self.trades),
            "logs": list(self.logs),
            "backtest": load_backtest_summary(),
            "dataStatus": load_data_status(),
            "sourceOptions": [
                {"id": option.source_id, "label": option.label, "detail": option.detail}
                for option in MARKET_SOURCE_OPTIONS.values()
            ],
            "system": {
                "mode": "SIMULATION",
                "strategy": "CombinedStrategyV6",
                "symbol": "002796.SZ",
                "backend": "aiohttp",
                "transport": "WebSocket",
                "dataDirectory": DATA_DIR,
                "backtestDirectory": BACKTEST_RECORD_DIR,
            },
        }

    async def register(self, client: web.WebSocketResponse) -> None:
        self.clients.add(client)
        await client.send_json({"type": "bootstrap", **self.bootstrap()})

    def unregister(self, client: web.WebSocketResponse) -> None:
        self.clients.discard(client)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[web.WebSocketResponse] = []
        for client in list(self.clients):
            try:
                await client.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    async def _pump(self) -> None:
        while True:
            changed = False
            while True:
                try:
                    message = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                entry = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "level": self._log_level(str(message)),
                    "message": str(message),
                }
                self.logs.appendleft(entry)
                await self.broadcast({"type": "log", "log": entry})

            latest_update = None
            while True:
                try:
                    latest_update = self.update_queue.get_nowait()
                except queue.Empty:
                    break
            if latest_update is not None:
                dashboard = latest_update.get("dashboard")
                if dashboard:
                    if dashboard.get("status") == "PAUSE":
                        merged = dict(self.snapshot)
                        merged.update(dashboard)
                        merged["feed"] = {
                            **(self.snapshot.get("feed") or {}),
                            **(dashboard.get("feed") or {}),
                        }
                        merged["decision"] = {
                            **(self.snapshot.get("decision") or {}),
                            "action": "HOLD",
                            "state": "paused",
                            "headline": "等待交易窗口",
                            "reason": f"当前为非交易时段：{dashboard.get('pause', {}).get('window', '等待开盘')}",
                            "detail": "策略状态已保存，进入交易时段后自动继续。",
                        }
                        dashboard = merged
                    self.snapshot = dashboard
                    quote = dashboard.get("quote") or {}
                    if quote.get("price", 0) > 0:
                        point = {
                            "time": dashboard.get("feed", {}).get("lastTick") or dashboard.get("updatedAt", ""),
                            "price": quote.get("price", 0),
                            "vwap": quote.get("vwap", 0),
                            "localVwap": quote.get("localVwap", 0),
                            "trade": dashboard.get("trade"),
                        }
                        self.chart.append(point)
                    dashboard["chart"] = list(self.chart)
                    trade = dashboard.get("trade")
                    if trade:
                        self.trades.appendleft(trade)
                        await self.broadcast({"type": "trade", "trade": trade})
                    await self.broadcast(dashboard)
                    changed = True

            if self.worker is not None and not self.worker.is_alive():
                if self.snapshot.get("status") in {"STARTING", "RUNNING", "PAUSE", "STOPPING"}:
                    self.snapshot = dict(self.snapshot)
                    self.snapshot["status"] = "STOPPED"
                    self.snapshot["updatedAt"] = datetime.now().isoformat(timespec="seconds")
                    await self.broadcast({"type": "runtime", **self.runtime_status()})
                self.worker = None
                self.stop_event = None
                changed = True

            await asyncio.sleep(0.08 if changed else 0.15)

    @staticmethod
    def _log_level(message: str) -> str:
        upper = message.upper()
        if "[ERROR]" in upper or upper.startswith("[!]"):
            return "error"
        if "[WARN" in upper or "[DQ:" in upper:
            return "warning"
        if "[OK]" in upper:
            return "success"
        return "info"
