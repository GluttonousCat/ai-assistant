import { useEffect, useMemo, useState } from 'react'
import { api } from './api'

const STATE_LABEL = {
  RANGE_FORMATION: '形成中',
  EARLY_TRADABLE_RANGE: '早期可交易',
  MATURE_RANGE: '成熟区间',
  RANGE_RENEWAL: '区间更新',
}
const STATE_CLASS = {
  RANGE_FORMATION: 'FORMATION',
  EARLY_TRADABLE_RANGE: 'EARLY',
  MATURE_RANGE: 'MATURE',
  RANGE_RENEWAL: 'RENEWAL',
}
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? '—' : Number(v).toFixed(d))
const pct = (v) => (v === null || v === undefined || isNaN(v) ? '—' : `${(v * 100).toFixed(1)}%`)

// 简易折线图 (SVG): 滚动胜率
function LineChart({ data, height = 180 }) {
  if (!data || data.length === 0) return <div className="empty">暂无成熟数据</div>
  const W = 720, H = height, P = { l: 40, r: 14, t: 14, b: 26 }
  const vals = data.map((d) => d.hq_holding_rate ?? d.holding_rate)
  const allVals = data.flatMap((d) => [d.holding_rate, d.hq_holding_rate].filter((v) => v != null))
  const maxV = Math.max(1, ...allVals), minV = Math.min(0, ...allVals)
  const x = (i) => P.l + (i / Math.max(1, data.length - 1)) * (W - P.l - P.r)
  const y = (v) => P.t + (1 - (v - minV) / (maxV - minV || 1)) * (H - P.t - P.b)
  const path = (key) =>
    data.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(d[key] ?? d.holding_rate).toFixed(1)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="linechart">
      {[0, 0.25, 0.5, 0.75, 1].map((g) => (
        <g key={g}>
          <line x1={P.l} x2={W - P.r} y1={y(g)} y2={y(g)} stroke="#2a3140" strokeWidth="1" />
          <text x={P.l - 6} y={y(g) + 4} fill="#8b949e" fontSize="10" textAnchor="end">{Math.round(g * 100)}%</text>
        </g>
      ))}
      <path d={path('holding_rate')} fill="none" stroke="#8b949e" strokeWidth="1.5" strokeDasharray="4 3" />
      <path d={path('hq_holding_rate')} fill="none" stroke="#4aa3ff" strokeWidth="2.5" />
      {data.map((d, i) => (
        <circle key={i} cx={x(i)} cy={y(d.hq_holding_rate ?? d.holding_rate)} r="3" fill="#4aa3ff" />
      ))}
      {data.map((d, i) => (
        <text key={i} x={x(i)} y={H - 8} fill="#8b949e" fontSize="9" textAnchor="middle">
          {d.date.slice(5)}
        </text>
      ))}
    </svg>
  )
}

// 简易柱状图 (SVG): 分数档维持率
function BarChart({ data, height = 180 }) {
  if (!data || data.length === 0) return <div className="empty">暂无成熟数据</div>
  const W = 480, H = height, P = { l: 40, r: 14, t: 14, b: 30 }
  const maxV = Math.max(1, ...data.map((d) => d.holding_rate))
  const bw = (W - P.l - P.r) / data.length
  const y = (v) => P.t + (1 - v / maxV) * (H - P.t - P.b)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="linechart">
      {[0, 0.25, 0.5, 0.75, 1].map((g) => (
        <g key={g}>
          <line x1={P.l} x2={W - P.r} y1={y(g)} y2={y(g)} stroke="#2a3140" strokeWidth="1" />
          <text x={P.l - 6} y={y(g) + 4} fill="#8b949e" fontSize="10" textAnchor="end">{Math.round(g * 100)}%</text>
        </g>
      ))}
      {data.map((d, i) => (
        <g key={i}>
          <rect x={P.l + i * bw + bw * 0.15} y={y(d.holding_rate)} width={bw * 0.7}
            height={Math.max(0, H - P.b - y(d.holding_rate))} fill="#4aa3ff" rx="3" opacity="0.85" />
          <text x={P.l + i * bw + bw / 2} y={y(d.holding_rate) - 5} fill="#e6edf3" fontSize="10" textAnchor="middle">
            {Math.round(d.holding_rate * 100)}%
          </text>
          <text x={P.l + i * bw + bw / 2} y={H - 14} fill="#8b949e" fontSize="10" textAnchor="middle">{d.score_bin}</text>
          <text x={P.l + i * bw + bw / 2} y={H - 3} fill="#5b6470" fontSize="8" textAnchor="middle">n={d.n}</text>
        </g>
      ))}
    </svg>
  )
}

export default function Tracking({ onOpenChart }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [minScore, setMinScore] = useState(70)
  const [horizon, setHorizon] = useState(10)

  useEffect(() => {
    setLoading(true)
    api.tracking(minScore, horizon)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [minScore, horizon])

  const ov = data?.overall || {}

  return (
    <div>
      <div className="controls">
        <div className="control">
          <span>最低分</span>
          <select value={minScore} onChange={(e) => setMinScore(+e.target.value)}>
            {[55, 60, 65, 70, 75, 80].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div className="control">
          <span>观察窗口</span>
          <div className="seg">
            {[5, 10, 20].map((h) => (
              <button key={h} className={horizon === h ? 'on' : ''} onClick={() => setHorizon(h)}>{h}日</button>
            ))}
          </div>
        </div>
      </div>

      {loading ? (
        <div className="loading"><div className="spinner" />加载中…</div>
      ) : error ? (
        <div className="error">加载失败: {error}</div>
      ) : !data ? null : (
        <>
          <div className="stats">
            <div className="stat-card"><div className="label">累计信号</div><div className="value accent">{ov.total_signals ?? '—'}</div><div className="hint">{ov.first_date} ~ {ov.last_date}</div></div>
            <div className="stat-card"><div className="label">全部维持率</div><div className="value">{pct(ov.holding_rate_all)}</div><div className="hint">{horizon}日 range_holding</div></div>
            <div className="stat-card"><div className="label">高分维持率 (≥{minScore})</div><div className="value gold">{pct(ov.holding_rate_hq)}</div><div className="hint">信号组核心指标</div></div>
            <div className="stat-card"><div className="label">近期高分维持率</div><div className="value up">{pct(ov.recent_hq_holding_rate)}</div><div className="hint">近 {ov.recent_n ?? 0} 个信号</div></div>
          </div>

          {ov.matured === 0 || !ov.total_signals ? (
            <div className="empty">
              信号已落盘跟踪，但事后指标需待未来 {horizon} 个交易日数据齐备后回填。<br />
              <span className="dim">每日 21:30 调度会自动回填 — 最早一批信号将在数据成熟后显示胜率。</span>
            </div>
          ) : (
            <div className="chart-grid">
              <div className="panel">
                <div className="panel-title">滚动胜率（虚线=全部, 实线=高分≥{minScore}）</div>
                <LineChart data={data.by_date} />
              </div>
              <div className="panel">
                <div className="panel-title">分数档维持率</div>
                <BarChart data={data.by_score_bin} />
              </div>
            </div>
          )}

          <div className="table-wrap">
            <div className="panel-title" style={{ padding: '4px 0 10px' }}>信号跟踪明细（近 100 条, {horizon}日窗口）</div>
            <table className="grid">
              <thead>
                <tr>
                  <th>信号日</th><th>标的</th><th>L1行业</th><th>L2行业</th><th>状态</th><th>分数</th><th>区间</th>
                  <th>RangePos</th><th>风险</th><th>成熟度</th><th>停留度</th><th>维持</th><th>远期收益</th>
                </tr>
              </thead>
              <tbody>
                {(data.recent_signals || []).map((r, i) => (
                  <tr key={i} onClick={() => onOpenChart && onOpenChart(r.symbol)}>
                    <td className="dim">{r.signal_date}</td>
                    <td><span className="sym">{r.name || r.symbol}<span className="code">{r.symbol}</span></span></td>
                    <td className="dim">{r.industry_l1 || '—'}</td>
                    <td className="dim">{r.industry_l2 || '—'}</td>
                    <td><span className={`badge ${STATE_CLASS[r.state]}`}>{STATE_LABEL[r.state] || r.state}</span></td>
                    <td><span className={`score-pill ${r.range_score >= 80 ? 's80' : ''}`}>{fmt(r.range_score, 0)}</span></td>
                    <td className="dim">{fmt(r.lower)} ~ {fmt(r.upper)}</td>
                    <td>{fmt(r.range_pos)}</td>
                    <td><span className={`risk ${r.trend_risk}`}>{r.trend_risk}</span></td>
                    <td>{r.matured ? <span className="dim">已成熟</span> : <span style={{ color: 'var(--gold)' }}>进行中</span>}</td>
                    <td>{r.matured ? pct(r.persist_ratio) : '—'}</td>
                    <td>{r.matured ? (r.range_holding ? <span className="up">✓</span> : <span className="down">✗</span>) : '—'}</td>
                    <td className={r.fwd_ret > 0 ? 'up' : r.fwd_ret < 0 ? 'down' : 'dim'}>{r.matured ? pct(r.fwd_ret) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
