import { useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'
import ResultChart, { buildChartSpec, COL_LABELS, parseNum, fmtDateCol } from './ResultChart'
import Markdownish from './Markdownish'

const EXAMPLES = [
  { icon: '🤖', text: '中际旭创2025年报净利润多少？' },
  { icon: '🔗', text: 'AI算力产业链有哪些环节' },
  { icon: '🎯', text: '中际旭创的预期差' },
  { icon: '🛡️', text: '中际旭创有没有财务风险' },
]

// 回答类型徽章: SSE data 事件携带 intent, 前端按类型显示图标
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
    return String(parseFloat(n.toFixed(4))) // 去掉浮点尾噪
  }
  return String(v)
}

// 结果呈现: 可视化优先 (折线/柱状), 不可图示时回退精简表格
// 单值列 (股票名/报告期) 折叠为标题; ts_code 一律隐藏
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

// Agent 对话: 自然语言 -> Agent 自主决策+工具循环 (SSE 流式输出, 支持多轮追问)
export default function AgentChat() {
  const [messages, setMessages] = useState([]) // {role, text, data?, columns?, stage?, intent?}
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef(null)
  const stickRef = useRef(true) // 用户贴底时跟随新内容, 上翻查看时不打扰
  // 会话 ID: 每次进入页面生成一次, 全程携带 — 服务端据此注入多轮历史 (追问可省略主语)
  const sessionRef = useRef(null)
  if (!sessionRef.current) {
    sessionRef.current = 'web-' + (crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`)
  }

  // 跨页联动: 其他页面 (如产业链页标的 chip) 写入 chat:pending 后跳转过来, 自动发出提问
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

  // 流式输出时贴底跟随; 用户上翻后不再强制拉回
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

    // 流式消息: text 逐块增长, stage 显示当前阶段
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
            // 首个 stage "意图识别: X" 提前确定回答类型 (徽章即时显示)
            const im = /^意图识别:\s*(\w+)/.exec(ev.message || '')
            // 步骤卡片: 已有步骤标 ok, 新步骤 active
            const stages = [
              ...(msg.stages || []).map((s) => ({ ...s, ok: true })),
              { message: ev.message, ok: false },
            ]
            return { ...msg, stage: ev.message, stages, intent: im ? im[1] : msg.intent }
          })
        } else if (ev.type === 'tool_call') {
          // 工具执行完: 给最后一个步骤补上耗时/行数, 失败标红
          updateLast((msg) => {
            const stages = (msg.stages || []).map((s, j, arr) => (
              j === arr.length - 1
                ? { ...s, done: true, meta: ev.ok ? `${ev.elapsed_ms}ms${ev.rows ? ` · ${ev.rows}行` : ''}` : '失败', failed: !ev.ok }
                : s
            ))
            return { ...msg, stages }
          })
        } else if (ev.type === 'data') {
          // 表格型结果累积为卡片流 (复合回答可能多张图表, 保留最近 4 张)
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
          // 结果卡片流: agent 复合回答可能产生多张表格/图表 (保留最近 4 张)
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
                        {m.stages.map((s, j) => (
                          <div key={j} className={`agent-step ${s.failed ? 'failed' : s.ok ? 'ok' : 'active'}`}>
                            <span className="agent-dot">{s.ok ? '✓' : s.failed ? '⚠' : ''}</span>
                            {s.message}{s.meta ? ` (${s.meta})` : ''}
                          </div>
                        ))}
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
