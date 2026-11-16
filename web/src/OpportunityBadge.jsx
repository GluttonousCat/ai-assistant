import { opportunityFor } from './opportunities'

export default function OpportunityBadge({ value }) {
  const kind = opportunityFor(value)
  const detail = typeof value === 'object' && value?.pattern_label
  return <span className="opportunity-badge" style={{ '--opportunity-color': kind.color }} title={`${kind.description}${detail ? ` · ${detail}` : ''}`}>
    <span aria-hidden="true">{kind.symbol}</span>{kind.label}
  </span>
}
