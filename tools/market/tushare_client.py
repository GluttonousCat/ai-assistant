"""
Tushare 行情数据客户端
- token / 数据库连接统一从 core.config 读取
- 提供日线抓取与入库能力
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

import pandas as pd

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)


class TushareClient:
    """Tushare 行情客户端"""

    def __init__(self, token: Optional[str] = None, db_url: Optional[str] = None):
        config = get_config()
        self.token = token or config.tushare_token
        self.db_url = db_url or config.mysql_url

        if not self.token:
            raise ValueError("未配置 TUSHARE_TOKEN (请在 .env 中设置)")

        import tushare as ts
        self._ts = ts
        self.pro = ts.pro_api(self.token)

        # 延迟创建 engine, 避免无数据库场景下报错
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            from sqlalchemy import create_engine
            self._engine = create_engine(self.db_url)
        return self._engine

    def fetch_daily(self, trade_date: str) -> pd.DataFrame:
        """获取单日全市场日线数据"""
        return self.pro.daily(trade_date=trade_date)

    def fetch_stock_daily(
        self, ts_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """获取个股区间日线"""
        return self.pro.daily(
            ts_code=ts_code, start_date=start_date, end_date=end_date
        )

    def save_daily_to_db(self, date_str: str, table: str = "daily_data") -> bool | str:
        """
        抓取单日数据并入库
        返回: True 成功 / False 失败或非交易日 / "STOP" 权限不足
        """
        from sqlalchemy import text

        try:
            df = self.fetch_daily(date_str)
            if df is None or df.empty:
                return False

            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

            with self.engine.begin() as conn:
                conn.execute(
                    text(f"DELETE FROM {table} WHERE trade_date = :d"),
                    {"d": date_str},
                )
                df.to_sql(
                    table, con=conn, if_exists="append",
                    index=False, chunksize=1000,
                )
            return True
        except Exception as e:
            msg = str(e)
            if "权限" in msg or "积分" in msg:
                logger.error(f"积分/权限不足, 无法访问 daily 接口: {e}")
                return "STOP"
            logger.error(f"抓取 {date_str} 失败: {e}")
            return False

    def batch_fetch_daily(self, start: datetime, end: datetime,
                          table: str = "daily_data",
                          sleep: float = 0.5) -> int:
        """批量抓取区间日线 (跳过周末), 返回成功交易日数量"""
        date_range = pd.date_range(start, end)
        logger.info(f"计划检查 {len(date_range)} 个日历日...")

        count = 0
        for current_date in date_range:
            if current_date.weekday() >= 5:
                continue
            day_str = current_date.strftime("%Y%m%d")
            result = self.save_daily_to_db(day_str, table=table)

            if result == "STOP":
                break
            elif result:
                logger.info(f"成功入库: {day_str}")
                count += 1
                time.sleep(sleep)
            else:
                logger.debug(f"跳过非交易日: {day_str}")

        logger.info(f"任务结束, 共填充 {count} 个交易日数据")
        return count


if __name__ == "__main__":
    client = TushareClient()
    client.batch_fetch_daily(datetime(2024, 1, 1), datetime(2024, 12, 31))
