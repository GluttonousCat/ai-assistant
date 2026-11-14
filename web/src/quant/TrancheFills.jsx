const number = (v, digits = 2) => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits })

export default function TrancheFills({ fills }) {
  if (!fills?.length) return null
  const buys = fills.filter((f) => f.side === 'buy').length
  const sells = fills.length - buys
  return <details className="quant-tranche-fills"><summary>分批成交 · {buys} 买 / {sells} 卖</summary><div className="quant-scroll"><table className="quant-table"><thead><tr><th>时间</th><th>方向</th><th>数量 / 价格</th><th>金额 / 费用</th></tr></thead><tbody>{fills.map((f, i) => <tr key={`${f.time}-${f.side}-${i}`}><td>{f.time.replace('T', ' ').slice(0, 16)}</td><td>{f.side === 'buy' ? '买入' : '卖出'}</td><td>{number(f.quantity)}<small>{number(f.price, 4)} 元</small></td><td>{number(f.notional)}<small>费用 {number(f.fee)}</small></td></tr>)}</tbody></table></div><small>逐笔成交费用已计入；完整持仓周期只计一笔胜负。</small></details>
}
