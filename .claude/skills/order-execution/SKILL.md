---
name: order-execution
description: 本项目下单执行算法。当需要修改/调试组合合约下单(spread_trader)、被动到价单状态机、网格策略(grid_strategy)、实盘账户配置([trade])，或涉及 TqSdk insert_order/组合合约交易限制时使用。
---

# 下单执行算法

项目第 3 块，**目前只完成核心骨架**。数据管道见 skill `market-data-pipeline`；筛选见 `opportunity-screening`。

## 硬性事实（已验证）

- **交易所组合合约（DCE.SP / CZCE.SPD / GFEX.SP）只有实盘账户能交易**，TqSim/模拟不支持（官方文档 option_trade.html 查实）。开发一律走 dry-run，实盘用 TqAccount。
- 组合合约单笔委托 = 交易所撮合双腿，**无单腿腿风险**——这是全项目选择组合合约执行的根本原因。
- 组合盘口薄，`wait_update` 必须带 deadline（`time.time()+0.4`），否则永久阻塞。

## spread_trader.py

- **PassiveSpreadOrder 状态机**（被动到价单）：按目标价差挂限价 → 等成交 → 超时撤单（order_timeout）→ 价差走坏放弃（give_up_ticks）。
- `resolve_combo(api, prod, near, far)`：找到对应交易所组合合约，处理腿序（组合腿序为远−近时方向要翻转）。
- `side_to_order`：做多价差(买近卖远)/做空价差 → 组合合约的 BUY/SELL 映射，含腿序 orient 修正。
- `place_passive_spread(...)`：供筛选结果自动触发的编程入口；CLI 手动下单也走 main。
- **dry-run 是默认模式**：真行情模拟成交，不真实发单；`--live` 才走 TqAccount。
- config.ini `[trade]`：broker_id/account/password（**目前为空，实盘账户待用户填**）、order_timeout=60、max_volume=10、give_up_ticks=0。

## 未完成（待办清单）

- `_run_live` **从未在实盘验证**——需用户填实盘账户后小单验证 direction/offset 语义与成交后两腿持仓形态。
- 持仓跟踪、平仓管理、部分成交补挂、从筛选结果自动触发下单——全部未做。
- 选中机会（opportunities.html localStorage `pinnedOpps`）目前只存浏览器；要喂给下单程序需升级为文件存储（如 data/selected_opps.json）。

## grid_strategy.py（网格策略，用户明确暂停中）

- 网格引擎骨架已建：`grid_params`/`GridSpreadStrategy`；`profit_ticks` 基于 futures_comm_info 逐合约手续费（往返费用 2×fee 规则折算最小盈利跳数）；排队/跳价定价逻辑。
- `GRID_CONFIG` 是占位，待接 strategy_config.xlsx 的每品种配置。恢复开发前先向用户确认参数设计。

## 手续费依赖

逐合约手续费/保证金表 `futures_comm_info.py`（9qihuo 抓取 + TqSdk 字段合并，F 合约剔除），维护方法见 skill `all-product-trading-rules` 的 references/futures-comm-info.md。
