# -*- encoding: utf-8 -*-
"""
内容管线 CLI

    python -m agent.content profile 中际旭创                     # 画像 (纯数据)
    python -m agent.content article --stock 中际旭创             # 公众号文章 (画像模式)
    python -m agent.content article --topic 光模块               # 公众号文章 (研报综述模式)
    python -m agent.content interpret 中芯国际                   # 投研解读 (年报语料增强)
    python -m agent.content interpret 中芯国际 --ppt             # 解读 + PPT 一条龙
    python -m agent.content ppt 中际旭创                         # 画像 -> pptgen 出片
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    p = argparse.ArgumentParser(description="内容管线: 画像/公众号文章/PPT")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="上市公司画像 (纯数据组装)")
    sp.add_argument("stock")
    sp.add_argument("--years", type=int, default=5)

    sa = sub.add_parser("article", help="公众号文章")
    sa.add_argument("--stock", default="", help="股票画像模式")
    sa.add_argument("--topic", default="", help="研报综述模式")
    sa.add_argument("--years", type=int, default=5)

    si = sub.add_parser("interpret",
                        help="投研解读 (画像+年报语料+六模块+五图, 可联动PPT)")
    si.add_argument("stock")
    si.add_argument("--years", type=int, default=5)
    si.add_argument("--annual-year", type=int, default=0,
                    help="指定年报年度 (缺省最新已下载)")
    si.add_argument("--no-annual", action="store_true", help="不用年报语料")
    si.add_argument("--ppt", action="store_true", help="解读后联动 PPT 出片")

    st = sub.add_parser("ppt", help="画像 PPT (走 ../pptgen)")
    st.add_argument("stock")
    st.add_argument("--out", default="")
    st.add_argument("--task-book-only", action="store_true",
                    help="只生成任务书不出片")

    args = p.parse_args()
    if args.cmd == "profile":
        from agent.content.profile import build_company_profile, profile_digest
        prof = build_company_profile(args.stock, args.years)
        print(json.dumps({k: prof[k] for k in
                          ("name", "ts_code", "ok_sections", "failed_sections")},
                         ensure_ascii=False))
        print("\n---- 摘要 ----\n", profile_digest(prof))
        return 0
    if args.cmd == "article":
        from agent.content.article import write_stock_article, write_topic_article
        res = (write_topic_article(args.topic) if args.topic
               else write_stock_article(args.stock, args.years))
        print(json.dumps({k: v for k, v in res.items() if k != "preview"},
                         ensure_ascii=False, indent=1))
        print("\n---- 预览 ----\n", res["preview"])
        return 0
    if args.cmd == "interpret":
        from agent.content.article import write_stock_article
        res = write_stock_article(
            args.stock, args.years,
            annual_year=args.annual_year or None,
            use_annual=not args.no_annual)
        print(json.dumps({k: v for k, v in res.items() if k != "preview"},
                         ensure_ascii=False, indent=1))
        print("\n---- 预览 ----\n", res["preview"])
        if args.ppt:
            from agent.content.ppt import generate_profile_ppt
            p = generate_profile_ppt(args.stock)
            print(json.dumps({k: p[k] for k in ("ok", "pptx", "error")
                              if k in p}, ensure_ascii=False, indent=1))
            return 0 if p.get("ok") else 1
        return 0
    if args.cmd == "ppt":
        from agent.content.ppt import generate_profile_ppt, make_task_book
        res = (make_task_book(args.stock) if args.task_book_only
               else generate_profile_ppt(args.stock, args.out))
        print(json.dumps(res if args.task_book_only else
                         {k: res[k] for k in ("ok", "pptx", "task_book", "error")},
                         ensure_ascii=False, indent=1))
        return 0 if res.get("ok", True) else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
