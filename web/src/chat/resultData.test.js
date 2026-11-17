import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildChartSpec, fmtDateCol, parseNum } from './resultData.js'

test('cumulative quarterly statements use independent bars; daily quotes use lines', () => {
  const rows = [{ end_date: '2026-03-31', revenue: 1 }, { end_date: '2026-06-30', revenue: 2 }]
  assert.equal(buildChartSpec(['end_date', 'revenue'], rows).kind, 'period')
  const daily = rows.map((r) => ({ trade_date: r.end_date, close: r.revenue }))
  assert.equal(buildChartSpec(['trade_date', 'close'], daily).kind, 'line')
})

test('comparison bars remain sorted and bounded without mutating source rows', () => {
  const rows = Array.from({ length: 16 }, (_, i) => ({ name: `股票${i}`, value: i }))
  const copy = structuredClone(rows)
  const result = buildChartSpec(['name', 'value'], rows)
  assert.equal(result.kind, 'bar')
  assert.equal(result.rows.length, 12)
  assert.equal(result.omitted, 4)
  assert.equal(result.rows[0].value, 15)
  assert.deepEqual(rows, copy)
})

test('date deduplication preserves the last row and non-chartable data falls back', () => {
  const rows = [{ trade_date: '2026-11-16', close: 1 }, { trade_date: '2026-11-17', close: 2 }, { trade_date: '2026-11-16', close: 3 }]
  const spec = buildChartSpec(['trade_date', 'close'], rows)
  assert.deepEqual(spec.times, ['2026-11-16', '2026-11-17'])
  assert.equal(spec.rowAt('2026-11-16').close, 3)
  assert.equal(buildChartSpec(['text'], [{ text: '描述' }]), null)
  assert.equal(parseNum('invalid'), null)
  assert.equal(fmtDateCol('end_date', '2026-06-30'), '2026 中报')
})
