# 盘后趋势图文邮件

> 更新：2026-11-16。入口 `jobs/send_market_digest.py`；领域 `skills/trend/digest.py`；绘图 `tools/market/chart_image.py`；传输 `tools/notifications/email.py`。

## 一、系统设计

每天上海时间18:00，Codex自动化调用项目虚拟环境运行：

```powershell
.venv/Scripts/python.exe -m jobs.send_market_digest --send --wait
```

与后端17点行情任务分离：17点由项目服务采集并发布快照，18点邮件任务只读取本地库和已发布结果，
不重新选参、不调用外部行情、不扫描新规则。运行机器、Codex与17点项目服务需保持可用；
不在后端额外挂第二个18点发送器，避免重复调度。每日含休市日，休市日报明确注明最近完整交易日。

普通调用不发信：`.venv/Scripts/python.exe -m jobs.send_market_digest`生成HTML/EML/PNG本地预览。
实际发送必须显式带`--send`，18点前返回not_due。没有SMTP配置返回blocked，不能将预览成功当作发送成功。

邮件使用与看板一致的候选范围、评分、已发布参数和真实因子前复权；按**评分≥60分**筛选，过滤ST及退市名称，
成熟/被排除走势不会因高分再次成为候选。入场候选、观察候选分别分组，不提升观察记录的eligible字段。
分类优先context_pattern，其次原pattern/family，与前端一致；蓝↑趋势回调、紫■底部反转，未知不默认归为反转。

## 二、需求优化

- 深色卡片、总数与数据截止时间、评分汇总表、母趋势、风险距离、参考止损、筛选解释与检查结果。
- 每股一张内嵌PNG：原生日K及60F、M8/13/21、成交量、日K入场标记/60F观察标记；静态高清图无需外网图片服务器。
- 使用CID关联HTML与图片，正文为兼容邮件客户端的table与内联样式，并提供纯文本备选和UTF-8 BOM CSV。
- 本地HTML将图片转为data URI方便离线查看，发送邮件使用CID；邮件不依赖JavaScript或SVG。
- 默认每部分最多20只，并按实际MIME字节大小控制在8MiB以内，必要时拆分，不静默丢掉高分标的。
- 数据未齐时每10分钟等待/重试，最多到当日20点；届时仍缺数据只发延迟说明，不夹带旧股票名单。

## 三、问题

| 问题 | 根因 | 处理 |
|---|---|---|
| 仅有收件邮箱无法自动投递 | 缺少认证发件通道 | 独立系统邮箱SMTP，客户端授权码仅存本地.env；收件邮箱不需授权 |
| 18点误把昨日结果当今日 | 只看存在快照或全局最大日期 | 检查应有交易日、两个周期截止、完整行情任务及其snapshot run_id一致 |
| 高分观察被当作买入 | 评分门槛没有区分信号状态 | 与看板候选范围一致，入场/观察分组，原eligible不修改 |
| SMTP断线后重复投递 | DATA可能已被服务器接受但回执丢失 | 持久化sending/sent/uncertain；不确定停止自动重发，提示核对收件箱 |
| 多部分邮件重试重复前半段 | 没有逐部分记录回执 | 锁+逐部分保存Message-ID与回执，只重试明确未接受的部分 |
| 进程在发送后崩溃 | 本地sending记录尚未转sent | 下次认定uncertain，保守停止，不宣称严格的exactly-once投递 |
| 损坏发送记录导致重发 | 非法JSON被当作首次运行的空状态 | 发送记录严格读取，损坏立即停止；缓存收件人与Message-ID必须匹配记录 |
| 图中价格标签与刻度/下一周期标题碰撞 | 单图右边距及子图间距不足 | 调整右侧价格牌与子图间距，真实PNG目视复核 |

## 四、处理方案

发件人推荐专用QQ/163邮箱，免自建邮件服务器。已支持TLS 465及STARTTLS；禁止明文、不校验证书等模式。
163需在邮箱内开启SMTP并获取客户端授权码，不使用邮箱登录密码。
参考[SMTP与授权码配置说明](https://support.huaweicloud.com/usermanual-roma/roma_03_5051.html)。

以下只作占位示例，实际收发地址和授权码写入不纳入Git的`.env`：

```dotenv
MARKET_DIGEST_TO=reader@example.com
MARKET_DIGEST_MIN_SCORE=60
MARKET_DIGEST_OBSERVATIONS=true
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=sender@163.com
SMTP_FROM=sender@163.com
SMTP_PASSWORD=CLIENT_AUTHORIZATION_CODE
```

同一SMTP适配器也可配置[Resend SMTP](https://resend.com/changelog/smtp-service)：user=resend、password=API Key、from=已验证域名地址。
正式发送要求[自有域名验证](https://resend.com/docs/dashboard/domains/introduction)；
[免费额度](https://resend.com/pricing)为每月3000封、每日100封（2026-11-16核实）。目前没有创建第三方邮件服务账号或订购服务。

静态绘图依赖matplotlib与numpy，当前项目虚拟环境已具备，无额外安装步骤。图表是高清静态PNG，邮件内不提供前端的交互缩放。

输出均在`output/market/email_digest/`：预览按snapshot run_id归档，正式发送按本地发送日和收件配置指纹归档。
正式信件在发送前全部生成，EML包含固定Message-ID；重试复用已生成文件，不能混合不同快照。
锁与JSON原子写复用`core/local_state.py`，行情更新工具同样复用此基础设施。
发送成功仅表示SMTP服务器接受，不能证明邮件最终进收件箱；首次实际投递后需由收件人检查收件箱/垃圾箱。

认证失败、无效收件人或明确5xx拒绝停止本轮，网络与4xx明确未接受可重试；回执不确定不自动重发。
日志/自动化摘要只返回脱敏状态和异常类型，不输出授权码或SMTP服务器原始回包。
缺授权码时可查看预览，但不得声称每日实际投递已经就绪。

## 五、验证记录

2026-11-16：以13日15:00已发布快照生成11只≥60分候选，1只入场、10只观察；保持前端分类与规则。
每只两周期K线均来自真实原生数据、截止相同、前复权无问题，整封约2.3MB。
实际PNG检查后修正价格牌/子图间距；浏览器预览尝试超时，未声称通过Outlook真实客户端排版验收。
SMTP TLS连接与EHLO通过（TLS1.3）；此时授权码尚未填写，未尝试认证、未发送任何邮件。
已登记每天18点的Codex自动化；缺发件授权时会明确blocked。

171项相关Python回归通过（含邮件、看板、调度与分层）。邮件专项覆盖：60分边界、ST/过期结构排除、父趋势分类优先、入场/观察分组、HTML转义、
CID与CSV编码、UTC→上海18点边界、旧快照等待、逐部分续发、回执丢失、崩溃/损坏记录保护、缓存收件人校验、永久拒绝停止、预览绝不调用SMTP；
行情锁/调度与分层回归通过，历史跨层依赖维持50项，无新增违规。
