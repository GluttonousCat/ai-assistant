import { useEffect, useRef, useState } from 'react'
import { platformApi } from '../platformApi'

const STATUS = { running: '运行中', queued: '排队中', completed: '已完成', complete: '已完成', error: '失败', failed: '失败', partial: '部分完成', interrupted: '已中断', skipped: '已跳过', unknown: '待确认', sent: '已发送', uncertain: '发送待确认', prepared: '已准备', sending: '发送中', retry: '等待重试', collecting: '采集中', scanning: '扫描中' }
const time = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const duration = (row) => {
  const seconds = Math.max(0, ((row.ended_at ? new Date(row.ended_at) : Date.now()) - new Date(row.started_at)) / 1000)
  return seconds < 60 ? `${Math.floor(seconds)}秒` : seconds < 3600 ? `${Math.floor(seconds / 60)}分钟` : `${(seconds / 3600).toFixed(1)}小时`
}

function Task({ row, active = false }) {
  const progress = Number.isFinite(row.completed) && row.total > 0
  return <details className={`obs-task ${row.status}`} open={active || undefined}>
    <summary>
      <span className={`obs-status ${row.status}`}>{STATUS[row.status] || row.status}</span>
      <strong>{row.label}</strong><span className="obs-stage">{row.stage}</span>
      <span className="obs-time" title={`开始：${time(row.started_at)}\n更新：${time(row.updated_at)}`}>{time(row.updated_at)} · {duration(row)}</span>
    </summary>
    <div className="obs-task-body">
      {progress && <div className="obs-progress"><progress max={row.total} value={row.completed} /><span>{row.completed} / {row.total} · {Math.min(100, row.completed / row.total * 100).toFixed(0)}%</span></div>}
      {row.warning && <p className="mon-err">{row.warning}</p>}
      <p className="mon-dim">开始 {time(row.started_at)} · 最近更新 {time(row.updated_at)}{row.pid ? ` · PID ${row.pid}` : ''}{row.parent_id ? ' · 流水线子任务' : ''}</p>
      {!!row.events?.length && <ol className="obs-events">{row.events.slice(-12).reverse().map((event, i) => <li key={i}><time>{time(event.at)}</time><span>{event.text}</span></li>)}</ol>}
    </div>
  </details>
}

function LogViewer({ live }) {
  const [source, setSource] = useState('jobs')
  const [level, setLevel] = useState('ALL')
  const [search, setSearch] = useState('')
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [follow, setFollow] = useState(true)
  const [reload, setReload] = useState(0)
  const terminal = useRef(null)
  useEffect(() => {
    let disposed = false
    let timer
    const controller = new AbortController()
    setData(null)
    const poll = async () => {
      try {
        const next = await platformApi.monitorLogs({ source, level, search, limit: 500 }, controller.signal)
        if (!disposed) { setData(next); setError('') }
      } catch (e) {
        if (!disposed && e.name !== 'AbortError') setError(e.message)
      } finally {
        if (!disposed && live) timer = setTimeout(poll, 2000)
      }
    }
    timer = setTimeout(poll, 300)
    return () => { disposed = true; clearTimeout(timer); controller.abort() }
  }, [source, level, search, live, reload])
  useEffect(() => {
    if (follow && terminal.current) terminal.current.scrollTop = terminal.current.scrollHeight
  }, [data, follow])
  const sources = data?.sources || { jobs: '后台任务', tools: '采集与工具', skills: '领域分析', harness: 'Agent 引擎', agent: 'Agent 历史', api: 'API', storage: '存储', core: '基础设施', app: '应用', cli: '命令行' }
  return <section className="obs-logs">
    <div className="obs-toolbar">
      <select aria-label="日志来源" value={source} onChange={(e) => setSource(e.target.value)}>{Object.entries(sources).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
      <select aria-label="日志级别" value={level} onChange={(e) => setLevel(e.target.value)}>{['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((key) => <option key={key} value={key}>{key === 'ALL' ? '全部级别' : key}</option>)}</select>
      <input aria-label="搜索日志" placeholder="搜索模块、股票代码或错误…" value={search} maxLength={120} onChange={(e) => setSearch(e.target.value)} />
      <label><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />跟随末尾</label>
      <button className="btn ghost" onClick={() => setReload((n) => n + 1)}>刷新</button>
    </div>
    <div className="obs-log-meta"><span>最新500行 · {data?.truncated ? '已截取日志尾部 · ' : ''}敏感字段已脱敏</span><span>{data ? `更新于 ${time(data.updated_at)}` : '读取日志…'}</span></div>
    {error && <p className="mon-err" role="alert">{error}</p>}
    <div className="obs-terminal" ref={terminal} tabIndex={0} aria-label="运行日志">
      {data?.lines.map((line, index) => <div key={index} className={`obs-line ${line.level}`}>{line.text || ' '}</div>)}
      {data && !data.lines.length && <p className="mon-dim">暂无匹配日志，可切换来源或清空筛选条件。</p>}
    </div>
  </section>
}

export default function BackgroundMonitor({ state, mode, live }) {
  const [kind, setKind] = useState('ALL')
  const [onlyErrors, setOnlyErrors] = useState(false)
  if (mode === 'logs') return <LogViewer live={live} />
  const active = state?.active || []
  const history = state?.history || []
  const all = [...active, ...history]
  const kinds = new Map(all.map((row) => [row.kind, row.label]))
  const matches = (row) => (kind === 'ALL' || row.kind === kind) && (!onlyErrors || ['failed', 'partial', 'interrupted', 'unknown'].includes(row.status))
  const failures = history.filter((row) => ['failed', 'partial', 'interrupted', 'unknown'].includes(row.status)).length
  return <div className="obs-background">
    <div className="obs-summary"><span><b>{active.length}</b> 正在运行 / 排队</span><span><b>{history.filter((row) => row.status === 'completed').length}</b> 最近完成</span><span className={failures ? 'mon-err' : ''}><b>{failures}</b> 需关注</span><small>最近运行记录 · 每2秒更新</small></div>
    <div className="obs-receipts">{state?.receipts?.map((receipt) => <div key={receipt.label}><strong>{receipt.label}</strong><span>{STATUS[receipt.status] || receipt.status || '暂无回执'}</span><p>{receipt.detail}</p><small>回执更新 {time(receipt.updated_at)}</small></div>)}</div>
    <div className="obs-toolbar"><select aria-label="任务类型" value={kind} onChange={(e) => setKind(e.target.value)}><option value="ALL">全部任务类型</option>{[...kinds].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><label><input type="checkbox" checked={onlyErrors} onChange={(e) => setOnlyErrors(e.target.checked)} />只看异常</label><span className="mon-dim">展开任务查看阶段记录；下载反检测等待属于正常运行。</span></div>
    <h3>当前任务</h3>
    {active.filter(matches).map((row) => <Task key={row.id} row={row} active />)}
    {!active.filter(matches).length && <div className="obs-idle">当前没有匹配的运行任务，后台将按原定计划执行。</div>}
    <h3>最近运行</h3>
    {history.filter(matches).map((row) => <Task key={row.id} row={row} />)}
    {!history.filter(matches).length && <div className="obs-idle">暂无匹配记录。新任务执行后会自动出现在这里，历史任务不会补记为新运行。</div>}
  </div>
}
