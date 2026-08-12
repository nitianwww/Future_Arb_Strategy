---
name: opportunity-screening
description: 本项目交易机会筛选。当需要修改/调试筛选策略(screener)、机会报告页(screen_report/opportunities.html)、品种详情图(product_detail)、转抛比例表(roll_ratio)、选中机会/自定义新增功能，或调整流动性过滤、可转抛判定、策略评分时使用。
---

# 交易机会筛选

项目第 2 块。数据管道见 skill `market-data-pipeline`；品种规则见 `all-product-trading-rules`；下单见 `order-execution`。

**人工筛选范式**（插值检验/注销月折价/递延+换月/极值刷波动，含实盘案例与 screener 策略映射）见 [references/manual-paradigms.md](references/manual-paradigms.md)——调整对应策略前先读，用其中案例验证口径。

## 核心模块 screener.py

数据结构：`Contract` / `Spread`(含 adjacent 相邻月标记) / `Butterfly` / `Opportunity`(含 liquid/flag/rollable) / `Universe`(meta, spread_by_pair)。

- `build_universe(api, meta, all_pairs=, max_months=8)`：all_pairs=True 生成全部月份对（否则只相邻）；**2026-08-12 起 screen_report 与 product_detail 均传 max_months=14=全月份窗口**（用户要求覆盖全月份；默认8个月窗口曾致品种页 03 后直接 05 漏 04）；`MAIN_CYCLE_159` 品种补 1/5/9 月合约对；F 合约剔除；`_sanity_check(filtered=)` 自检（品种过滤跑时 filtered=True 跳过对比与统计落盘）。统计文件 `output/universe_stats.json` 与 `universe_stats_allpairs.json` 分开。
- `_make_spread` 内：最新价差 **clamp 到 [合并买, 合并卖]**（用户曾抓到 PD -6 越界 bug）。
- `_fullcarry` = 1.1×近月价×利率%/100×月差/12 + 仓储费×30×月差 + `decay_cost`(时间贴水)。展示为负数。

## 策略清单（14 实现 + 1 stub）

**A 类实时（9）**：折角修复 `s_kink_repair`（**2026-08-10 用户重定义,弃蝶式口径**：三段 A/B/C=月差相同+跨注销数相同+近月等距，段互不重复；**2026-08-12 同构放松**：注销状态不再强制一致——三段状态相同=同构直接输出；非同构时注销偏置 `_cancel_bias`(仅远腿注销+1应偏高/仅近腿注销−1应偏低/both,none=0) 的 E=bias(B)−(bias(A)+bias(C))/2 须与信号方向同向(结构比同构更有利)才输出，插值为保守下限，conditions 带`同构性`列；**跨注销数仍须相同**(断链只解释偏离、方向不定，不放松)；B 为时间中段且 B≤A,C 或 B≥A,C；理论B=(A+C)/2，输出 理论−实际 价差；score=|价差|/主力价 比例倒序，无最小阈值；**三段区间完全不交叉(用户2026-08-11): A.far≤B.near 且 B.far≤C.near,首尾相接允许**；kind="kink"，structure="A | B | C"，`_annotate` 有对应腿识别分支(book=B段盘口、做多方向按B段判rollable)）、主力合约折角修复 `s_main_kink`（comparable 未放松）、注销月折价 `s_cancel_discount`、价差递延 `s_deferral`（同构分支=蝶值超阈值+comparable 不变；**2026-08-12 新增范式一b非同构分支**：共中腿+等月差泛化段对自行枚举，不限连续挂牌月、在 U.butterflies 之外，如 I 1-3 vs 3-5；跨注销数相同但注销状态不同时 E=bias(后段)−bias(前段) 给结构性不等式方向，实际蝶值落 0 或错侧=违背→按结构方向输出(E<0→卖后段买前段)，|蝶值|为保守错价下限；泛化蝶式结构名不在 bfn，`_annotate` 与 `_break_verdict` 按 "a-b-c" 拆两段回查 spn）、注销后首月溢价 `s_post_cancel_premium`(要求 m2 非注销月，否则 LH/B/RR 每月注销品种退化)、远期高低估 `s_forward_mispricing`、近月转抛 `s_near_roll`、远月转抛 `s_far_roll`、同构价差对比 `s_matched_pair`（**2026-08-10 用户重定义,弃两两配对/carry残差口径；2026-08-12 同构放松同 s_kink_repair**(分组键去掉两腿注销属性,非同构须 E 与信号同向)：组内每个 B 取**最近的完全不交叉**前后段为 A/C(2026-08-11 起不再是紧邻段,须 A.far≤B.near≤B.far≤C.near)+等距(每个B最多1条,不做全组合)；B vs 插值(A+C)/2 给方向与估值偏差；跨多个挂牌月的段用 `_adj_chain` 拆相邻月段链、逐位置算子段偏差、**归因到偏差最大的相邻月段**(conditions`归因`)；kind="kink"；score=|偏差|/主力价倒序,无阈值）。**远期/远月定义（用户口径）：前腿(近腿)到期 6 个月(180天)以上**——远期高低估 min_days=180；远月转抛"远月对"=前腿>180天(far_days)，其余为近月对。

**C 类统计（7，依赖历史分位）**：季节性正套 `s_seasonal_long` / 反套 `s_seasonal_short`（条件含**期限斜率+斜率同期分位**；另有**双自变量残差过滤(用户口径)**=`_resid_ctx` 用历史各年版逐日样本(剔当年,按日期与斜率序列对齐)回归 价差~期限斜率+距到期天数(距近腿交割月首日,与 version_series 口径一致)，当前 mid 相对回归预测的残差 z 需朝有利方向偏离：正套 z≤−T、反套 z≥T，PARAMS 键 `resid_z` 默认 1.5；不满足 → `flag=RESID_FLAG("未满足残差过滤")` 降级到报告第四段；**豁免(用户口径)**：近腿实际到期>180天(`far_days`)的远期段不受残差过滤约束直接保留(conditions 注"保留原因")；样本不足(<60点或<3年) → 不拦，conditions 注"残差回归=样本不足"）、价差统计套利 `s_stat_arb`（常规分支有两道硬筛：**顺季节性**=季节性夏普与方向同号；**期限结构历史同期过滤(用户口径)**=斜率分位>90(深度contango)时正套/做多是接结构性飞刀→放弃、<10(深度back)时反套/做空同理→放弃，PARAMS 键 slope_pctl_hi/lo。**2026-08-12 新增远期同期极值分支(免两道筛)**：前腿到期>far_days(默认120,注意非远期口径180——V2701约155天是动机案例) + 同期分位≤peer_lo(10)或≥peer_hi(90) + 同期样本≥peer_min_years(5年) → 直接输出，conditions`分支`="远期同期极值(免季节性/结构过滤,刷波动候选)"，命中后 continue 不再走常规分支；逻辑=时间充裕可等波动/换月催化,极值给安全边际,结构逆风可被覆盖）、蝶式统计 `s_bf_stat`、波动传递 `s_vol_transfer`、注销月近月崩塌 `s_cancel_collapse`（**注销段口径(用户纠正)**：效应段=注销月(近腿)−注销后月，如MA611注销→段=611-612；前一段610-611的抬高只是镜像。仓单高+分位仍高=做空价差（**2026-08-12 做空分支去掉 days≤near_days 临近窗口**——错价现在就存在不必等临近，J 距3月注销200天/Y 距11月80天的提前做空均可触发；做多两分支的天数条件不变），仓单低+临近+分位极低=做多，远期已深度定价=做多。`_annotate` 另给所有价差机会加 conditions`注销段` 标注：远腿注销=结构性偏高勿当错价做空/近腿注销=结构性偏低）、流动性溢价 `s_liq_premium`（主力换月进度；**触发过宽 ~95 条，收紧方案待用户拍板**）。

**品种期限结构 `curve_structure(U, prod)`**（`_annotate` 给每条机会附 `curve`/`slope_pctl` 字段，报告两专列）：按**是否注销月分组**（同特性月份互比，FG/SA 奇数月=注销月），组内 OI 加权回归 close~距到期月数；各组斜率同号且幅度≤3倍 → back/contango，异号 → 分歧；整体斜率=组内去均值合并回归（固定效应）。斜率同期分位走 `_slope_ctx`。

**仓单佐证（2026-08-04）**：`_annotate` 给每条 spread 类机会附 `warrant_pctl`（仓单历年同期分位，`_warrant_pct` 口径）+ conditions`仓单佐证`。判定：低仓单(≤30)支持正套、高仓单(≥70)支持反套 → 顺风/逆风，中间=中性；报告专列"仓单分位%"（顺绿/逆红，`data-wpctl`）。**定位=弱佐证不是信号**——系统化仓单跨期腿已在 Future_CTA 项目证伪（毛Sh仅0.3~0.6、主力滚动19%/日换手地板成本≈alpha 3~5倍，见该项目 analysis/warehouse/warehouse_research.md 跨期价差版一节），残值只放人工筛选层做方向背书。

**stub**：近月回归（未实现）。

## 关键判定与常量

- **可转抛 rollable**：kind=spread + 方向为正套(做多价差/买近卖远) + `can_roll`(区间无注销月) + 未进交割月。
- **流动性判定（2026-08-12 用户重定义,弃单腿OI口径）**：`LIQ_MAX_GAP_RATIO=0.003`——可交易段**合成盘口空隙**(ask−bid,组合合约盘口优先否则双腿合成,`spread_gap()`) / 主力合约最新价 > 0.3% 或盘口缺边 → 钓鱼桶；kink 只看 B 段、蝶式=前后段空隙之和、pair=两段之和；conditions 加`盘口空隙`("N跳/x.xxx%")。弃用原因：远月腿天然低OI会误杀范式二/四目标段(V 1-5、J 3-4 曾因此进不了统计策略)。同口径用于三处：`_annotate` 钓鱼分类、`_hist_ctx` C类总闸门(基准用近月价 abs_price,拿不到U)、screen_report 缓存预热候选。`LIQ_MIN_OI=5000` 仅保留为"最小腿OI"展示列参考，不再过滤。
- `ADVISORY_ONLY = {EC, T, TF, TL, TS}`：集运/国债现金交割，仅提示段不建仓。**股指 IF/IC/IH/IM 不在此列**——可交易/可转抛，正常进主列表。
- `DIVIDEND_PRODUCTS = {IF, IC, IH, IM}`：股指分红逻辑。成分股分红集中 3-9 月，远月贴现分红使近−远价差在该窗口偏大(季节性正常非错价)。价差跨 3-9 月的机会 `conditions` 打 `分红季` 标注。季节性/统计类走同期分位已正确处理；纯 carry 口径(转抛/远期高低估)fullcarry 未扣分红会高估，靠标注提示(精确需接分红点数据)。
- `REGIME_BREAKS = {"JM": [(202701, "交割标准提高")]}`：断层前后历史不可比。**2026-08-12 方向裁决 `_break_verdict`**：跨断层机会不再一律排除——新标准腿(月份≥断层月)**净暴露为多**则保留(conditions 加`规则断层`标注；蝶式 −近+2中−远 中腿=2701 净+1 保留,新腿互相对冲不剔)，净暴露为空则 run_screen 直接剔除(flag="规则断层-剔除(空新标准腿)")，无法判定(交易腿不跨而对比段跨/方向文本不识别)沿用原排除标注进仅提示段。
- 同构对比 `comparable(a,b,cancel_set)`：月差相同且 `cancels_between` 区间注销月数相同才可比（用户案例：PS 08-10 与 10-12 不可比，后者跨 11 月注销）。
- Sharpe 口径（注释39）：逐年同期日收益均值序列，sharpe(d)=sum/std，d=5..60 取最优持有期。

## Provider 注入（避免循环导入）

screener 不 import hist_stats/product_detail，由 screen_report 注入：
`sc.set_hist_provider(...)`（历史价差序列）、`sc.set_warrant_provider(...)`（仓单）、`sc.set_slope_provider(...)`（期限斜率，`_slope_ctx` 返回当前斜率+同期分位）。

## 主流程 screen_report.py

`build_universe(all_pairs=True)` → set providers → `hist_stats.warm_cache`(分块新连接) → `run_screen`(A+C) → CSV(`output/opportunities.csv`) → `archive_snapshot`(增量 parquet) → 命中品种详情图(`product_detail`) → 历史同期页(`hist_stats.ensure_pages`) → `write_report`。全量跑约 20-90 分钟（取决于缓存新鲜度），日志重定向 `screen_report_run.log`。

## 报告页 opportunities.html

- 四段表：主列表 / 钓鱼桶 / 仅提示 / 统计套利·未满足残差过滤(`flag==RESID_FLAG` 单列，不进钓鱼与仅提示段)。行带 `data-prod/data-strat/data-combo/data-roll/data-wpctl/data-sector`(板块下拉,来自配置表 industry_name → meta["industry"])。
- **列序(2026-08-11 用户口径)**：品种|方向|结构|策略|强度|最小腿OI|组合合约|可转抛|买量|买价|卖量|卖价|当前值|目标价|差值|利润率%|期限结构|斜率分位%|仓单分位%|[标注]|触发条件。**可转抛列只对做多方向填是/否,非做多留空**(kink类按B段判)；盘口四列=标的段(spread=本段, kink=B段)取 `Opportunity.book=(bid,bid_vol,ask,ask_vol)`(Contract/Spread 新增 bid_vol/ask_vol,合成=两腿对侧取小)；当前值/目标价/差值/利润率% 取 conditions 的 B、理论B=(A+C)/2、差=理论−实际、比例%(仅ABC类策略有值)。**改列序必须同步 PINJS c[i] 索引**(现 direction=c[1] structure=c[2] score=c[4] combo=c[6] curve=c[16] spctl=c[17])。
- 筛选栏：策略下拉、品种首字母、仅交易所组合、可转抛、**期限结构极端过滤(默认开)**——按行方向(data-dir: L=正套/S=反套,仅spread类)×斜率分位(data-spctl)：正套@分位>P 或 反套@分位<100−P 的行隐藏，P 可输入默认90；状态存 localStorage `oppFilterState`。
- 结构列链接历史同期页，品种列链接 `detail/{PROD}.html`（7 面板 ECharts，见 product_detail.py）。**斜率季节性面板(p7)展示取负(用户口径)**：价差习惯近−远，回归斜率是远−近向，取负后 正=back 负=contango；只改展示层（fetch 里 `-v`），screener/`_slope_ctx`/残差回归仍用原始斜率。
- **选中机会（PINJS）**：模板尾部 `<!--PINJS-->` 标记的自包含 JS 块。每行"选中"按钮存**点击瞬间整行快照**到 localStorage `pinnedOpps`（键=策略|结构|方向），📌 置顶区渲染+移除按钮，页面重生成不影响已选。含「＋自定义新增」表单（strategy=自定义，绿底）。
- **改 PINJS 注意**：块在 f-string 内，JS 大括号必须写 `{{ }}`；JS 正则**避免反斜杠**（用 `[0-9 :-]` 这类写法），否则 Python 转义地狱。改模板后老页面需就地同步（regex 替换 `<!--PINJS-->.*?</script>`）。

## 转抛比例表

`roll_ratio_report.py` 生成 `output/roll_ratio.html` + **`roll_ratio_data.csv`（数据层，重算以它为准）**；`roll_ratio_recompute.py` 不重取行情按 CSV 重算（HTML 解析仅兜底）。列含 盘口宽(跳)、到期天数、可转抛是/否；过滤器含资金利率输入（默认 6%）、last/ask 双口径。
