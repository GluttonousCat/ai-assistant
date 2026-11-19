import { useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'

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

function parseAnalysis(raw) {
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

function SourceQuote({ source }) {
  if (!source?.quote) return null
  return (
    <details>
      <summary className="dim">{source.page ? `PDF 第 ${source.page} 页` : `正文片段 ${source.segment}`} · 查看出处</summary>
      <blockquote>{source.quote}</blockquote>
    </details>
  )
}

function AnalysisOverview({ data }) {
  if (!data) return null
  const { coverage } = data
  const keyPoints = Array.isArray(data.key_points) ? data.key_points.filter(point => typeof point === 'string') : []
  const risks = Array.isArray(data.risks) ? data.risks.filter(risk => typeof risk === 'string') : []
  return (
    <div className="ai-result">
      {typeof data.rating === 'string' && data.rating && data.rating !== '无' && (
        <div className="report-meta"><span className="badge EARLY">报告评级 · {data.rating}</span></div>
      )}
      {coverage && (
        <p className="dim">
          已分析 {coverage.analyzed_segments}/{coverage.segment_count} 个正文片段
          {coverage.page_count ? ` · PDF 共 ${coverage.page_count} 页` : ''}
          {coverage.partial && ' · 正文覆盖不完整'}
          {coverage.missing_pages?.length > 0 && ` · 缺少 ${coverage.missing_pages.length} 页`}
          {coverage.rejected_forecasts > 0 && ` · ${coverage.rejected_forecasts} 条预测未通过出处核对`}
          {coverage.forecast_conflicts > 0 && ` · ${coverage.forecast_conflicts} 组预测口径冲突`}
        </p>
      )}
      {typeof data.core_view === 'string' && data.core_view && <p className="ai-coreview">{data.core_view}</p>}
      {keyPoints.length > 0 && (
        <ul className="ai-keypoints">
          {keyPoints.map((point, i) => <li key={i}>{point}</li>)}
        </ul>
      )}
      {risks.length > 0 && (
        <>
          <div className="panel-title">风险提示</div>
          <ul className="ai-keypoints">{risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul>
        </>
      )}
      {Array.isArray(data.evidence) && data.evidence.length > 0 && (
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

function ForecastTable({ forecasts }) {
  if (!forecasts?.length) return null
  return (
    <div className="forecast-box">
      <div className="panel-title">盈利预测与目标价</div>
      <table className="grid">
        <thead><tr><th>指标</th><th>期间</th><th>预测值</th><th>单位</th><th>预测依据</th></tr></thead>
        <tbody>
          {forecasts.map((forecast, i) => (
            <tr key={i}>
              <td>{METRIC_LABEL[forecast.metric] || forecast.metric}</td>
              <td className="dim">{forecast.period === 'NA' ? '—' : forecast.period || '—'}</td>
              <td>{forecast.value}</td>
              <td className="dim">{forecast.unit || '—'}</td>
              <td className="dim">{forecast.raw_text || '—'}<SourceQuote source={forecast.source} /></td>
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
  const detailRequest = useRef(null)
  const streamBoxRef = useRef(null)

  const pageSize = 15

  const load = async (p = page, searchQuery = query) => {
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
  useEffect(() => { window._reportsReload = () => load(page) }, [page, query, analysis, market]) // eslint-disable-line
  useEffect(() => () => {
    listRequest.current?.abort()
    detailRequest.current?.abort()
    aiAbortRef.current?.()
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
    // Abort the unfinished SSE connection (the backend analysis thread is unaffected and still completes ingestion)
    aiAbortRef.current?.()
    aiAbortRef.current = null
    setAi(null)
  }

  // AI analysis: open the streaming modal showing the backend agent flow + model output
  function analyze(r) {
    setBusyId(r.report_id)
    setAi({ id: r.report_id, title: r.title, stages: [], text: '', data: null, error: null, done: false })
    const handle = platformApi.analyzeReportStream(r.report_id, (ev) => {
      setAi((prev) => {
        if (!prev || prev.id !== r.report_id) return prev
        if (ev.type === 'stage') {
          // New stage arrived: mark all previous as done, current one becomes the active step
          return { ...prev, stages: [...prev.stages.map((s) => ({ ...s, status: 'ok' })), { message: ev.message, status: 'active' }] }
        }
        if (ev.type === 'delta') return { ...prev, text: prev.text + ev.text }
        if (ev.type === 'data') return { ...prev, data: ev.data }
        if (ev.type === 'error') return { ...prev, error: ev.message }
        if (ev.type === 'done') return { ...prev, done: true, stages: prev.stages.map((s) => ({ ...s, status: 'ok' })) }
        return prev
      })
    })
    aiAbortRef.current = handle.abort
    handle.promise
      .catch((e) => setAi((prev) => (prev && prev.id === r.report_id && !prev.error ? { ...prev, error: e.message } : prev)))
      .finally(() => {
        setBusyId(null)
        aiAbortRef.current = null
        load(page) // refresh the list status badges
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
                <ForecastTable forecasts={detail.analysis?.analysis_version >= 2
                  ? detail.analysis.forecasts
                  : detail.forecasts?.map(f => ({
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
                    {s.status === 'active' ? <span className="spinner-dot" /> : '✓'}
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
              <>
                <div className="panel-title" style={{ marginTop: 16 }}>模型输出 · 流式</div>
                <pre ref={streamBoxRef} className={`stream-out${!ai.done && !ai.error ? ' streaming' : ''}`}>
                  {ai.text}
                </pre>
              </>
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
                <ForecastTable forecasts={ai.data.forecasts} />
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
