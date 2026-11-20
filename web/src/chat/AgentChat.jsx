import { Fragment, memo, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { platformApi } from '../platformApi'
import ResultChart from './ResultChart'
import { buildChartSpec, COL_LABELS, parseNum, fmtDateCol } from './resultData'
import Markdownish from '../components/Markdownish'
import useChatWorkspace from './useChatWorkspace'
import brandMark from '../assets/favicon.svg?no-inline'
import './chat.css'

const EXAMPLES = [
  { label: '查财务', text: '中际旭创 2025 年净利润是多少？' },
  { label: '看产业链', text: 'AI 算力产业链有哪些环节？' },
  { label: '读研报', text: '近期研报如何看待中际旭创的业绩？' },
  { label: '做对比', text: '中际旭创和新易盛的毛利率有何差异？' },
]

const AnswerMarkdown = memo(Markdownish)

// Answer type badge: SSE data events carry intent; frontend shows an icon per type
const INTENT_META = {
  agent: { icon: '🤖', label: '智能对话' },
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


const ChatResults = memo(function ChatResults({ tables, data, columns }) {
  const blocks = (tables?.length ? tables : data?.length ? [{ data, columns }] : [])
    .map((tb) => ({ tb, view: resultView(tb) })).filter((block) => block.view)
  return <>{blocks.map(({ tb, view }, k) => (
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
                    ))}</>
})

// Agent chat: natural language -> agent autonomous decision + tool loop (SSE streaming output, multi-turn follow-ups supported)
export default function AgentChat() {
  const workspace = useChatWorkspace()
  const { messages, draft: input } = workspace.chat
  const { setMessages, setInput } = workspace
  const [copied, setCopied] = useState(null)
  const [copyError, setCopyError] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const streamRef = useRef(null)
  const inputRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef(null)
  const transcriptRef = useRef(null)
  const historyToggleRef = useRef(null)
  const historyRef = useRef(null)
  const positions = useRef(new Map())
  const stickRef = useRef(true) // follow new content while user is at bottom; don't disturb while scrolled up
  const sessionId = 'web-' + workspace.chat.id
  const empty = !messages.length && !busy

  useEffect(() => () => streamRef.current?.abort(), [])
  useLayoutEffect(() => {
    const el = inputRef.current
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [input, empty])

  useLayoutEffect(() => {
    const el = scrollRef.current
    const offset = positions.current.get(workspace.chat.id)
    el.scrollTop = offset ?? el.scrollHeight
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 16
    setAtBottom(stickRef.current)
    setCopied(null)
    setCopyError('')
  }, [workspace.chat.id])

  useLayoutEffect(() => {
    const el = scrollRef.current
    const follow = () => { if (stickRef.current) el.scrollTop = el.scrollHeight }
    const observer = new ResizeObserver(follow)
    observer.observe(el)
    observer.observe(transcriptRef.current)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (historyOpen) historyRef.current?.querySelector('.selected button:enabled')?.focus()
  }, [historyOpen])

  useEffect(() => {
    if (!historyOpen && !busy) return
    function onEscape(e) {
      if (e.key !== 'Escape' || e.isComposing || e.keyCode === 229 || e.defaultPrevented) return
      if (historyOpen) closeHistory()
      else streamRef.current?.abort()
    }
    document.addEventListener('keydown', onEscape)
    return () => document.removeEventListener('keydown', onEscape)
  }, [busy, historyOpen])

  async function copyAnswer(text, index) {
    try { await navigator.clipboard.writeText(text); setCopied(index); setCopyError('') }
    catch { setCopyError('复制失败，请选中文字后手动复制') }
  }

  // Cross-page linkage: other pages (e.g. chain-view stock chips) write chat:pending then jump here, auto-sending the question
  useEffect(() => {
    const pending = sessionStorage.getItem('chat:pending')
    if (pending) {
      // Defer the side effect so StrictMode's setup/cleanup replay does not
      // consume the question and immediately abort its only request.
      const timer = setTimeout(() => {
        sessionStorage.removeItem('chat:pending')
        send(pending)
      }, 0)
      return () => clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function scrollToBottom() {
    stickRef.current = true
    setAtBottom(true)
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }

  function onScroll() {
    const el = scrollRef.current
    if (el.hidden) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 16
    stickRef.current = nearBottom
    setAtBottom(nearBottom)
  }

  function closeHistory() {
    setHistoryOpen(false)
    historyToggleRef.current?.focus({ preventScroll: true })
  }

  function openChat(id) {
    const offsets = positions.current
    for (const key of offsets.keys()) {
      if (!workspace.chats.some((chat) => chat.id === key)) offsets.delete(key)
    }
    if (stickRef.current) offsets.delete(workspace.chat.id)
    else offsets.set(workspace.chat.id, scrollRef.current.scrollTop)
    if (id) workspace.select(id)
    else workspace.create()
    if (historyOpen) closeHistory()
    if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) inputRef.current?.focus({ preventScroll: true })
  }

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || streamRef.current) return
    if (!messages.length) workspace.rename(q.slice(0, 28))
    setCopied(null)
    setCopyError('')
    if (text == null) setInput('')
    scrollToBottom()
    setMessages((m) => [...m, { role: 'user', text: q }, { role: 'assistant', text: '', stage: '' }])
    setBusy(true)

    const updateLast = (fn) => {
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = fn(copy[copy.length - 1])
        return copy
      })
    }

    let receivedDone = false
    let failed = false
    const stream = platformApi.agentStreamRaw(q, sessionId, (ev) => {
        if (ev.type === 'done') { receivedDone = true }
        else if (ev.type === 'stage') {
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
            stages: msg.stage && msg.stages?.length
              ? msg.stages.map((s, j) => ({ ...s, ok: j < msg.stages.length - 1 }))
              : msg.stages,
          }))
        } else if (ev.type === 'error') {
          failed = true
          updateLast((msg) => ({
            ...msg, error: ev.message, stage: '',
            stages: (msg.stages || []).map((s) => ({ ...s, ok: true })),
          }))
        }
    })
    streamRef.current = stream
    try {
      await stream.promise
      if (!receivedDone && !failed) {
        updateLast((msg) => stream.signal.aborted
          ? { ...msg, notice: '已停止生成' }
          : { ...msg, error: '连接中断，回答可能不完整。' })
      }
    } catch (err) {
      if (err.name === 'AbortError') updateLast((msg) => ({ ...msg, notice: '已停止生成' }))
      else updateLast((msg) => ({ ...msg, error: err.message, stage: '' }))
    } finally {
      updateLast((msg) => ({
        ...msg, stage: '',
        stages: (msg.stages || []).map((s) => ({ ...s, ok: true })),
      }))
      streamRef.current = null
      setBusy(false)
    }
  }

  return (
    <div className="chat-workspace">
      {historyOpen && <button type="button" className="history-backdrop" aria-label="关闭对话历史" tabIndex={-1} onClick={closeHistory} />}
      <aside ref={historyRef} id="chat-history" className={`chat-history ${historyOpen ? 'visible' : ''}`} aria-label="对话历史">
        <div className="history-heading"><span>研究对话</span><button disabled={busy} onClick={() => openChat()} title="新建对话" aria-label="新建对话">＋</button></div>
        <p className="history-caption">最近对话 · 当前标签页</p>
        <div className="history-list">
          {workspace.chats.map((chat) => <div key={chat.id} className={`history-item ${chat.id === workspace.chat.id ? 'selected' : ''}`}>
            <button disabled={busy} title={chat.title} aria-current={chat.id === workspace.chat.id ? 'true' : undefined} onClick={() => openChat(chat.id)}>
              <span className="history-title">{chat.title}</span>
            </button>
            <button className="history-delete" disabled={busy} aria-label={`删除对话 ${chat.title}`} onClick={() => workspace.remove(chat.id)}>×</button>
          </div>)}
        </div>
        <p className="history-note">记录保存在当前标签页。服务端上下文保留约 30 分钟，超时可新建对话。</p>
      </aside>
      <div className="chat-page">
      <header className="chat-toolbar">
        <div><button ref={historyToggleRef} className="history-toggle" aria-label="切换对话历史" aria-controls="chat-history" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>☰</button><span className="chat-product">Alpha Radar</span><span className="chat-toolbar-sub">智能对话</span></div>
        <button className="chat-new" disabled={busy} onClick={() => openChat()}>＋ 新对话</button>
      </header>
      <div className={`chat-session${empty ? ' is-empty' : ''}`}>
        {empty && <div className="chat-intro">
          <img src={brandMark} alt="" width="44" height="44" />
          <h1>今天，想研究什么？</h1>
        </div>}
      <div className="chat-window" ref={scrollRef} hidden={empty} onScroll={onScroll} role="region" aria-label="对话消息" tabIndex={0}>
        <div className="chat-transcript" ref={transcriptRef}>
        {messages.map((m, i) => {
          const streaming = busy && i === messages.length - 1 && m.role === 'assistant'
          return (
            <div key={i} className={`chat-msg ${m.role}`}>
              {m.role === 'assistant' ? (
                <>
                  <img className="chat-avatar" src={brandMark} alt="Alpha Radar" width="28" height="28" />
                  <div className="chat-body">
                    {INTENT_META[m.intent] && (
                      <div className={`chat-type-badge ${m.intent}`}>
                        <span className="icon">{INTENT_META[m.intent].icon}</span>
                        {INTENT_META[m.intent].label}
                      </div>
                    )}
                    {m.stages?.length > 0 ? (
                      <details className="chat-process"><summary>{streaming ? (m.stage || "正在生成回答") : `研究过程 · ${m.stages.length} 个步骤`}</summary><div className="agent-steps">
                        {m.stages.map((s, j) => {
                          // Same batch (>1 call) is a parallel group: prefix the first entry with a ⚡并行×N label, indent within the batch
                          const prev = m.stages[j - 1]
                          const parallel = s.batch != null && s.batchSize > 1
                          const firstOfBatch = parallel && (!prev || prev.batch !== s.batch)
                          return (
                            <Fragment key={j}>
                              {firstOfBatch && <div className="agent-batch-label">⚡ 并行 ×{s.batchSize}</div>}
                              <div className={`agent-step ${parallel ? 'batched' : ''} ${s.failed ? 'failed' : s.ok ? 'ok' : 'active'}`}>
                                <span className="agent-dot">{s.failed ? '⚠' : s.ok ? '✓' : ''}</span>
                                {s.message}{s.meta ? ` (${s.meta})` : ''}
                              </div>
                            </Fragment>
                          )
                        })}
                      </div></details>
                    ) : (m.stage && (
                      <div className="chat-stage">
                        <span className="spinner-dot" /> {m.stage}
                      </div>
                    ))}
                    {(m.text || (streaming && !m.stage)) && (
                      <div className={`chat-bubble md${streaming ? ' streaming' : ''}`}>
                        {m.text ? <AnswerMarkdown text={m.text} /> : (
                          <div className="chat-bubble typing"><span></span><span></span><span></span></div>
                        )}
                      </div>
                    )}
                    <ChatResults tables={m.tables} data={m.data} columns={m.columns} />
                    {m.error && <div className="chat-error" role="alert">{m.error}<button disabled={busy} onClick={() => send(messages[i - 1]?.text)}>重试</button></div>}
                    {m.notice && <div className="chat-notice">{m.notice}</div>}
                    {!streaming && m.text && <div className="chat-actions"><button onClick={() => copyAnswer(m.text, i)}>{copied === i ? '已复制' : '复制回答'}</button></div>}
                  </div>
                </>
              ) : (
                <div className="chat-bubble user">{m.text}</div>
              )}
            </div>
          )
        })}
        </div>
      </div>

      <div className="chat-inputbar">
        {!empty && !atBottom && <button className="chat-jump" onClick={() => {
          scrollToBottom()
          scrollRef.current.focus({ preventScroll: true })
        }}>↓ 回到最新</button>}
        <form className="chat-input-shell" onSubmit={(e) => { e.preventDefault(); send() }}>
          <textarea
            ref={inputRef}
            aria-label="输入研究问题"
            rows={1}
            maxLength={16000}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                e.preventDefault()
                send()
              }
            }}
            placeholder={empty ? '输入股票名称、代码或研究问题…' : '继续追问…'}
          />
          <div className="composer-tools">
            <span className="composer-shortcut">{busy ? 'Esc 停止' : 'Enter 发送 · Shift + Enter 换行'}</span>
            {busy ? <button type="button" className="chat-send stop" onClick={() => streamRef.current?.abort()} aria-label="停止生成" title="停止生成"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" /></svg></button>
              : <button type="submit" className="chat-send" disabled={!input.trim()} aria-label="发送问题" title="发送问题"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg></button>}
          </div>
        </form>
        <div className="composer-note" role="status">{copyError || (workspace.storageError ? '浏览器存储不可用，对话仅在本页面保留' : busy ? '正在生成回答…' : '')}</div>
      </div>
      {empty && <div className="chat-examples" role="group" aria-label="示例问题">
        {EXAMPLES.map((ex) => <button key={ex.label} onClick={() => send(ex.text)}>
          <span className="example-label">{ex.label}</span><span>{ex.text}</span>
        </button>)}
      </div>}
      </div>
      </div>
    </div>
  )
}
