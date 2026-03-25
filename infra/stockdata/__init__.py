import akshare as ak

stock = ak.stock_sse_summary()
print(stock)

import akshare as ak

stock_szse_summary_df = ak.stock_szse_summary(date="20260323")
print(type(stock_szse_summary_df))
print(stock_szse_summary_df)


stock_szse_sector_summary_df = ak.stock_szse_sector_summary(symbol="当年",
                                                            date="202602")
print(stock_szse_sector_summary_df)

futures = ak.get_rank_sum_daily(start_day="20260323", end_day="20260323",
                              vars_list=["IF", ""])

print(futures)


