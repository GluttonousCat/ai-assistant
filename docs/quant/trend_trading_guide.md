# 趋势行情捕捉与动量收益系统
## -- 量价共振 · 趋势生命周期 工程指南

> **版本：V1.0**
>
> **核心目标：** 从全市场标的中，尽早发现"正在形成的可交易趋势（Tradable Trend）"，在趋势被量能确认、结构完整后，通过回调入场、突破确认等事件获取趋势收益；并在量价背离出现时及时退出。
>
> **姊妹篇：** 《可交易震荡区间挖掘与波动收益系统》--本系统与其共享同一个市场状态机的两个工作区。
>
> **核心原则：**
>
> 1. 不预测单根K线涨跌，而识别市场状态--趋势是与震荡对偶的一种状态。
> 2. 趋势系统的核心问题不是"涨没涨"，而是"上涨是否被量能确认、结构是否支持延续"。
> 3. **量在价先**：无量的趋势不可信；量价背离是最早的衰竭前兆。
> 4. 入场不追突破，等回调（Pullback Entry 优于 Chase Entry）。
> 5. 退出不设固定目标，用 Trend Invalidation + 移动止损。
> 6. 趋势系统和震荡系统是**同一个状态机的两个工作区**，不是两套独立系统。
> 7. 第一阶段规则 + Event Study，第二阶段再引入机器学习。
> 8. Scanner 的目标是每天发现少量高质量趋势标的，不是买卖点。

---

# 一、系统总览：与震荡系统的关系

最重要的架构决策：**不要另起炉灶**。

现有系统已经隐含了完整的市场生命周期：

```text
        ┌────────────────── 震荡系统工作区 ──────────────────┐
        │                                                    │
TREND ─► TREND_DECAY ─► RANGE_FORMATION ─► EARLY/MATURE_RANGE
  ▲                                                         │
  │                                                         ▼
  │              BREAKOUT_FAILURE ◄──── 假突破 ◄──────  BREAKOUT
  │                   │                                     │
  │                   ▼                                     ▼
  └──────────── RANGE_RENEWAL                        【趋势系统从这里接管】
                                                        │
                                          BREAKOUT_CONFIRMED
                                                        │
                                                  TREND_FORMING
                                                        │
                                                  TREND_ESTABLISHED
                                                        │
                                                  TREND_MATURITY
                                                        │
                                                  TREND_EXHAUSTION
                                                        │
                                                   TREND_DECAY ──► (回到震荡工作区)
```

三个关键衔接点：

| 衔接点 | 震荡系统视角 | 趋势系统视角 |
|---|---|---|
| BREAKOUT | 区间失效警报 | **趋势候选的起点** |
| BREAKOUT_CONFIRMED | （原系统未细分） | 有效突破确认，趋势形成中 |
| TREND_DECAY | 区间机会来临 | 趋势仓位退出信号 |

工程含义：`range_trading/regime/state_machine.py` 的 `Regime` 枚举扩展三个状态
（`BREAKOUT_CONFIRMED` / `TREND_FORMING` / `TREND_ESTABLISHED` / `TREND_EXHAUSTION`），
两个 Scanner 共享特征层与状态机，各自输出自己的 Ranking。

---

# 二、核心概念：什么叫"可交易趋势"

把"趋势"从视觉概念变成数学对象。一个高质量 Tradable Trend 同时具有：

\[
TradableTrend
=
Directional\ Efficiency
+
VolumePrice\ Alignment
+
Structure\ Integrity
+
Controllable\ Pullback
+
Early\ Stage
\]

三个**不可交易**的反例（必须显式建模排除）：

```text
反例A: 消息脉冲趋势
  一日 +8% 巨量长阳, 随后三日缩量回落全部回吐
  -> 单日量能脉冲, 无持续结构

反例B: 宽震荡随机越界
  在 40% 宽度的大区间里, 价格触及上沿
  -> 是区间的一部分, 不是趋势的起点

反例C: 晚期赶顶
  趋势已运行 60+ 日, 量价背离已现, 斜率陡峭化
  -> 追入的期望收益为负
```

因此：

> **可交易趋势 = 高方向效率 + 量价共振 + 结构完整 + 回调浅且缩量 + 处于早中期。**

---

# 三、第一核心变量：Directional Efficiency 的回归

震荡系统用 `DI = 1 - ER` 检测方向失效；趋势系统直接用 `ER` 检测方向回归。
**同一个指标，两个视角**--这是复用而非新增。

## 3.1 多尺度 ER

```text
ER_10   短期效率 (突破后立即评估)
ER_20   中期效率 (趋势形成确认)
ER_40   中长期效率 (趋势延续性)
ER_60   长期效率 (与震荡系统 DI_60 互补)
```

重点观察（对偶于震荡系统的 DI↑ 传递）：

```text
突破日:     ER_10 ↑↑
3~5日后:    ER_20 ↑
趋势确立:   ER_40 ↑, ER_60 ↑
```

若 ER_10↑ 但 ER_40/ER_60 仍低 -> 可能只是大震荡里的局部运动（反例B）。

## 3.2 ER 的变化率与加速度

对偶于 DITrend / DIAcceleration：

\[
ERSlope = ER_t - ER_{t-k}
\]
\[
ERAccel = \Delta ER_t - \Delta ER_{t-1}
\]

真正重要的是：

```text
ER ↑  且  ER slope ↑
```

这表示：方向效率正在回归--原来的震荡正在让位于趋势。

## 3.3 与 DI 的联动判定

不要单独使用 ER。组合：

```text
趋势形成:   ER_20 > 0.55  且  DI_60 还在下降
趋势确立:   ER_20 > 0.55  且  ER_40 > 0.45  且  DI_60 < 0.45
衰竭预警:   ER_20 见顶回落 + 量价背离 (见第五章)
```

---

# 四、趋势结构：HH/HL 与回调质量

现有 `structure.py` 已有 `hh_rate / hl_rate / net_slope`，趋势系统深化为结构三件套。

## 4.1 结构完整性（Structure Integrity）

以摆动点（Swing High/Low，5~9 日窗口的分形极值）定义结构：

\[
Integrity = \frac{近期\ HH\ 数 + HL\ 数}{结构点总数}
\]

```text
强趋势:   HH → HL → HH → HL (每个回调低点高于前低)
结构破坏: 价格跌破最近的 HL (第一个警告)
结构崩溃: 跌破前一个 HL (趋势失效, Trend Invalidation)
```

## 4.2 回调深度（Pullback Depth）

\[
PullbackDepth = \frac{SwingHigh - Low_{pullback}}{SwingHigh - SwingLow_{prev}}
\]

```text
健康回调:   Depth < 0.4 (回调不超过前一波段的 40%)
深度回调:   0.4 ~ 0.6 (趋势可能转为宽幅)
结构风险:   > 0.6 (接近 HL 破坏)
```

## 4.3 回调量能（Pullback Volume）

**这是趋势系统最重要的量价特征之一：**

\[
PullbackVolRatio = \frac{回调段平均量}{上涨段平均量}
\]

```text
理想回调:   < 0.7  (缩量回调, 卖压不重)
可接受:     0.7 ~ 1.0
危险信号:   > 1.0  (放量下跌, 派发嫌疑)
```

---

# 五、量价体系（本系统的重头戏）

趋势与震荡的最大差异：震荡系统里量能是辅助证据，趋势系统里**量能是必要条件**。
现有 `volume.py` 已有 4 个基础特征，本节扩展为完整的五层量价体系。

## 5.1 第一层：量能水平（已有）

```python
vol_ratio   # 量比: 当日量 / 20日均量        (已有)
vol_pct     # 量能分位: 时序百分位             (已有)
```

趋势场景的用法：

```text
突破日:     vol_ratio > 2.0 且 vol_pct > 0.8   -> 有效突破的量能门槛
趋势延续:   上涨日 vol_pct 均值 > 0.5
衰竭:       价格新高但 vol_pct < 0.3
```

## 5.2 第二层：量能结构（新增）

### Up/Down Volume 分解

```python
def updown_vol_ratio(close, vol, window):
    """N 日内上涨日成交量之和 / 下跌日成交量之和"""
    up = vol.where(close.diff() > 0, 0.0).rolling(window).sum()
    down = vol.where(close.diff() < 0, 0.0).rolling(window).sum()
    return up / (down + EPS)
```

```text
健康趋势:   UDVOL > 1.5  (买方主导)
中性:       0.8 ~ 1.5
派发嫌疑:   < 0.8  (跌日量更大)
```

### 量能持续性 vs 脉冲（Volume Persistence）

区分"持续放量"与"单日脉冲"：

\[
VolPersistence = \frac{VolMA_5}{VolMA_{20}} \div \frac{MaxVol_5}{VolMA_{20}}
\]

简化实现：5 日均量与 20 日均量之比，配合 5 日内最大单日量的占比：

```text
持续型:     VolMA5/VolMA20 > 1.3 且 max_share < 0.5
脉冲型:     max_share > 0.6 (一天贡献大半量能) -> 反例A 特征
```

## 5.3 第三层：OBV 与量价同步（扩展已有 obv_slope）

现有 `obv_slope` 保留，新增两个更有力的同步度量：

### 价格-OBV 新高同步性

\[
OBVConfirm = I(OBV_t \geq \max(OBV_{t-N..t-1})) \cdot I(Close_t \geq \max(Close_{t-N..t-1}))
\]

```text
价格新高 + OBV 同期新高   -> 趋势健康 (双确认)
价格新高 + OBV 未创新高   -> 顶背离 (Bearish Divergence) ★衰竭核心证据
OBV 新高 + 价格未创新高   -> 底吸嫌疑 (Accumulation)
```

### 量价相关系数

```python
def price_volume_corr(close, obv, window):
    """价格与 OBV 的滚动相关: 趋势中应高度正相关"""
    return close.rolling(window).corr(obv)
```

```text
健康趋势:   PVCorr > 0.7
背离区:     PVCorr < 0.3 (量价脱钩, 衰竭前兆)
```

## 5.4 第四层：资金流（新增）

日K 数据可算的三件套：

```python
# 1. A/D Line (Accumulation/Distribution)
clv = ((close - low) - (high - close)) / (high - low + EPS)   # 收盘位置值
ad = (clv * vol).cumsum()

# 2. CMF (Chaikin Money Flow, 窗口 20)
cmf = (clv * vol).rolling(20).sum() / vol.rolling(20).sum()

# 3. MFI (Money Flow Index, 窗口 14, 类 RSI 但以成交额加权)
```

```text
CMF > 0.1   资金持续流入 (趋势多方确认)
CMF < 0     资金流出 (与上涨方向背离 -> 警告)
```

## 5.5 第五层：VWAP 重心（扩展已有 vwap_dev）

现有 `vwap_dev`（收盘偏离滚动 VWAP）保留，新增**重心斜率**：

\[
VWAPSlope = \frac{VWAP_t - VWAP_{t-k}}{k \cdot VWAP_t}
\]

```text
趋势健康:   价格在 VWAP 上方 且 VWAPSlope > 0 (重心抬升)
回调企稳:   价格回踩 VWAP 附近 且 VWAPSlope 仍 > 0  ★最佳买点区域
趋势破坏:   价格跌破 VWAP 且 VWAPSlope 转负
```

## 5.6 量价共振矩阵（Volume-Price Alignment Matrix）

把五层证据合成一个对齐分数。约定每个子项输出 ∈ [0, 1]：

```text
VPA = 0.25 × BreakoutVolume     (突破日量能: vol_ratio>2 → 1)
    + 0.25 × OBVSync            (双新高同步 / PVCorr 归一)
    + 0.20 × UDVOL              (上涨日量占比, 归一)
    + 0.15 × CMF                (资金流方向, 归一)
    + 0.15 × VWAPSupport        (价格 vs VWAP 且重心同向)
```

```text
VPA ≥ 0.7   量价共振 (趋势可信)
0.4 ~ 0.7   部分确认 (等更多证据)
< 0.4       量价不配合 (拒绝该趋势信号, 无论价格多漂亮)
```

**工程铁律：价格形态再好，VPA 不达标就不入场。这是本系统与"看图追涨"的本质区别。**

---

# 六、突破质量：Boundary Escape

对偶于震荡系统的 Boundary Rejection（边界反应）：

> 震荡系统问："价格触碰边界后是否被弹回？"
> 趋势系统问："价格越过边界后是否成功逃离（Escape）？"

## 6.1 突破三要素

```text
1. 越界:     Close > Upper (收盘越界, 不是最高价越界)
2. 量能:     vol_ratio > 2.0 (突破日量能门槛)
3. 站稳:     后续 N 日收盘不回到 Upper 之下
```

## 6.2 Breakout-Retest 模式（最高胜率的确认路径）

```text
Day 0:  放量突破 Upper
Day 1~5: 缩量回调, 触及 Upper 附近 (原上沿变支撑)
Day K:  回踩不破 + 再度放量上行
        -> RETEST_CONFIRMED
```

```python
def breakout_retest(close, vol, upper, horizon=5):
    """突破后 horizon 内回踩原边界不破且缩量"""
    broke = close > upper
    retest = (close.shift(-1) < ...)   # 逐日判定, 实现时滚动构造
```

## 6.3 假突破过滤（False Breakout Filter，对偶 Trend Pullback Filter）

趋势系统的四大假阳性专章（详见第十八章）中，最重要的一项：

```text
过滤条件 (任一命中即降权):
1. 突破日 vol_ratio < 1.5        -> 缩量突破, 假概率高
2. 区间宽度 > 35% 且 Age > 20D   -> 宽震荡随机越界
3. 突破当日 long_upper_wick > 60% -> 上影线吞没, 日内被拒
4. 突破后 2 日内收回区间          -> BREAKOUT_FAILURE 快速确认
```

---

# 七、趋势阶段：Stage Detection

借用 Weinstein 四阶段模型，用可量化规则简化：

```text
Stage 1 积累:  ER 低 + 量能萎缩 + 价格在长期均线下方走平
Stage 2 上升:  ER 高 + HH/HL + 价格上穿并站稳 MA200/MA120 ★唯一做多区
Stage 3 派发:  ER 降 + 量价背离 + 高位宽幅
Stage 4 下降:  ER 高(向下) + LH/LL + 价格破 MA200
```

量化判定（日K）：

```python
def trend_stage(close, ma_long=120, er_window=40, obv_slope_w=40):
    above_ma = close > close.rolling(ma_long).mean()
    er = efficiency_ratio(close, er_window)
    obv_up = obv_slope(...) > 0
    if above_ma and er > 0.45:            return 2   # 上升
    if not above_ma and er > 0.45:        return 4   # 下降
    if above_ma and er < 0.35 and not obv_up: return 3  # 派发
    return 1                                            # 积累
```

**工程含义：趋势信号只在 Stage 2 内有效。Stage 3/4 中出现的"突破"一律降权或忽略。**

---

# 八、Trend Quality Score (TQS)

对偶于震荡系统的五分项 DailyRangeScore：

\[
TQS = 0.30\ Efficiency + 0.25\ VolumePrice + 0.20\ Structure
    + 0.15\ Sustainability + 0.10\ Stage
\]

### Efficiency（效率）
```text
ER_20 水平 + ER slope + 净位移斜率 (net_slope 归一)
```

### VolumePrice（量价）
```text
VPA 共振矩阵 (第五章) 原样输入
```

### Structure（结构）
```text
HH/HL 完整性 + 回调深度 (越浅越好) + 价格相对 VWAP 位置
```

### Sustainability（可持续性）
```text
量能持续性 (非脉冲) + 回调缩量 + CMF 为正
```

### Stage（阶段）
```text
Stage 2 早期 = 100 分, Stage 2 中期 = 70, Stage 2 晚期(斜率陡峭化+背离) = 40
Stage 1/3/4 = 0
```

### 状态阈值（初始工程参数，待校准）

```text
TQS < 40      无趋势 / 观察
40 ~ 60       TREND_FORMING (形成中)
60 ~ 75       TREND_ESTABLISHED (确立, 可入场)
75 ~ 90       HIGH_QUALITY_TREND
> 90          罕见; 若同时出现背离证据 -> 反而警惕晚期
```

---

# 九、状态机扩展

在现有 `Regime` 枚举上新增（不改动已有状态的语义）：

```python
class Regime(str, Enum):
    # ... 现有 8 状态保持不变 ...
    BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"   # 突破三要素齐备
    TREND_FORMING      = "TREND_FORMING"        # TQS >= 40 持续 confirm
    TREND_ESTABLISHED  = "TREND_ESTABLISHED"    # TQS >= 60 持续 confirm
    TREND_EXHAUSTION   = "TREND_EXHAUSTION"     # 衰竭证据 (见第十一章)
```

转移规则（延续现有状态机的确认/降级机制）：

```text
BREAKOUT
  │ 突破三要素成立 (越界+量能+站稳 confirm_days)
  ▼
BREAKOUT_CONFIRMED
  │ TQS >= 40 连续 confirm_days
  ▼
TREND_FORMING
  │ TQS >= 60 连续 confirm_days 且 Stage=2
  ▼
TREND_ESTABLISHED
  │ 出现衰竭证据组合 (背离 + ER回落 + HL破坏)
  ▼
TREND_EXHAUSTION ──► TREND_DECAY (交给震荡系统)
```

降级路径：任一趋势状态中 TQS 连续 `fallback_days` 低于 40 → 回 BREAKOUT/原状态。

---

# 十、入场事件（对偶震荡系统的 Event A~D）

趋势系统的四类 Long Event。**全部要求量价双确认。**

## Event 1：Volume Breakout（放量突破）

```text
条件: Close > Upper
    + vol_ratio > 2.0
    + VPA >= 0.6
    + 非宽震荡区间 (width < 35% 或 Age < 20D)
仓位: 最小试探仓 (突破入场的胜率低于回调, 用仓位补偿)
```

## Event 2：Pullback Entry（回调企稳）★核心事件

```text
前置: 处于 TREND_ESTABLISHED
条件: 回调深度 < 0.4
    + 回调量能比 < 0.7 (缩量回调)
    + 价格回踩 VWAP 或 EMA20 附近 (|vwap_dev| < 1.5%)
    + 当日出现企稳信号 (阳线 / 下影 / 量比回升)
仓位: 标准仓 (本系统最高胜率入场)
止损: 回调低点下方 (结构化止损)
```

## Event 3：VCP 再突破（波动收缩）

```text
前置: 趋势中的整理期
条件: 连续 >= 3 段收缩: 每段回调深度递减
    + 量能逐级萎缩 (VolMA5 递降)
    + 最终向上突破整理上沿 + 放量
```

## Event 4：Retest Confirmation（突破回踩确认）

```text
前置: 近 horizon 日内发生过 Event 1
条件: 回踩原 Upper 不破 (±1% 容差)
    + 回踩缩量
    + 再度放量收阳
仓位: 在 Event 1 基础上加仓
```

## Entry Score

\[
EntryScore = 0.30\ EventType + 0.25\ TQS + 0.25\ VPA + 0.20\ Location
\]

Location：相对 VWAP / EMA20 的位置（回调买点深度越好分越高，追高递减）。

---

# 十一、退出：Trend Invalidation

**不设固定盈利目标。** 趋势收益靠"截断亏损、让利润奔跑"实现。

## 11.1 三重衰竭确认（Trend Exhaustion）

任两项命中即 TREND_EXHAUSTION：

```text
1. 量价背离: 价格 20 日新高 + OBV 未新高 (或 PVCorr < 0.3)
2. ER 回落:  ER_20 从高点回落 > 0.15
3. 结构破坏: 跌破最近 HL
```

## 11.2 Chandelier 移动止损（工程默认）

\[
Stop = \max(High_{22D}) - 3 \times ATR_{14}
\]

逐日上移，不下调。趋势系统用移动止损替代"止盈"。

## 11.3 分批减仓规则

```text
TREND_EXHAUSTION 确认  -> 减 1/2
跌破 Chandelier        -> 清仓
跌破前一个 HL          -> 无条件清仓 (结构崩溃)
```

---

# 十二、Scanner 输出与两系统合并

趋势 Scanner 输出（对偶震荡 Ranking Table）：

| Symbol | Trend State | TQS | Stage | VPA | ER_20 | HH/HL | 回调深度 | Event | Entry |
|---|---|---:|---:|---:|---:|---|---:|---|---:|
| A | ESTABLISHED | 78 | 2早 | 0.82 | 0.62 | 完整 | 0.32 | Pullback | 91 |
| B | FORMING | 55 | 2早 | 0.58 | 0.51 | 完整 | 0.45 | Breakout | 72 |

**合并日报**：两个 Scanner 的 Top N 合并输出，按系统标注：

```text
[震荡] 605377.SH  FORMATION  score 85  下沿候选
[趋势] XXXXXX.SH  ESTABLISHED TQS 78  回调企稳事件
```

资金协同（第十四章）决定两类信号的优先级。

---

# 十三、信号跟踪与健康监控（复用现有基础设施）

`signal_tracking` 表结构完全复用，趋势信号的事后指标**不同**：

```text
震荡信号看:  persist_ratio / range_holding   (区间是否维持)
趋势信号看:  MFE / MAE / 持续天数 / 最大回撤比

trend_mfe        入场后 N 日最大有利偏移 (ATR 倍数)
trend_mae        入场后 N 日最大不利偏移
trend_days       信号到衰竭确认的交易日数
profit_ratio     MFE / |MAE| (趋势质量的事后度量)
```

滚动健康监控同样复用：**近期趋势信号的平均 MFE 是否稳定**、
按 TQS 分档的 profit_ratio 是否单调（对偶震荡系统的分数单调性检验）。

---

# 十四、两系统资金协同

## 14.1 市场级 Regime Gate

用指数（如 000300.SH）的日K跑同一套状态机，得到市场 Regime：

```text
指数 TREND  向上   -> 趋势策略权重 70% / 震荡 30%
指数 RANGE         -> 震荡 70% / 趋势 30%
指数 TREND 向下    -> 趋势(做空视角, 若可融券) / 震荡 30% / 现金优先
```

## 14.2 个股级互斥

同一标的不允许同时持有两类信号：

```text
标的处于 RANGE 族状态  -> 只接受震荡信号
标的处于 TREND 族状态  -> 只接受趋势信号
状态切换日             -> 双方都暂停一天 (等尘埃落定)
```

## 14.3 对冲组合视角

```text
震荡信号: 期望"均值回归", 彼此相关性低, 可多标的分散
趋势信号: 期望"右尾收益", 彼此相关性高(同涨), 需控制总仓位
```

---

# 十五、工程目录与代码映射

复用现有 `range_trading/`，新增模块最小化：

```text
range_trading/
├── features/
│   ├── directional.py    # 复用: ER/DI 不改 (趋势用 ER, 震荡用 DI)
│   ├── volume.py         # 扩展: +updown_vol_ratio +vol_persistence
│   │                     #      +price_volume_corr +obv_confirm
│   │                     #      +ad_line +cmf +vwap_slope
│   ├── structure.py      # 扩展: +swing_points +pullback_depth
│   │                     #      +structure_integrity
│   └── breakout.py       # 新增: 突破质量 + retest + 假突破过滤
├── scoring/
│   └── trend_score.py    # 新增: TQS 五分项 + VPA 矩阵
├── regime/
│   └── state_machine.py  # 扩展: +4 趋势状态与转移
├── scanner/
│   └── trend_scan.py     # 新增: 趋势 Ranking (复用 loader/universe)
└── research/
    ├── signal_tracking.py # 扩展: trend 事后指标字段
    └── event_study.py     # 复用方法论, 新写趋势版标签
```

配置同样集中在 `config.py`，新增 `TrendConfig` dataclass（对偶 `DailyConfig`），
所有阈值/权重不写死在特征里（延续第五十二章原则）。

## 15.1 TrendConfig 默认参数建议

```python
@dataclass
class TrendConfig:
    er_windows: tuple = (10, 20, 40, 60)
    er_confirm: float = 0.55           # ER_20 趋势门槛
    # 量价
    breakout_vol_ratio: float = 2.0    # 突破日量比门槛
    udvol_full: float = 1.5            # UDVOL 满分参考
    pv_corr_low: float = 0.3           # 量价脱钩警戒
    cmf_window: int = 20
    vpa_weights: dict = {...}          # 五层权重 (第八章)
    vpa_min_entry: float = 0.6         # 入场 VPA 门槛
    # 结构
    swing_window: int = 5              # 摆动点分形窗口
    pullback_max: float = 0.4          # 健康回调上限
    pullback_vol_max: float = 0.7      # 回调量能比上限
    # 状态机
    tqs_forming: float = 40.0
    tqs_established: float = 60.0
    confirm_days: int = 3
    # 退出
    chandelier_window: int = 22
    chandelier_atr: float = 3.0
    exhaustion_divergence: bool = True
```

---

# 十六、回测与验证路线（对偶 VALIDATION.md）

沿用震荡系统的验证方法论，趋势版的核心标签：

## 16.1 趋势持续标签（对偶"区间维持"）

在信号日 t，未来 K 日内满足：

```text
1. 收益:    Close_{t+K} / Close_t > 1 + 2×ATR%   (方向兑现)
2. 途中:    MAE < 1.5 ATR                          (不是先深回撤再涨)
3. 结构:    未跌破 t 时刻的最近 HL
```

则 `TrendValid_t = 1`。

## 16.2 必须分层验证的维度

```text
按 TQS 分档     -> 单调性检验 (对偶分数单调性)
按 VPA 分档     -> 量价确认的增量价值 (VPA 高 vs 低的趋势结局差异)
按 Stage        -> Stage2 早期 vs 晚期
按市场 Regime   -> 指数趋势/震荡/下跌中的表现差异
按 EventType    -> Pullback vs Breakout 的胜率/盈亏比对比
```

## 16.3 统计口径提醒

趋势收益是**右尾分布**：中位数意义不大，必须看
`期望值 = 胜率×平均盈利 - 败率×平均亏损`，并按 Event Study 事件驱动回测
（避免用当日最高/最低价成交的隐性前视，延续第四十五章纪律）。

---

# 十七、研究优先级（不要同时开发所有）

```text
Phase 1: 量价特征有效性 (VPA 各子项对趋势持续标签的 IC)
Phase 2: 突破质量 (Escape vs 假突破的判别力)
Phase 3: 趋势持续标签 + Event Study (复用方法论)
Phase 4: TQS 打分 + 状态机扩展
Phase 5: 入场事件 (Pullback 优先于 Breakout)
Phase 6: 退出与移动止损 (Chandelier 参数)
Phase 7: 两系统协同 (Regime Gate + 资金分配)
Phase 8: ML (P(TrendValid), E[MFE])
```

---

# 十八、假阳性专章：趋势系统的四大陷阱

对偶于震荡系统的"假阳性"章节，趋势系统同样需要专门的防护：

```text
陷阱1: 震荡区间随机越界
  防护: 区间宽度 > 35% 且 Age > 20D 的突破降权
        ER_40/ER_60 未跟上时只给观察不给信号

陷阱2: 缩量假突破
  防护: breakout_vol_ratio 门槛是硬条件, 不满足直接否决

陷阱3: 消息面单日脉冲 (One-day Wonder)
  防护: VolPersistence 过滤 (单日量占比 > 60% 的突破降权)
        + 次日/三日收盘确认

陷阱4: 高位赶顶追入
  防护: Stage 检测 (Stage3 一票否决)
        + 趋势年龄 > 60D 且出现背离 -> 只减仓不开新仓
```

---

# 十九、最终架构

```text
                     指数日K (市场 Regime Gate)
                              │
                 ┌────────────┴────────────┐
                 │                         │
           RANGE 工作区               TREND 工作区
                 │                         │
     DI↑ FlipRate↑ AC1↓          ER↑ VPA↑ Structure↑
     Boundary Formation          Boundary Escape
                 │                         │
         Early Range Score          Trend Quality Score
                 │                         │
     ┌───────────┴───────────┐   ┌─────────┴─────────┐
     │ 边界事件 (下沿红K等)   │   │ Pullback / Breakout│
     │ Range Invalidity 止损  │   │ Chandelier 移动止损│
     └───────────┬───────────┘   └─────────┬─────────┘
                 │                         │
                 └────────────┬────────────┘
                              ▼
                    信号跟踪 + 滚动健康监控
                    (persist vs MFE/MAE 双口径)
```

## 最终设计原则

**震荡系统赚"价格回归"的钱，趋势系统赚"价格持续"的钱；
前者用边界定义机会，后者用结构定义风险；
两者共用一套特征层与状态机，由市场与个股的 Regime 决定谁在工作。**

而趋势系统真正应该被量化、被预测的是：

\[
\boxed{
P(\text{突破后 K 日内形成可交易趋势}) \quad 与 \quad E[\text{MFE} | \text{Pullback Entry}]
}
\]

一旦这两个量能被稳定提前估计，趋势 Scanner 就有了核心能力：
**在全市场中，每天寻找"刚被量能确认、结构完整、处于早中期的趋势"，然后等回调。**

---

# 二十、与现有代码的落地对照表

| 指南章节 | 现有代码 | 需要做的 |
|---|---|---|
| 第三章 ER | `directional.py` | 直接复用 `efficiency_ratio`，新增列输出 |
| 第四章 结构 | `structure.py` | 新增 swing/pullback/structure 函数 |
| 第五章 量价 | `volume.py` (4特征) | 扩展 7 个新函数（5.2/5.3/5.4/5.5） |
| 第六章 突破 | `boundary.py` + `candle.py` | 新增 `breakout.py` |
| 第八章 TQS | `scoring/daily_score.py` | 新增 `trend_score.py`（结构对偶） |
| 第九章 状态机 | `regime/state_machine.py` | 枚举 +4 状态，转移规则扩展 |
| 第十二章 Scanner | `scanner/daily_scan.py` | 新增 `trend_scan.py` 复用 loader |
| 第十三章 跟踪 | `research/signal_tracking.py` | 表加趋势指标字段 |
| 前端 | `web/src/App.jsx` | Ranking 表复用，新增 Trend 页签 |

预计新增代码量 ~600 行（不含测试），全部落在现有骨架上。
