import { useEffect, useMemo, useState, useCallback } from 'react'
import { api } from './api'
import CandleChart from './CandleChart'
import Tracking from './Tracking'
import Setup from './Setup'

const STATE_LABEL = {
  RANGE_FORMATION: '形成中',
  EARLY_TRADABLE_RANGE: '早期可交易',
  MATURE_RANGE: '成熟区间',
  RANGE_RENEWAL: '区间更新',
}
const STATE_CLASS = {
  RANGE_FORMATION: 'FORMATION',
  EARLY_TRADABLE_RANGE: 'EARLY',
  MATURE_RANGE: 'MATURE',
  RANGE_RENEWAL: 'RENEWAL',
}
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? '—' : Number(v).toFixed(d))

function zoneOf(pos) {
  if (pos < 0.2) return 'lower'
  if (pos > 0.8) return 'upper'
  return 'middle'
}

export default function App({ embedded = false }) {
  const [tab, setTab] = useState('range')   // range | trend | setup
  const [rangeSub, setRangeSub] = useState('signals')   // signals | tracking
  const [data, setData] = useState(null)
  const [dates, setDates] = useState([])
  const [curDate, setCurDate] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [minScore, setMinScore] = useState(70)
  const [zoneFilter, setZoneFilter] = useState('all')
  const [riskFilter, setRiskFilter] = useState('all')
  const [stateFilter, setStateFilter] = useState([])   // 多选: [] = 全部
  const [sortKey, setSortKey] = useState('range_score')
  const [sortDir, setSortDir] = useState(-1)
  const [chartSym, setChartSym] = useState(null)
  const [chartData, setChartData] = useState(null)
  const [scanning, setScanning] = useState(false)

  const load = useCallback(async (date = null, score = minScore) => {
    setLoading(true)
    setError(null)
    try {
      const res = date ? await api.scan(date, score, true) : await api.latest(score, true)
      setData(res)
      setCurDate(res.date)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [minScore])

  useEffect(() => {
    load()
    api.dates().then((d) => setDates(d.dates)).catch(() => {})
  }, [])

  const filtered = useMemo(() => {
    if (!data) return []
    let rows = data.records || []
    if (zoneFilter !== 'all') rows = rows.filter((r) => zoneOf(r.range_pos) === zoneFilter)
    if (riskFilter !== 'all') rows = rows.filter((r) => r.trend_risk === riskFilter)
    if (stateFilter.length > 0) rows = rows.filter((r) => stateFilter.includes(r.state))
    const arr = [...rows]
    arr.sort((a, b) => {
      const va = a[sortKey] ?? -Infinity
      const vb = b[sortKey] ?? -Infinity
      return (va - vb) * sortDir
    })
    return arr
  }, [data, zoneFilter, riskFilter, stateFilter, sortKey, sortDir])

  const stats = useMemo(() => {
    if (!data) return null
    const rows = data.records || []
    const lower = rows.filter((r) => zoneOf(r.range_pos) === 'lower').length
    const upper = rows.filter((r) => zoneOf(r.range_pos) === 'upper').length
    const lowRisk = rows.filter((r) => r.trend_risk === 'LOW').length
    return { total: rows.length, lower, upper, lowRisk }
  }, [data])

  const openChart = async (sym) => {
    setChartSym(sym)
    setChartData(null)
    try {
      const res = await api.chart(sym, 60)
      setChartData(res)
    } catch (e) {
      setChartData({ error: e.message })
    }
  }

  const doScan = async () => {
    setScanning(true)
    try {
      await api.triggerScan()
      setTimeout(() => { load(); setScanning(false) }, 3000)
    } catch {
      setScanning(false)
    }
  }

  const onSort = (key) => {
    if (sortKey === key) setSortDir(-sortDir)
    else { setSortKey(key); setSortDir(-1) }
  }

  return (
    <div className="app">
      <div className="topbar" style={embedded ? { position: 'static', paddingTop: 14, paddingBottom: 14 } : undefined}>
        <div className="logo">Range Scanner</div>
        <div className="sub">可交易震荡区间 · 日K Early Detection</div>
        <div className="tabs">
          <button className={tab === 'range' ? 'on' : ''} onClick={() => setTab('range')}>震荡看板</button>
          <button className={tab === 'trend' ? 'on' : ''} onClick={() => setTab('trend')}>趋势看板</button>
        </div>
        <div className="spacer" />
        {curDate && <div className="date-badge">扫描日 <b>{curDate}</b></div>}
        {data?.last_scan_ts && (
          <div className="date-badge">更新 <b>{data.last_scan_ts.slice(5, 16)}</b></div>
        )}
      </div>

      {tab === 'trend' ? (
        <Setup onOpenChart={openChart} onDateChange={setCurDate} />
      ) : (
      <>
      <div className="controls" style={{ paddingBottom: 0 }}>
        <div className="seg subtabs">
          <button className={rangeSub === 'signals' ? 'on' : ''} onClick={() => setRangeSub('signals')}>当日信号</button>
          <button className={rangeSub === 'tracking' ? 'on' : ''} onClick={() => setRangeSub('tracking')}>历史表现</button>
        </div>
      </div>
      {rangeSub === 'tracking' ? (
        <Tracking onOpenChart={openChart} />
      ) : (
      <>
      <div className="controls">
        <div className="control">
          <span>日期</span>
          <select value={curDate || ''} onChange={(e) => load(e.target.value)}>
            {dates.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <div className="control">
          <span>最低分</span>
          <select value={minScore} onChange={(e) => { setMinScore(+e.target.value); load(curDate, +e.target.value) }}>
            {[60, 65, 70, 75, 80].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div className="control">
          <span>位置</span>
          <div className="seg">
            {[['all', '全部'], ['lower', '下沿'], ['middle', '中部'], ['upper', '上沿']].map(([k, l]) => (
              <button key={k} className={zoneFilter === k ? 'on' : ''} onClick={() => setZoneFilter(k)}>{l}</button>
            ))}
          </div>
        </div>
        <div className="control">
          <span>趋势风险</span>
          <div className="seg">
            {[['all', '全部'], ['LOW', '低'], ['MEDIUM', '中'], ['HIGH', '高']].map(([k, l]) => (
              <button key={k} className={riskFilter === k ? 'on' : ''} onClick={() => setRiskFilter(k)}>{l}</button>
            ))}
          </div>
        </div>
        <div className="control">
          <span>状态</span>
          <div className="seg">
            {Object.entries(STATE_LABEL).map(([k, l]) => (
              <button
                key={k}
                className={stateFilter.includes(k) ? 'on' : ''}
                onClick={() =>
                  setStateFilter((prev) =>
                    prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k]
                  )
                }
              >{l}</button>
            ))}
          </div>
        </div>
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn" onClick={doScan} disabled={scanning}>
          {scanning ? '扫描中…' : '立即扫描'}
        </button>
      </div>

      {stats && (
        <div className="stats">
          <div className="stat-card"><div className="label">高质量标的</div><div className="value accent">{stats.total}</div><div className="hint">score ≥ {minScore}</div></div>
          <div className="stat-card"><div className="label">下沿候选</div><div className="value up">{stats.lower}</div><div className="hint">RangePos &lt; 0.2</div></div>
          <div className="stat-card"><div className="label">上沿候选</div><div className="value gold">{stats.upper}</div><div className="hint">RangePos &gt; 0.8</div></div>
          <div className="stat-card"><div className="label">低趋势风险</div><div className="value">{stats.lowRisk}</div><div className="hint">TrendRisk LOW</div></div>
        </div>
      )}

      <div className="table-wrap">
        {loading ? (
          <div className="loading"><div className="spinner" />加载中…</div>
        ) : error ? (
          <div className="error">加载失败: {error}</div>
        ) : filtered.length === 0 ? (
          <div className="empty">当前筛选条件下无标的</div>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>标的</th>
                <th>状态</th>
                <th onClick={() => onSort('range_score')}>分数 {sortKey === 'range_score' ? (sortDir < 0 ? '↓' : '↑') : ''}</th>
                <th>区间</th>
                <th onClick={() => onSort('width')}>宽度</th>
                <th onClick={() => onSort('age')}>Age</th>
                <th onClick={() => onSort('range_pos')}>位置</th>
                <th>RangePos</th>
                <th>反应 S/R</th>
                <th>DI</th>
                <th>NATR</th>
                <th onClick={() => onSort('trend_risk')}>趋势风险</th>
                <th>收盘</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.symbol} onClick={() => openChart(r.symbol)}>
                  <td><span className="sym">{r.name || r.symbol}<span className="code">{r.symbol}</span></span></td>
                  <td><span className={`badge ${STATE_CLASS[r.state]}`}>{STATE_LABEL[r.state] || r.state}</span></td>
                  <td><span className={`score-pill ${r.range_score >= 80 ? 's80' : ''}`}>{fmt(r.range_score, 0)}</span></td>
                  <td className="dim">{fmt(r.lower)} ~ {fmt(r.upper)}</td>
                  <td>{fmt(r.width * 100, 1)}%</td>
                  <td>{r.age != null ? `${r.age}D` : '—'}</td>
                  <td>
                    <span className="posbar">
                      <span className="zone" style={{ left: 0, width: '20%' }} />
                      <span className="zone" style={{ right: 0, width: '20%' }} />
                      <span className="dot" style={{ left: `${Math.min(100, Math.max(0, r.range_pos * 100))}%` }} />
                    </span>
                  </td>
                  <td className={zoneOf(r.range_pos) === 'lower' ? 'up' : zoneOf(r.range_pos) === 'upper' ? 'gold' : 'dim'} style={{ color: zoneOf(r.range_pos) === 'upper' ? 'var(--gold)' : undefined }}>
                    {fmt(r.range_pos)}
                  </td>
                  <td className="dim">{fmt(r.support_response, 1)}/{fmt(r.resistance_response, 1)}</td>
                  <td>{fmt(r.di)}</td>
                  <td className="dim">{fmt(r.natr, 1)}%</td>
                  <td><span className={`risk ${r.trend_risk}`}>{r.trend_risk}</span></td>
                  <td>{fmt(r.close)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      </>
      )}
      </>
      )}

      <div className="footer">
        数据: stock.daily · 震荡+趋势双系统 · 每日 21:30 自动更新 · 点击行查看蜡烛图与详情
      </div>

      {chartSym && (
        <div className="modal-mask" onClick={() => setChartSym(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>{chartData?.state?.symbol === chartSym ? (chartData?.name || chartSym) : chartSym}</h2>
              <span className="code">{chartSym}</span>
              <button className="modal-close" onClick={() => setChartSym(null)}>×</button>
            </div>
            {!chartData ? (
              <div className="loading"><div className="spinner" />加载图表…</div>
            ) : chartData.error ? (
              <div className="error">{chartData.error}</div>
            ) : (
              <>
                <div className="chart-box">
                  <CandleChart candles={chartData.candles} range={chartData.range} />
                </div>
                {chartData.state && (
                  <div className="state-grid">
                    <div className="state-item"><div className="k">状态</div><div className="v">{STATE_LABEL[chartData.state.state] || chartData.state.state}</div></div>
                    <div className="state-item"><div className="k">RangeScore</div><div className="v" style={{ color: 'var(--accent)' }}>{fmt(chartData.state.range_score, 0)}</div></div>
                    <div className="state-item"><div className="k">区间</div><div className="v">{fmt(chartData.state.lower)} ~ {fmt(chartData.state.upper)}</div></div>
                    <div className="state-item"><div className="k">Age</div><div className="v">{chartData.state.age != null ? `${chartData.state.age}D` : '—'}</div></div>
                    <div className="state-item"><div className="k">RangePos</div><div className="v">{fmt(chartData.state.range_pos)}</div></div>
                    <div className="state-item"><div className="k">DI / Flip / AC1</div><div className="v">{fmt(chartData.state.di)} / {fmt(chartData.state.flip_rate)} / {fmt(chartData.state.ac1)}</div></div>
                    <div className="state-item"><div className="k">支撑/压力反应</div><div className="v">{fmt(chartData.state.support_response, 1)} / {fmt(chartData.state.resistance_response, 1)} ATR</div></div>
                    <div className="state-item"><div className="k">TrendRisk</div><div className={`v risk ${chartData.state.trend_risk}`}>{chartData.state.trend_risk}</div></div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
