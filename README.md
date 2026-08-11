# ETF 期权雷达云端数据任务

该仓库在中国交易日按北京时间 09:45、11:30、13:30、14:30、15:00 自动抓取并核验 510050、510300、510500、159915、588000 的近月/次月 ETF 期权数据。

- 完整抓取程序：`work/generate_option_report.py`
- 云端交易日与时点门禁：`work/cloud_refresh.py`
- 网页精简数据：`data/dashboard_data.json`
- 完整审计数据：`data/option_report_data.json`
- 分时归档：`data/snapshots/YYYY-MM-DD/HHmm.json`

只有当日、同源日期一致且交易日门禁通过的数据才会写入仓库。免费公开行情没有交易所级 SLA，模型结果不构成投资建议。