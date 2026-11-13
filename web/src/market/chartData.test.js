import test from 'node:test'
import assert from 'node:assert/strict'
import { boundedCache, captureView, chartRows, restoreView, seriesUpdate } from './chartData.js'

const bar = (time, close = 10) => ({ time, open: 10, high: 12, low: 9, close, volume: 100, ema_fast: 10, ema_slow: null })

test('a historical correction must replace history instead of only updating the tail', () => {
  const before = [bar(1), bar(2), bar(3)]
  const corrected = [bar(1, 11), bar(2), bar(3), bar(4)]
  assert.equal(seriesUpdate(before, corrected).type, 'replace')
  assert.equal(seriesUpdate(before, before.slice(1)).type, 'replace')
})

test('tail changes and new bars are incremental; identical prices require no series write', () => {
  const before = [bar(1), bar(2)]
  assert.deepEqual(seriesUpdate(before, [bar(1), bar(2, 11), bar(3)]), { type: 'update', from: 1 })
  assert.deepEqual(seriesUpdate(before, [...before, bar(3)]), { type: 'update', from: 2 })
  assert.equal(seriesUpdate(before, before.map((r) => ({ ...r }))).type, 'none')
  assert.equal(seriesUpdate(before, [bar(1), { ...bar(2), ema_fast: null }]).type, 'update')
})

test('timezone conversion preserves Shanghai dates and explicit UTC offsets', () => {
  const source = { open: 10, high: 11, low: 9, close: 10 }
  const rows = chartRows([
    { ...source, bar_end: '2026-11-12T15:00:00' },
    { ...source, bar_end: '2026-11-12T07:00:00Z', close: 10.5 },
    { ...source, bar_end: 'invalid' },
    { ...source, bar_end: '2026-11-11T15:00:00+08:00' },
    { ...source, bar_end: '2026-11-10T15:00:00', close: null },
  ])
  assert.equal(rows.length, 2)
  assert.equal(rows[1].time, Date.parse('2026-11-12T07:00:00Z') / 1000)
  assert.equal(rows[1].close, 10.5)
  assert.ok(rows[0].time < rows[1].time)
})

test('viewport remains anchored to the same candle when old bars roll out', () => {
  const before = [bar(1), bar(2), bar(3), bar(4)]
  const view = captureView(before, { from: 2, to: 4 })
  assert.deepEqual(restoreView([bar(2), bar(3), bar(4), bar(5)], view), { from: 1, to: 3 })
  assert.equal(restoreView([bar(8), bar(9)], view), null)
})

test('bounded caches isolate snapshot/stock/period keys and evict the least recently used', () => {
  const cache = boundedCache(2)
  const a = 'run-a:600000.SH:1D:0'
  const b = 'run-a:600000.SH:60F:0'
  cache.set(a, [bar(1)]); cache.set(b, [bar(2)])
  assert.equal(cache.get(a)[0].time, 1)
  cache.set('run-b:600000.SH:1D:0', [bar(3)])
  assert.equal(cache.get(b), undefined)
  assert.equal(cache.get('run-a:600000.SH:1D:1'), undefined)
  assert.equal(cache.get(a)[0].time, 1)
})
