export const OPPORTUNITIES = {
  trend_pullback: { key: 'trend_pullback', label: '趋势回调', symbol: '↑', color: '#80b8ff', shape: 'arrowUp', description: '原有升势中的回调与再次转强' },
  base_rebound: { key: 'base_rebound', label: '底部反转', symbol: '■', color: '#c6a0f6', shape: 'square', description: '阴跌、横盘或深坑后的反转 / 反弹机会，不代表已经确认长期底部' },
}
const unknown = { key: 'unknown', label: '待识别', symbol: '·', color: '#97948c', shape: 'arrowUp', description: '当前记录尚未给出机会类型' }

// Explicit backend classification wins over legacy phase hints. A first bullish
// candle alone is insufficient to infer a bottom reversal.
export function opportunityFor(source) {
  // Versioned parent-wave annotations explain the larger context without
  // overwriting the original local pattern/family stored in the snapshot.
  if (Object.hasOwn(OPPORTUNITIES, source?.context_pattern || '')) return OPPORTUNITIES[source.context_pattern]
  const key = typeof source === 'string' ? source : source?.pattern || source?.family
  if (key) return Object.hasOwn(OPPORTUNITIES, key) ? OPPORTUNITIES[key] : unknown
  if (source?.setup_phase === 'pullback_resume' || source?.startup?.phase === 'pullback_resume') return OPPORTUNITIES.trend_pullback
  if (source?.reversal_ignition) return OPPORTUNITIES.base_rebound
  return unknown
}
