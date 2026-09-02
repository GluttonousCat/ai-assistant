# 前端框架与主题设计文档

> Alpha Finance Radar 平台 · 整体前端架构 / 品牌 / 视觉主题
> 更新：2026-08-30

## 一、系统设计

### 1.1 技术栈与结构

- **React 19 + Vite 8**，无 TypeScript / 无路由库 / 无 UI 组件库 / 无状态管理库——纯手写 JSX + CSS（轻量可控）
- K 线图：lightweight-charts 5（区间看板弹层）

```
web/src/
├── main.jsx            # 入口 → Root
├── Root.jsx            # 登录态判定: 有 token → Platform, 无 → Login
├── Login.jsx           # 登录/注册页 (../frontend/login.md)
├── Platform.jsx        # 主框架: 侧边导航 + 用户区 + hash 路由(#chat/#reports/#range)
├── AgentChat.jsx       # Agent 问答 (chat.md)
├── Reports.jsx         # 研报中心 (reports.md)
├── App.jsx             # 区间扫描看板 (embedded 模式嵌入平台)
├── platformApi.js      # 平台 API 客户端 (JWT + SSE)
├── api.js              # 区间看板 API 客户端 (带 JWT)
└── index.css           # 全局主题 (CSS 变量体系)
```

### 1.2 页面路由

hash 路由（`window.location.hash`）：`#chat`（默认）→ Agent 对话；`#reports` → 研报中心；`#range` → 区间看板。刷新保持当前页。

### 1.3 请求层约定

- 所有请求自动附带 `Authorization: Bearer <token>`
- 401 分流：登录接口的 401 是「密码错误」正常展示；其他接口 401 清登录态 + `auth:expired` 事件 → Root 踢回登录页
- SSE（`chatStream`）：fetch + ReadableStream 手动解析

## 二、品牌命名（本轮需求 2）

| 位置 | 文案 |
|------|------|
| 浏览器标题（index.html） | Alpha Finance Radar |
| 登录页主标题 | Alpha Finance Radar |
| 登录页副标题 | 上市公司财务与研报智能分析平台 |
| 侧边栏品牌名 / 副名 | Alpha Finance Radar / Insight Engine |
| 问答页标题 | Alpha Radar · 投研 Agent |

命名逻辑：**Alpha**（超额收益）+ **Finance**（财务/金融域）+ **Radar**（雷达，呼应域名 alpharadar.link 的扫描/发现意象）；副名 Insight Engine 表「洞察引擎」。改文案只需 grep 这几处字符串。

## 三、视觉主题（本轮需求 3）：深黑 + 暗棕

### 3.1 色板（取自参考站 skillsmp.com dark 主题 CSS 变量）

| CSS 变量 | 值 | 用途 |
|----------|-----|------|
| `--bg` | `#0a0a0a` | 页面背景 |
| `--bg-2` | `#111111` | 卡片/表格行背景 |
| `--bg-3` | `#1a1a1a` | 悬浮/次级面板 |
| `--border` | `#2c2420` | 边框（偏棕的深灰） |
| `--text` | `#ededed` | 主文字 |
| `--text-dim` | `#a3a3a3` | 次要文字 |
| `--accent` | `#ad5e48` | **暗棕主色**（按钮/选中态/主强调） |
| `--accent-soft` | `#d99178` | 亮棕（hover/高亮文字/导航选中） |
| `--accent-deep` | `#7f3e2c` | 深棕（背景色块） |
| `--up` / `--down` | `#e05d47` / `#4e9a7a` | A 股红涨/绿跌（调向棕色系） |
| `--gold` | `#d9a066` | 金色（分数/上沿） |

### 3.2 落地范围

- `:root` 变量整体替换（原蓝灰暗色系 → 暗棕黑）
- 主按钮渐变：`#ad5e48 → #c46f56`；登录页品牌字/顶栏 logo 渐变：`#d99178 → #ad5e48`
- 侧边栏：`#141010 → #0a0a0a` 渐变底，导航选中态棕底亮棕字，头像 `#ad5e48 → #7f3e2c` 渐变
- 登录页背景光晕：棕色 radial-gradient
- 全部徽章（状态/市场/涨跌）从蓝/紫系改棕色系
- 全站通过 `var(--accent)` 等变量引用，后续换色只改 `:root` 一处

### 3.3 区间看板适配

看板（App.jsx/Setup/Tracking）同用 index.css 变量自动换肤；`App.jsx` 支持 `embedded` 属性（嵌入平台时顶栏改为非 sticky）。

## 四、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| 区间看板「加载失败: 未登录」 | 看板用旧 `api.js` 裸 fetch 不带 token，被全局鉴权中间件 401 | `api.js` 重写为带 JWT + 401 跳登录 | ✅ 已修复 |
| 品牌名「智能投研助手/财务 Agent 平台」过时 | 初版命名 | 全站更名 Alpha Finance Radar（本节二） | ✅ 本轮 |
| 蓝灰暗色主题与新品牌调性不符 | 初版默认配色 | 深黑+暗棕主题（本节三） | ✅ 本轮 |
| 研报中心副标题冗余（功能说明堆砌） | 初版文案 | 移除副标题，仅保留页面标题 | ✅ 本轮 |
| 需要桌面双击启动平台（非打开浏览器） | 初版快捷方式指向 URL | 桌面快捷方式 → `start_platform.bat`：未运行则拉起服务（窗口内实时日志）+6s 后自动开浏览器；已在运行则直接开页面。bat 全 ASCII（中文 GBK 断句坑）；日志同时写 `logs\server.log` | ✅ 本轮 |
| 导航需按角色分级（区间看板/用户管理 admin 专属） | 初版导航无权限概念 | NAV 项加 `adminOnly`：非 admin 置灰 🔒；新增 UserAdmin 页（角色切换）；hash 直达回落 | ✅ 本轮 |
| 用户管理仅 Gluttonouscat 可见 | 所有 admin 均可管理用户过宽 | NAV 加 `superOnly`（非超管不渲染该项）+ API `_require_super`；区间看板维持 admin 可见 | ✅ 本轮 |

## 五、构建与部署

```bash
cd web && npm run build     # 产物 → web/dist
# 后端 app.py 自动托管 dist: / → index.html, /assets → 静态资源
# 改前端后必须重新 build（uvicorn 不热载 dist）
```

开发模式：`npm run dev`（Vite 代理 /api → 127.0.0.1:8208）。
