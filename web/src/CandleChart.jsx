import { useEffect, useRef } from 'react'
import { createChart, ColorType, CandlestickSeries } from 'lightweight-charts'

/**
 * 蜡烛图 + 30日分位区间边界 (lightweight-charts v5 API)
 * props: candles [{t,o,h,l,c,v}], range {upper, lower}
 */
export default function CandleChart({ candles, range }) {
  const ref = useRef(null)

  useEffect(() => {
    if (!ref.current || !candles || candles.length === 0) return undefined

    const chart = createChart(ref.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#161b22' },
        textColor: '#8b949e',
        fontFamily: "'Segoe UI', system-ui, sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(42,49,64,0.5)' },
        horzLines: { color: 'rgba(42,49,64,0.5)' },
      },
      width: ref.current.clientWidth,
      height: ref.current.clientHeight,
      timeScale: { borderColor: '#2a3140', timeVisible: false },
      rightPriceScale: { borderColor: '#2a3140' },
      crosshair: {
        vertLine: { color: '#4aa3ff', labelBackgroundColor: '#2f6fdb' },
        horzLine: { color: '#4aa3ff', labelBackgroundColor: '#2f6fdb' },
      },
    })

    // v5: addSeries(SeriesType, options)
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#f43f5e', downColor: '#10b981',
      borderUpColor: '#f43f5e', borderDownColor: '#10b981',
      wickUpColor: '#f43f5e', wickDownColor: '#10b981',
    })
    series.setData(candles.map((c) => ({
      time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
    })))

    if (range) {
      series.createPriceLine({
        price: range.upper, color: '#4aa3ff', lineWidth: 2,
        lineStyle: 2, axisLabelVisible: true, title: 'Upper',
      })
      series.createPriceLine({
        price: range.lower, color: '#4aa3ff', lineWidth: 2,
        lineStyle: 2, axisLabelVisible: true, title: 'Lower',
      })
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
  }, [candles, range])

  return <div ref={ref} style={{ width: '100%', height: '100%' }} />
}
