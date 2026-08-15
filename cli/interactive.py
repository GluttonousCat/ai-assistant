#!/usr/bin/env python3
"""
交互式命令行模块
整合原 zsxq_interactive_crawler.py 的交互逻辑
"""
import sys
import argparse
from typing import Optional

from tools.zsxq.crawler import ZSXQCrawler
from tools.zsxq.downloader import FileDownloader
from storage.sqlite.account import AccountDatabase
from core.config import get_config
from utils.paths import get_path_manager


class InteractiveCLI:
    """交互式命令行"""

    def __init__(self):
        self.crawler: Optional[ZSXQCrawler] = None
        self.downloader: Optional[FileDownloader] = None
        self.account_db = AccountDatabase()

    def init_crawler(self, group_id: str = None):
        """初始化爬虫"""
        config = get_config()
        cookie = config.zsxq_cookie

        if not cookie:
            # 尝试从账号数据库获取
            account = self.account_db.get_default_account(mask_cookie=False)
            if account:
                cookie = account['cookie']
            else:
                print("❌ 未配置Cookie，请先添加账号")
                return False

        if not group_id:
            group_id = config.zsxq_group_id

        if not group_id:
            print("❌ 未配置群组ID")
            return False

        self.crawler = ZSXQCrawler(cookie, str(group_id))
        return True

    def show_menu(self):
        """显示菜单"""
        print("\n" + "=" * 50)
        print("🕷️ 知识星球数据采集器")
        print("=" * 50)
        print("\n📥 话题采集:")
        print("  1. 爬取最新话题")
        print("  2. 爬取历史数据")
        print("  3. 增量爬取")
        print("  4. 智能更新")
        print("\n📁 文件下载:")
        print("  5. 收集文件列表")
        print("  6. 下载文件 (每次1个)")
        print("\n⚙️ 其他:")
        print("  7. 查看数据库统计")
        print("  8. 账号管理")
        print("  0. 退出")
        print("=" * 50)

    def run(self):
        """运行交互循环"""
        if not self.init_crawler():
            return

        while True:
            try:
                self.show_menu()
                choice = input("\n请选择: ").strip()

                if choice == '0':
                    print("👋 再见")
                    break
                elif choice == '1':
                    self.crawl_latest()
                elif choice == '2':
                    self.crawl_historical()
                elif choice == '3':
                    self.crawl_incremental()
                elif choice == '4':
                    self.crawl_update()
                elif choice == '5':
                    self.collect_files()
                elif choice == '6':
                    self.download_files()
                elif choice == '7':
                    self.show_stats()
                elif choice == '8':
                    self.manage_accounts()
                else:
                    print("❌ 无效选择")

            except KeyboardInterrupt:
                print("\n⏹️ 中断")
                break
            except Exception as e:
                print(f"❌ 错误: {e}")

        if self.crawler:
            self.crawler.close()

    def crawl_latest(self):
        """爬取最新"""
        count = input("数量 (默认20): ").strip()
        count = int(count) if count.isdigit() else 20
        self.crawler.crawl_latest(count)

    def crawl_historical(self):
        """爬取历史"""
        pages = input("页数 (默认10): ").strip()
        pages = int(pages) if pages.isdigit() else 10
        self.crawler.crawl_historical(pages=pages)

    def crawl_incremental(self):
        """增量爬取"""
        pages = input("页数 (默认10): ").strip()
        pages = int(pages) if pages.isdigit() else 10
        self.crawler.crawl_incremental(pages=pages)

    def crawl_update(self):
        """智能更新"""
        self.crawler.crawl_latest_until_complete()

    def get_file_downloader(self):
        """获取文件下载器"""
        if self.downloader is None:
            from tools.zsxq.downloader import FileDownloader
            self.downloader = FileDownloader(
                self.crawler.cookie,
                self.crawler.group_id
            )
        return self.downloader

    def collect_files(self):
        """收集文件"""
        downloader = self.get_file_downloader()
        downloader.collect_files()

    def download_files(self):
        """下载文件"""
        downloader = self.get_file_downloader()
        downloader.download_pending(max_files=1)

    def show_stats(self):
        """显示统计"""
        stats = self.crawler.db.get_stats()
        print(f"\n📊 统计:")
        print(f"  话题: {stats.get('topics', 0)}")
        print(f"  用户: {stats.get('users', 0)}")
        print(f"  评论: {stats.get('comments', 0)}")

    def manage_accounts(self):
        """账号管理"""
        print("\n📋 账号列表:")
        accounts = self.account_db.list_accounts()
        for acc in accounts:
            default_mark = " (默认)" if acc['is_default'] else ""
            print(f"  - {acc['name']}{default_mark}")

        print("\n1. 添加账号  2. 删除账号  3. 设为默认")
        choice = input("选择: ").strip()

        if choice == '1':
            name = input("账号名称: ").strip()
            cookie = input("Cookie: ").strip()
            if cookie:
                self.account_db.add_account(cookie, name)
                print("✅ 添加成功")
        elif choice == '2':
            acc_id = input("账号ID: ").strip()
            if self.account_db.delete_account(acc_id):
                print("✅ 删除成功")


def auto_download():
    """自动下载模式"""
    config = get_config()
    cookie = config.zsxq_cookie
    group_id = config.zsxq_group_id

    if not cookie or not group_id:
        print("❌ 请先配置Cookie和群组ID")
        return

    print("🤖 自动下载模式")
    crawler = ZSXQCrawler(cookie, str(group_id))

    # 收集文件
    from tools.zsxq.downloader import FileDownloader
    downloader = FileDownloader(cookie, str(group_id))
    downloader.collect_files(max_pages=5)

    # 下载（每次1个）
    downloader.download_pending(max_files=1)

    crawler.close()
    downloader.close()


def start_cli():
    """启动CLI"""
    cli = InteractiveCLI()
    cli.run()


if __name__ == '__main__':
    start_cli()
