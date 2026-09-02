import { useEffect, useMemo, useState, useCallback } from 'react'
import { api } from './api'
import SetupBacktest from './SetupBacktest'

const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? '—' : Number(v).toFixed(d))
const pct = (v, d = 1) => (v === null || v === undefined || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`)

function scoreColor(s) {
  if (s >= 75) return 'var(--up)'        // 已偏热
  if (s >= 60) return 'var(--accent)'    // 黄金区间
  if (s >= 55) return 'var(--gold)'
  return 'var(--text-dim)'
}

export default function Setup({ onOpenChart, onDateChange }) {
  const [subTab, setSubTab] = useState('signals')   // signals | backtest
  const [data, setData] = useState(null)
  const [market, setMarket] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [detector, setDetector] = useState('all')   // all | base | momentum
  const [minScore, setMinScore] = useState(55)
  const [maxScore, setMaxScore] = useState(75)
  const [search, setSearch] = useState('')
  const [scanning, setScanning] = useState(false)
  const [boardFilter, setBoardFilter] = useState([])   // 多选: [] = 全部
  const [mvFilter, setMvFilter] = useState(false)      // true = 市值>100亿

  useEffect(() => {
    api.setupMarket().then(setMarket).catch(() => {})
  }, [])

  const load = useCallback(async (lo = minScore, hi = maxScore, det = detector) => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.setupLatest(lo, hi, det)
      setData(res)
      if (onDateChange) onDateChange(res.date)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [minScore, maxScore, detector])

  useEffect(() => { load() }, [])

  const filtered = useMemo(() => {
    if (!data) return []
    let rows = data.records || []
    if (search) {
      const q = search.trim().toLowerCase()
      rows = rows.filter((r) =>
        (r.symbol || '').toLowerCase().includes(q) ||
        (r.name || '').toLowerCase().includes(q))
    }
    if (boardFilter.length > 0) rows = rows.filter((r) => boardFilter.includes(r.board))
    if (mvFilter) rows = rows.filter((r) => r.total_mv_yi != null && r.total_mv_yi > 100)
    return [...rows].sort((a, b) => b.base_score - a.base_score)
  }, [data, search, boardFilter, mvFilter])

  const toggleBoard = (b) =>
    setBoardFilter((prev) => prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b])

  const doScan = async () => {
    setScanning(true)
    try {
      await api.setupTriggerScan()
      setTimeout(() => { load(); setScanning(false) }, 3000)
    } catch { setScanning(false) }
  }

  return (
    <div>
      <div className="controls" style={{ paddingBottom: 0 }}>
        <div className="seg subtabs">
          <button className={subTab === 'signals' ? 'on' : ''} onClick={() => setSubTab('signals')}>当日候选</button>
          <button className={subTab === 'backtest' ? 'on' : ''} onClick={() => setSubTab('backtest')}>历史回测</button>
        </div>
      </div>
      {subTab === 'backtest' ? (
        <SetupBacktest onOpenChart={onOpenChart} />
      ) : (
      <>
      <div className="controls">
        <div className="control">
          <span>识别器</span>
          <div className="seg">
            {[['all', '全部'], ['base', '蓄势盾'], ['momentum', '动量矛']].map(([k, l]) => (
              <button key={k} className={detector === k ? 'on' : ''}
                onClick={() => { setDetector(k); load(minScore, maxScore, k) }}>{l}</button>
            ))}
          </div>
        </div>
        <div className="control">
          <span>分数下限</span>
          <select value={minScore} onChange={(e) => { setMinScore(+e.target.value); load(+e.target.value, maxScore) }}>
            {[50, 55, 60, 65].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div className="control">
          <span>分数上限</span>
          <select value={maxScore} onChange={(e) => { setMaxScore(+e.target.value); load(minScore, +e.target.value) }}>
            {[70, 75, 80, 85].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div className="control">
          <span>搜索</span>
          <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="代码 / 名称" style={{ width: 140 }} />
        </div>
        <div className="control">
          <span>板块</span>
          <div className="seg">
            {[['main', '主板'], ['cyb', '创业板'], ['kcb', '科创板']].map(([k, l]) => (
              <button key={k} className={boardFilter.includes(k) ? 'on' : ''} onClick={() => toggleBoard(k)}>{l}</button>
            ))}
          </div>
        </div>
        <div className="control">
          <span>市值</span>
          <div className="seg">
            <button className={mvFilter ? 'on' : ''} onClick={() => setMvFilter(!mvFilter)}>＞100亿</button>
          </div>
        </div>
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn" onClick={doScan} disabled={scanning}>
          {scanning ? '扫描中…' : '立即扫描'}
        </button>
      </div>

      {/* 大盘状态条 */}
      {market && (
        <div className="panel" style={{ margin: '0 28px 12px', padding: '12px 18px' }}>
          <div className="panel-title" style={{ marginBottom: 0 }}>
            大盘指引 · 上证指数 {market.latest_date} 收 {market.latest_close}
            <span className={`badge ${market.state === 'BULL' ? 'EST' : market.state === 'BEAR' ? 'EXH' : 'RENEWAL'}`} style={{ marginLeft: 10 }}>
              {market.state_cn}
            </span>
            {market.state === 'BEAR' && (
              <span className="dim" style={{ marginLeft: 10, fontSize: 11 }}>⚠ 大盘弱势, 蓄势候选成功率下降, 谨慎</span>
            )}
            {market.state === 'BULL' && (
              <span className="dim" style={{ marginLeft: 10, fontSize: 11 }}>✓ 大盘强势, 蓄势候选环境友好</span>
            )}
          </div>
        </div>
      )}

      {data && (
        <div className="stats">
          <div className="stat-card"><div className="label">蓄势候选</div><div className="value accent">{data.count}</div><div className="hint">{data.date} · DIGESTING</div></div>
          <div className="stat-card"><div className="label">黄金区间 (60-70分)</div><div className="value" style={{ color: 'var(--accent)' }}>
            {(data.records || []).filter((r) => r.base_score >= 60 && r.base_score < 70).length}
          </div><div className="hint">回测最优带 (主升率 53%)</div></div>
          <div className="stat-card"><div className="label">偏热 (≥75分)</div><div className="value" style={{ color: 'var(--up)' }}>
            {(data.records || []).filter((r) => r.base_score >= 75).length}
          </div><div className="hint">接近启动尾声, 谨慎</div></div>
          <div className="stat-card"><div className="label">强承接 (量比≥2)</div><div className="value" style={{ color: 'var(--gold)' }}>
            {(data.records || []).filter((r) => r.rally_vol >= 2).length}
          </div><div className="hint">上涨日量显著占优</div></div>
        </div>
      )}

      <div className="table-wrap">
        {loading ? (
          <div className="loading"><div className="spinner" />加载中…</div>
        ) : error ? (
          <div className="error">加载失败: {error}</div>
        ) : !filtered.length ? (
          <div className="empty">当前筛选下无候选</div>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>标的</th><th>板块</th><th>识别器</th>
                {detector === 'momentum' ? (
                  <>
                    <th>动量分</th><th>平台</th><th>回撤7日</th><th>量能</th><th>涨停5日</th><th>妖股</th>
                  </>
                ) : (
                  <>
                    <th>蓄势分</th><th>承接</th><th>消化</th><th>结构</th>
                    <th>反弹</th><th>量比</th><th>缩量</th><th>振幅</th>
                  </>
                )}
                <th>市值</th><th>行业</th><th>收盘</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.symbol} onClick={() => onOpenChart && onOpenChart(r.symbol)}>
                  <td><span className="sym">{r.name || r.symbol}<span className="code">{r.symbol}</span></span></td>
                  <td><span className={`badge ${r.board === 'cyb' ? 'BO' : r.board === 'kcb' ? 'FORM' : 'RENEWAL'}`}>
                    {{ main: '主板', cyb: '创业板', kcb: '科创板' }[r.board] || r.board}
                  </span></td>
                  <td><span className={`badge ${r.detector === 'momentum' ? 'EST' : r.detector === 'both' ? 'FORM' : 'DIG'}`}>
                    {{ momentum: '动量矛', base: '蓄势盾', both: '双信号' }[r.detector] || '—'}
                  </span></td>
                  {detector === 'momentum' ? (
                    <>
                      <td><span className="score-pill" style={{ color: scoreColor(r.m_weighted_score || 0), background: 'rgba(74,163,255,0.1)' }}>{fmt(r.m_weighted_score, 0)}</span></td>
                      <td><span className={`badge ${r.momentum_state === 'READY' ? 'EST' : r.momentum_state === 'PLATEAU' ? 'FORM' : 'RENEWAL'}`}>
                        {{ READY: '待突破', PLATEAU: '平台', IGNITED: '点火', NONE: '—' }[r.momentum_state] || r.momentum_state}
                      </span></td>
                      <td className={r.m_dd7 >= -0.08 ? 'up' : 'down'}>{pct(r.m_dd7)}</td>
                      <td className={r.m_vol_ratio >= 0.9 ? 'up' : ''}>{fmt(r.m_vol_ratio)}</td>
                      <td>{r.m_limit5 != null ? fmt(r.m_limit5, 0) : '—'}</td>
                      <td>{r.m_absurd ? <span className="gold">⚠妖</span> : <span className="dim">否</span>}</td>
                    </>
                  ) : (
                    <>
                      <td><span className="score-pill" style={{ color: scoreColor(r.base_score), background: 'rgba(74,163,255,0.1)' }}>{fmt(r.base_score, 0)}</span></td>
                      <td className="dim">{fmt(r.score_a, 0)}</td>
                      <td className="dim">{fmt(r.score_b, 0)}</td>
                      <td className="dim">{fmt(r.score_c, 0)}</td>
                      <td className={r.cum_rally >= 0.1 ? 'up' : ''}>{pct(r.cum_rally)}</td>
                      <td className={r.rally_vol >= 2 ? 'up' : ''}>{fmt(r.rally_vol)}</td>
                      <td className={r.vol_shrink < 0.7 ? 'dim' : ''}>{fmt(r.vol_shrink)}</td>
                      <td className="dim">{pct(r.range_pct)}</td>
                    </>
                  )}
                  <td>{r.total_mv_yi != null ? `${fmt(r.total_mv_yi, 0)}亿` : '—'}</td>
                  <td className="dim">{r.m_industry || r.industry_l2 || r.industry_l1 || '—'}</td>
                  <td>{fmt(r.close)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      </>
      )}
    </div>
  )
}