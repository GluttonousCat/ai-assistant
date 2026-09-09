"""查看个股最新报告期主营构成 (chain-beta 模板关键词排查用)

输出该公司最新报告期 P(产品)/I(行业)/D(地区) 维度构成与收入占比,
用于对照公司真实披露口径设计/修正链模板 keywords。

用法 (项目根目录):
    .venv/Scripts/python.exe .agents/skills/chain-beta/scripts/show_mainbiz.py 002463.SZ
    .venv/Scripts/python.exe .agents/skills/chain-beta/scripts/show_mainbiz.py 002463.SZ --type I
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # 项目根

from storage.pg import PgClient  # noqa: E402

_TYPE_LABEL = {"P": "产品", "I": "行业", "D": "地区"}


def main() -> None:
    parser = argparse.ArgumentParser(description="个股最新报告期主营构成")
    parser.add_argument("ts_code", help="股票代码, 如 002463.SZ")
    parser.add_argument("--type", default="P", choices=["P", "I", "D"],
                        help="构成维度 (默认 P=产品)")
    args = parser.parse_args()

    with PgClient() as pg:
        latest = pg.fetch_one(
            "SELECT DISTINCT ON (ts_code) ts_code, end_date FROM fin.fina_mainbz "
            "WHERE ts_code=%s AND biz_type=%s ORDER BY ts_code, end_date DESC",
            (args.ts_code, args.type))
        if not latest:
            print(f"无主营构成数据: {args.ts_code} (可能未同步, 见 data-ops.md)")
            raise SystemExit(1)
        rows = pg.fetch_all(
            "SELECT bz_item, sales_share_pct, gross_margin_pct, is_sub_item "
            "FROM fin.v_main_biz "
            "WHERE ts_code=%s AND end_date=%s AND biz_type=%s "
            "ORDER BY sales_share_pct DESC NULLS LAST LIMIT 15",
            (args.ts_code, latest["end_date"], args.type))

    print(f"{args.ts_code} 最新报告期 {latest['end_date']} "
          f"{_TYPE_LABEL[args.type]}维度构成 (占比% / 毛利率%):")
    for r in rows:
        sub = " [子项]" if r["is_sub_item"] else ""
        gm = f" / {r['gross_margin_pct']}" if r["gross_margin_pct"] is not None else ""
        print(f"  {str(r['bz_item'])[:36]:36} {r['sales_share_pct']}{gm}{sub}")


if __name__ == "__main__":
    main()
