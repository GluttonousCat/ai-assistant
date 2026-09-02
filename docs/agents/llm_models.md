# LLM 模型体系设计

> Alpha Finance Radar 平台 · 模型路由与用途体系
> 更新：2026-08-30

## 一、系统设计

### 1.1 模型约定（现行）

| 类别 | 模型 | 说明 |
|------|------|------|
| **文本模型（统一）** | `deepseek-v4-flash-0731` | 思考型模型，**必须以 `enable_thinking=False` 调用**（否则思考过程吃光 token、content 返回空——已踩坑并修复） |
| **多模态模型（统一）** | `qwen3.8-flash` | 视觉输入（图片型 PDF OCR / 图片理解）。deepseek-v4-flash 在 Dashscope 网关**不接收图片输入**（实测返回"未收到图片"），视觉用途必须走 qwen |
| embedding（预留） | `text-embedding-v3` | 向量化（未来研报 RAG） |

网关：`https://dashscope.aliyuncs.com/compatible-mode/v1`（阿里云百炼，OpenAI 兼容协议，key 在 `.env OPENAI_API_KEY`）。

### 1.2 用途路由（purpose 体系）

模型选择按「用途」解耦，代码不感知具体模型名，配置见 `config.yaml llm.models`：

```yaml
llm:
  models:
    default:   {model: deepseek-v4-flash-0731}   # 兜底
    agent:     {model: deepseek-v4-flash-0731}   # Skill/Agent 推理
    extract:   {model: deepseek-v4-flash-0731}   # 结构化提取
    analysis:  {model: deepseek-v4-flash-0731}   # 轻量元数据
    vision:    {model: qwen3.8-flash}            # 多模态
    embedding: {model: text-embedding-v3}
```

| 用途 | 场景 | 工厂函数 |
|------|------|---------|
| `default` | 兜底，未指定用途的一切 | `get_llm()` |
| `agent` | 意图识别 L2、SQL 生成/解读、研报综合解读、扫描件解读 | `get_agent_llm()` |
| `extract` | 研报长文本结构化 JSON 提取 | `get_extract_llm()` |
| `analysis` | 文件名→机构/标的/行业/地区（LLM Analysis） | 直接读 `llm.models.analysis.model` |
| `vision` | 图片型 PDF 逐页 OCR、图片理解 | `get_vision_llm()` |

**换模型只改 config.yaml 一处**。每个用途还可单独覆盖 `api_key`/`base_url`（不同厂商混布，如 agent 用 Claude、extract 用 deepseek）：
- 配置项 `llm.models.<用途>.api_key / base_url`
- 或环境变量 `LLM_<用途>_API_KEY / LLM_<用途>_BASE_URL`

### 1.3 实现链路

```
调用方 (skill / api / tool)
   │ get_agent_llm() / get_vision_llm() ...
   ▼
llm/client.py LLMClient(purpose=...)
   │ core/config.py llm_model_for(purpose)   ← 用途→模型名
   │ core/config.py llm_model_override(purpose) ← 用途→key/base_url 覆盖
   ▼
OpenAI SDK (dashscope compatible-mode)
```

## 二、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| deepseek-v4-flash 视觉调用返回"我没有收到图片" | 该模型在 dashscope 网关不接收图片输入（三种消息格式实测均失败） | 视觉用途独立路由到 qwen 多模态（purpose 体系由此建立） | ✅ 已修复 |
| deepseek 提取整批"解析失败"、content 为空 | 思考模型默认开思考，`max_tokens=200` 被思考过程耗尽，`finish_reason=length` | 全部 deepseek 调用加 `extra_body={"enable_thinking": False}`；不设小 max_tokens | ✅ 已修复 |
| qwen-flash 小模型元数据提取质量差（标的硬凑） | 模型能力 + prompt 规则弱 | 换回 deepseek-v4-flash + 规范化 prompt（词表约束），见 frontend/reports.md | ✅ 已修复 |
| 模型名散落各处硬编码 | 初版直接写死 | purpose 路由体系（本档 1.2），代码零模型名 | ✅ 本轮 |

## 三、调用规范（新代码必读）

1. **不要在业务代码里写模型名**——用 `get_*_llm()` 工厂，用途路由自动解析
2. **deepseek 调用一律 `enable_thinking=False`**（工厂已内置则无需重复）
3. 新增用途：config.yaml 加 `llm.models.<新用途>` + `llm/client.py` 加工厂函数，两处各一行
4. 视觉调用成本敏感（每页一次）：单册页数上限护栏（默认 10 页）
