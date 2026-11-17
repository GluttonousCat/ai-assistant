import { useEffect, useMemo, useState } from 'react'
import { platformApi } from '../platformApi'
import OpportunityBadge from '../OpportunityBadge'
import { opportunityFor } from '../opportunities'
import TrendChart from './TrendChart'
import './tracking.css'

const HORIZONS = [1, 3, 5, 10]
const percent = (value) => value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
const tone = (value) => value == null ? '' : value > 0 ? 'positive' : value < 0 ? 'negative' : ''
const EMPTY = []
const TRACKING = { selection_mode: 'tracking' }

function Sparkline({ path = [] }) {
  const values = path.map((p) => p.return_pct).filter((v) => v != null)
  if (values.length < 2) return <span className="tracking-muted">等待后续走势</span>
  const minimum = Math.min(0, ...values), maximum = Math.max(0, ...values)
  const span = Math.max(maximum - minimum, 1), low = (minimum + maximum - span) / 2
  const y = (value) => 31 - (value - low) / span * 26
  let connected = false
  const line = path.map((p, i) => {
    if (p.return_pct == null) { connected = false; return '' }
    const point = `${connected ? 'L' : 'M'}${(i / (path.length - 1) * 106 + 2).toFixed(1)},${y(p.return_pct).toFixed(1)}`
    connected = true
    return point
  }).join(' ')
  return <svg width="110" height="36" viewBox="0 0 110 36" role="img" aria-label="入选后收盘走势，缺失行情断开显示"><path d={`M0,${y(0)}H110`} stroke="#443e32" strokeDasharray="2 3" /><path d={line} fill="none" stroke={values.at(-1) >= 0 ? '#e87768' : '#55ad92'} strokeWidth="1.8" /></svg>
}

function Horizon({ value }) {
  if (value?.status === 'ready') return <span className={tone(value.return_pct)}>{percent(value.return_pct)}</span>
  return <span className="tracking-muted" title={value?.status === 'missing' ? '已到期，但行情或真实复权因子缺失（含停牌），不顺延到下一根K线' : '尚未到对应交易日'}>{value?.status === 'missing' ? '缺数据' : '未到期'}</span>
}

export default function SignalTracking({ revision }) {
  const [requestedDate, setRequestedDate] = useState('')
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('')
  const [pattern, setPattern] = useState('')
  const [board, setBoard] = useState('')
  const [sort, setSort] = useState('score')
  const [code, setCode] = useState('')
  const [timeframe, setTimeframe] = useState('1D')
  const [chart, setChart] = useState(null)
  const [chartBusy, setChartBusy] = useState(false)
  const [chartError, setChartError] = useState('')
  const [reset, setReset] = useState(0)
  const [focusSignal, setFocusSignal] = useState(0)
  const chartIdentity = report?.run_id && code ? `${report.run_id}:${code}:${timeframe}` : ''
  // Never give a new chart key an old response: its viewport cache would retain
  // a daily-bar position as though it belonged to the new 60F instrument.
  const detail = chart?.identity === chartIdentity ? chart.detail : null

  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError('')
    platformApi.marketTracking(requestedDate, { signal: controller.signal }).then((r) => { if (!controller.signal.aborted) setReport(r) }).catch((e) => {
      if (!controller.signal.aborted) setError(e.message)
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [requestedDate, revision, retry])

  const rows = useMemo(() => (report?.data || []).filter((r) =>
    (!query || `${r.ts_code}${r.name}`.toUpperCase().includes(query.trim().toUpperCase()))
    && (!group || (group === 'entry' ? r.eligible : !r.eligible))
    && (!pattern || opportunityFor(r).key === pattern) && (!board || r.board === board))
    .sort((a, b) => sort === 'score' ? b.score - a.score : (b.follow_up.latest_return_pct ?? -Infinity) - (a.follow_up.latest_return_pct ?? -Infinity)), [report, query, group, pattern, board, sort])
  useEffect(() => { if (!rows.some((r) => r.ts_code === code)) setCode(rows[0]?.ts_code || '') }, [rows, code])
  const selected = rows.find((r) => r.ts_code === code)
  useEffect(() => {
    if (!code || !report?.run_id || busy || error) return
    const controller = new AbortController()
    setChart(null); setChartBusy(true); setChartError('')
    platformApi.marketTrackingChart(code, report.run_id, timeframe, { signal: controller.signal }).then((r) => { if (!controller.signal.aborted) setChart({ identity: chartIdentity, detail: r }) }).catch((e) => {
      if (!controller.signal.aborted) setChartError(e.message)
    }).finally(() => { if (!controller.signal.aborted) setChartBusy(false) })
    return () => controller.abort()
  }, [code, report?.run_id, timeframe, chartIdentity, busy, error, retry])

  return <section className="signal-tracking">
    <div className="tracking-intro"><div><h2>让每一次关注，都有后续</h2><p>回到入选那天，看看信号之后的几天，价格如何演变。</p></div><label>入选日期 <select aria-label="入选日期" disabled={busy} value={requestedDate || report?.signal_date || ''} onChange={(e) => setRequestedDate(e.target.value)}>{!report?.dates?.length && <option value="">暂无历史快照</option>}{report?.dates?.map((d) => <option key={d.run_id} value={d.date}>{d.date}</option>)}</select></label></div>
    {error && <div className="trend-notice error" role="alert">{error} <button className="btn ghost" onClick={() => setRetry((n) => n + 1)}>重试</button></div>}
    {busy ? <div className="trend-placeholder">正在整理历史信号与后续行情…</div> : report?.signal_date && !error ? <>
      <div className="tracking-stats"><div><span>当日候选</span><strong>{report.summary.count}<small>只</small></strong><p>入选时评分 ≥60 · 保留原始状态</p></div><div><span>已有后续行情</span><strong>{report.summary.measured}<small>只</small></strong><p>未到期与缺数据分别显示</p></div><div><span>最新收盘较入选日上涨</span><strong>{report.summary.positive}<small>/ {report.summary.measured}</small></strong><p>价格方向统计，非交易胜率</p></div><div><span>最新收盘平均变化</span><strong className={tone(report.summary.mean_return_pct)}>{percent(report.summary.mean_return_pct)}</strong><p>等权观察值 · 未扣交易成本</p></div></div>
      <div className="tracking-toolbar"><input aria-label="搜索跟踪股票" placeholder="搜索代码 / 名称" value={query} onChange={(e) => setQuery(e.target.value)} /><select aria-label="跟踪候选状态" value={group} onChange={(e) => setGroup(e.target.value)}><option value="">全部状态</option><option value="entry">当时入场候选</option><option value="watch">当时观察候选</option></select><select aria-label="跟踪机会类型" value={pattern} onChange={(e) => setPattern(e.target.value)}><option value="">全部机会</option><option value="trend_pullback">趋势回调</option><option value="base_rebound">底部反转</option></select><select aria-label="跟踪板块" value={board} onChange={(e) => setBoard(e.target.value)}><option value="">全部板块</option>{['主板', '创业板', '科创板', '北交所'].map((b) => <option key={b}>{b}</option>)}</select><select aria-label="跟踪排序" value={sort} onChange={(e) => setSort(e.target.value)}><option value="score">当时评分排序</option><option value="return">最新涨幅排序</option></select><span>{rows.length} 只 · 点击行看K线 · 横向滑动看各周期</span></div>
      <div className="tracking-table-wrap"><table className="tracking-table"><thead><tr><th>股票 / 当时状态</th><th>机会 / 评分</th><th>后续走势</th>{HORIZONS.map((h) => <th key={h}>T+{h}<small>交易日收盘</small></th>)}<th>最新变化<small>实际行情日期</small></th><th>期间最高<small>相对入选日收盘</small></th><th>期间最低<small>相对入选日收盘</small></th></tr></thead><tbody>{rows.map((r) => <tr key={r.ts_code} className={code === r.ts_code ? 'selected' : ''} onClick={() => setCode(r.ts_code)}><td><button className="tracking-stock" onClick={() => setCode(r.ts_code)} aria-pressed={code === r.ts_code}><b>{r.name}</b><small>{r.ts_code} · {r.board}</small></button><span className={`tracking-state ${r.eligible ? 'entry' : ''}`}>{r.eligible ? '入场候选' : '观察候选'}</span></td><td><OpportunityBadge value={r} /><small className="tracking-score">{r.score.toFixed(1)} 分</small></td><td><Sparkline path={r.follow_up.path} /></td>{HORIZONS.map((h) => <td key={h}><Horizon value={r.follow_up.horizons[h]} /></td>)}<td><span className={tone(r.follow_up.latest_return_pct)}>{percent(r.follow_up.latest_return_pct)}</span><small>{r.follow_up.latest_date || '等待行情'}</small></td><td className={tone(r.follow_up.max_up_pct)} title={r.follow_up.issue || ''}>{percent(r.follow_up.max_up_pct)}</td><td className={tone(r.follow_up.max_down_pct)} title={r.follow_up.issue || ''}>{percent(r.follow_up.max_down_pct)}</td></tr>)}</tbody></table>{!rows.length && <div className="trend-placeholder">该日期或筛选条件下没有候选，可切换日期或放宽页面筛选。</div>}</div>
      {selected && <section className="tracking-chart-panel"><header><div><h3>{selected.name} <small>{selected.ts_code}</small></h3><p>{report.signal_date} 入选 · 当时{selected.eligible ? '入场候选' : '观察候选'} · {selected.score.toFixed(1)}分 <OpportunityBadge value={selected} /></p></div><div className="tracking-chart-controls">{['1D', '60F'].map((tf) => <button className={tf === timeframe ? 'active' : ''} key={tf} onClick={() => setTimeframe(tf)}>{tf === '1D' ? '日K' : '60F'}</button>)}<button onClick={() => setFocusSignal((v) => v + 1)}>定位入选日</button><button onClick={() => setReset((v) => v + 1)}>回到最新</button></div></header><div className="tracking-chart-note"><span>↓ 金色标记为入选日 · 右侧为之后的真实走势</span><span>查询上限 {report.as_of?.slice(0, 10)} 收盘</span></div>{selected.follow_up.issue && <div className="trend-notice">{selected.follow_up.issue}</div>}{chartError && <div className="trend-notice error">{chartError}</div>}{detail?.issue && <div className="trend-notice">{detail.issue}，不能据此计算复权收益。</div>}<div className="tracking-chart-canvas"><TrendChart data={detail?.data || EMPTY} stop={detail?.signal_close} stopLabel="入选日收盘" instrumentKey={`tracking:${report.run_id}:${code}:${timeframe}`} config={TRACKING} timeframe={timeframe} resetView={reset} focusSignal={focusSignal} />{chartBusy && <div className="tracking-loading">读取后续K线…</div>}</div><p className="tracking-original">当时的筛选解释：{selected.reason}</p></section>}
      <details className="tracking-method"><summary>统计口径与快照信息</summary><p>从入选日原生收盘价起，按交易所交易日计算T+1/3/5/10，用每个交易日的真实复权因子处理除权；停牌不顺延、不填零。最高/最低为入选后有效日K的高低价相对入选日收盘变化，不是可成交收益或峰值回撤。平均变化只计有后续行情的股票，各股行情截止日见表内。</p><p>读取历史保存的名单，不按当前规则补造旧信号。当天多次发布取最后一版，同股不同日期可重复出现；每个日期是独立观察组，不是去重后的交易组合。历史行情及因子被供应商修订时，复盘价格会随之更新。</p><p>快照版本：{report.version || '旧版本'} · 实际发布：{String(report.published_at || '未知').replace('T', ' ').slice(0, 19)}。历史回填产生的快照不代表当时已可获得该信号。这里只跟踪价格，不模拟买卖、仓位或交易成本。</p></details>
    </> : !error && <div className="trend-placeholder">还没有已发布的筛选快照。每日扫描完成后，这里会自动保留可复盘的日期。</div>}
  </section>
}
