"""
Tushare 财务数据 -> PostgreSQL 同步模块

功能:
- 全量回填: income(利润表) / balancesheet(资产负债表) / cashflow(现金流量表) / fina_indicator(财务指标)
- 数据源: 单只股票拉取 (income 等接口 2000积分可用, income_vip 需5000积分)
- report_type=1 (合并报表, 最新)
- 断点续传: 按 ts_code 逐只, sync_meta 记录进度, 可反复执行

用法(命令行):
    python -m tools.market.sync_financial                   # 全量4表
    python -m tools.market.sync_financial --tables income   # 仅利润表
    python -m tools.market.sync_financial --limit 100       # 前100只测试
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import init_fin_schema

logger = get_logger(__name__)

API_SLEEP = 0.35  # 200次/分钟 -> 0.35s 间隔留余量

# 各表配置: 表名 -> (Tushare接口名, 拉取字段)
FIN_TABLE_FIELDS = {
    "income": (
        "income",
        ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
         "comp_type", "end_type", "basic_eps", "diluted_eps", "total_revenue",
         "revenue", "int_income", "prem_earned", "comm_income",
         "n_commis_income", "n_oth_income", "n_oth_b_income", "prem_income",
         "out_prem", "une_prem_reser", "reins_income", "n_sec_tb_income",
         "n_sec_uw_income", "n_asset_mg_income", "oth_b_income",
         "fv_value_chg_gain", "invest_income", "ass_invest_income",
         "forex_gain", "total_cogs", "oper_cost", "int_exp", "comm_exp",
         "biz_tax_surchg", "sell_exp", "admin_exp", "fin_exp",
         "assets_impair_loss", "prem_refund", "compens_payout",
         "reser_insur_liab", "div_payt", "reins_exp", "oper_exp",
         "compens_payout_refu", "insur_reser_refu", "reins_cost_refund",
         "other_bus_cost", "operate_profit", "non_oper_income",
         "non_oper_exp", "nca_disploss", "total_profit", "income_tax",
         "n_income", "n_income_attr_p", "minority_gain", "oth_compr_income",
         "t_compr_income", "compr_inc_attr_p", "compr_inc_attr_m_s",
         "ebit", "ebitda", "insurance_exp", "undist_profit",
         "distable_profit", "rd_exp", "fin_exp_int_exp", "fin_exp_int_inc",
         "transfer_surplus_rese", "transfer_housing_imprest",
         "transfer_oth", "adj_lossgain", "withdra_legal_surplus",
         "withdra_legal_pubfund", "withdra_biz_devfund", "withdra_rese_fund",
         "withdra_oth_ersu", "workers_welfare", "distr_profit_shrhder",
         "prfshare_payable_dvd", "comshare_payable_dvd",
         "capit_comstock_div", "net_after_nr_lp_correct",
         "credit_impa_loss", "net_expo_hedging_benefits",
         "oth_impair_loss_assets", "total_opcost", "amodcost_fin_assets",
         "oth_income", "asset_disp_income", "continued_net_profit",
         "end_net_profit", "update_flag"],
    ),
    "balancesheet": (
        "balancesheet",
        ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
         "comp_type", "end_type", "total_share", "cap_rese",
         "undistr_porfit", "surplus_rese", "special_rese", "money_cap",
         "trad_asset", "notes_receiv", "accounts_receiv", "oth_receiv",
         "prepayment", "invent", "oth_cur_assets", "fix_assets", "cip",
         "intang_assets", "r_and_d", "goodwill", "lt_equity_invest",
         "oth_eq_invest", "oth_lt_invest", "lt_rec", "dep_inv",
         "oth_ncur_assets", "total_assets", "st_borr", "notes_payable",
         "acct_payable", "adv_receipts", "employee_payable", "taxes_payable",
         "oth_cur_liab", "bond_payable", "lt_payable", "lt_borr",
         "total_liab", "equity_attr_p", "minority_int",
         "total_hldr_eqy_exc_min_int", "total_hldr_eqy_inc_min_int",
         "total_liab_hldr_eqy", "update_flag"],
    ),
    "cashflow": (
        "cashflow",
        ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
         "comp_type", "end_type", "netprofit_yoy", "dt_netprofit_yoy",
         "or_yoy", "dt_or_yoy", "netprofit_yoy_extra", "n_cashflow_act",
         "n_cashflow_inv_act", "n_cash_flows_fnc_act", "c_fr_sale_sg",
         "recp_tax_rends", "n_cash_inr_dep_fi", "n_incr_loans_cb",
         "n_incr_borr_oth_fi", "n_incr_disp_fiolig", "c_paid_gs_for_rtr",
         "n_incr_disp_fiolis", "c_paid_for_intang_invest",
         "n_incr_inv_full_subs", "c_paid_act_inv", "n_cash_trans_inv",
         "c_rec_repay_borr", "c_paid_dist_dpcp_int_exp", "update_flag"],
    ),
    "fina_indicator": (
        "fina_indicator",
        ["ts_code", "ann_date", "end_date", "report_type", "comp_type",
         "roe", "roe_waa", "roe_dt", "roa", "npta", "roe_waa_dt", "netprofit_yoy",
         "dt_netprofit_yoy", "or_yoy", "dt_or_yoy", "q_sales_yoy",
         "q_profit_yoy", "grossprofit_margin", "netprofit_margin",
         "profit_to_gr", "or_yoy_waa", "grossprofit_margin_waa",
         "netprofit_margin_waa", "ocfps", "eps", "bps", "assets_turn",
         "inv_turn", "ca_turn", "debt_to_assets", "current_ratio",
         "quick_ratio", "update_flag"],
    ),
}


def get_whitelist_codes() -> List[str]:
    """从 PG stock_basic 读取白名单 ts_code 列表"""
    from tools.market.sync_tushare import get_whitelist
    codes = get_whitelist(use_cache=True, refresh_remote=False)
    return sorted(codes)


def get_fin_progress(table: str) -> Optional[str]:
    """已同步到的 ts_code (水位线, 续传用)"""
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT last_code FROM fin.sync_meta WHERE table_name=%s",
            (table,),
        )
    return row["last_code"] if row else None


def set_fin_progress(table: str, last_code: str, total: int) -> None:
    with PgClient() as pg:
        pg.execute(
            """INSERT INTO fin.sync_meta (table_name, last_code, total_rows, updated_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (table_name) DO UPDATE
               SET last_code = EXCLUDED.last_code,
                   total_rows = EXCLUDED.total_rows,
                   updated_at = now()""",
            (table, last_code, total),
        )


def _normalize_date_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """日期列 (YYYYMMDD int/str) 转 date 对象"""
    if col in df.columns:
        df[col] = df[col].map(
            lambda v: _to_date(v) if pd.notna(v) and v != "" else None
        )
    return df


def _to_date(v) -> Optional[date]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s == "nan":
        return None
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d").date()
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def fetch_stock_fin(pro, api_name: str, ts_code: str, fields: List[str],
                    start: str = "19900101") -> pd.DataFrame:
    """拉取单只股票单个接口的历史财报数据"""
    try:
        df = getattr(pro, api_name)(
            ts_code=ts_code,
            start_date=start,
            fields=",".join(fields),
        )
        return df
    except Exception as e:
        logger.warning(f"  {api_name} {ts_code} 抓取失败: {e}")
        time.sleep(2)
        return pd.DataFrame()


def backfill_fin_table(pro, table: str, codes: List[str],
                       limit: Optional[int] = None) -> int:
    """回填单张财务表 (逐股票)"""
    api_name, fields = FIN_TABLE_FIELDS[table]
    logger.info(f"回填 {table}: {len(codes)} 只股票, limit={limit}")

    # 断点: 从上次 ts_code 之后继续
    last_code = get_fin_progress(table)
    if last_code and last_code in codes:
        idx = codes.index(last_code)
        codes = codes[idx + 1:]
        logger.info(f"断点续传: 从 {last_code} 之后继续, 剩余 {len(codes)} 只")
    elif last_code:
        logger.info(f"断点续传: 上次进度 {last_code} 已不在白名单, 全量重跑")

    if limit:
        codes = codes[:limit]

    total = 0
    for i, ts_code in enumerate(codes, 1):
        df = fetch_stock_fin(pro, api_name, ts_code, fields)
        if df is None or df.empty:
            continue

        # 归一化日期列
        for c in ("ann_date", "f_ann_date", "end_date"):
            _normalize_date_col(df, c)

        # 只保留表内存在的列 (schema 定义之外的忽略)
        with PgClient() as pg:
            table_cols = {c["name"] for c in pg.get_columns("fin", table)}
        keep = [c for c in df.columns if c in table_cols]
        df = df[keep]

        with PgClient() as pg:
            pg.upsert_df(
                df, "fin", table,
                conflict_keys=["ts_code", "end_date", "report_type"],
            )
            total += len(df)

        # 每只股票更新进度 (崩溃可从该股票续)
        set_fin_progress(table, ts_code, total)

        if i % 50 == 0:
            logger.info(f"  {table}: 已处理 {i}/{len(codes)} 只, 累计 {total} 行")

        time.sleep(API_SLEEP)

    logger.info(f"回填完成 {table}: 累计 {total} 行")
    return total


def run_financial(start_ts_code: Optional[str] = None,
                  tables: Optional[Iterable[str]] = None,
                  limit: Optional[int] = None) -> Dict[str, int]:
    """执行财务数据全量回填"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    # 初始化 fin schema
    with PgClient() as pg:
        init_fin_schema(pg)

    codes = get_whitelist_codes()
    if not codes:
        raise RuntimeError("白名单为空, 请先运行行情同步")

    if start_ts_code:
        try:
            start_idx = codes.index(start_ts_code)
            codes = codes[start_idx:]
        except ValueError:
            logger.warning(f"起始代码 {start_ts_code} 不在白名单, 全量开始")

    tables = set(tables or list(FIN_TABLE_FIELDS.keys()))
    result: Dict[str, int] = {}
    for t in tables:
        result[t] = backfill_fin_table(pro, t, codes, limit=limit)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tushare 财务数据回填 PostgreSQL")
    parser.add_argument("--tables", type=str,
                        default="income,balancesheet,cashflow,fina_indicator",
                        help="要同步的表 (income/balancesheet/cashflow/fina_indicator)")
    parser.add_argument("--start", type=str, default=None,
                        help="起始 ts_code (断点续传)")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理股票数 (测试用)")
    args = parser.parse_args()

    tbls = [t.strip() for t in args.tables.split(",") if t.strip()]
    print(f"开始财务回填: tables={tbls}, limit={args.limit}")
    result = run_financial(start_ts_code=args.start, tables=tbls, limit=args.limit)
    print(f"财务回填完成: {result}")