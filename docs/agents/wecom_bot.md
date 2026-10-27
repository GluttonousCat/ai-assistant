# 企业微信智能机器人桥接 (jobs/wecom_bot)

> 2026-10-27 首版。把本地 Agent 问答接入企业微信「智能机器人」做实测体验。

## 1. 系统设计

```
企微用户 @机器人 ──wss──> jobs/wecom_bot.py (本机主动外拨, 无需公网)
    │ aibot_msg_callback (text.content)
    ▼
harness.loop.run_agent_stream(text, session_id)   ← 进程内直调, 与 web 端同一条问答链路
    │ stage/tool_call → 状态刷新; delta → 回答节流刷新
    ▼
aibot_respond_msg (msgtype=stream, 内容刷新式) ──wss──> 企微同一条消息逐帧更新
```

- **连接方向**：本进程向 `wss://openws.work.weixin.qq.com` 外拨并订阅（`aibot_subscribe`,
  bot_id+secret），企微从该连接推消息/事件——**零公网暴露**（无需域名/IP/回调URL/内网穿透）。
- **协议依据**：官方 `@wecom/aibot-node-sdk`（`src/ws.ts` / `types/api.ts`）；心跳 `ping` 每 30s，
  100s 无入帧判死重连（指数退避封顶 30s）。
- **流式语义**：同一 `stream.id` 的每帧 content **整体刷新**消息（非追加）；`finish=true` 收口。
- **会话映射**：`session_id = wecom-<chatid或userid>`，群共享一份记忆、私聊各自独立；历史在
  本进程内存（harness.session, TTL 30min），与 web 端（api 进程）互不相通。
- **护栏**：单帧 content ≤20000 字节（官方 20480）；流式 330s 强制收口（官方上限 6 分钟）；
  同会话串行（后到的提问 3s 内抢不到锁直接回"处理中"）；msgid 去重；单条消息异常不拖垮连接。

## 2. 需求

- 目的：验证企微智能机器人功能本身的体验（好不好用），顺带让本地问答多一个入口。
- 约束：Windows 家用机、无公网——排除 URL 回调模式（需公网域名+加解密+5s 应答+异步推送）。

## 3. 问题（验证中遇到）

| 问题 | 处理 |
|------|------|
| `run_agent_stream` import 写在 `_handle_message` 局部作用域, `_answer` 内 NameError | import 移入 `_answer`（首测即暴露, 已修） |
| Agent 决策轮本身非流式, 企微"流式"观感不足 | 用 stage/tool_call 状态行即时刷新（"⏳ 调用工具 query_financials…"）, delta 到达后节流 1.5s 刷全量 |
| 底层工具可能挂死导致流式超 6 分钟 | `wait_for(it.__anext__(), remain)` 逐事件超时, 330s 到点强制 finish |
| 首跑 `errcode=853000 invalid bot_id or secret` | Secret 生成后配置页未保存生效; 用户重新保存后同凭证订阅成功 |
| `websockets` 库 30s 心跳即被 1002 `incorrect masking` 杀死 | 企微网关应答帧带 MASK 位, 违反 RFC6455（服务器帧禁掩码）; 官方 Node `ws` 库宽容故无感 |
| 去掉 JSON 心跳改用库自带协议 ping → 50s keepalive 超时死 | 企微**不响应协议层 ping**, 只认自家 JSON 心跳（实测探针证实） |
| 以上两条互斥约束 | 弃 `websockets` 切 **aiohttp**（对服务器掩码帧宽容）+ 每 30s JSON ping; 实测 100s/3 心跳周期零断连 |

## 4. 运行方式

```
# .env 加两行 (企微后台 → 智能机器人 → API配置 → 使用长连接)
WECOM_BOT_ID=aib...
WECOM_BOT_SECRET=...

.venv/Scripts/python.exe -m jobs.wecom_bot    # 前台常驻, Ctrl+C 退出; 日志 logs/jobs.log
```

在企微里找到该机器人（单聊或拉群 @它）发文本即问即答；图片/语音等暂回复"仅支持文本"。

## 5. 验证记录

- 2026-10-27 假 WS 端到端实测（真实 Agent 链路）：问题"贵州茅台最新股价多少"→
  意图路由财务子Agent → query_financials → 6 帧回复（初始 ack/状态×3/节流部分/finish 全文），
  答案含当日（2026-10-27）收盘价 1355.90、总市值 1.69 万亿、PE 20.8 —— 全链路通过。
- 2026-10-27 真实凭证上线：订阅成功（首跑 853000 系 Secret 未保存生效），aiohttp 版
  100s 观察窗（3 个心跳周期）零断连，收到企微事件回调推送 —— 通道稳定，转用户实测。
