import { lazy, Suspense } from 'react'
import { compactNum, fmtDateCol, parseNum, periodLabel } from './chat/resultData'

const LineChart = lazy(() => import('./chat/ResultLineChart'))

// ---------- Rendering ----------

export default function ResultChart({ spec }) {
  if (!spec) return null
  return (
    <div className="chat-chart">
      {spec.kind === 'line' ? <Suspense fallback={<div className="loading">正在加载走势图…</div>}><LineChart spec={spec} /></Suspense>
        : spec.kind === 'period' ? <PeriodChart spec={spec} />
        : <BarChart spec={spec} />}
    </div>
  )
}

// Tonghuaxun-style per-period bar chart: one bar per report period, no connecting line (Q1/H1/Q3/annual are cumulative and mutually incomparable)
function PeriodChart({ spec }) {
  const W = 660, H = 250
  const padL = 56, padR = 8, padT = 12, padB = 34
  const n = spec.times.length
  const m = spec.metrics.length
  const plotW = W - padL - padR
  const plotH = H - padT - padB
  const slot = plotW / n

  // Y range (including the 0 baseline)
  let min = 0, max = 0
  for (const t of spec.times) {
    for (const mt of spec.metrics) {
      const v = parseNum(spec.rowAt(t)?.[mt.col])
      if (v !== null) { min = Math.min(min, v); max = Math.max(max, v) }
    }
  }
  if (min === 0 && max === 0) max = 1
  const span = (max - min) || 1
  max += span * 0.08
  if (min < 0) min -= span * 0.08
  const y0 = padT + plotH * (max / (max - min)) // y of the zero axis
  const yOf = (v) => padT + plotH * ((max - v) / (max - min))

  const ticks = 4
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) => min + ((max - min) * i) / ticks)
  const barW = Math.min(26, (slot * 0.62) / m)
  const rotateLabels = !spec.annualOnly && slot < 40

  return (
    <>
      <div className="chart-legend">
        {spec.metrics.map((mt) => {
          const latest = spec.times[n - 1]
          return (
            <span key={mt.col} className="legend-item">
              <span className="legend-dot" style={{ background: mt.color }} />
              {mt.label}
              <b className="legend-val">{compactNum(parseNum(spec.rowAt(latest)?.[mt.col]))}</b>
            </span>
          )
        })}
        <span className="legend-time">{fmtDateCol('end_date', spec.times[n - 1])}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="bar-svg" role="img">
        {yTicks.map((v, i) => (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={yOf(v)} y2={yOf(v)} className="grid-line" />
            <text x={padL - 6} y={yOf(v) + 3.5} textAnchor="end" className="axis-val">{compactNum(v)}</text>
          </g>
        ))}
        {min < 0 && <line x1={padL} x2={W - padR} y1={y0} y2={y0} stroke="#4a3a32" strokeWidth="1" />}
        {spec.times.map((t, i) => {
          const cx = padL + slot * (i + 0.5)
          return (
            <g key={t}>
              {spec.metrics.map((mt, j) => {
                const v = parseNum(spec.rowAt(t)?.[mt.col])
                if (v === null) return null
                const x = cx - (barW * m) / 2 + barW * j
                const y = yOf(Math.max(v, 0))
                const h = Math.max(1.5, Math.abs(yOf(v) - y0))
                return (
                  <rect key={mt.col} x={x} y={v >= 0 ? y : y0} width={barW} height={h} rx={2}
                    fill={v >= 0 ? mt.color : '#10b981'} opacity={0.88}>
                    <title>{`${fmtDateCol('end_date', t)} ${mt.label}: ${v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`}</title>
                  </rect>
                )
              })}
              <text
                x={rotateLabels ? cx + 2 : cx} y={rotateLabels ? H - padB + 10 : H - padB + 14}
                textAnchor={rotateLabels ? 'start' : 'middle'}
                transform={rotateLabels ? `rotate(-38, ${cx + 2}, ${H - padB + 10})` : undefined}
                className="axis-label"
              >
                {periodLabel(t, spec.annualOnly)}
              </text>
            </g>
          )
        })}
      </svg>
    </>
  )
}


function BarChart({ spec }) {
  const W = 640, rowH = 32, padT = 4, padB = 4, labelW = 100, valW = 76
  const rows = spec.rows
  const H = rows.length * rowH + padT + padB
  const vals = rows.map((r) => r.value)
  const max = Math.max(0, ...vals)
  const min = Math.min(0, ...vals)
  const span = max - min || 1
  const barMax = W - labelW - valW - 12
  const x0 = labelW + ((-min) / span) * barMax // zero-axis position

  return (
    <>
      <div className="chart-legend">
        <span className="legend-item">{spec.metric.label}对比</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="bar-svg" role="img">
        {min < 0 && <line x1={x0} x2={x0} y1={padT} y2={H - padB} stroke="#2c2420" />}
        {rows.map((r, i) => {
          const y = padT + i * rowH
          const w = Math.max(2, (Math.abs(r.value) / span) * barMax)
          const pos = r.value >= 0
          return (
            <g key={r.label + i}>
              <text x={labelW - 10} y={y + rowH / 2 + 4} textAnchor="end" className="bar-label">{r.label}</text>
              <rect
                x={pos ? x0 : x0 - w} y={y + 5} width={w} height={rowH - 10} rx={5}
                fill={pos ? '#f43f5e' : '#10b981'} opacity={0.82}
              />
              <text
                x={pos ? x0 + w + 8 : x0 - w - 8} y={y + rowH / 2 + 4}
                textAnchor={pos ? 'start' : 'end'} className="bar-val"
              >
                {compactNum(r.value)}
              </text>
            </g>
          )
        })}
      </svg>
      {spec.omitted > 0 && <div className="chart-note">其余 {spec.omitted} 项未展示</div>}
    </>
  )
}
