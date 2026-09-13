# 投研解读 Agent（Interpret Agent）设计文档

> Alpha Finance Radar · content/ + tools/cninfo + .agents/skills/interpret-skill
> 更新：2026-09-10

## 一、系统设计

### 1.1 定位

平台数据资产的**内容出口 Agent**：输入「股票 + 体裁」→ 编排多源数据（量化画像 /
年报原文 / 研报观点 / 十大股东）→ 确定性配图 → LLM 成文 → 发布级解读产物
（公众号 md / PPT）。是内容管线（content_pipeline.md）的 Agent 化定稿，
新增**年报原文维度**——把巨潮爬来的年报 PDF 变成解读语料。

### 1.2 编排流（管线即 Agent）

```
输入 (股票/主题)
  │
  ├─ ① 量化画像 (build_company_profile, 十板块, 纯数据零 LLM)
  ├─ ② 年报语料 (NEW): fin.cninfo_announcement 定位 PDF
  │     → fitz 提全文 → 章节切片(经营讨论/风险) → extract LLM 提炼要点(中文)
  ├─ ③ 配图五张 (charts.py, 数据确定性渲染, 图不会错)
  │
  ├─ ④ 成文 (agent LLM): 六模块 prompt + {digest} + {annual_report} 素材包
  │     模块一(公司速览)/模块五(风险) 吸收年报语料, 其余模块不变
  │
  └─ ⑤ 产物: output/articles/*.md; 可选联动 PPT (pptgen 桥)
        单源失败降级: 无年报 PDF → 素材包标注"无年报语料", 文章照出
```

**为什么是编排型而非自由工具循环**：内容生产要求稳定可复现（同一股票重跑结构
一致、数字单源），自由循环引入不确定性且难以断言评测。对话 Agent（core/agent/loop.py）
负责"问"，解读 Agent 负责"产"——两者通过 MCP 工具衔接。

### 1.3 三入口

| 入口 | 形态 | 说明 |
|------|------|------|
| CLI | `python -m skills.content interpret 中芯国际 [--annual-year 2025] [--ppt]` | 主入口, 断点友好 |
| ZCode 会话 | `.agents/skills/interpret-skill` (SKILL.md 驱动) | 对齐 beta/alpha-skill 模式, 会话内直接说"给XX做解读" |
| 对话 Agent | MCP `write_article` (写类, 沙盒默认排除, 需显式放开) | 聊天页触发, 复用既有工具 |

### 1.4 年报语料提取设计（核心增量）

```
fin.cninfo_announcement (category=ndbg, download_status=done, 文件在盘)
  → 定位: max(report_year) 的全文行, 无则降级 None
  → fitz 全文 (PyMuPDF; 复用 tools/finance/report_extractor 的多级回退思路)
  → 章节切片 (宽松定位, LLM 容错高):
       经营讨论: "管理层讨论与分析" 第2次出现(跳过目录) → 下一"第X节"标记, 截 9000 字
       风险: "可能面对的风险" / "风险因素" 命中 → 后 6000 字
  → extract LLM 提炼 (≤600字, 一律中文): 业务要点/经营亮点与隐忧/风险清单
  → 注入素材包 {annual_report}; prompt 模块一/五增加吸收指令
```

- 版权边界：年报是公司公开披露文件，摘录转述无版权问题（区别于券商研报），
  但 prompt 仍要求"提炼转述，不整段搬运"
- 图片型 PDF（无文本层）：fitz 提取为空 → 降级跳过（OCR 走 tools/pdf 的 ocr_pdf 属后续可选）

## 二、需求优化

- 体裁矩阵：股票解读（本 Agent 主线）/ 主题综述（write_topic_article 已有）/
  PPT（联动可选）——同一数据底座三种出口
- 年报年度选择：默认最新已披露年报（cninfo 表内可用的最大 report_year），
  `--annual-year` 显式指定
- 解读与量化分离（用户决策）：文章不含 range_trading 量化信号，量化另走看板

## 三、问题（历史问题与处理记录）

| 问题 | 根因 | 处理 |
|------|------|------|
| （设计期预判）年报 PDF 未下载 | 批量任务按 ts_code 序尚未跑到该股 | interpret 前置单股补拉 `tools.cninfo.downloader --stock <code>`；素材包降级不阻塞成文 |
| （设计期预判）fitz 提取为空 | 图片型 PDF 无文本层 | 空文本直接降级 None，文章照出（不自动 OCR，控制时长） |

## 四、验证记录

| 日期 | 内容 | 结果 |
|------|------|------|
| 2026-09-10 | 中芯国际(688981.SH) E2E 解读（设计验证用例，见 content_pipeline.md 联动记录） | 见提交记录 |

## 五、使用

```bash
python -m skills.content interpret 中芯国际                    # 最新年报语料 + 六模块 + 五图
python -m skills.content interpret 中芯国际 --annual-year 2024 # 指定年报年度
python -m skills.content interpret 中芯国际 --ppt              # 解读完联动出 PPT
python -m skills.content interpret 中芯国际 --no-annual        # 纯画像模式(=article)
```

ZCode 会话：直接说「给中芯国际做一份解读」触发 interpret-skill。
