import tushare as ts
import pandas as pd
from sqlalchemy import create_engine, text
import time
from datetime import datetime

# 1. 基础配置
TUSHARE_TOKEN = '0a9ad9e135d858ac90dc0d91b18a1ba3d4251b93831b069edb23f188'
DB_URL = "mysql+pymysql://root:%40Lish145210@127.0.0.1:3306/stock"

# 初始化
pro = ts.pro_api(TUSHARE_TOKEN)
engine = create_engine(DB_URL)


def fetch_single_day(date_str):
    """抓取单日数据并入库"""
    try:
        # 获取日线数据
        df = pro.daily(trade_date=date_str)

        # 如果是假期或周末，Tushare 会返回一个空的 DataFrame
        if df is None or df.empty:
            return False

        # 数据预处理
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date

        # 写入数据库
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM daily_data WHERE trade_date = :d"),
                {"d": date_str}
            )
            df.to_sql('daily_data', con=conn, if_exists='append', index=False,
                      chunksize=1000)

        return True
    except Exception as e:
        # 如果是因为积分不足导致的报错，打印出来
        if "权限" in str(e) or "积分" in str(e):
            print(f"致命错误: 你的积分可能不足以访问 daily 接口。详情: {e}")
            return "STOP"
        print(f"抓取 {date_str} 失败: {e}")
        return False


def batch_fetch_2026_manual():
    # 1. 手动生成 2026-01-01 到今天的日期范围
    start_date = datetime(2022, 1, 1)
    end_date = datetime(2024, 12, 31)
    # 生成日期序列
    date_range = pd.date_range(start_date, end_date)

    print(f"计划检查 {len(date_range)} 个日历日...")

    count = 0
    for current_date in date_range:
        if current_date.weekday() >= 5:
            continue

        day_str = current_date.strftime('%Y%m%d')

        # 执行抓取
        result = fetch_single_day(day_str)

        if result == "STOP":
            break
        elif result:
            print(f"成功入库: {day_str} √")
            count += 1
            # Tushare 每分钟限制：如果是最低档积分，建议 sleep 久一点
            time.sleep(0.5)
        else:
            print(f"跳过非交易日: {day_str}")

    print(f"任务结束！共计填充了 {count} 个交易日的数据。")


if __name__ == "__main__":
    batch_fetch_2026_manual()