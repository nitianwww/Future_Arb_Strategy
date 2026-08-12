# all_product_config 列结构

`Sheet1`，每行一个期货品种（按品种代码排序）。列：

| 列名 | 含义 | 口径/示例 |
|------|------|----------|
| `product` | 品种代码 | 大写，如 A、CU、RM、IF；与交易所一致（郑商所大写、其它多小写但表内统一大写） |
| `product_name` | 品种中文名 | 豆一、铜、菜粕 |
| `multiple` | 合约乘数 | TqSdk `volume_multiple`，权威。如 IF=300、CU=5 |
| `tick_size` | 最小变动价位 | TqSdk `price_tick`，权威。如 TS=0.002、AU=0.02 |
| `trading_hours` | 交易时段 | JSON 二维数组，跨日用 24+ 记法：夜盘 21:00 收 23:00/25:00(次日01:00)/26:30(次日02:30) |
| `industry_name` | 板块 | 有色、油脂、化工、焦煤钢矿、贵金属、谷物、农产品、软商品、建材、能源、股指、国债、服务 |
| `exchange` | 交易所 | SHFE/INE/DCE/CZCE/GFEX/CFFEX |
| `daily_storage_fee` | 仓储费(每天每单位) | 多档取最高，单位随品种 → 见 storage-fee.md |
| `delivery_month` | **仓单注销月** | 字符串列表如 `['03','07','11']`；不可转抛品种统一 `1-12`；长期有效留空 → 见 rollover-and-cancel-month.md |
| `roll_over_arbitrage` | **是否可转抛** | Y/N，跨期套利能否转抛 → 见 rollover-and-cancel-month.md |
| `index` | 标的指数(股指/指数类) | 如 SSE.000300 |
| `underlying_type` | 标的类型 | spot / index |
| `main_contract` | 主力合约示例 | 历史值，参考用 |
| `create_date` | 建表日期 | |
| `time_decay` | **特殊时间贴水**(日历/月份衰减) | JSON 列表，每年重复，两种类型可混用：每日型 `{"per_day":4,"from":"08-01","to":"11-21"}`(CF棉花)；阶梯型 `{"step":"08-01","amount":20}`(SR白糖,跨过该日+amount)。转抛类计算必须计入 |
| `verified_date` | **最后核对日期** | 该行规则最近一次对交易所细则/TqSdk 核对的日期(YYYY-MM-DD)。**改任何字段必须同时更新此列**；核对越久远越要警惕细则已修订(CJ 贴水就是 2024-12 修订新增的) |
| `note` | 备注 | 前缀 `[仓储费]`/`[转抛]`/`[注销月]`/`[时间贴水]`/`[上市]` + 原有"新增/更新"说明 |
| `list_date` | **品种上市日期** | RQData `all_instruments(type='Future')` 每品种最早合约 `listed_date`(YYYY-MM-DD)。改代码品种追溯前身(A←S、MA←ME、ZC←TC、OI←RO、RI←ER、WH←WS、PM←WT)；FU/WR/FB 长期停摆后重新挂牌，算"有效交易时长"用重启日(见 note `[上市]`)。20260710 版新增 |

注意：
- `delivery_month` 字段名是历史遗留，**实际含义是"仓单强制注销月"**，不是交割月。
- 写回 xlsx 用 openpyxl 保留格式；备注**追加不覆盖**。
