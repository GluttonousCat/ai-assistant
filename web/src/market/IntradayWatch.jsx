import { useEffect, useMemo, useState } from 'react'
import { platformApi } from '../platformApi'
import { startPolling } from '../lib/polling'
import OpportunityBadge from '../OpportunityBadge'
import IndustryLabel from './IndustryLabel'
import IntradayFollowUp from './IntradayFollowUp'
import { compareStockScores, stockSearchText, meetsDisplayScore, MIN_DISPLAY_SCORE } from './stockList'
import { opportunityFor } from '../opportunities'
import TrendChart from './TrendChart'
import './intraday.css'

const EMPTY = []
const OBSERVATION = { selection_mode: 'tracking' }
const SLOTS = ['10:30', '11:30', '14:00', '15:00']

export default function IntradayWatch({ industries }) {
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [code, setCode] = useState('')
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('')
  const [stage, setStage] = useState('new')
  const [chart, setChart] = useState(null)
  const [chartError, setChartError] = useState('')
  const [timeframe, setTimeframe] = useState('60F')
  const [reset, setReset] = useState(0)
  const [focus, setFocus] = useState(0)
  useEffect(() => {
    setBusy(true)
    return startPolling(async (signal) => {
      try {
        const next = await platformApi.marketIntraday({ signal })
        if (!signal.aborted) {
          setReport((previous) => previous?.run_id === next.run_id && previous?.created_at === next.created_at
            && Boolean(previous?.follow_up) === Boolean(next.follow_up)
            && JSON.stringify(previous?.schedule) === JSON.stringify(next.schedule) ? previous : next)
          setError('')
        }
      } finally { if (!signal.aborted) setBusy(false) }
    }, { pauseWhenHidden: true, onError: (error) => setError(error.message) })
  }, [revision])
  const candidates = useMemo(() => (report?.data || []).filter(meetsDisplayScore), [report])
  const rows = useMemo(() => candidates.filter((r) =>
    (!query || stockSearchText(r, industries).includes(query.trim().toUpperCase()))
    && (!kind || opportunityFor(r).key === kind)
    && (!stage || (stage === 'new' ? r.new_today : !r.new_today))).sort(compareStockScores), [candidates, query, kind, stage, industries])
  useEffect(() => { if (!rows.some((r) => r.ts_code === code)) setCode(rows[0]?.ts_code || '') }, [rows, code])
  const selected = rows.find((r) => r.ts_code === code)
  const identity = selected && report?.run_id ? `${report.run_id}:${report.created_at}:${code}:${timeframe}:${revision}` : ''
  useEffect(() => {
    if (!identity) return
    const controller = new AbortController()
    setChartError('')
    platformApi.marketIntradayChart(code, report.run_id, timeframe, { signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted) setChart({ identity, value: r })
    }).catch((e) => { if (!controller.signal.aborted) setChartError(e.message) })
    return () => controller.abort()
  }, [identity, code, report?.run_id, timeframe])
  const detail = chart?.identity === identity ? chart.value : null
  const slots = report?.slots || SLOTS.filter((slot) => slot <= String(report?.as_of).slice(11, 16))
  const job = report?.schedule
  const working = ['collecting', 'scanning'].includes(job?.status)
  const progress = job?.progress

  return <section className="intraday-watch">
    <header className="intraday-header"><div><h2>盘中60F观察</h2><p>交易日四次更新已收盘60F，观察结构演变，等待盘后复核。</p></div><button className="btn ghost" disabled={busy} onClick={() => setRevision((v) => v + 1)}>{busy ? '读取中…' : '刷新观察结果'}</button></header>
    <div className="intraday-sessions" aria-label="60F自动更新时间">{SLOTS.map((slot) => <span key={slot} className={slots.includes(slot) ? 'complete' : ''}><b>{slot}</b><small>收盘</small></span>)}<p>收盘2分钟后开始更新 · 页面每分钟检查新结果</p></div>
    {working && <div className="intraday-job" role="status">正在{job.status === 'collecting' ? '采集' : '计算'} {job.run_id.replace('_', ' · ')}{progress?.total ? `：${progress.completed} / ${progress.total}只` : '…'}。完成后自动显示新名单。</div>}
    {job?.status === 'error' && <div className="trend-notice" role="status">本轮更新尚未完成，{job.attempts >= 3 ? '已达本轮重试上限，将等待下一时点' : '后台保留断点重试'}。下方仍为最近一次成功结果。</div>}
    {error && <div className="trend-notice error" role="alert">{error}</div>}
    {busy && !report ? <div className="trend-placeholder">正在读取已发布盘中观察…</div> : !report?.as_of ? <div className="trend-placeholder">{report?.note || '等待离线采集与扫描完成。'}</div> : <>
      <div className="intraday-scope"><b>观察截止 {String(report.as_of).replace('T', ' ').slice(0, 16)}</b><span>今日已收盘{slots.length}根 · 日K背景截至 {String(report.daily_as_of).slice(0, 10)}</span><p>{report.score_scope}。所有记录均为观察，尚未满足完整日K入场确认。</p></div>
      <IntradayFollowUp rows={report.follow_up} industries={industries} />
      <h3 className="intraday-section-title">盘中观察信号</h3>
      <div className="tracking-stats intraday-stats">
        <div><span>采集范围</span><strong>{report.universe}<small>只</small></strong><p>当前已保存的非ST股票池</p></div>
        <div><span>已完成前缀可得</span><strong>{report.collected}<small>只</small></strong><p>{report.collection_failed}只获取不完整，未据此推荐</p></div>
        <div><span>盘中观察候选 · ≥{MIN_DISPLAY_SCORE}分</span><strong>{candidates.length}<small>只</small></strong><p>复用现行60F结构 · 保留风险门槛</p></div>
        <div><span>今天出现新起点</span><strong>{candidates.filter((r) => r.new_today).length}<small>只</small></strong><p>其余为前一阶段结构继续观察</p></div>
      </div>
      <div className="intraday-filters"><input aria-label="搜索盘中观察" placeholder="名称 / 代码 / 二级行业" value={query} onChange={(e) => setQuery(e.target.value)} /><select aria-label="盘中机会类型" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">全部机会</option><option value="trend_pullback">趋势回调</option><option value="base_rebound">底部反转</option></select><select aria-label="盘中起点" value={stage} onChange={(e) => setStage(e.target.value)}><option value="">全部起点</option><option value="new">今天新起点</option><option value="continued">延续观察</option></select><span>{rows.length}只 · 免费网页行情快照，非实时推送</span></div>
      <div className="trend-workspace">
        <div className="trend-stock-panel"><div className="trend-list-heading">评分 ≥{MIN_DISPLAY_SCORE} · 从高到低</div><div className="trend-stock-list">
          {rows.map((r) => <div key={r.ts_code} className={`trend-stock-row ${r.ts_code === code ? 'selected' : ''}`}><button className="trend-stock-select" onClick={() => setCode(r.ts_code)}><span><span className="stock-name-line"><b>{r.name}</b><IndustryLabel code={r.ts_code} industries={industries} /></span><small>{r.ts_code} · {r.board}</small><OpportunityBadge value={r} /><small>{r.new_today ? '今天新起点' : '延续观察'} · 第{r.trend60.age_bars}根</small></span><span><b>{r.score.toFixed(1)}<small>/ 80分</small></b><small>风险 {r.risk_pct.toFixed(2)}%</small></span></button></div>)}
          {!rows.length && <div className="intraday-empty">没有符合本次条件的观察候选。</div>}
        </div></div>
        <div className="trend-detail-panel">
          {selected ? <><header className="intraday-selected"><div><h3>{selected.name} <small>{selected.ts_code}</small><IndustryLabel code={selected.ts_code} industries={industries} /></h3><p>{selected.parent_trend_label} · {selected.local_trigger_label}</p></div><OpportunityBadge value={selected} /></header>
            <div className="intraday-two-bars">{selected.today_bars.map((r) => <div key={r.bar_end}><span>{r.bar_end.slice(11, 16)} · 已收盘</span><strong className={r.close >= r.open ? 'up' : 'down'}>{r.close.toFixed(2)}</strong><small>开 {r.open.toFixed(2)} · 高 {r.high.toFixed(2)} · 低 {r.low.toFixed(2)}</small></div>)}<div><span>60F结构参考</span><strong>{selected.stop_price.toFixed(2)}</strong><small>风险距离 {selected.risk_pct.toFixed(2)}% · 非成交指令</small></div></div>
            <p className="intraday-reason">{selected.reason}</p>
            <div className="trend-chart-toolbar"><div className="trend-chart-period" role="group" aria-label="盘中图表周期"><button className={timeframe === '60F' ? 'on' : ''} onClick={() => setTimeframe('60F')}>60F</button><button className={timeframe === '1D' ? 'on' : ''} onClick={() => setTimeframe('1D')}>日K</button></div><button className="trend-chart-reset" onClick={() => setReset((v) => v + 1)}>查看最近</button><button className="trend-chart-reset" onClick={() => setFocus((v) => v + 1)}>定位结构起点</button><span>拖动平移 · 滚轮缩放 · 触屏双指缩放</span></div>
            <p className="intraday-chart-note">{timeframe === '1D' ? '近5年完整日K背景' : `60F截至 ${String(report.as_of).slice(0, 16)}`} · 同一真实前复权基准 · 金色标记为60F结构起点，非入场确认。</p>
            <div className="intraday-chart-canvas trend-chart-viewport">
              <TrendChart data={detail?.data || EMPTY} instrumentKey={`${report.run_id}:${code}:${timeframe}`} timeframe={timeframe} config={OBSERVATION} stop={detail ? selected.stop_price : null} stopLabel="60F结构参考" referenceLabel="结构起点" resetView={reset} focusSignal={focus} />
              {!detail && !chartError && <div className="trend-chart-overlay"><span className="trend-chart-spinner" />正在读取本次K线快照…</div>}
              {(chartError || detail?.issue) && <div className="trend-chart-overlay" role="alert">{chartError || detail.issue}<button className="btn ghost" onClick={() => setRevision((v) => v + 1)}>重新读取</button></div>}
            </div>
          </> : <div className="trend-placeholder">选择一只股票，核对已收盘K线与结构。</div>}
        </div>
      </div>
      <p className="intraday-footnote">结果生成于 {String(report.created_at).replace('T', ' ').slice(0, 19)}。此页读取独立离线结果，未替换盘后正式名单；后续实时价格可能已变化，原17点更新与18点邮件仍按完整收盘数据运行。</p>
    </>}
  </section>
}
