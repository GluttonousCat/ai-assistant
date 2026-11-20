import IndustryLabel from './IndustryLabel'

function FollowUpRows({ rows, industries }) {
  return <ul className="intraday-follow-list">
    {rows.map((row) => <li key={row.ts_code}>
      <span><b>{row.name}</b> <small>{row.ts_code}</small><IndustryLabel code={row.ts_code} industries={industries} /></span>
      <span>{row.retired_at ? `${row.retired_at.slice(11, 16)} 连续两根60F收破参考位，移出本轮` : row.events.length ? row.events.join(' · ') : row.state}</span>
      <small>上次入选 {row.last_seen}{row.change_pct != null ? ` · 较昨收 ${row.change_pct > 0 ? '+' : ''}${row.change_pct.toFixed(2)}%` : ''}</small>
    </li>)}
  </ul>
}

export default function IntradayFollowUp({ rows, industries }) {
  const retired = rows?.filter((row) => row.retired_at) || []
  const active = rows?.filter((row) => !row.retired_at) || []
  const changes = active.filter((row) => row.events.length)
  const others = active.filter((row) => !row.events.length)
  const unverified = active.filter((row) => row.scan_verified === false).length
  return <section className="intraday-follow" aria-label="历史标的跟踪">
    <h3>历史标的跟踪</h3>
    {!rows ? <p>本轮还没有历史跟踪结果。</p> : <>
      <p>跟踪中 {active.length} 只 · 今日移出 {retired.length} 只{unverified > 0 ? ` · 扫描待核验 ${unverified} 只` : ''}</p>
      {retired.length > 0 && <FollowUpRows rows={retired} industries={industries} />}
      {changes.length > 0 && <FollowUpRows rows={changes} industries={industries} />}
      {!retired.length && !changes.length && <p>本轮暂无已记录的信号变化。</p>}
      {others.length > 0 && <details><summary>查看其余 {others.length} 只</summary>
        <FollowUpRows rows={others} industries={industries} />
      </details>}
    </>}
  </section>
}
