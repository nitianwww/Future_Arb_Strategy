# -*- coding: utf-8 -*-
"""
跨期价差下单执行引擎（交易所官方组合合约）。

- 标的：交易所跨期组合合约 DCE.SP / CZCE.SPD / GFEX.SP（单条下单，交易所撮合，无腿腿风险）。
- 组合下单仅实盘支持，模拟不支持 → 开发用 dry-run（连真行情，只模拟成交、不真发单）。
- 策略：被动到价单 —— 挂在目标价差等成交；超时或价差走坏则撤单。方向统一 近月−远月。
- 触发：手动指令式(CLI/函数)，并预留 place_passive_spread() 供自动盯盘(第2部分)调用。

用法示例(dry-run 默认)：
  python spread_trader.py --near DCE.m2509 --far DCE.m2511 --side 买价差 --vol 2 --price -40
  加 --live 走实盘(需 config.ini [trade] 填期货公司账户)。
  或直接给组合： python spread_trader.py --symbol "DCE.SP m2509&m2511" --side BUY --vol 1 --price -40
"""

import os
import sys
import time
import math
import argparse
import configparser
from datetime import datetime

from tqsdk import TqApi, TqAuth, TqAccount
import spread_monitor as sm

HERE = os.path.dirname(os.path.abspath(__file__))


def now():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 配置 / 连接
# --------------------------------------------------------------------------- #
def load_trade_config():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(HERE, "config.ini"), encoding="utf-8")
    return dict(
        user=cfg.get("auth", "user", fallback="").strip(),
        password=cfg.get("auth", "password", fallback="").strip(),
        broker_id=cfg.get("trade", "broker_id", fallback="").strip(),
        account=cfg.get("trade", "account", fallback="").strip(),
        trade_password=cfg.get("trade", "password", fallback="").strip(),
        order_timeout=cfg.getfloat("trade", "order_timeout", fallback=60.0),
        max_volume=cfg.getint("trade", "max_volume", fallback=10),
        give_up_ticks=cfg.getfloat("trade", "give_up_ticks", fallback=0.0),  # 0=不启用
    )


def build_api(cfg, dry_run):
    auth = TqAuth(cfg["user"], cfg["password"])
    if dry_run:
        log("连接行情（dry-run，使用模拟账户仅取行情，不真发组合单）...")
        return TqApi(auth=auth)                       # 默认 TqSim，仅用于行情
    if not (cfg["broker_id"] and cfg["account"] and cfg["trade_password"]):
        raise SystemExit("实盘需要在 config.ini [trade] 填 broker_id / account / password")
    log(f"连接实盘 OTG：{cfg['broker_id']} / {cfg['account']} ...")
    return TqApi(account=TqAccount(cfg["broker_id"], cfg["account"], cfg["trade_password"]), auth=auth)


# --------------------------------------------------------------------------- #
# 组合合约解析：由 近/远 outright 找组合合约与方向
# --------------------------------------------------------------------------- #
def resolve_combo(api, near, far):
    """返回 (combo_symbol, orient)。orient=+1 组合腿序为(near,far)；-1 为(far,near)；找不到 (None,0)。"""
    ex = near.split(".")[0]
    for c in api.query_quotes(ins_class="COMBINE", exchange_id=ex, expired=False) or []:
        parsed = sm.parse_combine(c)
        if not parsed:
            continue
        _ex, _pfx, f1, f2 = parsed
        if (f1, f2) == (near, far):
            return c, +1
        if (f1, f2) == (far, near):
            return c, -1
    return None, 0


def side_to_order(side, orient, price):
    """把 买价差/卖价差(近-远口径) + 组合腿序 → (组合下单方向, 组合限价)。"""
    buy = side in ("买价差", "BUY", "buy")
    if orient >= 0:                 # 组合就是 近-远
        return ("BUY" if buy else "SELL"), price
    else:                           # 组合是 远-近，价差取负、方向反转
        return ("SELL" if buy else "BUY"), round(-price, 6)


# --------------------------------------------------------------------------- #
# 被动到价单 状态机
# --------------------------------------------------------------------------- #
class PassiveSpreadOrder:
    def __init__(self, api, symbol, direction, volume, limit_price, offset="OPEN",
                 timeout=60.0, give_up_ticks=0.0, dry_run=True):
        self.api = api
        self.symbol = symbol
        self.direction = direction          # 组合下单方向 BUY/SELL
        self.volume = int(volume)
        self.limit_price = limit_price
        self.offset = offset
        self.timeout = timeout
        self.give_up_ticks = give_up_ticks
        self.dry_run = dry_run

    def _wait_quote(self, q):
        end = time.time() + 10
        while time.time() < end:
            self.api.wait_update(deadline=time.time() + 0.4)
            if not sm.is_nan(q.ask_price1) or not sm.is_nan(q.bid_price1) or not sm.is_nan(q.last_price):
                return True
        return False

    def _opportunity_gone(self, q, tick):
        """价差走坏(我们永远不可能以更好价成交)且超出容忍 → 放弃。"""
        if not self.give_up_ticks:
            return False
        tol = self.give_up_ticks * (tick or 1)
        if self.direction == "BUY":
            ask = sm.clean(q.ask_price1)      # 买价差成本；市场卖价比我们限价高太多
            return ask is not None and ask > self.limit_price + tol
        else:
            bid = sm.clean(q.bid_price1)
            return bid is not None and bid < self.limit_price - tol

    def run(self):
        q = self.api.get_quote(self.symbol)
        if not self._wait_quote(q):
            log(f"取不到 {self.symbol} 盘口，放弃。"); return dict(filled=0, status="NO_QUOTE")
        tick = q.price_tick or 1
        log(f"组合 {self.symbol}  盘口 买{sm.clean(q.bid_price1)}/卖{sm.clean(q.ask_price1)} 最新{sm.clean(q.last_price)}  price_tick={tick}")
        log(f"下单意图：{self.direction} {self.volume} 手 @ 限价 {self.limit_price}  offset={self.offset}  "
            f"({'DRY-RUN 模拟' if self.dry_run else '实盘'})")
        return self._run_dry(q, tick) if self.dry_run else self._run_live(q, tick)

    # ---- 实盘 ----
    def _run_live(self, q, tick):
        order = self.api.insert_order(symbol=self.symbol, direction=self.direction,
                                      offset=self.offset, volume=self.volume,
                                      limit_price=self.limit_price)
        start = time.time(); canceled = False
        while order.status != "FINISHED":
            self.api.wait_update(deadline=time.time() + 0.4)
            traded = self.volume - order.volume_left
            if not canceled and time.time() - start > self.timeout and order.volume_left > 0:
                log(f"超时 {self.timeout:.0f}s，撤剩余 {order.volume_left} 手"); self.api.cancel_order(order); canceled = True
            elif not canceled and self._opportunity_gone(q, tick) and order.volume_left > 0:
                log("价差走坏，撤单"); self.api.cancel_order(order); canceled = True
        traded = self.volume - order.volume_left
        log(f"结束：成交 {traded}/{self.volume} 手  均价 {sm.clean(order.trade_price)}  {order.last_msg}")
        return dict(filled=traded, avg_price=sm.clean(order.trade_price), status="FILLED" if traded == self.volume else "PARTIAL")

    # ---- dry-run：用真行情模拟被动成交(市价穿过限价即成交，成交在限价) ----
    def _run_dry(self, q, tick):
        start = time.time(); filled = 0
        log("DRY-RUN 挂单中，等待市价穿越限价 ...")
        while True:
            self.api.wait_update(deadline=time.time() + 0.4)
            ask, bid = sm.clean(q.ask_price1), sm.clean(q.bid_price1)
            hit = (self.direction == "BUY" and ask is not None and ask <= self.limit_price) or \
                  (self.direction == "SELL" and bid is not None and bid >= self.limit_price)
            if hit:
                filled = self.volume
                log(f"DRY-RUN 成交(模拟)：{self.direction} {filled} 手 @ {self.limit_price}"
                    f"（触发价 市场{'卖' if self.direction=='BUY' else '买'}={ask if self.direction=='BUY' else bid}）")
                return dict(filled=filled, avg_price=self.limit_price, status="FILLED")
            if time.time() - start > self.timeout:
                log(f"DRY-RUN 超时 {self.timeout:.0f}s 未成交（市价未穿越限价 {self.limit_price}）")
                return dict(filled=0, avg_price=None, status="TIMEOUT")
            if self._opportunity_gone(q, tick):
                log("DRY-RUN 价差走坏，撤单")
                return dict(filled=0, avg_price=None, status="GAVE_UP")


# --------------------------------------------------------------------------- #
# 对外接口（手动 & 自动通用）
# --------------------------------------------------------------------------- #
def place_passive_spread(api, cfg, *, symbol=None, near=None, far=None, side=None,
                         direction=None, volume=1, price=None, offset="OPEN",
                         timeout=None, dry_run=True):
    """
    下一笔被动价差单。两种指定方式：
      A) symbol=组合合约 + direction=BUY/SELL(组合方向) + price=组合限价
      B) near/far outright + side=买价差/卖价差 + price=近-远目标价差(自动解析组合与方向)
    volume/price 必填。返回执行结果 dict。
    """
    if volume > cfg["max_volume"]:
        raise SystemExit(f"手数 {volume} 超过风控上限 max_volume={cfg['max_volume']}")
    if price is None:
        raise SystemExit("必须给定 price（限价，防止市价误单）")

    if symbol:
        combo, d = symbol, (direction or "").upper()
        limit = price
        if d not in ("BUY", "SELL"):
            raise SystemExit("用 --symbol 时需给 --side BUY 或 SELL（组合方向）")
    else:
        if not (near and far and side):
            raise SystemExit("需给 --near --far --side（或用 --symbol）")
        combo, orient = resolve_combo(api, near, far)
        if not combo:
            raise SystemExit(f"未找到 {near}&{far} 的交易所组合合约（该对不可组合下单）")
        d, limit = side_to_order(side, orient, price)
        log(f"解析组合：{combo}  腿序{'近-远' if orient>=0 else '远-近'}  → 组合方向 {d} @ {limit}")

    task = PassiveSpreadOrder(api, combo, d, volume, limit, offset=offset,
                              timeout=timeout if timeout is not None else cfg["order_timeout"],
                              give_up_ticks=cfg["give_up_ticks"], dry_run=dry_run)
    return task.run()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="跨期价差组合合约下单（默认 dry-run）")
    ap.add_argument("--symbol", help="组合合约，如 'DCE.SP m2509&m2511'")
    ap.add_argument("--near", help="近月 outright，如 DCE.m2509")
    ap.add_argument("--far", help="远月 outright，如 DCE.m2511")
    ap.add_argument("--side", help="买价差 / 卖价差（配 --near/--far）；或 BUY / SELL（配 --symbol）")
    ap.add_argument("--vol", type=int, required=True, help="手数")
    ap.add_argument("--price", type=float, required=True, help="限价(近-远口径的目标价差；可负)")
    ap.add_argument("--offset", default="OPEN", choices=["OPEN", "CLOSE", "CLOSETODAY"])
    ap.add_argument("--timeout", type=float, default=None, help="挂单超时秒数")
    ap.add_argument("--live", action="store_true", help="走实盘(默认 dry-run)")
    args = ap.parse_args()

    cfg = load_trade_config()
    dry_run = not args.live
    if not dry_run:
        log("⚠ 实盘模式：将发出真实组合单！")
    api = build_api(cfg, dry_run)
    try:
        res = place_passive_spread(
            api, cfg, symbol=args.symbol, near=args.near, far=args.far,
            side=args.side, direction=args.side, volume=args.vol, price=args.price,
            offset=args.offset, timeout=args.timeout, dry_run=dry_run)
        log(f"执行结果：{res}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
