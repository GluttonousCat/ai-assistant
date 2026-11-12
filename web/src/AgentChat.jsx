import { Fragment, useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'
import ResultChart, { buildChartSpec, COL_LABELS, parseNum, fmtDateCol } from './ResultChart'
import Markdownish from './Markdownish'

const EXAMPLES = [
  { icon: '🤖', text: '中际旭创2025年报净利润多少？' },
  { icon: '🔗', text: 'AI算力产业链有哪些环节' },
  { icon: '🎯', text: '中际旭创的预期差' },
  { icon: '🛡️', text: '中际旭创有没有财务风险' },
]

// Answer type badge: SSE data events carry intent; frontend shows an icon per type
const INTENT_META = {
  agent: { icon: '🤖', label: 'Agent' },
  query: { icon: '📊', label: '财务数据' },
  compare: { icon: '⚖️', label: '对比分析' },
  report: { icon: '📄', label: '研报解读' },
  chain: { icon: '🔗', label: '产业链' },
  alpha: { icon: '🎯', label: '预期差' },
}

const NAME_COLS = ['stock_name', 'name']
const DATE_COLS = ['end_date', 'ann_date']
const FOLDABLE = [...NAME_COLS, ...DATE_COLS]

function fmtCell(v) {
  if (v === null || v === undefined || v === '') return '—'
  const n = parseNum(v)
  if (n !== null) {
    if (Math.abs(n) >= 1000) return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
    return String(parseFloat(n.toFixed(4))) // strip floating-point trailing noise
  }
  return String(v)
}

// Result presentation: visualization first (line/bar chart), fall back to a compact table when not chartable
// Single-valued columns (stock name / report period) collapse into the caption; ts_code is always hidden
function resultView(m) {
  const cols = (m.columns || []).filter((c) => c !== 'ts_code')
  if (!m.data?.length) return null

  const distinct = (c) => [...new Set(m.data.map((r) => r[c]).filter((v) => v !== null && v !== undefined))]
  const caption = FOLDABLE
    .filter((c) => cols.includes(c) && distinct(c).length === 1)
    .map((c) => (NAME_COLS.includes(c) ? String(distinct(c)[0]) : fmtDateCol(c, distinct(c)[0])))
    .join(' · ') || null

  const chart = buildChartSpec(m.columns, m.data)
  if (chart) return { caption, chart }

  const show = cols.filter((c) => !(FOLDABLE.includes(c) && distinct(c).length === 1))
  if (show.length === 0) return null
  return { caption, columns: show }
}

// Agent chat: natural language -> agent autonomous decision + tool loop (SSE streaming output, multi-turn follow-ups supported)
export default function AgentChat() {
  const [messages, setMessages] = useState([]) // {role, text, data?, columns?, stage?, intent?}
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef(null)
  const stickRef = useRef(true) // follow new content while user is at bottom; don't disturb while scrolled up
  // Session ID: generated once on page entry and carried throughout — server injects multi-turn history from it
  // (follow-ups may omit the subject)
  const sessionRef = useRef(null)
  if (!sessionRef.current) {
    sessionRef.current = 'web-' + (crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`)
  }

  // Cross-page linkage: other pages (e.g. chain-view stock chips) write chat:pending then jump here, auto-sending the question
  useEffect(() => {
    const pending = sessionStorage.getItem('chat:pending')
    if (pending) {
      sessionStorage.removeItem('chat:pending')
      send(pending)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const scrollToBottom = (smooth = false) => {
    const el = scrollRef.current
    if (!el) return
    stickRef.current = true
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }

  // Stick to bottom while streaming; stop forcing back down once the user scrolls up
  useEffect(() => {
    if (stickRef.current) {
      const el = scrollRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [messages, busy])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stickRef.current = nearBottom
    setAtBottom(nearBottom)
  }

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || busy) return
    setInput('')
    scrollToBottom()
    setMessages((m) => [...m, { role: 'user', text: q }])
    setBusy(true)

    // Streaming message: text grows chunk by chunk, stage shows the current phase
    setMessages((m) => [...m, { role: 'assistant', text: '', stage: '' }])

    const updateLast = (fn) => {
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = fn(copy[copy.length - 1])
        return copy
      })
    }

    try {
      await platformApi.agentStream(q, sessionRef.current, (ev) => {
        if (ev.type === 'stage') {
          updateLast((msg) => {
            // First stage "意图识别: X" determines the answer type early (badge shows immediately)
            const im = /^意图识别:\s*(\w+)/.exec(ev.message || '')
            // Step cards: existing steps marked ok, new step active; batch = decision round number (for parallel grouping)
            const stages = [
              ...(msg.stages || []).map((s) => ({ ...s, ok: true })),
              { message: ev.message, batch: ev.batch, batchSize: ev.batch_size, ok: false },
            ]
            return { ...msg, stage: ev.message, stages, intent: im ? im[1] : msg.intent }
          })
        } else if (ev.type === 'tool_call') {
          // Tool finished: match step by same-batch + same-name to fill in elapsed/rows; mark red on failure
          updateLast((msg) => {
            const stages = (msg.stages || []).slice()
            const prefix = `调用 ${ev.tool}(`
            let idx = stages.findIndex((s) => !s.done && s.batch === ev.batch && (s.message || '').startsWith(prefix))
            if (idx === -1) idx = stages.findIndex((s) => !s.done && (s.message || '').startsWith(prefix))
            if (idx === -1) idx = stages.length - 1
            if (stages[idx]) {
              stages[idx] = {
                ...stages[idx], done: true,
                meta: ev.ok ? `${ev.elapsed_ms}ms${ev.rows ? ` · ${ev.rows}行` : ''}` : '失败',
                failed: !ev.ok,
              }
            }
            return { ...msg, stages }
          })
        } else if (ev.type === 'data') {
          // Tabular results accumulate into a card stream (compound answers may yield multiple charts; keep last 4)
          updateLast((msg) => ({
            ...msg,
            intent: msg.intent || ev.intent,
            tables: [...(msg.tables || []),
              { data: ev.data, columns: ev.columns, rows: ev.rows }].slice(-4),
            stage: '',
            stages: (msg.stages || []).map((s) => ({ ...s, ok: true })),
          }))
        } else if (ev.type === 'delta') {
          updateLast((msg) => ({
            ...msg, text: msg.text + ev.text, stage: '',
            stages: msg.stages?.length
              ? msg.stages.map((s, j) => ({ ...s, ok: j < msg.stages.length - 1 }))
              : msg.stages,
          }))
        } else if (ev.type === 'error') {
          updateLast((msg) => ({
            ...msg, text: `❌ ${ev.message}`, stage: '',
            stages: (msg.stages || []).map((s) => ({ ...s, ok: true })),
          }))
        }
      })
    } catch (err) {
      updateLast((msg) => ({ ...msg, text: `❌ ${err.message}`, stage: '' }))
    } finally {
      updateLast((msg) => ({
        ...msg, stage: '',
        stages: (msg.stages || []).map((s) => ({ ...s, ok: true })),
      }))
      setBusy(false)
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-window" ref={scrollRef} onScroll={onScroll}>
        {messages.length === 0 && !busy && (
          <div className="chat-empty">
            <div className="chat-empty-icon">✦</div>
            <h3>Alpha Radar · Agent</h3>
            <p>随意问 · 财务数据 / 研报观点 / 产业链 / 量化信号 · 支持多轮追问</p>
            <div className="chat-examples">
              {EXAMPLES.map((ex) => (
                <button key={ex.text} onClick={() => send(ex.text)}>
                  <span className="ex-icon">{ex.icon}</span>{ex.text}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => {
          const streaming = busy && i === messages.length - 1 && m.role === 'assistant'
          // Result card stream: agent compound answers may produce multiple tables/charts (keep last 4)
          const blocks = m.role === 'assistant'
            ? (m.tables?.length
                ? m.tables
                : (m.data?.length ? [{ data: m.data, columns: m.columns }] : []))
                .map((tb) => ({ tb, view: resultView(tb) }))
                .filter((b) => b.view)
            : []
          return (
            <div key={i} className={`chat-msg ${m.role}`}>
              {m.role === 'assistant' ? (
                <>
                  <div className="chat-avatar">✦</div>
                  <div className="chat-body">
                    {INTENT_META[m.intent] && (
                      <div className={`chat-type-badge ${m.intent}`}>
                        <span className="icon">{INTENT_META[m.intent].icon}</span>
                        {INTENT_META[m.intent].label}
                      </div>
                    )}
                    {m.stages?.length > 0 ? (
                      <div className="agent-steps">
                        {m.stages.map((s, j) => {
                          // Same batch (>1 call) is a parallel group: prefix the first entry with a ⚡并行×N label, indent within the batch
                          const prev = m.stages[j - 1]
                          const parallel = s.batch != null && s.batchSize > 1
                          const firstOfBatch = parallel && (!prev || prev.batch !== s.batch)
                          return (
                            <Fragment key={j}>
                              {firstOfBatch && <div className="agent-batch-label">⚡ 并行 ×{s.batchSize}</div>}
                              <div className={`agent-step ${parallel ? 'batched' : ''} ${s.failed ? 'failed' : s.ok ? 'ok' : 'active'}`}>
                                <span className="agent-dot">{s.ok ? '✓' : s.failed ? '⚠' : ''}</span>
                                {s.message}{s.meta ? ` (${s.meta})` : ''}
                              </div>
                            </Fragment>
                          )
                        })}
                      </div>
                    ) : (m.stage && (
                      <div className="chat-stage">
                        <span className="spinner-dot" /> {m.stage}
                      </div>
                    ))}
                    {(m.text || !m.stage) && (
                      <div className={`chat-bubble md${streaming ? ' streaming' : ''}`}>
                        {m.text ? <Markdownish text={m.text} /> : (
                          <div className="chat-bubble typing"><span></span><span></span><span></span></div>
                        )}
                      </div>
                    )}
                    {blocks.map(({ tb, view }, k) => (
                      <div className="chat-table-block" key={k}>
                        {view.caption && <div className="chat-table-caption">{view.caption}</div>}
                        {view.chart ? (
                          <ResultChart spec={view.chart} />
                        ) : (
                          <div className="chat-table">
                            <table className="grid">
                              <thead>
                                <tr>{view.columns.map((c) => <th key={c}>{COL_LABELS[c] || c}</th>)}</tr>
                              </thead>
                              <tbody>
                                {tb.data.map((row, j) => (
                                  <tr key={j}>
                                    {view.columns.map((c) => (
                                      <td key={c}>
                                        {(c === 'end_date' || c === 'ann_date' || c === 'trade_date')
                                          ? fmtDateCol(c, row[c])
                                          : fmtCell(row[c])}
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="chat-bubble user">{m.text}</div>
              )}
            </div>
          )
        })}
        <div className="chat-window-pad" />
      </div>

      {!atBottom && (
        <button className="chat-jump" title="回到底部" onClick={() => scrollToBottom(true)}>↓</button>
      )}

      <div className="chat-inputbar">
        <div className="chat-input-shell">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send()}
            placeholder="随意问, 例如: 中际旭创最近三年净利润 / 环比下降了吗, 数据对吗"
            disabled={busy}
          />
          <button className="btn" onClick={() => send()} disabled={busy || !input.trim()}>发送</button>
        </div>
      </div>
    </div>
  )
}
