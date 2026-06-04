# Regime Research Report

交易日数: 99

## 1. Regime 分布

| Regime | 天数 | 占比 |
|--------|-----:|-----:|
| OVERSOLD_BOUNCE | 7 | 7.1% |
| HIGH_VOLUME_TREND | 6 | 6.1% |
| EXHAUSTION | 31 | 31.3% |
| RANGE | 55 | 55.6% |

## 2. 各 Regime 未来收益统计

| Regime | N | 1日均值 | 3日均值 | 5日均值 | 5日中位 | 5日P25 | 5日P75 | 5日均DD | 1日胜率 | 3日胜率 | 5日胜率 | Score |
|--------|--:|--------:|--------:|--------:|--------:|-------:|-------:|--------:|--------:|--------:|--------:|------:|
| OVERSOLD_BOUNCE | 7 | -0.79% | 4.57% | 7.40% | 3.61% | 0.53% | 12.24% | 6.53% | 29% | 71% | 86% | 0.512 |
| HIGH_VOLUME_TREND | 6 | -1.21% | 3.59% | 3.90% | 3.25% | -5.50% | 8.21% | 5.82% | 33% | 50% | 50% | 0.072 |
| EXHAUSTION | 31 | -0.61% | -0.46% | -0.25% | -1.26% | -5.76% | 7.74% | 7.24% | 45% | 42% | 39% | -0.226 |
| RANGE | 55 | 1.70% | 2.52% | 4.10% | 4.20% | -7.20% | 12.98% | 7.84% | 57% | 63% | 57% | 0.111 |

## 3. 当前写死参数 vs 数据建议

| Regime | N | Score | 当前Floor | 当前Ceil | 建议Floor | 建议Ceil | ΔFloor | ΔCeil | 5日均值 | 5日胜率 | 5日均DD |
|--------|--:|------:|----------:|---------:|----------:|---------:|-------:|------:|--------:|--------:|--------:|
| OVERSOLD_BOUNCE | 7 | 0.512 | 45% | 85% | 55% | 85% | +10% | +0% | 7.40% | 86% | 6.53% |
| HIGH_VOLUME_TREND | 6 | 0.072 | 45% | 85% | 45% | 85% | +0% | +0% | 3.90% | 50% | 5.82% |
| EXHAUSTION | 31 | -0.226 | 45% | 85% | 40% | 70% | -5% | -15% | -0.25% | 39% | 7.24% |
| RANGE | 55 | 0.111 | 45% | 85% | 45% | 85% | +0% | +0% | 4.10% | 57% | 7.84% |

## 4. 保守性分析

- **防守状态 (WEAK+BREAKDOWN)**: 0 天 (0.0%)
- **进攻状态 (TREND_UP+REPAIR)**: 0 天 (0.0%)
- **中性状态 (RANGE)**: 55 天 (55.6%)



## 5. 状态转换矩阵

| From \ To | OVERSOLD_BOUNCE | HIGH_VOLUME_TREND | EXHAUSTION | RANGE |
|-----------|------:|------:|------:|------:|
| OVERSOLD_BOUNCE | 2 | 0 | 1 | 4 |
| HIGH_VOLUME_TREND | 0 | 0 | 2 | 4 |
| EXHAUSTION | 4 | 3 | 13 | 11 |
| RANGE | 1 | 3 | 15 | 35 |

## 6. Tag 独立分析

| Tag | 天数 | 5日均值 | 5日均DD | 5日胜率 |
|-----|-----:|--------:|--------:|--------:|
| break_major_low | 4 | 9.61% | 5.36% | 100% |
| lower_highs | 14 | 7.96% | 5.09% | 77% |
| high_volume | 13 | 6.70% | 6.89% | 54% |
| break_recent_low | 9 | 6.35% | 6.86% | 78% |
| above_ma5 | 54 | 3.74% | 7.63% | 56% |
| below_vwap | 48 | 3.45% | 6.77% | 51% |
| above_vwap | 51 | 2.49% | 8.04% | 55% |
| below_ma5 | 45 | 1.98% | 7.18% | 50% |
| higher_lows | 69 | 0.01% | 8.26% | 45% |

## 7. Regime × Tag 交叉分析 (N≥3)

| Regime | Tag | N | 1日均值 | 5日均值 | 5日均DD | 5日胜率 |
|--------|-----|--:|--------:|--------:|--------:|--------:|
| RANGE | high_volume | 4 | 5.25% | 20.30% | 3.46% | 75% |
| RANGE | below_vwap | 11 | 2.80% | 12.57% | 5.22% | 70% |
| EXHAUSTION | lower_highs | 3 | -1.47% | 10.52% | 3.15% | 100% |
| OVERSOLD_BOUNCE | break_major_low | 3 | 0.73% | 8.44% | 5.72% | 100% |
| OVERSOLD_BOUNCE | lower_highs | 3 | -0.73% | 7.83% | 5.39% | 67% |
| OVERSOLD_BOUNCE | below_ma5 | 7 | -0.79% | 7.40% | 6.53% | 86% |
| OVERSOLD_BOUNCE | below_vwap | 7 | -0.79% | 7.40% | 6.53% | 86% |
| OVERSOLD_BOUNCE | break_recent_low | 7 | -0.79% | 7.40% | 6.53% | 86% |
| RANGE | lower_highs | 8 | 2.47% | 6.91% | 5.80% | 71% |
| OVERSOLD_BOUNCE | higher_lows | 5 | -1.87% | 6.71% | 7.00% | 80% |
| RANGE | below_ma5 | 15 | 2.80% | 4.85% | 6.33% | 64% |
| HIGH_VOLUME_TREND | above_ma5 | 6 | -1.21% | 3.90% | 5.82% | 50% |
| HIGH_VOLUME_TREND | above_vwap | 6 | -1.21% | 3.90% | 5.82% | 50% |
| HIGH_VOLUME_TREND | high_volume | 6 | -1.21% | 3.90% | 5.82% | 50% |
| RANGE | above_ma5 | 40 | 1.31% | 3.84% | 8.36% | 55% |
| EXHAUSTION | above_ma5 | 8 | 0.79% | 3.11% | 5.32% | 62% |
| RANGE | above_vwap | 44 | 1.45% | 2.18% | 8.43% | 54% |
| HIGH_VOLUME_TREND | higher_lows | 3 | 1.61% | 1.20% | 7.60% | 67% |
| EXHAUSTION | below_vwap | 30 | -0.57% | -0.51% | 7.35% | 37% |
| EXHAUSTION | higher_lows | 30 | -0.57% | -0.51% | 7.35% | 37% |
| RANGE | higher_lows | 31 | 0.92% | -0.68% | 9.41% | 45% |
| EXHAUSTION | below_ma5 | 23 | -1.10% | -1.42% | 7.91% | 30% |
| EXHAUSTION | high_volume | 3 | -5.29% | -5.87% | 13.61% | 33% |

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
- **EXHAUSTION** (N=31, score=-0.226): 降低 floor -5%, 降低 ceiling -15%
