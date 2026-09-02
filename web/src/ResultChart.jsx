import { useEffect, useRef } from 'react'
import { createChart, ColorType, LineSeries } from 'lightweight-charts'

// 结果数据可视化:
// - 报告期序列 (end_date/ann_date) -> 同花顺式分期柱状图 (Q1/H1/Q3/年报 各自独立柱, 不连线)
//   长序列 (>20 期) 自动聚合为年度柱 (近 12 年年报)
// - 日线序列 (trade_date) -> 折线图 (连续交易日, 连线有意义)
// - 分类对比 (股票+数值)     -> 横向条形图
// 与 AgentChat 共享的展示工具也集中在此 (COL_LABELS/parseNum/compactNum/fmtDateCol)

export const COL_LABELS = {
  stock_name: '股票', name: '股票', industry: '行业',
  end_date: '报告期', trade_date: '交易日', ann_date: '公告日',
  total_revenue: '营业总收入', revenue: '营业收入',
  n_income: '净利润', n_income_attr_p: '归母净利润',
  net_after_nr_lp_correct: '扣非净利润',
  operate_profit: '营业利润', total_profit: '利润总额', basic_eps: '每股收益',
  rd_exp: '研发费用',
  total_assets: '总资产', total_liab: '总负债', equity_attr_p: '归母净资产',
  money_cap: '货币资金', accounts_receiv: '应收账款', invent: '存货',
  goodwill: '商誉',
  n_cashflow_act: '经营现金流', n_cashflow_inv_act: '投资现金流',
  n_cash_flows_fnc_act: '筹资现金流',
  roe: 'ROE(%)', roe_waa: '加权ROE(%)',
  netprofit_yoy: '净利润同比(%)', or_yoy: '营收同比(%)',
  grossprofit_margin: '毛利率(%)', netprofit_margin: '净利率(%)',
  debt_to_assets: '资产负债率(%)',
  current_ratio: '流动比率', quick_ratio: '速动比率',
  close: '收盘价', open: '开盘价', high: '最高', low: '最低',
  vol: '成交量', amount: '成交额', pct_chg: '涨跌幅(%)',
  pe_ttm: '市盈率TTM', pb: '市净率', ps_ttm: '市销率TTM',
  total_mv: '总市值(万元)', circ_mv: '流通市值(万元)', turnover_rate: '换手率(%)',
  value: '数值',
}

const PERIOD_LABELS = { '03-31': '一季报', '06-30': '中报', '09-30': '三季报', '12-31': '年报' }
// X 轴紧凑期别: 24Q1 / 24H1 / 24Q3 / 24年报
const PERIOD_SHORT = { '03-31': 'Q1', '06-30': 'H1', '09-30': 'Q3', '12-31': '年报' }
const DATE_COLS = ['end_date', 'ann_date', 'trade_date']
const NAME_COLS = ['stock_name', 'name']
const SERIES_COLORS = ['#f43f5e', '#f5b942', '#b8a8f5', '#4aa3ff']
const BAR_MAX_ROWS = 12
const ANNUAL_TRIGGER = 20 // 报告期超过此数 -> 聚合为年度柱

// 数值解析: 兼容 PG NUMERIC 序列化成的纯数字字符串
export function parseNum(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && /^[+-]?\d+(\.\d+)?$/.test(v.trim())) {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

// 坐标轴/图例用的紧凑数值 (亿/万为单位缩写, 与字段单位无关)
export function compactNum(v) {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e12) return (v / 1e12).toFixed(1) + '万亿'
  if (a >= 1e8) return (v / 1e8).toFixed(1) + '亿'
  if (a >= 1e4) return (v / 1e4).toFixed(1) + '万'
  if (a >= 100) return v.toFixed(0)
  return String(parseFloat(v.toFixed(2)))
}

export function fmtDateCol(col, v) {
  if (v === null || v === undefined || v === '') return '—'
  const s = String(v)
  if ((col === 'end_date' || col === 'ann_date') && /^\d{4}-\d{2}-\d{2}/.test(s)) {
    const label = PERIOD_LABELS[s.slice(5, 10)]
    if (label) return `${s.slice(0, 4)} ${label}`
  }
  return s
}

function isNumericCol(data, col) {
  let seen = 0
  for (const r of data.slice(0, 10)) {
    const v = r[col]
    if (v === null || v === undefined || v === '') continue
    if (parseNum(v) === null) return false
    seen += 1
  }
  return seen > 0
}

// 柱状 X 轴标签: 全为年报时用年份, 混合期别用 短标签
function periodLabel(t, annualOnly) {
  if (annualOnly) return t.slice(2, 4) + '年'
  return t.slice(2, 4) + (PERIOD_SHORT[t.slice(5, 10)] || t.slice(5, 10))
}

/**
 * 识别结果集的可视化形态:
 * - period: 报告期序列 (end_date/ann_date) + 1~4 数值列, 单实体 -> 分期柱状图
 * - line  : 日线序列 (trade_date) + 1~4 数值列, 单实体 -> 折线图
 * - bar   : 名称列 + 恰好 1 个数值列, 2~30 行 (多实体对比/行业排名) -> 横向条形图
 * - null  : 不可图示 (调用方回退表格)
 */
export function buildChartSpec(columns, data) {
  const cols = (columns || []).filter((c) => c !== 'ts_code')
  if (!data || data.length === 0) return null
  const dateCol = DATE_COLS.find((c) => cols.includes(c))
  const nameCol = NAME_COLS.find((c) => cols.includes(c))
  const metricCols = cols.filter(
    (c) => c !== dateCol && c !== nameCol && isNumericCol(data, c)
  )
  const singleEntity = !nameCol
    || [...new Set(data.map((r) => r[nameCol]).filter((v) => v !== null && v !== undefined))].length <= 1

  if (dateCol && metricCols.length >= 1 && metricCols.length <= 4 && singleEntity) {
    // 升序去重 (SQL 常为 DESC), 同日多行取最后一行
    const byDate = new Map()
    for (const r of data) {
      const t = String(r[dateCol] ?? '').slice(0, 10)
      if (/^\d{4}-\d{2}-\d{2}$/.test(t)) byDate.set(t, r)
    }
    let times = [...byDate.keys()].sort()
    if (times.length < 2) return null

    let annualOnly = false
    if (dateCol !== 'trade_date' && times.length > ANNUAL_TRIGGER) {
      // 季度数据跨度太长 -> 聚合为年度柱 (近 12 年年报), 避免 40+ 根拥挤柱
      const annual = times.filter((t) => t.endsWith('-12-31'))
      if (annual.length >= 5) {
        times = annual
        annualOnly = true
      }
    }
    const metrics = metricCols.map((c, i) => ({
      col: c, label: COL_LABELS[c] || c, color: SERIES_COLORS[i % SERIES_COLORS.length],
    }))
    return {
      kind: dateCol === 'trade_date' ? 'line' : 'period',
      metrics,
      times,
      annualOnly,
      rowAt: (t) => byDate.get(t),
    }
  }

  // 多实体 (对比/行业排名) 或无日期: 名称 + 单一数值 -> 横向条形图
  if (nameCol && metricCols.length === 1 && data.length >= 2 && data.length <= 30) {
    const col = metricCols[0]
    const rows = data
      .map((r) => ({ label: String(r[nameCol] ?? '—'), value: parseNum(r[col]) }))
      .filter((r) => r.value !== null)
      .sort((a, b) => b.value - a.value)
    if (rows.length >= 2) {
      const omitted = rows.length > BAR_MAX_ROWS ? rows.length - BAR_MAX_ROWS : 0
      return {
        kind: 'bar',
        metric: { label: COL_LABELS[col] || col },
        rows: rows.slice(0, BAR_MAX_ROWS),
        omitted,
      }
    }
  }
  return null
}

// ---------- 渲染 ----------

export default function ResultChart({ spec }) {
  if (!spec) return null
  return (
    <div className="chat-chart">
      {spec.kind === 'line' ? <LineChart spec={spec} />
        : spec.kind === 'period' ? <PeriodChart spec={spec} />
        : <BarChart spec={spec} />}
    </div>
  )
}

// 同花顺式分期柱状图: 每个报告期一根柱, 不连线 (Q1/H1/Q3/年报 累计口径互不可比)
function PeriodChart({ spec }) {
  const W = 660, H = 250
  const padL = 56, padR = 8, padT = 12, padB = 34
  const n = spec.times.length
  const m = spec.metrics.length
  const plotW = W - padL - padR
  const plotH = H - padT - padB
  const slot = plotW / n

  // Y 范围 (含 0 基线)
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
  const y0 = padT + plotH * (max / (max - min)) // 零轴 y
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

function LineChart({ spec }) {
  const ref = useRef(null)
  // 流式期间父组件每次渲染都会重建 spec 对象, 以内容为 key 避免图表反复重建
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

function BarChart({ spec }) {
  const W = 640, rowH = 32, padT = 4, padB = 4, labelW = 100, valW = 76
  const rows = spec.rows
  const H = rows.length * rowH + padT + padB
  const vals = rows.map((r) => r.value)
  const max = Math.max(0, ...vals)
  const min = Math.min(0, ...vals)
  const span = max - min || 1
  const barMax = W - labelW - valW - 12
  const x0 = labelW + ((-min) / span) * barMax // 零轴位置

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
