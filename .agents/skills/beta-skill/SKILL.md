---
name: beta-skill
description: 在本投研平台（Alpha Finance Radar）做产业链 Beta 分析：把趋势话术定位到产业链（AI算力/半导体国产化/人形机器人/苹果代工等种子链），将环节映射到 A 股标的（主营构成占比/申万L2/研报三源证据），计算环节指数超额收益与景气度；并支持 LLM 生成新种子链与关键词命中率校验。凡用户提到 产业链、链条、环节、上游、下游、受益标的、板块挖掘，具体环节词（光模块/PCB/覆铜板/HBM/液冷/丝杠/减速器…），或要新增、修改、排查产业链模板时，一律使用本技能——不需要用户说"beta"。
---

# 产业链 Beta 挖掘（beta-skill）

实现载体是自治模块 `beta_alpha/`（模板 `chains/`、量化 `analysis/`、编排 `skills/`、SSE `streaming.py`）；平台对话页意图 `chain` 与「产业链」页面走同一实现。本技能在 ZCode 会话中直接驱动该模块。

所有命令在项目根目录执行，Python 一律 `.venv/Scripts/python.exe`。PG 需可达（分析命令直连数据库，无需服务常驻）。

## 步骤

1. **判断需求类型**（决定路径）：
   - 看环节行情/强弱、不要长文 → 步骤 2（命令 A，无 LLM，秒级）
   - 完整分析含中文综述 → 步骤 3（命令 B，含 LLM 约 1-2 分钟）
   - 新增一条链 → 步骤 4（forge 闭环）
   - 排查某条链为什么漏了某个股/出现误报 → 步骤 5（--check + 探词）
   - 主营构成数据同步/回填 → 读 `references/data-ops.md`，不在本流程内

2. **命令 A：量化速览**
   ```bash
   .venv/Scripts/python.exe -m beta_alpha.analysis.chain_analysis ai_compute
   ```
   参数为 chain_id（`ai_compute` / `semiconductor` / `robot` / `apple_oem_chain`，即 `beta_alpha/chains/` 下文件名）。输出环节数值表 + 每环节头部标的 `名称(档位|主营占比%)`。
   列含义：`strong/medium`=强档（主营占比≥30%）/中档（10~30%或行业+研报双命中）标的数；`excess_120d/20d`=对沪深300 的 120/20 日超额(%)；`or_yoy_median`=成分股营收同比中位数；`report_heat_6m`=近半年研报热度。`NaN`=环节强+中成分不足 2 只（数据未覆盖，非 bug）。

3. **命令 B：完整分析（含 LLM 综述）**
   ```bash
   .venv/Scripts/python.exe -m beta_alpha.skills.beta "AI算力产业链有哪些环节"
   .venv/Scripts/python.exe -m beta_alpha.skills.beta "AI算力链的覆铜板环节有哪些标的"   # 单环节下钻
   ```
   自然语言先规则匹配、失败则 LLM 从模板清单选链。返回"未找到匹配的产业链模板"时改用命令 A 直接传 chain_id，或走步骤 4 建链。
   解读口径：120 日与 20 日超额结合判断上/下游谁先动；20 日反转 + 高研报热度 = 资金切换信号；标的证据串注明主营构成命中的业务名与占比，发现"汽车板簧"混进"汽车板"类误报按步骤 5 收紧关键词。

4. **新增种子链（双通道）**
   ```bash
   .venv/Scripts/python.exe -m beta_alpha.forge "磷化工产业链"        # LLM闭环: 草稿->结构校验->命中率->自动修正
   .venv/Scripts/python.exe -m beta_alpha.forge "存储" --no-save      # 只看草稿
   ```
   网页端同款：平台「产业链」页 →「＋ 新建链」。手写通道：复制 `beta_alpha/chains/_template.yaml` 编辑保存。
   YAML 字段规范、关键词设计纪律（产品口径+应用口径、泛词黑名单）先读 `references/chain-template-guide.md` 再动手；完整示例见 `assets/chain-example.yaml`。

5. **校验与排错**
   ```bash
   .venv/Scripts/python.exe -m beta_alpha.forge --check <chain_id>
   ```
   输出每环节每关键词的命中股票数（绿✓/红✗）与披露样例。判读：英文缩写类（GPU/HBM）主营构成零命中属正常（只命中研报 tags）；**环节整体 0 只才必须改**——用探词工具看该公司真实披露口径后补词：
   ```bash
   .venv/Scripts/python.exe .agents/skills/beta-skill/scripts/show_mainbiz.py 002463.SZ
   ```
   （`--type I/D` 切行业/地区维度。）改完必须重跑步骤 2 验证。

## 成功标准

- 交付给用户的每个数字都来自命令输出，禁止凭记忆报数；数据缺失写明"数据不足"
- 输出一律简体中文；不给投资建议，保持"结构描述 + 证据引用"
- 新增/修改链后：`--check` 通过且无"环节整体零命中"，命令 A 可正常出表
- 环节指标计算无需服务常驻；若遇 Cloudflare 524 类超时，检查素材缓存层（`analysis/chain_analysis.py` 内置 10min 素材缓存 + 300s 结果缓存）
