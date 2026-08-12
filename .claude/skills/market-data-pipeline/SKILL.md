---
name: market-data-pipeline
description: 本项目实时行情与历史行情处理。当需要修改/调试价差监控(spread_monitor)、日K缓存(hist_stats/daily_k)、历史同期统计页、每日tick下载(hist_tick_daily)、仓单数据(warrants)、快照归档(snapshots)，或涉及 TqSdk 连接管理、K线预热、数据目录布局时使用。
---

# 实时行情与历史行情处理

项目第 1 块（历史数据）+ 实时盘口部分。品种规则/手续费见 skill `all-product-trading-rules`；筛选见 `opportunity-screening`；下单见 `order-execution`。

## 实时行情：spread_monitor.py

- **发现组合**：`query_quotes(ins_class="COMBINE")`，符号如 `DCE.SP m2609&m2611`（CZCE 前缀 `SPD`，GFEX `SP`）。`parse_combine` 解析腿：只留同品种跨期，剔除 SPC/IPS 跨品种和 F 结算腿。已知遗漏：`COMBINE_EXCHANGES` 缺 GFEX（其他模块已覆盖）。
- **方向恒为 近月 − 远月**。组合腿序若为远−近，盘口取负翻转（comb_orient）。
- **三腿盘口**：近月 outright + 远月 outright + 交易所组合，各自保留 bid/ask 价与量。合成价差：合成买=近bid−远ask，合成卖=近ask−远bid。
- **合并盘口**：合并买价=max(合成买, 组合买)，合并卖价=min(合成卖, 组合卖)。最新价差必须 **clamp 到 [合并买, 合并卖]**。
- **比例口径**（详见 all-product-trading-rules/references/spread-monitor-and-ratios.md）：价差比例%=价差/近月×100；仓储费比例=价差/(−仓储费×30)；转抛比例=价差/−fullcarry。
- 标签：主力/注销月/注销月后第一个月/近月（`compute_tags`）。
- `can_roll(near, far)`：[near, far) 区间内无注销月才可转抛；`in_delivery_month` 已进交割月剔除（股指 INDEX_PRODUCTS 豁免）；`decay_cost` 支持 per_day 窗口与 step 阶梯（时间贴水，每年重复）。

## 全局铁律

- **F 结尾结算价合约（如 l2607F）在所有模块全部剔除**：`is_settle_f()`（正则 `\d[Ff]$`）。用户明确决定。
- **`wait_update` 必须带 deadline**：`api.wait_update(deadline=time.time()+0.4)`，冷门组合合约无行情推送会永久阻塞。
- **单一 TqApi 连接订阅约 1000 根 K线序列后会整体退化超时**。批量取K线必须分块换新连接：`hist_stats.warm_cache(user, pwd, symbols, chunk=250)` 每 250 合约关旧连接开新连接。
- 空K线 bar（epoch=0）要过滤 `datetime>0`，否则图表坐标轴被拉爆。

## 日K缓存：hist_stats.py

- 路径 `data/daily_k/{symbol}.parquet`，列 **date/close/oi**（2026-07-10 起全量含 oi；无 oi 列=旧 schema 视为过期需重取）。
- `_cache_fresh`：过期合约（末bar距今>45天）**不可变永久缓存**；活跃合约每日刷新。
- `get_daily_batch(api, symbols)` 每批 12 个订阅；`warm_cache` 是唯一大批量入口。
- **年份版本**（历史同期统计）：`find_versions` 找各年固定合约对（如 m2509-m2601, m2409-m2501…不跨年拼接）；`version_series` 在**近月腿交割月 1 日前截断**；横轴 MM-DD（`_ordkey` 锚定近月月份）；`peer_stats` 算同期分位/全期分位。
- CZCE 三位月份码跨十年歧义（SA109→2021 还是 2031）：过期合约用 K线末 bar 年份定年（`_delivery_year`）。
- **同码换代污染（用户抓到的 bug）**：CZCE 三位码每 10 年循环，TqSdk 按代码取 K线会把上一代合约数据连在一起（实例：MA610-611 的 2026 年版混入 2016 年数据，历史页红线出现"今天之后"的幽灵段、斜率序列冒出 2016/2017 年份）。`_trim_recycled(df)` 在 `get_daily_batch` 读缓存与新取两处兜底：序列内 >180 天断档=换代，只留最后一段。
- **期限结构斜率**：`_wls_slope` 持仓量加权回归（close ~ 距到期月数），`slope_series_cached(prod)` 逐日序列（有 memo 缓存）。供筛选条件、历史页图2、详情页面板7。**图2/面板7 展示层取负**（用户口径：价差习惯近−远，取负后 正=back 负=contango）；screener 内部（`_slope_ctx`/残差回归/curve_structure）仍用原始斜率。
- 历史同期页生成：`build_spread_hist`（单价差）/`build_pair_hist`（A/B/A−B 三图）→ `output/detail/hist/{PROD}_{NN}-{FF}.html`、`{PROD}_{aa}{bb}_vs_{cc}{dd}.html`；入口 `ensure_pages(jobs)`，jobs 来自 `hist_jobs_from_opps`。

## 每日 tick 下载：hist_tick_daily.py

- 组合合约全量 tick → `{data.root}/ticks_combo/{YYYYMMDD}/part_*.parquet`；腿合约压成 1 分钟盘口 bar → `{data.root}/book1m/`。全市场约 11.5MB/天。
- `data.root = I:\futures_data`（config.ini [data]），**必须在 BaiduSyncdisk 之外**（同步盘会撑爆/冲突）。
- `_meta_daily.json` 记录 done_symbols 断点续跑；`--date YYYYMMDD` 指定日、`--backfill N` 补最近 N 个工作日缺口；`--products` 过滤时**不许**标记当日 done。
- TqSdk 专业版能力（已实测）：普通合约 tick 回溯 2016+，组合 tick 至少回溯 2021，过期合约 K线可取。

## 仓单与快照

- 仓单：`product_detail.fetch_warrants(prod)` 走 RQData `futures.get_warehouse_stocks`（on_warrant），缓存 `data/warrants/{prod}.parquet` 每日一刷。RQData 账号密码均 15618075501（`rqdatac.init`）。主力序列 `futures.get_dominant`。
- 快照归档：`screen_report.archive_snapshot` → `data/snapshots/{spreads,opportunities}/{YYYYMMDD}.parquet`，**只追加新文件不替换**（用户要求增量存储）。

## 凭据与配置

config.ini：[auth] 天勤账号密码均 15618075501；[settings] exchanges/max_months=6/funding_rate=6/product_config=all_product_config20260629.xlsx；[data] root。Python 3.13.8（C:\veighna_studio），tqsdk 3.8.6 专业版。
