const day = (value) => value ? value.slice(0, 10) : '暂无'
const stages = { daily: '更新日K与复权因子', universe: '更新股票池', '60F': '补齐60F', audit: '核对数据完整性', snapshot: '重算看板' }

export default function MarketFreshness({ value }) {
  if (!value) return null
  const { warehouse = {}, job = {} } = value
  const snapshotBehind = !value.snapshot_as_of || day(value.snapshot_as_of) < value.expected_date
  const running = job.status === 'running'
  return <div className="trend-freshness" role="status" aria-live="polite">
    <div><b>数据更新</b><span>日K入库 {day(warehouse.daily_end)}</span><span>60F入库 {day(warehouse.bar_end)}</span><span>看板截止 {day(value.snapshot_as_of)}</span></div>
    <p>{running ? `${stages[job.stage] || '后台更新中'}${['60F', 'snapshot'].includes(job.stage) ? ` · ${job.completed || 0} / ${job.total || 0} 只` : ''}，完成后自动刷新。` : ['error', 'partial'].includes(job.status) ? '更新尚未完成，保留已发布快照。' : snapshotBehind ? '看板尚未更新到最近应有交易日。' : '已展示最近应有交易日的快照。'} 应有交易日 {value.expected_date} · {value.schedule_enabled ? `交易日 ${value.schedule_time} 自动更新` : '定时更新已关闭'}（上海时间）。</p>
    {job.coverage?.daily_absent > 0 && <p className="trend-freshness-detail">目标日 {job.coverage.daily_absent} 只缺少日K记录{job.coverage.suspended != null ? `，其中 ${job.coverage.suspended} 只已核实全天停牌` : '，尚未核实停牌状态'}。库内日期为最新记录日期，完整性以逐股审计为准。</p>}
  </div>
}
