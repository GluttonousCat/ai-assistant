import { summarizeRules } from './ruleVocabulary.js'

export const opportunityNames = { both: '底部反转 + 上涨回调', base_rebound: '底部反转', trend_pullback: '上涨回调' }
export const poolNames = { mainboard: '五指数成分 · 主板', five_indices: '五指数成分 · 全板块', '000300.SH': '沪深300', '000016.SH': '上证50', '000905.SH': '中证500', '000852.SH': '中证1000', '932000.CSI': '中证2000' }
export const statusNames = { queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', interrupted: '已中断' }
export const isStaged = (draft) => draft.execution.entry_plan !== 'single' || draft.execution.exit_plan !== 'single'
export function summarizeDraft(draft) {
  if (draft.mode === 'rules') return [`${poolNames[draft.universe]}，排除当前ST及退市简称。`, ...summarizeRules(draft)]
  const e = draft.entry, x = draft.execution
  const exits = isStaged(draft)
    ? `${x.exit_plan === 'single' ? '不分批止盈' : `盈利${x.profit_first_pct}% / ${x.profit_second_pct}%后按${x.exit_plan}分批卖出，保留尾仓`}；${x.profit_first_pct}%后启动成本保护及M21跟踪`
    : `${({ legacy: '结构止损 + R目标', protected: '结构止损 + 盈利保护 + R目标', reversal: '反弹失败与ATR跟踪，不固定R止盈' })[x.exit_policy]}${x.exit_policy !== 'reversal' ? `，目标${x.take_profit_r}R` : ''}`
  return [
    `${poolNames[draft.universe]}，排除当前ST及退市简称。`,
    `60F ${opportunityNames[e.family]}；固定M8 / M13 / M21，MACD底背离或受控回调转强。`,
    `阳线实体至少${e.min_body_atr} ATR，量比至少${e.min_volume_ratio}，信号间隔${e.cooldown_bars}根60F。`,
    `结构止损距离${e.min_risk_pct}%—${e.max_risk_pct}%；日均成交额门槛${e.min_daily_amount / 10000}万元。`,
    `当日四根完整60F，信号收盘未失效，下一交易日开盘买入；日K不另加形态门槛。`,
    `最多${x.max_positions}只，单股计划${+(x.position_weight * 100).toFixed(2)}%，账户初始风险预算${x.risk_budget_pct}%；${x.entry_plan === 'single' ? '单次买入' : `${x.entry_plan}分批买入，盈利0.5R / 1R且60F转强才加仓`}。`,
    `${exits}；最长${x.max_holding_bars}个交易日。盘后检查、次日执行，计划风险不等于实际亏损上限。`,
  ]
}

export function newRequestId() {
  return crypto.randomUUID()
}
