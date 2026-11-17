import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, CrosshairMode, HistogramSeries, LineSeries, createChart, createSeriesMarkers } from 'lightweight-charts'
import { boundedCache, captureView, chartRows, restoreView, seriesUpdate, signalMarkers } from './chartData'
import OpportunityBadge from '../OpportunityBadge'
import { opportunityFor } from '../opportunities'

const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
const minuteFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
const timeLabel = (time, timeframe) => typeof time === 'number' ? (timeframe === '1D' ? dateFormat : minuteFormat).format(time * 1000) : ''
const candle = ({ time, open, high, low, close }) => ({ time, open, high, low, close })
const average = (row, field) => Number.isFinite(row[field]) ? { time: row.time, value: row[field] } : { time: row.time }
const turnover = (row) => Number.isFinite(row.volume) && row.volume >= 0 ? { time: row.time, value: row.volume, color: row.close >= row.open ? '#e8776866' : '#55ad9266' } : { time: row.time }
const latestRange = (length) => ({ from: Math.max(0, length - 120), to: length + 4 })
const signalRange = (rows) => {
  const index = rows.findIndex((row) => row.tracking_signal)
  return index < 0 ? latestRange(rows.length) : { from: Math.max(0, index - 60), to: Math.max(index + 12, Math.min(rows.length + 4, index + 60)) }
}

function applyRange(state, range, isCurrent) {
  const request = ++state.rangeRequest
  state.chart.timeScale().setVisibleLogicalRange(range)
  // Autoscale can widen price labels and shrink the time axis by a few pixels.
  // Reapply once after layout, without letting an old request move a new chart.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (isCurrent() && state.rangeRequest === request) state.chart.timeScale().setVisibleLogicalRange(range)
  }))
}

const TrendChart = memo(function TrendChart({ data, instrumentKey, stop, stopLabel = '参考止损', config, timeframe = '60F', resetView = 0, focusSignal = 0 }) {
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
      timeScale: { borderColor: '#302b23', rightOffset: 4, barSpacing: 8, minBarSpacing: .1, lockVisibleTimeRangeOnResize: true },
    })
    const candles = chart.addSeries(CandlestickSeries, { upColor: '#e87768', downColor: '#55ad92', wickUpColor: '#e87768', wickDownColor: '#55ad92', borderVisible: false })
    const fast = chart.addSeries(LineSeries, { color: '#dfb671', lineWidth: 2, priceLineVisible: false, lastValueVisible: false })
    const quick = chart.addSeries(LineSeries, { color: '#d89bc1', lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
    const slow = chart.addSeries(LineSeries, { color: '#98a9cb', lineWidth: 2, priceLineVisible: false, lastValueVisible: false })
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1)
    const markers = createSeriesMarkers(candles, [])
    chart.panes()[0].setStretchFactor(4)
    chart.panes()[1].setStretchFactor(1)
    const state = { chart, candles, quick, fast, slow, volume, markers, rows: [], byTime: new Map(), views: boundedCache(20), key: '', priceLine: null, shown: null, pending: null, hoverTime: null, rangeRequest: 0 }
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
    state.rangeRequest += 1
    const range = state.chart.timeScale().getVisibleLogicalRange()
    if (state.key && state.rows.length && range) state.views.set(state.key, captureView(state.rows, range))
    const changed = state.key !== instrumentKey
    const previous = changed ? [] : state.rows
    const plan = seriesUpdate(previous, rows)
    if (plan.type === 'replace') {
      state.candles.setData(rows.map(candle))
      state.fast.setData(rows.map((r) => average(r, 'ma13')))
      state.quick.setData(rows.map((r) => average(r, 'ma8')))
      state.slow.setData(rows.map((r) => average(r, 'ma21')))
      state.volume.setData(rows.map(turnover))
    } else if (plan.type === 'update') {
      for (const row of rows.slice(plan.from)) {
        state.candles.update(candle(row))
        state.fast.update(average(row, 'ma13'))
        state.quick.update(average(row, 'ma8'))
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
      applyRange(state, saved || (timeframe === '1D' ? { from: 0, to: rows.length + 4 } : config?.selection_mode === 'tracking' ? signalRange(rows) : latestRange(rows.length)), () => api.current === state)
    }
    if (changed) state.candles.priceScale().applyOptions({ autoScale: true })
  }, [rows, instrumentKey, config?.selection_mode, timeframe])

  useEffect(() => {
    api.current.chart.applyOptions({
      timeScale: { timeVisible: timeframe === '60F', secondsVisible: false, minBarSpacing: timeframe === '1D' ? .1 : 2, tickMarkFormatter: (time) => timeLabel(time, timeframe) },
      localization: { timeFormatter: (time) => timeLabel(time, timeframe) },
    })
  }, [timeframe])

  useEffect(() => {
    const mtf = config?.selection_mode === 'mtf'
    const tracked = rows.filter((r) => r.tracking_signal).map((r) => ({ time: r.time, position: 'aboveBar', shape: 'arrowDown', color: '#dfb671', text: '入选日' }))
    api.current.markers.setMarkers(config?.selection_mode === 'tracking' ? tracked : (mtf ? timeframe === '1D' || config?.fresh_reversal : ['early', 'dual'].includes(config?.selection_mode)) ? signalMarkers(rows, { dailyEntry: mtf }) : [])
  }, [rows, config?.selection_mode, config?.fresh_reversal, timeframe])

  useEffect(() => {
    const state = api.current
    if (state.priceLine) { state.candles.removePriceLine(state.priceLine); state.priceLine = null }
    if (rows.length && stop > 0) state.priceLine = state.candles.createPriceLine({ price: stop, color: config?.selection_mode === 'tracking' ? '#dfb671' : '#c68979', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: stopLabel })
  }, [stop, stopLabel, rows.length, config?.selection_mode])

  useEffect(() => {
    const state = api.current
    state.rangeRequest += 1
    if (state.rows.length) {
      applyRange(state, latestRange(state.rows.length), () => api.current === state)
      state.candles.priceScale().applyOptions({ autoScale: true })
    }
  }, [resetView])

  useEffect(() => {
    if (focusSignal && rows.length) {
      api.current.rangeRequest += 1
      const state = api.current
      applyRange(state, signalRange(rows), () => api.current === state)
      api.current.candles.priceScale().applyOptions({ autoScale: true })
    }
  }, [focusSignal, rows])

  const active = hover?.key === instrumentKey ? hover.row : rows.at(-1)
  const showDays = (count) => {
    const state = api.current
    if (state.rows.length) {
      const range = { from: Math.max(0, state.rows.length - count), to: state.rows.length + 4 }
      state.candles.priceScale().applyOptions({ autoScale: true })
      applyRange(state, range, () => api.current === state)
    }
  }
  return <>
    {timeframe === '1D' && <div className="trend-history-range" role="group" aria-label="日K历史范围"><span>日K · 近5年可用历史</span><button onClick={() => showDays(63)}>近3月</button><button onClick={() => showDays(252)}>近1年</button><button onClick={() => showDays(rows.length)}>近5年</button></div>}
    <div className="trend-chart-legend">
      <span style={{ color: '#d89bc1' }}>M8{Number.isFinite(active?.ma8) ? ` · ${active.ma8.toFixed(2)}` : ''}</span><span className="trend-legend-fast">M13{Number.isFinite(active?.ma13) ? ` · ${active.ma13.toFixed(2)}` : ''}</span><span className="trend-legend-slow">M21{Number.isFinite(active?.ma21) ? ` · ${active.ma21.toFixed(2)}` : ''}</span>
      {config?.selection_mode === 'mtf' && active?.trend_state && <span>60F背景：{active.trend_state}</span>}
      {config?.selection_mode !== 'tracking' && active && opportunityFor(active).key !== 'unknown' && <OpportunityBadge value={active} />}
      {config?.selection_mode !== 'tracking' && active?.parent_trend_label && <span>母趋势：{active.parent_trend_label}{active.parent_pullback_bars > 0 ? ` · 回调${active.parent_pullback_bars}根日K` : ''}{active.local_trigger_label ? ` · ${active.local_trigger_label}` : ''}</span>}
      {active && <span className="trend-ohlc">{timeLabel(active.time, timeframe)}　开 {active.open.toFixed(2)}　高 {active.high.toFixed(2)}　低 {active.low.toFixed(2)}　收 {active.close.toFixed(2)}</span>}
      {active?.startup_count > 0 && ['early', 'dual'].includes(config?.selection_mode) && <span className="trend-legend-fast">{active.pattern_label || ''} · {active.setup_phase === 'pullback_resume' ? '回调再起，本轮' : '本段'}第 {active.startup_count} 阳 / {active.startup_age} 根K线</span>}
    </div>
    {config?.selection_mode !== 'tracking' && <div className="trend-signal-legend" aria-label="信号类型图例"><OpportunityBadge value="trend_pullback" /><OpportunityBadge value="base_rebound" /><span>箭头 / 方块为确认 · 圆点为观察 · 灰色为起势背景</span></div>}
    <div ref={host} className="trend-chart" aria-label={`${timeframe === '1D' ? '日K' : '60分钟K线'}、均线及成交量，可拖动缩放`} />
  </>
})

export default TrendChart
