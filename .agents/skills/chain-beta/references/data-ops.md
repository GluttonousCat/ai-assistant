# 数据维护手册（chain-beta 依赖的数据）

## 主营构成 fina_mainbz（映射层的硬证据来源）

- 接口：Tushare `fina_mainbz`，一次调用返回单只股票**全部历史 × 全部维度**（`bz_code`=P产品/D地区/I行业，另有 455006000 等编码=销售模式等补充维度）
- 表：`fin.fina_mainbz`（原始）；`fin.v_main_biz`（清洗视图：剔维度表头行"产品/行业/地区"、合计行，`is_sub_item` 标记"其中"/冒号分层子项，派生 `sales_share_pct` 收入占比与 `gross_margin_pct` 毛利率）
- 同步脚本：`tools/market/sync_mainbz.py`，断点续传（水位线 `fin.sync_meta` 表 `fina_mainbz` 行），限流自动等待重试

```bash
# 全量回填（断点续传, 中断后重跑自动继续）
.venv/Scripts/python.exe -m tools.market.sync_mainbz
# 增量（只重拉最近14天公告财报的股票; 幂等）
.venv/Scripts/python.exe -m tools.market.sync_mainbz --mode incremental
# 定向补拉若干只（测试/急救）
.venv/Scripts/python.exe -c "from core.config import get_config; import tushare as ts, time
from tools.market.sync_mainbz import sync_one_stock
pro = ts.pro_api(get_config().tushare_token)
for c in ['300308.SZ', '600183.SH']: print(c, sync_one_stock(pro, c)); time.sleep(0.4)"
```

监控窗口：`start_mainbz_sync.bat`（新开 CMD 跑回填，`logs/sync_mainbz.log` 同步落盘，可 tail）。进度查询：

```bash
.venv/Scripts/python.exe -c "from storage.pg import PgClient
with PgClient() as pg:
    print(dict(pg.fetch_one(\"SELECT last_code, total_rows FROM fin.sync_meta WHERE table_name='fina_mainbz'\")))"
```

## 研报链抽取 fin.chain_extract（环节热度的研报来源）

- 深度提取完成后自动后台抽取（挂 `skills/report/skill.py`，常驻服务进程内生效），每篇报告每环节一行，幂等（先删后插）
- 存量回填：`.venv/Scripts/python.exe -m scripts.backfill_chain_extract --days 90 --limit 100`（每篇一次 LLM 调用约 30-90s）
- 重抽单篇：`DELETE FROM fin.chain_extract WHERE report_id=X` 后重跑回填脚本
- 注意：`extract_chain` 的后台线程方式（`spawn_chain_extract`）只适用于常驻进程；一次性脚本里直接调 `extract_chain(report_id, prompt)` 同步执行

## 行情/行业依赖

- 环节指数：`stock.daily`（价格）× `stock.adj_factor`（复权，独立表；`daily` 内嵌的 adj_factor/close_adj 预计算列历史上为空，代码已按 join 处理，勿"优化"回去）
- 基准：`stock.index_daily` 的 `000300.SH`（沪深300）
- 申万 L2 成分：`stock.stock_industry`（当前成员 = `out_date IS NULL AND is_new='Y'`）+ `stock.index_classify`
- 景气度：`fin.fina_indicator`（`report_type='1'` 合并报表）

## 故障速查

| 症状 | 排查 |
|---|---|
| `relation fin.chain_extract does not exist` | 新环境首次跑：执行任一 `init_fin_schema` 路径或 backfill 脚本（内含建表） |
| Decimal 除零崩溃（pandas object dtype） | PG NUMERIC 列取出后必须 `astype(float)` 再算 |
| Tushare 报"每分钟最多访问" | 脚本已内置 60s 等待重试；确认没有并行多开同步进程 |
| 积分/权限不足 | fina_mainbz 需 500 积分；报错会直接终止并列明确原因 |
