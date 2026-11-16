import { useEffect, useMemo, useState } from 'react'
import { platformApi } from '../platformApi'
import TrendChart from './TrendChart'
import useStockChart from './useStockChart'
import OpportunityBadge from '../OpportunityBadge'
import { opportunityFor } from '../opportunities'
import './dashboard.css'

const INDICES = { '000016.SH': '上证50', '000300.SH': '沪深300', '000905.SH': '中证500', '000852.SH': '中证1000', '932000.CSI': '中证2000' }
const number = (v, digits = 2) => v == null ? '—' : Number(v).toFixed(digits)
const money = (v) => v == null ? '—' : v >= 1e8 ? `${(v / 1e8).toFixed(2)}亿` : `${(v / 1e4).toFixed(0)}万`
const dateLabel = (v) => String(v || '—').replace('T', ' ').slice(0, 16)
const EMPTY_BARS = []

export default function StockDashboard({ user }) {
  const [snapshot, setSnapshot] = useState(null)
  const mtfMode = snapshot?.metadata?.config?.selection_mode === 'mtf'
  const riskMode = !!snapshot?.metadata?.config?.risk_setups
  const freshMode = !!snapshot?.metadata?.config?.fresh_reversal
  const [board, setBoard] = useState('')
  const SCREEN_TIMEFRAME = mtfMode ? '1D' : '60F'
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState('')
  const [selectedChartTimeframe, setChartTimeframe] = useState('1D')
  const chartTimeframe = snapshot && !snapshot.metadata?.timeframes?.['1D'] ? '60F' : selectedChartTimeframe
  const [pattern, setPattern] = useState('')
  const [mode, setMode] = useState('candidate')
  const [sort, setSort] = useState('score')
  const [code, setCode] = useState('')
  const [chartRevision, setChartRevision] = useState(0)
  const [resetChartView, setResetChartView] = useState(0)
  const { detail, error: chartError, busy: chartBusy } = useStockChart(code, snapshot?.run_id, chartTimeframe, chartRevision)
  const [rulesOpen, setRulesOpen] = useState(false)
  const starKey = `alpha:watchlist:${user?.username || user?.id || 'local'}`
  const [stars, setStars] = useState(() => {
    try { const saved = JSON.parse(localStorage.getItem(starKey) || '[]'); return Array.isArray(saved) ? saved : [] } catch { return [] }
  })

  async function load() {
    setBusy(true); setError('')
    try { setSnapshot(await platformApi.marketDashboard()); setChartRevision((v) => v + 1) } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }
  useEffect(() => { let active = true; platformApi.marketDashboard().then((r) => { if (active) setSnapshot(r) }).catch((e) => { if (active) setError(e.message) }).finally(() => { if (active) setBusy(false) }); return () => { active = false } }, [])
  const rows = useMemo(() => {
    const search = query.trim().toUpperCase()
    return (snapshot?.data || []).filter((r) => (!search || `${r.name}${r.ts_code}`.toUpperCase().includes(search))
      && (!index || r.indices.includes(index))
      && (!board || r.board === board)
      && (!pattern || opportunityFor(r).key === pattern)
      && (mode === 'all' || mode === 'star' && stars.includes(r.ts_code) || mode === 'candidate' && (riskMode ? r.setup_watch : freshMode ? r.trend60?.fresh : r.eligible) || mode === 'entry' && r.eligible || mode === 'first' && r.state === '首阳观察' || mode === 'resume' && r.state === '回调再起观察' || mode === 'observe' && ['首次回调观察', '早期反转观察', ...(freshMode ? [] : ['等待日K确认', '60F趋势未确认']), '回调再起观察', '趋势观察', '等待启动', '等待形态', '启动已过'].includes(r.state)))
      .sort((a, b) => sort === 'efficiency' && freshMode ? (a.trend60?.age_bars ?? 1e9) - (b.trend60?.age_bars ?? 1e9) : sort === 'efficiency' ? (b.startup?.efficiency ?? b.metrics?.efficiency ?? 0) - (a.startup?.efficiency ?? a.metrics?.efficiency ?? 0) : sort === 'risk' ? (a.risk_pct ?? 1e9) - (b.risk_pct ?? 1e9) : b.score - a.score || a.ts_code.localeCompare(b.ts_code))
  }, [snapshot, query, index, mode, sort, stars, pattern, board, freshMode, riskMode])
  const selected = rows.find((r) => r.ts_code === code)
  useEffect(() => {
    if (rows.length && !rows.some((r) => r.ts_code === code)) setCode(rows[0].ts_code)
    if (!rows.length) setCode('')
  }, [rows, code])
  function toggleStar(value) {
    const next = stars.includes(value) ? stars.filter((s) => s !== value) : [...stars, value]
    setStars(next)
    try { localStorage.setItem(starKey, JSON.stringify(next)) } catch { /* Browsing remains usable if storage is unavailable. */ }
  }
  function exportCsv() {
    const escape = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = [['代码', '名称', '板块', '60F原始起点', '反转根数', '距起点ATR', '入场周期', '机会分类', '机会类型', '状态', '质量分', '60F趋势背景', '本轮转强时间', '前次起势背景时间', '结构证据', '收复位', '推进阳线数', '启动总根数', '本段效率', 'ER20', 'ADX', '参考止损', '风险距离%', '筛选解释', '策略截止'], ...rows.map((r) => [r.ts_code, r.name, r.board, r.trend60?.origin, r.trend60?.age_bars, r.trend60?.gain_atr, SCREEN_TIMEFRAME, opportunityFor(r).label, r.pattern_label, r.state, r.score, r.trend60?.state, r.startup?.start || (r.eligible ? r.bar_end : ''), r.startup?.parent_start, r.startup?.evidence, r.startup?.neckline, r.startup?.bullish_count, r.startup?.age_bars, r.startup?.efficiency, r.metrics?.efficiency, r.metrics?.adx, r.stop_price, r.risk_pct, r.reason, viewAsOf])].map((r) => r.map(escape).join(',')).join('\r\n')
    const url = URL.createObjectURL(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' }))
    const a = document.createElement('a'); a.href = url; a.download = `${SCREEN_TIMEFRAME}筛选_${viewAsOf.slice(0, 10)}.csv`; a.click(); URL.revokeObjectURL(url)
  }
  const meta = snapshot?.metadata
  const dualMode = mtfMode || meta?.config?.selection_mode === 'dual'
  const earlyMode = meta?.config?.selection_mode === 'dual' || meta?.config?.selection_mode === 'early'
  const viewMeta=meta?.timeframes?.[SCREEN_TIMEFRAME] || meta
  const viewAsOf=viewMeta?.as_of || snapshot?.as_of
  const chartAsOf=meta?.timeframes?.[chartTimeframe]?.as_of || snapshot?.as_of
  const metrics = selected?.metrics
  const stale = snapshot && (new Date().getTime() - Date.parse(viewAsOf.replace(' ', 'T') + '+08:00')) > 4 * 86400e3

  return <div className="trend-board">
    <header className="trend-header">
      <div><div className="trend-eyebrow">ALPHA FINANCE RADAR / MARKET</div><h1>股票行情</h1><p>{mtfMode ? riskMode ? '60F结构机会 · 低位反转 / 趋势回调 / 缺口守稳' : freshMode ? '60F低位早期反转 · 日K确认入场 · M8 / M13 / M21' : '60F确定趋势 · 日K确认入场 · M8 / M13 / M21' : dualMode ? '60F趋势筛选 · 趋势回调 · 阴跌与砸坑反弹' : '以主趋势筛选机会，让每一次关注都有依据。'}</p></div>
      <div className="trend-header-actions"><span className="trend-offline">● 盘后研究</span><button className="btn ghost" disabled={busy} onClick={load}>{busy ? '读取中…' : '刷新快照'}</button></div>
    </header>
    {error && <div className="trend-notice error" role="alert">{error}<button className="btn ghost" onClick={load}>重试</button></div>}
    {busy && !snapshot && <div className="trend-placeholder">正在读取行情与筛选结果…</div>}
    {snapshot && <>
      {snapshot.latest_rules_version && snapshot.version !== snapshot.latest_rules_version && <div className="trend-notice">当前展示的是上一版筛选快照；新回调与缺口规则的全池重算尚未发布。历史结果保持原有解释。</div>}
      <div className="trend-cards">
        <div><span>扫描股票池</span><strong>{meta.universe.toLocaleString()}<small>只</small></strong><p>{freshMode ? '非ST上市股票 · 按板块区分' : '五指数成分去重'}</p></div>
        <div className="trend-card-gold"><span>{mtfMode ? '日K入场候选' : dualMode ? '两类机会确认' : earlyMode ? '启动第2—4阳候选' : '规则通过'}</span><strong>{viewMeta.eligible}<small>只</small></strong><p>{riskMode ? `首次回调 ${viewMeta.patterns?.["趋势首次回调"] || 0} · 缺口 ${viewMeta.patterns?.["低位缺口守稳"] || 0}` : freshMode ? `阴跌 ${viewMeta.patterns?.["阴跌反转"] || 0} · 深坑 ${viewMeta.patterns?.["深坑反转"] || 0}` : dualMode ? `趋势回调 ${viewMeta.patterns?.['趋势回调'] || 0} · 修复 ${viewMeta.patterns?.[mtfMode ? '止跌修复' : '阴跌/砸坑反弹'] || 0}` : earlyMode ? `2阳 ${viewMeta.states['启动第2阳'] || 0} · 3阳 ${viewMeta.states['启动第3阳'] || 0} · 4阳 ${viewMeta.states['启动第4阳'] || 0}` : `突破 ${viewMeta.states['突破候选'] || 0} · 回踩 ${viewMeta.states['回踩候选'] || 0}`}</p></div>
        <div><span>{mtfMode ? riskMode ? '60F结构机会观察' : freshMode ? '60F早期反转观察' : '60F趋势就绪' : earlyMode ? '首阳观察' : '趋势观察'}</span><strong>{mtfMode ? (riskMode ? meta.opportunity_watch : freshMode ? meta.reversal_watch : meta.trend_ready) || 0 : viewMeta.states[earlyMode ? '首阳观察' : '趋势观察'] || 0}<small>只</small></strong><p>{mtfMode ? '仍须日K转强确认，不直接买入' : meta.config.track_swing_context ? `回调再起 ${viewMeta.states['回调再起观察'] || 0} 只见“再起”` : earlyMode ? '启动待确认，不预判后续连阳' : '保留趋势，等待确认'}</p></div>
        <div><span>{mtfMode ? '日K' : '60F'}策略截止 · 上海时间</span><strong className="trend-date">{dateLabel(viewAsOf)}</strong><p>行情最新 {dateLabel(viewMeta.data_latest)}</p></div>
      </div>
      {(stale || viewMeta.data_latest.slice(0, 10) !== viewAsOf.slice(0, 10)) && <div className="trend-notice">{stale ? '当前为历史快照。' : ''} 复权因子截至 {meta.factor_latest}，策略只使用已完整验证的数据。刷新读取已发布结果；全池计算由离线任务完成。</div>}
      <section className="trend-workspace">
        <aside className="trend-stock-panel">
          <div className="trend-search"><span>⌕</span><input aria-label="搜索股票" placeholder="搜索名称或代码" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
          <select aria-label="指数成分池" value={index} onChange={(e) => setIndex(e.target.value)}><option value="">全部股票（可按指数缩小）</option>{Object.entries(INDICES).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select>
          <select aria-label="上市板块" value={board} onChange={e => setBoard(e.target.value)}><option value="">全部板块</option>{['主板', '创业板', '科创板', '北交所'].map(b => <option key={b} value={b}>{b}{meta.boards?.[b] != null ? ` · ${meta.boards[b]}` : ''}</option>)}</select>
          {freshMode && <p className="trend-list-note">{riskMode ? '底部反转与趋势回调分别观察；以结构失效距离衡量风险，首次与后续回调保留细分。' : '观察阴跌 / 深坑后的早期反转；观察标记不代表已经确认底部。'}</p>}
          {(dualMode || freshMode || riskMode) && <select aria-label="机会类型" value={pattern} onChange={e=>setPattern(e.target.value)}><option value="">趋势回调 / 底部反转 · 全部</option><option value="trend_pullback">↑ 趋势回调（含首次与后续）</option><option value="base_rebound">■ 底部反转（含阴跌 / 深坑修复）</option></select>}
          <div className="trend-tabs">{[['candidate', riskMode ? '机会' : freshMode ? '反转' : '候选'], ...(freshMode ? [['entry', '入场']] : []), ...(earlyMode ? [['first', '首阳']] : []), ...(meta.config.track_swing_context ? [['resume', '再起']] : []), ['observe', '观察'], ['star', '自选'], ['all', '全部']].map(([key, label]) => <button key={key} className={mode === key ? 'active' : ''} onClick={() => setMode(key)}>{label}{key === 'star' ? ` ${stars.length}` : ''}</button>)}</div>
          <div className="trend-list-heading"><span>{rows.length} 只股票</span><select aria-label="排序" value={sort} onChange={(e) => setSort(e.target.value)}><option value="score">质量分排序</option><option value="efficiency">{freshMode ? "反转起点新近排序" : "趋势效率排序"}</option><option value="risk">风险距离排序</option></select><button onClick={exportCsv} title="导出当前筛选" aria-label="导出当前筛选">↓</button></div>
          <div className="trend-stock-list">
            {rows.slice(0, 250).map((r) => <div className={`trend-stock-row ${code === r.ts_code ? 'selected' : ''}`} key={r.ts_code}>
              <button className="trend-star" aria-label={`${stars.includes(r.ts_code) ? '移出' : '加入'}自选 ${r.name}`} onClick={() => toggleStar(r.ts_code)}>{stars.includes(r.ts_code) ? '★' : '☆'}</button>
              <button className="trend-stock-select" onClick={() => setCode(r.ts_code)}><span><b>{r.name}</b><small>{r.ts_code} · {r.board || "未分类"} · {r.industry}</small><OpportunityBadge value={r} /></span><span><b className={r.eligible ? 'trend-gold' : ''}>{r.score.toFixed(1)}</b><small className={r.eligible ? 'trend-gold' : ''}>{r.state}</small></span></button>
            </div>)}
            {rows.length > 250 && <p className="trend-list-note">显示前 250 只，请搜索或切换指数缩小范围；导出包含全部筛选结果。</p>}
            {!rows.length && <div className="trend-empty">{mode === 'star' ? '暂无自选，点击股票旁的 ☆ 添加。' : '当前条件下没有股票。可切换“全部”查看过滤原因。'}</div>}
          </div>
        </aside>
        <div className="trend-detail-panel">
          {selected ? <>
            <div className="trend-stock-title"><div><h2>{selected.name} <small>{selected.ts_code} · {selected.board || "未分类"}</small></h2><p>{selected.indices.map((i) => INDICES[i] || i).join(' / ')} <span>· 名单日期 {selected.membership_date}</span></p></div><div className="trend-selected-tags"><OpportunityBadge value={selected} /><span className={`trend-state ${selected.eligible ? 'passed' : ''}`}>{selected.state}</span></div></div>
            {dualMode && <p className="trend-startup-info">机会类型：{selected.pattern_label || '等待有效形态'} · {mtfMode ? '60F趋势 → 日K入场' : `筛选周期：${SCREEN_TIMEFRAME}`}</p>}
            {freshMode && (!riskMode || selected.pattern !== 'trend_pullback') && selected.trend60?.origin && <p className="trend-startup-info">{selected.trend60.kind} · 60F起点 {dateLabel(selected.trend60.origin)} · 第 {selected.trend60.age_bars} 根 / 推进 {selected.trend60.bullish_count} 阳 · 距起点 {number(selected.trend60.gain_atr)} ATR。首阳是观察线索，须等日K确认，不代表已见底。</p>}
            {riskMode && selected.pattern === 'trend_pullback' && <p className="trend-startup-info">趋势回调轮次：{selected.pullback_round || '—'} · 首次与后续再起分开记录；不把回调转强称为新首阳，仍须通过结构风险检查。</p>}
            {earlyMode && selected.startup?.start && <p className="trend-startup-info">{selected.startup.phase === 'pullback_resume' ? '本轮回调转强' : '本段首阳'} {dateLabel(selected.startup.start)} · 已推进 {selected.startup.bullish_count} 根阳线 / 共 {selected.startup.age_bars} 根K线</p>}
            {selected.startup?.phase === 'pullback_resume' && selected.startup.parent_start && selected.startup.parent_start < selected.startup.start && <p className="trend-startup-info">前次起势（背景）{dateLabel(selected.startup.parent_start)} · 回调后重新转强，不计作新首阳</p>}
            {earlyMode && meta.config.require_precursor && selected.startup?.start && <p className="trend-startup-info">本轮转强前证据：{selected.startup.evidence || '无有效结构'} · 当时收复位 {number(selected.startup.neckline)}</p>}
            <div className="trend-metrics"><div><span>截止价</span><b>{number(metrics?.close)}</b></div><div><span>{mtfMode ? '60F趋势背景' : earlyMode ? '本段推进效率' : '趋势效率 ER'}</span><b>{mtfMode ? selected.trend60?.state || '等待确认' : number(earlyMode ? selected.startup?.efficiency : metrics?.efficiency)}</b></div><div><span>{mtfMode ? '日K波动 ATR' : earlyMode ? '启动进度' : '趋势强度 ADX'}</span><b>{mtfMode ? number(metrics?.atr) : earlyMode ? selected.startup?.bullish_count ? `第 ${selected.startup.bullish_count} 阳` : '等待启动' : number(metrics?.adx, 1)}</b></div><div><span>20日日均成交额</span><b>{money(metrics?.daily_amount)}</b></div><div><span>初始止损 / 距离</span><b>{number(selected.stop_price)} <small>{number(selected.risk_pct)}%</small></b></div></div>
            <div className="trend-chart-shell">
              <div className="trend-chart-toolbar">
                <div className="trend-chart-period" role="group" aria-label="K线图周期">
                  {[['1D', '日K'], ['60F', '60F']].map(([key, label]) => <button key={key} className={chartTimeframe === key ? 'on' : ''} aria-pressed={chartTimeframe === key} disabled={key === '1D' && !meta?.timeframes?.['1D']} title={key === '1D' && !meta?.timeframes?.['1D'] ? '当前快照暂无日K数据' : `显示${key === '1D' ? '日K' : '60F K线'}`} onClick={() => setChartTimeframe(key)}>{label}</button>)}
                </div>
                <button className="trend-chart-reset" disabled={!detail?.data.length} onClick={() => setResetChartView((v) => v + 1)}>回到最新</button>
                <span>截至 {dateLabel(chartAsOf)}</span>
              </div>
              <div className="trend-chart-viewport" aria-busy={chartBusy}>
                <TrendChart data={detail?.data || EMPTY_BARS} instrumentKey={`${code}:${chartTimeframe}`} stop={detail?.price_basis === 'forward_asof' && chartAsOf?.slice(0, 10) === viewAsOf?.slice(0, 10) ? selected.stop_price : null} stopLabel={mtfMode ? '日K策略初始止损' : '60F策略参考止损'} config={meta.config} timeframe={chartTimeframe} resetView={resetChartView} />
                {chartBusy && (detail?.data.length ? <span className="trend-chart-updating" role="status">更新中…</span> : <div className="trend-chart-overlay" role="status"><span className="trend-chart-spinner" />正在加载 {selected.name} 的{chartTimeframe === '1D' ? '日K' : '60F K线'}…</div>)}
                {!chartBusy && chartError && !detail?.data.length && <div className="trend-chart-overlay" role="alert"><span>{chartError}</span><button className="btn ghost" onClick={() => setChartRevision((v) => v + 1)}>重试K线</button></div>}
                {!chartBusy && !chartError && !detail?.data.length && <div className="trend-chart-overlay">没有可用的{chartTimeframe === '1D' ? '日K' : '60F K线'}</div>}
              </div>
            </div>
            {chartError && !!detail?.data.length && <div className="trend-notice" role="alert">K线更新失败：{chartError}，当前保留上次数据。<button className="btn ghost" onClick={() => setChartRevision((v) => v + 1)}>重试</button></div>}
            <div className="trend-chart-foot"><span>{chartTimeframe === '1D' ? '原生日K' : '60分钟'} · {detail?.price_basis === 'forward_asof' ? '前复权（截至所选日期，含除权除息调整）' : '原始价格'} · 成交量：股</span><span>{mtfMode ? 'M8/M13/M21 · 60F判断趋势，日K标注入场' : `图表 M8/13/21 · 策略 EMA${meta.config.fast}/${meta.config.slow} · 信号按图中周期标记`}</span></div>
            {detail?.issue && <div className="trend-notice">{detail.issue}，展示原始行情，不据此生成趋势候选。</div>}
            {detail?.version_notice && <div className="trend-notice">{detail.version_notice}</div>}
            <div className="trend-check-title"><h3>{mtfMode ? '日K' : '60F'} · 为什么{selected.eligible ? '入选' : '未入选'}</h3><span>质量分用于排序，不代表胜率</span></div>
            {selected.checks?.length ? <div className="trend-checks">{selected.checks.map((c) => <div key={c.key} className={c.passed ? 'passed' : ''}><span>{c.passed ? '✓' : '−'}</span><div><b>{c.label}</b><small>{c.rule}</small></div></div>)}</div> : <div className="trend-notice">{selected.reason}</div>}
          </> : <div className="trend-placeholder">从左侧选择一只股票，查看趋势与筛选依据。</div>}
        </div>
      </section>
      <section className="trend-method"><button aria-expanded={rulesOpen} onClick={() => setRulesOpen(!rulesOpen)}><span>策略说明 <small>{snapshot.version} · 视频理念 → 可解释规则</small></span><span>{rulesOpen ? '−' : '+'}</span></button>{rulesOpen && <div>
        <p>{riskMode ? '保留低位阴跌/深坑反转，也识别已有升势的受控回调（首次与后续分别标注）。未补的向上缺口可作为独立转强结构，不能因实体较小一律排除。缺口与常规阳线使用各自结构止损，仍受流动性、过度偏离和风险距离约束。日K收盘确认，下一交易日开盘尝试成交；不能用全天守住缺口的信息倒推当日开盘买入。' : freshMode ? '先找60F阴跌或深坑的低位背景，再追踪首根有实体、收复和量能支持的阳线。起点后最多12根60F、前4根推进阳线、较起点涨幅不超过3ATR且距M21不超过2ATR；局部回调不重置起点。早期观察不等于买入，日K仍须转强、流动性与风险距离通过。低位不保证更低风险。' : mtfMode ? '60F用M8/M13/M21识别向上趋势或开始止跌修复；日K须有受控回调或阴跌盘整背景，再出现有实体、收复和量能支持的转强阳线。日K收盘确认后，下一交易日开盘尝试买入；不把每根60F转强当作交易，也不把“机构线”视作真实资金流证据。' : dualMode ? '只抓两类：①原升势未破的受控回调，止跌后重新推进；②横盘阴跌后的结构修复，或先有平台、下破砸坑后重新收回原支撑。第二类不要求日线多头，也不将反弹直接称为长期反转。首个有效转强信号观察，第2—4根推进确认。' : earlyMode ? meta.config.require_precursor ? '日线过滤大方向 → 前面先守住低点，并有低点抬高、波动收敛、抛压减弱、底部盘整中的至少两项证据 → 突破短期阻力的有效转强阳线进入观察 → 第2—4根推进确认。首阳指第一个有效结构信号，不保证是最低点后的第一根阳线。' : '日线过滤大方向 → 回调或盘整后的首阳观察 → 第2—4根推进阳线确认 → 同周期结构与ATR控制风险。短暂整理不重置计数，超过早期窗口不再作为新启动推荐。' : '日线确认大方向 → 60F确认主趋势 → ER与ADX过滤杂乱走势 → 放量突破或回踩转强 → 用同周期ATR与结构低点设置参考风险边界。'}</p>
        {freshMode && meta.config.reversal_cycle_reset && <p>旧反转起点不会永久挡住新周期：相隔至少64根60F，且此前16根至少75%收在M21下方、M21持续下降及回撤充分，再满足新的低位首阳条件，才开始新的反转观察。短暂回调不能重置起点。</p>}
        {meta.config.track_swing_context && <p>先识别前次起势背景，再判断本轮转强。局部回调或机会类型切换不会抹掉前次起势；结构仍在时标为“回调再起”，移入观察列表。灰色“起势背景”只说明价格结构，不代表当时满足买入条件；第2—4阳统计本轮推进。</p>}
        <p>视频原例为期货。当前实现是面向A股做多的研究策略，未将“主力资金推动”当作可直接观测的事实；流畅度仅用价格路径效率和趋势强度近似。</p>
        <p>{mtfMode ? '只使用当天15:00已完成的完整60F背景与原生日K。' : '60F与原生日K分别计算，只使用已收盘数据；'}15F/30F精细择时尚未接入。参数是研究假设，仍需新样本验证；{freshMode ? '股票池为采集时的非ST上市名单，不能作为历史全市场或历史ST状态回放。' : '成分股为已保存的日期快照，不是完整历史成员序列。'}A股实际执行需考虑T+1、涨跌停、滑点及交易费用。</p>
        <p>当前参数：{mtfMode ? `M${meta.config.fast}/M${meta.config.mid}/M${meta.config.slow}简单均线，日K阳线实体≥${meta.config.daily_body_atr}ATR，初始止损至少${meta.config.stop_atr}ATR，同股冷却${meta.config.cooldown_days}个交易日` : `EMA ${meta.config.fast}/${meta.config.slow}，${earlyMode ? `首阳实体≥${meta.config.startup_body_atr}ATR、量比≥${meta.config.startup_volume_ratio}，本段效率≥${meta.config.startup_min_efficiency}` : `ER≥${meta.config.min_efficiency}，ADX≥${meta.config.min_adx}`}`}，偏离≤{meta.config.max_extension_atr}ATR，风险距离≤{meta.config.max_risk_pct}%。本周期已记录 {viewMeta.states['数据待补'] || 0} 只数据待补。</p>
      </div>}</section>
    </>}
  </div>
}
