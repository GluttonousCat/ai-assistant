import IndustryLabel from './IndustryLabel'

function FollowUpRows({ rows, industries }) {
  return <ul className="intraday-follow-list">
    {rows.map((row) => <li key={row.ts_code}>
      <span><b>{row.name}</b> <small>{row.ts_code}</small><IndustryLabel code={row.ts_code} industries={industries} /></span>
      <span>{row.events.length ? row.events.join(' · ') : row.state}</span>
      <small>上次入选 {row.last_seen}{row.change_pct != null ? ` · 较昨收 ${row.change_pct > 0 ? '+' : ''}${row.change_pct.toFixed(2)}%` : ''}</small>
    </li>)}
  </ul>
}

export default function IntradayFollowUp({ rows, industries }) {
  const changes = rows?.filter((row) => row.events.length) || []
  const others = rows?.filter((row) => !row.events.length) || []
  const unverified = rows?.filter((row) => row.scan_verified === false).length || 0
  return <section className="intraday-follow" aria-label="历史标的跟踪">
    <h3>历史标的跟踪</h3>
    {!rows ? <p>本轮还没有历史跟踪结果。</p> : <>
      <p>跟踪 {rows.length} 只 · 记录变化 {changes.length} 只{unverified > 0 ? ` · 扫描待核验 ${unverified} 只` : ''}</p>
      {changes.length ? <FollowUpRows rows={changes} industries={industries} /> : <p>本轮暂无已记录的信号变化。</p>}
      {others.length > 0 && <details><summary>查看其余 {others.length} 只</summary>
        <FollowUpRows rows={others} industries={industries} />
      </details>}
    </>}
  </section>
}
