import test from 'node:test'
import assert from 'node:assert/strict'
import { compareStockScores, stockSearchText } from './stockList.js'

test('intraday ranking follows score, regardless of new-origin status', () => {
  const rows = [
    { ts_code: '600002.SH', score: 65, new_today: true },
    { ts_code: '600003.SH', score: 78, new_today: false },
    { ts_code: '600001.SH', score: 78, new_today: true },
  ]
  assert.deepEqual(rows.filter(() => true).sort(compareStockScores).map((r) => r.ts_code), ['600001.SH', '600003.SH', '600002.SH'])
  assert.equal(rows[0].ts_code, '600002.SH')
})

test('missing or invalid scores stay last; zero is a valid score', () => {
  const rows = [{ ts_code: 'a', score: null }, { ts_code: 'b', score: 0 }, { ts_code: 'c', score: 'bad' }]
  assert.deepEqual(rows.sort(compareStockScores).map((r) => r.ts_code), ['b', 'a', 'c'])
})

test('stock search includes current level-2 industry without using legacy level-1 labels', () => {
  const stock = { ts_code: '000959.SZ', name: '首钢股份', industry: '旧一级行业' }
  const text = stockSearchText(stock, { '000959.SZ': '普钢' })
  assert.ok(text.includes('普钢') && text.includes('000959.SZ') && text.includes('首钢股份'))
  assert.ok(!text.includes('旧一级行业'))
  assert.equal(stockSearchText(stock, {}), '000959.SZ首钢股份')
})
