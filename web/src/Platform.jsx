import { useEffect, useRef, useState } from 'react'
import { platformApi, auth } from './platformApi'
import AgentChat from './AgentChat'
import Reports from './Reports'
import ChainView from './ChainView'
import UserAdmin from './UserAdmin' // user management (admin only)
import Monitor from './Monitor' // real-time monitor (agent sessions/tools/LLM, admin only)
import StockDashboard from './market/StockDashboard'
import QuantDashboard from './quant/QuantDashboard'

// Navigation definition: items with adminOnly are greyed out and unclickable for non-admins

const NAV = [
  { key: 'chat', label: '智能对话', icon: '✦' },
  { key: 'market', label: '股票行情', icon: '▥' },
  { key: 'quant', label: '量化策略', icon: '∿' },
  { key: 'chain', label: '行业挖掘', icon: '⛓' },
  { key: 'reports', label: '研报中心', icon: '▤' },
  { key: 'monitor', label: '实时监控', icon: '◉', adminOnly: true },
  { key: 'users', label: '用户管理', icon: '⚙', superOnly: true },       // rendered for super admin only
]

const SIDEBAR_PREFERENCE = 'alpha:sidebar-collapsed'

// Platform shell: sidebar nav + top bar (user info/logout) + role gating
export default function Platform({ user, onLogout }) {
  const isAdmin = user?.role === 'admin'
  const isSuper = isAdmin && user?.is_super_admin === true
  const sidebarToggle = useRef(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      const saved = localStorage.getItem(SIDEBAR_PREFERENCE)
      if (saved === 'true' || saved === 'false') return saved === 'true'
    } catch { /* Storage restrictions must not prevent navigation. */ }
    return window.matchMedia('(max-width: 700px)').matches
  })
  const [nav, setNav] = useState(() => {
    const target = NAV.find((n) => n.key === window.location.hash.slice(1))
    return target && !(target.adminOnly && !isAdmin) && !(target.superOnly && !isSuper) ? target.key : 'chat'
  })

  useEffect(() => {
    const onExpired = () => onLogout()
    window.addEventListener('auth:expired', onExpired)
    return () => window.removeEventListener('auth:expired', onExpired)
  }, [onLogout])

  useEffect(() => {
    const syncHash = () => {
      const hash = window.location.hash.replace('#', '')
      const target = NAV.find((n) => n.key === hash)
      // Direct hash navigation to an under-privileged target (adminOnly/superOnly) -> fall back to chat page
      if (target && !(target.adminOnly && !isAdmin) && !(target.superOnly && !isSuper)) {
        setNav(hash)
      } else if (target) setNav('chat')
    }
    syncHash()
    window.addEventListener('hashchange', syncHash)
    return () => window.removeEventListener('hashchange', syncHash)
  }, [isAdmin, isSuper])

  useEffect(() => {
    window.location.hash = nav
  }, [nav])

  useEffect(() => {
    try { localStorage.setItem(SIDEBAR_PREFERENCE, String(sidebarCollapsed)) }
    catch { /* Keep the current layout usable without persistent storage. */ }
  }, [sidebarCollapsed])

  function collapseSidebar() {
    setSidebarCollapsed(true)
    sidebarToggle.current?.focus()
  }

  return (
    <div className={`platform ${sidebarCollapsed ? 'sidebar-collapsed' : 'sidebar-expanded'}`}>
      {!sidebarCollapsed && <button className="sidebar-backdrop" aria-label="收起侧边栏" onClick={collapseSidebar} />}
      <aside className={`sidebar ${sidebarCollapsed ? 'is-collapsed' : ''}`} aria-label="Alpha Finance Radar 导航" onKeyDown={(e) => { if (e.key === 'Escape') collapseSidebar() }}>
        <div className="side-brand">
          <span className="logo-mark" aria-hidden="true">◆</span>
          <div className="brand-copy">
            <div className="brand-name">Alpha Finance Radar</div>
            <div className="brand-sub">Insight Engine</div>
          </div>
          <button ref={sidebarToggle} type="button" className="sidebar-toggle" onClick={() => setSidebarCollapsed((value) => !value)} aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'} title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'} aria-expanded={!sidebarCollapsed} aria-controls="platform-navigation">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M9 4v16" /><path d={sidebarCollapsed ? 'm13 9 3 3-3 3' : 'm16 9-3 3 3 3'} /></svg>
          </button>
        </div>
        <nav className="side-nav" id="platform-navigation" aria-label="主导航">
          {NAV.filter((n) => !n.superOnly || isSuper).map((n) => {
            const locked = n.adminOnly && !isAdmin
            return (
              <button
                key={n.key}
                className={nav === n.key ? 'on' : ''}
                disabled={locked}
                title={locked ? `${n.label}（仅管理员可用）` : n.label}
                aria-label={locked ? `${n.label}（仅管理员可用）` : n.label}
                aria-current={nav === n.key ? 'page' : undefined}
                onClick={() => { setNav(n.key); if (window.matchMedia('(max-width: 700px)').matches) collapseSidebar() }}
              >
                <span className="nav-icon" aria-hidden="true">{locked ? '🔒' : n.icon}</span><span className="nav-label">{n.label}</span>
              </button>
            )
          })}
        </nav>
        <div className="side-foot">
          <div className="user-chip" title={`${user.display_name || user.username} · ${isAdmin ? '管理员' : '研究员'}`}>
            <span className="avatar">{(user.display_name || user.username || '?').slice(0, 1).toUpperCase()}</span>
            <div className="user-meta">
              <div className="user-name">{user.display_name || user.username}</div>
              <div className="user-role">{isAdmin ? '管理员' : '研究员'}</div>
            </div>
          </div>
          <button className="btn ghost logout-btn" onClick={onLogout} aria-label="退出登录" title="退出登录"><svg className="logout-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 5H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4M9 12h12m-4-4 4 4-4 4" /></svg><span className="nav-label">退出登录</span></button>
        </div>
      </aside>

      <main className="main">
        {nav === 'chat' && <AgentChat />}
        {nav === 'market' && <StockDashboard key={user.username} user={user} />}
        {nav === 'quant' && <QuantDashboard />}
        {nav === 'chain' && <ChainView />}
        {nav === 'reports' && <Reports />}
        {nav === 'monitor' && isAdmin && <Monitor />}
        {nav === 'users' && isSuper && <UserAdmin />}
      </main>
    </div>
  )
}

// Persistent login session check
export function useSession() {
  const [user, setUser] = useState(null)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    const saved = auth.getUser()
    const token = auth.getToken()
    if (saved && token) {
      platformApi
        .me()
        .then((me) => setUser({ ...saved, ...me }))
        .catch(() => auth.clear())
        .finally(() => setChecked(true))
    } else {
      setChecked(true)
    }
  }, [])

  return { user, checked, setUser }
}
