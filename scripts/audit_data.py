"""数据仓库全面审核脚本 — 一次性跑出所有关键指标"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.pg import PgClient

def run():
    with PgClient() as pg:
        # ========== 1. 所有表行数 ==========
        print("=" * 60)
        print("1. 所有表行数总览")
        print("=" * 60)
        rows = pg.fetch_all("""
            SELECT schemaname, relname, reltuples::bigint AS cnt
            FROM pg_stat_user_tables
            WHERE schemaname IN ('stock','fin')
            ORDER BY schemaname, relname
        """)
        for r in rows:
            print(f"  {r['schemaname']}.{r['relname']:<25s} {r['cnt']:>12,}")

        # ========== 2. stock schema 详细 ==========
        print("\n" + "=" * 60)
        print("2. STOCK SCHEMA 详细审核")
        print("=" * 60)

        # stock_basic
        r = pg.fetch_one("SELECT count(*) as cnt FROM stock.stock_basic")
        print(f"\n  stock_basic: {r['cnt']:,} 只股票")
        r = pg.fetch_one("""
            SELECT count(*) as cnt FROM stock.stock_basic
            WHERE ts_code IS NULL OR name IS NULL OR industry IS NULL
        """)
        print(f"    空值(ts_code/name/industry): {r['cnt']}")

        # stock_alias
        r = pg.fetch_one("SELECT count(*) as cnt FROM stock.stock_alias")
        print(f"\n  stock_alias: {r['cnt']} 条别名")
        rows = pg.fetch_all("SELECT * FROM stock.stock_alias LIMIT 5")
        for r in rows:
            print(f"    示例: {r}")

        # trade_calendar
        r = pg.fetch_one("""
            SELECT count(*) as total,
                   min(cal_date) as min_date, max(cal_date) as max_date,
                   count(CASE WHEN is_open=1 THEN 1 END) as open_days
            FROM stock.trade_calendar
        """)
        print(f"\n  trade_calendar: {r['total']:,} 天 ({r['min_date']} ~ {r['max_date']}), 交易日 {r['open_days']:,} 天")

        # daily
        r = pg.fetch_one("""
            SELECT count(*) as total,
                   min(trade_date) as min_date, max(trade_date) as max_date,
                   count(DISTINCT ts_code) as stocks,
                   count(DISTINCT trade_date) as dates
            FROM stock.daily
        """)
        print(f"\n  daily: {r['total']:,} 行")
        print(f"    股票数: {r['stocks']:,}, 交易日数: {r['dates']:,}")
        print(f"    时间范围: {r['min_date']} ~ {r['max_date']}")
        # 空值检查
        r2 = pg.fetch_one("""
            SELECT count(*) as cnt FROM stock.daily
            WHERE close IS NULL OR vol IS NULL
        """)
        print(f"    空值(close/vol): {r2['cnt']}")

        # daily 每年行数分布
        rows = pg.fetch_all("""
            SELECT left(trade_date, 4) as yr, count(*) as cnt
            FROM stock.daily
            GROUP BY yr ORDER BY yr
        """)
        print(f"    年度分布:")
        for r in rows:
            print(f"      {r['yr']}: {r['cnt']:>10,}")

        # daily_basic
        r = pg.fetch_one("""
            SELECT count(*) as total,
                   min(trade_date) as min_date, max(trade_date) as max_date,
                   count(DISTINCT ts_code) as stocks,
                   count(DISTINCT trade_date) as dates
            FROM stock.daily_basic
        """)
        print(f"\n  daily_basic: {r['total']:,} 行")
        print(f"    股票数: {r['stocks']:,}, 交易日数: {r['dates']:,}")
        print(f"    时间范围: {r['min_date']} ~ {r['max_date']}")
        r2 = pg.fetch_one("""
            SELECT count(*) as cnt FROM stock.daily_basic
            WHERE pe IS NULL AND pb IS NULL AND total_mv IS NULL
        """)
        print(f"    全空(pe/pb/total_mv): {r2['cnt']}")

        # adj_factor
        r = pg.fetch_one("""
            SELECT count(*) as total,
                   min(trade_date) as min_date, max(trade_date) as max_date,
                   count(DISTINCT ts_code) as stocks
            FROM stock.adj_factor
        """)
        print(f"\n  adj_factor: {r['total']:,} 行")
        print(f"    股票数: {r['stocks']:,}, 时间: {r['min_date']} ~ {r['max_date']}")

        # sync_meta
        rows = pg.fetch_all("SELECT * FROM stock.sync_meta ORDER BY sync_type, ts_code LIMIT 20")
        print(f"\n  sync_meta (前20):")
        for r in rows:
            print(f"    {r}")

        # ========== 3. fin schema 详细 ==========
        print("\n" + "=" * 60)
        print("3. FIN SCHEMA 详细审核")
        print("=" * 60)

        for tbl in ['income', 'balancesheet', 'cashflow', 'fina_indicator']:
            r = pg.fetch_one(f"""
                SELECT count(*) as total,
                       count(DISTINCT ts_code) as stocks,
                       min(end_date) as min_date, max(end_date) as max_date,
                       count(DISTINCT end_date) as periods
                FROM fin.{tbl}
            """)
            print(f"\n  {tbl}: {r['total']:,} 行")
            print(f"    股票数: {r['stocks']:,}, 报告期数: {r['periods']}")
            print(f"    时间范围: {r['min_date']} ~ {r['max_date']}")

            # 报告期分布
            rows = pg.fetch_all(f"""
                SELECT end_date, count(*) as cnt
                FROM fin.{tbl}
                GROUP BY end_date ORDER BY end_date DESC LIMIT 10
            """)
            print(f"    最近10个报告期:")
            for r2 in rows:
                print(f"      {r2['end_date']}: {r2['cnt']:,}")

        # fina_indicator 关键字段空值
        r = pg.fetch_one("""
            SELECT
                count(*) as total,
                count(roe) as has_roe,
                count(grossprofit_margin) as has_gpm,
                count(netprofit_margin) as has_npm,
                count(debt_to_assets) as has_dta,
                count(or_yoy) as has_or_yoy,
                count(netprofit_yoy) as has_np_yoy
            FROM fin.fina_indicator
        """)
        print(f"\n  fina_indicator 关键字段覆盖:")
        total = r['total'] or 1
        for k in ['has_roe','has_gpm','has_npm','has_dta','has_or_yoy','has_np_yoy']:
            pct = r[k] / total * 100
            print(f"    {k}: {r[k]:,} / {total:,} ({pct:.1f}%)")

        # income 关键字段空值
        r = pg.fetch_one("""
            SELECT
                count(*) as total,
                count(revenue) as has_revenue,
                count(n_income) as has_n_income,
                count(total_profit) as has_total_profit
            FROM fin.income
        """)
        print(f"\n  income 关键字段覆盖:")
        total = r['total'] or 1
        for k in ['has_revenue','has_n_income','has_total_profit']:
            pct = r[k] / total * 100
            print(f"    {k}: {r[k]:,} / {total:,} ({pct:.1f}%)")

        # ========== 4. 宽表视图 ==========
        print("\n" + "=" * 60)
        print("4. 宽表视图验证")
        print("=" * 60)
        for v in ['fin.v_financial_summary', 'stock.v_daily_valuation']:
            try:
                r = pg.fetch_one(f"SELECT count(*) as cnt FROM {v}")
                print(f"  {v}: {r['cnt']:,} 行")
                # 样本
                sample = pg.fetch_one(f"SELECT * FROM {v} LIMIT 1")
                if sample:
                    cols = list(sample.keys())
                    print(f"    列数: {len(cols)}, 前10列: {cols[:10]}")
            except Exception as e:
                print(f"  {v}: ❌ {e}")

        # ========== 5. 交叉验证 ==========
        print("\n" + "=" * 60)
        print("5. 交叉验证 & 覆盖缺口")
        print("=" * 60)

        # daily 有但 daily_basic 没有的日期
        r = pg.fetch_one("""
            SELECT count(DISTINCT d.trade_date) as missing_dates
            FROM stock.daily d
            LEFT JOIN stock.daily_basic db ON d.trade_date = db.trade_date
            WHERE db.trade_date IS NULL
        """)
        print(f"  daily 有但 daily_basic 缺失的交易日数: {r['missing_dates']}")

        # daily 有但 adj_factor 没有的股票
        r = pg.fetch_one("""
            SELECT count(DISTINCT d.ts_code) as missing_stocks
            FROM stock.daily d
            LEFT JOIN stock.adj_factor af ON d.ts_code = af.ts_code
            WHERE af.ts_code IS NULL
        """)
        print(f"  daily 有但 adj_factor 缺失的股票数: {r['missing_stocks']}")

        # stock_basic 有但 fina_indicator 没有的股票
        r = pg.fetch_one("""
            SELECT count(*) as missing
            FROM stock.stock_basic sb
            LEFT JOIN fin.fina_indicator fi ON sb.ts_code = fi.ts_code
            WHERE fi.ts_code IS NULL
        """)
        print(f"  stock_basic 有但 fina_indicator 缺失的股票数: {r['missing']}")

        # 最近交易日是否有数据
        r = pg.fetch_one("""
            SELECT max(cal_date) as latest
            FROM stock.trade_calendar
            WHERE is_open = 1 AND cal_date <= to_char(now(), 'YYYYMMDD')
        """)
        latest_open = r['latest']
        r2 = pg.fetch_one("SELECT count(*) as cnt FROM stock.daily WHERE trade_date = %s", (latest_open,))
        r3 = pg.fetch_one("SELECT count(*) as cnt FROM stock.daily_basic WHERE trade_date = %s", (latest_open,))
        print(f"\n  最近交易日: {latest_open}")
        print(f"    daily 行数: {r2['cnt']:,}")
        print(f"    daily_basic 行数: {r3['cnt']:,}")

        # ========== 6. 财务数据每股覆盖深度 ==========
        print("\n" + "=" * 60)
        print("6. 财务数据覆盖深度 (每只股票有多少期)")
        print("=" * 60)
        for tbl in ['income', 'balancesheet', 'cashflow', 'fina_indicator']:
            rows = pg.fetch_all(f"""
                SELECT cnt, count(*) as stocks
                FROM (SELECT ts_code, count(*) as cnt FROM fin.{tbl} GROUP BY ts_code) t
                GROUP BY cnt ORDER BY cnt
            """)
            print(f"\n  {tbl} 报告期数分布:")
            for r in rows:
                print(f"    {r['cnt']:>3} 期: {r['stocks']:>5} 只股票")

if __name__ == "__main__":
    run()
