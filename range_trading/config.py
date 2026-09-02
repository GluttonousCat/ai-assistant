"""
日K层参数配置

【设计原则】(指南第五十二章)
- Feature 只输出原始值, 不写死任何阈值;
- 所有窗口 / 满分参考值 / 状态阈值 / 打分权重全部集中在本配置;
- 未来做 parameter sweep / walk-forward 校准时, 只需替换本配置对象。

窗口取值依据指南 15.1 / 15.2 节 (日K观察窗口: 10D / 20D / 30D / 60D / 120D)。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict


@dataclass
class DailyConfig:
    """日K Early Range Detection 全量参数 (V1 工程起点, 非最终 alpha)"""

    # ---------- 方向效率 (指南第三~五节) ----------
    di_windows: tuple = (10, 20, 40, 60)     # 多尺度 DI 窗口
    di_slope_k: int = 5                       # DITrend = DI_t - DI_{t-k}
    di_slope_full: float = 0.10               # di_slope 达到该值 -> TrendFailure 满分
    di_accel_full: float = 0.02               # di_accel 达到该值 -> 满分

    # ---------- 反向运动频率 / 自相关 (指南第六、七节) ----------
    flip_windows: tuple = (10, 20)
    flip_full: float = 0.45                   # FlipRate 达到该值 -> 满分 (A股日K经验值)
    ac1_window: int = 20
    ac1_full: float = 0.30                    # AC1 <= -0.30 -> 满分

    # ---------- 波动 (指南第八节) ----------
    atr_window: int = 14
    natr_pct_window: int = 250                # NATR 时序分位数回看窗口 (~一年)
    natr_floor: float = 0.20                  # 分位 < 0.2 -> 波动不足, VolatilityQuality 为 0
    natr_ceil: float = 0.70                   # 分位 >= 0.7 -> 满分
    atr_stab_window: int = 20

    # ---------- 价格边界 (指南第九~十二节) ----------
    boundary_windows: tuple = (20, 30, 60)    # 多尺度边界; 主判定窗口取 boundary_primary
    boundary_primary: int = 30                # 中期 Price Acceptance (20D/30D)
    upper_quantile: float = 0.90
    lower_quantile: float = 0.10
    boundary_slope_k: int = 10                # 边界斜率差分步长
    boundary_slope_ref: float = 0.002         # 边界日均移动占 mid 0.2% -> 完全不稳定
    width_slope_ref: float = 0.002            # 区间宽度日均变化 0.2% -> 完全不稳定
    density_window: int = 30
    touch_band: float = 0.10                  # 触碰带: 区间宽度的 10%
    touch_window: int = 30
    touch_full: int = 4                       # 窗口内触碰 >= 4 次 -> 满分
    reaction_horizon: int = 5                 # 触碰后反应的观察天数 (MFE 窗口)
    reaction_window: int = 60                 # 已成熟反应的滚动平均窗口
    reaction_full_atr: float = 2.0            # 平均反应 >= 2 ATR -> 满分

    # ---------- 趋势结构 / 假阳性过滤 (指南第三十七~三十九节) ----------
    ema_windows: tuple = (20, 60)
    ema_slope_k: int = 10
    ema_slope_decay_ref: float = 0.001        # |EMA斜率| 日均降幅 0.1% -> 衰减满分
    bias_ref: float = 0.001                   # EMA60 斜率日均 0.1% -> 偏置满格
    hh_hl_window: int = 20
    hh_hl_floor: float = 0.50                 # (HH率+HL率)/2 超过该值开始惩罚
    hh_hl_full: float = 0.80
    net_slope_window: int = 60
    net_slope_ref: float = 0.0015             # 净位移日均 0.15% -> 趋势延续满分
    di_long_floor: float = 0.40               # DI_60 低于该值 -> 长周期趋势未失效, 开始惩罚

    # ---------- K线事件 (指南第二十四、四十节; 日K层仅轻量使用) ----------
    body_min: float = 0.50                    # Bullish/Bearish Event 实体占比阈值
    expansion_min: float = 1.20               # TR/ATR 阈值

    # ---------- 量能 (指南第二十九、五十三节; 日K层为辅助证据, 不进总分) ----------
    volume_ma_window: int = 20                # 量比基准窗口
    volume_pct_window: int = 250              # 量能时序分位回看窗口
    obv_slope_k: int = 20                     # OBV 斜率步长
    vwap_window: int = 20                     # 量能重心 (VWAP) 窗口
    breakout_volume_min: float = 1.20         # 突破确认的量比下限 (<=0 关闭量能确认)

    # ---------- 打分权重 (指南第二十章, 适配日K) ----------
    score_weights: Dict[str, float] = field(default_factory=lambda: {
        "trend_failure": 0.30,
        "inefficiency": 0.25,
        "range_formation": 0.20,
        "boundary_evidence": 0.15,
        "volatility_quality": 0.10,
    })
    # 分项内部权重
    sub_weights: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "trend_failure": {"di_slope": 0.50, "di_accel": 0.30, "slope_decay": 0.20},
        "inefficiency": {"di_level": 0.50, "flip": 0.30, "ac1": 0.20},
        "range_formation": {"upper_stab": 0.30, "lower_stab": 0.30, "width_stab": 0.20, "density": 0.20},
        "boundary_evidence": {"touch": 0.50, "rejection": 0.50},
        "volatility_quality": {"natr_pct": 0.70, "atr_stab": 0.30},
        # TrendContinuation 惩罚项 (指南第三十八节: RangeProb = TransitionEvidence - TrendContinuationEvidence)
        "trend_continuation": {"hh_hl": 0.45, "net_slope": 0.35, "di_long": 0.20},
    })
    trend_penalty_weight: float = 0.35        # 趋势延续证据最多扣减的分值比例 (0.35 * 100)

    # ---------- 区间宽度约束 (指南第三十四节: Width_min < RangeWidth < Width_max) ----------
    width_ideal_max: float = 0.35              # 相对宽度 <= 35% 不惩罚
    width_hard_max: float = 0.60               # >= 60% 全额惩罚 (过宽区间无边界交易价值)
    width_penalty_weight: float = 0.25         # 宽度惩罚最大扣减 (0.25 * 100)

    # ---------- 状态机阈值 (指南第十四、十六、二十一章; 初始工程参数) ----------
    score_trend_decay: float = 40.0           # >= -> TREND_DECAY
    score_range_formation: float = 55.0       # >= -> RANGE_FORMATION
    score_early_range: float = 65.0           # >= -> EARLY_TRADABLE_RANGE
    score_high_quality: float = 80.0          # >= -> 高质量 (报告标注)
    confirm_days: int = 3                     # 状态升级需连续满足天数 (日K转换更慢)
    decay_confirm_days: int = 2               # TREND -> TREND_DECAY 确认天数
    fallback_days: int = 5                    # 状态降级需连续不满足天数
    min_formation_stability: float = 0.50     # 升入 RANGE_FORMATION 需 width_stab 下限
    min_formation_touch: int = 2              # 升入 EARLY 需窗口内最少触碰次数
    min_mature_age: int = 20                  # EARLY -> MATURE 的区间年龄 (交易日)
    breakout_confirm_days: int = 2            # 收盘越界连续天数 -> BREAKOUT
    breakout_exit_days: int = 2               # BREAKOUT 后收回区间天数 -> RANGE_RENEWAL
    renewal_hold_days: int = 5                # RANGE_RENEWAL 保持天数后按分数重新归类

    # ---------- 数据 / Universe ----------
    min_history: int = 200                    # 最少日K根数 (低于此不参与扫描)
    universe_amount_floor: float = 10000.0    # 近 60 日日均成交额下限 (千元, 默认 1000 万元)
    universe_lookback: int = 60               # universe 流动性统计窗口 (交易日)
    scan_batch_size: int = 300                # 全市场扫描分批拉取的每批股票数

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEFAULT_DAILY_CONFIG = DailyConfig()


@dataclass
class TrendConfig:
    """
    趋势系统参数 (《趋势行情捕捉与动量收益系统》工程指南)

    【架构原则】与震荡系统 (DailyConfig) 共享特征层与状态机, 趋势阈值集中于此。
    量价是趋势系统的硬门槛 (VPA), 不是加分项。
    """

    # ---------- 方向效率回归 (指南第三章) ----------
    er_windows: tuple = (10, 20, 40, 60)     # 多尺度 ER (与 DI 同源)
    er_confirm: float = 0.55                  # ER_20 趋势门槛
    er_slope_k: int = 5
    er_slope_full: float = 0.10               # ER 斜率满分参考
    net_slope_full: float = 0.003             # 净位移日均 0.3% -> 满分

    # ---------- 量价体系 (指南第五章) ----------
    breakout_vol_ratio: float = 2.0           # 突破日量比门槛 (硬条件)
    breakout_vol_min: float = 1.5             # 低于此判定缩量突破 (降权)
    udvol_window: int = 20
    udvol_full: float = 1.5                   # UDVOL >= 1.5 满分
    vol_persist_fast: int = 5
    vol_persist_slow: int = 20
    vol_persist_full: float = 1.3             # VolMA5/VolMA20 >= 1.3 满分
    pv_corr_window: int = 20
    pv_corr_low: float = 0.30                 # 量价脱钩警戒 (背离证据)
    obv_confirm_window: int = 20
    cmf_window: int = 20
    cmf_full: float = 0.10                    # CMF >= 0.10 满分
    vwap_window: int = 30
    vwap_slope_k: int = 10
    vwap_slope_full: float = 0.002            # 重心日均移动 0.2% 满分

    # VPA 共振矩阵权重 (指南 5.6)
    vpa_weights: Dict[str, float] = field(default_factory=lambda: {
        "breakout_volume": 0.25,
        "obv_sync": 0.25,
        "udvol": 0.20,
        "cmf": 0.15,
        "vwap_support": 0.15,
    })
    vpa_min_entry: float = 0.60               # 入场 VPA 硬门槛

    # ---------- 趋势结构 (指南第四章) ----------
    swing_window: int = 5                     # 摆动点分形窗口 (右侧确认, 无前视)
    swing_confirm_lag: int = 5                # swing 确认滞后 (右侧窗口)
    pullback_max: float = 0.40                # 健康回调上限 (前波段 40%)
    pullback_soft: float = 0.60               # 结构风险线
    pullback_entry_zone: tuple = (0.15, 0.45) # 回调入场深度区间
    pullback_vol_max: float = 0.70            # 回调量能比上限 (缩量回调)
    integrity_window: int = 60                # 结构完整性统计窗口

    # ---------- 阶段检测 (指南第七章) ----------
    stage_ma_long: int = 120
    stage_er_window: int = 40
    # 阈值经真实强势股分布校准 (近60日涨>50%样本, n=40):
    #   er_40 中位 0.12 / p75 0.22 / max 0.45 —— A股强势股走"进二退一"阶梯,
    #   40日净位移/路径比天然被回调拉长, 故取 p75 作为 Stage 2 硬门槛。
    stage_er_threshold: float = 0.22          # Stage 2/4 效率门槛 (原 0.45 过严)
    stage_er_flat: float = 0.12               # Stage 3 派发: 效率回落至中位数以下
    trend_late_age: int = 60                  # 趋势年龄超过此 -> 晚期

    # ---------- TQS 打分权重 (指南第八章) ----------
    tqs_weights: Dict[str, float] = field(default_factory=lambda: {
        "efficiency": 0.30,
        "volume_price": 0.25,
        "structure": 0.20,
        "sustainability": 0.15,
        "stage": 0.10,
    })

    # ---------- 状态机阈值 (指南第九章) ----------
    tqs_forming: float = 40.0                 # >= -> TREND_FORMING
    tqs_established: float = 60.0             # >= -> TREND_ESTABLISHED
    trend_confirm_days: int = 3
    trend_fallback_days: int = 5
    breakout_hold_days: int = 3               # 突破站稳天数 (BREAKOUT_CONFIRMED)
    exhaustion_hold_days: int = 3             # EXHAUSTION 保持天数后 -> TREND_DECAY

    # ---------- 衰竭证据 (指南第十一章) ----------
    divergence_lookback: int = 20             # 价格新高/OBV 新高同步窗口
    er_fall_amount: float = 0.15              # ER_20 从高点回落幅度

    # ---------- 游资脉冲过滤 (指南第十八章陷阱3: One-day Wonder) ----------
    # pulse_share = 近5日最大单日量 / 5日量和。> pulse_hard 视为游资对倒脉冲:
    # 单日贡献过半量能, 后续必然枯竭, 无法支撑趋势延续 -> 硬扣分。
    # 经真实强势股校准: 持续放量票 pulse_share 约 0.2~0.35, 游资脉冲票 > 0.5。
    pulse_hard: float = 0.50                  # 脉冲硬阈值
    pulse_penalty_full: float = 0.30          # 脉冲最严重时扣减 TQS 的比例

    # ---------- 退出 (指南 11.2 Chandelier) ----------
    chandelier_window: int = 22
    chandelier_atr: float = 3.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEFAULT_TREND_CONFIG = TrendConfig()
