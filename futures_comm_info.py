# -*- coding: utf-8 -*-
"""维护 futures_comm_info.xlsx：从 9qihuo 抓手续费/保证金，TqSdk 补交易所/tick/乘数/交易时段。
逐合约一行。凭据从同目录 config.ini [auth] 读取。"""

import os
import re
import configparser
from datetime import datetime

import requests
import urllib3
import pandas as pd
from bs4 import BeautifulSoup
from lxml import etree
from tqsdk import TqApi, TqAuth

urllib3.disable_warnings()
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "futures_comm_info.xlsx")


def _auth():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(HERE, "config.ini"), encoding="utf-8")
    return cfg.get("auth", "user", fallback="").strip(), cfg.get("auth", "password", fallback="").strip()


def fetch_futures_comm_info(file=DEFAULT_FILE):
    # 1) 抓 9qihuo 手续费表
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    res = requests.get("https://www.9qihuo.com/qihuoshouxufei", headers={"User-Agent": ua},
                       verify=False, timeout=60)
    text = res.text
    html = etree.HTML(text)
    tr_els = html.xpath('//table[@id="heyuetbl"]/tr')

    data = []
    for tr in tr_els:
        td_els = tr.xpath("td")
        if len(td_els) != 13:
            continue
        data.append(["".join(td.xpath(".//text()")) for td in td_els])
    if not data:
        raise SystemExit("9qihuo 表为空，网页结构可能已变，请检查。")

    df_raw = pd.DataFrame(data=data)
    df_raw.columns = ["合约品种", "现价", "涨/跌停板", "保证金-买开%", "保证金-卖开%", "保证金/每手",
                      "手续费-开仓", "手续费-平昨", "手续费-平今", "每跳毛利/元", "手续费(开+平)",
                      "每跳净利/元", "备注"]

    # 更新时间（正则更稳）
    def grab(label):
        m = re.search(label + r"[：:]\s*([0-9\-]+\s+[0-9:.]+)", text)
        return m.group(1) if m else ""
    comm_update_time = grab("手续费更新时间")
    price_update_time = grab("价格更新时间")
    print(f"9qihuo 手续费更新 {comm_update_time} / 价格更新 {price_update_time}，抓到 {len(df_raw)} 行")

    # 2) TqSdk 补 交易所 / tick / 乘数 / 交易时段
    user, pwd = _auth()
    if not user or not pwd:
        raise SystemExit("config.ini [auth] 缺 user/password")
    api = TqApi(auth=TqAuth(user, pwd))
    try:
        contracts = [x for x in sorted(api.query_quotes(ins_class="FUTURE", expired=False)) if "@" not in x]

        def sym_of(contract):
            # 取合约代码的前导字母作品种代码，兼容3/4位月份与 'F' 等后缀(如 l2607F->l)
            return re.match(r"^[A-Za-z]+", contract.split(".")[1]).group()

        exchange_dict, rep = {}, {}
        for c in contracts:
            s = sym_of(c)
            exchange_dict.setdefault(s, c.split(".")[0])
            rep.setdefault(s, c)            # 每品种取一个代表合约

        qlist = api.get_quote_list(list(rep.values()))
        for _ in range(6):                  # 等静态字段填充
            api.wait_update()
        qd = {q.instrument_id: q for q in qlist}

        ticksize_dict, multiplier_dict, trading_time_dict = {}, {}, {}
        for s, c in rep.items():
            q = qd.get(c)
            ticksize_dict[s] = q.price_tick if q else None
            multiplier_dict[s] = q.volume_multiple if q else None
            tt = getattr(q, "trading_time", None) if q else None
            if tt is not None:
                trading_time_dict[s] = {"day": [list(x) for x in (tt.day or [])],
                                        "night": [list(x) for x in (tt.night or [])]}
            else:
                trading_time_dict[s] = None
    finally:
        api.close()

    # 3) 解析与拼装
    def name(x):
        x = x.replace(" ", "")
        code = x.split("(")[1][:-1]                  # 如 l2607F / ag2406 / CF601
        sym = re.match(r"^[A-Za-z]+", code).group()  # 前导字母作品种代码
        suffix = code[len(sym):]                     # 月份(+后缀)，如 2607F / 2406 / 601
        namepart = x.split("(")[0]
        cn = namepart[:-len(suffix)] if (suffix and namepart.endswith(suffix)) else namepart
        return cn, sym, code

    def limit(x):
        a, b = x.split("/")
        return float(a), float(b)

    def fee(x):
        # "3元" -> 固定3, 费率0 ; "0.5/万分之(...)" -> 固定0, 费率=0.5*1e-4
        if "/" not in x:
            return float(re.sub(r"[^0-9.]", "", x) or 0), 0.0
        return 0.0, float(x.split("/")[0]) * 0.0001

    df = df_raw.copy()
    df["品种名称"], df["品种代码"], df["合约代码"] = zip(*df["合约品种"].apply(name))
    df = df[df["品种代码"].isin(exchange_dict)]    # 丢掉 TqSdk 查不到的(已下市等)
    df["交易所"] = df["品种代码"].map(exchange_dict)
    df["天勤代码"] = df["交易所"] + "." + df["合约代码"]
    df["最新价"] = df["现价"].apply(float)
    df["最新价更新时间"] = price_update_time
    df["涨停"], df["跌停"] = zip(*df["涨/跌停板"].apply(limit))
    df["保证金率"] = df["保证金-买开%"].apply(lambda x: float(x.strip("%")) * 0.01)
    df["最小价差"] = df["品种代码"].map(ticksize_dict)
    df["合约乘数"] = df["品种代码"].map(multiplier_dict)
    df["交易时段"] = df["品种代码"].map(trading_time_dict)
    df["手续费-开仓固定"], df["手续费-开仓费率"] = zip(*df["手续费-开仓"].apply(fee))
    df["手续费-平仓固定"], df["手续费-平仓费率"] = zip(*df["手续费-平昨"].apply(fee))
    df["手续费-平今固定"], df["手续费-平今费率"] = zip(*df["手续费-平今"].apply(fee))
    df["手续费更新时间"] = comm_update_time

    df = df[df.columns[-20:]]
    df["交易时段"] = df["交易时段"].astype(str)     # dict -> 字符串入表
    df.to_excel(file, index=False)
    print(f"已写 {file}，共 {len(df)} 个合约")
    return df


def check_and_update_futures_common_info_daily(file=DEFAULT_FILE):
    if os.path.exists(file):
        t1 = datetime.fromtimestamp(os.path.getmtime(file)).date()
        print(f"(futures_comm_info.xlsx 最后修改于){t1}  今天{datetime.now().date()}")
        if t1 == datetime.now().date():
            print("futures_comm_info is up to date"); return
    print("futures_comm_info is updating ...")
    fetch_futures_comm_info(file=file)


def read_futures_info():
    comm = pd.read_excel(os.path.join(HERE, "futures_comm_info.xlsx")).set_index("合约代码").to_dict(orient="index")
    rebate_path = os.path.join(HERE, "futures_rebate.xlsx")
    rebate = pd.read_excel(rebate_path).set_index("代码").to_dict(orient="index") if os.path.exists(rebate_path) else {}
    return comm, rebate


if __name__ == "__main__":
    fetch_futures_comm_info()
