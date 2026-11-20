export const insightTitles = {
  theses: '研究判断',
  assumptions: '关键假设',
  changes: '预期变化',
  watchpoints: '跟踪与验证',
}

function claimKey(note) {
  if (typeof note?.statement !== 'string' || !note.statement.trim() ||
      typeof note.source?.quote !== 'string' || !note.source.quote.trim()) return null
  return JSON.stringify([note.statement, note.attribution, note.stance,
    note.source.segment, note.source.page, note.source.quote])
}

// The same sourced claim may appear in the overview, the research cards and
// the evidence list. Preserve its research fields whichever section shows it.
export function claimSections(analysis) {
  const groups = Object.entries(insightTitles).map(([kind, title]) => ({
    title,
    entries: Array.isArray(analysis.research_insights?.[kind]) ? analysis.research_insights[kind] : [],
  }))
  const byClaim = new Map()
  for (const { entries } of groups) {
    for (const entry of entries) {
      const key = claimKey(entry)
      if (key) byClaim.set(key, entry)
    }
  }
  const overview = analysis.summary_evidence
  const sections = [
    { title: '核心观点', entries: [overview?.core_view], featured: true },
    { title: '材料要点', entries: overview?.key_points, featured: true },
    { title: '风险提示', entries: overview?.risks, featured: true },
    ...groups,
    { title: '其他研究依据', entries: analysis.evidence },
  ]
  const seen = new Set()
  const result = []
  for (const section of sections) {
    const entries = []
    for (const note of Array.isArray(section.entries) ? section.entries : []) {
      const key = claimKey(note)
      if (!key || seen.has(key)) continue
      seen.add(key)
      entries.push({ ...byClaim.get(key), ...note })
    }
    if (entries.length) result.push({ ...section, entries })
  }
  return result
}
