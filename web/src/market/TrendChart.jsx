import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, CrosshairMode, HistogramSeries, LineSeries, createChart, createSeriesMarkers } from 'lightweight-charts'
import { boundedCache, captureView, chartRows, restoreView, seriesUpdate, signalMarkers } from './chartData'

const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
const minuteFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
const timeLabel = (time, timeframe) => typeof time === 'number' ? (timeframe === '1D' ? dateFormat : minuteFormat).format(time * 1000) : ''
const candle = ({ time, open, high, low, close }) => ({ time, open, high, low, close })
const average = (row, field) => Number.isFinite(row[field]) ? { time: row.time, value: row[field] } : { time: row.time }
const turnover = (row) => Number.isFinite(row.volume) && row.volume >= 0 ? { time: row.time, value: row.volume, color: row.close >= row.open ? '#e8776866' : '#55ad9266' } : { time: row.time }
const latestRange = (length) => ({ from: Math.max(0, length - 120), to: length + 4 })

const TrendChart = memo(function TrendChart({ data, instrumentKey, stop, stopLabel = '参考止损', config, timeframe = '60F', resetView = 0 }) {
  const host = useRef(null)
  const api = useRef(null)
  const frames = useRef(null)
  const [hover, setHover] = useState(null)
  const rows = useMemo(() => chartRows(data), [data])

  // The canvas and series live for this component's lifetime, not per response.
  useEffect(() => {
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { color: '#111110' }, textColor: '#97948c', fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif', panes: { separatorColor: '#292721' } },
      grid: { vertLines: { color: '#1b1b18' }, horzLines: { color: '#24231e' } },
      crosshair: { mode: CrosshairMode.Normal },
      kineticScroll: { mouse: true, touch: true },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
      rightPriceScale: { borderColor: '#302b23' },
      timeScale: { borderColor: '#302b23', rightOffset: 4, barSpacing: 8, minBarSpacing: 2 },
    })
    const candles = chart.addSeries(CandlestickSeries, { upColor: '#e87768', downColor: '#55ad92', wickUpColor: '#e87768', wickDownColor: '#55ad92', borderVisible: false })
    const fast = chart.addSeries(LineSeries, { color: '#dfb671', lineWidth: 2, priceLineVisible: false, lastValueVisible: false })
    const slow = chart.addSeries(LineSeries, { color: '#98a9cb', lineWidth: 2, priceLineVisible: false, lastValueVisible: false })
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1)
    const markers = createSeriesMarkers(candles, [])
    chart.panes()[0].setStretchFactor(4)
    chart.panes()[1].setStretchFactor(1)
    const state = { chart, candles, fast, slow, volume, markers, rows: [], byTime: new Map(), views: boundedCache(20), key: '', priceLine: null, shown: null, pending: null, hoverTime: null }
    api.current = state
    const move = (param) => {
      state.hoverTime = state.byTime.has(param.time) ? param.time : null
      state.pending = state.byTime.get(state.hoverTime) || state.rows.at(-1) || null
      if (state.pending === state.shown || frames.current != null) return
      frames.current = requestAnimationFrame(() => {
        frames.current = null
        if (state.pending !== state.shown) {
          state.shown = state.pending
          setHover({ key: state.key, row: state.shown })
        }
      })
    }
    chart.subscribeCrosshairMove(move)
    return () => {
      if (frames.current != null) cancelAnimationFrame(frames.current)
      frames.current = null
      chart.unsubscribeCrosshairMove(move)
      markers.detach()
      chart.remove()
      api.current = null
    }
  }, [])

  useEffect(() => {
    const state = api.current
    const range = state.chart.timeScale().getVisibleLogicalRange()
    if (state.key && state.rows.length && range) state.views.set(state.key, captureView(state.rows, range))
    const changed = state.key !== instrumentKey
    const previous = changed ? [] : state.rows
    const plan = seriesUpdate(previous, rows)
    if (plan.type === 'replace') {
      state.candles.setData(rows.map(candle))
      state.fast.setData(rows.map((r) => average(r, 'ma13')))
      state.slow.setData(rows.map((r) => average(r, 'ma21')))
      state.volume.setData(rows.map(turnover))
    } else if (plan.type === 'update') {
      for (const row of rows.slice(plan.from)) {
        state.candles.update(candle(row))
        state.fast.update(average(row, 'ma13'))
        state.slow.update(average(row, 'ma21'))
        state.volume.update(turnover(row))
      }
    }
    state.key = instrumentKey
    state.rows = rows
    state.byTime = new Map(rows.map((r) => [r.time, r]))
    if (changed) state.hoverTime = null
    state.shown = state.byTime.get(state.hoverTime) || rows.at(-1) || null
    state.pending = state.shown
    setHover({ key: instrumentKey, row: state.shown })
    if (rows.length) {
      const saved = restoreView(rows, state.views.get(instrumentKey))
      state.chart.timeScale().setVisibleLogicalRange(saved || latestRange(rows.length))
    }
    if (changed) state.candles.priceScale().applyOptions({ autoScale: true })
  }, [rows, instrumentKey])

  useEffect(() => {
    api.current.chart.applyOptions({
      timeScale: { timeVisible: timeframe === '60F', secondsVisible: false, tickMarkFormatter: (time) => timeLabel(time, timeframe) },
      localization: { timeFormatter: (time) => timeLabel(time, timeframe) },
    })
  }, [timeframe])

  useEffect(() => {
    api.current.markers.setMarkers(['early', 'dual'].includes(config?.selection_mode) ? signalMarkers(rows) : [])
  }, [rows, config?.selection_mode])

  useEffect(() => {
    const state = api.current
    if (state.priceLine) { state.candles.removePriceLine(state.priceLine); state.priceLine = null }
    if (rows.length && stop > 0) state.priceLine = state.candles.createPriceLine({ price: stop, color: '#c68979', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: stopLabel })
  }, [stop, stopLabel, rows.length])

  useEffect(() => {
    const state = api.current
    if (state.rows.length) {
      state.chart.timeScale().setVisibleLogicalRange(latestRange(state.rows.length))
      state.candles.priceScale().applyOptions({ autoScale: true })
    }
  }, [resetView])

  const active = hover?.key === instrumentKey ? hover.row : rows.at(-1)
  return <>
    <div className="trend-chart-legend">
      <span className="trend-legend-fast">MA 13{Number.isFinite(active?.ma13) ? ` · ${active.ma13.toFixed(2)}` : ''}</span><span className="trend-legend-slow">MA 21{Number.isFinite(active?.ma21) ? ` · ${active.ma21.toFixed(2)}` : ''}</span>
      {active && <span className="trend-ohlc">{timeLabel(active.time, timeframe)}　开 {active.open.toFixed(2)}　高 {active.high.toFixed(2)}　低 {active.low.toFixed(2)}　收 {active.close.toFixed(2)}</span>}
      {active?.startup_count > 0 && ['early', 'dual'].includes(config?.selection_mode) && <span className="trend-legend-fast">{active.pattern_label || ''} · {active.setup_phase === 'pullback_resume' ? '回调再起，本轮' : '本段'}第 {active.startup_count} 阳 / {active.startup_age} 根K线</span>}
    </div>
    <div ref={host} className="trend-chart" aria-label={`${timeframe === '1D' ? '日K' : '60分钟K线'}、均线及成交量，可拖动缩放`} />
  </>
})

export default TrendChart
