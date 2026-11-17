import { useEffect, useMemo, useState } from 'react'
import { platformApi } from '../platformApi'
import OpportunityBadge from '../OpportunityBadge'
import { opportunityFor } from '../opportunities'
import './intraday.css'

export default function IntradayWatch() {
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [code, setCode] = useState('')
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('')
  const [stage, setStage] = useState('')
  const [image, setImage] = useState(null)
  const [imageError, setImageError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError('')
    platformApi.marketIntraday({ signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted) setReport(r)
    }).catch((e) => { if (!controller.signal.aborted) setError(e.message) })
      .finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [revision])
  const rows = useMemo(() => (report?.data || []).filter((r) =>
    (!query || `${r.ts_code}${r.name}`.toUpperCase().includes(query.trim().toUpperCase()))
    && (!kind || opportunityFor(r).key === kind)
    && (!stage || (stage === 'new' ? r.new_today : !r.new_today))), [report, query, kind, stage])
  useEffect(() => { if (!rows.some((r) => r.ts_code === code)) setCode(rows[0]?.ts_code || '') }, [rows, code])
  const selected = rows.find((r) => r.ts_code === code)
  const identity = selected && report?.run_id ? `${report.run_id}:${report.created_at}:${code}:${revision}` : ''
  useEffect(() => {
    if (!identity) return
    const controller = new AbortController()
    setImageError('')
    platformApi.marketIntradayChart(code, report.run_id, { signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted) setImage({ identity, value: r.image })
    }).catch((e) => { if (!controller.signal.aborted) setImageError(e.message) })
    return () => controller.abort()
  }, [identity, code, report?.run_id])
  const picture = image?.identity === identity ? image.value : null

  return <section className="intraday-watch">
    <header className="intraday-header"><div><h2>午间60F观察</h2><p>固定已完成的上午两根，先观察结构，再等待盘后复核。</p></div><button className="btn ghost" disabled={busy} onClick={() => setRevision((v) => v + 1)}>{busy ? '读取中…' : '刷新观察结果'}</button></header>
    {error && <div className="trend-notice error" role="alert">{error}</div>}
    {busy && !report ? <div className="trend-placeholder">正在读取已发布盘中观察…</div> : !report?.as_of ? <div className="trend-placeholder">{report?.note || '等待离线采集与扫描完成。'}</div> : <>
      <div className="intraday-scope"><b>观察截止 {String(report.as_of).replace('T', ' ').slice(0, 16)}</b><span>仅10:30 / 11:30两根 · 日K背景截至 {String(report.daily_as_of).slice(0, 10)}</span><p>{report.score_scope}。所有记录均为观察，尚未满足完整日K入场确认。</p></div>
      <div className="tracking-stats intraday-stats">
        <div><span>采集范围</span><strong>{report.universe}<small>只</small></strong><p>当前已保存的非ST股票池</p></div>
        <div><span>两根完整可得</span><strong>{report.collected}<small>只</small></strong><p>{report.collection_failed}只获取不完整，未据此推荐</p></div>
        <div><span>盘中观察候选</span><strong>{report.data.length}<small>只</small></strong><p>复用现行60F结构 · 保留风险门槛</p></div>
        <div><span>今天出现新起点</span><strong>{report.data.filter((r) => r.new_today).length}<small>只</small></strong><p>其余为前一阶段结构继续观察</p></div>
      </div>
      <div className="intraday-filters"><input aria-label="搜索盘中观察" placeholder="搜索代码 / 名称" value={query} onChange={(e) => setQuery(e.target.value)} /><select aria-label="盘中机会类型" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">全部机会</option><option value="trend_pullback">趋势回调</option><option value="base_rebound">底部反转</option></select><select aria-label="盘中起点" value={stage} onChange={(e) => setStage(e.target.value)}><option value="">全部起点</option><option value="new">今天新起点</option><option value="continued">延续观察</option></select><span>{rows.length}只 · 免费网页行情快照，非实时推送</span></div>
      <div className="trend-workspace">
        <div className="trend-stock-panel"><div className="trend-list-heading">今日新起点优先 · 盘中观察分</div><div className="trend-stock-list">
          {rows.map((r) => <div key={r.ts_code} className={`trend-stock-row ${r.ts_code === code ? 'selected' : ''}`}><button className="trend-stock-select" onClick={() => setCode(r.ts_code)}><span><b>{r.name}</b><small>{r.ts_code} · {r.board}</small><OpportunityBadge value={r} /><small>{r.new_today ? '今天新起点' : '延续观察'} · 第{r.trend60.age_bars}根</small></span><span><b>{r.score.toFixed(1)}<small>/ 80分</small></b><small>风险 {r.risk_pct.toFixed(2)}%</small></span></button></div>)}
          {!rows.length && <div className="intraday-empty">没有符合本次条件的观察候选。</div>}
        </div></div>
        <div className="trend-detail-panel">
          {selected ? <><header className="intraday-selected"><div><h3>{selected.name} <small>{selected.ts_code}</small></h3><p>{selected.parent_trend_label} · {selected.local_trigger_label}</p></div><OpportunityBadge value={selected} /></header>
            <div className="intraday-two-bars">{selected.today_bars.map((r) => <div key={r.bar_end}><span>{r.bar_end.slice(11, 16)} · 已收盘</span><strong className={r.close >= r.open ? 'up' : 'down'}>{r.close.toFixed(2)}</strong><small>开 {r.open.toFixed(2)} · 高 {r.high.toFixed(2)} · 低 {r.low.toFixed(2)}</small></div>)}<div><span>60F结构参考</span><strong>{selected.stop_price.toFixed(2)}</strong><small>风险距离 {selected.risk_pct.toFixed(2)}% · 非成交指令</small></div></div>
            <p className="intraday-reason">{selected.reason}</p>
            <p className="intraday-chart-note">日K只展示此前完整背景；两图采用今天真实因子的同一前复权基准。</p>
            {imageError ? <div className="trend-notice error">{imageError}</div> : picture ? <img className="intraday-chart-image" src={picture} alt={`${selected.name} 近5年日K背景与今天11:30截止的60F观察`} /> : <div className="trend-placeholder">正在读取本次K线快照…</div>}
          </> : <div className="trend-placeholder">选择一只股票，核对上午两根K线与结构。</div>}
        </div>
      </div>
      <p className="intraday-footnote">结果生成于 {String(report.created_at).replace('T', ' ').slice(0, 19)}。此页读取独立离线结果，未替换盘后正式名单；后续实时价格可能已变化，原17点更新与18点邮件仍按完整收盘数据运行。</p>
    </>}
  </section>
}
