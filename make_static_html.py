# -*- coding: utf-8 -*-
"""生成静态合成数据的 HTML，方便离线调试前端（不连行情）。
复用 spread_monitor 的 build_table / write_html，确保与实盘版结构一致。
输出: output/spread_monitor_static.html
"""
import os
from types import SimpleNamespace
from datetime import datetime
import spread_monitor as sm


def q(last, tick, oi):
    """构造一个假 Quote：bid/ask 围绕 last，含持仓量(定主力)。"""
    return SimpleNamespace(
        last_price=last,
        bid_price1=round(last - tick, 4), bid_volume1=oi // 50 + 5,
        ask_price1=round(last + tick, 4), ask_volume1=oi // 60 + 4,
        open_interest=oi,
    )


def combo_q(spread, tick):
    """假价差合约盘口(近-远方向)。"""
    return SimpleNamespace(
        last_price=spread,
        bid_price1=round(spread - tick, 4), bid_volume1=120,
        ask_price1=round(spread + tick, 4), ask_volume1=95,
        open_interest=0,
    )


# (交易所, 品种, [(月码, 最新价, 持仓量)...], tick, 转抛, 仓储费, 注销月集合, 是否有价差合约)
SPECS = [
    ("DCE", "m",  [("2509", 3050, 800000), ("2511", 3010, 1200000), ("2601", 2980, 600000), ("2603", 2960, 200000)],
     1, "Y", 0.5, {3, 7, 11}, True),
    ("CZCE", "CF", [("509", 13800, 50000), ("511", 13900, 140000), ("601", 13950, 90000)],
     5, "Y", 0.8, {11}, True),
    ("SHFE", "cu", [("2508", 79200, 90000), ("2509", 79050, 210000), ("2510", 78900, 70000)],
     10, "Y", 0.3, set(), False),
    ("SHFE", "rb", [("2510", 3120, 1500000), ("2601", 3140, 900000), ("2605", 3180, 400000)],
     1, "N", 0.15, set(range(1, 13)), False),
    ("DCE", "p",  [("2509", 8800, 300000), ("2511", 8700, 500000), ("2601", 8650, 220000)],
     2, "N", 0.9, set(range(1, 13)), True),
    ("GFEX", "lc", [("2509", 72000, 180000), ("2511", 73500, 260000), ("2601", 74800, 120000)],
     20, "Y", 5, {3, 7, 11}, False),
    ("GFEX", "pt", [("2512", 320.0, 8000), ("2602", 322.5, 15000), ("2604", 324.0, 5000)],
     0.05, "Y", 0.0018, {8}, False),
    ("CFFEX", "IF", [("2607", 4850, 60000), ("2608", 4808, 40000), ("2609", 4760, 30000)],
     0.2, "Y", None, set(), False),
    ("INE", "sc", [("2508", 510.0, 30000), ("2509", 508.5, 50000), ("2510", 507.0, 20000)],
     0.1, "Y", 0.2, set(), False),
]


def build():
    pairs, quotes, meta = [], {}, {}
    first_combo = [True]   # 给第一个组合故意错价，制造一个正套利(绿)行便于调样式
    for exch, prod, months, tick, roll, fee, cancel, has_comb in SPECS:
        meta[prod.upper()] = {"roll": roll, "fee": fee, "cancel": cancel}
        syms = []
        for mc, last, oi in months:
            sym = f"{exch}.{prod}{mc}"
            quotes[sym] = q(last, tick, oi)
            syms.append((sym, last))
        for i in range(len(syms) - 1):
            (near, n_last), (far, f_last) = syms[i], syms[i + 1]
            comb = None; orient = 0
            if has_comb:
                pfx = "SPD" if exch == "CZCE" else "SP"
                comb = f"{exch}.{pfx} {sm.code_of(near)}&{sm.code_of(far)}"
                spread = round(n_last - f_last, 4)
                cq = combo_q(spread, tick)
                if first_combo[0]:           # 抬高组合买价 -> 卖组合套利 > 0
                    cq.bid_price1 = round(spread + 10 * tick, 4)
                    first_combo[0] = False
                quotes[comb] = cq
                orient = 1
            pairs.append({"exchange": exch, "product": prod, "near": near, "far": far,
                          "comb": comb, "comb_orient": orient})
    return pairs, quotes, meta


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    pairs, quotes, meta = build()
    df = sm.build_table(pairs, quotes, meta)
    out = os.path.join(here, "output"); os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "spread_monitor_static.html")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (静态合成数据)"
    sm.write_html(df, path, ts, refresh_seconds=999999)  # 基本不自动刷新，便于调试
    print(f"行数 {len(df)}  ->  {path}")


if __name__ == "__main__":
    main()
