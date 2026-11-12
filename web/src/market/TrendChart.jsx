import { useEffect, useRef, useState } from 'react'
import { CandlestickSeries, HistogramSeries, LineSeries, createChart } from 'lightweight-charts'

const stamp = (s) => Date.parse(s.replace(/Z$/, '').slice(0, 19) + '+08:00') / 1000
const timeLabel = (time) => typeof time === 'number'
  ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(time * 1000)) : ''

export default function TrendChart({ data, stop, config }) {
  const host = useRef(null)
  const [hover, setHover] = useState(null)
  useEffect(() => {
    if (!host.current || !data?.length) return
    const chart = createChart(host.current, {
      height: 440, width: host.current.clientWidth,
      layout: { background: { color: '#111110' }, textColor: '#97948c', fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif', panes: { separatorColor: '#292721' } },
      grid: { vertLines: { color: '#1b1b18' }, horzLines: { color: '#24231e' } },
      rightPriceScale: { borderColor: '#302b23' },
      timeScale: { borderColor: '#302b23', timeVisible: true, secondsVisible: false, tickMarkFormatter: timeLabel },
      localization: { timeFormatter: timeLabel },
    })
    const candles = chart.addSeries(CandlestickSeries, { upColor: '#e87768', downColor: '#55ad92', wickUpColor: '#e87768', wickDownColor: '#55ad92', borderVisible: false })
    const fast = chart.addSeries(LineSeries, { color: '#dfb671', lineWidth: 2, priceLineVisible: false, lastValueVisible: false })
    const slow = chart.addSeries(LineSeries, { color: '#98a9cb', lineWidth: 2, priceLineVisible: false, lastValueVisible: false })
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1)
    const rows = data.map((r) => ({ ...r, time: stamp(r.bar_end) })).filter((r) => Number.isFinite(r.time))
    candles.setData(rows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })))
    fast.setData(rows.filter((r) => r.ema_fast != null).map((r) => ({ time: r.time, value: r.ema_fast })))
    slow.setData(rows.filter((r) => r.ema_slow != null).map((r) => ({ time: r.time, value: r.ema_slow })))
    volume.setData(rows.map((r) => ({ time: r.time, value: r.volume, color: r.close >= r.open ? '#e8776866' : '#55ad9266' })))
    chart.panes()[0].setStretchFactor(4)
    chart.panes()[1].setStretchFactor(1)
    if (stop > 0) candles.createPriceLine({ price: stop, color: '#c68979', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '参考止损' })
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, rows.length - 120), to: rows.length + 4 })
    const byTime = new Map(rows.map((r) => [r.time, r]))
    setHover(rows.at(-1))
    chart.subscribeCrosshairMove((param) => setHover(byTime.get(param.time) || rows.at(-1)))
    const observer = new ResizeObserver(([entry]) => chart.applyOptions({ width: entry.contentRect.width }))
    observer.observe(host.current)
    return () => { observer.disconnect(); chart.remove() }
  }, [data, stop])
  return <>
    <div className="trend-chart-legend">
      <span className="trend-legend-fast">EMA {config?.fast || 20}</span><span className="trend-legend-slow">EMA {config?.slow || 60}</span>
      {hover && <span className="trend-ohlc">{timeLabel(hover.time)}　开 {hover.open.toFixed(2)}　高 {hover.high.toFixed(2)}　低 {hover.low.toFixed(2)}　收 {hover.close.toFixed(2)}</span>}
    </div>
    <div ref={host} className="trend-chart" aria-label="60分钟K线、均线及成交量，可拖动缩放" />
  </>
}
