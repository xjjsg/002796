"""Serialization helpers for the web trading dashboard.

The strategy engine deliberately exposes Python objects.  This module is the
single boundary that converts those objects into stable, JSON-safe dashboard
payloads for HTTP and WebSocket clients.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import INITIAL_CAPITAL, LOT_SIZE, SYMBOL_CODE, SYMBOL_NAME, clamp, parse_dt
from .execution import calculate_trade_costs
from .position import PositionMode, TradeRecord
from .trade_records import canonicalize_trade_row, trade_to_dict


SIGNAL_SPECS = (
    ("cross_buy", "跨日加仓", "BUY", "_score_cross_buy", "cross_enter_score", 0.25),
    ("cross_sell", "跨日减仓", "SELL", "_score_cross_sell", "cross_enter_score", 0.25),
    ("local_trim", "日内 T 减仓", "SELL", "_score_local_trim", "local_enter_score", 0.80),
    ("local_cover", "日内 T 回补", "BUY", "_score_local_cover", "local_cover_enter_score", 0.85),
    ("main_flow", "主力流出保护", "SELL", "_score_main_flow_distribution", "main_flow_guard_score", 0.50),
    ("buy_timing", "买点确认", "BUY", "_score_buy_timing", "local_buy_timing_score", 0.80),
    ("sell_timing", "卖点确认", "SELL", "_score_sell_timing", "local_sell_timing_score", 0.50),
)

AUXILIARY_SIGNAL_KEYS = {"buy_timing", "sell_timing"}

FACTOR_SPECS = (
    ("day_return", "今日涨跌", "pct"),
    ("day_vwap_dev", "日 VWAP 偏离", "pct3"),
    ("local_vwap_dev", "30m VWAP 偏离", "pct3"),
    ("velocity", "5m 动量", "pct3"),
    ("acceleration", "动量加速度", "pct4"),
    ("vol_mom", "量能动量", "multiple"),
    ("range_position", "日内区间位置", "pct_unsigned"),
    ("pullback_from_high", "距日高回撤", "pct"),
    ("below_vwap_ratio", "低于 VWAP 时长", "pct_unsigned"),
    ("orderbook_imbalance", "盘口不平衡", "pct"),
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_score(strategy: Any, method_name: str, snapshot: Any) -> float:
    if snapshot is None:
        return 0.0
    method = getattr(strategy, method_name, None)
    if not callable(method):
        return 0.0
    try:
        return max(0.0, min(1.0, float(method(snapshot))))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _format_factor(value: float | None, style: str) -> str:
    if value is None:
        return "--"
    if style == "pct":
        return f"{value * 100:+.2f}%"
    if style == "pct3":
        return f"{value * 100:+.3f}%"
    if style == "pct4":
        return f"{value * 100:+.4f}%"
    if style == "pct_unsigned":
        return f"{value * 100:.1f}%"
    if style == "multiple":
        return f"{value:.2f}x"
    return f"{value:.2f}"


def _factor_tone(key: str, value: float | None) -> str:
    if value is None or abs(value) < 1e-12:
        return "neutral"
    negative_is_positive = key in {"pullback_from_high"}
    positive = value < 0 if negative_is_positive else value > 0
    return "positive" if positive else "negative"


def _tick_time_context(tick: dict[str, Any] | None) -> tuple[datetime | None, str]:
    tick = tick or {}
    dt = parse_dt(tick.get("Time", tick.get("timestamp", tick.get("dt"))))
    if dt is not None:
        return dt, dt.strftime("%H:%M:%S")
    time_text = str(tick.get("server_time") or "")
    return None, time_text


def _dashboard_can_signal(
    strategy: Any,
    tick: dict[str, Any] | None,
    start_time: str = "10:00:00",
) -> tuple[bool, str, str]:
    dt, time_str = _tick_time_context(tick)
    last_signal_time = str(getattr(strategy, "last_signal_time", "14:45:00") or "14:45:00")
    if time_str:
        if time_str < start_time:
            return False, f"{start_time} 后才进入策略信号窗口", time_str
        if time_str > last_signal_time:
            return False, f"{last_signal_time} 后不再开新信号", time_str
    day_count = int(getattr(strategy, "day_trade_count", 0) or 0)
    max_count = int(getattr(strategy, "max_day_trades", 0) or 0)
    if max_count and day_count >= max_count:
        return False, "今日交易次数已达到策略上限", time_str
    last_trade_dt = getattr(strategy, "last_trade_dt", None)
    cooldown = getattr(strategy, "cooldown", None)
    if dt is not None and last_trade_dt is not None and cooldown is not None and dt - last_trade_dt < cooldown:
        return False, "仍在交易冷却期内", time_str
    return True, "", time_str


def _would_trade_to_target(
    strategy: Any,
    current_price: float,
    target_pct: float,
    *,
    force_floor: bool = False,
) -> tuple[bool, str]:
    if current_price <= 0:
        return False, "价格无效"
    total = _safe_float(strategy.total_asset(current_price))
    shares = int(getattr(strategy, "shares", 0) or 0)
    cash = _safe_float(getattr(strategy, "cash", 0.0))
    target_pct = clamp(
        target_pct,
        _safe_float(getattr(strategy, "floor_pct", 0.0)),
        _safe_float(getattr(strategy, "ceil_pct", 1.0), 1.0),
    )
    target_shares = int((total * target_pct) / current_price / LOT_SIZE) * LOT_SIZE
    diff = target_shares - shares
    min_lots = int(getattr(strategy, "min_trade_lots", 3) or 3)
    min_shares = LOT_SIZE if force_floor else min_lots * LOT_SIZE
    if diff >= min_shares:
        commission_rate = _safe_float(getattr(strategy, "commission_rate", 0.0))
        max_affordable = int((cash / (current_price * (1.0 + commission_rate))) / LOT_SIZE) * LOT_SIZE
        if min(diff, max_affordable) >= min_shares:
            return True, ""
        return False, "可用现金不足或不足最小买入手数"
    if diff <= -min_shares:
        sell_shares = int(min(-diff, shares) / LOT_SIZE) * LOT_SIZE
        if sell_shares >= min_shares:
            return True, ""
        return False, "可卖股数不足"
    return False, "目标仓位变化不足最小交易手数"


def _blocked_or_triggered(
    strategy: Any,
    snapshot: Any,
    tick: dict[str, Any] | None,
    key: str,
    score: float,
    scores: dict[str, float],
    threshold: float,
) -> tuple[str, str]:
    current_price = _safe_float(getattr(snapshot, "price", 0.0))
    if current_price <= 0 and tick:
        current_price = _safe_float(tick.get("price", tick.get("Close", 0.0)))

    if key == "main_flow":
        decision = getattr(strategy, "regime_decision", None)
        regime = getattr(getattr(decision, "regime", None), "value", "UNKNOWN")
        _, time_str = _tick_time_context(tick)
        start_time = str(getattr(strategy, "main_flow_guard_start_time", "10:30:00") or "10:30:00")
        if not bool(getattr(strategy, "enable_main_flow_guard", False)):
            return "blocked", "主力流出保护未启用"
        if decision is None:
            return "blocked", "市场状态尚未确认"
        if regime != "UPTREND":
            return "blocked", f"仅上涨趋势中执行，当前为 {regime}"
        if int(getattr(strategy, "shares", 0) or 0) <= 0:
            return "blocked", "当前无持仓可减"
        if time_str and time_str < start_time:
            return "blocked", f"{start_time} 后才启用主力流出保护"
        if getattr(strategy, "main_flow_guard_date", None) == getattr(strategy, "current_date", None):
            return "blocked", "今日主力流出保护已处理"
        if _safe_float(getattr(snapshot, "day_return", 0.0)) > -0.035:
            return "blocked", "日内跌幅未达到 3.5% 防守条件"
        target = clamp(
            _safe_float(getattr(strategy, "main_flow_guard_target_pct", 0.40), 0.40),
            _safe_float(getattr(strategy, "floor_pct", 0.0)),
            _safe_float(getattr(strategy, "ceil_pct", 1.0), 1.0),
        )
        margin = _safe_float(getattr(strategy, "regime_rebalance_margin", 0.0))
        if _safe_float(strategy.current_position_pct(current_price)) <= target + margin:
            return "blocked", "当前仓位已在防守目标附近"
        ok, reason = _would_trade_to_target(strategy, current_price, target)
        return ("triggered", "满足执行条件") if ok else ("blocked", reason)

    start_time = str(getattr(strategy, "cross_start_time", "10:00:00") or "10:00:00")
    can_signal, reason, time_str = _dashboard_can_signal(strategy, tick, start_time)
    if not can_signal:
        return "blocked", reason

    allow_cross_day = bool(getattr(strategy, "_allow_cross_day_signals")())
    allow_short_entry, allow_short_cover, allow_long_entry, allow_long_exit = getattr(strategy, "_local_t_permissions")()
    local_cycle = getattr(strategy, "local_t_cycle", None)

    if key == "cross_buy":
        pending = getattr(strategy, "pending_cross_buy", None)
        if not allow_cross_day:
            return "blocked", "当前市场状态禁止跨日加仓"
        if local_cycle == "short":
            return "blocked", "日内 short T 周期中不做跨日加仓"
        if pending is not None:
            pending_score, target = pending
            score = max(score, _safe_float(pending_score))
        else:
            if not bool(getattr(strategy, "_cross_buy_entry_signal")(snapshot, score)):
                return "blocked", "跨日加仓主信号未满足执行确认"
            if score < scores.get("cross_sell", 0.0):
                return "blocked", "卖出信号强于买入信号"
            target = getattr(strategy, "_cross_target")("buy", score)
        if target <= _safe_float(getattr(strategy, "target_pct", 0.0)) + _safe_float(getattr(strategy, "min_target_move", 0.0)):
            return "blocked", "目标仓位提升不足最小调仓幅度"
        ok, trade_reason = _would_trade_to_target(strategy, current_price, target)
        if not ok:
            return "blocked", trade_reason
        execute_time = str(getattr(strategy, "cross_buy_execute_time", "14:00:00") or "14:00:00")
        if time_str and time_str < execute_time:
            return "pending", f"跨日买入延迟到 {execute_time} 执行"
        return "triggered", "满足执行条件"

    if key == "cross_sell":
        if not allow_cross_day:
            return "blocked", "当前市场状态禁止跨日减仓"
        if score < scores.get("cross_buy", 0.0):
            return "blocked", "买入信号强于卖出信号"
        target = getattr(strategy, "_cross_target")("sell", score)
        if target >= _safe_float(getattr(strategy, "target_pct", 0.0)) - _safe_float(getattr(strategy, "min_target_move", 0.0)):
            return "blocked", "目标仓位下降不足最小调仓幅度"
        ok, trade_reason = _would_trade_to_target(strategy, current_price, target)
        return ("triggered", "满足执行条件") if ok else ("blocked", trade_reason)

    if key == "local_trim":
        if local_cycle == "long":
            if not allow_long_exit:
                return "blocked", "当前市场状态禁止平多 T"
            base = getattr(strategy, "_base_for_local_t")()
            if _safe_float(getattr(strategy, "target_pct", 0.0)) <= base + _safe_float(getattr(strategy, "min_target_move", 0.0)):
                return "blocked", "当前仓位未高于日内 T 基准"
            ok, trade_reason = _would_trade_to_target(strategy, current_price, base, force_floor=True)
            return ("triggered", "满足执行条件") if ok else ("blocked", trade_reason)
        if not allow_short_entry:
            return "blocked", "当前市场状态禁止开 short T"
        if not bool(getattr(strategy, "_can_open_local_cycle")()):
            return "blocked", "日内 T 周期次数已用完或已有周期"
        if not bool(getattr(strategy, "_local_short_entry_signal")(snapshot, score)):
            sell_timing = _safe_score(strategy, "_score_sell_timing", snapshot)
            if sell_timing < _safe_float(getattr(strategy, "local_sell_timing_score", 0.50), 0.50):
                return "blocked", "卖点确认不足"
            if _safe_float(getattr(snapshot, "day_return", 0.0)) < _safe_float(getattr(strategy, "local_short_min_day_return", 0.015), 0.015):
                return "blocked", "日内涨幅不足 short T 条件"
            return "blocked", "short T 入口过滤条件未满足"
        target = getattr(strategy, "_target_for_local_t")("trim", score)
        ok, trade_reason = _would_trade_to_target(strategy, current_price, target)
        return ("triggered", "满足执行条件") if ok else ("blocked", trade_reason)

    if key == "local_cover":
        if local_cycle == "short":
            if not allow_short_cover:
                return "blocked", "当前市场状态禁止回补 short T"
            base = getattr(strategy, "_base_for_local_t")()
            target = min(getattr(strategy, "_target_for_local_t")("cover", score), base)
            ok, trade_reason = _would_trade_to_target(strategy, current_price, target, force_floor=True)
            return ("triggered", "满足执行条件") if ok else ("blocked", trade_reason)
        if not allow_long_entry:
            return "blocked", "当前市场状态禁止开 long T"
        if not bool(getattr(strategy, "_can_open_local_cycle")()):
            return "blocked", "日内 T 周期次数已用完或已有周期"
        if not bool(getattr(strategy, "_local_long_entry_signal")(snapshot, score)):
            buy_timing = _safe_score(strategy, "_score_buy_timing", snapshot)
            if buy_timing < _safe_float(getattr(strategy, "local_buy_timing_score", 0.80), 0.80):
                return "blocked", "买点确认不足"
            return "blocked", "long T 入口过滤条件未满足"
        target = getattr(strategy, "_target_for_local_t")("cover", score)
        ok, trade_reason = _would_trade_to_target(strategy, current_price, target)
        return ("triggered", "满足执行条件") if ok else ("blocked", trade_reason)

    return "blocked", "策略执行条件未满足"


def build_signals(strategy: Any, snapshot: Any, tick: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    scores = {
        key: _safe_score(strategy, method_name, snapshot)
        for key, _, _, method_name, _, _ in SIGNAL_SPECS
    }
    pending_cross_buy = getattr(strategy, "pending_cross_buy", None)
    if pending_cross_buy:
        scores["cross_buy"] = max(scores["cross_buy"], _safe_float(pending_cross_buy[0]))

    for key, label, direction, method_name, threshold_attr, default_threshold in SIGNAL_SPECS:
        score = scores[key]
        threshold = _safe_float(
            getattr(strategy, threshold_attr, default_threshold) if threshold_attr else default_threshold,
            default_threshold,
        )
        ratio = score / threshold if threshold > 0 else score
        reason = ""
        if key in AUXILIARY_SIGNAL_KEYS:
            if score >= threshold:
                state = "confirm"
                reason = "辅助确认项，不单独触发交易"
            elif ratio >= 0.75:
                state = "near"
            else:
                state = "watching"
        elif score >= threshold:
            state, reason = _blocked_or_triggered(strategy, snapshot, tick, key, score, scores, threshold)
        elif ratio >= 0.75:
            state = "near"
        else:
            state = "watching"
        signals.append(
            {
                "key": key,
                "label": label,
                "direction": direction,
                "score": score,
                "threshold": threshold,
                "progress": max(0.0, min(1.0, ratio)),
                "state": state,
                "reason": reason,
                "executable": state == "triggered",
            }
        )
    return signals


def build_factors(snapshot: Any) -> list[dict[str, Any]]:
    factors: list[dict[str, Any]] = []
    for key, label, style in FACTOR_SPECS:
        raw = None if snapshot is None else _safe_float(getattr(snapshot, key, None), 0.0)
        factors.append(
            {
                "key": key,
                "label": label,
                "raw": raw,
                "value": _format_factor(raw, style),
                "tone": _factor_tone(key, raw),
            }
        )
    return factors


def _position_pct(shares: int, cash: float, price: float) -> float:
    asset = cash + shares * price
    return shares * price / asset if asset > 0 and price > 0 else 0.0


def trade_record_to_payload(
    trade: TradeRecord,
    *,
    strategy: Any | None = None,
    tick: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = trade_to_dict(trade, strategy=strategy, tick=tick, source="runtime")
    return trade_row_to_payload(row)


def trade_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    row = canonicalize_trade_row(row)
    side = str(row.get("side") or "").upper()
    price = _safe_float(row.get("price"))
    shares = int(_safe_float(row.get("shares")))
    cash_after = _safe_float(row.get("cash_after"))
    position_shares = int(_safe_float(row.get("position_shares")))
    mark_price = _safe_float(row.get("last_price"), price) or price
    amount = _safe_float(row.get("amount"), price * shares)
    commission = _safe_float(row.get("commission"))
    stamp_tax = _safe_float(row.get("stamp_tax"))
    if amount <= 0 and price > 0 and shares > 0:
        costs = calculate_trade_costs(side, price, shares)
        amount = costs.amount
        commission = costs.commission
        stamp_tax = costs.stamp_tax

    position_after = _safe_float(row.get("position_pct_after"), -1.0)
    if position_after < 0:
        position_after = _position_pct(position_shares, cash_after, mark_price)

    before_shares = position_shares
    before_cash = cash_after
    if price > 0 and shares > 0 and side in {"BUY", "SELL"}:
        costs = calculate_trade_costs(side, price, shares)
        if side == "BUY":
            before_shares = max(0, position_shares - shares)
            before_cash = cash_after + costs.buy_cash_required
        else:
            before_shares = position_shares + shares
            before_cash = cash_after - costs.sell_cash_received
    position_before = _position_pct(before_shares, before_cash, mark_price)

    timestamp = str(row.get("timestamp") or row.get("tick_time") or "")
    return {
        "id": "|".join(
            [
                timestamp,
                side,
                f"{price:.4f}",
                str(shares),
                str(row.get("reason") or ""),
            ]
        ),
        "timestamp": timestamp,
        "time": timestamp[-8:] if len(timestamp) >= 8 else timestamp,
        "side": side,
        "status": str(row.get("status") or "FILLED").upper(),
        "statusLabel": "已成交",
        "price": price,
        "shares": shares,
        "amount": amount,
        "commission": commission,
        "stampTax": stamp_tax,
        "cashAfter": cash_after,
        "positionShares": position_shares,
        "positionBefore": position_before,
        "positionAfter": position_after,
        "targetPct": _safe_float(row.get("target_pct")),
        "mode": str(row.get("mode") or ""),
        "reason": str(row.get("reason") or ""),
        "detail": str(row.get("detail") or ""),
        "source": str(row.get("source") or ""),
        "executionSource": str(row.get("execution_source") or "strategy-sim"),
        "scores": {
            "crossBuy": _safe_float(row.get("cross_buy_score")),
            "crossSell": _safe_float(row.get("cross_sell_score")),
            "localTrim": _safe_float(row.get("local_trim_score")),
            "localCover": _safe_float(row.get("local_cover_score")),
            "buyTiming": _safe_float(row.get("buy_timing_score")),
            "sellTiming": _safe_float(row.get("sell_timing_score")),
        },
    }


def _decision_payload(
    strategy: Any,
    signals: list[dict[str, Any]],
    trade_payload: dict[str, Any] | None,
    current_price: float,
) -> dict[str, Any]:
    if trade_payload:
        action = trade_payload["side"]
        return {
            "action": action,
            "state": "filled",
            "headline": "买入成交" if action == "BUY" else "卖出成交",
            "reason": trade_payload["reason"],
            "detail": trade_payload["detail"],
            "leadingSignal": next(
                (item for item in signals if item["direction"] == action),
                signals[0] if signals else None,
            ),
            "restrictions": [],
        }

    min_buy_cash = current_price * 100 * (1.0 + _safe_float(getattr(strategy, "commission_rate", 0.0)))
    restrictions: list[str] = []
    if _safe_float(getattr(strategy, "target_pct", 0.0)) >= _safe_float(getattr(strategy, "ceil_pct", 1.0)) - 1e-6:
        restrictions.append("目标仓位已到上限，当前只关注卖出信号")
    if _safe_float(getattr(strategy, "cash", 0.0)) < min_buy_cash:
        restrictions.append("可用现金不足一手，买入路径已暂停")
    day_count = int(getattr(strategy, "day_trade_count", 0) or 0)
    max_count = int(getattr(strategy, "max_day_trades", 0) or 0)
    if max_count and day_count >= max_count:
        restrictions.append("今日交易次数已达到策略上限")

    actionable = [item for item in signals if item.get("state") in {"triggered", "pending"}]
    near_signals = [item for item in signals if item.get("state") == "near"]
    blocked_signals = [item for item in signals if item.get("state") == "blocked"]
    leading_pool = actionable or near_signals or blocked_signals or signals
    leading = max(leading_pool, key=lambda item: item["progress"], default=None)
    if restrictions:
        reason = restrictions[0]
    elif actionable:
        if leading["state"] == "pending":
            reason = f"{leading['label']}已确认，{leading.get('reason') or '等待执行窗口'}"
        else:
            reason = f"{leading['label']}满足执行条件，等待成交回报"
    elif blocked_signals:
        reason = f"{leading['label']}高分但未执行：{leading.get('reason') or '执行条件未满足'}"
    elif leading:
        reason = f"{leading['label']}正在形成，当前 {leading['score']:.2f} / 阈值 {leading['threshold']:.2f}"
    else:
        reason = "等待行情形成可执行信号"
    return {
        "action": "HOLD",
        "state": "watching",
        "headline": "继续观察",
        "reason": reason,
        "detail": "策略尚未产生新的成交动作。",
        "leadingSignal": leading,
        "restrictions": restrictions,
    }


def build_dashboard_snapshot(
    tick: dict[str, Any],
    strategy: Any,
    trade_record: TradeRecord | None = None,
    *,
    status: str = "RUNNING",
    market_source: str = "",
    market_source_label: str = "",
    requested_market_source: str = "",
) -> dict[str, Any]:
    current_price = _safe_float(tick.get("price", tick.get("Close")))
    calc = getattr(strategy, "factor_calc", None)
    snapshot = getattr(calc, "last_snapshot", None)
    prev_close = _safe_float(tick.get("prev_close"), _safe_float(getattr(calc, "prev_close", 0.0)))
    day_return = (
        _safe_float(getattr(snapshot, "day_return", 0.0))
        if snapshot is not None
        else (current_price / prev_close - 1.0 if prev_close > 0 else 0.0)
    )
    equity = _safe_float(strategy.total_asset(current_price))
    capital = _safe_float(getattr(strategy, "initial_capital", INITIAL_CAPITAL), INITIAL_CAPITAL)
    pnl = equity - capital
    position_pct = _safe_float(strategy.current_position_pct(current_price))
    signals = build_signals(strategy, snapshot, tick)
    trade_payload = (
        trade_record_to_payload(trade_record, strategy=strategy, tick=tick)
        if trade_record is not None
        else None
    )
    decision = getattr(strategy, "regime_decision", None)
    regime = {
        "name": getattr(getattr(decision, "regime", None), "value", "UNKNOWN"),
        "score": _safe_float(getattr(decision, "regime_score", 0.0)),
        "confidence": _safe_float(getattr(decision, "confidence", 0.0)),
        "floorPct": _safe_float(
            getattr(decision, "target_floor_pct", getattr(strategy, "floor_pct", 0.0))
        ),
        "ceilingPct": _safe_float(
            getattr(decision, "target_ceiling_pct", getattr(strategy, "ceil_pct", 1.0))
        ),
        "detail": str(getattr(decision, "detail", "") or ""),
        "tags": list(getattr(decision, "tags", ()) or ()),
        "allowCrossDay": bool(getattr(decision, "allow_cross_day", True)),
        "allowLocalT": bool(getattr(decision, "allow_local_t", True)),
    }

    asks = [
        {"level": level, "price": _safe_float(tick.get(f"sp{level}")), "volume": int(_safe_float(tick.get(f"sv{level}")))}
        for level in range(5, 0, -1)
    ]
    bids = [
        {"level": level, "price": _safe_float(tick.get(f"bp{level}")), "volume": int(_safe_float(tick.get(f"bv{level}")))}
        for level in range(1, 6)
    ]
    local_base = getattr(strategy, "local_base_target_pct", None)
    local_entry = getattr(strategy, "local_t_entry_price", None)

    return {
        "type": "snapshot",
        "status": status,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "symbol": {
            "code": "002796.SZ",
            "sourceCode": SYMBOL_CODE,
            "name": SYMBOL_NAME,
        },
        "feed": {
            "requestedSource": requested_market_source,
            "activeSource": market_source,
            "label": market_source_label,
            "fallback": bool(tick.get("market_source_fallback")),
            "lastTick": str(tick.get("server_time") or ""),
        },
        "quote": {
            "price": current_price,
            "changePct": day_return,
            "prevClose": prev_close,
            "open": _safe_float(tick.get("open")),
            "high": _safe_float(tick.get("high")),
            "low": _safe_float(tick.get("low")),
            "tickVolume": _safe_float(tick.get("tick_vol")),
            "vwap": _safe_float(getattr(snapshot, "vwap", getattr(calc, "vwap", 0.0))),
            "localVwap": _safe_float(getattr(snapshot, "local_vwap", 0.0)),
        },
        "account": {
            "shares": int(getattr(strategy, "shares", 0) or 0),
            "cash": _safe_float(getattr(strategy, "cash", 0.0)),
            "equity": equity,
            "pnl": pnl,
            "pnlPct": pnl / capital if capital > 0 else 0.0,
            "positionPct": position_pct,
            "targetPct": _safe_float(getattr(strategy, "target_pct", 0.0)),
            "floorPct": _safe_float(getattr(strategy, "floor_pct", 0.0)),
            "ceilingPct": _safe_float(getattr(strategy, "ceil_pct", 1.0)),
            "mode": getattr(getattr(strategy, "mode", PositionMode.NEUTRAL), "value", "NEUTRAL"),
            "dayTradeCount": int(getattr(strategy, "day_trade_count", 0) or 0),
            "maxDayTrades": int(getattr(strategy, "max_day_trades", 0) or 0),
            "lastTradeTime": (
                getattr(strategy, "last_trade_dt").isoformat(timespec="seconds")
                if isinstance(getattr(strategy, "last_trade_dt", None), datetime)
                else ""
            ),
            "localCycle": str(getattr(strategy, "local_t_cycle", None) or "none"),
            "localBasePct": _safe_float(local_base) if local_base is not None else None,
            "localEntryPrice": _safe_float(local_entry) if local_entry is not None else None,
            "localEntryShares": int(getattr(strategy, "local_t_entry_shares", 0) or 0),
        },
        "regime": regime,
        "decision": _decision_payload(strategy, signals, trade_payload, current_price),
        "signals": signals,
        "factors": build_factors(snapshot),
        "orderbook": {
            "asks": asks,
            "bids": bids,
            "imbalance": _safe_float(getattr(snapshot, "orderbook_imbalance", 0.0)),
        },
        "trade": trade_payload,
    }


def build_pause_snapshot(
    *,
    win_name: str,
    wait_s: float,
    market_source: str,
    market_source_label: str,
    requested_market_source: str,
) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "status": "PAUSE",
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "symbol": {"code": "002796.SZ", "sourceCode": SYMBOL_CODE, "name": SYMBOL_NAME},
        "feed": {
            "requestedSource": requested_market_source,
            "activeSource": market_source,
            "label": market_source_label,
            "fallback": False,
            "lastTick": "",
        },
        "pause": {"window": win_name, "waitSeconds": max(0.0, wait_s)},
    }
