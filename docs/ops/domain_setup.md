# 域名配置文档（内部运维，地址已脱敏）

> 智能投研助手平台公网访问方案（Cloudflare Tunnel）
> 平台本地服务 → 公网域名 `<your-domain>`
> 文档更新：2026-08-30

## 一、方案概述

家用电脑（无公网 IP）上的服务通过 **Cloudflare Tunnel** 发布到公网：

```
用户浏览器 ──HTTPS──> Cloudflare 边缘节点 ──加密隧道──> 本机 cloudflared 服务
                                                      │
                                                      ▼
                                              127.0.0.1:8208 (uvicorn)
```

**选型理由**（相对其他方案的对比结论）：

| 对比项 | Cloudflare Tunnel | 端口映射/DDNS | 云服务器 | 花生壳/ngrok 免费版 |
|--------|:-:|:-:|:-:|:-:|
| 需要公网 IP | ❌ 不需要 | ✅ 需要（家宽基本没有） | — | ❌ |
| 免费额度 | 完全免费 | 免费 | 每年几百元 | 限流量/限带宽 |
| HTTPS 证书 | 自动 | 需自签/另配 | 需配置 | 部分支持 |
| 数据位置 | 本地 PG（不迁移） | 本地 | 需迁移上云 | 本地 |
| 可用性 | 电脑开机即在 | 电脑开机即在 | 7×24 | 电脑开机即在 |

**代价**：电脑关机则网站下线（本平台运行模式为每天 6:30 开机常驻，影响可接受）。

---

## 二、当前配置信息（实际生效值）

### 域名与路由

| 项 | 值 |
|----|----|
| 主域名 | `<your-domain>`（Cloudflare Registrar 注册，真实地址见本地 .env/hosts） |
| 访问地址 | **<your-domain>** |
| Public Hostname | Subdomain `app` + Domain `<your-domain>` |
| Service | `HTTP` → `localhost:8208` |
| SSL | Cloudflare 自动签发（Edge Certificates，全额 TLS） |

### 本机组件

| 组件 | 配置 | 开机自启 |
|------|------|:-:|
| 后端服务 | uvicorn `app:app`，监听 **127.0.0.1:8208**（仅回环） | ✅ 计划任务 `AIAssistantServer`（登录时触发） |
| 隧道客户端 | cloudflared 2026.8.2，Windows 服务 `cloudflared` | ✅ StartType=Automatic（随系统启动，无需登录） |
| 隧道凭证 | token 存于 `C:\ProgramData\cloudflared\token`（token-file 方式） | — |
| 隧道连接器 | ID `d2a2a474-7f4e-4733-b09c-c11195d2f371`，HA 连接 ×4 | — |

### 安全状态

- 后端只监听 `127.0.0.1`：**局域网内其他设备无法直接访问 8208**，公网流量只能经 Cloudflare 隧道进入
- 平台自带 JWT 登录鉴权（所有 `/api/*` 需 Bearer Token，详见 `api/middleware.py` 白名单）
- HTTPS 由 Cloudflare 边缘终结，浏览器到边缘全程加密

---

## 三、运维手册

### 日常检查（两条命令）

```powershell
# 1. 隧道是否在线（返回 readyConnections>=1 即健康）
curl http://127.0.0.1:20241/ready

# 2. 本地服务是否在跑
curl http://127.0.0.1:8208/health
```

公网验证：浏览器访问 `<your-domain>/health` 返回 `{"status":"ok",...}`。

### 服务管理

```powershell
# 隧道服务
Get-Service cloudflared            # 状态查看
Restart-Service cloudflared        # 重启隧道（改 Public Hostname 后一般不需要）
Start-ScheduledTask AIAssistantServer   # 手动拉起后端（开机登录后自动执行）
```

### 开机时序

1. 系统启动 → Windows 服务 `cloudflared` 自启（无需登录，隧道先就绪）
2. 用户登录 → 计划任务 `AIAssistantServer` 拉起后端（uvicorn）
3. 两者就绪后域名可达（预计开机后 1~2 分钟内）

### 常见问题

| 现象 | 排查 |
|------|------|
| 域名打不开 | ① `curl http://127.0.0.1:8208/health`——不通则后端没起（看计划任务/重启电脑后是否登录）② `curl http://127.0.0.1:20241/ready`——不通则隧道断了（`Restart-Service cloudflared`）③ 都通则查 Cloudflare 控制台 Tunnel 状态与 Public Hostname 配置 |
| 电脑重启后域名慢 | cloudflared 无需登录即自启，但后端要等用户登录计划任务；1~2 分钟后自愈 |
| 想换端口 | 改两处：计划任务 `AIAssistantServer` 参数的 `--port`，以及 Cloudflare 控制台 Service 的 URL |
| 忘记 admin 密码 | 见下文「账号安全」的 reset 方法 |

---

## 三.5 桌面入口（双击启动程序）

桌面快捷方式 **Alpha Finance Radar** → `start_platform.bat`：
- **未运行** → 前台拉起服务，窗口内实时滚动后端日志（含 FastAPI 每个请求的访问日志），
  6 秒后自动打开浏览器；服务退出时窗口暂停提示（不闪退）
- **已在运行**（开机计划任务已自动起）→ 打开浏览器 + 窗口进入**实时日志跟读模式**
  （PowerShell `Get-Content -Wait` 跟读 `logs\server.log`）——照样能看到全部接口调用；
  关闭窗口只停止观看，后台服务不受影响

> 与登录自启计划任务 `AIAssistantServer` 互为补充：开机自动起（计划任务，日志写文件）；
> 桌面图标既是启动器也是**日志查看器**。
> 注意：bat 内容为纯 ASCII（中文会导致 GBK 控制台断句错误）；计时用 `ping`（`timeout` 命令
> 会被 Git Bash 环境劫持失效）。

## 四、账号安全（重要）

域名公开后 `admin/admin123` 这类默认口令会被公网扫描很快撞库。**上线前必做**：

1. 登录后右上角无入口的话，直接调 API 改密：
   ```powershell
   # 登录拿 token 后修改
   curl -X POST <your-domain>/api/auth/change-password `
     -H "Authorization: Bearer <token>" -H "Content-Type: application/json" `
     -d '{"old_password":"admin123","new_password":"新的强密码"}'
   ```
2. 种子管理员账密可在 `.env` 覆盖：`AUTH_ADMIN_USER` / `AUTH_ADMIN_PASSWORD`（仅首次建库生效）

### 进阶（可选）：Cloudflare Access 双因素门禁

在 Zero Trust → Access → Applications 为 `<your-domain>` 添加 Self-hosted 应用，
策略选 Email OTP（你的邮箱）。效果：访问域名前先过 Cloudflare 的邮箱验证码，
登录页完全不对外暴露，等于免费的双因素。

---

## 五、复现步骤（换机器/重装时）

1. **域名**：Cloudflare Registrar 注册（或现有域名 NS 托管到 Cloudflare）
2. **建隧道**：Zero Trust → Networks → Tunnels → Create a Tunnel（Cloudflared）→ 命名
3. **装客户端**（管理员 PowerShell）：
   ```powershell
   winget install --id Cloudflare.cloudflared
   & "C:\Program Files (x86)\cloudflared\cloudflared.exe" service install <控制台给的token>
   ```
   装完即 Windows 服务，随开机自启
4. **配路由**：控制台该隧道 → Public Hostname → Add：
   Subdomain `app` / Domain `<your-domain>` / Type `HTTP` / URL `localhost:8208`
5. **后端加固**：后端只绑回环 `--host 127.0.0.1`（本仓库的 `AIAssistantServer` 计划任务已是此配置）
6. 验证：`https://app.<域名>/health` 返回 ok

---

## 六、相关文件位置

| 文件 | 作用 |
|------|------|
| `C:\ProgramData\cloudflared\token` | 隧道 token（服务读取） |
| `C:\Program Files (x86)\cloudflared\` | cloudflared 安装目录 |
| 计划任务 `AIAssistantServer` | 后端开机自启（`taskschd.msc` 可查看） |
| Windows 服务 `cloudflared` | 隧道开机自启（`services.msc` 可查看） |
| `scripts/install_server_autostart.ps1` | 后端自启任务的安装脚本 |
| `.env` | `AUTH_SECRET`（JWT 签名密钥）、种子管理员账密 |
| `api/middleware.py` | API 鉴权白名单（登录/注册/静态资源免鉴权） |

## 七、已验证记录（2026-08-30）

- ✅ 隧道在线：`/ready` 返回 `readyConnections: 4`
- ✅ 本地服务：`127.0.0.1:8208/health` → 200
- ✅ 公网访问：`<your-domain>/health` → `{"status":"ok"}`（HTTPS 生效）
- ✅ 回环加固生效：局域网 IP 访问 8208 不可达，仅 Tunnel 可进
- ✅ 公网登录：`POST /api/auth/login` 拿到 JWT
- ✅ 公网鉴权：带 token 的 `/api/reports` → 200；无效 token / 无 token → 401
- ✅ 公网 SSE 流式：`POST /api/v1/query/stream` 返回 71 个事件（3 stage + 1 data + 66 delta + 1 done），
  打字机效果经 Tunnel 正常（cloudflared 原生支持 SSE，无需额外配置）

**验证注意事项**：命令行工具直连测试时建议带浏览器 UA（如 `Mozilla/5.0 ...`），
Python `urllib` 默认 UA 偶尔会被 Cloudflare 的 Bot 防护拦下 403；浏览器访问不受影响。
