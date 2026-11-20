import { useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'
import { claimSections, insightTitles } from './reports/claimSections'

const ANALYSIS_LABEL = { done: '已分析', failed: '失败', none: '未分析' }
const MARKET_LABEL = { A股: 'A股', H股: 'H股', 台股: 'TW股', TW股: 'TW股', 日股: '日股', 韩股: '韩股', 美股: '美股', 欧洲股: '欧洲股', 东南亚股: '东南亚股', 宏观: '宏观', 商品: '商品', 行业: '行业', 其他: '其他' }
const METRIC_LABEL = {
  revenue: '营业收入', net_profit: '净利润', eps: 'EPS',
  roe: 'ROE', target_price: '目标价', gross_margin: '毛利率',
}

const EVIDENCE_LABEL = {
  driver: '逻辑与假设', catalyst: '催化因素', valuation: '估值依据',
  revision: '预期调整', risk: '风险', industry: '行业观察',
}

const MATERIAL_LABEL = { original: '研报原文', commentary: '二手解读', unknown: '材料性质待核实' }
const SPEAKER_LABEL = { broker: '券商', management: '公司管理层', survey: '调查受访者', third_party: '第三方', unknown: '发言主体待核实' }
const STANCE_LABEL = { observation: '观察', expectation: '预期', conditional: '条件判断', risk: '风险提示', unknown: '语气待核实' }

function parseAnalysis(raw) {
  try {
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

function SourceQuote({ source, label = '原文', expanded = false }) {
  if (typeof source?.quote !== 'string' || !source.quote.trim()) return null
  const location = source.page ? `PDF 第 ${source.page} 页` : source.segment ? `正文片段 ${source.segment}` : ''
  return (
    <details className="report-source" open={expanded}>
      <summary className="dim">{[label, location].filter(Boolean).join(' · ')}</summary>
      <blockquote>{source.quote}</blockquote>
    </details>
  )
}

function ClaimLabels({ entry, commentary }) {
  const speaker = SPEAKER_LABEL[entry.attribution] || SPEAKER_LABEL.unknown
  const stance = STANCE_LABEL[entry.stance] || STANCE_LABEL.unknown
  return <div className="claim-labels">{commentary ? '材料转述 · ' : ''}{speaker} · {stance}</div>
}

function ClaimSection({ title, entries, featured, attributed, commentary }) {
  return (
    <section className="research-insights">
      <h3>{title}</h3>
      <ul>
        {entries.map((entry, i) => {
          const verbatim = attributed && entry.statement === entry.source?.quote
          const attributes = [
            ['涉及对象', entry.subject], ['观察指标', entry.indicator],
            ['条件', entry.condition], ['时间范围', entry.horizon],
          ].filter(([, value]) => typeof value === 'string' && value.trim())
          return (
            <li key={i}>
              {attributed && <ClaimLabels entry={entry} commentary={commentary} />}
              {!verbatim && <p>{attributed && <span className="dim">中文转述 · </span>}{entry.statement}</p>}
              {attributes.length > 0 && (
                <dl>
                  {attributes.map(([label, value]) => (
                    <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                  ))}
                </dl>
              )}
              <SourceQuote source={entry.source} label={commentary ? '解读材料摘录' : '原文摘录'} expanded={featured || verbatim} />
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function AnalysisOverview({ data }) {
  if (!data) return null
  const { coverage } = data
  const materialLabel = MATERIAL_LABEL[data.material_type]
  const commentary = data.material_type === 'commentary'
  const attributed = Number(data.analysis_version) >= 4
  const sections = attributed ? claimSections(data) : []
  const showRating = typeof data.rating === 'string' && data.rating && data.rating !== '无' &&
    (!attributed || Boolean(data.rating_source?.quote))
  const keyPoints = Array.isArray(data.key_points) ? data.key_points.filter(point => typeof point === 'string') : []
  const risks = Array.isArray(data.risks) ? data.risks.filter(risk => typeof risk === 'string') : []
  return (
    <div className="ai-result">
      {(materialLabel || showRating) && (
        <div className="report-meta">
          {materialLabel && <strong>{materialLabel}</strong>}
          {showRating && (
            <span className="badge EARLY">{commentary ? '材料转述评级' : '材料所列评级'} · {data.rating}</span>
          )}
        </div>
      )}
      {commentary && <p className="report-attribution">下列判断和预测来自这份解读材料的转述，展开出处可核对材料中的表述。</p>}
      {!attributed && <p className="report-attribution">历史分析尚未逐条标注发言人和语气，可重新分析后对照引文。</p>}
      {materialLabel && data.material_source && <SourceQuote source={data.material_source} label="材料署名依据" />}
      {showRating && data.rating_source && <SourceQuote source={data.rating_source} label={commentary ? '转述评级出处' : '评级出处'} />}
      {coverage && (
        <p className="dim">
          已分析 {coverage.analyzed_segments}/{coverage.segment_count} 个正文片段
          {coverage.page_count ? ` · PDF 共 ${coverage.page_count} 页` : ''}
          {coverage.partial && ' · 正文覆盖不完整'}
          {coverage.missing_pages?.length > 0 && ` · 缺少 ${coverage.missing_pages.length} 页`}
          {coverage.rejected_forecasts > 0 && ` · ${coverage.rejected_forecasts} 条预测未通过出处核对`}
          {coverage.forecast_conflicts > 0 && ` · ${coverage.forecast_conflicts} 组预测口径冲突`}
          {coverage.rejected_evidence > 0 && ` · ${coverage.rejected_evidence} 条研究依据未通过核对`}
          {coverage.rejected_insights > 0 && ` · ${coverage.rejected_insights} 条研究判断的条件或字段未通过核对`}
        </p>
      )}
      {attributed && !sections.length && <p className="dim">本次分析没有可逐条对照原文的观点。</p>}
      {sections.map(section => (
        <ClaimSection key={section.title} {...section} commentary={commentary} attributed />
      ))}
      {attributed && Array.isArray(data.forecast_excerpts) && data.forecast_excerpts.length > 0 && (
        <details className="report-evidence">
          <summary>待核实预测口径的原文 · {data.forecast_excerpts.length} 条</summary>
          {data.forecast_excerpts.map((source, i) => <SourceQuote key={i} source={source} expanded />)}
        </details>
      )}
      {!attributed && typeof data.core_view === 'string' && data.core_view && <p className="ai-coreview">{data.core_view}</p>}
      {!attributed && keyPoints.length > 0 && (
        <ul className="ai-keypoints">
          {keyPoints.map((point, i) => <li key={i}>{point}</li>)}
        </ul>
      )}
      {!attributed && Object.entries(insightTitles).map(([group, title]) => {
        const entries = data.research_insights?.[group]
        return Array.isArray(entries) && entries.length > 0 &&
          <ClaimSection key={group} title={title} entries={entries.filter(entry => typeof entry?.statement === 'string')} commentary={commentary} />
      })}
      {!attributed && risks.length > 0 && (
        <>
          <div className="panel-title">风险提示</div>
          <ul className="ai-keypoints">{risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul>
        </>
      )}
      {!attributed && Array.isArray(data.evidence) && data.evidence.length > 0 && (
        <details className="report-evidence">
          <summary>研究依据 · {data.evidence.length} 条</summary>
          <ul className="ai-keypoints">
            {data.evidence.map((note, i) => (
              <li key={i}>
                <strong>{EVIDENCE_LABEL[note.kind]} · </strong>{note.statement}
                <SourceQuote source={note.source} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function ForecastTable({ forecasts, materialType, analysisVersion }) {
  if (!forecasts?.length) return null
  const showSymbols = forecasts.some(forecast => forecast.ts_code || forecast.symbol)
  return (
    <div className="forecast-box">
      <div className="panel-title">
        {materialType === 'commentary' ? '解读材料转述的预测与目标价' : '盈利预测与目标价'}
      </div>
      <table className="grid">
        <thead><tr>{showSymbols && <th>标的</th>}<th>指标</th><th>期间</th><th>预测值</th><th>单位</th><th>预测依据</th></tr></thead>
        <tbody>
          {forecasts.map((forecast, i) => (
            <tr key={i}>
              {showSymbols && <td>{forecast.ts_code || forecast.symbol || '—'}</td>}
              <td>{METRIC_LABEL[forecast.metric] || forecast.metric}</td>
              <td className="dim">{forecast.period === 'NA' ? '—' : forecast.period || '—'}</td>
              <td>{forecast.value}</td>
              <td className="dim">{forecast.unit || '—'}</td>
              <td className="dim">
                {analysisVersion >= 4 && <ClaimLabels entry={forecast} commentary={materialType === 'commentary'} />}
                {forecast.raw_text || '—'}
                {forecast.header_source && <SourceQuote source={forecast.header_source} label="预测期表头" />}
                <SourceQuote source={forecast.source} label={materialType === 'commentary' ? '解读材料摘录' : '预测出处'} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Reports center: Zhixingqiu (知识星球) report library + LLM extraction results
export default function Reports() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [query, setQuery] = useState('')
  const [analysis, setAnalysis] = useState('all')
  const [market, setMarket] = useState('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [detail, setDetail] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [jumpInput, setJumpInput] = useState('')
  // AI analysis streaming modal state: { id, title, stages:[{message,status}], text, data, error, done }
  const [ai, setAi] = useState(null)
  const aiAbortRef = useRef(null)
  const listRequest = useRef(null)
  const reloadList = useRef(null)
  const detailRequest = useRef(null)
  const openDetailId = useRef(null)
  const streamBoxRef = useRef(null)

  const pageSize = 15

  const load = async (p = page, searchQuery = query) => {
    reloadList.current = () => load(p, searchQuery)
    listRequest.current?.abort()
    const controller = new AbortController()
    listRequest.current = controller
    setLoading(true)
    setError(null)
    try {
      const params = {
        keyword: searchQuery || undefined, analysis: analysis === 'all' ? undefined : analysis,
        market: market === 'all' ? undefined : market,
        page: p, page_size: pageSize,
      }
      const res = await platformApi.reports(params, { signal: controller.signal })
      if (controller.signal.aborted) return
      setItems(res.items || [])
      setTotal(res.total || 0)
      setPage(p)
    } catch (e) {
      if (!controller.signal.aborted) setError(e.message)
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  useEffect(() => { load(1) }, [analysis, market]) // eslint-disable-line
  useEffect(() => { window._reportsReload = () => reloadList.current?.() }, [])
  useEffect(() => () => {
    listRequest.current?.abort()
    detailRequest.current?.abort()
    aiAbortRef.current?.()
    aiAbortRef.current = null
    delete window._reportsReload
  }, [])

  const pages = Math.max(1, Math.ceil(total / pageSize))

  function search() {
    setQuery(keyword)
    load(1, keyword)
  }

  function doJump() {
    const p = parseInt(jumpInput, 10)
    if (!isNaN(p) && p >= 1 && p <= pages && p !== page) load(p)
  }

  async function openDetail(id) {
    openDetailId.current = id
    detailRequest.current?.abort()
    const controller = new AbortController()
    detailRequest.current = controller
    setDetail({ loading: true })
    try {
      const d = await platformApi.reportDetail(id, { signal: controller.signal })
      if (!controller.signal.aborted) setDetail({ ...d, analysis: parseAnalysis(d.analysis_json) })
    } catch (e) {
      if (!controller.signal.aborted) setDetail({ error: e.message })
    }
  }

  function closeDetail() {
    openDetailId.current = null
    detailRequest.current?.abort()
    setDetail(null)
  }

  // Download the original report PDF (locally downloaded file, saved via authorized fetch blob)
  async function downloadOriginal(d) {
    try {
      await platformApi.downloadReport(d.report_id)
    } catch (e) {
      alert(`下载失败: ${e.message}`)
    }
  }

  // Stick to bottom while streaming
  useEffect(() => {
    const el = streamBoxRef.current
    if (el && ai && !ai.done) el.scrollTop = el.scrollHeight
  }, [ai?.text]) // eslint-disable-line

  function closeAi() {
    // Keep listening while hidden so persisted changes reach the current list and detail.
    setAi(null)
  }

  // AI analysis: open the streaming modal showing the backend agent flow + model output
  function analyze(r) {
    setBusyId(r.report_id)
    setAi({ id: r.report_id, title: r.title, stages: [], text: '', data: null, error: null, done: false })
    let saved = false
    let streamFinished = false
    const handle = platformApi.analyzeReportStream(r.report_id, (ev) => {
      if (ev.type === 'data') saved = true
      if (ev.type === 'done') streamFinished = true
      setAi((prev) => {
        if (!prev || prev.id !== r.report_id) return prev
        if (ev.type === 'stage') {
          // New stage arrived: mark all previous as done, current one becomes the active step
          return { ...prev, stages: [...prev.stages.map((s) => ({ ...s, status: 'ok' })), { message: ev.message, status: 'active' }] }
        }
        if (ev.type === 'delta') return { ...prev, text: prev.text + ev.text }
        if (ev.type === 'data') return { ...prev, data: ev.data }
        if (ev.type === 'error') return { ...prev, error: ev.message, stages: prev.stages.map((s) => s.status === 'active' ? { ...s, status: 'failed' } : s) }
        if (ev.type === 'done') return { ...prev, done: true, stages: prev.stages.map((s) => s.status === 'active' ? { ...s, status: 'ok' } : s) }
        return prev
      })
    })
    aiAbortRef.current = handle.abort
    handle.promise
      .then(() => {
        if (aiAbortRef.current === handle.abort && !streamFinished) throw new Error('分析连接中断，可在详情中查看已保存的结果。')
      })
      .catch((e) => setAi((prev) => (prev && prev.id === r.report_id && !prev.error ? { ...prev, error: e.message } : prev)))
      .finally(() => {
        if (aiAbortRef.current !== handle.abort) return
        setBusyId(null)
        aiAbortRef.current = null
        reloadList.current?.()
        if (saved && openDetailId.current === r.report_id) openDetail(r.report_id)
      })
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>研报中心</h2>
        </div>
        <div className="controls" style={{ padding: 0 }}>
          <div className="control">
            <input
              style={{
                background: 'var(--bg-2)', color: 'var(--text)',
                border: '1px solid var(--border)', borderRadius: 7, padding: '7px 10px',
                width: 220, fontSize: 13, outline: 'none',
              }}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
              placeholder="搜索标题 / 分析师 / 机构"
            />
          </div>
          <div className="control">
            <select value={analysis} onChange={(e) => setAnalysis(e.target.value)}>
              <option value="all">全部状态</option>
              <option value="done">已分析</option>
              <option value="pending">未分析</option>
            </select>
          </div>
          <div className="control">
            <select value={market} onChange={(e) => setMarket(e.target.value)}>
              <option value="all">全部市场</option>
              {Object.entries(MARKET_LABEL).filter(([k]) => k !== '台股')
                .map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </div>
          <button className="btn ghost" onClick={search}>搜索</button>
        </div>
      </div>

      <div className="table-wrap">
        {loading ? (
          <div className="loading"><div className="spinner" />加载中…</div>
        ) : error ? (
          <div className="error">{error}</div>
        ) : items.length === 0 ? (
          <div className="empty">暂无研报。请先运行知识星球爬虫并同步到 PG (scripts/sync_zsxq_to_pg.py)。</div>
        ) : (
          <>
            <table className="grid">
              <thead>
                <tr>
                  <th>标题</th><th>机构</th><th>标的</th><th>行业</th><th>地区</th>
                  <th>市场</th><th>发布日期</th><th>状态</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.report_id} onClick={() => openDetail(r.report_id)}>
                    <td style={{ maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis' }} title={r.title || ''}>
                      {r.title || '(无标题)'}
                    </td>
                    <td className="dim" style={{ whiteSpace: 'nowrap' }}>{r.org_name || '—'}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {r.target
                        ? <span className="sym" style={{ fontSize: 13 }}>{r.target}</span>
                        : <span className="dim">—</span>}
                    </td>
                    <td className="dim">{r.industry || '—'}</td>
                    <td className="dim">{r.region || '—'}</td>
                    <td>{r.market
                      ? <span className="badge FORM">{MARKET_LABEL[r.market] || r.market}</span>
                      : <span className="dim">—</span>}</td>
                    <td className="dim">{r.publish_date || '—'}</td>
                    <td>
                      <span className={`badge ${r.analysis_status === 'done' ? 'EARLY' : r.analysis_status === 'failed' ? 'EXH' : 'FORMATION'}`}>
                        {ANALYSIS_LABEL[r.analysis_status] || '未分析'}
                      </span>
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {r.analysis_status === 'done' ? (
                        <span className="dim" style={{ fontSize: 12 }}>✓</span>
                      ) : (
                        <button className="btn ghost" style={{ padding: '4px 12px' }} disabled={busyId != null}
                          onClick={() => analyze(r)}>
                          {busyId === r.report_id ? '分析中…' : 'AI 分析'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="pager">
              <button className="btn ghost" disabled={page <= 1} onClick={() => load(page - 1)}>上一页</button>
              <span className="dim">{page} / {pages} · 共 {total} 篇</span>
              <button className="btn ghost" disabled={page >= pages} onClick={() => load(page + 1)}>下一页</button>
              <span className="pager-jump">
                跳至
                <input
                  type="number"
                  min="1"
                  max={pages}
                  value={jumpInput}
                  onChange={(e) => setJumpInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && doJump()}
                  placeholder={String(page)}
                />
                页
                <button className="btn ghost" onClick={doJump}>跳转</button>
              </span>
            </div>
          </>
        )}
      </div>

      {detail && (
        <div className="modal-mask" onClick={closeDetail}>
          <div className="modal report-modal" onClick={(e) => e.stopPropagation()}>
            {detail.loading ? (
              <div className="loading"><div className="spinner" />加载研报…</div>
            ) : detail.error ? (
              <div className="error">{detail.error}</div>
            ) : (
              <>
                <div className="modal-head">
                  <h2 style={{ fontSize: 17 }}>{detail.title || `研报 #${detail.report_id}`}</h2>
                  <span className="code">{detail.ts_code || detail.symbols || '未识别标的'}</span>
                  {detail.file_path && (
                    <button className="btn ghost" style={{ padding: '5px 14px', marginLeft: 'auto' }}
                      onClick={() => downloadOriginal(detail)}>
                      ⬇ 下载原文
                    </button>
                  )}
                  <button className="btn ghost" disabled={busyId != null}
                    onClick={() => { closeDetail(); analyze(detail) }}>
                    {detail.analysis_status === 'done' ? '重新分析' : 'AI 分析'}
                  </button>
                  <button className="modal-close" onClick={closeDetail}>×</button>
                </div>
                <div className="report-meta">
                  <span>{detail.org_name || '未知机构'}</span>
                  <span>{detail.author || '未知作者'}</span>
                  <span>{detail.publish_date || '日期未知'}</span>
                  <span className="badge EARLY">{detail.report_type || '研报'}</span>
                  {detail.market && <span className="badge FORM">{MARKET_LABEL[detail.market] || detail.market}</span>}
                  {detail.analysis_status === 'done' && <span className="badge DIG">已 AI 分析</span>}
                </div>
                {detail.file_name && (
                  <div className="report-filename" title={detail.file_name}>原始文件: {detail.file_name}</div>
                )}

                <AnalysisOverview data={detail.analysis} />
                <ForecastTable analysisVersion={detail.analysis?.analysis_version} materialType={detail.analysis?.material_type} forecasts={detail.analysis?.analysis_version >= 2
                  ? detail.analysis.forecasts
                  : detail.forecasts?.map(f => ({
                    ts_code: f.ts_code, symbol: f.symbol,
                    metric: f.forecast_type, period: f.forecast_period,
                    value: f.forecast_value, unit: f.forecast_unit, raw_text: f.raw_text,
                  }))} />

                <div className="report-text">
                  <div className="panel-title">研报正文节选</div>
                  <pre>{detail.content_text || '(无文本内容)'}</pre>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {ai && (
        <div className="modal-mask">
          <div className="modal ai-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2 style={{ fontSize: 17 }}>AI 分析</h2>
              <span className="code" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 520 }}>
                {ai.title || `研报 #${ai.id}`}
              </span>
              <button className="modal-close" onClick={closeAi}>×</button>
            </div>

            <div className="panel-title" style={{ marginTop: 10 }}>Agent 流程</div>
            <div className="agent-steps">
              {ai.stages.map((s, i) => (
                <div key={i} className={`agent-step ${s.status}`}>
                  <span className="agent-dot">
                    {s.status === 'active' ? <span className="spinner-dot" /> : s.status === 'failed' ? '×' : '✓'}
                  </span>
                  <span>{s.message}</span>
                </div>
              ))}
              {ai.stages.length === 0 && (
                <div className="agent-step active">
                  <span className="agent-dot"><span className="spinner-dot" /></span>
                  <span>启动中…</span>
                </div>
              )}
            </div>

            {ai.text && (
              <details className="report-evidence">
                <summary>模型提取草稿</summary>
                <pre ref={streamBoxRef} className={`stream-out${!ai.done && !ai.error ? ' streaming' : ''}`}>
                  {ai.text}
                </pre>
              </details>
            )}

            {ai.data && (
              <div className="ai-result">
                <div className="panel-title" style={{ marginTop: 16 }}>结构化结果</div>
                <div className="report-meta">
                  {ai.data.market && <span className="badge FORM">{MARKET_LABEL[ai.data.market] || ai.data.market}</span>}
                  {ai.data.report_type && <span className="badge DIG">{ai.data.report_type}</span>}
                  <span>{ai.data.org_name || '未知机构'}</span>
                  <span className="code">{ai.data.ts_code || (ai.data.symbols || []).join(', ') || '未识别标的'}</span>
                </div>
                <AnalysisOverview data={ai.data} />
                <ForecastTable analysisVersion={ai.data.analysis_version} materialType={ai.data.material_type} forecasts={ai.data.forecasts} />
              </div>
            )}

            {ai.error && <div className="ai-error">⚠ {ai.error}</div>}

            <div className="ai-foot">
              {ai.done && !ai.error && (
                <button className="btn" onClick={() => { closeAi(); openDetail(ai.id) }}>查看详情</button>
              )}
              <button className="btn ghost" onClick={closeAi}>
                {ai.done || ai.error ? '关闭' : '后台运行并关闭'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
