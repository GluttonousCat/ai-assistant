export const metricNames = { close: '收盘价', raw_close: '不复权收盘价', open: '开盘价', high: '最高价', low: '最低价', ma: 'MA均线', ema: 'EMA均线', rsi: 'RSI', atr: 'ATR', macd_dif: 'MACD DIF', macd_dea: 'MACD DEA', macd_hist: 'MACD柱(DIF−DEA)', volume: '成交量', volume_ratio: '量比', prior_high: '前N根最高价', prior_low: '前N根最低价', return_pct: 'N根涨幅(%)' }
export const periodMetrics = new Set(['ma', 'ema', 'rsi', 'atr', 'volume_ratio', 'prior_high', 'prior_low', 'return_pct'])
export const operatorNames = { gt: '大于', ge: '大于等于', lt: '小于', le: '小于等于', cross_up: '上穿', cross_down: '下穿' }
export const makeIndicator = (metric = 'close') => ({ type: 'indicator', metric, timeframe: '60F', period: periodMetrics.has(metric) ? (['rsi', 'atr'].includes(metric) ? 14 : 13) : null, lag: 0, multiplier: 1 })
export const makeCondition = (operator = 'gt') => ({ type: 'condition', left: makeIndicator(), operator, right: makeIndicator('ma') })
export const makeGroup = () => ({ type: 'group', logic: 'all', children: [makeCondition()] })
export function describeOperand(ref) {
  if (ref.type === 'constant') return String(ref.value)
  return `${ref.timeframe} ${metricNames[ref.metric]}${ref.period != null ? `(${ref.period})` : ''}${ref.lag ? `·前${ref.lag}根` : ''}${ref.multiplier !== 1 ? `×${ref.multiplier}` : ''}`
}
export function describeRule(node) {
  if (!node) return '未设置指标离场'
  if (node.type === 'condition') return `${describeOperand(node.left)} ${operatorNames[node.operator]} ${describeOperand(node.right)}`
  return `(${node.children.map(describeRule).join(node.logic === 'all' ? ' 且 ' : ' 或 ')})`
}
export function summarizeRules(draft) {
  const rules = draft.rules, x = draft.execution
  return [
    `入场：${describeRule(rules.entry)}。`,
    `离场：${describeRule(rules.exit)}。`,
    `${rules.sampling === 'at_close' ? '仅在15:00评估' : '当天任一完成的60F满足即可形成盘后计划'}；${rules.trigger === 'edge' ? '只在入场条件从不满足转为满足时触发' : '入场条件持续满足时可再次触发'}；冷却${rules.cooldown_bars}根60F（纯日K规则按交易日），次日开盘执行。`,
    `按实际买入价设置${rules.stop_loss_pct}%计划止损${rules.take_profit_pct == null ? '，不设固定止盈' : `、${rules.take_profit_pct}%计划止盈`}；最长${x.max_holding_bars}个交易日。阈值在收盘检查，次日执行，跳空/跌停可能扩大损失。`,
    `最多${x.max_positions}只，单股计划${+(x.position_weight * 100).toFixed(2)}%，账户初始风险预算${x.risk_budget_pct}%；单次进出。${rules.min_daily_amount ? `20日日均成交额至少${rules.min_daily_amount / 10000}万元。` : '不设额外成交额门槛。'}`,
  ]
}
