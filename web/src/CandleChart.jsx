import { useEffect, useRef } from 'react'
import { createChart, ColorType, CrosshairMode, CandlestickSeries, HistogramSeries } from 'lightweight-charts'

/**
 * 蜡烛图 + 成交量副图 + 区间带 (lightweight-charts v5)
 * props: candles [{t,o,h,l,c,v}], range {upper, lower}, height?
 */
export default function CandleChart({ candles, range, height = 478 }) {
  const ref = useRef(null)

  useEffect(() => {
    if (!ref.current || !candles || candles.length === 0) return undefined

    // 主图 + 成交量副图 布局: 主体 78%, 成交量 22%
    const chart = createChart(ref.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#131722' },
        textColor: '#9aa4b2',
        fontFamily: "'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif",
        fontSize: 12,
      },
      grid: {
        vertLines: { color: 'rgba(42,49,64,0.35)' },
        horzLines: { color: 'rgba(42,49,64,0.35)' },
      },
      width: ref.current.clientWidth,
      height,
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#4aa3ff80', width: 1, style: 2, labelBackgroundColor: '#2f6fdb' },
        horzLine: { color: '#4aa3ff80', width: 1, style: 2, labelBackgroundColor: '#2f6fdb' },
      },
      rightPriceScale: {
        borderColor: '#2a3140',
        scaleMargins: { top: 0.08, bottom: 0.28 },   // 下方留成交量空间
      },
      timeScale: {
        borderColor: '#2a3140',
        timeVisible: false,
        rightOffset: 3,
        barSpacing: 8,
        minBarSpacing: 2,
      },
      handleScale: { axisPressedMouseMove: { time: true, price: false } },
    })

    // ---- 蜡烛 (A股配色: 红涨绿跌, 更柔和的现代色) ----
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#ef5350', downColor: '#26a69a',
      borderUpColor: '#ef5350', borderDownColor: '#26a69a',
      wickUpColor: '#ef535080', wickDownColor: '#26a69a80',
      priceLineColor: '#4aa3ff',
      priceLineStyle: 2,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    series.setData(candles.map((c) => ({
      time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
    })))

    // ---- 区间带: 上/下沿 + 中轴 ----
    if (range) {
      series.createPriceLine({
        price: range.upper, color: '#f5b942', lineWidth: 2,
        lineStyle: 0, axisLabelVisible: true, title: '上沿',
      })
      series.createPriceLine({
        price: range.lower, color: '#f5b942', lineWidth: 2,
        lineStyle: 0, axisLabelVisible: true, title: '下沿',
      })
      const mid = (range.upper + range.lower) / 2
      series.createPriceLine({
        price: mid, color: '#f5b94260', lineWidth: 1,
        lineStyle: 2, axisLabelVisible: false, title: '',
      })
    }

    // ---- 成交量副图 (叠在主图底部) ----
    const volSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      color: '#4aa3ff40',
    })
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.80, bottom: 0 },   // 成交量只占底部 20%
    })
    volSeries.setData(candles.map((c) => ({
      time: c.t, value: c.v,
      color: c.c >= c.o ? '#ef535045' : '#26a69a45',   // 红涨绿跌 (半透明)
    })))

    chart.timeScale().fitContent()

    const onResize = () => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth })
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
    }
  }, [candles, range, height])

  return <div ref={ref} style={{ width: '100%', height }} />
}