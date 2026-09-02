"""
可交易震荡区间挖掘系统 (range_trading)

基于《可交易震荡区间挖掘与波动收益系统: 30F - 60F - 日K 工程指南》实现。

当前阶段只有日K数据 (stock.daily, 前复权), 因此 V1 只实现日K层:
- features/  : Directional Inefficiency / FlipRate / AC1 / NATR / Quantile 边界 / 边界反应 等核心特征
- scoring/   : DailyRangeScore 五分项打分 (TrendFailure / Inefficiency / RangeFormation / BoundaryEvidence / VolatilityQuality)
- regime/    : 日K状态机 (TREND -> TREND_DECAY -> RANGE_FORMATION -> EARLY_TRADABLE_RANGE -> MATURE_RANGE ...)
- data/      : 从 stock.daily 加载前复权日K
- scanner/   : 全市场日K慢扫描, 输出 Ranking Table

架构分层 (指南第十五章):
    日K 负责 "环境与中期结构" -- 回答 "这个标的是否处于适合震荡交易的中期环境?"
    未来引入 60F / 30F 数据后, 再扩展 Early Range Detection 与 Trading Event 层。

用法示例:
    python -m range_trading.scanner.daily_scan --top 20
    python -m range_trading.scanner.daily_scan --symbol 000001.SZ
"""
