import assert from 'node:assert/strict'
import test from 'node:test'
import { claimSections } from './claimSections.js'

const core = {
  statement: '若需求回升，利润率可能改善。', attribution: 'broker', stance: 'conditional',
  source: { segment: 2, page: 1, quote: 'Margins could improve if demand recovers.' },
}

test('a featured claim keeps its original, attribution and research fields exactly once', () => {
  const survey = { ...core, attribution: 'survey' }
  const sections = claimSections({
    summary_evidence: { core_view: core, key_points: [core, survey] },
    evidence: [core, survey],
    research_insights: { assumptions: [{ ...core, condition: '需求回升' }] },
  })
  assert.deepEqual(sections.map(section => [section.title, section.featured]), [
    ['核心观点', true], ['材料要点', true],
  ])
  assert.deepEqual(sections[0].entries, [{ ...core, condition: '需求回升' }])
  assert.deepEqual(sections[1].entries, [survey])
})

test('an unsourced summary cannot replace a missing quote', () => {
  const sections = claimSections({
    core_view: '利润率将提高。',
    summary_evidence: { core_view: { ...core, source: { quote: '' } } },
    research_insights: { theses: [null] }, evidence: [core],
  })
  assert.deepEqual(sections, [{ title: '其他研究依据', entries: [core] }])
})
