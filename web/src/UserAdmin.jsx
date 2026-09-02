import { useEffect, useState } from 'react'
import { platformApi } from './platformApi'

// 用户管理 (仅 admin): 账号列表 + 角色切换 (赋予/收回管理员)
export default function UserAdmin() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyId, setBusyId] = useState(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await platformApi.listUsers()
      setItems(res.items || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function toggleRole(u) {
    const next = u.role === 'admin' ? 'user' : 'admin'
    if (!window.confirm(`确认将 ${u.username} ${next === 'admin' ? '提升为' : '降级为'}${next === 'admin' ? '管理员' : '普通用户'}？`)) return
    setBusyId(u.user_id)
    try {
      await platformApi.setUserRole(u.user_id, next)
      await load()
    } catch (e) {
      alert(`操作失败: ${e.message}`)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>用户管理</h2>
          <p className="sub">账号列表与角色管理 · 仅管理员可见</p>
        </div>
        <button className="btn ghost" onClick={load}>刷新</button>
      </div>

      <div className="table-wrap">
        {loading ? (
          <div className="loading"><div className="spinner" />加载中…</div>
        ) : error ? (
          <div className="error">{error}</div>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>ID</th><th>用户名</th><th>角色</th><th>状态</th>
                <th>最后登录</th><th>注册时间</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((u) => (
                <tr key={u.user_id}>
                  <td className="dim">#{u.user_id}</td>
                  <td className="sym">{u.username}</td>
                  <td>
                    <span className={`badge ${u.role === 'admin' ? 'EARLY' : 'FORMATION'}`}>
                      {u.role === 'admin' ? '管理员' : '用户'}
                    </span>
                  </td>
                  <td className="dim">{u.is_active ? '正常' : '停用'}</td>
                  <td className="dim">{u.last_login_at || '—'}</td>
                  <td className="dim">{u.created_at || '—'}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <button
                      className="btn ghost"
                      style={{ padding: '4px 12px' }}
                      disabled={busyId === u.user_id}
                      onClick={() => toggleRole(u)}
                    >
                      {busyId === u.user_id ? '处理中…'
                        : u.role === 'admin' ? '降为用户' : '设为管理员'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
