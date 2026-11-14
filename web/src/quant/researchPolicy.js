// Rejected experiments remain manually reviewable, but cannot become defaults.
export function preferredRun(items) {
  const allowed = items.filter((r) => !r.diagnostics?.superseded && r.diagnostics?.eligible_for_default !== false && r.diagnostics?.research_status !== 'rejected')
  return allowed.find((r) => r.diagnostics?.research_selected && r.strategy_id?.startsWith('swing_'))
    || allowed.find((r) => r.strategy_id === 'mtf_reversal_v6' && r.diagnostics?.exit_profile === 'structure_2r' && r.universe_mode === 'fixed_snapshot')
    || allowed.find((r) => r.strategy_id === 'mtf_dual_v5' && r.universe_mode === 'fixed_snapshot')
    || allowed[0]
}
