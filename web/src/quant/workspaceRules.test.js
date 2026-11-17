import assert from 'node:assert/strict'
import test from 'node:test'
import { summarizeDraft, isStaged } from './workspaceRules.js'
import { makeIndicator, makeCondition, describeRule } from './ruleVocabulary.js'

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
test('custom rules show logical groups and fixed fill-price exits without hidden template logic', () => {
  const rules = { entry: { type: 'group', logic: 'all', children: [makeCondition('cross_up'), { type: 'group', logic: 'any', children: [{ type: 'condition', left: makeIndicator('rsi'), operator: 'gt', right: { type: 'constant', value: 40 } }, makeCondition()] }] }, exit: null, sampling: 'at_close', trigger: 'edge', stop_loss_pct: 3, take_profit_pct: null, cooldown_bars: 12, min_daily_amount: 0 }
  const text = summarizeDraft({ ...draft, mode: 'rules', rules }).join('\n')
  assert.match(describeRule(rules.entry), /且.*或/)
  assert.match(text, /RSI\(14\) 大于 40/)
  assert.match(text, /实际买入价设置3%计划止损/)
  assert.match(text, /不设固定止盈/)
  assert.doesNotMatch(text, /MACD底背离|目标3R|阳线实体/)
})
