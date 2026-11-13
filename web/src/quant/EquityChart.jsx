import { useEffect, useRef } from 'react'
import { createChart, LineSeries } from 'lightweight-charts'

export default function EquityChart({ data, initialCash }) {
  const host = useRef(null)
  useEffect(() => {
    if (!host.current || !data?.length) return
    const chart = createChart(host.current, {
      height: 340, width: host.current.clientWidth,
      layout: { background: { color: '#121110' }, textColor: '#aaa095' },
      grid: { vertLines: { color: '#201c18' }, horzLines: { color: '#29231e' } },
      rightPriceScale: { borderColor: '#3b3027' },
      timeScale: { borderColor: '#3b3027', timeVisible: false },
    })
    const nav = chart.addSeries(LineSeries, { color: '#dfb671', lineWidth: 2, title: '策略', priceFormat: { type: 'price', precision: 3, minMove: .001 } })
    nav.setData(data.map((p) => ({ time: p.date, value: p.equity / initialCash })))
    for (const [code, title, color] of [['000852.SH', '中证1000', '#8da3c9'], ['000300.SH', '沪深300', '#85a58c']]) {
      const line = chart.addSeries(LineSeries, { color, lineWidth: 1, lineStyle: 2, title, priceFormat: { type: 'price', precision: 3, minMove: .001 } })
      line.setData(data.map((p) => p.benchmarks?.[code] == null ? { time: p.date } : { time: p.date, value: p.benchmarks[code] }))
    }
    chart.timeScale().fitContent()
    const observer = new ResizeObserver(([entry]) => chart.applyOptions({ width: entry.contentRect.width }))
    observer.observe(host.current)
    return () => { observer.disconnect(); chart.remove() }
  }, [data, initialCash])
  return <div ref={host} className="quant-chart" aria-label="策略净值、中证1000和沪深300基准净值曲线" />
}
