# -*- coding: utf-8 -*-
"""冒烟测试：连接 -> 发现合约 -> 验证组合解析 -> 取一次快照 -> 退出"""
import os, time
from datetime import datetime
from tqsdk import TqApi, TqAuth
import spread_monitor as sm

cfg = sm.load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"))
print("连接 ...")
api = TqApi(auth=TqAuth(cfg["user"], cfg["password"]))
try:
    # 看看 DCE/CZCE 原始组合符号长什么样
    for ex in ["DCE", "CZCE"]:
        combos = api.query_quotes(ins_class="COMBINE", exchange_id=ex, expired=False) or []
        print(f"\n{ex} COMBINE 数量={len(combos)}，样例:")
        for c in combos[:6]:
            print("   ", repr(c), "-> parse:", sm.parse_combine(c))

    pairs = sm.discover_pairs(api, cfg["exchanges"], cfg["max_months"],
                              cfg["only_adjacent"], cfg["products"])
    with_comb = sum(1 for p in pairs if p["comb"])
    print(f"\n配对总数={len(pairs)}，含交易所价差合约={with_comb}")
    print("前 5 个含组合的配对:")
    shown = 0
    for p in pairs:
        if p["comb"]:
            print("   ", p["exchange"], p["product"], sm.code_of(p["near"]),
                  sm.code_of(p["far"]), "comb=", p["comb"], "orient=", p["comb_orient"])
            shown += 1
            if shown >= 5:
                break

    symbols = sm.collect_symbols(pairs)
    print(f"\n订阅 {len(symbols)} 个合约，等待盘口填充 ...")
    qlist = api.get_quote_list(symbols)
    quotes = {q.instrument_id: q for q in qlist}
    # 等几轮 wait_update 让盘口到位
    deadline = time.time() + 12
    while time.time() < deadline:
        api.wait_update()
    meta = sm.load_product_meta(os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["product_config"]))
    df = sm.build_table(pairs, quotes, meta)
    print("快照行数:", len(df))
    # 打印几行有组合的
    sub = df[df["价差合约"] != ""].head(8)
    import pandas as pd
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 240)
    cols = ["交易所","品种","近月","远月","价差合约",
            "合成买价差","合成买量","合成卖价差","合成卖量",
            "组合买价","组合买量","组合卖价","组合卖量","最优套利"]
    print(sub[cols].to_string(index=False))

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["output_dir"]), exist_ok=True)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["output_dir"])
    sm.write_html(df, os.path.join(out, "spread_monitor.html"), ts, cfg["refresh_seconds"])
    sm.write_excel(df, os.path.join(out, "spread_monitor.xlsx"), ts)
    print("\n已写出 output/spread_monitor.html 和 .xlsx")
finally:
    api.close()
    print("done")
