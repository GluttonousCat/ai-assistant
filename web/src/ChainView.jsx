import { useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'
import Markdownish from './Markdownish'

// 新建链弹窗: 双通道共用 — LLM 生成草稿(可编辑) / 直接手写 YAML; 保存前服务端校验
function ForgeModal({ onClose, onCreated }) {
  const [theme, setTheme] = useState('')
  const [stages, setStages] = useState([])
  const [busy, setBusy] = useState(false)
  const [yamlText, setYamlText] = useState('')
  const [report, setReport] = useState(null)
  const [rounds, setRounds] = useState(0)
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)
  const forgeRef = useRef(null)

  useEffect(() => () => forgeRef.current?.abort(), [])

  function generate() {
    const t = theme.trim()
    if (!t || busy) return
    setBusy(true); setErr(''); setStages([]); setReport(null); setRounds(0)
    const handle = platformApi.forgeChainRaw(t, (ev) => {
      if (ev.type === 'stage') {
        setStages((s) => [...s.map((x) => ({ ...x, ok: true })), { message: ev.message, ok: false }])
      } else if (ev.type === 'data') {
        setYamlText(ev.yaml || '')
        setReport(ev.report || null)
        setRounds(ev.rounds || 0)
      } else if (ev.type === 'error') {
        setErr(ev.message || '生成失败')
      }
    })
    forgeRef.current = handle
    handle.promise
      .catch((e) => setErr(e.message))
      .finally(() => { setBusy(false); setStages((s) => s.map((x) => ({ ...x, ok: true }))) })
  }

  async function save() {
    if (!yamlText.trim() || saving) return
    setSaving(true); setErr('')
    try {
      const r = await platformApi.saveChain(yamlText)
      onCreated(r.chain_id)
      onClose()
    } catch (e) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="forge-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="forge-panel">
        <div className="forge-head">
          <b>新建产业链</b>
          <span className="text-dim">LLM 生成草稿 → 编辑 → 保存；或直接手写 YAML</span>
          <button className="btn ghost sm" onClick={onClose}>关闭</button>
        </div>
        <div className="forge-theme">
          <input
            value={theme}
            onChange={(e) => setTheme(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && generate()}
            placeholder="主题, 如: 磷化工产业链 / 存储产业链 / 低空经济"
            disabled={busy}
          />
          <button className="btn" onClick={generate} disabled={busy || !theme.trim()}>
            {busy ? '生成中…' : '✦ 生成草稿'}
          </button>
        </div>
        {stages.length > 0 && (
          <div className="agent-steps">
            {stages.map((s, i) => (
              <div key={i} className={`agent-step ${s.ok ? 'ok' : 'active'}`}>
                <span className="agent-dot">{s.ok ? '✓' : ''}</span>{s.message}
              </div>
            ))}
          </div>
        )}
        <textarea
          className="forge-yaml"
          value={yamlText}
          onChange={(e) => setYamlText(e.target.value)}
          spellCheck={false}
          placeholder={'手写通道: 复制 beta_alpha/chains/_template.yaml 到这里编辑后保存'}
        />
        {report && (
          <div className="forge-report">
            <div className="forge-report-head">
              关键词命中率 (vs 主营构成披露){rounds > 1 ? ' · 已自动修正一轮' : ''}
            </div>
            {report.nodes.map((n) => (
              <div key={n.id} className="forge-report-node">
                <span className={`forge-node-name ${n.total_stocks === 0 ? 'miss' : ''}`}>
                  {n.name} <em>{n.total_stocks}只</em>
                </span>
                {n.keywords.map((kw) => (
                  <span key={kw.keyword} className={`kw-chip ${kw.stocks === 0 ? 'miss' : 'hit'}`}
                    title={kw.samples?.length ? `样例: ${kw.samples.join(' / ')}` : ''}>
                    {kw.keyword} {kw.stocks}
                  </span>
                ))}
              </div>
            ))}
          </div>
        )}
        {err && <div className="chain-error">❌ {err}</div>}
        <div className="forge-actions">
          <span className="text-dim">保存即上线 (chains/&lt;chain_id&gt;.yaml), 可随时编辑再存</span>
          <button className="btn" onClick={save} disabled={saving || !yamlText.trim()}>
            {saving ? '校验保存中…' : '保存并分析'}
          </button>
        </div>
      </div>
    </div>
  )
}

// 产业链页: 链选择 -> 环节卡片墙(超额/景气/成员) -> AI 流式解读(按需)
// 标的 chip 点击 -> 跳对话页自动发「XX的预期差」(chat:pending 机制)
function cls(v) { return v == null ? 'na' : v > 0 ? 'up' : 'down' }
function fmtPct(v) { return v == null ? '—' : `${v > 0 ? '+' : ''}${v}%` }
const TIER_CLS = { 强: 'strong', 中: 'mid', 弱: 'weak' }

export default function ChainView() {
  const [chains, setChains] = useState([])
  const [active, setActive] = useState(null)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [aiText, setAiText] = useState('')
  const [aiBusy, setAiBusy] = useState(false)
  const [openNode, setOpenNode] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [forgeOpen, setForgeOpen] = useState(false)
  const aiRef = useRef(null)

  useEffect(() => {
    platformApi.chains()
      .then((r) => {
        setChains(r.chains || [])
        if (r.chains?.length) setActive((a) => a || r.chains[0].chain_id)
      })
      .catch((e) => setErr(e.message))
  }, [])

  function handleCreated(chainId) {
    platformApi.chains()
      .then((r) => setChains(r.chains || []))
      .catch(() => {})
    setActive(chainId)   // 自动切到新链 (analysis effect 触发)
  }

  useEffect(() => {
    if (!active) return
    stopAI()
    setLoading(true); setErr(''); setData(null); setAiText(''); setOpenNode(null)
    platformApi.chainAnalysis(active)
      .then(setData)
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, refreshKey])

  useEffect(() => () => stopAI(), []) // 离开页面中断 AI 流

  function stopAI() {
    aiRef.current?.abort()
    aiRef.current = null
    setAiBusy(false)
  }

  function askAI() {
    if (!data || aiBusy) return
    stopAI()
    setAiText(''); setAiBusy(true)
    const handle = platformApi.chatStreamRaw(`${data.chain.name}全景解读`, (ev) => {
      if (ev.type === 'delta') setAiText((t) => t + ev.text)
      else if (ev.type === 'error') setAiText((t) => t + `\n❌ ${ev.message}`)
    })
    aiRef.current = handle
    handle.promise
      .catch((e) => setAiText((t) => t + `\n❌ ${e.message}`))
      .finally(() => setAiBusy(false))
  }

  function gotoAlpha(name) {
    sessionStorage.setItem('chat:pending', `${name}的预期差`)
    window.location.hash = 'chat'
  }

  const activeMeta = chains.find((c) => c.chain_id === active)

  return (
    <div className="chain-page">
      <div className="chain-head">
        <div className="chain-chips">
          {chains.map((c) => (
            <button
              key={c.chain_id}
              className={`chain-chip ${active === c.chain_id ? 'on' : ''}`}
              onClick={() => setActive(c.chain_id)}
            >
              {c.name}
            </button>
          ))}
          {!chains.length && !err && <span className="text-dim">加载产业链…</span>}
        </div>
        {activeMeta && (
          <div className="chain-sub">
            <span className="chain-desc">{activeMeta.desc}</span>
            {activeMeta.drivers?.length > 0 && (
              <span className="chain-drivers">驱动: {activeMeta.drivers.join(' / ')}</span>
            )}
          </div>
        )}
        <div className="chain-toolbar">
          <span className="text-dim">
            {data ? `基准 ${data.benchmark} · 120日窗口 · 市值加权` : ''}
          </span>
          <span className="spacer" />
          <button className="btn ghost" onClick={() => setForgeOpen(true)}>＋ 新建链</button>
          <button className="btn ghost" onClick={() => setRefreshKey((k) => k + 1)} disabled={loading}>
            {loading ? '计算中…' : '刷新'}
          </button>
          <button className="btn" onClick={askAI} disabled={!data || aiBusy}>
            {aiBusy ? '解读生成中…' : '✦ AI 解读本链'}
          </button>
        </div>
      </div>

      {err && <div className="chain-error">❌ {err}</div>}
      {loading && (
        <div className="chain-loading">
          <span className="spinner-dot" /> 三源映射与环节指数计算中 (约数秒)…
        </div>
      )}

      {data && !loading && (
        <>
          <div className="chain-grid">
            {data.nodes.map((n) => {
              const m = n.metrics || {}
              const ex = m.excess_120d
              const open = openNode === n.id
              return (
                <div key={n.id} className={`chain-card ${open ? 'open' : ''}`}>
                  <div className="chain-card-head" onClick={() => setOpenNode(open ? null : n.id)}>
                    <div className="chain-card-title">
                      {n.name}
                      <span className="chain-tier">
                        强{n.strong_count}·中{n.medium_count}
                      </span>
                    </div>
                    <div className={`chain-excess ${cls(ex)}`}>
                      <b>{fmtPct(ex)}</b>
                      <span>120日超额</span>
                    </div>
                  </div>
                  <div className="chain-metrics">
                    <span>20日超额 <b className={cls(m.excess_20d)}>{fmtPct(m.excess_20d)}</b></span>
                    <span>营收中位 <b>{fmtPct(m.or_yoy_median)}</b></span>
                    <span>研报热度 <b>{n.report_heat_6m ?? 0}</b></span>
                    {n.chain_mentions > 0 && <span>环节提及 <b>{n.chain_mentions}</b></span>}
                  </div>
                  <div className="chain-members">
                    {n.members.slice(0, 6).map((mem) => (
                      <button
                        key={mem.ts_code}
                        className={`member-chip t-${TIER_CLS[mem.tier] || 'weak'}`}
                        title={mem.evidence}
                        onClick={() => gotoAlpha(mem.name)}
                      >
                        {mem.name}
                        {mem.mainbz_share != null && <em>{mem.mainbz_share}%</em>}
                      </button>
                    ))}
                    {!n.members.length && <span className="text-dim">暂无匹配标的</span>}
                  </div>
                  {open && (
                    <table className="grid chain-member-table">
                      <thead>
                        <tr><th>标的</th><th>档位</th><th>主营占比</th><th>主营业务</th><th>研报</th><th>证据</th></tr>
                      </thead>
                      <tbody>
                        {n.members.map((mem) => (
                          <tr key={mem.ts_code}>
                            <td><button className="link-like" onClick={() => gotoAlpha(mem.name)}>{mem.name}</button></td>
                            <td><span className={`tier-dot t-${TIER_CLS[mem.tier] || 'weak'}`}>{mem.tier}</span></td>
                            <td>{mem.mainbz_share != null ? `${mem.mainbz_share}%` : '—'}</td>
                            <td>{mem.mainbz_items || '—'}</td>
                            <td>{mem.report_hits || 0}</td>
                            <td className="evi">{mem.evidence}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )
            })}
          </div>

          {(aiText || aiBusy) && (
            <div className="chain-ai">
              <div className="chain-ai-head">
                <span className="chat-avatar sm">✦</span> AI 解读
                {aiBusy && <span className="spinner-dot" />}
                {!aiBusy && aiText && (
                  <button className="btn ghost sm" onClick={askAI}>重新生成</button>
                )}
              </div>
              <div className={`chat-bubble md ${aiBusy ? 'streaming' : ''}`}>
                {aiText ? <Markdownish text={aiText} /> : (
                  <div className="chat-bubble typing"><span></span><span></span><span></span></div>
                )}
              </div>
            </div>
          )}

          <div className="chain-foot text-dim">
            档位: 强=主营占比≥30% / 中=10~30%或行业+研报双命中 · 环节指数=强+中档市值加权 ·
            点击标的名跳转对话页查「预期差」 · 数据来自主营构成/申万L2/研报三源
          </div>
        </>
      )}

      {forgeOpen && (
        <ForgeModal onClose={() => setForgeOpen(false)} onCreated={handleCreated} />
      )}
    </div>
  )
}
