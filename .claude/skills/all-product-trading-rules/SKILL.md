---
name: all-product-trading-rules
description: 中国期货全品种交易规则速查与 all_product_config 表维护。当需要查询或核对某期货品种的合约乘数(multiple)、最小变动价位(tick)、交易时间、仓储费、仓单注销月、是否可转抛(跨期套利)、交割方式(仓库/厂库/车船板)，或新增/更新 all_product_config*.xlsx 配置表时使用。覆盖上期所(SHFE)、能源中心(INE)、大商所(DCE)、郑商所(CZCE)、广期所(GFEX)、中金所(CFFEX)。涉及 TqSdk 字段、国泰君安规则汇总表、各交易所交割细则。也覆盖价差监控(spread_monitor)的盘口合成、合并买卖价、价差比例/仓储费比例/转抛比例等计算口径，以及逐合约手续费/保证金表 futures_comm_info 的维护与更新。
---

# 全品种交易规则与 all_product_config 维护

本 skill 固化了 `all_product_config*.xlsx`（期货套利项目的品种参数表）各字段的口径、判定规则、权威数据源与核对方法。维护或核对该表、或回答某品种交易规则时，先读本文件，再按需读 references。

## 先路由

- 字段含义、列结构 → `references/config-schema.md`
- **是否可转抛(roll_over) + 仓单注销月** 的判定规则与逐品种取值 → `references/rollover-and-cancel-month.md`（最常用）
- 仓储费口径（多档取最高、单位、季节性、SH干湿吨等）与逐品种值 → `references/storage-fee.md`
- 去哪核实每个字段、各交易所官网反爬绕法 → `references/data-sources-and-verification.md`
- 交易所/品种分类事实（价差合约、仅厂库、车船板、每月注销、长期有效金属等）→ `references/exchange-facts.md`
- 价差监控的盘口合成、标签、合并买卖价、价差比例/仓储费比例/转抛比例口径（`spread_monitor.py`）→ `references/spread-monitor-and-ratios.md`
- 逐合约手续费/保证金表 futures_comm_info 的维护、更新方法、与 all_product 的关系（`futures_comm_info.py`）→ `references/futures-comm-info.md`

## 核心规则（高频，先记住）

### 1. 是否可转抛 roll_over_arbitrage（Y/N）
判断能否做跨期正套（近月接货拿仓单→远月再抛出交割）。**满足任一即不可转抛(N)**：
1. 每个合约月都注销仓单；
2. 含车(船)板交割；
3. 以注册/生产日计有效期、无具体注销月份（如"生产日起360天"）。
4. **双重规则**（既像可转抛又像不可）→ 保守取 **N**。

**仅厂库交割（无仓库）→ 算可转抛(Y)**，但备注注明"仅厂库"。
现金交割/无标准仓单（国债、集运EC、股指仅财务滚动）：国债/EC 记 **N**；股指记 **Y**。

### 2. 仓单注销月 delivery_month
- **不可转抛(N) 的品种，注销月一律写 1–12**（即使无对应合约月份）；不可转抛的具体原因写进备注。
- **可转抛(Y) 的品种，注销月 = 实际强制注销月份**；仓单长期有效(无强制注销)则留空。
- 有效期挂钩生产年份的，记**最终注销的日历月份**（如棉花N+1年11月→11、红枣→9、菜油→5）。
- **闭环校验**：全表必须满足 `转抛=N ⟺ 注销月=1–12`，0 违例。

### 3. 仓储费 daily_storage_fee
- 多档（库房/货场、仓库/厂库、新疆/内地、季节）一律**取最高档**，各档写进备注。
- 单位随合约：多数 元/吨·天；黄金 AU 元/克、白银 AG 元/千克、原油 SC 元/桶、胶合板 BB 元/张、纤维板/原木 FB/LG 元/立方米。
- 特殊：**SH 烧碱**按干吨口径（约32%纯度，干吨≈湿吨×3）。

## 维护流程（更新/核对时）
1. **乘数 multiple、tick**：以 TqSdk 实时为准（`query_symbol_info` / `get_quote` 的 `volume_multiple`/`price_tick`），最权威。国君表/其它可能过时。
2. **仓储费、注销月、交割方式**：国君《结算细则》sheet 为底，**新品种或有疑问时去对应交易所官网交割细则/手册核实**（反爬绕法见 data-sources）。
3. 改 xlsx 用 openpyxl，**保留格式**，备注用前缀 `[仓储费]`/`[转抛]`/`[注销月]` 追加、**不覆盖原有备注**。新文件名 `all_product_config<YYYYMMDD>.xlsx`，保留旧版对照。
4. 改完跑闭环校验（转抛⟺注销月）和 tick/乘数对 TqSdk 复查。

## 备注前缀约定
`[仓储费]` 多档/单位/来源；`[转抛]` 不可转抛原因或仅厂库说明；`[注销月]` 注销特殊情况。原有"新增/更新"说明一律保留。
