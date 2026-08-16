# 可交易震荡区间挖掘与波动收益系统
## ——30F / 60F / 日K 多周期 Early Range Detection 工程指南

> **版本：V1.0**
>
> **核心目标：** 从全市场标的中，尽可能早地发现正在形成的“可交易震荡区间（Tradable Range）”，并在区间形成后，通过边界位置、红K/反转事件、假突破等信号获取波动收益。
>
> **核心原则：**
>
> 1. 不预测单根K线涨跌，而识别市场状态。
> 2. 不等待震荡已经成熟，而检测 `Trend → Range` transition。
> 3. 不把低波动等同于震荡；真正需要的是**低方向性 + 足够波动**。
> 4. 不把“红K”本身作为 alpha，而研究 `Regime × Location × Event`。
> 5. 日K、60F、30F不是同一套指标缩放后使用，而是承担不同职责。
> 6. 第一阶段以规则 + Event Study 为主，第二阶段再引入机器学习。
> 7. Scanner 的目标不是产生买卖点，而是每天发现少量高质量研究对象。

---

# 一、系统总览

整个系统建议设计为四层：

```text
                    全市场 Universe
                           │
                           ▼
                ┌────────────────────┐
                │ ① Market Regime    │
                │    日K             │
                └─────────┬──────────┘
                          │
                中长期环境 / 大结构
                          │
                          ▼
                ┌────────────────────┐
                │ ② Range Formation  │
                │    60F             │
                └─────────┬──────────┘
                          │
                 Early Range / Range
                          │
                          ▼
                ┌────────────────────┐
                │ ③ Trading Setup    │
                │    30F             │
                └─────────┬──────────┘
                          │
               Boundary + Event
                          │
                          ▼
                ┌────────────────────┐
                │ ④ Execution        │
                │ Entry / Exit / Risk │
                └────────────────────┘
```

三套周期的职责明确区分：

| 周期 | 核心职责 | 主要问题 |
|---|---|---|
| 日K | 大结构、环境、长期 Range | “这个标的是否处于适合震荡交易的中期环境？” |
| 60F | **Early Range Detection** | “震荡是不是正在形成？” |
| 30F | **Trading Event Detection** | “现在是否到了边界附近的可交易时刻？” |

不要做：

```text
日K RSI
60F RSI
30F RSI
然后三个一起打分
```

这会造成高度相关的重复信息。

应该做：

```text
日K：定义环境
60F：识别状态转换
30F：寻找交易事件
```

---

# 二、核心概念：什么叫“可交易震荡区间”

我们需要把“震荡”从视觉概念变成数学对象。

一个高质量 Tradable Range 同时具有以下特征：

```text
                    High Boundary
                ───────────────────
                  ↑      ↓     ↑
                       ↓
                 ↑         ↓
                    ↑   ↓
                ↓         ↑
                ───────────────────
                    Low Boundary
```

它不是简单的：

> 价格波动小。

而是：

> **价格运动路径很长，但净位移较小；同时存在稳定的上下价格边界，并且触碰边界后有较强的反应。**

因此：

\[
TradableRange
=
Directional\ Inefficiency
+
Boundary\ Stability
+
Boundary\ Rejection
+
Sufficient\ Volatility
\]

---

# 三、Directional Inefficiency

这是整个系统最核心的变量。

定义：

\[
Path_N =
\sum_{i=1}^{N}|C_i-C_{i-1}|
\]

定义净位移：

\[
Displacement_N =
|C_t-C_{t-N}|
\]

Efficiency Ratio：

\[
ER_N =
\frac{Displacement_N}{Path_N+\epsilon}
\]

Directional Inefficiency：

\[
DI_N = 1-ER_N
\]

于是：

```text
趋势：

100 → 105 → 110 → 115 → 120

Path ≈ 20
Displacement ≈ 20

ER ≈ 1
DI ≈ 0
```

震荡：

```text
100 → 110 → 102 → 115 → 104 → 112

Path 很大
Displacement 较小

ER ↓
DI ↑
```

因此：

> **DI 不是判断“价格有没有波动”，而是判断“波动有没有产生方向”。**

这是本系统的核心。

---

# 四、最重要的不是 DI，而是 DI 的变化

如果：

\[
DI_t = 0.75
\]

只能说明现在已经低方向性。

但我们真正希望提前发现：

\[
DI_{t-5}=0.25
\]

逐渐变成：

\[
DI_t=0.65
\]

所以需要：

\[
DITrend =
DI_t-DI_{t-k}
\]

以及：

\[
DIAcceleration =
\Delta DI_t-\Delta DI_{t-1}
\]

真正重要的是：

```text
DI ↑
DI slope ↑
DI acceleration ↑
```

这表示：

> **原来的趋势正在失去方向效率。**

---

# 五、多尺度 DI

不能只使用一个窗口。

建议每个周期至少使用三个尺度：

```text
短周期 DI
中周期 DI
长周期 DI
```

例如 60F：

```text
DI_8
DI_16
DI_32
```

检测：

```text
DI_8   ↑↑
DI_16  ↑
DI_32  ↑
```

含义：

> 最近的方向性首先失效，然后逐渐传递到更长周期。

这比单独 `DI_20 > threshold` 更适合 Early Detection。

---

# 六、第二个核心变量：反向运动频率

DI 上升并不一定代表震荡。

可能只是：

```text
上涨
↓
剧烈回调
↓
继续上涨
```

因此加入 Return Flip Rate。

定义：

\[
FlipRate_N =
\frac{
\sum_{i=2}^{N}
I(sign(r_i)\neq sign(r_{i-1}))
}{
N-1
}
\]

趋势：

```text
+ + + + + + - +
```

FlipRate 低。

震荡：

```text
+ - + - + - + -
```

FlipRate 高。

所以：

\[
DI↑ + FlipRate↑
\]

是非常重要的组合。

---

# 七、第三个核心变量：Return Autocorrelation

计算：

\[
AC_1 = Corr(r_t,r_{t-1})
\]

震荡中的价格通常存在明显的反向运动倾向，因此：

\[
AC_1 < 0
\]

是一个重要辅助证据。

但是注意：

**不要单独使用 AC1。**

因为极端行情、微观结构噪声和跳跃也会造成负相关。

正确使用方式：

```text
DI ↑
+
FlipRate ↑
+
AC1 ↓
```

形成组合证据。

---

# 八、第四个核心变量：波动必须保留

这是本系统与普通“低波动震荡识别”的关键区别。

你要的不是：

```text
低波动 + 低方向
```

而是：

```text
低方向 + 足够波动
```

定义：

\[
NATR =
\frac{ATR_N}{Close}
\]

同时计算：

\[
NATRPercentile
\]

最终希望：

```text
DI ↑
FlipRate ↑
NATR 不低
```

因此：

> **高质量震荡 = 高波动但低方向性。**

---

# 九、第五个核心变量：价格边界

Directional Inefficiency 只能说明：

> “价格开始来回走。”

但不能证明：

> “存在可以交易的上下沿。”

因此需要 Boundary Detection。

不要简单使用：

```python
rolling_high
rolling_low
```

因为一个异常 spike 会严重污染区间。

优先使用：

\[
Upper = Quantile(Price, 90\%)
\]

\[
Lower = Quantile(Price, 10\%)
\]

或者：

```text
Price Histogram
Volume Profile
KDE
Swing Cluster
```

寻找：

> **Price Acceptance Area**

---

# 十、Range Position

定义：

\[
RangePos =
\frac{Price-Lower}
{Upper-Lower}
\]

得到：

```text
0.00        下沿
0.20        下部
0.50        中部
0.80        上部
1.00        上沿
```

这是 30F 交易系统最重要的变量之一。

因为：

```text
RangePos ≈ 0.05
```

和：

```text
RangePos ≈ 0.50
```

即使红K完全相同，交易价值也完全不同。

---

# 十一、Boundary Stability

边界必须稳定。

定义：

\[
UpperSlope \approx 0
\]

\[
LowerSlope \approx 0
\]

同时：

\[
RangeWidth =
Upper-Lower
\]

在一段时间内不能持续扩大。

可以定义：

\[
BoundaryStability
=
1-
NormalizedSlope
\]

以及：

\[
WidthStability
=
1-
|\Delta RangeWidth|
\]

---

# 十二、Boundary Rejection

一个价格边界是否值得交易，不是看它“画出来好不好看”。

而是看：

> **历史上价格触碰这个区域以后，是否发生明显反应。**

例如下沿：

```text
Touch 1 → +2.1 ATR
Touch 2 → +1.7 ATR
Touch 3 → +2.4 ATR
```

可以定义：

\[
SupportResponse =
Mean(MFE/ATR)
\]

同理：

\[
ResistanceResponse
\]

---

# 十三、Range Quality

建议最终建立：

\[
RangeQuality =
w_1 DI
+w_2 FlipRate
+w_3 NegativeAC
+w_4 BoundaryStability
+w_5 BoundaryResponse
+w_6 Volatility
\]

而不是：

```text
RangeQuality = ADX < 20
```

---

# 十四、Early Range Detection 的状态机

整个系统使用状态机，而不是单一分类器。

```text
TREND
  │
  │ DI开始上升
  │ FlipRate开始上升
  ▼
TREND_DECAY
  │
  │ 上下价格结构开始形成
  ▼
RANGE_FORMATION
  │
  │ Boundary稳定 + 多次反应
  ▼
EARLY_TRADABLE_RANGE
  │
  │ Range成熟
  ▼
MATURE_RANGE
  │
  ├───────────────┐
  │               │
  ▼               ▼
BREAKOUT       BREAKOUT_FAILURE
                  │
                  ▼
             RANGE_RENEWAL
```

其中最重要的是：

```text
TREND_DECAY
RANGE_FORMATION
EARLY_TRADABLE_RANGE
```

这三个状态。

---

# 十五、日K系统：负责“环境与中期结构”

日K不应该照搬60F。

日K的任务不是捕捉某一个交易点。

它回答：

> **“这个标的未来几天到几周是否可能存在稳定的震荡机会？”**

---

## 15.1 日K观察窗口

建议：

```text
短期：10D
中期：20D / 30D
长期：60D / 120D
```

---

## 15.2 日K核心 Feature

### A. 长周期 Directional Inefficiency

建议：

```text
DI_10
DI_20
DI_40
DI_60
```

重点观察：

```text
DI_20 ↑
DI_40 ↑
DI_60 ↑
```

但日K的阈值应该明显比 30F/60F 更宽。

---

### B. 中期趋势斜率

例如：

```text
EMA20 slope
EMA60 slope
```

关注：

\[
Slope_{20}\rightarrow0
\]

而不是机械判断：

```text EMA20 < EMA60
```

---

### C. 中期 Price Acceptance

使用：

```text
20D / 30D price distribution
```

寻找：

```text
高密度价格区域
```

---

### D. Range Width

例如：

\[
RangeWidth =
\frac{Upper-Lower}{Mid}
\]

同时计算：

```text
RangeWidth slope
```

---

### E. Daily Range Age

判断当前结构已经存在多久。

例如：

```text
Age = 3D
Age = 8D
Age = 20D
```

---

# 十六、日K Early Range Detector

日K的状态转换应该更慢。

例如：

```text
Day -10
强趋势

Day -7
趋势减速

Day -5
DI开始上升

Day -3
价格开始反复测试区域

Day -1
Upper / Lower开始稳定

Day 0
Early Daily Range
```

因此日K可以产生：

```text
DailyRangeProbability
```

例如：

\[
P(Range_{next\ 5\sim15D}=1)
\]

---

# 十七、日K最重要的输出

日K系统最终不应该告诉30F：

> “现在可以买。”

而应该输出：

```text
DailyRegime = RANGE_FORMING
```

以及：

```text
DailyRangeHigh
DailyRangeLow
DailyRangeScore
DailyRangeAge
DailySupport
DailyResistance
```

例如：

```text
Symbol: XXX

Daily Regime:
EARLY_RANGE

Range:
1180 ~ 1320

RangeScore:
88

Age:
5D

Support:
1210

Resistance:
1305

TrendRisk:
Low
```

然后交给60F。

---

# 十八、60F系统：整个项目的核心

60F 是：

# Early Range Formation Detector

它回答：

> **“这个震荡是不是正在形成？”**

---

## 18.1 60F窗口

推荐：

```text
DI：
8 / 16 / 32

Range：
24 / 32 / 48

Boundary：
24 / 36 / 48

Volatility：
14 / 24
```

这些不是最终参数，而是 V1 的研究起点。

必须通过 Event Study / Walk-forward 优化。

---

# 十九、60F核心 Feature

建议：

```text
DI_8
DI_16
DI_32

ΔDI_8
ΔDI_16
ΔDI_32

FlipRate_8
FlipRate_16

AC1_16

NATR_14
NATR_percentile

UpperQuantile
LowerQuantile

UpperSlope
LowerSlope

RangeWidth
RangeWidthSlope

PriceDensity

TouchCount

RejectionStrength
```

---

# 二十、60F最核心的 Early Range Score

建议第一版：

\[
ERS_{60}
=
0.30 TrendFailure
+
0.25 Inefficiency
+
0.20 RangeFormation
+
0.15 BoundaryEvidence
+
0.10 VolatilityQuality
\]

其中：

### TrendFailure

```text
DI slope
DI acceleration
ADX decay
MA slope decay
```

### Inefficiency

```text
DI
FlipRate
AC1
```

### RangeFormation

```text
Upper stability
Lower stability
Price density
Range width stability
```

### BoundaryEvidence

```text
Touch count
Rejection
Failed breakout
```

### VolatilityQuality

```text
NATR percentile
ATR stability
```

---

# 二十一、60F状态划分

建议：

```text
ERS < 40
TREND

40 ~ 60
TREND_DECAY

60 ~ 75
RANGE_FORMATION

75 ~ 90
EARLY_TRADABLE_RANGE

> 90
HIGH_QUALITY_RANGE
```

注意：

**这些阈值只是初始工程参数，不是真实 alpha。**

最终应该由历史数据校准。

---

# 二十二、30F系统：不负责发现 Range

这是非常重要的架构原则。

60F说：

> “这里可能存在一个 Range。”

30F才说：

> “现在可以交易了。”

30F应该专注：

```text
Location
+
Price Action
+
Volatility Event
```

---

# 二十三、30F的核心任务

假设60F已经得到：

```text
RangeHigh = 1300
RangeLow  = 1200
```

30F计算：

\[
RangePos =
\frac{Price-1200}{100}
\]

---

## 下沿交易区域

例如：

```text
RangePos < 0.20
```

重点寻找：

```text
Bullish Reversal
False Breakdown
Large Bullish Candle
Lower Low Reclaim
Volume Expansion
```

---

## 上沿交易区域

例如：

```text
RangePos > 0.80
```

重点寻找：

```text
Bearish Reversal
False Breakout
Upper Rejection
Large Bearish Candle
```

---

# 二十四、30F红K不能简单定义为 Close > Open

定义 Bullish Event：

\[
BodyRatio =
\frac{Close-Open}{High-Low}
\]

以及：

\[
ExpansionRatio =
\frac{TR}{ATR_{20}}
\]

然后：

```text
Close > Open
BodyRatio > threshold
ExpansionRatio > threshold
```

进一步加入：

```text
Volume / VolumeMA
CloseLocation
LowerWick
PreviousLowBreak
```

---

# 二十五、30F最重要的四类 Long Event

## Event A：Lower Boundary Reversal

```text
RangeLow
──────────────

       ↓
      Low
       ↓
      █
      ███
       ↑
      红K
```

条件：

```text
RangePos < 0.20
+
Bullish Event
```

---

## Event B：False Breakdown

```text
RangeLow
───────────────

       ↓
       ↓
       █
       █
       ↑
      ███
      Close重新站回
```

条件：

\[
Low_t < Lower
\]

同时：

\[
Close_t > Lower
\]

这是非常值得重点研究的 setup。

---

## Event C：Reclaim

例如跌破短期结构后重新站回：

```text
Previous Low
────────────

      ↓
      ↓
      █
      ███
       ↑
```

---

## Event D：Expansion Reversal

区间内突然出现：

```text
TR / ATR >> 1
```

同时：

```text
Close > Open
```

这类事件可能意味着：

> Range 内部的短期波动开始扩张。

---

# 二十六、30F不只看当前K，还要看“前置路径”

一个红K：

```text
100 → 102
```

和：

```text
100 → 94 → 96 → 101
```

完全不同。

后者可能是：

> **下破失败 + 强势回收**

因此 30F Event 必须包含：

```text
Previous 3~6 bars
```

的上下文。

---

# 二十七、30F Entry Score

建议：

\[
EntryScore =
0.30 Location
+
0.25 ReversalEvent
+
0.20 RangeQuality
+
0.15 VolatilityExpansion
+
0.10 Volume
\]

例如：

```text
Location             95
Reversal Event       88
Range Quality        91
Volatility Expansion 84
Volume               76

EntryScore           89
```

---

# 二十八、三周期如何组合

这是整个系统最终的 Hierarchical Architecture。

```text
                     日K
                      │
             中期市场结构判断
                      │
          ┌───────────┴───────────┐
          │                       │
       RANGE                  TREND
          │                       │
          ▼                       │
         60F                      │
          │                       │
    Early Range Detection         │
          │                       │
    ┌─────┴─────┐                 │
    │           │                 │
  Forming    Mature               │
    │           │                 │
    ▼           ▼                 │
   30F         30F                 │
    │           │                  │
 Boundary    Boundary              │
 Event       Event                 │
    │           │                  │
    └─────┬─────┘                  │
          ▼                        │
       Entry                       │
```

---

# 二十九、三个周期的核心差异

| 项目 | 日K | 60F | 30F |
|---|---|---|---|
| 主要目的 | 环境识别 | Early Range | Entry |
| 预测对象 | 中期结构 | Range形成 | 下一段波动 |
| 时间尺度 | 数天~数周 | 数小时~数天 | 数小时 |
| 核心指标 | 中期DI | DI transition | Price Action |
| Range检测 | 粗粒度 | 核心 | 使用60F结果 |
| Boundary | 中期 | 核心 | 精细化 |
| 红K | 不重要 | 辅助 | 核心 |
| Volume | 辅助 | 重要 | 重要 |
| Execution | 否 | 否 | 是 |

---

# 三十、不要把三个周期的 Score 简单相乘

例如不要：

\[
Score =
DailyScore
\times
60FScore
\times
30FScore
\]

因为它们不是独立变量。

更合理的是：

```text
Daily：Gate
60F：Primary Score
30F：Entry Score
```

例如：

```text
Daily Regime:
ALLOW_RANGE_TRADING

60F:
EarlyRangeScore = 87

30F:
EntryScore = 91
```

最终：

\[
OpportunityScore
=
0.45 ERS_{60}
+
0.40 EntryScore_{30}
+
0.15 DailyContext
\]

---

# 三十一、日K应该作为 Gate，而不是强制条件

例如：

```text
Daily:
Strong Uptrend
```

并不意味着：

> 绝对不能做震荡。

但它意味着：

> 下沿做多的胜率可能更高，上沿做空风险更大。

因此日K应该提供：

```text
LongBias
ShortBias
TrendRisk
RangeCompatibility
```

而不是简单：

```text BUY / SELL
```

---

# 三十二、真正的 Scanner 输出

每天运行后，不应该输出几百个股票。

最终输出应该是一个 Ranking Table：

| Symbol | Daily Regime | 60F ERS | Range | Age | 30F Event | Location | Entry |
|---|---|---:|---|---:|---|---:|---:|
| A | Early Range | 92 | 1200-1320 | 4 | False Breakdown | 0.08 | 93 |
| B | Range | 88 | 800-920 | 11 | Reclaim | 0.14 | 89 |
| C | Formation | 84 | 100-115 | 3 | Bullish Expansion | 0.19 | 86 |

---

# 三十三、Scanner应该分两个阶段运行

## 第一阶段：全市场慢扫描

每天对所有标的计算：

```text
Daily
60F
```

筛选：

```text
Daily compatible
AND
60F ERS > threshold
```

得到：

```text
Top 50
```

---

## 第二阶段：30F实时/准实时扫描

只对 Top 50：

```text
30F Event Detection
```

得到：

```text
Top 10
```

这样计算量和噪声都明显下降。

---

# 三十四、Range Start 的 Label 是整个机器学习阶段的核心

不要一开始预测收益。

先定义：

> 什么叫“一个真正的可交易 Range 开始形成”？

例如：

在时刻 \(t\)，未来 \(K\) 根K线内满足：

### 1. Directional Inefficiency

\[
DI > threshold
\]

### 2. Range Width

\[
Width_{min}
<
RangeWidth
<
Width_{max}
\]

### 3. Boundary Stability

上下边界斜率较低。

### 4. Boundary Rejection

至少出现：

```text
2次以上有效反应
```

### 5. Tradable Volatility

\[
NATR > MinVol
\]

则：

\[
Label_t = 1
\]

---

# 三十五、真正要训练的不是“未来涨跌”

第一阶段训练：

\[
P(RangeForm_{t:t+K}=1|X_t)
\]

也就是：

> **未来 K 根K线内形成高质量 Range 的概率。**

第二阶段：

\[
P(TradeableRange)
\]

第三阶段才研究：

\[
E[MFE]
\]

以及：

\[
E[MAE]
\]

---

# 三十六、Event Study 必须先于 ML

对每个 Range Start：

记录：

```text
t-10
t-8
t-6
t-5
t-4
t-3
t-2
t-1
t
```

的：

```text
DI
ΔDI
FlipRate
AC1
ATR
NATR
BoundaryWidth
PriceDensity
MA slope
Volume
```

然后观察：

> 哪些 feature 在 Range 开始前就已经发生结构性变化？

例如最终可能发现：

```text
t-5:
DI_8 ↑

t-4:
FlipRate ↑

t-3:
AC1 ↓

t-2:
Upper / Lower 稳定

t-1:
第一轮边界 rejection

t:
Range confirmed
```

这才是真正的 Early Signal。

---

# 三十七、必须特别研究“假阳性”

Early Detection 最大的问题不是漏掉 Range。

而是：

> 把正常回调误判成 Range。

典型：

```text
上涨趋势
   ↓
突然回调
   ↓
DI ↑
   ↓
系统误判 Range
   ↓
继续上涨
```

因此需要专门建立：

# Trend Pullback Filter

---

# 三十八、Trend Pullback Filter

重点检测：

```text
Long-term MA slope
Higher High / Higher Low
DI_long
Breakout continuation
Volume structure
```

例如：

```text
DI_8 ↑
但 DI_32 仍很低
且价格始终保持 Higher Low
```

那么更可能是：

> Trend Pullback

而不是：

> Range Formation

所以：

\[
RangeProbability
=
TransitionEvidence
-
TrendContinuationEvidence
\]

---

# 三十九、另一个重要的假阳性：高波动趋势

例如：

```text
100
110
103
115
108
122
116
130
```

DI可能下降。

但它仍然是趋势。

所以必须检查：

\[
NetSlope
\]

以及：

\[
HigherHighRate
\]

例如：

```text
HigherHighRate 高
+
HigherLowRate 高
```

说明：

> 虽然波动很大，但方向仍然存在。

---

# 四十、Range Breakout Failure 是系统中的特殊状态

例如：

```text
Range High
────────────────────
                 ↑
                 │
                1435
                 │
                 ↓
                 ↓
────────────────────
```

如果：

\[
High > Upper
\]

但：

\[
Close < Upper
\]

且后续重新回到 Range：

这是：

```text
BREAKOUT_FAILURE
```

这个状态不能直接丢弃。

因为它可能是非常好的交易机会：

```text
上破失败
→ 回归 Range
→ 下一个目标 Range Mid
```

---

# 四十一、Range 内部应该划分三个交易区域

不要把整个 Range 都视为可交易。

```text
Upper 20%
──────────────
卖出/等待反转区

20% ~ 80%
──────────────
Noise / Hold / 不主动开仓

Lower 20%
──────────────
买入/等待反转区
```

因此：

> **Range 是环境，Boundary 是机会。**

---

# 四十二、Entry 的目标也不应该固定为“涨多少”

对于下沿做多：

\[
Target =
RangeMid
\]

第一目标通常应该是：

```text
Range Mid
```

而不是无限期待：

```text
Upper Boundary
```

可以做：

```text
TP1 = Mid
TP2 = Upper
```

这样与震荡的统计特性更加一致。

---

# 四十三、止损应该围绕“Range Invalidity”

不要简单：

```text
止损 = -2%
```

而应该：

```text
Range Low
     ↓
False Breakdown
     ↓
重新收回
```

如果之后：

```text
再次有效跌破 Range Low
+
DI重新下降/趋势开始形成
```

那么：

> 原来的 Range 假设失效。

所以：

\[
Stop =
RangeInvalidation
\]

而不是固定百分比。

---

# 四十四、Position Sizing

最终仓位应该与：

```text
Range Width
ATR
MAE
Liquidity
Confidence
```

有关。

例如：

\[
PositionSize
\propto
\frac{RiskBudget}
{ExpectedMAE}
\]

不同 Range：

```text
窄Range + 高胜率
宽Range + 高波动
```

不能使用相同仓位。

---

# 四十五、回测必须采用事件驱动

不能：

```text
每天计算指标
然后看未来价格
```

必须严格按照：

```text
t时刻可见信息
        ↓
生成Signal
        ↓
t+1可执行
        ↓
计算MFE/MAE
```

尤其避免：

```text
使用当日最高价/最低价
同时假设在当日某价格成交
```

这种隐性 look-ahead。

---

# 四十六、必须做 Walk-Forward

推荐：

```text
Train
──────────────
Validation
──────────────
Test
──────────────
Walk Forward
```

而不是：

```text
2015~2025
全部优化
```

---

# 四十七、必须分市场状态做统计

至少拆：

```text
Bull Market
Bear Market
High Volatility
Low Volatility
Index Range
Index Trend
```

因为：

> Range strategy 在不同市场环境下的表现可能完全不同。

---

# 四十八、必须分 Range 类型

至少：

```text
Type A
正常水平震荡

Type B
Trend → Range

Type C
Breakout Failure → Range

Type D
High Volatility Range

Type E
Low Volatility Compression
```

不要把它们混成一个策略。

---

# 四十九、最终可以形成四个 Alpha Module

## Module A：Range Formation

预测：

\[
P(Range)
\]

---

## Module B：Boundary Alpha

预测：

\[
P(Reversion|Boundary)
\]

---

## Module C：Bullish Event

预测：

\[
P(PositiveMFE|LowerBoundary,BullishEvent)
\]

---

## Module D：Breakout Failure

预测：

\[
P(ReturnToRange|FailedBreakout)
\]

最终组合：

```text
Range Formation
        ↓
Boundary
        ↓
Event
        ↓
Expected MFE
        ↓
Risk-adjusted Opportunity
```

---

# 五十、推荐的工程目录

```text
range_trading/
│
├── config/
│   ├── daily.yaml
│   ├── 60m.yaml
│   └── 30m.yaml
│
├── data/
│   ├── loader.py
│   ├── adjustment.py
│   ├── resample.py
│   └── universe.py
│
├── features/
│   ├── directional.py
│   ├── volatility.py
│   ├── boundary.py
│   ├── structure.py
│   └── candle.py
│
├── regime/
│   ├── daily_regime.py
│   ├── range_transition_60m.py
│   └── state_machine.py
│
├── events/
│   ├── bullish_event_30m.py
│   ├── bearish_event_30m.py
│   ├── false_breakout.py
│   └── reclaim.py
│
├── scoring/
│   ├── daily_score.py
│   ├── range_score.py
│   └── entry_score.py
│
├── scanner/
│   ├── daily_scan.py
│   ├── 60m_scan.py
│   └── 30m_scan.py
│
├── research/
│   ├── event_study.py
│   ├── labels.py
│   ├── walk_forward.py
│   └── diagnostics.py
│
├── backtest/
│   ├── engine.py
│   ├── execution.py
│   ├── position.py
│   └── risk.py
│
└── reports/
    ├── daily_candidates.py
    └── range_dashboard.py
```

---

# 五十一、Feature API 建议

所有 Feature 都统一输入：

```python
df
```

输出：

```python
pd.Series
```

例如：

```python
def directional_inefficiency(close, window):
    path = close.diff().abs().rolling(window).sum()
    displacement = close.diff(window).abs()
    er = displacement / (path + 1e-12)
    return 1.0 - er
```

---

# 五十二、不要把阈值写死在 Feature 中

错误：

```python
if di > 0.7:
    return 1
```

正确：

```python
feature = directional_inefficiency(close, 16)
```

然后：

```python
score = score_range_transition(feature, config)
```

这样未来才能做：

```text
parameter sweep
walk-forward
cross-market calibration
```

---

# 五十三、建议所有 Feature 做 Cross-sectional / Time-series Normalization

例如：

```text
DI
```

本身跨股票差异不一定很大。

但：

```text
ATR
Volume
RangeWidth
```

差异非常大。

建议使用：

### Time-series percentile

```text
ATR percentile
Volume percentile
RangeWidth percentile
```

以及：

### Cross-sectional rank

```text
volume_rank
volatility_rank
```

这样 Scanner 才能跨标的比较。

---

# 五十四、第一阶段不要上复杂 ML

V1：

```text
Rule-based
```

V2：

```text
Logistic Regression
```

V3：

```text
LightGBM / CatBoost
```

V4：

```text
Sequence Model / HMM / State Space
```

推荐路线：

```text
Rule
 ↓
证明 alpha 存在
 ↓
统计模型
 ↓
ML
```

而不是：

```text
100个feature
 ↓
LightGBM
 ↓
发现一个漂亮回测
 ↓
实盘爆炸
```

---

# 五十五、如果使用 ML，模型目标应该分层

### Model 1

\[
P(RangeFormation)
\]

### Model 2

\[
P(BoundaryReversion)
\]

### Model 3

\[
E[MFE]
\]

### Model 4

\[
E[MAE]
\]

最终：

\[
ExpectedValue
=
P(Win)\cdot E[MFE]
-
P(Loss)\cdot E[MAE]
\]

这比直接训练：

```text
future_return
```

更贴合你的策略结构。

---

# 五十六、Daily Scanner 最终报告

每天建议生成：

```text
================================================
        TRADABLE RANGE DAILY SCANNER
================================================

#1  XXX
Daily Regime: EARLY_RANGE
60F ERS:      93
Range:        1180 - 1320
Range Age:    4
Support:      1200
Resistance:   1305

30F:
RangePos:     0.11
Event:        FALSE_BREAKDOWN_RECLAIM
EventScore:   91

Expected:
MFE:          1.7 ATR
MAE:          0.6 ATR

------------------------------------------------

#2  YYY
...
```

这样你每天打开系统，不是看到：

> “RSI 29，买入。”

而是看到：

> **“XXX 正在形成一个高质量震荡区间，目前价格位于下沿 11%，30F 刚刚出现假跌破回收事件。”**

这才是这个系统真正应该提供的决策信息。

---

# 五十七、最终的核心数据结构

建议每个标的每天维护一个 Range Object：

```python
RangeState(
    symbol,
    timeframe,

    state,
    start_time,
    age,

    upper,
    lower,
    mid,
    width,

    directional_inefficiency,
    di_slope,
    flip_rate,
    autocorr,

    volatility,
    volatility_percentile,

    upper_stability,
    lower_stability,

    support_response,
    resistance_response,

    breakout_failure_score,

    range_score,
    confidence,
)
```

30F 再产生：

```python
TradingEvent(
    symbol,
    timestamp,

    event_type,
    range_position,

    candle_strength,
    expansion_ratio,
    volume_ratio,

    entry_score,

    expected_mfe,
    expected_mae,
)
```

---

# 五十八、整个系统的核心逻辑可以压缩成一句话

```text
日K：
“这个市场环境允许我寻找震荡。”

        ↓

60F：
“这个标的原来的趋势正在失去方向效率，
而且价格开始形成稳定的上下边界。”

        ↓

30F：
“价格现在已经到了边界，
并出现了一个有统计优势的反转/红K事件。”

        ↓

Trade：
“以 Range Invalidity 为止损，
以 Range Mid / Opposite Boundary 为目标。”

        ↓

Exit：
“震荡失效或波动收益已经兑现。”
```

---

# 五十九、最重要的研究优先级

不要同时开发所有东西。

按以下顺序：

### Phase 1：证明 Directional Inefficiency 有效

研究：

```text
DI
DI slope
DI acceleration
FlipRate
AC1
```

能否提前识别 Range。

---

### Phase 2：证明 Boundary 有效

研究：

```text
Quantile Range
Price Density
Touch Count
Rejection
```

是否能识别“可交易”而非普通横盘。

---

### Phase 3：定义 Range Start

建立：

```text
RangeStart Label
```

这是整个项目最重要的数据资产。

---

### Phase 4：60F Early Range Detector

实现：

```text
Trend
→ Trend Decay
→ Range Formation
→ Early Range
```

---

### Phase 5：30F Event Study

研究：

```text
Lower Boundary
+
Bullish Event
```

以及：

```text
False Breakdown
+
Reclaim
```

---

### Phase 6：日K Context

把日K作为：

```text
Regime Filter
```

加入系统。

---

### Phase 7：完整回测

加入：

```text
Entry
Exit
Stop
Position Size
Fees
Slippage
Liquidity
```

---

### Phase 8：ML

最后才：

```text
P(Range)
P(Reversion)
Expected MFE
Expected MAE
```

---

# 六十、最终系统的 Alpha 核心

如果把整个工程压缩到最核心的几个变量，我认为 V1 应该围绕下面这个结构：

\[
\boxed{
TrendFailure
\rightarrow
DirectionalInefficiency
\rightarrow
BoundaryFormation
\rightarrow
BoundaryRejection
\rightarrow
30F\ Event
}
\]

其中：

\[
TrendFailure
=
DI↑ + FlipRate↑ + AC_1↓
\]

\[
RangeFormation
=
BoundaryStable + WidthStable + PriceDensity↑
\]

\[
TradableRange
=
RangeFormation + BoundaryResponse + SufficientVolatility
\]

最终交易：

\[
Entry
=
TradableRange
+
RangePosition
+
Bullish/BearishEvent
\]

而不是：

\[
Entry = RedCandle
\]

---

# 六十一、最终架构

```text
                         ┌──────────────┐
                         │   日K环境     │
                         └──────┬───────┘
                                │
                  Range Compatible / Trend Risk
                                │
                                ▼
                         ┌──────────────┐
                         │    60F       │
                         │              │
                         │ DI Transition│
                         │ FlipRate     │
                         │ AC1          │
                         │ Boundary     │
                         │ Density      │
                         │ Rejection    │
                         └──────┬───────┘
                                │
                        Early Range Score
                                │
                         Score > Threshold
                                │
                                ▼
                         ┌──────────────┐
                         │    30F       │
                         │              │
                         │ RangePos     │
                         │ BullishEvent │
                         │ Reclaim      │
                         │ FalseBreak   │
                         │ Expansion    │
                         └──────┬───────┘
                                │
                         Entry Score
                                │
                                ▼
                       ┌─────────────────┐
                       │ Trading Engine  │
                       │                 │
                       │ Entry           │
                       │ Stop            │
                       │ TP1 / TP2       │
                       │ Position Size   │
                       └─────────────────┘
```

## 最终设计原则

**日K负责“选环境”，60F负责“找刚开始形成的震荡”，30F负责“找边界上的波动事件”。**

而整个系统最核心的研究对象不是“红K”，甚至也不是“震荡”。

真正应该被量化、被预测的是：

\[
\boxed{
P(\text{未来几根/几十根K形成一个高质量、可交易的 Range})
}
\]

一旦这个 `Range Formation Probability` 能够被稳定地提前预测，你的每日 Scanner 就有了核心能力：

> **在全市场中，不是寻找已经震荡的股票，而是每天寻找“即将进入可交易震荡状态”的股票。**

随后，再让 30F 去等待真正的边界事件。

这会让整个系统从一个“技术指标选股器”，变成一个**多周期市场状态检测 + 事件驱动交易系统**。