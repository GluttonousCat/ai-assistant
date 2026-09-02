import { useEffect, useMemo, useState } from 'react'
import { api } from './api'

const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? '—' : Number(v).toFixed(d))
const pct = (v) => (v === null || v === undefined || isNaN(v) ? '—' : `${(v * 100).toFixed(1)}%`)
const pct1 = (v) => (v === null || v === undefined || isNaN(v) ? '—' : `${(v * 100).toFixed(0)}%`)

// 命中率折线图 (SVG): 逐日胜率 / 主升率
// 大盘 × 策略对照图: 上证指数收盘 (右轴) vs 逐日胜率 (左轴)
function MarketVsRate({ byDay, shSeries, height = 200 }) {
  if (!byDay || byDay.length === 0 || !shSeries || shSeries.length === 0) {
    return <div className="empty">暂无指数数据</div>
  }
  const W = 760, H = height, P = { l: 44, r: 44, t: 14, b: 26 }
  // 按日期对齐: 指数收盘取回测区间内
  const dates = byDay.map((d) => d.date)
  const shMap = {}
  for (const p of shSeries) shMap[p.t] = p.c
  // 只保留回测区间内的指数点
  const idxSeries = dates.map((d) => shMap[d]).filter((v) => v != null)
  const allIdx = idxSeries
  const maxI = Math.max(...allIdx), minI = Math.min(...allIdx)
  const iRange = (maxI - minI) || 1
  const x = (i) => P.l + (i / Math.max(1, dates.length - 1)) * (W - P.l - P.r)
  const yRate = (v) => P.t + (1 - (v ?? 0)) * (H - P.t - P.b)
  const yIdx = (v) => P.t + (1 - (v - minI) / iRange) * (H - P.t - P.b)
  const pathRate = byDay.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${yRate(d.win_rate).toFixed(1)}`).join(' ')
  const pathIdx = byDay.map((d, i) => {
    const v = shMap[d.date]
    return v == null ? null : `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${yIdx(v).toFixed(1)}`
  }).filter(Boolean).join(' ')
  const tickEvery = Math.max(1, Math.floor(dates.length / 12))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="linechart">
      {[0, 0.5, 1].map((g) => (
        <g key={g}>
          <line x1={P.l} x2={W - P.r} y1={yRate(g)} y2={yRate(g)} stroke="#2a3140" strokeWidth="1" />
          <text x={P.l - 5} y={yRate(g) + 4} fill="#8b949e" fontSize="9" textAnchor="end">{Math.round(g * 100)}%</text>
        </g>
      ))}
      {/* 指数右轴刻度 */}
      {[0, 0.5, 1].map((g) => (
        <text key={`r${g}`} x={W - P.r + 5} y={yIdx(minI + g * iRange) + 4} fill="#5b6470" fontSize="9">
          {Math.round(minI + g * iRange)}
        </text>
      ))}
      <path d={pathIdx} fill="none" stroke="#f5b942" strokeWidth="2" />
      <path d={pathRate} fill="none" stroke="#4aa3ff" strokeWidth="2" strokeDasharray="5 3" />
      {dates.map((d, i) => (
        i % tickEvery === 0 ? (
          <text key={i} x={x(i)} y={H - 8} fill="#8b949e" fontSize="8" textAnchor="middle">{d.slice(2)}</text>
        ) : null
      ))}
    </svg>
  )
}

function RateChart({ data, keyA, keyB, height = 190 }) {
  if (!data || data.length === 0) return <div className="empty">暂无数据</div>
  const W = 760, H = height, P = { l: 40, r: 14, t: 14, b: 26 }
  const maxV = Math.max(1, ...data.flatMap((d) => [d[keyA], d[keyB]].filter((v) => v != null)))
  const x = (i) => P.l + (i / Math.max(1, data.length - 1)) * (W - P.l - P.r)
  const y = (v) => P.t + (1 - (v ?? 0) / maxV) * (H - P.t - P.b)
  const path = (key) => data.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(' ')
  const tickEvery = Math.max(1, Math.floor(data.length / 14))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="linechart">
      {[0, 0.5, 1].map((g) => (
        <g key={g}>
          <line x1={P.l} x2={W - P.r} y1={y(g)} y2={y(g)} stroke="#2a3140" strokeWidth="1" />
          <text x={P.l - 6} y={y(g) + 4} fill="#8b949e" fontSize="10" textAnchor="end">{Math.round(g * 100)}%</text>
        </g>
      ))}
      <path d={path(keyA)} fill="none" stroke="#8b949e" strokeWidth="1.5" strokeDasharray="4 3" />
      <path d={path(keyB)} fill="none" stroke="#4aa3ff" strokeWidth="2.5" />
      {data.map((d, i) => (
        <g key={i}>
          {i % tickEvery === 0 && <circle cx={x(i)} cy={y(d[keyB])} r="2.5" fill="#4aa3ff" />}
        </g>
      ))}
      {data.map((d, i) => (
        i % tickEvery === 0 ? (
          <text key={i} x={x(i)} y={H - 8} fill="#8b949e" fontSize="8" textAnchor="middle">{d.date.slice(2)}</text>
        ) : null
      ))}
    </svg>
  )
}

// 每日候选数柱状图
function NBar({ data, height = 150 }) {
  if (!data || data.length === 0) return <div className="empty">暂无数据</div>
  const W = 760, H = height, P = { l: 40, r: 14, t: 14, b: 26 }
  const maxV = Math.max(1, ...data.map((d) => d.n || 0))
  const bw = (W - P.l - P.r) / data.length
  const y = (v) => P.t + (1 - v / maxV) * (H - P.t - P.b)
  const tickEvery = Math.max(1, Math.floor(data.length / 14))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="linechart">
      {data.map((d, i) => (
        <g key={i}>
          <rect x={P.l + i * bw + bw * 0.2} y={y(d.n || 0)} width={bw * 0.6}
            height={Math.max(0.5, H - P.b - y(d.n || 0))} fill="#4aa3ff" rx="2" opacity="0.8" />
          {i % tickEvery === 0 && (
            <text x={P.l + i * bw + bw / 2} y={H - 8} fill="#8b949e" fontSize="8" textAnchor="middle">{d.date.slice(2)}</text>
          )}
        </g>
      ))}
    </svg>
  )
}

export default function SetupBacktest({ onOpenChart }) {
  const [data, setData] = useState(null)
  const [market, setMarket] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    Promise.all([api.setupBacktest(), api.setupMarket().catch(() => null)])
      .then(([d, m]) => { setData(d); setMarket(m); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const overall = useMemo(() => {
    if (!data || !data.by_day || data.by_day.length === 0) return null
    const rows = data.by_day
    const n = rows.reduce((s, r) => s + (r.n || 0), 0)
    if (!n) return null
    const wmean = (k) => rows.reduce((s, r) => s + (r.n || 0) * (r[k] || 0), 0) / n
    return {
      n, days: rows.length, range: data.range || '',
      win: wmean('win_rate'), surge: wmean('surge_rate'),
      mfe: wmean('avg_mfe'), ret: wmean('avg_close_ret'),
    }
  }, [data])

  return (
    <div>
      {loading ? (
        <div className="loading"><div className="spinner" />加载中…</div>
      ) : error ? (
        <div className="error">加载失败: {error}</div>
      ) : !data || !data.by_day || data.by_day.length === 0 ? (
        <div className="empty">
          尚无历史回测数据。<br />
          <span className="dim">运行 `python -m range_trading.scanner.setup_backtest_daily` 生成逐日回测。</span>
        </div>
      ) : (
        <>
          {overall && (
            <div className="stats">
              <div className="stat-card"><div className="label">回测区间</div><div className="value" style={{ fontSize: 18 }}>{overall.range}</div><div className="hint">{overall.days} 个交易日</div></div>
              <div className="stat-card"><div className="label">总候选</div><div className="value accent">{overall.n.toLocaleString()}</div><div className="hint">全市场逐日扫描</div></div>
              <div className="stat-card"><div className="label">胜率 (20日收正)</div><div className="value">{pct1(overall.win)}</div><div className="hint">整体</div></div>
              <div className="stat-card"><div className="label">主升率</div><div className="value" style={{ color: 'var(--gold)' }}>{pct1(overall.surge)}</div><div className="hint">MFE≥8% 且回撤可控</div></div>
              <div className="stat-card"><div className="label">平均 MFE</div><div className="value up">{pct(overall.mfe)}</div><div className="hint">平均收盘 {pct(overall.ret)}</div></div>
            </div>
          )}

          {/* 大盘指引 */}
          {market && market.series && market.series['000001.SH'] && (
            <div className="panel" style={{ margin: '0 28px 16px' }}>
              <div className="panel-title">
                大盘指引 · 上证指数 {market.latest_date} 收 {market.latest_close}
                <span className={`badge ${market.state === 'BULL' ? 'EST' : market.state === 'BEAR' ? 'EXH' : 'RENEWAL'}`} style={{ marginLeft: 10 }}>
                  {market.state_cn}
                </span>
                <span className="dim" style={{ marginLeft: 10, fontSize: 11 }}>策略失效期(2月中)与指数下杀同步 — 大盘弱势时蓄势候选应降权</span>
              </div>
              <MarketVsRate byDay={data.by_day} shSeries={market.series['000001.SH']} />
            </div>
          )}

          <div className="chart-grid">
            <div className="panel">
              <div className="panel-title">逐日表现（虚线=胜率, 实线=主升率）</div>
              <RateChart data={data.by_day} keyA="win_rate" keyB="surge_rate" />
            </div>
            <div className="panel">
              <div className="panel-title">每日候选数</div>
              <NBar data={data.by_day} />
            </div>
          </div>

          <div className="table-wrap">
            <div className="panel-title" style={{ padding: '4px 0 10px' }}>逐日汇总</div>
            <table className="grid">
              <thead>
                <tr>
                  <th>日期</th><th>候选数</th><th>胜率</th><th>主升率</th><th>平均MFE</th><th>平均收盘</th><th>涨停数</th>
                </tr>
              </thead>
              <tbody>
                {data.by_day.map((r) => (
                  <tr key={r.date}>
                    <td className="dim">{r.date}</td>
                    <td>{r.n != null ? r.n.toLocaleString() : '—'}</td>
                    <td className={r.win_rate >= 0.6 ? 'up' : r.win_rate < 0.4 ? 'down' : ''}>{pct(r.win_rate)}</td>
                    <td className={r.surge_rate >= 0.5 ? 'gold' : ''}>{pct(r.surge_rate)}</td>
                    <td className="dim">{pct(r.avg_mfe)}</td>
                    <td className={r.avg_close_ret > 0 ? 'up' : r.avg_close_ret < 0 ? 'down' : 'dim'}>{pct(r.avg_close_ret)}</td>
                    <td>{r.limit_up_n != null ? r.limit_up_n : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {data.detail && data.detail.length > 0 && (
            <div className="table-wrap">
              <div className="panel-title" style={{ padding: '4px 0 10px' }}>候选明细 (最近 500 条, 20日窗口)</div>
              <table className="grid">
                <thead>
                  <tr>
                    <th>候选日</th><th>标的</th><th>蓄势分</th><th>涨停</th><th>承接量比</th><th>MFE</th><th>MAE</th><th>收盘涨幅</th><th>主升</th>
                  </tr>
                </thead>
                <tbody>
                  {data.detail.map((r, i) => (
                    <tr key={i} onClick={() => onOpenChart && onOpenChart(r.symbol)} style={{ cursor: 'pointer' }}>
                      <td className="dim">{String(r.date).slice(0, 10)}</td>
                      <td><span className="sym">{r.name || r.symbol}<span className="code">{r.symbol}</span></span></td>
                      <td>{r.base_score != null ? r.base_score.toFixed(0) : '—'}</td>
                      <td>{r.limit_up ? <span className="up">Y</span> : <span className="dim">N</span>}</td>
                      <td>{r.rally_vol != null ? r.rally_vol.toFixed(1) : '—'}</td>
                      <td className="up">{pct(r.mfe)}</td>
                      <td className="down">{pct(r.mae)}</td>
                      <td className={r.close_ret > 0 ? 'up' : r.close_ret < 0 ? 'down' : 'dim'}>{pct(r.close_ret)}</td>
                      <td>{r.surge ? <span className="up">✓</span> : <span className="down">✗</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}