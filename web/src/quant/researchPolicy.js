// Rejected experiments remain manually reviewable, but cannot become defaults.
export function preferredRun(items) {
  const allowed = items.filter((r) => !r.diagnostics?.superseded && r.diagnostics?.eligible_for_default !== false && r.diagnostics?.research_status !== 'rejected')
  return allowed.find((r) => r.diagnostics?.research_selected && r.strategy_id?.startsWith('swing_'))
    || allowed.find((r) => r.strategy_id === 'mtf_reversal_v6' && r.diagnostics?.exit_profile === 'structure_2r' && r.universe_mode === 'fixed_snapshot')
    || allowed.find((r) => r.strategy_id === 'mtf_dual_v5' && r.universe_mode === 'fixed_snapshot')
    || allowed[0]
}

const marketNames = {
  baseline: '442基准', fast: '指数短线修复', trend: '指数趋势修复',
  trend_entry_only: '指数仅过滤首仓', both: '两指数趋势共振',
  avoid_down: '两指数共弱暂停', daily: '指数日级环境',
  trend_relative: '指数趋势与相对强弱', daily_relative: '指数日级与相对强弱',
  soft_only: '市场共跌软离场观察', trend_soft: '趋势过滤与软离场观察',
  trend_relative_soft: '趋势与抗跌观察', daily_relative_soft: '日级与抗跌观察',
}

export function marketResearchLabel(run) {
  if (!run.diagnostics?.research_kind?.startsWith('market_')) return null
  const variant = run.diagnostics.research_variant || ''
  const name = variant.replace(/__cost_stress$/, '')
  const stress = variant.endsWith('__cost_stress') ? ' · 双倍滑点' : ''
  if (marketNames[name]) return marketNames[name] + stress
  const words = { any: '原始确认', hold: '收盘保持', plain: '原排序', relative: '相对强势',
    support: '结构距离排序', blended: '综合排序', quick: '3/5%止盈', protect: '5/8%止盈·3%保护',
    runner: '8/13%止盈', normal: '原加仓节奏', early: '提前加仓', ext1: '距M13≤1ATR', 'ext2.5': '距M13≤2.5ATR' }
  const label = (part) => {
    if (part === 'patience0') return '不启用M21软离场'
    if (/^patience\d+$/.test(part)) return `M21软离场最少持有${part.slice(8)}日`
    if (part === 'profit5') return '5/8%止盈'
    if (part === 'profit8') return '8/13%止盈'
    return words[part] || (/^\d+$/.test(part) ? `${part}日` : part)
  }
  return name.split('_').map(label).join(' · ') + stress
}

export function marketPolicyText(policy) {
  if (!policy) return ''
  const modes = { none: '保留60F个股机会', any_fast: '至少一指数短线修复', any_trend: '至少一指数趋势修复',
    both_trend: '两指数趋势修复', avoid_both_down: '两指数同时走弱则暂停', daily_trend: '至少一指数日级环境修复' }
  return `参考沪深300与中证1000：${modes[policy.entry_mode] || policy.entry_mode}；${policy.relative_filter ? '要求个股近期表现强于按历史波动估计的大盘影响；' : ''}${policy.guard_adds ? '市场共跌时暂停加仓；' : ''}${policy.soft_grace_days ? '市场共跌且个股相对抗跌时，软结构离场最多多观察一个交易日，每个持仓周期一次；' : ''}原价格止损、失败反弹和持仓超时照常处理，缺少指数数据不放宽离场。`
}
