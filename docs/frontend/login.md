# 登录系统设计文档

> Alpha Finance Radar 平台 · 用户认证子系统
> 更新：2026-08-30

## 一、系统设计

### 1.1 架构

```
浏览器 (React SPA)                     后端 (FastAPI)
┌──────────────────┐   POST /api/auth/login    ┌─────────────────────┐
│ Login.jsx        │ ─────────────────────────> │ api/auth_api.py     │
│  用户名/密码      │ <───────────────────────── │  → core/security    │
│  JWT 存 localStorage │  {token, user}          │    bcrypt 校验       │
└──────────────────┘                           │    签发 JWT (HS256)  │
        │  Authorization: Bearer <token>        └─────────────────────┘
        └──────────────> 所有 /api/* 请求 → api/middleware.py 校验
                                              → api/deps.py get_current_user
```

### 1.2 关键组件

| 文件 | 职责 |
|------|------|
| `web/src/Login.jsx` | 登录/注册双模式表单，前端校验 |
| `web/src/Root.jsx` | 根路由：有 token → 平台；无 → 登录页；启动时 `/me` 校验 token 有效性 |
| `web/src/platformApi.js` | 统一请求层：自动附带 Bearer token；401 时清除登录态并广播 `auth:expired` 事件跳回登录页 |
| `core/security.py` | bcrypt 哈希（cost 12）、JWT 签发/解析（AUTH_SECRET，默认 12h 过期）、用户表读写 |
| `api/auth_api.py` | /login /register /me /change-password 接口 |
| `api/middleware.py` | 全局鉴权中间件（白名单：登录/注册/健康检查/静态资源） |
| PG `auth.users` | 用户表（username 唯一、password_hash、role admin/user、is_active、last_login_at） |

### 1.3 设计决策

- **JWT 而非 Session**：前后端分离 + 无状态校验，适合 SPA；登出即前端清 token
- **bcrypt cost 12**：单次校验 ~100ms，暴力破解成本足够
- **登录态持久化**：localStorage 存 token + user，页面刷新不掉线；启动时调 `/me` 验 token，过期自动踢回登录页
- **种子管理员**：首次启动自动建 `admin/admin123`（.env `AUTH_ADMIN_USER/PASSWORD` 可覆盖）——公网部署后必须改密

## 二、注册规则（本轮需求 1 实现）

| 字段 | 规则 | 说明 |
|------|------|------|
| 注册码 | 必填，当前 `123456` | 受控注册（`.env AUTH_INVITE_CODE` 可改）；无码/错码 → 403 |
| 用户名 | 仅英文字母/数字/下划线，≥6 位 | 正则 `^[A-Za-z0-9_]{6,}$`，防中文/特殊字符 |
| 密码 | ≥8 位 | 注册时强制；登录时只查非空 |

> 「显示名称」字段已移除（2026-08-30）：注册只收 用户名/密码/注册码，display_name 默认等于用户名。

### 校验层次（双层防绕过）

1. **前端**（`Login.jsx validate()`）：提交前即时提示「用户名仅限英文字母/数字/下划线, 至少 6 位」「密码至少 8 位」
2. **后端**（`api/auth_api.py RegisterRequest`）：
   - Pydantic `Field(min_length=6/8)` → 长度不足返回 422
   - `re.fullmatch(r"[A-Za-z0-9_]{6,64}")` → 字符类型不合法返回 400（前端可被绕过，后端强制）
   - `core/security.create_user` 兜底「密码至少 8 位」

## 二.5 角色与权限体系（2026-08-31）

| 角色 | 权限 |
|------|------|
| 超级管理员（`Gluttonouscat`，.env `SUPER_ADMIN` 可改） | 全部 + 区间看板 + **用户管理（独占）** |
| `admin`（其他管理员） | Agent 对话 / 研报中心 / **区间看板可看可点**；**用户管理完全不可见**（导航不渲染，API 403） |
| `user` | Agent 对话 / 研报中心；区间看板置灰 🔒；用户管理不可见 |

**机制三层**：
1. 前端门控：`Platform.jsx` 导航项 `adminOnly` → 非 admin `disabled` + 🔒 图标；hash 直达也回落对话页
2. API 门控：`/api/range/*` 整组挂 `require_admin`；`GET /api/auth/users`、`POST /api/auth/users/{id}/role` 同
3. **实时校验**：`require_admin` 以数据库当前角色为准（不信 JWT 内 role）——提权/降权**无需重新登录，即时生效**
   （防降权后旧 token 残留 admin 权限 12h）

**管理操作**：仅超级管理员在「用户管理」页切换任意账号角色（其他 admin 连入口都看不到，
API 层 `_require_super` 同样拦截，用户名匹配 .env `SUPER_ADMIN`，默认 Gluttonouscat）；防呆：不能改自己的角色。

**区间看板**：所有 admin（超级管理员+被提升的管理员）可见可点；user 置灰。

**种子账号策略变更**：`ensure_auth_schema` 原"admin 用户不存在就自动重建"会导致删除弱口令账号后重启复活
（实际发生过）——已改为**仅用户表完全为空时**才创建种子（首次部署引导）。日常管理员任命走用户管理页。

## 三、密码可见性切换（小眼睛）

`Login.jsx` 密码框右侧眼睛按钮（👁/🙈）：

- `.pwd-wrap` 相对定位容器 + `.pwd-input`（`padding-right: 42px` 给按钮留位）
- `.pwd-toggle` 绝对定位在右端，`type="button"`（不会触发表单提交）
- 点击切换 input 的 `type` 在 `password` / `text` 之间，aria-label 同步「显示密码/隐藏密码」

## 四、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| 登录失败提示显示「登录已失效」而非「用户名或密码错误」 | 前端统一 401 拦截器把登录接口的 401（密码错）也当成登录态失效 | `platformApi.js` 中 401 分流：`/api/auth/login` 的 401 走正常错误展示，其他接口 401 才清登录态 | ✅ 已修复 |
| 公网部署后 admin/admin123 面临撞库 | 默认口令 | 提供改密 API；域名文档提醒上线必改 | ⚠️ 用户操作 |
| 密码明文输入无法核对 | 无切换控件 | 加小眼睛按钮（本节三） | ✅ 本轮 |
| 弱口令可注册 | 原仅 ≥6 位 | 双层校验升级为用户名英文≥6、密码≥8（本节二） | ✅ 本轮 |
| 公网开放注册有滥用风险 | 无门槛 | 注册码门槛（默认 123456，.env 可改）；「显示名称」字段移除 | ✅ 本轮 |
| 删除 admin 后重启又自动复活 | 种子逻辑"admin 不存在就重建" | 种子改为仅空表创建；admin 删除后由 Gluttonouscat(admin) 独占管理权 | ✅ 已修复 |
| 需要角色权限分级（区间看板 admin 专属/朋友仅研究员） | 初版无角色体系 | 三层门控（前端 disabled/API require_admin/DB 实时角色）+ 用户管理页 | ✅ 本轮 |

## 五、验证记录

- API 级：5 位用户名 → 422；中文用户名 → 400；7 位密码 → 422；合法注册 → 200；新账号登录 → 200（测试账号已清理）
- 注册码：错误码 → 403；空码 → 422；正确码 → 200；重复用户名 → 409（2026-08-30 验证）
- 浏览器级：登录页新主题渲染正常；密码眼睛按钮 DOM 存在；错误提示文案正确显示
