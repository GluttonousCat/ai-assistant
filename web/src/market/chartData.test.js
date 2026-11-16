import test from 'node:test'
import assert from 'node:assert/strict'
import { boundedCache, captureView, chartRows, restoreView, seriesUpdate, signalMarkers } from './chartData.js'
import { opportunityFor } from '../opportunities.js'

const bar = (time, close = 10) => ({ time, open: 10, high: 12, low: 9, close, volume: 100, ma13: 10, ma21: null })

test('multi-timeframe entries are labeled as daily entries rather than a candle count', () => {
  assert.equal(signalMarkers([{time:1,eligible:true}], {dailyEntry:true})[0].text,'日K入场')
  const before=[bar(1),bar(2)]
  assert.equal(seriesUpdate(before,[bar(1),{...bar(2),ma8:10.5}]).type,'update')
})

test('chart distinguishes non-tradable background, original first and pullback resumption', () => {
  const markers = signalMarkers([
    { time: 1, swing_ignition: true },
    { time: 2, first_watch: true, startup_count: 1 },
    { time: 3, resume_watch: true, setup_phase: 'pullback_resume', startup_count: 1 },
    { time: 4, eligible: true, setup_phase: 'pullback_resume', startup_count: 2 },
    { time: 5 },
  ])
  assert.deepEqual(markers.map((m) => m.text), ['起势背景', '首阳观察', '趋势回调 · 再起观察', '趋势回调 · 再起第2阳确认'])
  assert.equal(markers[0].color, '#97948c')
  assert.equal(markers[2].shape, 'circle')
  assert.equal(markers[3].shape, 'arrowUp')
})

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
  assert.equal(seriesUpdate(before, [bar(1), { ...bar(2), ma13: null }]).type, 'update')
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

test('60F reversal observation is a non-executable circle distinct from daily entry', () => {
  const markers = signalMarkers([{time: 1, reversal_ignition: true, eligible: false}]);
  assert.equal(markers[0].shape, 'circle');
  assert.equal(markers[0].text, '底部反转 · 观察');
  assert.equal(markers[0].position, 'aboveBar');
});

test('opportunity family and confirmation stage remain distinct across periods', () => {
  const rows = [
    { time: 1, pattern: 'trend_pullback', eligible: true, startup_count: 2 },
    { time: 2, pattern: 'base_rebound', eligible: true, startup_count: 2 },
    { time: 3, pattern: 'base_rebound', first_watch: true },
    { time: 4, pattern: 'trend_pullback', swing_ignition: true },
  ]
  const markers = signalMarkers(rows)
  assert.equal(markers[0].shape, 'arrowUp')
  assert.equal(markers[1].shape, 'square')
  assert.notEqual(markers[0].color, markers[1].color)
  assert.equal(markers[2].shape, 'circle')
  assert.equal(markers[2].color, markers[1].color)
  assert.equal(markers[3].color, '#97948c')
  assert.equal(markers[3].text, '起势背景')
  assert.deepEqual(signalMarkers(rows.slice(0, 2), { dailyEntry: true }).map(m => m.text), ['趋势回调 · 日K入场', '底部反转 · 日K入场'])
})

test('backend family wins over old phase hints and an unclassified bullish bar stays unknown', () => {
  assert.equal(opportunityFor({ pattern: 'base_rebound', setup_phase: 'pullback_resume' }).key, 'base_rebound')
  assert.equal(opportunityFor({ family: 'trend_pullback' }).key, 'trend_pullback')
  assert.equal(opportunityFor({ first_watch: true }).key, 'unknown')
  assert.equal(opportunityFor({ pattern: 'future_setup', reversal_ignition: true }).key, 'unknown')
  assert.equal(opportunityFor({ pattern: 'constructor' }).key, 'unknown')
})

test('a local reversal inside a daily advance is annotated as a pullback, preserving observation status', () => {
  const row = { time: 1, pattern: 'base_rebound', context_pattern: 'trend_pullback', parent_trend: 'pullback', reversal_ignition: true, eligible: false }
  assert.equal(opportunityFor(row).key, 'trend_pullback')
  const marker = signalMarkers([row])[0]
  assert.equal(marker.shape, 'circle')
  assert.equal(marker.text, '趋势回调 · 局部反转观察')
  assert.equal(row.pattern, 'base_rebound')
  assert.equal(opportunityFor({ ...row, context_pattern: '' }).key, 'base_rebound')
})
