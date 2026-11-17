import { useEffect, useRef, useState } from 'react'
import { platformApi } from '../platformApi'
import RuleBuilder from './RuleBuilder'
import { isStaged, newRequestId, opportunityNames, poolNames, statusNames, summarizeDraft } from './workspaceRules'

function NumberField({ label, value, onChange, min, max, step = 'any', hint }) {
  return <label className="quant-field"><span>{label}</span><input type="number" value={value} min={min} max={max} step={step} required onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))} />{hint && <small>{hint}</small>}</label>
}

export default function StrategyWorkspace({ onView }) {
  const [catalog, setCatalog] = useState({ drafts: [], tasks: [] })
  const [draft, setDraft] = useState(null)
  const [text, setText] = useState('')
  const [generation, setGeneration] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [start, setStart] = useState('2026-01-01')
  const [end, setEnd] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 1)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  })
  const [sampleLimit, setSampleLimit] = useState(200)
  const [availability, setAvailability] = useState(null)
  const mounted = useRef(false)
  const pendingRequest = useRef(null)
  const dirtyVersion = useRef(0)

  useEffect(() => {
    mounted.current = true
    let active = true
    const availabilityController = new AbortController()
    platformApi.quantAvailability({ signal: availabilityController.signal }).then((data) => {
      if (!active) return
      setAvailability(data)
      if (data.available_end) setEnd((current) => current > data.available_end ? data.available_end : current)
    }).catch((e) => { if (active && e.name !== 'AbortError') setError(e.message) })
    let timer, controller
    const refresh = async () => {
      controller = new AbortController()
      try {
        const data = await platformApi.quantWorkspace({ signal: controller.signal })
        if (active) { setCatalog(data); setDraft((current) => current || data.defaults) }
      } catch (e) { if (active && e.name !== 'AbortError') setError(e.message) }
      if (active) timer = setTimeout(refresh, 4000)
    }
    refresh()
    return () => { active = false; mounted.current = false; clearTimeout(timer); controller?.abort(); availabilityController.abort() }
  }, [])

  const change = (group, key, value) => {
    dirtyVersion.current += 1
    pendingRequest.current = null
    setDraft((current) => group ? { ...current, [group]: { ...current[group], [key]: value } } : { ...current, [key]: value })
    setConfirmed(false); setNotice('')
  }
  const refresh = async () => {
    const data = await platformApi.quantWorkspace()
    if (mounted.current) setCatalog(data)
  }
  const generate = async () => {
    setBusy('generate'); setError(''); setNotice('')
    const version = dirtyVersion.current
    try {
      const result = await platformApi.quantGenerate({ text, base: draft })
      if (!mounted.current) return
      if (version !== dirtyVersion.current) { setNotice('生成期间配置已有修改，请重新生成草稿。'); return }
      setDraft(result.draft); setGeneration(result); setConfirmed(false); pendingRequest.current = null
    } catch (e) { if (mounted.current) setError(e.message) }
    finally { if (mounted.current) setBusy('') }
  }
  const save = async (run = false) => {
    setBusy(run ? 'run' : 'save'); setError(''); setNotice('')
    try {
      let request = pendingRequest.current
      if (!run || !request) {
        const saved = await platformApi.quantSaveDraft(draft)
        request = { draft_id: saved.id, request_id: newRequestId(), start, end, sample_limit: sampleLimit }
        if (run) pendingRequest.current = request
      }
      if (run) {
        await platformApi.quantSubmit(request)
        pendingRequest.current = null
      }
      if (mounted.current) setNotice(run ? '已提交后台回测，可以离开页面，稍后在运行记录查看结果。' : '策略版本已保存。每次保存生成独立版本，旧回测配置保持冻结。')
      await refresh()
    } catch (e) { if (mounted.current) setError(e.message) }
    finally { if (mounted.current) setBusy('') }
  }

  if (!draft) return <div className="quant-empty">正在打开策略创作工作台…{error && <p role="alert">{error}</p>}</div>
  const e = draft.entry, x = draft.execution, staged = isStaged(draft), custom = draft.mode === 'rules'
  const field = (group, key, label, options = {}) => <NumberField key={key} label={label} value={draft[group][key]} onChange={(value) => change(group, key, value)} {...options} />

  return <div className="quant-workspace">
    <section className="quant-panel"><div className="quant-section-title"><h2>描述交易逻辑</h2><span>自然语言 → 可编辑规则 → 回测</span></div><textarea className="quant-prompt" value={text} onChange={(event) => { setText(event.target.value); dirtyVersion.current += 1 }} placeholder="例如：60F收盘价上穿MA13，且RSI14大于40时买入；跌破MA21离场，按买入价止损3%、止盈8%，最多持有20个交易日。" maxLength={4000} rows={2} /><div className="quant-workspace-actions"><small>自由组合均线 / RSI / MACD / 量能 / 突破；可先添加条件再回测。</small><button className="btn ghost" disabled={!!busy || text.trim().length < 5} onClick={generate}>{busy === 'generate' ? '正在理解策略…' : '生成规则草稿'}</button></div>
      {generation && <div className="quant-generation"><strong>模型解释与待确认事项</strong><p>以下为生成时的解释；编辑后以当前规则预览与冻结配置为准。</p><ul>{generation.assumptions.map((item, i) => <li key={i}>{item}</li>)}</ul>{generation.unsupported.length > 0 && <div className="quant-notice error"><strong>以下要求尚不能执行</strong><ul>{generation.unsupported.map((item, i) => <li key={i}>{item}</li>)}</ul><p>请修改描述并重新生成，或清除这次生成结果后改用表单定义策略。</p><button className="btn ghost" onClick={() => { setGeneration(null); setDraft((current) => ({ ...current, interpretation_notes: [], unresolved_requirements: [] })); setConfirmed(false) }}>明确移除未支持要求，改用表单配置</button></div>}</div>}
    </section>
    <form onSubmit={(event) => { event.preventDefault(); save(true) }}><fieldset disabled={!!busy}>
      <section className="quant-panel"><div className="quant-section-title"><h2>策略定义</h2><select aria-label="加载已保存的策略版本" defaultValue="" disabled={!!busy} onChange={(event) => { const row = catalog.drafts.find((d) => d.id === event.target.value); if (row) { setDraft(row.spec); setGeneration(row.spec.unresolved_requirements?.length || row.spec.interpretation_notes?.length ? { assumptions: row.spec.interpretation_notes || [], unsupported: row.spec.unresolved_requirements || [] } : null); setConfirmed(false); pendingRequest.current = null; dirtyVersion.current += 1 } }}><option value="" disabled>加载已保存的版本</option>{catalog.drafts.map((row) => <option key={row.id} value={row.id}>{row.spec.name} · {new Date(row.created_at).toLocaleString('zh-CN')}</option>)}</select></div>
        <div className="quant-form-grid"><label className="quant-field"><span>策略名称</span><input required maxLength={80} value={draft.name} onChange={(event) => change(null, 'name', event.target.value)} /></label><label className="quant-field"><span>股票池</span><select value={draft.universe} onChange={(event) => change(null, 'universe', event.target.value)}>{Object.entries(poolNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="quant-field"><span>创作方式</span><select value={draft.mode || 'template'} onChange={(event) => { const mode = event.target.value; change(null, 'mode', mode); if (mode === 'rules') { change(null, 'rules', draft.rules || catalog.defaults.rules); change('execution', 'entry_plan', 'single'); change('execution', 'exit_plan', 'single') } }}><option value="rules">自由组合规则</option><option value="template">已有60F形态模板</option></select></label></div>
        {custom ? <RuleBuilder value={draft.rules} onChange={(value) => change(null, 'rules', value)} /> : <><label className="quant-field"><span>60F机会类型</span><select value={e.family} onChange={(event) => change('entry', 'family', event.target.value)}>{Object.entries(opportunityNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>        <details className="quant-advanced"><summary>入场细则与形态阈值 · M8 / M13 / M21</summary><div className="quant-form-grid">{field('entry', 'min_body_atr', '最小阳线实体 / ATR', { min: .01, max: 3 })}{field('entry', 'min_volume_ratio', '最小量比', { min: .01, max: 5 })}{field('entry', 'cooldown_bars', '信号冷却 / 60F根数', { min: 1, max: 120, step: 1 })}{field('entry', 'divergence_improvement_atr', '底背离DIF改善 / ATR', { min: .001, max: 1 })}{field('entry', 'pullback_depth_atr', '回调深度上限 / ATR', { min: .01, max: 10 })}{field('entry', 'stop_buffer_atr', '结构止损缓冲 / ATR', { min: .01, max: 3 })}{field('entry', 'min_daily_amount', '20日日均成交额 / 元', { min: 1, max: 10000000000 })}</div></details></>}

      </section>
      <section className="quant-panel"><div className="quant-section-title"><h2>资金、风险与离场</h2><span>计划风险受约束 · 跳空仍可能超出</span></div><div className="quant-form-grid">
        {field('execution', 'initial_cash', '初始资金 / 元', { min: 10000, max: 100000000 })}{field('execution', 'max_positions', '最多持仓 / 只', { min: 1, max: 30, step: 1 })}
        <NumberField label="单股计划仓位 / %" value={x.position_weight === '' ? '' : +(x.position_weight * 100).toFixed(4)} min={.1} max={100} onChange={(value) => change('execution', 'position_weight', value === '' ? '' : value / 100)} />
        {field('execution', 'risk_budget_pct', '单股账户初始风险预算 / %', { min: .01, max: 1 })}
        {custom ? <>{field('rules', 'stop_loss_pct', '买入价计划止损 / %', { min: .01, max: 5 })}<label className="quant-field"><span>买入价计划止盈 / %（可不设置）</span><input type="number" min={.01} max={100} step="any" value={draft.rules.take_profit_pct ?? ''} placeholder="留空，仅按离场条件 / 超时退出" onChange={(event) => change('rules', 'take_profit_pct', event.target.value === '' ? null : Number(event.target.value))} /></label>{field('rules', 'min_daily_amount', '20日日均成交额 / 元（0关闭）', { min: 0, max: 10000000000 })}</> : <>
        {field('entry', 'min_risk_pct', '最小结构止损距离 / %', { min: .01, max: 4 })}{field('entry', 'max_risk_pct', '最大结构止损距离 / %', { min: .01, max: 5, hint: '用于筛除离失效位过远的机会，不是固定止损率。' })}
        <label className="quant-field"><span>买入计划</span><select value={x.entry_plan} onChange={(event) => change('execution', 'entry_plan', event.target.value)}><option value="single">单次买入</option><option value="442">40% / 40% / 20%</option><option value="532">50% / 30% / 20%</option></select></label><label className="quant-field"><span>卖出计划</span><select value={x.exit_plan} onChange={(event) => change('execution', 'exit_plan', event.target.value)}><option value="single">单次卖出</option><option value="442">40% / 40% / 20%</option><option value="532">50% / 30% / 20%</option></select></label>
</>}
        {field('execution', 'max_holding_bars', '最长持有 / 交易日', { min: 1, max: 120, step: 1 })}
        {!custom && !staged && <><label className="quant-field"><span>离场模式</span><select value={x.exit_policy} onChange={(event) => change('execution', 'exit_policy', event.target.value)}><option value="legacy">结构止损 + R目标</option><option value="protected">盈利保护 + ATR跟踪 + R目标</option><option value="reversal">反弹失败与ATR跟踪 · 无固定R目标</option></select></label>{x.exit_policy !== 'reversal' && field('execution', 'take_profit_r', '目标止盈 / R', { min: .1, max: 20 })}{x.exit_policy !== 'legacy' && <>{field('execution', 'protect_after_r', '盈利保护启动 / R', { min: .1, max: x.exit_policy === 'reversal' ? 2 : 10 })}{field('execution', 'trail_atr', '跟踪止损 / ATR', { min: .1, max: 10 })}{field('execution', 'failure_bars', '均线失守确认 / 日', { min: 1, max: 10, step: 1 })}</>}</>}
        {!custom && staged && <>{field('execution', 'profit_first_pct', '首档止盈 / 成本保护启动 / %', { min: .1, max: 50 })}{x.exit_plan !== 'single' && field('execution', 'profit_second_pct', '第二档止盈 / %', { min: .1, max: 100 })}</>}
      </div>{!custom && <label className="quant-check"><input type="checkbox" checked={x.trend_exit} onChange={(event) => change('execution', 'trend_exit', event.target.checked)} />启用60F均线失效离场</label>}{custom && <p className="quant-rule-help">组合规则首版采用单次进出，不隐含模板均线离场；442/532可在已有60F模板模式使用。</p>}<details className="quant-advanced"><summary>费用与成交假设</summary><div className="quant-form-grid">{field('execution', 'commission_rate', '双边佣金 / 小数', { min: 0, max: .01 })}{field('execution', 'minimum_commission', '最低佣金 / 元', { min: 0, max: 100 })}{field('execution', 'stamp_tax', '卖方印花税 / 小数', { min: 0, max: .01 })}{field('execution', 'transfer_fee', '双边过户费 / 小数', { min: 0, max: .01 })}{field('execution', 'slippage', '单边滑点 / 小数', { min: 0, max: .02 })}</div><p>使用真实涨跌停价、逐批T+1、整手与共享现金约束；参与率固定为前一根成交量的1%。</p></details></section>
      <section className="quant-panel quant-rule-preview"><div className="quant-section-title"><h2>规则预览</h2><span>这份配置将随回测保存</span></div><ol>{summarizeDraft(draft).map((line) => <li key={line}>{line}</li>)}</ol><details><summary>查看完整配置 JSON</summary><pre>{JSON.stringify(draft, null, 2)}</pre></details>{generation && !generation.unsupported.length && <label className="quant-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />已检查生成规则及参数，确认按上述配置回测</label>}</section>
      <section className="quant-panel"><div className="quant-section-title"><h2>测试区间</h2><span>后台串行运行 · 可离开页面</span></div><div className="quant-form-grid"><label className="quant-field"><span>开始日期</span><input type="date" min="2026-01-01" max={end} value={start} required onChange={(event) => { setStart(event.target.value); pendingRequest.current = null }} /></label><label className="quant-field"><span>结束日期</span><input type="date" min={start} value={end} required onChange={(event) => { setEnd(event.target.value); pendingRequest.current = null }} /></label><label className="quant-field"><span>运行规模</span><select value={sampleLimit} onChange={(event) => { setSampleLimit(Number(event.target.value)); pendingRequest.current = null }}><option value={50}>快速检查 · 前50只</option><option value={200}>快速检查 · 前200只</option><option value={0}>所选股票池 · 全量</option></select><small>快速检查按代码取样，不能代表整个股票池。</small></label></div><div className="quant-workspace-actions"><span>只使用本地已入库行情，不下单。</span><div><button className="btn ghost" type="button" disabled={!!busy} onClick={() => save(false)}>{busy === 'save' ? '保存中…' : '保存策略版本'}</button><button className="btn" type="submit" disabled={!!busy || !!draft.unresolved_requirements?.length || !!generation?.unsupported.length || (!!generation && !confirmed)}>{busy === 'run' ? '提交中…' : '运行回测'}</button></div></div></section>
    </fieldset></form>
    {availability && <p className="quant-caption">本地回测数据共同截至 {availability.available_end || '尚未就绪'}；分钟线、日K、复权因子与真实涨跌停均需覆盖，运行时还会逐股检查。</p>}
    {error && <div className="quant-notice error" role="alert">{error}</div>}{notice && <div className="quant-notice" role="status">{notice}</div>}
    <section className="quant-panel"><div className="quant-section-title"><h2>我的运行记录</h2><span>自动刷新 · 规则与结果独立留存</span></div><div className="quant-scroll"><table className="quant-table"><thead><tr><th>策略 / 区间</th><th>状态与进度</th><th>收益 / 回撤</th><th>操作</th></tr></thead><tbody>{catalog.tasks.map((task) => <tr key={task.id}><td>{task.spec.name}<small>{task.request.start} — {task.request.end} · {task.request.sample_limit ? `前${task.request.sample_limit}只` : '全量'}</small><small>{new Date(task.created_at).toLocaleString('zh-CN')}</small></td><td><span className={`quant-task-status ${task.status}`}>{statusNames[task.status]}</span><small>{task.progress}{task.status === 'running' && task.total ? ` ${task.completed || 0} / ${task.total}` : ''}</small></td><td>{task.metrics ? `${task.metrics.total_return_pct.toFixed(2)}% / ${task.metrics.max_drawdown_pct.toFixed(2)}%` : '—'}</td><td><button className="btn ghost" disabled={task.status !== 'completed'} onClick={() => onView(task)}>查看结果</button><button className="btn ghost" disabled={!!busy} onClick={() => { setDraft(task.spec); setStart(task.request.start); setEnd(task.request.end); setSampleLimit(task.request.sample_limit); setGeneration(task.spec.unresolved_requirements?.length || task.spec.interpretation_notes?.length ? { assumptions: task.spec.interpretation_notes || [], unsupported: task.spec.unresolved_requirements || [] } : null); setConfirmed(false); pendingRequest.current = null; dirtyVersion.current += 1; window.scrollTo({ top: 0, behavior: 'smooth' }) }}>复制配置</button></td></tr>)}</tbody></table></div>{!catalog.tasks.length && <p className="quant-empty">保存你的第一个策略，运行后会在这里保留记录。</p>}</section>
  </div>
}
