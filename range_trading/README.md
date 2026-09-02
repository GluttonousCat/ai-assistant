# range_trading — 日K层实现

基于《可交易震荡区间挖掘与波动收益系统：30F - 60F - 日K 工程指南》实现的多周期体系中的 **日K层**。
当前仅有日K数据（`stock.daily`），因此 V1 只实现日K职责：**环境与中期结构识别**——
回答"这个标的未来几天到几周是否可能存在稳定的震荡机会？"（指南第十五章）。

60F（Early Range Detection 核心）与 30F（Trading Event）待分时数据接入后扩展。

## 目录结构与指南章节映射

```
range_trading/
├── config.py            # 全量参数（窗口/满分参考/阈值/权重），对应指南第五十二章"阈值不写死"
├── data/loader.py       # universe 筛选 + 分批拉取 + 前复权重构
├── features/
│   ├── directional.py   # DI / DI斜率 / DI加速度 / FlipRate / AC1        （指南三~七节）
│   ├── volatility.py    # ATR / NATR / NATR时序分位 / ATR稳定性          （指南八节）
│   ├── boundary.py      # Quantile边界 / RangePos / 稳定性 / 密度 / 触碰反应（指南九~十二节）
│   ├── structure.py     # EMA斜率 / 净位移 / HH-HL率                      （指南15.2B、三十七~三十九节）
│   └── candle.py        # Bullish/Bearish Event / 假突破 / 假跌破         （指南二十四、四十节）
├── scoring/daily_score.py   # 五分项 DailyRangeScore + 假阳性扣减          （指南二十、三十八节）
├── regime/
│   ├── state_machine.py # 8状态机 + RangeState 数据结构                    （指南十四、五十七章）
│   └── daily_regime.py  # 特征→打分→状态机 全流程入口
├── scanner/daily_scan.py    # 全市场慢扫描 CLI + Ranking 报告              （指南三十二、三十三章）
└── tests/               # 合成数据单元测试（不依赖数据库）
```

## 使用

```bash
# 单标的日K报告（指南第十七章输出格式 + 状态演变）
python -m range_trading.scanner.daily_scan --symbol 000001.SZ

# 全市场扫描（universe: ≥200根日K 且 日均成交额≥1000万元）
python -m range_trading.scanner.daily_scan --top 20

# 指定日期 / 调试
python -m range_trading.scanner.daily_scan --date 2026-08-14 --limit 100
```

结果落盘 `output/range_scan/scan_YYYYMMDD.{csv,json}`。

Python API：

```python
from range_trading.regime import run_daily_regime

detail_df, state = run_daily_regime(df, symbol="000001.SZ")   # df: trade_date/open/high/low/close/vol/amount
print(state.state, state.range_score, state.upper, state.lower)
```

## 关键设计决策

1. **前复权**：`stock.daily` 的 `adj_factor/close_adj` 列尚未回填（全 NULL），
   loader 用 `pct_chg` 累计收益重构前复权（与 tushare qfq 数学等价，以窗口最后一日为基准）。
   待 adj 数据回填后可切换。
2. **无前视**：所有特征只用 t 及之前数据；边界反应（MFE/ATR）的 5 日未来窗口通过
   `shift(horizon)` 滞后回填到成熟时刻（有单元测试验证）。
3. **假阳性防护**（指南三十七~三十九节）：HH/HL 率 + 净位移斜率 + 长周期 DI 构成
   TrendContinuationEvidence，作为扣减项（最多 35 分）；过宽区间（>60%）额外扣减（指南三十四节）。
4. **状态机升级需连续 confirm_days 满足**（日K转换更慢），降级需连续 fallback_days 不满足，
   避免状态抖动；假突破进入 BREAKOUT_FAILURE 而非丢弃（指南四十节）。
5. **所有阈值集中在 `DailyConfig`**，支持 parameter sweep / walk-forward 校准（指南四十六章）。

## 后续路线（指南五十九章优先级）

- [ ] Phase 2/3：Event Study（Range Start 标注）与 Walk-forward 阈值校准
- [ ] 60F Early Range Detector（需分时数据 / 日K重采样不可替代）
- [ ] 30F Trading Event（Boundary × Event × Regime）
- [ ] 回测引擎（事件驱动，避免隐性 look-ahead，指南四十五章）
