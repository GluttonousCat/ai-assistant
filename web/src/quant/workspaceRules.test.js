import assert from 'node:assert/strict'
import test from 'node:test'
import { summarizeDraft, isStaged } from './workspaceRules.js'

const draft = { universe: 'mainboard', entry: { family: 'both', min_body_atr: .2, min_volume_ratio: .8, cooldown_bars: 12, min_risk_pct: .8, max_risk_pct: 4, min_daily_amount: 30000000 }, execution: { entry_plan: 'single', exit_plan: 'single', max_positions: 10, position_weight: .1, risk_budget_pct: .35, exit_policy: 'protected', take_profit_r: 3, max_holding_bars: 20, profit_first_pct: 5, profit_second_pct: 8 } }
test('rule preview distinguishes structural risk from a guaranteed stop and day shape from execution', () => {
  const lines = summarizeDraft(draft).join('\n')
  assert.match(lines, /结构止损距离0.8%—4%/)
  assert.match(lines, /日K不另加形态门槛/)
  assert.match(lines, /计划风险不等于实际亏损上限/)
  assert.match(lines, /目标3R/)
})
test('staged preview never claims inactive fixed R target is used', () => {
  const staged = { ...draft, execution: { ...draft.execution, entry_plan: '532', exit_plan: '442' } }
  assert.equal(isStaged(staged), true)
  const lines = summarizeDraft(staged).join('\n')
  assert.match(lines, /532分批买入/)
  assert.match(lines, /盈利5% \/ 8%后按442分批卖出/)
  assert.doesNotMatch(lines, /目标3R/)
})
