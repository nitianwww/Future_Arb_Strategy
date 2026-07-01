# -*- coding: utf-8 -*-
"""
价差网格做市/均值回归策略（跑在组合合约上，架于 spread_trader 之上）。

规则（已与用户确认）：
- 目标仓位网格：以合理价为中枢，价差每偏离 ticks_per_lot 跳加 lot 手，封顶 max_pos；double 时对称做空价差。
    目标仓位 = clip( floor((合理价−spread)/(ticks_per_lot×tick)) × lot , ±max_pos )
- 无底仓，仓位完全由目标仓位算法驱动。
- 盈利间隔(跳) = ceil(价差往返手续费 / (tick×乘数))；往返手续费 = 近开+远开+近平今+远平今 (两腿×开平)。
    手续费取自 futures_comm_info.xlsx（含固定/费率、平今免收）。
- 定价：排队(挂best被动) vs 插队(超best一跳)。若插队后潜在盈利 ≥ 2×往返手续费 → 插队，否则排队。
- 算 spread 要除掉自己的挂单(净化盘口)。
- 默认 dry-run（真行情模拟成交、不真发）。

用法：
  python grid_strategy.py --near DCE.m2609 --far DCE.m2611          # dry-run
  python grid_strategy.py --symbol "DCE.SP m2609&m2611"
  加 --live 走实盘（未实测，谨慎）。
"""

import os
import math
import time
import argparse
from datetime import datetime

import pandas as pd
from tqsdk import TqApi, TqAuth

import spread_monitor as sm
import spread_trader as st

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 逐品种网格配置（占位；后续对接 strategy_config.xlsx 等）
# --------------------------------------------------------------------------- #
DEFAULT_GRID = dict(fair=None, max_pos=5, lot=1, ticks_per_lot=2, two_way=True,
                    profit_ticks=None,          # None=自动算
                    poll=1.0, reprice_ticks=1)  # 轮询秒、盘口移动>=几跳则改价

GRID_CONFIG = {
    # 品种代码(大写) -> 覆盖项。fair 必填(手工设定)。示例：
    # "M": dict(fair=-10, max_pos=5, lot=1, ticks_per_lot=2, two_way=True),
}


def grid_params(product):
    p = dict(DEFAULT_GRID)
    p.update(GRID_CONFIG.get(product.upper(), {}))
    return p


# --------------------------------------------------------------------------- #
# 手续费 -> 价差往返手续费 -> 盈利间隔
# --------------------------------------------------------------------------- #
def load_comm_row(product):
    path = os.path.join(HERE, "futures_comm_info.xlsx")
    if not os.path.exists(path):
        return None
    df = pd.read_excel(path)
    df["P"] = df["品种代码"].astype(str).str.upper()
    r = df[df["P"] == product.upper()]
    return r.iloc[0] if len(r) else None


def round_trip_fee(comm_row, price, mult):
    """价差往返手续费/手 = 2×开 + 2×平今；固定+费率(费率按 价格×乘数)。缺失则返回 None。"""
    if comm_row is None:
        return None
    def one(fixed_col, rate_col):
        return float(comm_row[fixed_col]) + float(comm_row[rate_col]) * price * mult
    open_fee = one("手续费-开仓固定", "手续费-开仓费率")
    close_fee = one("手续费-平今固定", "手续费-平今费率")
    return 2 * open_fee + 2 * close_fee


def profit_ticks(rt_fee, tick, mult):
    per_tick = tick * mult
    if not per_tick:
        return 1
    return max(1, math.ceil((rt_fee or 0) / per_tick))


# --------------------------------------------------------------------------- #
# 网格目标仓位
# --------------------------------------------------------------------------- #
def grid_target(spread, p, tick):
    """正=价差多头(买近卖远)，负=价差空头。"""
    if spread is None or p["fair"] is None:
        return 0
    step = p["ticks_per_lot"] * tick
    if step <= 0:
        return 0
    dev = p["fair"] - spread                 # >0: 价差低于合理价 -> 做多价差
    if dev >= 0:
        lots = int(dev // step) * p["lot"]
    else:
        if not p["two_way"]:
            return 0
        lots = -(int((-dev) // step) * p["lot"])
    return max(-p["max_pos"], min(p["max_pos"], lots))


# --------------------------------------------------------------------------- #
# 网格策略（单组合，单活动单）
# --------------------------------------------------------------------------- #
class GridSpreadStrategy:
    def __init__(self, api, combo, orient, product, mult, tick, p, comm_row, dry_run=True):
        self.api, self.combo, self.orient = api, combo, orient
        self.product, self.mult, self.tick, self.p = product, mult, tick, p
        self.comm_row, self.dry_run = comm_row, dry_run
        self.q = api.get_quote(combo)
        self.position = 0            # 当前价差净持仓(手)，正=多价差
        self.active = None           # {'dir':'BUY/SELL','price':x,'lot':n,'mode':'排队/插队','order':obj}
        self._last_log = 0

    # 净化盘口：除掉自身挂单
    def clean_book(self):
        q = self.q
        bid, bidv = sm.clean(q.bid_price1), sm.clean(q.bid_volume1)
        ask, askv = sm.clean(q.ask_price1), sm.clean(q.ask_volume1)
        a = self.active
        if a:
            lvl2 = lambda name: sm.clean(getattr(q, name, None))
            if a["dir"] == "BUY" and bid is not None and a["price"] == bid:
                if bidv is not None and bidv - a["lot"] <= 0:
                    bid = lvl2("bid_price2") or (bid - self.tick)
            if a["dir"] == "SELL" and ask is not None and a["price"] == ask:
                if askv is not None and askv - a["lot"] <= 0:
                    ask = lvl2("ask_price2") or (ask + self.tick)
        return bid, ask

    def spread_ref(self, bid, ask):
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return sm.clean(self.q.last_price)

    def rt_fee_and_pt(self):
        leg_price = sm.clean(self.q.last_price) or 0
        rt = round_trip_fee(self.comm_row, abs(leg_price) or 3000, self.mult)
        pt = self.p["profit_ticks"] or profit_ticks(rt, self.tick, self.mult)
        return rt, pt

    def decide_price(self, side, bid, ask, rt_fee):
        """返回 (price, mode)。side=BUY 建/加多价差; SELL 减/平/做空。"""
        fair = self.p["fair"]
        if side == "BUY":
            queue, jump = bid, (bid + self.tick if bid is not None else None)
            # 插队后潜在盈利(回到合理价) ≥ 2×费用 → 插队
            if jump is not None and (fair - jump) * self.mult >= 2 * (rt_fee or 0):
                return jump, "插队"
            return queue, "排队"
        else:
            queue, jump = ask, (ask - self.tick if ask is not None else None)
            if jump is not None and (jump - fair) * self.mult >= 2 * (rt_fee or 0):
                return jump, "插队"
            return queue, "排队"

    # dry-run：市价穿越我方挂单价即成交
    def _dry_fill(self, bid, ask):
        a = self.active
        if not a:
            return False
        if a["dir"] == "BUY" and ask is not None and ask <= a["price"]:
            return True
        if a["dir"] == "SELL" and bid is not None and bid >= a["price"]:
            return True
        return False

    def _place(self, side, lot, price, mode):
        log(f"下单[{mode}] {side} {lot}手 @ {price}  (持仓{self.position}->目标)")
        order = None
        if not self.dry_run:
            order = self.api.insert_order(symbol=self.combo, direction=side,
                                          offset="OPEN" if self._is_open(side) else "CLOSE",
                                          volume=lot, limit_price=price)
        self.active = dict(dir=side, price=price, lot=lot, mode=mode, order=order)

    def _is_open(self, side):
        # 加大 |持仓| 为开仓，缩小为平仓
        if side == "BUY":
            return self.position >= 0
        return self.position <= 0

    def _cancel(self):
        if self.active and self.active.get("order") is not None and not self.dry_run:
            self.api.cancel_order(self.active["order"])
        self.active = None

    def step(self):
        bid, ask = self.clean_book()
        spread = self.spread_ref(bid, ask)
        if spread is None:
            return
        rt_fee, pt = self.rt_fee_and_pt()
        target = grid_target(spread, self.p, self.tick)

        # 1) 成交检测
        if self.active is not None:
            filled = self._order_filled(bid, ask)
            if filled:
                signed = self.active["lot"] if self.active["dir"] == "BUY" else -self.active["lot"]
                self.position += signed
                log(f"成交{'(模拟)' if self.dry_run else ''} {self.active['dir']} {self.active['lot']}手 @ {self.active['price']} → 持仓 {self.position}")
                self.active = None

        # 2) 目标 vs 持仓
        delta = target - self.position
        self._log_state(spread, target, pt, delta, bid, ask)

        if delta == 0:
            if self.active:                          # 已到位，撤掉多余挂单
                self._cancel()
            return

        side = "BUY" if delta > 0 else "SELL"
        opening = self._is_open(side)
        # 开仓需边际盖过盈利间隔；平仓(缩仓)无条件允许
        if opening:
            dev_ticks = abs(self.p["fair"] - spread) / self.tick
            if dev_ticks < pt:
                if self.active:
                    self._cancel()
                return

        price, mode = self.decide_price(side, bid, ask, rt_fee)
        if price is None:
            return
        # 已有相同方向挂单：盘口移动超过阈值才改价
        if self.active and self.active["dir"] == side:
            if abs(self.active["price"] - price) >= self.p["reprice_ticks"] * self.tick:
                self._cancel(); self._place(side, self.p["lot"], price, mode)
        elif self.active and self.active["dir"] != side:
            self._cancel(); self._place(side, self.p["lot"], price, mode)
        elif not self.active:
            self._place(side, self.p["lot"], price, mode)

    def _order_filled(self, bid, ask):
        if self.dry_run:
            return self._dry_fill(bid, ask)
        o = self.active.get("order")
        return o is not None and o.status == "FINISHED" and o.volume_left == 0

    def _log_state(self, spread, target, pt, delta, bid, ask):
        t = time.time()
        if t - self._last_log < 2:
            return
        self._last_log = t
        log(f"净盘口 买{bid}/卖{ask}  spread≈{spread:.2f}  合理价{self.p['fair']}  盈利间隔{pt}跳  "
            f"目标{target} 持仓{self.position} 差{delta}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(near=None, far=None, symbol=None, dry_run=True, minutes=None):
    cfg = st.load_trade_config()
    api = st.build_api(cfg, dry_run)
    try:
        if symbol:
            combo, orient = symbol, +1
            parsed = sm.parse_combine(symbol)
            near = parsed[2] if parsed else near
        else:
            if not (near and far):
                raise SystemExit("需 --near --far 或 --symbol")
            combo, orient = st.resolve_combo(api, near, far)
            if not combo:
                raise SystemExit(f"未找到 {near}&{far} 的组合合约")
        product = sm.product_of(near)
        info = api.query_symbol_info(near)
        mult = float(info.iloc[0]["volume_multiple"]); tick = float(info.iloc[0]["price_tick"])
        p = grid_params(product)
        if p["fair"] is None:
            raise SystemExit(f"品种 {product.upper()} 未配置 fair(合理价)，请在 GRID_CONFIG 里设置")
        comm = load_comm_row(product)
        log(f"网格启动 组合={combo} 品种={product.upper()} 乘数={mult} tick={tick} 参数={p} "
            f"({'DRY-RUN' if dry_run else '实盘'})")
        strat = GridSpreadStrategy(api, combo, orient, product, mult, tick, p, comm, dry_run)

        end = time.time() + minutes * 60 if minutes else None
        while True:
            api.wait_update(deadline=time.time() + p["poll"])
            strat.step()
            if end and time.time() > end:
                log("到达运行时长，退出。"); break
    except KeyboardInterrupt:
        log("已停止。")
    finally:
        api.close()


def main():
    ap = argparse.ArgumentParser(description="价差网格策略（默认 dry-run）")
    ap.add_argument("--near"); ap.add_argument("--far"); ap.add_argument("--symbol")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--minutes", type=float, default=None, help="运行分钟数(默认一直跑)")
    a = ap.parse_args()
    run(near=a.near, far=a.far, symbol=a.symbol, dry_run=not a.live, minutes=a.minutes)


if __name__ == "__main__":
    main()
