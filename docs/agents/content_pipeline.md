# 内容资产管线（画像/公众号文章/PPT）设计文档

> Alpha Finance Radar · content/ 模块
> 更新：2026-09-09

## 一、系统设计

### 1.1 定位与链路

把平台的数据资产（Tushare 财务/研报库/量化）变成**可发布的内容资产**：

```
                    ┌─ 对话: build_company_profile 工具直接返回
上市公司画像 (纯数据) ─┼─ 公众号文章: write_article → output/articles/*.md (人工发布)
  (八大板块一次组装)   └─ PPT: 画像 → 任务书(LLM策划) → ../pptgen 出 .pptx
研报主题 ────────────── 综述文章: write_article(topic) 多篇观点对比成文
```

### 1.2 模块结构

```
content/
  profile.py   画像组装 (纯数据零 LLM): 内部全部 REGISTRY.call 复用 MCP 工具层;
               八大板块: 概况/主营/近N年财务/估值分位/预测分歧/兑现校验/风险/研报/形态
               单板块失败降级不拖垮整体 (failed_sections 记录)
  article.py   公众号成文: 股票画像模式 / 研报主题综述模式; 产物 output/articles/*.md
  ppt.py       pptgen 桥: 画像→任务书→ ../pptgen auto 出片 (LLM 配置经环境变量注入,
               deepseek 纯文本 --no-vision); 任务书永远落盘, 出片失败可人工两步走
  prompts.py   提示词 (数据纪律禁编数 / 版权纪律: 观点转述标注机构 / 固定免责声明)
  cli.py       python -m content profile|article|ppt
```

MCP 工具 (mcp/tools/content.py): `build_company_profile`(只读, 全循环可用) +
`write_article`(写类, 沙盒默认排除)。

### 1.3 关键设计

- **画像=数据底座**: 文章和 PPT 都从同一份画像 digest 出发, 数字单源一致
- **digest 控 token**: 画像 JSON 砍成关键数字摘要 (财务年度聚合到年报行, 主营优先产品维度)
- **合规红线内置于 prompt**: 券商观点只转述+标注机构、禁搬运原文; 文末固定免责声明
- **pptgen 解耦**: 独立项目独立 venv, 桥只传任务书和 LLM 环境变量, 互不侵入

## 二、需求优化

- 公众号发布为人工操作 (md 粘贴编辑器); 微信草稿箱 API 属后续可选 (涉及凭据与外发确认)
- 文章两种模式: 股票画像 (深而全) / 主题综述 (多空对比) — 覆盖栏目化运营的两种体裁

## 三、问题（历史问题与处理记录）

| 问题 | 根因 | 处理 |
|------|------|------|
| 文章生成崩溃 `int('32,768')` | config.yaml 的 max_tokens 写了千分位逗号 | client 层容错解析 (去逗号); 同时解释了画像「最近N年」失效之谜——NL2SQL 同因静默回落规则引擎 |
| digest 财务全 None | digest 取中文列名, SQL 返回字段原名 (revenue/n_income_attr_p) | 按字段原名取数 + 按年聚合 (优先年报行) + 元转亿 |
| digest 打印 20 行季报 | 未按年聚合, 5 年窗口季度行全量输出 | 按年分组: 优先 12-31 年报行, 缺则当年最新期 |
| 主营构成混维度重复 | 产品/地区/行业三维度条目混排 | digest 只取产品维度 (biz_type=P) top4 |
| `python -m content` 不可执行 | 缺 __main__.py | 补入口调用 cli.main |

## 四、验证记录

| 日期 | 内容 | 结果 |
|------|------|------|
| 2026-09-09 | 画像 E2E (中际旭创): 8/9 板块 ok (verification 无预测数据正常降级); digest 含 12 年年度成长史/估值分位/风险信号/研报/形态 | ✅ |
| 2026-09-09 | 公众号文章 E2E: 2607 字, 标题《中际旭创：从1.2亿到418亿…》开头即发布水准, 产物 output/articles/20260909_gzh_中际旭创.md | ✅ |
| 2026-09-09 | PPT E2E: 画像→任务书→pptgen 出片成功 (6页 16:9 带动画 23KB); 缺失数据页 agent 自觉标注"未披露"不编造 (数据纪律生效)。期间两次失败均为环境问题 (断网 APIConnectionError / 相对路径落错目录), 代码修复: --out 绝对路径 + --max-steps 300 防御 | ✅ |
| 2026-09-09 | pytest 全量 55 passed (含工具计数 21/只读 18 更新) | ✅ |

## 五、使用

```bash
python -m content profile 中际旭创                     # 画像 (秒级, 纯数据)
python -m content article --stock 中际旭创             # 公众号文章 (画像模式)
python -m content article --topic 光模块               # 研报主题综述
python -m content ppt 中际旭创                         # PPT 全自动
python -m content ppt 中际旭创 --task-book-only        # 只出任务书 (人工两步出片)
```

对话内 (放开写工具后): 「给中际旭创做个画像」「写篇光模块研报综述」。
