import { opportunityFor } from './opportunities'

export default function OpportunityBadge({ value }) {
  const kind = opportunityFor(value)
  const details = [value?.parent_trend_label, value?.local_trigger_label, value?.pattern_label && `原规则：${value.pattern_label}`].filter(Boolean)
  return <span className="opportunity-badge" style={{ '--opportunity-color': kind.color }} title={[kind.description, ...details].join(' · ')}>
    <span aria-hidden="true">{kind.symbol}</span>{kind.label}
  </span>
}
