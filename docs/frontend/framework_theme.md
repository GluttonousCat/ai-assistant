# 前端框架与主题设计文档

> Alpha Finance Radar 平台 · 整体前端架构 / 品牌 / 视觉主题
> 更新：2026-11-20

## 一、系统设计

### 1.1 技术栈与结构

- **React 19 + Vite 8**，无 TypeScript / 无路由库 / 无 UI 组件库 / 无状态管理库——纯手写 JSX + CSS（轻量可控）
- 折线图：lightweight-charts 5（问答结果日线序列）

```
web/src/
├── main.jsx            # 入口 → Root
├── Root.jsx            # 登录态判定: 有 token → Platform, 无 → Login
├── Login.jsx           # 登录/注册页 (../frontend/login.md)
├── Platform.jsx        # 主框架: 侧边导航 + 用户区 + hash 路由(Agent/产业链/研报中心/实时监控/用户管理)
├── chat/AgentChat.jsx       # Agent 问答 (chat.md)
├── Reports.jsx         # 研报中心 (reports.md)
├── ChainView.jsx       # 产业链页
├── market/             # 股票看板/60F图表/局部样式 (stock_dashboard.md)
├── UserAdmin.jsx       # 用户管理 (仅超级管理员)
├── monitor/Monitor.jsx         # 实时监控 (adminOnly)
├── platformApi.js      # 平台 API 客户端 (JWT + SSE)
└── index.css           # 全局主题 (CSS 变量体系)
```

### 1.2 页面路由

hash 路由（`window.location.hash`）：`#chat`（默认）→ 智能对话；`#market` → 股票行情；`#quant` → 量化策略；`#chain` → 行业挖掘；`#reports` → 研报中心；实时监控/用户管理各有对应 hash 入口。刷新保持当前页。

2026-11-12新增 `#market` 股票看板，普通研究员可用。首次nav直接从合法hash初始化，避免挂载effect将看板跳回chat。

### 1.3 请求层约定

- 所有请求自动附带 `Authorization: Bearer <token>`
- 401 分流：登录接口的 401 是「密码错误」正常展示；其他接口 401 清登录态 + `auth:expired` 事件 → Root 踢回登录页
- SSE（`chatStream`）：fetch + ReadableStream 手动解析

### 1.4 导航收起与展开

`Platform.jsx` 统一管理导航状态。展开宽 260px，收起时桌面宽 88px，1200px 及以下宽 78px。
收起时隐藏品牌文字、菜单名称和用户详情，保留导航图标、头像、退出按钮；
图标入口保留中文`aria-label`和悬停提示，当前页带`aria-current`。
收起状态通过浏览器`localStorage`的`alpha:sidebar-collapsed`保存，存储不可用时仍可切换。
首次使用窄屏默认收起；窄屏展开为浮层，点击遮罩、选择页面或在导航内按Escape即可收起。
折叠仅改变布局，不卸载当前页面，对话与流式输出继续保留；图表沿用ResizeObserver适配可用宽度。
平台布局及窄屏导航规则归`index.css`，不再由`chat.css`强制缩窄全局导航。

## 二、品牌命名（本轮需求 2）

| 位置 | 文案 |
|------|------|
| 浏览器标题（index.html） | Alpha Finance Radar |
| 标签页图标、登录页与对话头像 | 共用 `web/src/assets/favicon.svg`，暖棕底配米白色 A；构建时生成带内容哈希的图标 URL |
| 登录页主标题 | Alpha Finance Radar |
| 登录页副标题 | 上市公司财务与研报智能分析平台 |
| 侧边栏品牌名 / 副名 | Alpha Finance Radar / Insight Engine |
| 问答页标题 | Alpha Radar · 智能对话 |
| 侧边栏三个研究入口 | 智能对话 / 股票行情 / 行业挖掘 |
| 行情页标题 | 股票行情（当前提供趋势筛选） |
| 行业页标题 | 行业挖掘（当前以产业链分析为核心） |

命名逻辑：**Alpha**（超额收益）+ **Finance**（财务/金融域）+ **Radar**（雷达，呼应 Radar 扫描/发现意象）；副名 Insight Engine 表「洞察引擎」。改文案只需 grep 这几处字符串。

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

### 3.3 全站页面适配

各页面（Agent 问答/产业链/研报中心/实时监控/用户管理）同用 index.css 变量自动换肤。

## 四、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| 品牌与常驻导航挤占对话空间 | 固定216px侧栏且窄屏折叠样式散在对话CSS | 2026-11-13增加可记忆的64px图标侧栏及始终可见的展开按钮，窄屏支持浮层，平台统一管理布局 | ✅ 已修复 |
| 入口名称与用户使用习惯不一致 | Agent偏技术命名，股票看板/产业链限制模块表达范围 | 2026-11-13统一导航及页面标题为智能对话/股票行情/行业挖掘，保留原hash与领域业务名 | ✅ 已修复 |
| 区间看板「加载失败: 未登录」 | 看板用旧 `api.js` 裸 fetch 不带 token，被全局鉴权中间件 401 | `api.js` 重写为带 JWT + 401 跳登录 | ✅ 已修复 |
| 品牌名「智能投研助手/财务 Agent 平台」过时 | 初版命名 | 全站更名 Alpha Finance Radar（本节二） | ✅ 本轮 |
| 蓝灰暗色主题与新品牌调性不符 | 初版默认配色 | 深黑+暗棕主题（本节三） | ✅ 本轮 |
| 研报中心副标题冗余（功能说明堆砌） | 初版文案 | 移除副标题，仅保留页面标题 | ✅ 本轮 |
| 需要桌面双击启动平台（非打开浏览器） | 初版快捷方式指向 URL | 桌面快捷方式 → `start_platform.bat`：未运行则拉起服务（窗口内实时日志）+6s 后自动开浏览器；已在运行则直接开页面。bat 全 ASCII（中文 GBK 断句坑）；日志同时写 `logs\server.log` | ✅ 本轮 |
| 导航需按角色分级（区间看板/用户管理 admin 专属） | 初版导航无权限概念 | NAV 项加 `adminOnly`：非 admin 置灰 🔒；新增 UserAdmin 页（角色切换）；hash 直达回落 | ✅ 本轮 |
| 用户管理仅超级管理员可见 | 所有 admin 均可管理用户过宽 | NAV 加 `superOnly`（非超管不渲染该项）+ API `_require_super`；区间看板维持 admin 可见 | ✅ 本轮 |

2026-11-17历史问题与处理：静态导入所有业务导致入口包含未使用的图表；改为会话hook独立、
页面lazy/Suspense与FeatureBoundary隔离，绘图与纯数据模块分开。JSON/SSE/下载共用http鉴权，旧API出口兼容。

## 五、构建与部署

2026-11-13：三个研究入口及对应页面名称已统一；前端构建、lint（保留6项既有告警）、分层检查及边界测试通过。
同日完成侧栏收起/展开与窄屏样式收敛；前端构建、lint和分层检查通过，边界测试1项通过，
主服务首页、最新JS/CSS均200，已核对部署资源包含切换按钮、持久化键和收起布局规则。

```bash
cd web && npm run build     # 产物 → web/dist
# 后端 app.py 自动托管 dist: / → index.html, /assets → 静态资源
# 改前端后必须重新 build（uvicorn 不热载 dist）
```

开发模式：`npm run dev`（Vite 代理 /api → 127.0.0.1:9001）。

2026-11-17：入口JS由498.10KB降至200.53KB，图表库在需要时加载；25项前端测试、构建和lint零告警通过。
浏览器覆盖四个主要入口、hash直达、会话恢复及390px无溢出；687项Python测试通过。
