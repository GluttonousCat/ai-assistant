import { useEffect, useState } from 'react'
import { platformApi } from '../platformApi'
import EquityChart from './EquityChart'
import TrancheFills from './TrancheFills'
import StrategyWorkspace from './StrategyWorkspace'
import { summarizeDraft } from './workspaceRules'
import OpportunityBadge from '../OpportunityBadge'
import { preferredRun, marketResearchLabel, marketPolicyText } from './researchPolicy'
import './quant.css'

const number = (v, digits = 2) => v == null ? '—' : Number(v).toFixed(digits)
const percent = (v) => v == null ? '—' : `${number(v)}%`
const amount = (v) => v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const isSwing = (r) => r?.strategy_id?.startsWith('swing_')
const period = (v) => v === '1D' ? '日K' : v
const mode = (v) => v === 'observed_only' ? '仅已知名单' : '固定名单研究'
const moment = (v) => String(v || '—').replace('T', ' ').slice(0, 16)
const reasons = { tranche_profit_1: '第一笔止盈', tranche_profit_2: '第二笔止盈', structure_failed: '60F结构失效', rebound_failed: '反弹未推进退出', stop_loss: '结构止损', protected_stop: '盈利保护退出', take_profit: '目标止盈', trend_break: '单根均线失守', trend_break_confirmed: '持续失守均线', holding_timeout: '持仓超时', t1_deferred_stop: 'T+1延后止损' }
const profiles = { legacy_single: '原分段退出（对照）', full_entry_full_exit: '同引擎单买单卖', entry_442_only: '442仅分批买', exit_442_only: '442仅分批卖', entry_exit_442: '442分批进出', entry_exit_532: '532分批进出', structural_carry: '确认支撑·结构持有', scheduled_2r_10d: '盘后2R / 10日', scheduled_3r_20d: '盘后3R / 20日', scheduled_protected: '盘后盈利保护', scheduled_reversal: '盘后分段离场', reversal_staged: '分段离场（实验对照）', single_line_2r: '单根均线+2R（对照）', structure_2r: '结构止损+2R', protected_3r: '盈利保护+ATR+3R' }
const trancheText = (config) => {
  const weights = (items) => items.map((x) => `${number(x * 100, 0)}%`).join('/')
  const entry = config.entry_weights.length > 1 ? `按单股计划资金${weights(config.entry_weights)}分批买；盈利${config.add_at_r.join('/')}R且60F确认后才加仓，禁止下跌摊成本（统一复权尺度）。` : '单次买入计划仓位。'
  const exit = config.exit_weights.length > 1 ? `按实际建仓的${weights(config.exit_weights)}分批卖，前两笔在收盘盈利${config.take_profit_pct.join('/')}%后次日执行，尾仓随趋势。` : '不分批止盈，按趋势、止损或超时一次退出。'
  const structure = config.trend_exit ? `先收复M21、持有至少${config.min_trend_holding_bars}个交易日，再出现完成60F持续失守M21且M13转弱时退出全部可卖持仓；` : '按原价格止损及盈利保护管理，不启用M21软结构退出；'
  return `${entry}${exit}资金、整手及风险限制可能使计划无法建满；R按首笔买价至初始止损固定。减仓意图锁定加仓。收盘盈利${config.protect_at_pct}%后保护剩余成本，并按60F M21留0.5ATR缓冲上移保护。${structure}`
}
const PAGE_SIZE = 30

export default function QuantDashboard() {
  const [tab, setTab] = useState('workspace')
  const [strategies, setStrategies] = useState([])
  const [runs, setRuns] = useState([])
  const [runId, setRunId] = useState('')
  const workspaceSelected = runId.startsWith('workspace:')
  const [detail, setDetail] = useState(null)
  const [page, setPage] = useState(0)
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(true)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [error, setError] = useState('')
  const [showSuperseded, setShowSuperseded] = useState(false)
  const visibleRuns = runs.filter((r) => showSuperseded || !r.diagnostics?.superseded)

  useEffect(() => {
    if (tab !== 'results' || workspaceSelected) { setBusy(false); return }
    let active = true
    setBusy(true); setError('')
    Promise.all([platformApi.quantStrategies(), platformApi.quantBacktests()]).then(([catalog, results]) => {
      if (!active) return
      setStrategies(catalog.data); setRuns(results.data)
      setRunId((current) => current.startsWith('workspace:') || results.data.some((r) => r.run_id === current && !r.diagnostics?.superseded) ? current : preferredRun(results.data)?.run_id || '')
    }).catch((e) => { if (active) setError(e.message) }).finally(() => { if (active) setBusy(false) })
    return () => { active = false }
  }, [revision, tab, workspaceSelected])

  useEffect(() => {
    if (!runId) { setDetail(null); return }
    let active = true
    setLoadingDetail(true); setError(''); setDetail(null)
    const load = runId.startsWith('workspace:') ? platformApi.quantWorkspaceResult(runId.slice(10), page * PAGE_SIZE, PAGE_SIZE) : platformApi.quantBacktest(runId, page * PAGE_SIZE, PAGE_SIZE)
    load.then((r) => { if (active) setDetail(r) }).catch((e) => { if (active) setError(e.message) }).finally(() => { if (active) setLoadingDetail(false) })
    return () => { active = false }
  }, [runId, page, revision])

  const names = Object.fromEntries(strategies.map((s) => [s.id, s.name]))
  const metrics = detail?.metrics
  const config = detail?.config
  const hasBenchmark = (code) => detail?.equity?.some((point) => point.benchmarks?.[code] != null)

  return <div className="quant-page">
    <header className="quant-header"><div><div className="quant-eyebrow">ALPHA FINANCE RADAR / QUANT</div><h1>量化策略</h1><p>制定策略 · 测试运行 · 审阅回测</p></div>{tab === 'results' && <div className="quant-workspace-actions"><button className="btn ghost" disabled={busy} onClick={() => setRevision((v) => v + 1)}>{busy ? '读取中…' : '刷新结果'}</button>{workspaceSelected && <button className="btn ghost" onClick={() => { setRunId(''); setPage(0) }}>浏览历史研究</button>}</div>}</header>
    <nav className="quant-tabs" aria-label="量化工作区"><button aria-pressed={tab === 'workspace'} onClick={() => setTab('workspace')}>策略工作台</button><button aria-pressed={tab === 'results'} onClick={() => setTab('results')}>回测报告</button></nav>
    <div hidden={tab !== 'workspace'}><StrategyWorkspace onView={(task) => { setRunId(`workspace:${task.id}`); setPage(0); setTab('results') }} /></div>
    {tab === 'results' && <>
    {error && <div className="quant-notice error" role="alert">{error}</div>}
    <details className="quant-library"><summary>查看历史策略库 · 保留原定义与研究对照</summary><section className="quant-strategies" aria-label="策略库">{strategies.map((s) => <article key={s.id}><span>{['v8', 'v9'].includes(s.generation) ? '主板60F信号 / 盘后执行' : ['v5', 'v6', 'v7'].includes(s.generation) ? '60F趋势 / 日K入场' : '历史策略 · 60F / 日K'} · {s.version}</span><h2>{s.name}</h2><p>{s.description}</p><small>{['v5', 'v6', 'v7', 'v8', 'v9'].includes(s.generation) ? 'M8/M13/M21 · 次日开盘模型' : '保留原参数与原回测，供对照'}</small></article>)}</section></details>
    <div className="quant-notice">2026年样本内研究：固定名单全年回放存在成分股前视与幸存者偏差，不能直接作为可实现收益。策略已参考过2026年样例，后续还需新的样本验证。</div>
    {!runs.length && !runId.startsWith('workspace:') && !busy && <div className="quant-empty">后台回测尚无已完成结果，请稍后刷新。</div>}
    {(runs.length > 0 || runId.startsWith('workspace:')) && <>
      <details className="quant-library" open={!runId.startsWith('workspace:')}><summary>已发布的历史研究报告</summary><section className="quant-results"><div className="quant-section-title"><h2>回测结果</h2><span>每行是一次独立资金组合 · 点击查看</span><button className="btn ghost" aria-pressed={showSuperseded} onClick={() => { if (showSuperseded && runs.find((r) => r.run_id === runId)?.diagnostics?.superseded) { setRunId(preferredRun(runs)?.run_id || ''); setPage(0) } setShowSuperseded(!showSuperseded) }}>{showSuperseded ? '收起失效历史' : `查看失效历史 ${runs.filter((r) => r.diagnostics?.superseded).length}`}</button></div><div className="quant-scroll"><table className="quant-table"><thead><tr><th>策略 / 退出方案</th><th>周期</th><th>名单口径</th><th>总收益</th><th>最大回撤</th><th>平仓笔数</th><th>胜率 / 盈利因子</th></tr></thead><tbody>{visibleRuns.map((r) => <tr key={r.run_id} className={r.run_id === runId ? 'selected' : ''}><td><button className="quant-run-select" aria-pressed={r.run_id === runId} onClick={() => { setRunId(r.run_id); setPage(0) }}>{names[r.strategy_id] || r.strategy_id}</button><small>{r.diagnostics?.superseded ? '已替代 · ' : ''}{marketResearchLabel(r) || profiles[r.diagnostics?.exit_profile] || '历史固定退出'} · {r.strategy_version || 'v4'}</small></td><td>{isSwing(r) ? (r.diagnostics?.execution_schedule === 'precommitted' ? '60F信号 · 预挂条件单' : '60F信号 · 盘后执行') : r.diagnostics?.trend_timeframe === '60F' ? '60F趋势 → 日K' : period(r.timeframe)}</td><td>{mode(r.universe_mode)}</td><td className={r.metrics.total_return_pct >= 0 ? 'quant-gain' : 'quant-loss'}>{percent(r.metrics.total_return_pct)}</td><td>{percent(r.metrics.max_drawdown_pct)}</td><td>{r.metrics.closed_trades}</td><td>{percent(r.metrics.win_rate_pct)} / {number(r.metrics.profit_factor)}</td></tr>)}</tbody></table></div></section></details>
      {loadingDetail && <div className="quant-empty">正在读取净值与成交明细…</div>}
      {detail && <>
        <div className="quant-section-title"><h2>{detail.workspace_spec?.name || names[detail.strategy_id]} · {period(detail.timeframe)}</h2><span>{detail.start} — {detail.end} · {mode(detail.universe_mode)}</span></div>
        {detail.universe_mode === 'observed_only' && <div className="quant-notice">系统最早取得名单：{moment(detail.diagnostics.first_observed_at)}。名单取得之前的信号已剔除；若没有有效交易样本，零收益不能用来判断策略效果。</div>}
        {detail.diagnostics.input_source && <div className="quant-notice">{detail.diagnostics.input_source}</div>}
        {detail.workspace_spec && <section className="quant-panel quant-rule-preview"><h2>本次运行的策略规则</h2><ol>{summarizeDraft(detail.workspace_spec).map((line) => <li key={line}>{line}</li>)}</ol><details><summary>查看冻结配置</summary><pre>{JSON.stringify(detail.workspace_spec, null, 2)}</pre></details></section>}
        {!detail.workspace_spec && isSwing(detail) && <div className={`quant-notice${detail.diagnostics.training_qualified === false ? ' error' : ''}`}>主板60F参数研究 · {detail.diagnostics.training_qualified === false ? '开发段未通过收益或风险门槛，保留失败研究供检查。' : '参数与执行规则按报告冻结；历史收益不代表未来收益。'} 5%是目标，计划止损不是实际损失保证。</div>}{detail.diagnostics.superseded && <div className="quant-notice error">此批结果已被替代：{detail.diagnostics.superseded_reason}。保留原始结果供核查，不应用于评价当前策略。</div>}
        <section className="quant-cards"><div><span>组合总收益</span><strong className={metrics.total_return_pct >= 0 ? 'quant-gain' : 'quant-loss'}>{percent(metrics.total_return_pct)}</strong><small>包含未平仓估值</small></div><div><span>最大回撤</span><strong>{percent(metrics.max_drawdown_pct)}</strong><small>每日收盘净值口径</small></div><div><span>盈利因子 / 胜率</span><strong>{number(metrics.profit_factor)} <small>/ {percent(metrics.win_rate_pct)}</small></strong><small>仅统计完整已平仓周期{metrics.worst_trade_return_pct != null && <> · 最差 {percent(metrics.worst_trade_return_pct)}</>}</small></div><div><span>平均仓位 / 费用</span><strong>{percent(metrics.average_exposure_pct)}</strong><small>累计费用 {amount(metrics.total_fees)} 元</small></div></section>
        <section className="quant-panel"><div className="quant-section-title"><h2>资金净值</h2><span>起点归一为1 · 价格指数基准不含分红</span></div><EquityChart data={detail.equity} initialCash={config.initial_cash} /><div className="quant-chart-legend"><span>● 策略</span><span>● 中证1000{!hasBenchmark('000852.SH') && '（未提供）'}</span><span>● 沪深300{!hasBenchmark('000300.SH') && '（未提供）'}</span></div></section>
        <section className="quant-panel"><h2>执行与数据口径</h2><div className="quant-method"><p>初始资金 {amount(config.initial_cash)} 元，最多 {config.max_positions} 只，单只目标 {percent(config.position_weight * 100)}；信号收盘后，下一根K线开盘尝试买入，同一启动段只买一次。</p>{!detail.workspace_spec && isSwing(detail) && <p>全部形态、均线与MACD条件来自60F；日K仅提供执行价格与流动性，无日K阳线门槛。主板原五指数池研究：{detail.diagnostics.research_kind === 'online_market' ? '本轮固定指数相对强弱模型，比较13组风险上限与分批节奏；仅按1—6月选择，2026全部已参与研究，不是独立样本外。' : detail.diagnostics.research_kind?.startsWith('market_') ? `本轮${detail.diagnostics.candidate_count || '多'}组指数环境与分批节奏对照，参数预先冻结，仅按1—6月选择；各段早已看过，均属样本内。` : detail.diagnostics.research_kind === 'tranche_campaign' ? '本轮固定442/532和单买单卖、仅分批买、仅分批卖对照；不按全年收益挑选赢家，仍为2026样本内研究。' : detail.diagnostics.research_kind === 'ordered_ablation' ? '本轮7组固定对照，分离入场和退出贡献；不按全年收益挑选赢家。2026各分段均已看过，仍属样本内研究。' : `${detail.strategy_config?.regime_guard ? '两轮共60组；本轮24组加入60F市场广度与恢复过滤' : '本轮36组'}，仅在1—6月选参，后续固定检验。`}5%是研究目标。{config.close_only ? '盘后检查止盈止损，下一交易日开盘执行。' : '此行假设提前挂券商条件单，盘中触发；需要券商支持，无法等同于盘后手动处理。'}</p>}{detail.diagnostics.trend_timeframe === '60F' && !isSwing(detail) && <p>完整60F确认趋势，日K转强才触发买入；退出方案：{profiles[detail.diagnostics.exit_profile]}。{detail.strategy_id === "mtf_reversal_v6" ? "退出规则按本行方案冻结，结构止损+2R与分段退出对照，不按各段成绩重选参数；" : "方案只在1—6月选择，再固定检验7—8月与9—11月；"}这些时段仍有已知样例及固定名单偏差。</p>}{detail.diagnostics.research_kind === 'online_market' && <p>参考沪深300与中证1000的相对表现，每月只用此前已结束的历史辅助样本更新候选排序；不足500条时持币，预热期包含在全年收益里。所有个股形态与预测特征来自60F，日K只供执行与流动性。当前通过的是历史研究门槛，跳空和跌停仍可能突破计划风险，新的样本尚待验证。</p>}{config.market_policy && <p>{marketPolicyText(config.market_policy)}</p>}{detail.diagnostics.hold_signal_close && <p>入场前要求最后一根完整60F收盘保持在原60F触发价以上，不另加日K形态条件。</p>}<p>执行T+1、真实涨跌停价和现金约束。双边佣金 {number(config.commission_rate * 10000, 1)}‱（最低 {config.minimum_commission} 元），卖方印花税 {number(config.stamp_tax * 10000, 1)}‱，双边过户费 {number(config.transfer_fee * 10000, 1)}‱，单边滑点 {number(config.slippage * 10000, 1)}bp。</p><p>{config.exit_policy === 'tranches' ? trancheText(config) : config.exit_policy === 'structure' ? `原始止损保持；收盘同时盈利${config.protect_after_r}R及${config.protect_after_pct}%后加入成本保护，仅按入场后形成并已确认的60F低点、留${config.support_buffer_atr}ATR缓冲抬升保护线；60F连续失守M21且M13转弱才判结构退出；不固定2R/3R截断；` : config.exit_policy === 'reversal' ? `原结构止损保持；${config.rebound_patience}日未推进${config.rebound_min_progress_r}R且收回成本下方则次日退出；收盘盈利${config.protect_after_r}R后加入成本缓冲，峰值达到${config.trail_after_r}R后按${config.trail_atr}ATR跟踪，止损只升不降、次根生效；峰值曾达${config.protect_after_r}R，且先收复${isSwing(detail) ? '60F' : '日K'} M21后，再连续${config.failure_bars}日失守才退出，不固定2R截断；` : config.exit_policy === 'protected' ? `初始结构止损；盈利达到${config.protect_after_r}R后按成本缓冲与${config.trail_atr}ATR跟踪保护，止损只上移，下一根生效；至少持有${config.min_trend_holding_bars}根后，连续${config.failure_bars}根失守M21退出；` : config.trend_exit ? '结构止损、单根失守均线退出；' : '保留结构止损，不使用单根均线退出；'}{!['reversal', 'structure', 'tranches'].includes(config.exit_policy) && <>{config.take_profit_r}R目标止盈、</>}{config.max_holding_bars}根超时。{config.close_only ? '仅按收盘价检查阈值，下一交易日开盘处理；盘中触价不假设成交，跳空和跌停可能突破计划风险。' : '同根同时触及止盈止损时按止损处理；买入当日触发止损，延到下一交易日可成交时处理。'}</p>{config.risk_budget_pct > 0 && <p>单笔初始风险预算≤净值{config.risk_budget_pct}%，同时受单股仓位与现金约束；跳空或跌停可能突破预算。</p>}<p>流动性按前一根成交量的 {percent(config.participation * 100)} 限制入场规模。复权收益为因子份额调整近似，未还原现金分红/送配股台账；无逐笔盘口，未建模退出拆单冲击。</p><p>样本 {detail.diagnostics.universe_size} 只，可用 {detail.diagnostics.usable_stocks} 只，{detail.diagnostics.bar_count?.toLocaleString() || '—'} 根{period(detail.timeframe)}；年初指标需预热，缺失行情和因子不填补。</p></div><details><summary>查看数据限制与执行审计</summary><ul>{detail.diagnostics.warnings.map((w) => <li key={w}>{w}</li>)}</ul><pre>{JSON.stringify(detail.execution_audit, null, 2)}</pre></details></section>
        <section className="quant-panel"><div className="quant-section-title"><h2>已平仓成交 · {metrics.closed_trades} 笔</h2><span>{config.exit_policy === "tranches" ? "按累计投入成本计算完整周期净收益；分笔费用已扣除" : "每笔收益已扣双边费用"}</span></div><div className="quant-scroll"><table className="quant-table"><thead><tr><th>股票 / 类型</th><th>买入</th><th>卖出</th><th>退出原因</th><th>净盈亏</th><th>净收益率</th></tr></thead><tbody>{detail.trades.map((t, i) => <tr key={`${t.ts_code}-${t.entry_time}-${i}`}><td>{t.name}<small>{t.ts_code}</small><OpportunityBadge value={t} />{t.entry_evidence && <details><summary>查看60F前置结构</summary>{t.entry_evidence.source === 'ordered-60f' ? <p>推进起点 {moment(t.entry_evidence.impulse_start)} → 高点 {moment(t.entry_evidence.impulse_peak)}；第 {t.entry_evidence.pullback_round} 次回调转强。</p> : <p>背离后的低点测试、测试量能收缩及60F收复确认。</p>}</details>}</td><td>{moment(t.entry_time)}<small>{number(t.entry_price)} 元{t.fills ? "（首笔）" : ""}</small></td><td>{moment(t.exit_time)}<small>{number(t.exit_price)} 元{t.fills ? "（末笔）" : ""}</small><TrancheFills fills={t.fills} /></td><td>{reasons[t.reason] || t.reason}{t.market_grace_used && <small>市场共跌时曾多观察一日</small>}</td><td className={t.pnl >= 0 ? 'quant-gain' : 'quant-loss'}>{amount(t.pnl)}</td><td>{percent(t.return_pct)}</td></tr>)}</tbody></table></div>{!detail.trades.length && <p className="quant-empty">暂无已平仓交易。</p>}<div className="quant-pagination"><button className="btn ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>上一页</button><span>{page + 1} / {Math.max(1, Math.ceil(metrics.closed_trades / PAGE_SIZE))}</span><button className="btn ghost" disabled={(page + 1) * PAGE_SIZE >= metrics.closed_trades} onClick={() => setPage((p) => p + 1)}>下一页</button></div></section>
        {!!detail.open_positions.length && <section className="quant-panel"><h2>未平仓 · {detail.open_positions.length} 只</h2><p className="quant-caption">按末次已知报价估值，期末不强行伪造卖出。</p><div className="quant-scroll"><table className="quant-table"><thead><tr><th>股票</th><th>买入时间</th><th>持仓估值</th><th>浮动盈亏</th><th>最后报价</th></tr></thead><tbody>{detail.open_positions.map((p) => <tr key={p.ts_code}><td>{p.name}<small>{p.ts_code}</small><OpportunityBadge value={p} /></td><td>{moment(p.entry_time)}<TrancheFills fills={p.fills} /></td><td>{amount(p.market_value)}</td><td>{amount(p.unrealized_pnl)}{p.partial_realized_pnl != null && <small>已兑现 {amount(p.partial_realized_pnl)} 元</small>}</td><td>{moment(p.last_quote)}</td></tr>)}</tbody></table></div></section>}
      </>}
    </>}
    </>}
  </div>
}
