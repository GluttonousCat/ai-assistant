import { useEffect, useState } from 'react'
import { platformApi } from '../platformApi'
import EquityChart from './EquityChart'
import './quant.css'

const number = (v, digits = 2) => v == null ? '—' : Number(v).toFixed(digits)
const percent = (v) => v == null ? '—' : `${number(v)}%`
const amount = (v) => v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const period = (v) => v === '1D' ? '日K' : v
const mode = (v) => v === 'observed_only' ? '仅已知名单' : '固定名单研究'
const family = (v) => v === 'trend_pullback' ? '趋势回调' : '阴跌/砸坑反弹'
const moment = (v) => String(v || '—').replace('T', ' ').slice(0, 16)
const reasons = { stop_loss: '结构止损', take_profit: '2R止盈', trend_break: '均线失守', holding_timeout: '持仓超时', t1_deferred_stop: 'T+1延后止损' }
const PAGE_SIZE = 30

export default function QuantDashboard() {
  const [strategies, setStrategies] = useState([])
  const [runs, setRuns] = useState([])
  const [runId, setRunId] = useState('')
  const [detail, setDetail] = useState(null)
  const [page, setPage] = useState(0)
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(true)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    setBusy(true); setError('')
    Promise.all([platformApi.quantStrategies(), platformApi.quantBacktests()]).then(([catalog, results]) => {
      if (!active) return
      setStrategies(catalog.data); setRuns(results.data)
      setRunId((current) => results.data.some((r) => r.run_id === current) ? current : (results.data.find((r) => r.strategy_id === 'trend_dual_v4' && r.timeframe === '60F' && r.universe_mode === 'fixed_snapshot') || results.data[0])?.run_id || '')
    }).catch((e) => { if (active) setError(e.message) }).finally(() => { if (active) setBusy(false) })
    return () => { active = false }
  }, [revision])

  useEffect(() => {
    if (!runId) { setDetail(null); return }
    let active = true
    setLoadingDetail(true); setError(''); setDetail(null)
    platformApi.quantBacktest(runId, page * PAGE_SIZE, PAGE_SIZE).then((r) => { if (active) setDetail(r) }).catch((e) => { if (active) setError(e.message) }).finally(() => { if (active) setLoadingDetail(false) })
    return () => { active = false }
  }, [runId, page, revision])

  const names = Object.fromEntries(strategies.map((s) => [s.id, s.name]))
  const metrics = detail?.metrics
  const config = detail?.config

  return <div className="quant-page">
    <header className="quant-header"><div><div className="quant-eyebrow">ALPHA FINANCE RADAR / QUANT</div><h1>量化策略</h1><p>策略定义 · 成交约束 · 回测证据</p></div><button className="btn ghost" disabled={busy} onClick={() => setRevision((v) => v + 1)}>{busy ? '读取中…' : '刷新结果'}</button></header>
    {error && <div className="quant-notice error" role="alert">{error}</div>}
    <section className="quant-strategies" aria-label="策略库">{strategies.map((s) => <article key={s.id}><span>60F / 日K · {s.version}</span><h2>{s.name}</h2><p>{s.description}</p><small>与股票行情使用相同的入场规则</small></article>)}</section>
    <div className="quant-notice">2026年样本内研究：固定名单全年回放存在成分股前视与幸存者偏差，不能直接作为可实现收益。策略已参考过2026年样例，后续还需新的样本验证。</div>
    {!runs.length && !busy && <div className="quant-empty">后台回测尚无已完成结果，请稍后刷新。</div>}
    {runs.length > 0 && <>
      <section className="quant-results"><div className="quant-section-title"><h2>回测结果</h2><span>每行是一次独立资金组合 · 点击查看</span></div><div className="quant-scroll"><table className="quant-table"><thead><tr><th>策略</th><th>周期</th><th>名单口径</th><th>总收益</th><th>最大回撤</th><th>平仓笔数</th><th>胜率</th></tr></thead><tbody>{runs.map((r) => <tr key={r.run_id} className={r.run_id === runId ? 'selected' : ''}><td><button className="quant-run-select" aria-pressed={r.run_id === runId} onClick={() => { setRunId(r.run_id); setPage(0) }}>{names[r.strategy_id] || r.strategy_id}</button></td><td>{period(r.timeframe)}</td><td>{mode(r.universe_mode)}</td><td className={r.metrics.total_return_pct >= 0 ? 'quant-gain' : 'quant-loss'}>{percent(r.metrics.total_return_pct)}</td><td>{percent(r.metrics.max_drawdown_pct)}</td><td>{r.metrics.closed_trades}</td><td>{percent(r.metrics.win_rate_pct)}</td></tr>)}</tbody></table></div></section>
      {loadingDetail && <div className="quant-empty">正在读取净值与成交明细…</div>}
      {detail && <>
        <div className="quant-section-title"><h2>{names[detail.strategy_id]} · {period(detail.timeframe)}</h2><span>{detail.start} — {detail.end} · {mode(detail.universe_mode)}</span></div>
        {detail.universe_mode === 'observed_only' && <div className="quant-notice">系统最早取得名单：{moment(detail.diagnostics.first_observed_at)}。名单取得之前的信号已剔除；若没有有效交易样本，零收益不能用来判断策略效果。</div>}
        <section className="quant-cards"><div><span>组合总收益</span><strong className={metrics.total_return_pct >= 0 ? 'quant-gain' : 'quant-loss'}>{percent(metrics.total_return_pct)}</strong><small>包含未平仓估值</small></div><div><span>最大回撤</span><strong>{percent(metrics.max_drawdown_pct)}</strong><small>每日收盘净值口径</small></div><div><span>盈利因子 / 胜率</span><strong>{number(metrics.profit_factor)} <small>/ {percent(metrics.win_rate_pct)}</small></strong><small>仅统计已平仓交易</small></div><div><span>平均仓位 / 费用</span><strong>{percent(metrics.average_exposure_pct)}</strong><small>累计费用 {amount(metrics.total_fees)} 元</small></div></section>
        <section className="quant-panel"><div className="quant-section-title"><h2>资金净值</h2><span>起点归一为1 · 价格指数基准不含分红</span></div><EquityChart data={detail.equity} initialCash={config.initial_cash} /><div className="quant-chart-legend"><span>● 策略</span><span>● 中证1000</span><span>● 沪深300</span></div></section>
        <section className="quant-panel"><h2>执行与数据口径</h2><div className="quant-method"><p>初始资金 {amount(config.initial_cash)} 元，最多 {config.max_positions} 只，单只目标 {percent(config.position_weight * 100)}；信号收盘后，下一根K线开盘尝试买入，同一启动段只买一次。</p><p>执行T+1、真实涨跌停价和现金约束。双边佣金 {number(config.commission_rate * 10000, 1)}‱（最低 {config.minimum_commission} 元），卖方印花税 {number(config.stamp_tax * 10000, 1)}‱，双边过户费 {number(config.transfer_fee * 10000, 1)}‱，单边滑点 {number(config.slippage * 10000, 1)}bp。</p><p>固定结构止损、{config.take_profit_r}R止盈、均线失守或 {config.max_holding_bars} 根超时；同根同时触及止盈止损时按止损处理。买入当日触发止损，延到下一交易日可成交时处理。</p><p>流动性按前一根成交量的 {percent(config.participation * 100)} 限制入场规模。复权收益为因子份额调整近似，未还原现金分红/送配股台账；无逐笔盘口，未建模退出拆单冲击。</p><p>样本 {detail.diagnostics.universe_size} 只，可用 {detail.diagnostics.usable_stocks} 只，{detail.diagnostics.bar_count.toLocaleString()} 根K线；年初指标需预热，缺失行情和因子不填补。</p></div><details><summary>查看数据限制与执行审计</summary><ul>{detail.diagnostics.warnings.map((w) => <li key={w}>{w}</li>)}</ul><pre>{JSON.stringify(detail.execution_audit, null, 2)}</pre></details></section>
        <section className="quant-panel"><div className="quant-section-title"><h2>已平仓成交 · {metrics.closed_trades} 笔</h2><span>每笔收益已扣双边费用</span></div><div className="quant-scroll"><table className="quant-table"><thead><tr><th>股票 / 类型</th><th>买入</th><th>卖出</th><th>退出原因</th><th>净盈亏</th><th>净收益率</th></tr></thead><tbody>{detail.trades.map((t, i) => <tr key={`${t.ts_code}-${t.entry_time}-${i}`}><td>{t.name}<small>{t.ts_code} · {family(t.family)}</small></td><td>{moment(t.entry_time)}<small>{number(t.entry_price)} 元</small></td><td>{moment(t.exit_time)}<small>{number(t.exit_price)} 元</small></td><td>{reasons[t.reason] || t.reason}</td><td className={t.pnl >= 0 ? 'quant-gain' : 'quant-loss'}>{amount(t.pnl)}</td><td>{percent(t.return_pct)}</td></tr>)}</tbody></table></div>{!detail.trades.length && <p className="quant-empty">暂无已平仓交易。</p>}<div className="quant-pagination"><button className="btn ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>上一页</button><span>{page + 1} / {Math.max(1, Math.ceil(metrics.closed_trades / PAGE_SIZE))}</span><button className="btn ghost" disabled={(page + 1) * PAGE_SIZE >= metrics.closed_trades} onClick={() => setPage((p) => p + 1)}>下一页</button></div></section>
        {!!detail.open_positions.length && <section className="quant-panel"><h2>未平仓 · {detail.open_positions.length} 只</h2><p className="quant-caption">按末次已知报价估值，期末不强行伪造卖出。</p><div className="quant-scroll"><table className="quant-table"><thead><tr><th>股票</th><th>买入时间</th><th>持仓估值</th><th>浮动盈亏</th><th>最后报价</th></tr></thead><tbody>{detail.open_positions.map((p) => <tr key={p.ts_code}><td>{p.name}<small>{p.ts_code}</small></td><td>{moment(p.entry_time)}</td><td>{amount(p.market_value)}</td><td>{amount(p.unrealized_pnl)}</td><td>{moment(p.last_quote)}</td></tr>)}</tbody></table></div></section>}
      </>}
    </>}
  </div>
}
