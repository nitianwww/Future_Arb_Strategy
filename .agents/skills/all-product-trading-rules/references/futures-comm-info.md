# futures_comm_info 表的维护与定位

逐**合约**的手续费/保证金表，由 `futures_comm_info.py` 生成。与 `all_product_config`(逐**品种**)互补，不冲突。

## 是什么
- 文件：`futures_comm_info.xlsx`（每个未到期合约一行，~800 行）。
- 数据源：手续费/保证金从 **9qihuo**(www.9qihuo.com/qihuoshouxufei) 抓；交易所/tick/乘数/交易时段从 **TqSdk** 补。
- 配套：`futures_rebate.xlsx`(返佣，按"代码"索引)；`read_futures_info()` 同时读这两张。

## 列(20)
品种名称, 品种代码, 合约代码, 交易所, 天勤代码, 最新价, 最新价更新时间, 涨停, 跌停, 保证金率,
最小价差, 合约乘数, 交易时段, 手续费-开仓固定, 手续费-开仓费率, 手续费-平仓固定, 手续费-平仓费率,
手续费-平今固定, 手续费-平今费率, 手续费更新时间
- 手续费两种计法：**固定**(元/手，如 3元→固定3、费率0) 或 **费率**(按金额万分比，如 "0.5/万分之"→费率 0.5×1e-4、固定0)。
- 索引键：`合约代码`(如 ag2406)。

## 怎么更新
```bash
python futures_comm_info.py        # 抓 9qihuo + TqSdk，覆盖写 futures_comm_info.xlsx
```
- 凭据从 `config.ini [auth]` 读（不要再硬编码）。
- 跑前建议备份旧表（`futures_comm_info_backup_<date>.xlsx`）。
- `check_and_update_futures_common_info_daily()` 会按文件修改日期判断当天是否已更新。

## 维护要点 / 已踩过的坑
1. **TqSdk 静态字段(price_tick/volume_multiple)需 wait_update 后才有值** → 用 `get_quote_list` 订阅代表合约后 `wait_update` 几轮再读，否则全 NaN。
2. **trading_time 是 TradingTime 对象**(不是 dict) → 取 `.day/.night` 转成 `{"day":[[..]],"night":[[..]]}` 再入表。
3. **品种代码解析**：用合约代码的**前导字母** `re.match(r"^[A-Za-z]+", code)`，兼容 3/4 位月份和 **'F' 后缀合约**(DCE 塑料新标准品 l2607F/pp2607F/v2607F；旧的按"末位是否数字"切片会错切成 l2/pp2/v2)。
4. 更新时间用正则从页面文本抓 `手续费更新时间：` / `价格更新时间：`，比 DOM .previous 稳。
5. 9qihuo 网页：表 `id="heyuetbl"`、数据行 13 列；`requests` 需 `verify=False`(自签证书) + 常规 UA。

## 与 all_product_config 的关系
- **定位不同**：comm = 逐合约 + 手续费/保证金；all_product = 逐品种 + 交易规则(仓储费/注销月/转抛等)。
- **重叠字段**(交易所/最小价差 tick/合约乘数)两表都源自 TqSdk，**应完全一致**；核对脚本(按品种代码大写聚合后比对)实测 **0 冲突**。
- **覆盖差异**(非冲突)：
  - comm 含 **F 标准品合约**(l/pp/v 的 ...F)，all_product 不单列。
  - all_product 含但 comm 抓不到的冷门停盘品种：**JR 粳稻、LR 晚籼稻、PM 普麦、RI 早籼稻、WH 强麦、ZC 动力煤**(9qihuo 不挂这些不活跃合约的手续费/行情)。
- 核对方法：comm 按 `品种代码.upper()` 去重，与 all_product 的 `product.upper()` 比 交易所/tick/乘数，并列出只在一边的品种。
