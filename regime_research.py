"""Regime Research: data-driven analysis of MarketRegime labels.

This script:
1. Loads all available market data and aggregates daily OHLCV bars.
2. Runs MarketRegimeEngine tick-by-tick (using EOD ticks) to label each day.
3. Computes forward 1/3/5 day returns and max drawdown per regime.
4. Outputs statistics tables and data-driven position band recommendations.

Usage:
    python regime_research.py [--start-date 2026-01-05] [--output-dir regime_research_output]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from market_data import DATA_DIR, MarketDataBundle, load_market_data, row_to_tick
from market_regime import MarketRegime, MarketRegimeEngine

START_DATE = "2026-01-05"
OUTPUT_DIR = Path(__file__).resolve().parent / "regime_research_output"


# ── helpers ──────────────────────────────────────────────────────────────────


@dataclass
class DailyRegimeLabel:
    date: str
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    amount: float
    regime: str
    tags: tuple[str, ...]
    confidence: float
    target_floor_pct: float
    target_ceiling_pct: float
    regime_score: float
    # forward returns (filled later)
    fwd_1d_return: float | None = None
    fwd_3d_return: float | None = None
    fwd_5d_return: float | None = None
    fwd_5d_max_dd: float | None = None
    fwd_3d_max_dd: float | None = None
    fwd_1d_max_dd: float | None = None


@dataclass
class RegimeStats:
    regime: str
    sample_count: int
    avg_fwd_1d: float
    avg_fwd_3d: float
    avg_fwd_5d: float
    med_fwd_1d: float
    med_fwd_3d: float
    med_fwd_5d: float
    p25_fwd_5d: float
    p75_fwd_5d: float
    avg_fwd_5d_dd: float
    avg_fwd_3d_dd: float
    win_rate_1d: float
    win_rate_3d: float
    win_rate_5d: float
    score: float
    suggested_floor: float
    suggested_ceiling: float


def _safe_pct(a: float, b: float) -> float:
    return a / b - 1.0 if b > 0 else 0.0


def _max_dd_from_prices(prices: list[float]) -> float:
    if not prices:
        return 0.0
    peak = prices[0]
    dd = 0.0
    for p in prices:
        if p > peak:
            peak = p
        if peak > 0:
            dd = max(dd, 1.0 - p / peak)
    return dd


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = pct / 100.0 * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _median(values: list[float]) -> float:
    return _percentile(values, 50.0)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ── core logic ───────────────────────────────────────────────────────────────


def aggregate_daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate tick-level data into daily OHLCV bars."""
    daily = (
        df.groupby("date", sort=True)
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("cum_volume", "last"),
            amount=("cum_amount", "last"),
        )
        .reset_index()
    )
    daily["vwap"] = daily.apply(
        lambda r: r["amount"] / r["volume"] if r["volume"] > 0 else r["close"], axis=1
    )
    return daily


def label_daily_regimes(df: pd.DataFrame, daily: pd.DataFrame) -> list[DailyRegimeLabel]:
    """Run MarketRegimeEngine on the last tick of each day to label regimes."""
    engine = MarketRegimeEngine()
    dates = sorted(daily["date"].unique())
    labels: list[DailyRegimeLabel] = []

    for date_str in dates:
        day_ticks = df[df["date"] == date_str]
        if day_ticks.empty:
            continue

        # Feed all ticks of the day to the engine to simulate real-time
        decision = None
        for _, row in day_ticks.iterrows():
            tick = row_to_tick(row)
            decision = engine.update(tick)

        if decision is None:
            continue

        day_bar = daily[daily["date"] == date_str].iloc[0]
        label = DailyRegimeLabel(
            date=date_str,
            open=float(day_bar["open"]),
            high=float(day_bar["high"]),
            low=float(day_bar["low"]),
            close=float(day_bar["close"]),
            vwap=float(day_bar["vwap"]),
            volume=float(day_bar["volume"]),
            amount=float(day_bar["amount"]),
            regime=decision.regime.value,
            tags=decision.tags,
            confidence=decision.confidence,
            target_floor_pct=decision.target_floor_pct,
            target_ceiling_pct=decision.target_ceiling_pct,
            regime_score=decision.regime_score,
        )
        labels.append(label)

    return labels


def compute_forward_returns(labels: list[DailyRegimeLabel]) -> None:
    """Fill forward 1/3/5 day returns and drawdowns in-place."""
    n = len(labels)
    closes = [lab.close for lab in labels]

    for i, lab in enumerate(labels):
        if i + 1 < n:
            lab.fwd_1d_return = _safe_pct(closes[i + 1], closes[i])
            lab.fwd_1d_max_dd = _max_dd_from_prices(closes[i : i + 2])
        if i + 3 < n:
            lab.fwd_3d_return = _safe_pct(closes[i + 3], closes[i])
            lab.fwd_3d_max_dd = _max_dd_from_prices(closes[i : i + 4])
        elif i + 1 < n:
            remaining = closes[i:]
            lab.fwd_3d_return = _safe_pct(remaining[-1], remaining[0])
            lab.fwd_3d_max_dd = _max_dd_from_prices(remaining)
        if i + 5 < n:
            lab.fwd_5d_return = _safe_pct(closes[i + 5], closes[i])
            lab.fwd_5d_max_dd = _max_dd_from_prices(closes[i : i + 6])
        elif i + 1 < n:
            remaining = closes[i:]
            lab.fwd_5d_return = _safe_pct(remaining[-1], remaining[0])
            lab.fwd_5d_max_dd = _max_dd_from_prices(remaining)


def compute_regime_stats(labels: list[DailyRegimeLabel]) -> list[RegimeStats]:
    """Compute per-regime statistics and data-driven position bands."""
    regime_groups: dict[str, list[DailyRegimeLabel]] = defaultdict(list)
    for lab in labels:
        regime_groups[lab.regime].append(lab)

    stats_list: list[RegimeStats] = []
    all_fwd5 = [lab.fwd_5d_return for lab in labels if lab.fwd_5d_return is not None]
    all_fwd5_dd = [lab.fwd_5d_max_dd for lab in labels if lab.fwd_5d_max_dd is not None]
    all_fwd1 = [lab.fwd_1d_return for lab in labels if lab.fwd_1d_return is not None]
    global_std_5d = _std(all_fwd5) if all_fwd5 else 0.05
    global_std_dd = _std(all_fwd5_dd) if all_fwd5_dd else 0.05
    global_std_1d = _std(all_fwd1) if all_fwd1 else 0.03
    global_mean_5d = _mean(all_fwd5) if all_fwd5 else 0.0
    global_mean_dd = _mean(all_fwd5_dd) if all_fwd5_dd else 0.0
    global_mean_1d = _mean(all_fwd1) if all_fwd1 else 0.0

    for regime_name in [r.value for r in MarketRegime]:
        group = regime_groups.get(regime_name, [])
        count = len(group)
        if count == 0:
            stats_list.append(RegimeStats(
                regime=regime_name, sample_count=0,
                avg_fwd_1d=0, avg_fwd_3d=0, avg_fwd_5d=0,
                med_fwd_1d=0, med_fwd_3d=0, med_fwd_5d=0,
                p25_fwd_5d=0, p75_fwd_5d=0,
                avg_fwd_5d_dd=0, avg_fwd_3d_dd=0,
                win_rate_1d=0, win_rate_3d=0, win_rate_5d=0,
                score=0, suggested_floor=0.45, suggested_ceiling=0.85,
            ))
            continue

        fwd1 = [l.fwd_1d_return for l in group if l.fwd_1d_return is not None]
        fwd3 = [l.fwd_3d_return for l in group if l.fwd_3d_return is not None]
        fwd5 = [l.fwd_5d_return for l in group if l.fwd_5d_return is not None]
        dd5 = [l.fwd_5d_max_dd for l in group if l.fwd_5d_max_dd is not None]
        dd3 = [l.fwd_3d_max_dd for l in group if l.fwd_3d_max_dd is not None]

        avg1 = _mean(fwd1)
        avg3 = _mean(fwd3)
        avg5 = _mean(fwd5)
        med1 = _median(fwd1)
        med3 = _median(fwd3)
        med5 = _median(fwd5)
        p25_5 = _percentile(fwd5, 25)
        p75_5 = _percentile(fwd5, 75)
        avg_dd5 = _mean(dd5)
        avg_dd3 = _mean(dd3)
        wr1 = sum(1 for v in fwd1 if v > 0) / len(fwd1) if fwd1 else 0
        wr3 = sum(1 for v in fwd3 if v > 0) / len(fwd3) if fwd3 else 0
        wr5 = sum(1 for v in fwd5 if v > 0) / len(fwd5) if fwd5 else 0

        # Score: normalized composite
        norm_5d = (avg5 - global_mean_5d) / global_std_5d if global_std_5d > 0 else 0
        norm_wr = (wr5 - 0.5) / 0.25  # centered at 50%
        norm_dd = -(avg_dd5 - global_mean_dd) / global_std_dd if global_std_dd > 0 else 0
        norm_1d = (avg1 - global_mean_1d) / global_std_1d if global_std_1d > 0 else 0
        score = 0.40 * norm_5d + 0.25 * norm_wr + 0.25 * norm_dd + 0.10 * norm_1d

        # Map score to position bands
        if score >= 0.6:
            floor, ceiling = 0.70, 1.00
        elif score >= 0.2:
            floor, ceiling = 0.55, 0.85
        elif score >= -0.2:
            floor, ceiling = 0.45, 0.85
        elif score >= -0.6:
            floor, ceiling = 0.40, 0.70
        else:
            floor, ceiling = 0.40, 0.55

        stats_list.append(RegimeStats(
            regime=regime_name,
            sample_count=count,
            avg_fwd_1d=avg1,
            avg_fwd_3d=avg3,
            avg_fwd_5d=avg5,
            med_fwd_1d=med1,
            med_fwd_3d=med3,
            med_fwd_5d=med5,
            p25_fwd_5d=p25_5,
            p75_fwd_5d=p75_5,
            avg_fwd_5d_dd=avg_dd5,
            avg_fwd_3d_dd=avg_dd3,
            win_rate_1d=wr1,
            win_rate_3d=wr3,
            win_rate_5d=wr5,
            score=score,
            suggested_floor=floor,
            suggested_ceiling=ceiling,
        ))

    return stats_list


def compute_tag_analysis(labels: list[DailyRegimeLabel]) -> list[dict[str, Any]]:
    """Analyze forward returns by individual tag across all regimes."""
    tag_groups: dict[str, list[DailyRegimeLabel]] = defaultdict(list)
    for lab in labels:
        for tag in lab.tags:
            tag_groups[tag].append(lab)

    results = []
    for tag, group in sorted(tag_groups.items()):
        fwd1 = [l.fwd_1d_return for l in group if l.fwd_1d_return is not None]
        fwd3 = [l.fwd_3d_return for l in group if l.fwd_3d_return is not None]
        fwd5 = [l.fwd_5d_return for l in group if l.fwd_5d_return is not None]
        dd5 = [l.fwd_5d_max_dd for l in group if l.fwd_5d_max_dd is not None]
        results.append({
            "tag": tag,
            "count": len(group),
            "avg_fwd_1d": round(_mean(fwd1) * 100, 3) if fwd1 else None,
            "avg_fwd_3d": round(_mean(fwd3) * 100, 3) if fwd3 else None,
            "avg_fwd_5d": round(_mean(fwd5) * 100, 3) if fwd5 else None,
            "avg_fwd_5d_dd": round(_mean(dd5) * 100, 3) if dd5 else None,
            "win_rate_5d": round(sum(1 for v in fwd5 if v > 0) / len(fwd5) * 100, 1) if fwd5 else None,
        })
    return results


def compute_regime_tag_cross(labels: list[DailyRegimeLabel]) -> list[dict[str, Any]]:
    """Cross-analyze regime × tag combinations."""
    cross_groups: dict[tuple[str, str], list[DailyRegimeLabel]] = defaultdict(list)
    for lab in labels:
        for tag in lab.tags:
            cross_groups[(lab.regime, tag)].append(lab)

    results = []
    for (regime, tag), group in sorted(cross_groups.items()):
        if len(group) < 3:
            continue
        fwd5 = [l.fwd_5d_return for l in group if l.fwd_5d_return is not None]
        dd5 = [l.fwd_5d_max_dd for l in group if l.fwd_5d_max_dd is not None]
        fwd1 = [l.fwd_1d_return for l in group if l.fwd_1d_return is not None]
        results.append({
            "regime": regime,
            "tag": tag,
            "count": len(group),
            "avg_fwd_1d": round(_mean(fwd1) * 100, 3) if fwd1 else None,
            "avg_fwd_5d": round(_mean(fwd5) * 100, 3) if fwd5 else None,
            "avg_fwd_5d_dd": round(_mean(dd5) * 100, 3) if dd5 else None,
            "win_rate_5d": round(sum(1 for v in fwd5 if v > 0) / len(fwd5) * 100, 1) if fwd5 else None,
        })
    return results


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


# ── current hardcoded vs data-driven comparison ─────────────────────────────


def compare_params(stats_list: list[RegimeStats]) -> list[dict[str, Any]]:
    """Compare current hardcoded params in MarketRegimeEngine with data-driven suggestions."""
    # Current hardcoded params from market_regime.py _decision()
    hardcoded = {
        "TREND_UP":  {"floor": 0.70, "ceiling": 1.00},
        "REPAIR":    {"floor": 0.55, "ceiling": 0.85},
        "RANGE":     {"floor": 0.45, "ceiling": 0.85},
        "WEAK":      {"floor": 0.40, "ceiling": 0.70},
        "BREAKDOWN": {"floor": 0.40, "ceiling": 0.55},
    }
    comparisons = []
    for s in stats_list:
        hc = hardcoded.get(s.regime, {"floor": 0.45, "ceiling": 0.85})
        comparisons.append({
            "regime": s.regime,
            "sample_count": s.sample_count,
            "score": round(s.score, 3),
            "current_floor": hc["floor"],
            "current_ceiling": hc["ceiling"],
            "data_floor": s.suggested_floor,
            "data_ceiling": s.suggested_ceiling,
            "floor_delta": round(s.suggested_floor - hc["floor"], 3),
            "ceiling_delta": round(s.suggested_ceiling - hc["ceiling"], 3),
            "avg_fwd_5d": round(s.avg_fwd_5d * 100, 3),
            "win_rate_5d": round(s.win_rate_5d * 100, 1),
            "avg_fwd_5d_dd": round(s.avg_fwd_5d_dd * 100, 3),
        })
    return comparisons


# ── output ───────────────────────────────────────────────────────────────────


def generate_report(
    labels: list[DailyRegimeLabel],
    stats_list: list[RegimeStats],
    tag_analysis: list[dict[str, Any]],
    cross_analysis: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    """Write all outputs: CSVs, JSON, and markdown report."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Daily regime table CSV
    daily_rows = []
    for lab in labels:
        row = {
            "date": lab.date,
            "open": round(lab.open, 2),
            "high": round(lab.high, 2),
            "low": round(lab.low, 2),
            "close": round(lab.close, 2),
            "vwap": round(lab.vwap, 2),
            "volume": round(lab.volume, 0),
            "regime": lab.regime,
            "tags": "|".join(lab.tags),
            "confidence": round(lab.confidence, 3),
            "floor_pct": lab.target_floor_pct,
            "ceiling_pct": lab.target_ceiling_pct,
            "fwd_1d_return": round(lab.fwd_1d_return * 100, 3) if lab.fwd_1d_return is not None else None,
            "fwd_3d_return": round(lab.fwd_3d_return * 100, 3) if lab.fwd_3d_return is not None else None,
            "fwd_5d_return": round(lab.fwd_5d_return * 100, 3) if lab.fwd_5d_return is not None else None,
            "fwd_5d_max_dd": round(lab.fwd_5d_max_dd * 100, 3) if lab.fwd_5d_max_dd is not None else None,
        }
        daily_rows.append(row)
    pd.DataFrame(daily_rows).to_csv(output_dir / "daily_regime_table.csv", index=False, encoding="utf-8-sig")

    # 2. Regime stats JSON
    stats_dicts = [asdict(s) for s in stats_list]
    for d in stats_dicts:
        for k in ["avg_fwd_1d", "avg_fwd_3d", "avg_fwd_5d", "med_fwd_1d", "med_fwd_3d", "med_fwd_5d",
                   "p25_fwd_5d", "p75_fwd_5d", "avg_fwd_5d_dd", "avg_fwd_3d_dd",
                   "win_rate_1d", "win_rate_3d", "win_rate_5d", "score",
                   "suggested_floor", "suggested_ceiling"]:
            if k in d:
                d[k] = round(d[k], 6)
    (output_dir / "regime_stats.json").write_text(
        json.dumps(stats_dicts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 3. Tag analysis JSON
    (output_dir / "tag_analysis.json").write_text(
        json.dumps(tag_analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4. Cross analysis JSON
    (output_dir / "regime_tag_cross.json").write_text(
        json.dumps(cross_analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 5. Comparison JSON
    (output_dir / "param_comparison.json").write_text(
        json.dumps(comparisons, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 6. Markdown report
    md = _build_markdown_report(labels, stats_list, tag_analysis, cross_analysis, comparisons)
    (output_dir / "regime_research_report.md").write_text(md, encoding="utf-8")

    print(f"\n=== Regime Research Complete ===")
    print(f"Output directory: {output_dir}")
    print(f"  daily_regime_table.csv  ({len(labels)} days)")
    print(f"  regime_stats.json       ({len(stats_list)} regimes)")
    print(f"  tag_analysis.json       ({len(tag_analysis)} tags)")
    print(f"  regime_tag_cross.json   ({len(cross_analysis)} combinations)")
    print(f"  param_comparison.json   ({len(comparisons)} regimes)")
    print(f"  regime_research_report.md")


def _build_markdown_report(
    labels: list[DailyRegimeLabel],
    stats_list: list[RegimeStats],
    tag_analysis: list[dict[str, Any]],
    cross_analysis: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("# Regime Research Report\n")
    lines.append(f"交易日数: {len(labels)}\n")

    # Regime distribution
    lines.append("## 1. Regime 分布\n")
    lines.append("| Regime | 天数 | 占比 |")
    lines.append("|--------|-----:|-----:|")
    for s in stats_list:
        pct = s.sample_count / len(labels) * 100 if labels else 0
        lines.append(f"| {s.regime} | {s.sample_count} | {pct:.1f}% |")
    lines.append("")

    # Regime statistics
    lines.append("## 2. 各 Regime 未来收益统计\n")
    lines.append("| Regime | N | 1日均值 | 3日均值 | 5日均值 | 5日中位 | 5日P25 | 5日P75 | 5日均DD | 1日胜率 | 3日胜率 | 5日胜率 | Score |")
    lines.append("|--------|--:|--------:|--------:|--------:|--------:|-------:|-------:|--------:|--------:|--------:|--------:|------:|")
    for s in stats_list:
        lines.append(
            f"| {s.regime} | {s.sample_count} "
            f"| {s.avg_fwd_1d*100:.2f}% | {s.avg_fwd_3d*100:.2f}% | {s.avg_fwd_5d*100:.2f}% "
            f"| {s.med_fwd_5d*100:.2f}% | {s.p25_fwd_5d*100:.2f}% | {s.p75_fwd_5d*100:.2f}% "
            f"| {s.avg_fwd_5d_dd*100:.2f}% "
            f"| {s.win_rate_1d*100:.0f}% | {s.win_rate_3d*100:.0f}% | {s.win_rate_5d*100:.0f}% "
            f"| {s.score:.3f} |"
        )
    lines.append("")

    # Param comparison
    lines.append("## 3. 当前写死参数 vs 数据建议\n")
    lines.append("| Regime | N | Score | 当前Floor | 当前Ceil | 建议Floor | 建议Ceil | ΔFloor | ΔCeil | 5日均值 | 5日胜率 | 5日均DD |")
    lines.append("|--------|--:|------:|----------:|---------:|----------:|---------:|-------:|------:|--------:|--------:|--------:|")
    for c in comparisons:
        lines.append(
            f"| {c['regime']} | {c['sample_count']} | {c['score']:.3f} "
            f"| {c['current_floor']*100:.0f}% | {c['current_ceiling']*100:.0f}% "
            f"| {c['data_floor']*100:.0f}% | {c['data_ceiling']*100:.0f}% "
            f"| {c['floor_delta']*100:+.0f}% | {c['ceiling_delta']*100:+.0f}% "
            f"| {c['avg_fwd_5d']:.2f}% | {c['win_rate_5d']:.0f}% | {c['avg_fwd_5d_dd']:.2f}% |"
        )
    lines.append("")

    # Key finding: is the machine too conservative?
    lines.append("## 4. 保守性分析\n")
    total_days = len(labels)
    defensive_count = sum(1 for l in labels if l.regime in ("WEAK", "BREAKDOWN"))
    aggressive_count = sum(1 for l in labels if l.regime in ("TREND_UP", "REPAIR"))
    range_count = sum(1 for l in labels if l.regime == "RANGE")
    lines.append(f"- **防守状态 (WEAK+BREAKDOWN)**: {defensive_count} 天 ({defensive_count/total_days*100:.1f}%)")
    lines.append(f"- **进攻状态 (TREND_UP+REPAIR)**: {aggressive_count} 天 ({aggressive_count/total_days*100:.1f}%)")
    lines.append(f"- **中性状态 (RANGE)**: {range_count} 天 ({range_count/total_days*100:.1f}%)")
    lines.append("")

    # Check if WEAK/BREAKDOWN days actually had good forward returns
    weak_bd_labs = [l for l in labels if l.regime in ("WEAK", "BREAKDOWN") and l.fwd_5d_return is not None]
    if weak_bd_labs:
        avg_fwd5_wb = _mean([l.fwd_5d_return for l in weak_bd_labs])
        wr5_wb = sum(1 for l in weak_bd_labs if l.fwd_5d_return > 0) / len(weak_bd_labs)
        lines.append(f"- WEAK+BREAKDOWN 标记日的未来5日均值: **{avg_fwd5_wb*100:.2f}%**, 胜率: **{wr5_wb*100:.0f}%**")
        if avg_fwd5_wb > 0 and wr5_wb > 0.45:
            lines.append(f"  - ⚠️ **防守状态日后续平均收益为正且胜率接近半数，说明状态机标记了太多不该防守的日子**")
        elif avg_fwd5_wb > 0:
            lines.append(f"  - ⚠️ 防守状态日后续平均收益为正，可能存在过度防守")
    lines.append("")

    tu_labs = [l for l in labels if l.regime == "TREND_UP" and l.fwd_5d_return is not None]
    if tu_labs:
        avg_fwd5_tu = _mean([l.fwd_5d_return for l in tu_labs])
        wr5_tu = sum(1 for l in tu_labs if l.fwd_5d_return > 0) / len(tu_labs)
        lines.append(f"- TREND_UP 标记日的未来5日均值: **{avg_fwd5_tu*100:.2f}%**, 胜率: **{wr5_tu*100:.0f}%**")
        if avg_fwd5_tu > 0.02 and wr5_tu > 0.55:
            lines.append(f"  - ✅ TREND_UP 状态确实对应正向收益，状态识别有效")
        elif avg_fwd5_tu < 0:
            lines.append(f"  - ❌ TREND_UP 标记后收益为负，状态识别可能有问题")
    lines.append("")

    # Transition analysis
    lines.append("## 5. 状态转换矩阵\n")
    transitions: dict[tuple[str, str], int] = defaultdict(int)
    for i in range(len(labels) - 1):
        transitions[(labels[i].regime, labels[i + 1].regime)] += 1

    all_regimes = [r.value for r in MarketRegime]
    lines.append("| From \\ To | " + " | ".join(all_regimes) + " |")
    lines.append("|-----------|" + "|".join(["------:" for _ in all_regimes]) + "|")
    for r_from in all_regimes:
        row_vals = [str(transitions.get((r_from, r_to), 0)) for r_to in all_regimes]
        lines.append(f"| {r_from} | " + " | ".join(row_vals) + " |")
    lines.append("")

    # Tag analysis
    lines.append("## 6. Tag 独立分析\n")
    lines.append("| Tag | 天数 | 5日均值 | 5日均DD | 5日胜率 |")
    lines.append("|-----|-----:|--------:|--------:|--------:|")
    for t in sorted(tag_analysis, key=lambda x: x.get("avg_fwd_5d") or 0, reverse=True):
        avg5 = f"{t['avg_fwd_5d']:.2f}%" if t["avg_fwd_5d"] is not None else "N/A"
        dd5 = f"{t['avg_fwd_5d_dd']:.2f}%" if t["avg_fwd_5d_dd"] is not None else "N/A"
        wr5 = f"{t['win_rate_5d']:.0f}%" if t["win_rate_5d"] is not None else "N/A"
        lines.append(f"| {t['tag']} | {t['count']} | {avg5} | {dd5} | {wr5} |")
    lines.append("")

    # Regime × tag cross
    if cross_analysis:
        lines.append("## 7. Regime × Tag 交叉分析 (N≥3)\n")
        lines.append("| Regime | Tag | N | 1日均值 | 5日均值 | 5日均DD | 5日胜率 |")
        lines.append("|--------|-----|--:|--------:|--------:|--------:|--------:|")
        for c in sorted(cross_analysis, key=lambda x: x.get("avg_fwd_5d") or 0, reverse=True):
            avg1 = f"{c['avg_fwd_1d']:.2f}%" if c["avg_fwd_1d"] is not None else "N/A"
            avg5 = f"{c['avg_fwd_5d']:.2f}%" if c["avg_fwd_5d"] is not None else "N/A"
            dd5 = f"{c['avg_fwd_5d_dd']:.2f}%" if c["avg_fwd_5d_dd"] is not None else "N/A"
            wr5 = f"{c['win_rate_5d']:.0f}%" if c["win_rate_5d"] is not None else "N/A"
            lines.append(f"| {c['regime']} | {c['tag']} | {c['count']} | {avg1} | {avg5} | {dd5} | {wr5} |")
        lines.append("")

    # Conclusion
    lines.append("## 8. 结论\n")
    lines.append("### Score 计算方式\n")
    lines.append("```")
    lines.append("score = 0.40 * normalized(avg_future_5d_return)")
    lines.append("      + 0.25 * normalized(win_rate_5d - 0.5)")
    lines.append("      - 0.25 * normalized(avg_future_5d_drawdown)")
    lines.append("      + 0.10 * normalized(avg_future_1d_return)")
    lines.append("```\n")
    lines.append("### Score → 仓位带映射\n")
    lines.append("| Score 区间 | Floor | Ceiling |")
    lines.append("|-----------|------:|--------:|")
    lines.append("| ≥ 0.6 | 70% | 100% |")
    lines.append("| 0.2 ~ 0.6 | 55% | 85% |")
    lines.append("| -0.2 ~ 0.2 | 45% | 85% |")
    lines.append("| -0.6 ~ -0.2 | 40% | 70% |")
    lines.append("| < -0.6 | 40% | 55% |")
    lines.append("")

    # Actionable recommendations
    lines.append("### 建议\n")
    for c in comparisons:
        if c["floor_delta"] != 0 or c["ceiling_delta"] != 0:
            direction = []
            if c["floor_delta"] > 0:
                direction.append(f"提高 floor {c['floor_delta']*100:+.0f}%")
            elif c["floor_delta"] < 0:
                direction.append(f"降低 floor {c['floor_delta']*100:+.0f}%")
            if c["ceiling_delta"] > 0:
                direction.append(f"提高 ceiling {c['ceiling_delta']*100:+.0f}%")
            elif c["ceiling_delta"] < 0:
                direction.append(f"降低 ceiling {c['ceiling_delta']*100:+.0f}%")
            adj = ", ".join(direction)
            lines.append(f"- **{c['regime']}** (N={c['sample_count']}, score={c['score']:.3f}): {adj}")
            if c["sample_count"] < 8:
                lines.append(f"  - ⚠️ 样本数不足 8，建议不单独调参，并入相近状态")
    lines.append("")

    return "\n".join(lines)


# ── main ─────────────────────────────────────────────────────────────────────


def run_research(
    start_date: str = START_DATE,
    end_date: str | None = None,
    data_dir: str | Path = DATA_DIR,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict[str, Any]:
    print(f"Loading market data from {start_date}...")
    bundle: MarketDataBundle = load_market_data(start_date=start_date, end_date=end_date, data_dir=data_dir)
    df = bundle.frame
    print(f"  rows={len(df)}, files={len(bundle.files)}")

    print("Aggregating daily bars...")
    daily = aggregate_daily_bars(df)
    print(f"  trading days={len(daily)}")

    print("Labeling daily regimes (tick-by-tick)...")
    labels = label_daily_regimes(df, daily)
    print(f"  labeled days={len(labels)}")

    print("Computing forward returns...")
    compute_forward_returns(labels)

    print("Computing regime statistics...")
    stats_list = compute_regime_stats(labels)

    print("Computing tag analysis...")
    tag_analysis = compute_tag_analysis(labels)

    print("Computing regime × tag cross analysis...")
    cross_analysis = compute_regime_tag_cross(labels)

    print("Comparing current vs data-driven params...")
    comparisons = compare_params(stats_list)

    output_path = Path(output_dir)
    generate_report(labels, stats_list, tag_analysis, cross_analysis, comparisons, output_path)

    # Print summary to console
    print("\n=== Regime Distribution ===")
    for s in stats_list:
        pct = s.sample_count / len(labels) * 100 if labels else 0
        print(f"  {s.regime:12s}  {s.sample_count:3d} days ({pct:5.1f}%)  "
              f"score={s.score:+.3f}  "
              f"5d_avg={s.avg_fwd_5d*100:+.2f}%  "
              f"5d_wr={s.win_rate_5d*100:.0f}%  "
              f"5d_dd={s.avg_fwd_5d_dd*100:.2f}%  "
              f"band={s.suggested_floor*100:.0f}-{s.suggested_ceiling*100:.0f}%")

    print("\n=== Current vs Data-Driven Bands ===")
    for c in comparisons:
        print(f"  {c['regime']:12s}  "
              f"current={c['current_floor']*100:.0f}-{c['current_ceiling']*100:.0f}%  "
              f"data={c['data_floor']*100:.0f}-{c['data_ceiling']*100:.0f}%  "
              f"Δfloor={c['floor_delta']*100:+.0f}%  Δceil={c['ceiling_delta']*100:+.0f}%")

    return {
        "labels_count": len(labels),
        "stats": [asdict(s) for s in stats_list],
        "comparisons": comparisons,
        "output_dir": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Regime research: data-driven analysis of MarketRegime labels.")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    run_research(
        start_date=args.start_date,
        end_date=args.end_date,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
