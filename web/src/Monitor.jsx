// Real-time monitor page (adminOnly): one SSE frame every 2s — agent sessions/funnel/tools/LLM + PG data freshness
import { useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'

const PATH_LABEL = { fast: '快车道', subagent: '域子Agent', full: '全工具循环' }
const EV_ICON = { session_start: '▶', session_end: '■', tool: '⚡', llm_fail: '✖', error: '✖' }

const ago = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts)
  return s < 60 ? `${s | 0}s前` : s < 3600 ? `${(s / 60) | 0}m前` : `${(s / 3600) | 0}h前`
}
const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`)
const fmtTok = (n) => (n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const fmtUp = (s) => `${(s / 3600) | 0}h ${((s % 3600) / 60) | 0}m`

function Lamp({ ok, label, sub }) {
  return (
    <div className={`mon-lamp ${ok ? 'ok' : 'bad'}`}>
      <span className="lamp-dot" />
      <div>
        <div className="lamp-label">{label}</div>
        <div className="lamp-sub">{sub}</div>
      </div>
    </div>
  )
}

function Card({ value, label, sub, warn }) {
  return (
    <div className={`mon-card${warn ? ' warn' : ''}`}>
      <div className="card-value">{value}</div>
      <div className="card-label">{label}</div>
      {sub && <div className="card-sub">{sub}</div>}
    </div>
  )
}

export default function Monitor() {
  const [st, setSt] = useState(null)
  const [live, setLive] = useState(true)
  const [err, setErr] = useState('')
  const streamRef = useRef(null)

  useEffect(() => {
    if (!live) return undefined
    let stopped = false
    let timer = null
    const start = () => {
      if (stopped) return
      setErr('')
      const handle = platformApi.monitorStream((ev) => {
        if (ev.state) setSt(ev.state)
      })
      streamRef.current = handle
      // Auto-reconnect (3s) after stream drop/server restart; `stopped` blocks reconnection on unmount or pause
      const retry = (msg) => {
        if (stopped) return
        setErr(msg)
        timer = setTimeout(start, 3000)
      }
      handle.promise.then(
        () => retry(''),
        (e) => retry(e && e.name === 'AbortError' ? '' : String(e.message || e)),
      )
    }
    start()
    return () => {
      stopped = true
      clearTimeout(timer)
      streamRef.current?.abort()
    }
  }, [live])

  if (!st) {
    return <div className="monitor"><div className="mon-empty">{err || '连接监控流…'}</div></div>
  }

  const m = st.metrics || {}
  const pg = st.pg || {}
  const c = m.counters || {}
  const sessions = m.sessions || []
  const active = sessions.filter((s) => s.status === 'running')
  const funnel = m.funnel || {}
  const funnelTotal = Object.values(funnel).reduce((a, b) => a + b, 0) || 1
  const tools = Object.entries(m.tools || {}).sort((a, b) => b[1].calls - a[1].calls).slice(0, 10)
  const llms = Object.entries(m.llm || {}).sort((a, b) => b[1].calls - a[1].calls)
  const tokenTotal = llms.reduce((a, [, v]) => a + (v.pt || 0) + (v.ct || 0), 0)
  const failRate = c.tool_calls ? ((c.tool_fails || 0) / c.tool_calls * 100).toFixed(1) : '0.0'
  const events = [...(m.events || [])].reverse()
  const pipe = pg.pipeline || {}

  const evText = (e) => {
    if (e.event === 'session_start') return `[${PATH_LABEL[e.path] || e.path}] ${e.text || ''}`
    if (e.event === 'tool') return `${e.ok ? '✓' : '✗'} ${e.tool} · ${fmtMs(e.ms || 0)}`
    if (e.event === 'session_end') return `会话结束 ${e.ok ? '✓' : '✗'} ${e.duration_s ?? ''}s`
    if (e.event === 'llm_fail') return `LLM 失败 (${e.purpose})`
    return e.error || e.reason || e.event
  }

  return (
    <div className="monitor">
      <header className="mon-head">
        <div>
          <h2>
            实时监控
            <span className={`live-dot ${live ? 'on' : ''}`} />
            <span className="live-word">{live ? 'LIVE' : '已暂停'}</span>
          </h2>
          <p className="mon-sub">
            自服务启动 {fmtUp(m.uptime_s || 0)} · 口径 {m.day} · {err && <span className="mon-err">{err}</span>}
          </p>
        </div>
        <button className="btn ghost" onClick={() => setLive(!live)}>{live ? '暂停' : '恢复'}</button>
      </header>

      <div className="mon-lamps">
        <Lamp ok={pg.ok} label="PostgreSQL" sub={pg.ok ? `${pg.latency_ms}ms` : (pg.error || '不可达')} />
        <Lamp ok={pipe.ocr === 0} label="研报管线" sub={pg.ok ? `OCR排队 ${pipe.ocr || 0} · 提取失败 ${pipe.failed || 0}` : '—'} />
        <Lamp ok={!!pg.last_trade_date} label="行情数据" sub={pg.last_trade_date ? `最新交易日 ${pg.last_trade_date}` : '—'} />
        <Lamp ok={(c.llm_fails || 0) < 5} label="LLM 网关" sub={`${c.llm_calls || 0} 调用 · 失败 ${c.llm_fails || 0}`} />
      </div>

      <div className="mon-cards">
        <Card value={c.sessions || 0} label="会话 (今日)" sub={`完成 ${c.session_ends || 0}`} />
        <Card value={active.length} label="进行中" sub={active.length ? active.map((s) => PATH_LABEL[s.path]).join(' / ') : '空闲'} />
        <Card value={c.tool_calls || 0} label="工具调用" sub={`失败率 ${failRate}%`} warn={parseFloat(failRate) > 20} />
        <Card value={fmtTok(tokenTotal)} label="LLM tokens" sub={`${c.llm_calls || 0} 次调用`} />
        <Card value={c.escalates || 0} label="域升级" sub="子Agent→全工具" warn={(c.escalates || 0) > 0} />
        <Card value={fmtTok(pipe.translated || 0)} label="研报中文译文" sub={`共 ${pipe.total || 0} 篇`} />
      </div>

      <div className="mon-grid">
        <section className="mon-panel">
          <h3>三级漏斗分布</h3>
          {Object.entries(PATH_LABEL).map(([k, label]) => {
            const n = funnel[k] || 0
            return (
              <div className="funnel-row" key={k}>
                <span className="funnel-label">{label}</span>
                <div className="funnel-bar"><i style={{ width: `${(n / funnelTotal) * 100}%` }} /></div>
                <span className="funnel-num">{n}</span>
              </div>
            )
          })}
        </section>

        <section className="mon-panel">
          <h3>活跃会话</h3>
          {sessions.length === 0 && <p className="mon-dim">暂无 (重启后清零)</p>}
          <table className="mon-table">
            <tbody>
              {sessions.slice(0, 8).map((s, i) => (
                <tr key={i}>
                  <td><span className={`path-badge ${s.path}`}>{PATH_LABEL[s.path] || s.path}</span></td>
                  <td className="mon-ellipsis" title={s.text}>{s.text || '—'}</td>
                  <td className="num">R{s.rounds || 0}</td>
                  <td className="num">{s.tools || 0} 工具</td>
                  <td className="num">{ago(s.start)}</td>
                  <td><span className={`st-dot ${s.status}`} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="mon-panel">
          <h3>工具排行</h3>
          {tools.length === 0 && <p className="mon-dim">暂无调用</p>}
          <table className="mon-table">
            <thead><tr><th>工具</th><th className="num">调用</th><th className="num">失败</th><th className="num">均耗时</th></tr></thead>
            <tbody>
              {tools.map(([name, v]) => (
                <tr key={name} className={v.fails > 0 ? 'has-fail' : ''}>
                  <td className="mon-ellipsis">{name}</td>
                  <td className="num">{v.calls}</td>
                  <td className="num">{v.fails}</td>
                  <td className="num">{fmtMs(v.calls ? Math.round(v.ms / v.calls) : 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="mon-panel">
          <h3>LLM 用途</h3>
          {llms.length === 0 && <p className="mon-dim">暂无调用</p>}
          <table className="mon-table">
            <thead><tr><th>用途</th><th className="num">调用</th><th className="num">失败</th><th className="num">tokens</th></tr></thead>
            <tbody>
              {llms.map(([p, v]) => (
                <tr key={p} className={v.fails > 0 ? 'has-fail' : ''}>
                  <td>{p}</td>
                  <td className="num">{v.calls}</td>
                  <td className="num">{v.fails}</td>
                  <td className="num">{fmtTok((v.pt || 0) + (v.ct || 0))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="mon-panel mon-events">
          <h3>事件流</h3>
          {events.length === 0 && <p className="mon-dim">暂无事件</p>}
          {events.map((e, i) => (
            <div className="ev-row" key={i}>
              <span className="ev-ts">{ago(e.ts)}</span>
              <span className={`ev-ic ${e.event}`}>{EV_ICON[e.event] || '·'}</span>
              <span className={`ev-text ${e.ok === false ? 'bad' : ''}`}>{evText(e)}</span>
            </div>
          ))}
        </section>

        <section className="mon-panel">
          <h3>数据新鲜度</h3>
          <table className="mon-table">
            <tbody>
              {(pg.freshness || []).map((r) => (
                <tr key={r.table_name}>
                  <td>{r.table_name}</td>
                  <td className="num">{r.updated_at ? ago(new Date(r.updated_at).getTime() / 1000) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
