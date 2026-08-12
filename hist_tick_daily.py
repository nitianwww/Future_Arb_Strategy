# -*- coding: utf-8 -*-
"""
每日 tick 下载（TqSdk 专业版 DataDownloader）。

口径（与历史数据方案一致）：
  - 组合合约(同品种 SP/SPD)：全量 tick 保留（独家稀缺、量小）
  - 腿(outright)：tick 下载后降采样成 1 分钟 bid/ask bar，原始不留
存储（数据根目录默认在同步夹之外，防网盘无限同步）：
  {root}/ticks_combo/{YYYYMMDD}/part_*.parquet   组合 tick（长表，含 symbol 列）
  {root}/book1m/{YYYYMMDD}/part_*.parquet        腿 1 分钟盘口 bar
  {root}/_meta_daily.json                        每日完成状态 + 已完成符号（断点续跑）
用法：
  python hist_tick_daily.py                      # 自动：补齐最近 lookback 个交易日中缺失的
  python hist_tick_daily.py --date 20260703      # 指定某交易日
  python hist_tick_daily.py --backfill 10        # 检查最近10个工作日，缺哪天补哪天
  python hist_tick_daily.py --products m,CF      # 限品种(调试用)
可挂 Windows 计划任务（每交易日 15:40）：
  schtasks /create /tn tick_daily /tr "python I:\\BaiduSyncdisk\\claude套利\\hist_tick_daily.py" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:40
交易日窗口 = 上一工作日 20:50 → 当日 15:30（夜盘归属当日；节假日空窗口无害，
周中节假日的夜盘缺口按交易所惯例节前无夜盘，误差可接受）。
"""

import os
import json
import time
import glob
import argparse
import configparser
from datetime import datetime, date, timedelta

import pandas as pd
from tqsdk import TqApi, TqAuth
from tqsdk.tools import DataDownloader

import spread_monitor as sm

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = 6                 # 并发下载任务数(共用一条ws)
BATCH_TIMEOUT = 180       # 单批超时(秒)
MAX_MONTHS = 8            # 每品种取最近N个月的腿


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_cfg():
    cp = configparser.ConfigParser()
    cp.read(os.path.join(HERE, "config.ini"), encoding="utf-8")
    root = cp.get("data", "root", fallback=r"I:\futures_data").strip()
    if "BaiduSyncdisk" in root:
        log(f"[警告] 数据根目录 {root} 在网盘同步夹内，会被无限同步！建议移出。")
    return dict(
        user=cp.get("auth", "user", fallback="").strip(),
        password=cp.get("auth", "password", fallback="").strip(),
        root=root,
    )


# --------------------------------------------------------------------------- #
# 交易日与窗口
# --------------------------------------------------------------------------- #
def prev_weekday(d: date) -> date:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def recent_weekdays(n: int, end: date) -> list:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def window_of(trade_date: date):
    p = prev_weekday(trade_date)
    return (datetime(p.year, p.month, p.day, 20, 50),
            datetime(trade_date.year, trade_date.month, trade_date.day, 15, 30))


def default_trade_date() -> date:
    now = datetime.now()
    d = now.date()
    if d.weekday() >= 5:                       # 周末 -> 上一工作日
        return prev_weekday(d + timedelta(days=1))
    if now.hour < 15 or (now.hour == 15 and now.minute < 35):
        return prev_weekday(d)                 # 当日未收盘 -> 下上一交易日
    return d


# --------------------------------------------------------------------------- #
# 符号清单（含过期，按日期过滤月份仍存续的）
# --------------------------------------------------------------------------- #
def build_symbols(api, trade_date: date, products_filter=None):
    ym = trade_date.year * 100 + trade_date.month
    pf = set(p.strip().upper() for p in products_filter) if products_filter else None

    legs_by_prod = {}
    for expired in (False, True):
        for ex in sm.FUTURE_EXCHANGES:
            for s in api.query_quotes(ins_class="FUTURE", exchange_id=ex, expired=expired) or []:
                if sm.is_settle_f(s):
                    continue                           # F结算价合约不下载
                prod = sm.product_of(s).upper()
                if pf and prod not in pf:
                    continue
                if sm.month_key(s) >= ym:              # 该日仍未到期(按月近似)
                    legs_by_prod.setdefault(prod, set()).add(s)

    legs = []
    for prod, ss in legs_by_prod.items():
        legs += sorted(ss, key=sm.month_key)[:MAX_MONTHS]
    leg_set = set(legs)

    combos = []
    for expired in (False, True):
        for ex in sm.COMBINE_EXCHANGES + (["GFEX"] if "GFEX" not in sm.COMBINE_EXCHANGES else []):
            for c in api.query_quotes(ins_class="COMBINE", exchange_id=ex, expired=expired) or []:
                parsed = sm.parse_combine(c)
                if not parsed:
                    continue
                _e, _p, f1, f2 = parsed
                if f1 in leg_set and f2 in leg_set:
                    combos.append(c)
    combos = sorted(set(combos))
    return legs, combos


# --------------------------------------------------------------------------- #
# 下载与转换
# --------------------------------------------------------------------------- #
TICK_COLS = ["last_price", "bid_price1", "bid_volume1", "ask_price1", "ask_volume1",
             "volume", "open_interest"]


def _read_tick_csv(fn, symbol):
    if not os.path.exists(fn) or os.path.getsize(fn) < 40:
        return None
    df = pd.read_csv(fn)
    if df.empty:
        return None
    ren = {f"{symbol}.{c}": c for c in TICK_COLS}
    df = df.rename(columns=ren)
    keep = ["datetime"] + [c for c in TICK_COLS if c in df.columns]
    df = df[keep].copy()
    df["symbol"] = symbol
    return df


def to_book1m(df):
    """tick -> 1分钟盘口bar：取每分钟末的 bid/ask/量/last，volume/oi 取末值。"""
    df = df.copy()
    df["minute"] = pd.to_datetime(df["datetime"]).dt.floor("min")
    g = df.groupby(["symbol", "minute"], as_index=False).last()
    g = g.drop(columns=["datetime"]).rename(columns={"minute": "datetime"})
    return g[["symbol", "datetime"] + [c for c in TICK_COLS if c in g.columns]]


def run_batch(api, tasks, tmp_dir):
    """tasks: [(symbol, start, end)] -> {symbol: DataFrame|None}"""
    dls = []
    for i, (sym, s, e) in enumerate(tasks):
        fn = os.path.join(tmp_dir, f"dl_{i}.csv")
        try:
            dls.append((sym, fn, DataDownloader(api, symbol_list=[sym], dur_sec=0,
                                                start_dt=s, end_dt=e, csv_file_name=fn)))
        except Exception as ex:
            log(f"  建任务失败 {sym}: {ex}")
    t0 = time.time()
    while time.time() - t0 < BATCH_TIMEOUT:
        api.wait_update(deadline=time.time() + 1)
        if all(d.is_finished() for _, _, d in dls):
            break
    out = {}
    for sym, fn, d in dls:
        out[sym] = _read_tick_csv(fn, sym) if d.is_finished() else None
        try:
            os.remove(fn)
        except OSError:
            pass
    return out


def download_day(api, cfg, trade_date: date, products_filter=None):
    ds = trade_date.strftime("%Y%m%d")
    root = cfg["root"]
    combo_dir = os.path.join(root, "ticks_combo", ds)
    book_dir = os.path.join(root, "book1m", ds)
    tmp_dir = os.path.join(root, "_tmp")
    for p in (combo_dir, book_dir, tmp_dir):
        os.makedirs(p, exist_ok=True)
    meta_path = os.path.join(root, "_meta_daily.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            meta = {}
    dm = meta.setdefault(ds, {"done": False, "done_symbols": []})
    done = set(dm.get("done_symbols", []))

    legs, combos = build_symbols(api, trade_date, products_filter)
    log(f"{ds} 符号清单: 腿 {len(legs)}  组合 {len(combos)}  (已完成 {len(done)})")
    s_dt, e_dt = window_of(trade_date)

    todo = [(sym, "combo") for sym in combos if sym not in done] + \
           [(sym, "leg") for sym in legs if sym not in done]
    part_idx = len(glob.glob(os.path.join(combo_dir, "part_*.parquet"))) + \
               len(glob.glob(os.path.join(book_dir, "part_*.parquet")))
    n_rows_c = n_rows_b = 0

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        res = run_batch(api, [(sym, s_dt, e_dt) for sym, _ in batch], tmp_dir)
        combo_frames, book_frames, ok_syms = [], [], []
        for sym, kind in batch:
            df = res.get(sym)
            if df is None:
                if sym in res:              # 完成但无数据(冷合约/停牌) 也算完成
                    ok_syms.append(sym)
                continue
            ok_syms.append(sym)
            if kind == "combo":
                combo_frames.append(df)
            else:
                book_frames.append(to_book1m(df))
        part_idx += 1
        if combo_frames:
            dfc = pd.concat(combo_frames, ignore_index=True)
            dfc.to_parquet(os.path.join(combo_dir, f"part_{part_idx:04d}.parquet"))
            n_rows_c += len(dfc)
        if book_frames:
            dfb = pd.concat(book_frames, ignore_index=True)
            dfb.to_parquet(os.path.join(book_dir, f"part_{part_idx:04d}.parquet"))
            n_rows_b += len(dfb)
        done.update(ok_syms)
        dm["done_symbols"] = sorted(done)
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
        log(f"  批 {i//BATCH+1}/{(len(todo)-1)//BATCH+1}  组合tick+{n_rows_c}  腿1m+{n_rows_b}  完成{len(done)}/{len(legs)+len(combos)}")

    # 带品种过滤时不标记整日完成(否则全市场补跑会误跳过)
    dm["done"] = (products_filter is None) and len(done) >= len(legs) + len(combos)
    dm["legs"], dm["combos"] = len(legs), len(combos)
    dm["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
    log(f"{ds} 完成={dm['done']}  组合tick {n_rows_c} 行  腿1m {n_rows_b} 行")
    return dm["done"]


def missing_dates(cfg, lookback: int):
    meta_path = os.path.join(cfg["root"], "_meta_daily.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            pass
    days = recent_weekdays(lookback, default_trade_date())
    return [d for d in days if not meta.get(d.strftime("%Y%m%d"), {}).get("done")]


def main():
    ap = argparse.ArgumentParser(description="每日 tick 下载(组合全量 + 腿1分钟bar)")
    ap.add_argument("--date", help="指定交易日 YYYYMMDD")
    ap.add_argument("--backfill", type=int, default=3, help="自动模式回看N个工作日补缺(默认3)")
    ap.add_argument("--products", help="限品种,逗号分隔(调试)")
    a = ap.parse_args()

    cfg = load_cfg()
    pf = [x for x in (a.products or "").split(",") if x.strip()] or None
    if a.date:
        dates = [datetime.strptime(a.date, "%Y%m%d").date()]
    else:
        dates = missing_dates(cfg, a.backfill)
        if not dates:
            log("最近交易日均已完成，无需下载。")
            return
        log(f"待补日期: {[d.strftime('%Y%m%d') for d in dates]}")

    api = TqApi(auth=TqAuth(cfg["user"], cfg["password"]))
    try:
        for d in dates:
            download_day(api, cfg, d, pf)
    finally:
        api.close()


if __name__ == "__main__":
    main()
