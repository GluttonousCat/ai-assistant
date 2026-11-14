import assert from 'node:assert/strict'
import test from 'node:test'
import { preferredRun } from './researchPolicy.js'

test('an explicitly rejected experiment cannot displace the existing default', () => {
  const rejected = { run_id: 'bad', strategy_id: 'swing_structure_v9', diagnostics: { research_selected: true, eligible_for_default: false, research_status: 'rejected' } }
  const existing = { run_id: 'old', strategy_id: 'mtf_reversal_v6', universe_mode: 'fixed_snapshot', diagnostics: { exit_profile: 'structure_2r' } }
  assert.equal(preferredRun([rejected, existing]), existing)
  assert.equal(preferredRun([rejected]), undefined)
})

test('superseded results are never selected even when marked primary', () => {
  const old = { strategy_id: 'swing_structure_v9', diagnostics: { research_selected: true, superseded: true } }
  const valid = { strategy_id: 'swing_structure_v9', diagnostics: { research_selected: true, eligible_for_default: true } }
  assert.equal(preferredRun([old, valid]), valid)
})
