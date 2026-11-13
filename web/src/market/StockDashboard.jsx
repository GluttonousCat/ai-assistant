import { useEffect, useMemo, useState } from 'react'
import { platformApi } from '../platformApi'
import TrendChart from './TrendChart'
import './dashboard.css'

const INDICES = { '000016.SH': '上证50', '000300.SH': '沪深300', '000905.SH': '中证500', '000852.SH': '中证1000', '932000.CSI': '中证2000' }
const number = (v, digits = 2) => v == null ? '—' : Number(v).toFixed(digits)
const money = (v) => v == null ? '—' : v >= 1e8 ? `${(v / 1e8).toFixed(2)}亿` : `${(v / 1e4).toFixed(0)}万`
const dateLabel = (v) => String(v || '—').replace('T', ' ').slice(0, 16)

export default function StockDashboard({ user }) {
  const [snapshot, setSnapshot] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState('')
  const [mode, setMode] = useState('candidate')
  const [sort, setSort] = useState('score')
  const [code, setCode] = useState('')
  const [detail, setDetail] = useState(null)
  const [chartError, setChartError] = useState('')
  const [chartBusy, setChartBusy] = useState(false)
  const [rulesOpen, setRulesOpen] = useState(false)
  const starKey = `alpha:watchlist:${user?.username || user?.id || 'local'}`
  const [stars, setStars] = useState(() => {
    try { const saved = JSON.parse(localStorage.getItem(starKey) || '[]'); return Array.isArray(saved) ? saved : [] } catch { return [] }
  })

  async function load() {
    setBusy(true); setError('')
    try { setSnapshot(await platformApi.marketDashboard()) } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }
  useEffect(() => { let active = true; platformApi.marketDashboard().then((r) => { if (active) setSnapshot(r) }).catch((e) => { if (active) setError(e.message) }).finally(() => { if (active) setBusy(false) }); return () => { active = false } }, [])
  const rows = useMemo(() => {
    const search = query.trim().toUpperCase()
    return (snapshot?.data || []).filter((r) => (!search || `${r.name}${r.ts_code}`.toUpperCase().includes(search))
      && (!index || r.indices.includes(index))
      && (mode === 'all' || mode === 'star' && stars.includes(r.ts_code) || mode === 'candidate' && r.eligible || mode === 'first' && r.state === '首阳观察' || mode === 'observe' && ['趋势观察', '等待启动', '启动已过'].includes(r.state)))
      .sort((a, b) => sort === 'efficiency' ? (b.startup?.efficiency ?? b.metrics?.efficiency ?? 0) - (a.startup?.efficiency ?? a.metrics?.efficiency ?? 0) : sort === 'risk' ? (a.risk_pct ?? 1e9) - (b.risk_pct ?? 1e9) : b.score - a.score || a.ts_code.localeCompare(b.ts_code))
  }, [snapshot, query, index, mode, sort, stars])
  const selected = snapshot?.data.find((r) => r.ts_code === code)
  useEffect(() => {
    if (rows.length && !rows.some((r) => r.ts_code === code)) setCode(rows[0].ts_code)
    if (!rows.length) setCode('')
  }, [rows, code])
  useEffect(() => {
    if (!code) { setDetail(null); return }
    let active = true
    setChartBusy(true); setDetail(null); setChartError('')
    platformApi.marketStock(code, snapshot?.run_id).then((r) => { if (active) setDetail(r) }).catch((e) => { if (active) setChartError(e.message) }).finally(() => { if (active) setChartBusy(false) })
    return () => { active = false }
  }, [code, snapshot?.run_id])
  function toggleStar(value) {
    const next = stars.includes(value) ? stars.filter((s) => s !== value) : [...stars, value]
    setStars(next)
    try { localStorage.setItem(starKey, JSON.stringify(next)) } catch { /* Browsing remains usable if storage is unavailable. */ }
  }
  function exportCsv() {
    const escape = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = [['代码', '名称', '状态', '质量分', '本段首阳时间', '反转前证据', '突破阻力位', '推进阳线数', '启动总根数', '本段效率', 'ER20', 'ADX', '参考止损', '风险距离%', '筛选解释', '策略截止'], ...rows.map((r) => [r.ts_code, r.name, r.state, r.score, r.startup?.start, r.startup?.evidence, r.startup?.neckline, r.startup?.bullish_count, r.startup?.age_bars, r.startup?.efficiency, r.metrics?.efficiency, r.metrics?.adx, r.stop_price, r.risk_pct, r.reason, snapshot.as_of])].map((r) => r.map(escape).join(',')).join('\r\n')
    const url = URL.createObjectURL(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' }))
    const a = document.createElement('a'); a.href = url; a.download = `60F筛选_${snapshot.as_of.slice(0, 10)}.csv`; a.click(); URL.revokeObjectURL(url)
  }
  const meta = snapshot?.metadata
  const earlyMode = meta?.config?.selection_mode === 'early'
  const metrics = selected?.metrics
  const stale = snapshot && (new Date().getTime() - Date.parse(snapshot.as_of.replace(' ', 'T') + '+08:00')) > 4 * 86400e3

  return <div className="trend-board">
    <header className="trend-header">
      <div><div className="trend-eyebrow">ALPHA FINANCE RADAR / MARKET</div><h1>趋势雷达 <span>60F</span></h1><p>以主趋势筛选机会，让每一次关注都有依据。</p></div>
      <div className="trend-header-actions"><span className="trend-offline">● 盘后研究</span><button className="btn ghost" disabled={busy} onClick={load}>{busy ? '读取中…' : '刷新快照'}</button></div>
    </header>
    {error && <div className="trend-notice error" role="alert">{error}<button className="btn ghost" onClick={load}>重试</button></div>}
    {busy && !snapshot && <div className="trend-placeholder">正在读取行情与筛选结果…</div>}
    {snapshot && <>
      <div className="trend-cards">
        <div><span>扫描股票池</span><strong>{meta.universe.toLocaleString()}<small>只</small></strong><p>五指数成分去重</p></div>
        <div className="trend-card-gold"><span>{earlyMode ? '启动第2—4阳候选' : '规则通过'}</span><strong>{meta.eligible}<small>只</small></strong><p>{earlyMode ? `2阳 ${meta.states['启动第2阳'] || 0} · 3阳 ${meta.states['启动第3阳'] || 0} · 4阳 ${meta.states['启动第4阳'] || 0}` : `突破 ${meta.states['突破候选'] || 0} · 回踩 ${meta.states['回踩候选'] || 0}`}</p></div>
        <div><span>{earlyMode ? '首阳观察' : '趋势观察'}</span><strong>{meta.states[earlyMode ? '首阳观察' : '趋势观察'] || 0}<small>只</small></strong><p>{earlyMode ? '启动待确认，不预判后续连阳' : '保留趋势，等待确认'}</p></div>
        <div><span>策略截止 · 上海时间</span><strong className="trend-date">{dateLabel(snapshot.as_of)}</strong><p>行情最新 {dateLabel(meta.data_latest)}</p></div>
      </div>
      {(stale || meta.data_latest.slice(0, 10) !== snapshot.as_of.slice(0, 10)) && <div className="trend-notice">{stale ? '当前为历史快照。' : ''} 复权因子截至 {meta.factor_latest}，策略只使用已完整验证的数据。刷新读取已发布结果；全池计算由离线任务完成。</div>}
      <section className="trend-workspace">
        <aside className="trend-stock-panel">
          <div className="trend-search"><span>⌕</span><input aria-label="搜索股票" placeholder="搜索名称或代码" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
          <select aria-label="指数成分池" value={index} onChange={(e) => setIndex(e.target.value)}><option value="">全部指数成分</option>{Object.entries(INDICES).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select>
          <div className="trend-tabs">{[['candidate', '候选'], ...(earlyMode ? [['first', '首阳']] : []), ['observe', '观察'], ['star', '自选'], ['all', '全部']].map(([key, label]) => <button key={key} className={mode === key ? 'active' : ''} onClick={() => setMode(key)}>{label}{key === 'star' ? ` ${stars.length}` : ''}</button>)}</div>
          <div className="trend-list-heading"><span>{rows.length} 只股票</span><select aria-label="排序" value={sort} onChange={(e) => setSort(e.target.value)}><option value="score">质量分排序</option><option value="efficiency">趋势效率排序</option><option value="risk">风险距离排序</option></select><button onClick={exportCsv} title="导出当前筛选" aria-label="导出当前筛选">↓</button></div>
          <div className="trend-stock-list">
            {rows.slice(0, 250).map((r) => <div className={`trend-stock-row ${code === r.ts_code ? 'selected' : ''}`} key={r.ts_code}>
              <button className="trend-star" aria-label={`${stars.includes(r.ts_code) ? '移出' : '加入'}自选 ${r.name}`} onClick={() => toggleStar(r.ts_code)}>{stars.includes(r.ts_code) ? '★' : '☆'}</button>
              <button className="trend-stock-select" onClick={() => setCode(r.ts_code)}><span><b>{r.name}</b><small>{r.ts_code} · {r.industry}</small></span><span><b className={r.eligible ? 'trend-gold' : ''}>{r.score.toFixed(1)}</b><small className={r.eligible ? 'trend-gold' : ''}>{r.state}</small></span></button>
            </div>)}
            {rows.length > 250 && <p className="trend-list-note">显示前 250 只，请搜索或切换指数缩小范围；导出包含全部筛选结果。</p>}
            {!rows.length && <div className="trend-empty">{mode === 'star' ? '暂无自选，点击股票旁的 ☆ 添加。' : '当前条件下没有股票。可切换“全部”查看过滤原因。'}</div>}
          </div>
        </aside>
        <div className="trend-detail-panel">
          {selected ? <>
            <div className="trend-stock-title"><div><h2>{selected.name} <small>{selected.ts_code}</small></h2><p>{selected.indices.map((i) => INDICES[i] || i).join(' / ')} <span>· 成分快照 {selected.membership_date}</span></p></div><span className={`trend-state ${selected.eligible ? 'passed' : ''}`}>{selected.state}</span></div>
            {earlyMode && selected.startup?.start && <p className="trend-startup-info">本段首阳 {dateLabel(selected.startup.start)} · 已推进 {selected.startup.bullish_count} 根阳线 / 共 {selected.startup.age_bars} 根K线</p>}
            {earlyMode && meta.config.require_precursor && selected.startup?.start && <p className="trend-startup-info">首阳前证据：{selected.startup.evidence || '无有效结构'} · 当时阻力位 {number(selected.startup.neckline)}</p>}
            <div className="trend-metrics"><div><span>截止价</span><b>{number(metrics?.close)}</b></div><div><span>{earlyMode ? '本段推进效率' : '趋势效率 ER'}</span><b>{number(earlyMode ? selected.startup?.efficiency : metrics?.efficiency)}</b></div><div><span>{earlyMode ? '启动进度' : '趋势强度 ADX'}</span><b>{earlyMode ? selected.startup?.bullish_count ? `第 ${selected.startup.bullish_count} 阳` : '等待启动' : number(metrics?.adx, 1)}</b></div><div><span>20日日均成交额</span><b>{money(metrics?.daily_amount)}</b></div><div><span>参考止损 / 距离</span><b>{number(selected.stop_price)} <small>{number(selected.risk_pct)}%</small></b></div></div>
            <div className="trend-chart-shell">
              {chartBusy ? <div className="trend-placeholder">加载 {selected.name} 的60F K线…</div> : chartError ? <div className="trend-placeholder error" role="alert">{chartError}</div> : detail?.data.length ? <TrendChart data={detail.data} stop={selected.stop_price} config={meta.config} /> : <div className="trend-placeholder">没有可用的60F K线</div>}
            </div>
            <div className="trend-chart-foot"><span>60分钟 · {detail?.price_basis === 'forward_asof' ? '按截止日复权' : '原始价格'} · 成交量：股</span><span>滚轮缩放 · 拖动平移 · 十字光标查看</span></div>
            {detail?.issue && <div className="trend-notice">{detail.issue}，展示原始行情，不据此生成趋势候选。</div>}
            <div className="trend-check-title"><h3>为什么{selected.eligible ? '入选' : '未入选'}</h3><span>质量分用于排序，不代表胜率</span></div>
            {selected.checks?.length ? <div className="trend-checks">{selected.checks.map((c) => <div key={c.key} className={c.passed ? 'passed' : ''}><span>{c.passed ? '✓' : '−'}</span><div><b>{c.label}</b><small>{c.rule}</small></div></div>)}</div> : <div className="trend-notice">{selected.reason}</div>}
          </> : <div className="trend-placeholder">从左侧选择一只股票，查看趋势与筛选依据。</div>}
        </div>
      </section>
      <section className="trend-method"><button aria-expanded={rulesOpen} onClick={() => setRulesOpen(!rulesOpen)}><span>策略说明 <small>{snapshot.version} · 视频理念 → 可解释规则</small></span><span>{rulesOpen ? '−' : '+'}</span></button>{rulesOpen && <div>
        <p>{earlyMode ? meta.config.require_precursor ? '日线过滤大方向 → 前面先守住低点，并有低点抬高、波动收敛、抛压减弱、底部盘整中的至少两项证据 → 突破短期阻力的有效转强阳线进入观察 → 第2—4根推进确认。首阳指第一个有效结构信号，不保证是最低点后的第一根阳线。' : '日线过滤大方向 → 回调或盘整后的首阳观察 → 第2—4根推进阳线确认 → 同周期结构与ATR控制风险。短暂整理不重置计数，超过早期窗口不再作为新启动推荐。' : '日线确认大方向 → 60F确认主趋势 → ER与ADX过滤杂乱走势 → 放量突破或回踩转强 → 用同周期ATR与结构低点设置参考风险边界。'}</p>
        <p>视频原例为期货。当前实现是面向A股做多的研究策略，未将“主力资金推动”当作可直接观测的事实；流畅度仅用价格路径效率和趋势强度近似。</p>
        <p>这里只使用60F与已完成日线，15F/30F精细择时尚未接入。参数是研究假设，尚未验证收益；成分股为已保存的日期快照，不是完整历史成员序列。A股实际执行需考虑T+1、涨跌停、滑点及交易费用。</p>
        <p>当前参数：EMA {meta.config.fast}/{meta.config.slow}，{earlyMode ? `首阳实体≥${meta.config.startup_body_atr}ATR、量比≥${meta.config.startup_volume_ratio}，本段效率≥${meta.config.startup_min_efficiency}；不强等ADX/20根ER确认` : `ER≥${meta.config.min_efficiency}，ADX≥${meta.config.min_adx}，突破量比≥${meta.config.min_volume_ratio}`}，偏离≤{meta.config.max_extension_atr}ATR，风险距离≤{meta.config.max_risk_pct}%。已记录 {meta.states['数据待补'] || 0} 只数据待补。</p>
      </div>}</section>
    </>}
  </div>
}
