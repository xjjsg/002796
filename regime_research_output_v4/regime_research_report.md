# Regime Research Report

交易日数: 99

## 1. Regime 分布

| Regime | 天数 | 占比 |
|--------|-----:|-----:|
| OVERSOLD_BOUNCE | 7 | 7.1% |
| HIGH_VOLUME_TREND | 6 | 6.1% |
| EXHAUSTION | 39 | 39.4% |
| RANGE | 47 | 47.5% |

## 2. 各 Regime 未来收益统计

| Regime | N | 1日均值 | 3日均值 | 5日均值 | 5日中位 | 5日P25 | 5日P75 | 5日均DD | 1日胜率 | 3日胜率 | 5日胜率 | Score |
|--------|--:|--------:|--------:|--------:|--------:|-------:|-------:|--------:|--------:|--------:|--------:|------:|
| OVERSOLD_BOUNCE | 7 | -0.79% | 4.57% | 7.40% | 3.61% | 0.53% | 12.24% | 6.53% | 29% | 71% | 86% | 0.512 |
| HIGH_VOLUME_TREND | 6 | -1.21% | 3.59% | 3.90% | 3.25% | -5.50% | 8.21% | 5.82% | 33% | 50% | 50% | 0.072 |
| EXHAUSTION | 39 | -0.68% | -0.44% | -0.70% | -1.36% | -5.76% | 7.74% | 7.33% | 44% | 41% | 38% | -0.248 |
| RANGE | 47 | 2.16% | 3.02% | 5.24% | 5.57% | -7.20% | 15.01% | 7.87% | 61% | 67% | 61% | 0.188 |

## 3. 当前写死参数 vs 数据建议

| Regime | N | Score | 当前Floor | 当前Ceil | 建议Floor | 建议Ceil | ΔFloor | ΔCeil | 5日均值 | 5日胜率 | 5日均DD |
|--------|--:|------:|----------:|---------:|----------:|---------:|-------:|------:|--------:|--------:|--------:|
| OVERSOLD_BOUNCE | 7 | 0.512 | 45% | 85% | 55% | 85% | +10% | +0% | 7.40% | 86% | 6.53% |
| HIGH_VOLUME_TREND | 6 | 0.072 | 45% | 85% | 45% | 85% | +0% | +0% | 3.90% | 50% | 5.82% |
| EXHAUSTION | 39 | -0.248 | 45% | 85% | 40% | 70% | -5% | -15% | -0.70% | 38% | 7.33% |
| RANGE | 47 | 0.188 | 45% | 85% | 45% | 85% | +0% | +0% | 5.24% | 61% | 7.87% |

## 4. 保守性分析

- **防守状态 (WEAK+BREAKDOWN)**: 0 天 (0.0%)
- **进攻状态 (TREND_UP+REPAIR)**: 0 天 (0.0%)
- **中性状态 (RANGE)**: 47 天 (47.5%)



## 5. 状态转换矩阵

| From \ To | OVERSOLD_BOUNCE | HIGH_VOLUME_TREND | EXHAUSTION | RANGE |
|-----------|------:|------:|------:|------:|
| OVERSOLD_BOUNCE | 2 | 0 | 1 | 4 |
| HIGH_VOLUME_TREND | 0 | 0 | 2 | 4 |
| EXHAUSTION | 4 | 3 | 24 | 8 |
| RANGE | 1 | 3 | 12 | 30 |

## 6. Tag 独立分析

| Tag | 天数 | 5日均值 | 5日均DD | 5日胜率 |
|-----|-----:|--------:|--------:|--------:|
| break_major_low | 4 | 9.61% | 5.36% | 100% |
| high_volume | 17 | 7.21% | 5.90% | 65% |
| break_recent_low | 9 | 6.35% | 6.86% | 78% |
| above_ma5 | 54 | 3.74% | 7.63% | 56% |
| below_vwap | 48 | 3.45% | 6.77% | 51% |
| lower_highs | 35 | 2.90% | 5.99% | 59% |
| above_vwap | 51 | 2.49% | 8.04% | 55% |
| below_ma5 | 45 | 1.98% | 7.18% | 50% |
| higher_lows | 69 | 0.01% | 8.26% | 45% |

## 7. Regime × Tag 交叉分析 (N≥3)

| Regime | Tag | N | 1日均值 | 5日均值 | 5日均DD | 5日胜率 |
|--------|-----|--:|--------:|--------:|--------:|--------:|
| RANGE | high_volume | 4 | 6.21% | 25.65% | 1.98% | 100% |
| RANGE | below_vwap | 9 | 3.17% | 16.78% | 4.28% | 88% |
| OVERSOLD_BOUNCE | break_major_low | 3 | 0.73% | 8.44% | 5.72% | 100% |
| OVERSOLD_BOUNCE | below_ma5 | 7 | -0.79% | 7.40% | 6.53% | 86% |
| OVERSOLD_BOUNCE | below_vwap | 7 | -0.79% | 7.40% | 6.53% | 86% |
| OVERSOLD_BOUNCE | break_recent_low | 7 | -0.79% | 7.40% | 6.53% | 86% |
| RANGE | lower_highs | 8 | 2.47% | 6.91% | 5.80% | 71% |
| OVERSOLD_BOUNCE | higher_lows | 5 | -1.87% | 6.71% | 7.00% | 80% |
| OVERSOLD_BOUNCE | lower_highs | 4 | -0.34% | 5.92% | 5.75% | 75% |
| RANGE | above_ma5 | 32 | 1.88% | 5.41% | 8.54% | 59% |
| RANGE | below_ma5 | 15 | 2.80% | 4.85% | 6.33% | 64% |
| HIGH_VOLUME_TREND | above_ma5 | 6 | -1.21% | 3.90% | 5.82% | 50% |
| HIGH_VOLUME_TREND | above_vwap | 6 | -1.21% | 3.90% | 5.82% | 50% |
| HIGH_VOLUME_TREND | high_volume | 6 | -1.21% | 3.90% | 5.82% | 50% |
| RANGE | above_vwap | 38 | 1.95% | 2.81% | 8.62% | 55% |
| HIGH_VOLUME_TREND | lower_highs | 4 | -1.75% | 1.99% | 4.74% | 50% |
| HIGH_VOLUME_TREND | higher_lows | 3 | 1.61% | 1.20% | 7.60% | 67% |
| EXHAUSTION | lower_highs | 19 | -0.04% | 0.97% | 6.38% | 53% |
| EXHAUSTION | above_ma5 | 16 | -0.09% | 0.34% | 6.49% | 50% |
| EXHAUSTION | higher_lows | 34 | -0.58% | 0.04% | 6.92% | 41% |
| EXHAUSTION | above_vwap | 7 | -1.75% | -0.49% | 6.76% | 57% |
| EXHAUSTION | high_volume | 6 | -0.98% | -0.70% | 8.87% | 50% |
| EXHAUSTION | below_vwap | 32 | -0.45% | -0.75% | 7.45% | 34% |
| RANGE | higher_lows | 27 | 1.16% | -1.40% | 10.26% | 41% |
| EXHAUSTION | below_ma5 | 23 | -1.10% | -1.42% | 7.91% | 30% |

## 8. 结论

### Score 计算方式

```
score = 0.40 * normalized(avg_future_5d_return)
      + 0.25 * normalized(win_rate_5d - 0.5)
      - 0.25 * normalized(avg_future_5d_drawdown)
      + 0.10 * normalized(avg_future_1d_return)
```

### Score → 仓位带映射

| Score 区间 | Floor | Ceiling |
|-----------|------:|--------:|
| ≥ 0.6 | 70% | 100% |
| 0.2 ~ 0.6 | 55% | 85% |
| -0.2 ~ 0.2 | 45% | 85% |
| -0.6 ~ -0.2 | 40% | 70% |
| < -0.6 | 40% | 55% |

### 建议

- **OVERSOLD_BOUNCE** (N=7, score=0.512): 提高 floor +10%
  - ⚠️ 样本数不足 8，建议不单独调参，并入相近状态
- **EXHAUSTION** (N=39, score=-0.248): 降低 floor -5%, 降低 ceiling -15%
