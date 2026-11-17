import { makeCondition, makeGroup, makeIndicator, metricNames, operatorNames, periodMetrics } from './ruleVocabulary'

function Operand({ value, onChange, allowConstant, label }) {
  const metric = value.type === 'constant' ? 'constant' : value.metric
  return <div className="quant-operand">
    {value.type === 'indicator' && <select aria-label={`${label}周期`} value={value.timeframe} onChange={(e) => onChange({ ...value, timeframe: e.target.value })}><option value="60F">60F</option><option value="1D">日K</option></select>}
    <select aria-label={`${label}指标`} value={metric} onChange={(e) => onChange(e.target.value === 'constant' ? { type: 'constant', value: 0 } : { ...makeIndicator(e.target.value), timeframe: value.timeframe || '60F', lag: value.lag || 0, multiplier: value.multiplier || 1 })}>
      {allowConstant && <option value="constant">数值</option>}{Object.entries(metricNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
    </select>
    {value.type === 'constant' ? <input aria-label={`${label}数值`} type="number" step="any" required value={value.value} onChange={(e) => onChange({ ...value, value: e.target.value === '' ? '' : Number(e.target.value) })} /> : <>
      {periodMetrics.has(metric) && <input aria-label={`${label}指标参数`} title="指标计算根数" type="number" min={2} max={250} step={1} required value={value.period} onChange={(e) => onChange({ ...value, period: e.target.value === '' ? '' : Number(e.target.value) })} />}
      <details className="quant-reference"><summary title="过去引用与倍数">{value.lag || value.multiplier !== 1 ? `前${value.lag}根 ×${value.multiplier}` : '引用'}</summary><label>向前引用<input aria-label={`${label}向前引用`} type="number" min={0} max={250} step={1} value={value.lag} onChange={(e) => onChange({ ...value, lag: Number(e.target.value) })} /></label><label>乘以倍数<input aria-label={`${label}倍数`} type="number" min={.001} max={100} step="any" value={value.multiplier} onChange={(e) => onChange({ ...value, multiplier: Number(e.target.value) })} /></label></details>
    </>}
  </div>
}

function GroupEditor({ value, onChange, depth = 0 }) {
  const replace = (index, child) => onChange({ ...value, children: value.children.map((current, i) => i === index ? child : current) })
  return <div className="quant-rule-group">
    <div className="quant-group-toolbar"><select aria-label="条件组关系" value={value.logic} onChange={(e) => onChange({ ...value, logic: e.target.value })}><option value="all">全部满足 AND</option><option value="any">任一满足 OR</option></select><span>{value.children.length}项条件</span></div>
    {value.children.map((node, index) => <div className={node.type === 'group' ? 'quant-nested-rule' : 'quant-condition'} key={index}>
      {node.type === 'group' ? <GroupEditor value={node} onChange={(child) => replace(index, child)} depth={depth + 1} /> : <>
        <Operand label="左侧" value={node.left} onChange={(left) => replace(index, { ...node, left })} />
        <select aria-label="比较关系" value={node.operator} onChange={(e) => replace(index, { ...node, operator: e.target.value })}>{Object.entries(operatorNames).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</select>
        <Operand label="右侧" value={node.right} allowConstant onChange={(right) => replace(index, { ...node, right })} />
      </>}
      <button className="quant-remove-rule" type="button" aria-label="移除条件" disabled={value.children.length === 1} onClick={() => onChange({ ...value, children: value.children.filter((_, i) => i !== index) })}>×</button>
    </div>)}
    <div className="quant-rule-actions"><button className="btn ghost" type="button" disabled={value.children.length >= 12} onClick={() => onChange({ ...value, children: [...value.children, makeCondition()] })}>＋ 条件</button><button className="btn ghost" type="button" disabled={depth >= 2 || value.children.length >= 12} onClick={() => onChange({ ...value, children: [...value.children, makeGroup()] })}>＋ 条件组</button></div>
  </div>
}

export default function RuleBuilder({ value, onChange }) {
  const set = (key, next) => onChange({ ...value, [key]: next })
  return <div className="quant-rule-builder">
    <div className="quant-section-title"><h3>入场条件</h3><span>可组合指标、比较、交叉与嵌套条件组</span></div><GroupEditor value={value.entry} onChange={(entry) => set('entry', entry)} />
    <div className="quant-section-title"><h3>离场条件</h3>{value.exit ? <button className="btn ghost" type="button" onClick={() => set('exit', null)}>仅用止盈止损 / 超时</button> : <button className="btn ghost" type="button" onClick={() => set('exit', { ...makeGroup(), children: [makeCondition('cross_down')] })}>＋ 定义指标离场</button>}</div>
    {value.exit && <GroupEditor value={value.exit} onChange={(exit) => set('exit', exit)} />}
    <div className="quant-form-grid"><label className="quant-field"><span>条件评估时点</span><select value={value.sampling} onChange={(e) => set('sampling', e.target.value)}><option value="at_close">仅15:00收盘</option><option value="any_completed_60f">当天任一完成60F</option></select></label><label className="quant-field"><span>入场触发</span><select value={value.trigger} onChange={(e) => set('trigger', e.target.value)}><option value="edge">条件由假转真</option><option value="level">条件满足即可</option></select></label><label className="quant-field"><span>信号冷却 / 60F根（纯日K按日）</span><input type="number" value={value.cooldown_bars} min={0} max={250} step={1} required onChange={(e) => set('cooldown_bars', Number(e.target.value))} /></label></div>
    <p className="quant-rule-help">前N根高低点不含当前K线；RSI使用Wilder平滑，MACD固定12/26/9。价格指标按真实因子复权，名义股价筛选请选择“不复权收盘价”。只用已完成K线，下一交易日执行。</p>
  </div>
}
