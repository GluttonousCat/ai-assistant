import IndustryLabel from './IndustryLabel'
import { stockSearchText } from './stockList'
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

const rowKey = (row) => row.episode_id || row.ts_code

function TrackingRow({ row, selected, onSelect, daily, signalDate, industries }) {
  const result = row.follow_up
  return <tr className={selected ? 'selected' : ''} onClick={onSelect}>
    <td><button className="tracking-stock" onClick={onSelect} aria-pressed={selected}><span className="stock-name-line"><b>{row.name}</b><IndustryLabel code={row.ts_code} industries={industries} /></span><small>{row.ts_code} · {row.board}</small></button>
      <span className={`tracking-state ${row.eligible || row.ever_eligible ? 'entry' : ''}`}>{row.eligible ? '初次入场候选' : row.ever_eligible ? '观察 → 已确认' : '观察候选'}</span>
      {!daily && <small>{row.in_latest_scan ? '最新扫描仍入选' : '已离开候选 · 继续复盘'}</small>}
    </td>
    <td><b>{row.first_seen || signalDate}</b>
      {daily ? <small>单日原始快照</small> : <><small>最近 {row.last_seen} · {row.age_sessions}个交易日</small><small>{row.round_reason}</small><details className="tracking-history" onClick={(e) => e.stopPropagation()}>
        <summary>出现 {row.seen_count} 次 · 每日记录</summary>
        <ul>{row.observations.map((item) => <li key={item.date} title={item.reason}><b>{item.date}</b>　{Number(item.score).toFixed(1)}分<br />{item.eligible ? '入场候选' : '观察候选'} · {item.state}</li>)}</ul>
      </details>{row.confirmed_at && <small>确认 {row.confirmed_at}</small>}</>}
    </td>
    <td><OpportunityBadge value={row} /><small className="tracking-score">首见 {Number(row.score).toFixed(1)} 分</small></td>
    <td><Sparkline path={result.path} /></td>
    {HORIZONS.map((h) => <td key={h}><Horizon value={result.horizons[h]} /></td>)}
    <td><span className={tone(result.latest_return_pct)}>{percent(result.latest_return_pct)}</span><small>{result.latest_date || '等待行情'}</small></td>
    <td className={tone(result.max_up_pct)} title={result.issue || ''}>{percent(result.max_up_pct)}</td>
    <td className={tone(result.max_down_pct)} title={result.issue || ''}>{percent(result.max_down_pct)}</td>
  </tr>
}

export default function SignalTracking({ revision, industries }) {
  const [view, setView] = useState('active')
  const [requestedDate, setRequestedDate] = useState('')
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('')
  const [pattern, setPattern] = useState('')
  const [board, setBoard] = useState('')
  const [sort, setSort] = useState('recent')
  const [selection, setSelection] = useState('')
  const [timeframe, setTimeframe] = useState('1D')
  const [chart, setChart] = useState(null)
  const [chartBusy, setChartBusy] = useState(false)
  const [chartError, setChartError] = useState('')
  const [reset, setReset] = useState(0)
  const [focusSignal, setFocusSignal] = useState(0)
  const daily = view === 'daily'
  const loading = busy || report?.view !== view

  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError('')
    const request = daily ? platformApi.marketTracking(requestedDate, { signal: controller.signal }) : platformApi.marketTrackingEpisodes(view, { signal: controller.signal })
    request.then((r) => { if (!controller.signal.aborted) setReport({ ...r, view }) }).catch((e) => {
      if (!controller.signal.aborted) setError(e.message)
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [requestedDate, view, daily, revision, retry])

  const rows = useMemo(() => (report?.data || []).filter((r) =>
    (!query || stockSearchText(r, industries).includes(query.trim().toUpperCase()))
    && (!group || (group === 'entry' ? (daily ? r.eligible : r.ever_eligible) : !(daily ? r.eligible : r.ever_eligible)))
    && (!pattern || opportunityFor(r).key === pattern) && (!board || r.board === board))
    .sort((a, b) => sort === 'return' ? (b.follow_up.latest_return_pct ?? -Infinity) - (a.follow_up.latest_return_pct ?? -Infinity)
      : sort === 'recent' ? String(b.first_seen || '').localeCompare(a.first_seen || '') || b.score - a.score : b.score - a.score), [report, query, group, pattern, board, sort, daily, industries])
  useEffect(() => { if (!rows.some((r) => rowKey(r) === selection)) setSelection(rows[0] ? rowKey(rows[0]) : '') }, [rows, selection])
  const selected = rows.find((r) => rowKey(r) === selection)
  const code = selected?.ts_code
  const runId = selected?.first_run_id || report?.run_id
  const chartIdentity = runId && code ? `${runId}:${code}:${timeframe}` : ''
  // Keep an old response out of a new instrument's viewport cache.
  const detail = chart?.identity === chartIdentity ? chart.detail : null
  useEffect(() => {
    if (!code || !runId || loading || error) return
    const controller = new AbortController()
    setChart(null); setChartBusy(true); setChartError('')
    platformApi.marketTrackingChart(code, runId, timeframe, { signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted) setChart({ identity: chartIdentity, detail: r })
    }).catch((e) => { if (!controller.signal.aborted) setChartError(e.message) })
      .finally(() => { if (!controller.signal.aborted) setChartBusy(false) })
    return () => controller.abort()
  }, [code, runId, timeframe, chartIdentity, loading, error, retry])

  return <section className="signal-tracking">
    <div className="tracking-intro"><div><h2>信号留下，后续看清</h2><p>连续入选合成一轮，价格表现从本轮首次记录起计算。</p></div>
      {daily && <label>入选日期 <select aria-label="入选日期" disabled={loading} value={requestedDate || report?.signal_date || ''} onChange={(e) => setRequestedDate(e.target.value)}>
        {!report?.dates?.length && <option value="">暂无历史快照</option>}{report?.dates?.map((d) => <option key={d.run_id} value={d.date}>{d.date}</option>)}
      </select></label>}
    </div>
    <div className="tracking-view-tabs" role="group" aria-label="信号跟踪视图">
      {[['active', '跟踪中'], ['archive', '已归档'], ['daily', '按日复盘']].map(([key, label]) => <button key={key} className={view === key ? 'active' : ''} aria-pressed={view === key} onClick={() => setView(key)}>{label}</button>)}
      {!daily && report?.merged_occurrences != null && <span>近 {report.scan_days} 个扫描日 · 合并 {report.merged_occurrences} 条连续重复记录</span>}
    </div>
    {error && <div className="trend-notice error" role="alert">{error} <button className="btn ghost" onClick={() => setRetry((n) => n + 1)}>重试</button></div>}
    {loading && !error ? <div className="trend-placeholder">正在整理机会轮次与后续行情…</div> : report?.summary && !error ? <>
      <div className="tracking-stats">
        <div><span>{daily ? '当日候选' : view === 'active' ? '跟踪中的机会' : '已归档机会'}</span><strong>{report.summary.count}<small>{daily ? '只' : '轮'}</small></strong><p>{daily ? '当时评分 ≥60 · 保留原始状态' : `${report.summary.stocks}只股票 · 基准不随每日刷新重置`}</p></div>
        <div><span>已有后续行情</span><strong>{report.summary.measured}<small>{daily ? '只' : '轮'}</small></strong><p>未到期与缺数据分别显示</p></div>
        <div><span>最新收盘较首见日上涨</span><strong>{report.summary.positive}<small>/ {report.summary.measured}</small></strong><p>价格方向统计，非交易胜率</p></div>
        <div><span>最新收盘平均变化</span><strong className={tone(report.summary.mean_return_pct)}>{percent(report.summary.mean_return_pct)}</strong><p>不同观察期 · 非组合收益</p></div>
      </div>
      <div className="tracking-toolbar">
        <input aria-label="搜索跟踪股票" placeholder="名称 / 代码 / 二级行业" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select aria-label="跟踪候选状态" value={group} onChange={(e) => setGroup(e.target.value)}><option value="">全部状态</option><option value="entry">{daily ? '当时入场候选' : '本轮曾确认入场'}</option><option value="watch">{daily ? '当时观察候选' : '本轮仍为观察'}</option></select>
        <select aria-label="跟踪机会类型" value={pattern} onChange={(e) => setPattern(e.target.value)}><option value="">全部机会</option><option value="trend_pullback">趋势回调</option><option value="base_rebound">底部反转</option></select>
        <select aria-label="跟踪板块" value={board} onChange={(e) => setBoard(e.target.value)}><option value="">全部板块</option>{['主板', '创业板', '科创板', '北交所'].map((b) => <option key={b}>{b}</option>)}</select>
        <select aria-label="跟踪排序" value={sort} onChange={(e) => setSort(e.target.value)}><option value="recent">最近发现</option><option value="score">首见评分排序</option><option value="return">最新涨幅排序</option></select>
        <span>{rows.length} {daily ? '只' : '轮'} · 点击行看K线 · 横向滑动看各周期</span>
      </div>
      <div className="tracking-table-wrap"><table className="tracking-table"><thead><tr>
        <th>股票 / 本轮状态</th><th>{daily ? '入选日期' : '首见 / 最近入选'}</th><th>机会 / 首见评分</th><th>后续走势</th>
        {HORIZONS.map((h) => <th key={h}>T+{h}<small>交易日收盘</small></th>)}<th>最新变化<small>实际行情日期</small></th><th>期间最高<small>相对首见日收盘</small></th><th>期间最低<small>相对首见日收盘</small></th>
      </tr></thead><tbody>{rows.map((r) => <TrackingRow key={rowKey(r)} row={r} selected={selection === rowKey(r)} onSelect={() => setSelection(rowKey(r))} daily={daily} signalDate={report.signal_date} industries={industries} />)}</tbody></table>
        {!rows.length && <div className="trend-placeholder">{view === 'archive' ? '满20个交易日的机会会自动归档；当前没有匹配记录。' : '没有匹配记录，可切换视图或调整筛选条件。'}</div>}
      </div>
      {selected && <section className="tracking-chart-panel">
        <header><div><h3>{selected.name} <small>{selected.ts_code}</small><IndustryLabel code={selected.ts_code} industries={industries} /></h3><p>{selected.first_seen || report.signal_date} 首见 · 首次{selected.eligible ? '入场候选' : '观察候选'} · {Number(selected.score).toFixed(1)}分 <OpportunityBadge value={selected} /></p></div>
          <div className="tracking-chart-controls">{['1D', '60F'].map((tf) => <button className={tf === timeframe ? 'active' : ''} key={tf} onClick={() => setTimeframe(tf)}>{tf === '1D' ? '日K' : '60F'}</button>)}<button onClick={() => setFocusSignal((v) => v + 1)}>定位入选日</button><button onClick={() => setReset((v) => v + 1)}>回到最新</button></div>
        </header>
        <div className="tracking-chart-note"><span>↓ 金色标记为本轮首见日 · 日K支持近5年与近期缩放</span><span>查询上限 {report.as_of?.slice(0, 10)} 收盘</span></div>
        {selected.follow_up.issue && <div className="trend-notice">{selected.follow_up.issue}</div>}
        {chartError && <div className="trend-notice error">{chartError}</div>}
        {detail?.issue && <div className="trend-notice">{detail.issue}，不能据此计算复权收益。</div>}
        <div className="tracking-chart-canvas"><TrendChart data={detail?.data || EMPTY} stop={detail?.signal_close} stopLabel="首见日收盘" instrumentKey={`tracking:${chartIdentity}`} config={TRACKING} timeframe={timeframe} resetView={reset} focusSignal={focusSignal} />{chartBusy && <div className="tracking-loading">读取历史与后续K线…</div>}</div>
        <p className="tracking-original">首见时的筛选解释：{selected.reason}</p>
      </section>}
      <details className="tracking-method"><summary>合并规则、统计口径与快照信息</summary>
        <p>同一股票、策略版本/参数、60F起点在连续已有扫描日入选，合为一轮。观察升级为入场保留在本轮，不重置基准；起点或策略改变、已有扫描日中断后重新入选，会新建一轮。缺失整个扫描日不会臆造退出。</p>
        <p>默认查看近120个扫描日中未满20个交易日的轮次；满20日自动归档，离开候选的记录仍跟踪，避免只留下持续上涨的股票。窗口首日已有的候选只是窗口内首见。展开每日记录可核对原始状态，按日复盘保留每日完整名单。</p>
        <p>价格从本轮首见日收盘起，以交易所日历及真实复权因子计算T+1/3/5/10；停牌不顺延、不填零。期间最高/最低为后续有效日K高低价相对首见日收盘的变化，不是可成交收益或峰值回撤。各轮起点和实际报价截止可能不同，平均值不代表组合收益。</p>
        <p>只读取历史保存的名单；历史行情修订时复盘价格可以更新。日K展示近5年实际可用历史，上市不足5年不补造旧K线；显示区间扩展不重算旧信号。当前所选版本：{selected?.source_version || report.version || '—'}，实际首次发布：{String(selected?.first_published_at || report.published_at || '未知').replace('T', ' ').slice(0, 19)}。回填快照不代表当时已可取得信号。</p>
      </details>
    </> : !error && <div className="trend-placeholder">暂无已发布快照，扫描完成后会自动出现跟踪记录。</div>}
  </section>
}
