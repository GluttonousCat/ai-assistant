// Only choose incremental updates when historical prices are unchanged.
// A corrected/adjusted historical candle must replace the full series.
const priceFields = ['open', 'high', 'low', 'close', 'volume', 'ma13', 'ma21']
const sameBar = (a, b) => a.time === b.time && priceFields.every((field) => a[field] === b[field])

export function signalMarkers(rows) {
  return rows.filter((r) => r.eligible || r.first_watch || r.resume_watch || r.swing_ignition).map((r) => {
    const resumed = r.setup_phase === 'pullback_resume'
    const confirmed = r.eligible
    const watch = r.resume_watch || r.first_watch
    return {
      time: r.time, position: 'belowBar', shape: confirmed ? 'arrowUp' : 'circle',
      color: confirmed ? '#68bea2' : watch ? '#dfb671' : '#97948c',
      text: confirmed ? `${resumed ? '再起第' : ''}${r.startup_count}阳确认` : r.resume_watch ? '回调再起观察' : r.first_watch ? '首阳观察' : '起势背景',
    }
  })
}

export function chartRows(data) {
  const byTime = new Map()
  for (const item of data || []) {
    const source = String(item.bar_end || '').replace(' ', 'T')
    const time = Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/.test(source) ? source : `${source}+08:00`) / 1000
    if (!Number.isFinite(time) || !['open', 'high', 'low', 'close'].every((key) => Number.isFinite(item[key]) && item[key] > 0)) continue
    byTime.set(time, { ...item, time })
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

export function seriesUpdate(previous, next) {
  if (!previous.length || next.length < previous.length) return { type: 'replace', from: 0 }
  for (let i = 0; i < previous.length - 1; i++) {
    if (!sameBar(previous[i], next[i])) return { type: 'replace', from: 0 }
  }
  const last = previous.length - 1
  if (previous[last].time !== next[last].time) return { type: 'replace', from: 0 }
  const from = sameBar(previous[last], next[last]) ? previous.length : last
  return { type: from === next.length ? 'none' : 'update', from }
}

export function boundedCache(limit = 20) {
  const values = new Map()
  return {
    get(key) {
      if (!values.has(key)) return undefined
      const value = values.get(key)
      values.delete(key); values.set(key, value)
      return value
    },
    set(key, value) {
      values.delete(key); values.set(key, value)
      if (values.size > limit) values.delete(values.keys().next().value)
    },
  }
}

export function captureView(rows, range) {
  if (!rows.length || !range) return null
  const index = Math.max(0, Math.min(rows.length - 1, Math.floor(range.from)))
  return { range: { ...range }, anchorTime: rows[index].time, anchorIndex: index }
}

export function restoreView(rows, view) {
  if (!view) return null
  const index = rows.findIndex((row) => row.time === view.anchorTime)
  if (index < 0) return null
  const offset = index - view.anchorIndex
  return { from: view.range.from + offset, to: view.range.to + offset }
}
