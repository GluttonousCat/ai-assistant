import { useEffect, useRef } from 'react'
import { createChart, ColorType, LineSeries } from 'lightweight-charts'
import { compactNum, parseNum } from './resultData'

export default function LineChart({ spec }) {
  const ref = useRef(null)
  // During streaming the parent rebuilds the spec object on every render; key by content to avoid chart rebuilds
  const key = JSON.stringify([spec.kind, spec.times, spec.metrics.map((m) => m.col)])
  const specRef = useRef(spec)
  specRef.current = spec

  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const s = specRef.current
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#a3a3a3',
        fontFamily: "'PingFang SC', 'Segoe UI', system-ui, sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(44,36,32,0.45)' },
        horzLines: { color: 'rgba(44,36,32,0.45)' },
      },
      width: el.clientWidth,
      height: 230,
      timeScale: { borderColor: '#2c2420' },
      rightPriceScale: { borderColor: '#2c2420' },
      crosshair: {
        vertLine: { color: '#d99178', labelBackgroundColor: '#7f3e2c' },
        horzLine: { color: '#d99178', labelBackgroundColor: '#7f3e2c' },
      },
      localization: { priceFormatter: compactNum },
    })
    for (const m of s.metrics) {
      const series = chart.addSeries(LineSeries, {
        color: m.color, lineWidth: 2, pointMarkersVisible: s.times.length <= 16,
      })
      series.setData(
        s.times
          .map((t) => ({ time: t, value: parseNum(s.rowAt(t)?.[m.col]) }))
          .filter((p) => p.value !== null)
      )
    }
    chart.timeScale().fitContent()
    const onResize = () => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth })
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
    }
  }, [key])

  const latest = spec.times[spec.times.length - 1]
  return (
    <>
      <div className="chart-legend">
        {spec.metrics.map((m) => (
          <span key={m.col} className="legend-item">
            <span className="legend-dot" style={{ background: m.color }} />
            {m.label}
            <b className="legend-val">{compactNum(parseNum(spec.rowAt(latest)?.[m.col]))}</b>
          </span>
        ))}
        <span className="legend-time">{latest}</span>
      </div>
      <div ref={ref} className="chart-canvas" />
    </>
  )
}
