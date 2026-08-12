# -*- coding: utf-8 -*-
"""不重新取行情：读上次 output/roll_ratio.html 快照，按正确的可转抛规则
(区间[近月,远月)无注销月) 重新筛选并重写 HTML。"""
import os
import re
import html
from datetime import datetime

import roll_ratio_report as rr
import spread_monitor as sm

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "output", "roll_ratio.html")
SRC_CSV = os.path.join(HERE, "output", "roll_ratio_data.csv")


def parse_csv(path):
    """优先数据源：report 落的 CSV(含隐藏字段)。返回 (rows, had_decay)。"""
    import csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        first = next(rd)                      # 元信息行 #ts=...
        header = next(rd) if first and str(first[0]).startswith("#") else first
        rows = [dict(zip(header, line)) for line in rd if len(line) == len(header)]
    return rows, ("时间贴水" in header)


def parse_rows(path):
    """从缓存 HTML 解析行；列名取自表头(兼容历次格式)。返回 (rows, had_decay)。"""
    h = open(path, encoding="utf-8").read()
    thead = h.split("<thead>")[1].split("</thead>")[0]
    cols = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
            for t in re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)]
    had_decay = "时间贴水" in cols
    tbody = h.split("<tbody>")[1].split("</tbody>")[0]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody, re.S):   # 兼容带 data 属性的行
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) != len(cols):
            continue
        row = dict(zip(cols, cells))
        row.pop("注销状态", None); row.pop("主力腿", None)   # 废弃列
        row["可转抛"] = "是"
        rows.append(row)
    return rows, had_decay


def to_num(row):
    for k in ["近月到期天数", "月差跨度(月)", "最新价差", "盘口宽(跳)", "fullcarry", "转抛比例"]:
        s = str(row.get(k, "")).strip()
        try:
            row[k] = float(s)
            if row[k].is_integer() and k in ("近月到期天数", "月差跨度(月)"):
                row[k] = int(row[k])
        except ValueError:
            pass
    return row


def main():
    if not os.path.exists(SRC):
        raise SystemExit("未找到上次快照 output/roll_ratio.html，需先跑一次 roll_ratio_report.py")
    meta = sm.load_product_meta(os.path.join(HERE, "all_product_config20260629.xlsx"))
    if os.path.exists(SRC_CSV):
        raw, had_decay = parse_csv(SRC_CSV)
        print(f"从数据层 CSV 读取 {len(raw)} 行 (含时间贴水列: {had_decay})")
    else:
        raw, had_decay = parse_rows(SRC)
        print(f"[回退] 从 HTML 解析 {len(raw)} 行 (含时间贴水列: {had_decay})")

    kept = []
    for row in raw:
        prod = str(row["品种"]).upper()
        try:
            nc, fc = row["月差"].split("-")
            nm, fm = sm.month_key(nc), sm.month_key(fc)
        except Exception:
            continue
        if rr.in_delivery_month(nm, prod):        # 近月已进交割月(股指除外) -> 排除
            continue
        cancel = meta.get(prod, {}).get("cancel", set())
        if not rr.can_roll(nm, fm, cancel):
            continue
        row = to_num(row)
        fc_pos = abs(row["fullcarry"]) if isinstance(row.get("fullcarry"), (int, float)) else None
        if not fc_pos:
            continue
        row["fullcarry"] = -fc_pos                # 显示为负(持有成本)，幂等
        # 解析价差买/卖，clamp 最新价差（修正之前漏掉的限制）
        bid = ask = None
        try:
            b_s, a_s = str(row.get("价差买/卖", "")).split("/")
            bid = float(b_s) if b_s not in ("", "None") else None
            ask = float(a_s) if a_s not in ("", "None") else None
        except Exception:
            pass
        last = row.get("最新价差")
        if isinstance(last, (int, float)) and bid is not None and ask is not None:
            lo, hi = min(bid, ask), max(bid, ask)
            last = round(min(max(last, lo), hi), 6)
        row["最新价差"] = last
        # 重建成本项：仓储(利率无关) + 时间贴水(重算) + 资金(基准6%)
        pm = meta.get(prod, {})
        fee = pm.get("fee") or 0.0
        months = row.get("月差跨度(月)") or 0
        stor = fee * 30 * (months if isinstance(months, (int, float)) else 0)
        dec = sm.decay_cost(nm, fm, pm.get("decay"))
        cached_dec = 0.0
        if had_decay:                                    # 扣掉缓存里已含的旧贴水，再加新算值
            try:
                cached_dec = abs(float(str(row.get("时间贴水", "")).strip() or 0))
            except ValueError:
                cached_dec = 0.0
        fc_base = fc_pos - cached_dec
        cap = max(fc_base - stor, 0.0)
        fc_all = fc_base + dec
        row["fullcarry"] = round(-fc_all, 2)
        row["时间贴水"] = (round(-dec, 2) if dec else "")
        if isinstance(last, (int, float)) and fc_all:
            row["转抛比例"] = round(last / (-fc_all), 4)
        row["_last"], row["_ask"] = last, ask
        row["_cap"], row["_stor"], row["_dec"] = round(cap, 4), round(stor, 4), round(dec, 4)
        kept.append(row)

    kept.sort(key=lambda r: r["转抛比例"] if isinstance(r["转抛比例"], (int, float)) else -1e18,
              reverse=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "（基于上次快照·修正可转抛规则重算）"
    rr.write_html(kept, SRC, ts, 6.0)
    rr.write_csv(kept, SRC_CSV, ts, 6.0)
    print(f"修正后可转抛月差 {len(kept)} 条（区间无注销月）-> {SRC} (+CSV)")
    for r in kept[:15]:
        print(f"  {r['品种']:>4} {r['月差']:<14} 到期{r['近月到期天数']}天 价差{r['最新价差']} "
              f"fullcarry{r['fullcarry']} 转抛比例{r['转抛比例']}")


if __name__ == "__main__":
    main()
