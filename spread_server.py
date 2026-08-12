# -*- coding: utf-8 -*-
"""
跨期套利半自动交易台后端。

- FastAPI + WebSocket 快照推送 + 命令队列；TqApi 非线程安全，所有 tqsdk 调用在工作线程内。
- 前端 web/trader.html（API 契约见该文件头部注释）；打开 http://127.0.0.1:8010 即用。
- 机会页打通：opportunities.html 选中的机会由页面 JS POST /api/opps_sync 过来，
  自动解析成标准套利组合加入自定义行情（去重、失败记日志）。
- 默认 dry-run：连真行情（TqSim 仅取行情），下单/算法全部内部模拟成交，不真发单。
  --live 用实盘 OTG 账户；账户优先 config.ini [trade]，为空则回落读期权项目
  I:/BaiduSyncdisk/claude 期权/config.json 的 account 段（用户指定用广发户）。

用法:
  python spread_server.py            # dry-run
  python spread_server.py --live     # 实盘（组合合约仅实盘可交易）
  python spread_server.py --port 8010
"""
import os
import re
import ast
import json
import time
import queue
import operator
import argparse
import threading
import traceback
import configparser
from datetime import datetime
from collections import deque

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

import spread_monitor as sm
import spread_trader as st_mod
from futures_comm_info import read_futures_info

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_FILE = os.path.join(HERE, "web", "trader.html")
DATA_DIR = os.path.join(HERE, "data")
WATCH_FILE = os.path.join(DATA_DIR, "trader_watchlist.json")
OPT_CFG = r"I:\BaiduSyncdisk\claude 期权\config.json"

ALIVE_ALGO = ("WORKING", "PENDING", "WAITING", "QUOTING_OPEN", "HOLDING", "QUOTING_CLOSE")


def now_hms():
    return datetime.now().strftime("%H:%M:%S")


def clean(x):
    v = sm.clean(x)
    return None if v is None else float(v)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config():
    cp = configparser.ConfigParser()
    cp.read(os.path.join(HERE, "config.ini"), encoding="utf-8")
    cfg = dict(
        user=cp.get("auth", "user", fallback="").strip(),
        password=cp.get("auth", "password", fallback="").strip(),
        broker_id=cp.get("trade", "broker_id", fallback="").strip(),
        account=cp.get("trade", "account", fallback="").strip(),
        trade_password=cp.get("trade", "password", fallback="").strip(),
        order_timeout=cp.getfloat("trade", "order_timeout", fallback=60.0),
        max_volume=cp.getint("trade", "max_volume", fallback=10),
        give_up_ticks=cp.getfloat("trade", "give_up_ticks", fallback=0.0),
        host=cp.get("server", "host", fallback="127.0.0.1"),
        port=cp.getint("server", "port", fallback=8010),
        push_interval=cp.getfloat("server", "push_interval_sec", fallback=1.0),
    )
    # 实盘账户回落：期权项目的广发户
    if not cfg["broker_id"] and os.path.exists(OPT_CFG):
        try:
            with open(OPT_CFG, encoding="utf-8") as f:
                acc = json.load(f).get("account", {})
            cfg["broker_id"] = acc.get("broker_id", "")
            cfg["account"] = acc.get("account_id", "")
            cfg["trade_password"] = acc.get("password", "")
        except Exception:
            pass
    return cfg


# --------------------------------------------------------------------------- #
# 共享状态（Web 线程 <-> TQ 工作线程）
# --------------------------------------------------------------------------- #
class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self._snapshot = "{}"
        self._logs = deque(maxlen=200)
        self._log_seq = 0

    def log(self, msg):
        with self.lock:
            self._log_seq += 1
            self._logs.append({"seq": self._log_seq, "t": now_hms(), "msg": str(msg)})
        print(f"[{now_hms()}] {msg}", flush=True)

    def logs(self):
        with self.lock:
            return list(self._logs)

    def set_snapshot(self, obj):
        s = json.dumps(obj, ensure_ascii=False, default=str)
        with self.lock:
            self._snapshot = s

    def get_snapshot_json(self):
        with self.lock:
            return self._snapshot


# --------------------------------------------------------------------------- #
# 安全公式解析（白名单 AST；与前端语法一致）
# --------------------------------------------------------------------------- #
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv}
_CMP = {ast.Gt: operator.gt, ast.Lt: operator.lt,
        ast.GtE: operator.ge, ast.LtE: operator.le}
_FIELDS = ("bid", "ask", "last")


class SafeExpr:
    """公式如 'ask <= bid(w3) - 3'。get(field, wid_or_None) -> float|None。"""

    def __init__(self, text):
        self.text = str(text)
        s = (self.text.replace("−", "-").replace("–", "-").replace("×", "*")
             .replace("÷", "/").replace("（", "(").replace("）", ")")
             .replace("＞", ">").replace("＜", "<").replace("＝", "="))
        try:
            tree = ast.parse(s, mode="eval")
        except SyntaxError:
            raise ValueError(f"公式语法错误: {text}")
        if not isinstance(tree.body, ast.Compare):
            raise ValueError("公式必须是一个比较式(含 > < >= <=)")
        self._validate(tree.body)
        self._tree = tree.body

    def _validate(self, node):
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or type(node.ops[0]) not in _CMP:
                raise ValueError("仅支持单个 > < >= <= 比较")
            self._validate(node.left); self._validate(node.comparators[0])
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN:
                raise ValueError("仅支持 + - * / 运算")
            self._validate(node.left); self._validate(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd)):
                raise ValueError("不支持的一元运算")
            self._validate(node.operand)
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError("常量只能是数字")
        elif isinstance(node, ast.Name):
            if node.id not in _FIELDS:
                raise ValueError(f"未知变量 {node.id}（只允许 bid/ask/last）")
        elif isinstance(node, ast.Call):
            if (not isinstance(node.func, ast.Name) or node.func.id not in _FIELDS
                    or len(node.args) != 1 or node.keywords
                    or not isinstance(node.args[0], ast.Name)):
                raise ValueError("函数只允许 bid(wN)/ask(wN)/last(wN)")
        else:
            raise ValueError(f"不支持的语法: {type(node).__name__}")

    def eval(self, get):
        """返回 True/False；任一引用价格缺失返回 None（视为不成立）。"""
        def ev(node):
            if isinstance(node, ast.Compare):
                a, b = ev(node.left), ev(node.comparators[0])
                if a is None or b is None:
                    return None
                return _CMP[type(node.ops[0])](a, b)
            if isinstance(node, ast.BinOp):
                a, b = ev(node.left), ev(node.right)
                if a is None or b is None:
                    return None
                if isinstance(node.op, ast.Div) and b == 0:
                    return None
                return _BIN[type(node.op)](a, b)
            if isinstance(node, ast.UnaryOp):
                v = ev(node.operand)
                if v is None:
                    return None
                return -v if isinstance(node.op, ast.USub) else v
            if isinstance(node, ast.Constant):
                return float(node.value)
            if isinstance(node, ast.Name):
                return get(node.id, None)
            if isinstance(node, ast.Call):
                return get(node.func.id, node.args[0].id)
            return None
        return ev(self._tree)


# --------------------------------------------------------------------------- #
# TQ 工作线程
# --------------------------------------------------------------------------- #
class Worker(threading.Thread):
    def __init__(self, cfg, state, live=False):
        super().__init__(daemon=True, name="tq-worker")
        self.cfg = cfg
        self.state = state
        self.live = live
        self.api = None
        self.rows = []            # 自定义行情 [{id,kind,leg_a,leg_b,symbol,legs,fee..}]
        self.next_id = 1
        self.opp_keys = set()     # 已同步过的机会 key
        self.quotes = {}          # symbol -> tq quote 对象
        self.comm = {}            # 合约代码 -> 费用表行
        self.code2full = {}       # jm2609 -> DCE.jm2609
        self.algos = []
        self.algo_seq = 0
        self.sim = SimBook(self)  # dry-run 模拟成交簿
        self.acct_label = ""

    # ---------- 基础 ----------
    def log(self, msg):
        self.state.log(msg)

    def get_quote(self, symbol):
        if symbol not in self.quotes:
            self.quotes[symbol] = self.api.get_quote(symbol)
        return self.quotes[symbol]

    # ---------- 行情行管理 ----------
    def _full_code(self, code):
        """jm2609 / DCE.jm2609 -> DCE.jm2609"""
        code = code.strip()
        if "." in code:
            return code
        row = self.comm.get(code) or self.comm.get(code.lower()) or self.comm.get(code.upper())
        if row and row.get("天勤代码"):
            return str(row["天勤代码"])
        raise ValueError(f"不认识的合约代码 {code}（费用表里没有）")

    def _resolve_leg(self, s):
        """输入组合代码或 '近&远' outright 对 -> 交易所组合合约代码。"""
        s = s.strip()
        if re.search(r"\.(SP|SPD|SPC|SP\s)", s) or " " in s:
            return s                                    # 已是组合代码
        if "&" in s:
            a, b = s.split("&", 1)
            near, far = self._full_code(a), self._full_code(b)
            combo, orient = st_mod.resolve_combo(self.api, near, far)
            if not combo:
                combo, orient = st_mod.resolve_combo(self.api, far, near)
            if not combo:
                raise ValueError(f"未找到 {near}&{far} 的交易所组合合约")
            return combo
        raise ValueError(f"无法识别腿 '{s}'（填组合代码或 近&远）")

    def _combo_legs(self, combo):
        parsed = sm.parse_combine(combo)
        if not parsed:
            raise ValueError(f"无法解析组合合约 {combo}")
        _ex, _pfx, f1, f2 = parsed
        return f1, f2

    def _row_fees(self, legs):
        """legs=outright 列表 -> (fee_today, fee_overnight, tick_value 近似)"""
        ft = fo = 0.0
        for sym in legs:
            code = sym.split(".")[-1]
            r = self.comm.get(code)
            if not r:
                return None, None
            px = float(r.get("最新价") or 0) or None
            q = self.quotes.get(sym)
            if q is not None:
                px = clean(q.last_price) or px
            mult = float(r.get("合约乘数") or 0)
            if not px or not mult:
                return None, None

            def fee(fixed, rate):
                f = float(r.get(fixed) or 0)
                return f if f > 0 else float(r.get(rate) or 0) * px * mult
            op = fee("手续费-开仓固定", "手续费-开仓费率")
            ct = fee("手续费-平今固定", "手续费-平今费率")
            cy = fee("手续费-平仓固定", "手续费-平仓费率")
            ft += op + ct
            fo += op + cy
        return round(ft, 2), round(fo, 2)

    def _tick_value(self, row):
        """1tick 毛利 = 组合 price_tick × 近月乘数"""
        legs = row["legs"]
        code = legs[0].split(".")[-1]
        r = self.comm.get(code)
        mult = float(r.get("合约乘数") or 0) if r else 0
        q = self.quotes.get(row["leg_a"])
        tick = clean(q.price_tick) if q is not None else None
        if not tick:
            r0 = self.comm.get(code)
            tick = float(r0.get("最小价差") or 0) if r0 else 0
        return round(tick * mult, 2) if tick and mult else None, tick

    def add_row(self, kind, leg_a, leg_b=None, note="", rid=None):
        la = self._resolve_leg(leg_a)
        lb = self._resolve_leg(leg_b) if kind == "dual" else None
        for r in self.rows:
            if r["leg_a"] == la and r.get("leg_b") == lb:
                raise ValueError(f"行情已存在: {r['symbol']} ({r['id']})")
        legs = list(self._combo_legs(la))
        if lb:
            legs += list(self._combo_legs(lb))
        short = lambda s: s.split(".", 1)[-1]
        symbol = la if kind == "single" else f"{short(la)} − {short(lb)}"
        if rid is None:
            rid = f"w{self.next_id}"
            self.next_id += 1
        row = dict(id=rid, kind=kind, leg_a=la, leg_b=lb,
                   symbol=symbol, legs=legs, note=note)
        # 订阅组合 + 各 outright 腿（腿行情用于持仓归集估值）
        self.get_quote(la)
        if lb:
            self.get_quote(lb)
        for s in legs:
            self.get_quote(s)
        self.rows.append(row)
        self.save_watchlist()
        self.log(f"已添加行情 {row['id']} {symbol}" + (f"（{note}）" if note else ""))
        return row

    def del_row(self, rid):
        n = len(self.rows)
        self.rows = [r for r in self.rows if r["id"] != rid]
        if len(self.rows) < n:
            self.save_watchlist()
            self.log(f"已删除行情 {rid}")

    def save_watchlist(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        data = dict(next_id=self.next_id, opp_keys=sorted(self.opp_keys),
                    rows=[{k: r[k] for k in ("id", "kind", "leg_a", "leg_b", "note")}
                          for r in self.rows])
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def load_watchlist(self):
        if not os.path.exists(WATCH_FILE):
            return
        try:
            with open(WATCH_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.opp_keys = set(data.get("opp_keys", []))
            for r in data.get("rows", []):
                try:
                    self.add_row(r["kind"], r["leg_a"], r.get("leg_b"),
                                 r.get("note", ""), rid=r["id"])
                except Exception as e:
                    self.log(f"恢复行情 {r.get('id')} 失败: {e}")
            self.next_id = max([data.get("next_id", 1)] +
                               [int(r["id"][1:]) + 1 for r in self.rows if r["id"][1:].isdigit()])
            self.save_watchlist()
        except Exception as e:
            self.log(f"读取 watchlist 失败: {e}")

    # ---------- 机会页同步 ----------
    def sync_opps(self, opps):
        added = 0
        for o in opps or []:
            key = f"{o.get('strategy')}|{o.get('structure')}|{o.get('direction')}"
            if key in self.opp_keys:
                continue
            combo = (o.get("combo") or "").strip()
            struct = (o.get("structure") or "").strip()
            note = f"机会:{o.get('strategy', '')}"
            try:
                if combo:
                    self.add_row("single", combo, note=note)
                else:
                    parts = struct.split("-")
                    if len(parts) != 2:
                        raise ValueError(f"结构 {struct} 不是两腿价差，需手动处理")
                    self.add_row("single", f"{parts[0]}&{parts[1]}", note=note)
                added += 1
            except ValueError as e:
                self.log(f"机会「{struct or combo}」未加入: {e}")
            self.opp_keys.add(key)          # 成功失败都记，避免反复刷日志
        if added:
            self.log(f"机会页同步：新增 {added} 条行情")
        self.save_watchlist()
        return added

    # ---------- 行情快照 ----------
    def _quote_dict(self, row):
        qa = self.quotes.get(row["leg_a"])
        d = dict(id=row["id"], kind=row["kind"], symbol=row["symbol"],
                 leg_a=row["leg_a"], leg_b=row.get("leg_b"), note=row.get("note", ""))
        tick_value, tick = self._tick_value(row)
        d["price_tick"] = tick
        d["tick_value"] = tick_value
        if row["kind"] == "single":
            d.update(bid=clean(qa.bid_price1), ask=clean(qa.ask_price1),
                     bid_vol=clean(qa.bid_volume1), ask_vol=clean(qa.ask_volume1),
                     last=clean(qa.last_price),
                     leg_a_last=clean(qa.last_price), leg_b_last=None)
            pre = clean(qa.pre_settlement) or clean(qa.pre_close)
            d["change"] = (round(d["last"] - pre, 4)
                           if d["last"] is not None and pre else None)
        else:
            qb = self.quotes.get(row["leg_b"])
            ab, aa = clean(qa.bid_price1), clean(qa.ask_price1)
            bb, ba = clean(qb.bid_price1), clean(qb.ask_price1)
            al, bl = clean(qa.last_price), clean(qb.last_price)
            d.update(
                bid=None if ab is None or ba is None else round(ab - ba, 4),
                ask=None if aa is None or bb is None else round(aa - bb, 4),
                bid_vol=None if ab is None or ba is None
                    else min(clean(qa.bid_volume1) or 0, clean(qb.ask_volume1) or 0),
                ask_vol=None if aa is None or bb is None
                    else min(clean(qa.ask_volume1) or 0, clean(qb.bid_volume1) or 0),
                last=None if al is None or bl is None else round(al - bl, 4),
                leg_a_last=al, leg_b_last=bl)
            pa = clean(qa.pre_settlement) or clean(qa.pre_close)
            pb = clean(qb.pre_settlement) or clean(qb.pre_close)
            d["change"] = (round(d["last"] - (pa - pb), 4)
                           if d["last"] is not None and pa and pb else None)
        return d

    def quote_values(self, wid_or_row):
        """给公式求值用：返回 {bid,ask,last}"""
        row = wid_or_row if isinstance(wid_or_row, dict) else \
            next((r for r in self.rows if r["id"] == wid_or_row), None)
        if row is None:
            return None
        d = self._quote_dict(row)
        return {f: d.get(f) for f in _FIELDS}

    # ---------- 持仓三视图 ----------
    def build_positions(self):
        # 单合约明细：live 从 api，dry-run 从 sim 簿
        if self.live:
            contract, net, meta = [], {}, {}
            for sym, p in (self.api.get_position() or {}).items():
                for side, vol in (("LONG", p.pos_long), ("SHORT", p.pos_short)):
                    if not vol:
                        continue
                    op = clean(p.open_price_long if side == "LONG" else p.open_price_short)
                    fp = clean(p.float_profit_long if side == "LONG" else p.float_profit_short)
                    mg = clean(p.margin_long if side == "LONG" else p.margin_short)
                    q = self.quotes.get(sym) or self.get_quote(sym)
                    contract.append(dict(symbol=sym, direction=side, volume=int(vol),
                                         can_close=int(vol), open_price=op,
                                         last_price=clean(q.last_price),
                                         float_profit=fp, margin=mg))
                    net[sym] = net.get(sym, 0) + (int(vol) if side == "LONG" else -int(vol))
                    meta.setdefault(sym, {})[side] = op
        else:
            contract, net, meta = self.sim.contract_view()
        combos = []                      # 所有已知组合（watchlist 出现过的）
        for r in self.rows:
            for c in ([r["leg_a"]] if r["kind"] == "single" else [r["leg_a"], r["leg_b"]]):
                if c and c not in [x[0] for x in combos]:
                    try:
                        combos.append((c, self._combo_legs(c)))
                    except ValueError:
                        pass
        combo_view = self._decompose_combos(dict(net), meta, combos)
        strat_view = self._decompose_strategies(dict(net), meta)
        return contract, combo_view, strat_view

    def _leg_mult(self, sym):
        r = self.comm.get(sym.split(".")[-1])
        return float(r.get("合约乘数") or 0) if r else 0

    def _mark_price(self, q):
        """估值价：优先最新价，无成交用买卖中间价"""
        if q is None:
            return None
        last = clean(q.last_price)
        if last is not None:
            return last
        b, a = clean(q.bid_price1), clean(q.ask_price1)
        if b is not None and a is not None:
            return round((a + b) / 2, 4)
        return b if b is not None else a

    def _decompose_combos(self, net, meta, combos):
        out = []
        for combo, (near, far) in combos:
            q = self.quotes.get(combo)
            last = self._mark_price(q)
            mult = self._leg_mult(near)
            for dirn, n_side, f_side in (("LONG", 1, -1), ("SHORT", -1, 1)):
                units = min(max(net.get(near, 0) * n_side, 0),
                            max(net.get(far, 0) * f_side, 0))
                if units <= 0:
                    continue
                op_n = (meta.get(near, {}) or {}).get("LONG" if n_side > 0 else "SHORT")
                op_f = (meta.get(far, {}) or {}).get("LONG" if f_side > 0 else "SHORT")
                open_sp = None if op_n is None or op_f is None else round(op_n - op_f, 4)
                fp = (None if last is None or open_sp is None or not mult
                      else round((last - open_sp) * (1 if dirn == "LONG" else -1) * mult * units, 1))
                out.append(dict(symbol=combo, direction=dirn, volume=int(units),
                                open_price=open_sp, last_price=last, float_profit=fp))
                net[near] = net.get(near, 0) - n_side * units
                net[far] = net.get(far, 0) - f_side * units
        return out

    def _decompose_strategies(self, net, meta):
        out = []
        for r in self.rows:
            if r["kind"] != "dual":
                continue
            a1, a2 = self._combo_legs(r["leg_a"])
            b1, b2 = self._combo_legs(r["leg_b"])
            vec = {}
            for s, w in ((a1, 1), (a2, -1), (b1, -1), (b2, 1)):
                vec[s] = vec.get(s, 0) + w
            vec = {s: w for s, w in vec.items() if w}
            d = self._quote_dict(r)
            mult = self._leg_mult(a1)
            for dirn, sign in (("LONG", 1), ("SHORT", -1)):
                units = None
                for s, w in vec.items():
                    have = net.get(s, 0) * (1 if w * sign > 0 else -1)
                    u = have // abs(w) if have > 0 else 0
                    units = u if units is None else min(units, u)
                if not units or units <= 0:
                    continue
                open_sp = 0.0
                ok = True
                for s, w in vec.items():
                    side = "LONG" if w * sign > 0 else "SHORT"
                    op = (meta.get(s, {}) or {}).get(side)
                    if op is None:
                        ok = False
                        break
                    open_sp += w * op
                open_sp = round(open_sp, 4) if ok else None
                last = d.get("last")
                if last is None and d.get("bid") is not None and d.get("ask") is not None:
                    last = round((d["bid"] + d["ask"]) / 2, 4)
                fp = (None if last is None or open_sp is None or not mult
                      else round((last - open_sp) * sign * mult * units, 1))
                out.append(dict(watch_id=r["id"], symbol=r["symbol"], direction=dirn,
                                volume=int(units), open_price=open_sp,
                                last_price=last, float_profit=fp))
                for s, w in vec.items():
                    net[s] = net.get(s, 0) - w * sign * units
        return out

    # ---------- 账户 / 委托 ----------
    def build_account(self):
        if self.live:
            a = self.api.get_account()
            return dict(balance=clean(a.balance), available=clean(a.available),
                        margin=clean(a.margin), float_profit=clean(a.float_profit),
                        close_profit=clean(a.close_profit), risk_ratio=clean(a.risk_ratio))
        return self.sim.account_view()

    def build_orders(self):
        if self.live:
            out = []
            for oid, o in (self.api.get_order() or {}).items():
                try:
                    t = datetime.fromtimestamp(o.insert_date_time / 1e9).strftime("%H:%M:%S")
                except Exception:
                    t = ""
                out.append(dict(order_id=oid, time=t, symbol=f"{o.exchange_id}.{o.instrument_id}",
                                direction=o.direction, offset=o.offset,
                                limit_price=clean(o.limit_price), volume=int(o.volume_orign),
                                volume_left=int(o.volume_left),
                                status="ALIVE" if o.status == "ALIVE" else "FINISHED",
                                last_msg=o.last_msg or ""))
            out.sort(key=lambda x: x["time"], reverse=True)
            return out[:100]
        return self.sim.orders_view()

    # ---------- 下单 / 撤单（live 与 dry-run 统一入口） ----------
    def place_order(self, symbol, direction, offset, volume, limit_price, tag=""):
        if volume > self.cfg["max_volume"]:
            raise ValueError(f"手数 {volume} 超风控上限 {self.cfg['max_volume']}")
        if self.live:
            o = self.api.insert_order(symbol=symbol, direction=direction, offset=offset,
                                      volume=int(volume), limit_price=float(limit_price))
            self.log(f"实盘报单 {symbol} {direction} {offset} {volume}手 @{limit_price} {tag}")
            return LiveOrderRef(o)
        return self.sim.place(symbol, direction, offset, int(volume), float(limit_price), tag)

    def cancel_order(self, ref):
        if ref is None:
            return
        if self.live:
            self.api.cancel_order(ref.order)
        else:
            self.sim.cancel(ref.order_id)

    def cancel_by_id(self, order_id):
        if self.live:
            orders = self.api.get_order() or {}
            for oid, o in orders.items():
                if (not order_id or oid == order_id) and o.status == "ALIVE":
                    self.api.cancel_order(o)
                    self.log(f"撤单 {oid}")
        else:
            self.sim.cancel(order_id or None)

    def chase(self, order_id, ticks):
        """撤掉未完成委托，按对价±ticks 重挂。"""
        items = []
        if self.live:
            for oid, o in (self.api.get_order() or {}).items():
                if (not order_id or oid == order_id) and o.status == "ALIVE":
                    items.append((f"{o.exchange_id}.{o.instrument_id}", o.direction,
                                  o.offset, int(o.volume_left), o))
        else:
            items = self.sim.chase_items(order_id or None)
        for sym, d, off, left, ref in items:
            q = self.get_quote(sym)
            tick = clean(q.price_tick) or 1
            px = clean(q.ask_price1) if d == "BUY" else clean(q.bid_price1)
            if px is None:
                self.log(f"追单失败 {sym}: 无对价")
                continue
            px = px + (ticks * tick if d == "BUY" else -ticks * tick)
            if self.live:
                self.api.cancel_order(ref)
            else:
                self.sim.cancel(ref)
            self.place_order(sym, d, off, left, round(px, 6), tag="追单")

    # ---------- 命令处理 ----------
    def handle_command(self, c):
        act = c.get("action")
        try:
            if act == "watch_add":
                if c.get("kind") == "dual":
                    self.add_row("dual", c["leg_a"], c["leg_b"])
                else:
                    self.add_row("single", c["leg_a"])
            elif act == "watch_del":
                self.del_row(c.get("id"))
            elif act == "order":
                self.place_order(c["symbol"], c["direction"], c.get("offset", "OPEN"),
                                 int(c["volume"]), float(c["limit_price"]), tag="手动限价")
            elif act == "cancel":
                self.cancel_by_id(c.get("order_id") or "")
            elif act == "chase":
                self.chase(c.get("order_id") or "", int(c.get("ticks", 0)))
            elif act == "algo_create":
                self.create_algo(c)
            elif act == "algo_cancel":
                for a in self.algos:
                    if a.algo_id == c.get("algo_id") and a.status in ALIVE_ALGO:
                        a.terminate("手动终止")
            elif act == "algo_clear_done":
                self.algos = [a for a in self.algos if a.status in ALIVE_ALGO]
            elif act == "opps_sync":
                self.sync_opps(c.get("opps"))
            else:
                self.log(f"未知命令 {act}")
        except Exception as e:
            self.log(f"命令 {act} 失败: {e}")

    def create_algo(self, c):
        kind = c.get("kind", "single")
        vol = int(c.get("volume", 1))
        if vol > self.cfg["max_volume"]:
            raise ValueError(f"手数 {vol} 超风控上限 {self.cfg['max_volume']}")
        self.algo_seq += 1
        aid = f"a{self.algo_seq}"
        if kind == "formula":
            row = next((r for r in self.rows if r["id"] == c.get("watch_id")), None)
            if row is None:
                raise ValueError(f"公式单找不到行情行 {c.get('watch_id')}")
            if row["kind"] != "single":
                raise ValueError("公式单暂只支持一腿行情")
            a = FormulaAlgo(self, aid, row, c["direction"], vol,
                            c["open_expr"], c["close_expr"],
                            float(c.get("reprice_min_sec", 3)))
        elif kind == "dual":
            a = DualAlgo(self, aid, c["leg_a"], c["leg_b"], c.get("passive_leg", "A"),
                         c["direction"], c.get("offset", "OPEN"), vol, float(c["price"]),
                         float(c.get("timeout", self.cfg["order_timeout"])))
        else:
            a = SingleAlgo(self, aid, c["symbol"], c["direction"], c.get("offset", "OPEN"),
                           vol, float(c["price"]),
                           float(c.get("timeout", self.cfg["order_timeout"])),
                           float(c.get("give_up_ticks", self.cfg["give_up_ticks"])))
        self.algos.append(a)
        self.log(f"算法单 {aid} 已创建: {a.describe()}")

    # ---------- 快照 ----------
    def build_snapshot(self):
        watch = []
        fees_cache = getattr(self, "_fees_cache", {})
        for r in self.rows:
            d = self._quote_dict(r)
            key = (r["leg_a"], r.get("leg_b"))
            if key not in fees_cache:
                fees_cache[key] = self._row_fees(r["legs"])
            d["fee_today"], d["fee_overnight"] = fees_cache[key]
            watch.append(d)
        self._fees_cache = fees_cache
        contract, combo, strategy = self.build_positions()
        return dict(
            mode="live" if self.live else "dry-run",
            account_label=self.acct_label,
            max_volume=self.cfg["max_volume"],
            account=self.build_account(),
            watchlist=watch,
            positions=dict(contract=contract, combo=combo, strategy=strategy),
            orders=self.build_orders(),
            algos=[a.to_dict() for a in self.algos],
            logs=self.state.logs(),
            ts=now_hms(),
        )

    # ---------- 主循环 ----------
    def run(self):
        from tqsdk import TqApi, TqAuth, TqAccount
        try:
            auth = TqAuth(self.cfg["user"], self.cfg["password"])
            if self.live:
                if not (self.cfg["broker_id"] and self.cfg["account"] and self.cfg["trade_password"]):
                    self.log("实盘缺少账户配置(config.ini [trade] 或 期权项目 config.json)")
                    return
                self.acct_label = f"{self.cfg['broker_id']} {self.cfg['account']}"
                self.log(f"连接实盘 OTG: {self.acct_label} ...")
                self.api = TqApi(account=TqAccount(self.cfg["broker_id"], self.cfg["account"],
                                                   self.cfg["trade_password"]), auth=auth)
            else:
                self.acct_label = "DRY-RUN 模拟簿"
                self.log("连接行情(dry-run, 模拟成交不真发单)...")
                self.api = TqApi(auth=auth)
            self.comm, _ = read_futures_info()
            self.log(f"费用表载入 {len(self.comm)} 合约")
            self.load_watchlist()
            self.log("工作线程就绪")
            last_push = 0.0
            while True:
                self.api.wait_update(deadline=time.time() + 0.4)
                while True:
                    try:
                        c = self.state.commands.get_nowait()
                    except queue.Empty:
                        break
                    self.handle_command(c)
                now = time.time()
                self.sim.step()
                for a in self.algos:
                    if a.status in ALIVE_ALGO:
                        try:
                            a.step(now)
                        except Exception as e:
                            a.terminate(f"算法异常: {e}")
                            self.log(f"算法 {a.algo_id} 异常终止: {e}")
                if now - last_push >= self.cfg["push_interval"]:
                    last_push = now
                    try:
                        self.state.set_snapshot(self.build_snapshot())
                    except Exception:
                        self.log("快照构建失败:\n" + traceback.format_exc(limit=3))
        except Exception:
            self.log("工作线程崩溃:\n" + traceback.format_exc(limit=5))
        finally:
            try:
                if self.api:
                    self.api.close()
            except Exception:
                pass


class LiveOrderRef:
    def __init__(self, order):
        self.order = order
        self.order_id = order.order_id

    @property
    def left(self):
        return int(self.order.volume_left)

    @property
    def alive(self):
        return self.order.status == "ALIVE"


# --------------------------------------------------------------------------- #
# dry-run 模拟成交簿：限价单按盘口穿越成交；持仓拆到 outright 腿
# --------------------------------------------------------------------------- #
class SimOrderRef:
    def __init__(self, rec):
        self.rec = rec
        self.order_id = rec["order_id"]

    @property
    def left(self):
        return self.rec["volume_left"]

    @property
    def alive(self):
        return self.rec["status"] == "ALIVE"


class SimBook:
    INIT_BALANCE = 1_000_000.0

    def __init__(self, worker):
        self.w = worker
        self.seq = 0
        self.orders = []          # 委托记录
        self.pos = {}             # sym -> {"LONG":{vol,open}, "SHORT":{...}}
        self.close_profit = 0.0

    # ---- 下单/撤单 ----
    def place(self, symbol, direction, offset, volume, price, tag=""):
        self.seq += 1
        rec = dict(order_id=f"s{self.seq}", time=now_hms(), symbol=symbol,
                   direction=direction, offset=offset, limit_price=price,
                   volume=volume, volume_left=volume, status="ALIVE",
                   last_msg=f"模拟已报{('·' + tag) if tag else ''}")
        self.orders.insert(0, rec)
        self.w.log(f"[模拟] 报单 {symbol} {direction} {offset} {volume}手 @{price} {tag}")
        return SimOrderRef(rec)

    def cancel(self, ref_or_id):
        for rec in self.orders:
            rid = ref_or_id.order_id if isinstance(ref_or_id, SimOrderRef) else ref_or_id
            if (rid is None or rec["order_id"] == rid) and rec["status"] == "ALIVE":
                rec["status"] = "FINISHED"
                rec["last_msg"] = "模拟已撤单"

    def chase_items(self, order_id):
        out = []
        for rec in self.orders:
            if (order_id is None or rec["order_id"] == order_id) and rec["status"] == "ALIVE":
                out.append((rec["symbol"], rec["direction"], rec["offset"],
                            rec["volume_left"], SimOrderRef(rec)))
        return out

    # ---- 撮合：市场对价穿越限价即全部成交（成交价=限价） ----
    def step(self):
        for rec in self.orders:
            if rec["status"] != "ALIVE":
                continue
            q = self.w.quotes.get(rec["symbol"])
            if q is None:
                continue
            ask, bid = clean(q.ask_price1), clean(q.bid_price1)
            hit = ((rec["direction"] == "BUY" and ask is not None and ask <= rec["limit_price"])
                   or (rec["direction"] == "SELL" and bid is not None and bid >= rec["limit_price"]))
            if hit:
                self._fill(rec)

    def _fill(self, rec):
        vol = rec["volume_left"]
        rec["volume_left"] = 0
        rec["status"] = "FINISHED"
        rec["last_msg"] = f"模拟全部成交 @{rec['limit_price']}"
        self.w.log(f"[模拟] 成交 {rec['symbol']} {rec['direction']} {vol}手 @{rec['limit_price']}")
        # 拆腿记持仓：组合 BUY = 买近卖远
        try:
            near, far = self.w._combo_legs(rec["symbol"])
            legs = [(near, rec["direction"]),
                    (far, "SELL" if rec["direction"] == "BUY" else "BUY")]
        except ValueError:
            legs = [(rec["symbol"], rec["direction"])]
        for sym, d in legs:
            q = self.w.quotes.get(sym) or self.w.get_quote(sym)
            px = clean(q.last_price) or 0.0
            self._apply(sym, d, rec["offset"], vol, px)

    def _apply(self, sym, direction, offset, vol, px):
        p = self.pos.setdefault(sym, {"LONG": {"vol": 0, "open": 0.0},
                                      "SHORT": {"vol": 0, "open": 0.0}})
        mult = self.w._leg_mult(sym) or 1
        if offset == "OPEN":
            side = p["LONG"] if direction == "BUY" else p["SHORT"]
            tot = side["vol"] + vol
            side["open"] = (side["open"] * side["vol"] + px * vol) / tot
            side["vol"] = tot
        else:
            side = p["SHORT"] if direction == "BUY" else p["LONG"]
            take = min(side["vol"], vol)
            sign = -1 if direction == "BUY" else 1     # 买平=平空头
            self.close_profit += (px - side["open"]) * sign * take * mult
            side["vol"] -= take

    # ---- 视图 ----
    def contract_view(self):
        contract, net, meta = [], {}, {}
        for sym, p in self.pos.items():
            q = self.w.quotes.get(sym)
            last = clean(q.last_price) if q is not None else None
            mult = self.w._leg_mult(sym) or 1
            r = self.w.comm.get(sym.split(".")[-1]) or {}
            mrate = float(r.get("保证金率") or 0.1)
            for side_name, sign in (("LONG", 1), ("SHORT", -1)):
                s = p[side_name]
                if not s["vol"]:
                    continue
                fp = (None if last is None
                      else round((last - s["open"]) * sign * s["vol"] * mult, 1))
                margin = round((last or s["open"]) * mult * s["vol"] * mrate, 0)
                contract.append(dict(symbol=sym, direction=side_name, volume=s["vol"],
                                     can_close=s["vol"], open_price=round(s["open"], 4),
                                     last_price=last, float_profit=fp, margin=margin))
                net[sym] = net.get(sym, 0) + sign * s["vol"]
                meta.setdefault(sym, {})[side_name] = s["open"]
        return contract, net, meta

    def account_view(self):
        contract, _, _ = self.contract_view()
        fp = sum(c["float_profit"] or 0 for c in contract)
        margin = sum(c["margin"] or 0 for c in contract)
        bal = self.INIT_BALANCE + self.close_profit + fp
        return dict(balance=round(bal, 1), available=round(bal - margin, 1),
                    margin=round(margin, 1), float_profit=round(fp, 1),
                    close_profit=round(self.close_profit, 1),
                    risk_ratio=round(margin / bal, 4) if bal else 0)

    def orders_view(self):
        return [dict(rec) for rec in self.orders[:100]]


# --------------------------------------------------------------------------- #
# 算法单
# --------------------------------------------------------------------------- #
def order_price(ref):
    """委托的当前限价（Sim/Live 通吃）"""
    if ref is None:
        return None
    if isinstance(ref, SimOrderRef):
        return ref.rec["limit_price"]
    return clean(ref.order.limit_price)


class AlgoBase:
    kind = "single"

    def __init__(self, worker, algo_id, symbol, direction, volume):
        self.w = worker
        self.algo_id = algo_id
        self.symbol = symbol
        self.direction = direction
        self.volume = volume
        self.filled = 0
        self.status = "WORKING"
        self.msg = ""
        self.deadline = None
        self.order = None          # LiveOrderRef / SimOrderRef

    def describe(self):
        return f"{self.symbol} {self.direction} {self.volume}手"

    def terminate(self, msg):
        self._cancel_order()
        if self.status in ALIVE_ALGO:
            self.status = "CANCELED"
        self.msg = msg

    def _cancel_order(self):
        if self.order is not None and self.order.alive:
            self.w.cancel_order(self.order)
        self.order = None

    def _sync_fill(self, prev_left):
        """返回自上次以来新增成交手数"""
        if self.order is None:
            return 0
        return max(0, prev_left - self.order.left)

    def to_dict(self):
        d = dict(algo_id=self.algo_id, kind=self.kind, symbol=self.symbol,
                 direction=self.direction, volume=self.volume, filled=self.filled,
                 status=self.status, msg=self.msg,
                 remain_sec=None if self.deadline is None
                 else max(0, int(self.deadline - time.time())))
        d.update(self.extra_dict())
        return d

    def extra_dict(self):
        return {}


class SingleAlgo(AlgoBase):
    """被动到价：按目标价挂组合限价单，超时撤、价差走坏撤。"""
    kind = "single"

    def __init__(self, w, aid, symbol, direction, offset, volume, price, timeout, give_up_ticks):
        super().__init__(w, aid, symbol, direction, volume)
        self.offset = offset
        self.price = price
        self.give_up_ticks = give_up_ticks
        self.deadline = time.time() + timeout
        self._last_left = volume

    def extra_dict(self):
        return dict(target_price=self.price)

    def step(self, now):
        if self.order is None:
            self.order = self.w.place_order(self.symbol, self.direction, self.offset,
                                            self.volume, self.price, tag=self.algo_id)
            self._last_left = self.order.left
            return
        self.filled += self._sync_fill(self._last_left)
        self._last_left = self.order.left
        if not self.order.alive:
            self.status = "FILLED" if self.filled >= self.volume else \
                ("PARTIAL" if self.filled else "CANCELED")
            return
        q = self.w.get_quote(self.symbol)
        tick = clean(q.price_tick) or 1
        gone = False
        if self.give_up_ticks:
            tol = self.give_up_ticks * tick
            if self.direction == "BUY":
                ask = clean(q.ask_price1)
                gone = ask is not None and ask > self.price + tol
            else:
                bid = clean(q.bid_price1)
                gone = bid is not None and bid < self.price - tol
        if now > self.deadline or gone:
            self._cancel_order()
            self.status = "PARTIAL" if self.filled else ("GAVE_UP" if gone else "TIMEOUT")
            self.msg = "价差走坏" if gone else "超时"


class DualAlgo(AlgoBase):
    """两腿：被动腿按目标组合价挂单（限频跟随），每成交即对价发另一腿。"""
    kind = "dual"
    REPRICE_SEC = 3.0

    def __init__(self, w, aid, leg_a, leg_b, passive, direction, offset, volume, price, timeout):
        short = lambda s: s.split(".", 1)[-1]
        super().__init__(w, aid, f"{short(leg_a)} − {short(leg_b)}", direction, volume)
        self.leg_a, self.leg_b = leg_a, leg_b
        self.passive = "B" if passive == "B" else "A"
        self.offset = offset
        self.price = price                       # 目标组合价 = A − B
        self.deadline = time.time() + timeout
        self.hedge_filled = 0
        self._last_left = volume
        self._last_quote_ts = 0.0

    def extra_dict(self):
        return dict(target_price=self.price, leg_a=self.leg_a, leg_b=self.leg_b,
                    passive_leg=self.passive)

    def _passive_symbol(self):
        return self.leg_a if self.passive == "A" else self.leg_b

    def _hedge_symbol(self):
        return self.leg_b if self.passive == "A" else self.leg_a

    def _passive_dir(self):
        # 买组合 = 买A 卖B
        if self.passive == "A":
            return self.direction
        return "SELL" if self.direction == "BUY" else "BUY"

    def _hedge_dir(self):
        return "SELL" if self._passive_dir() == "BUY" else "BUY"

    def _desired_price(self):
        """被动腿限价，使 组合价(A−B) = 目标价，对腿按对价立即可成交计。"""
        qh = self.w.get_quote(self._hedge_symbol())
        hb, ha = clean(qh.bid_price1), clean(qh.ask_price1)
        if self.passive == "A":
            ref = hb if self.direction == "BUY" else ha    # 对腿卖出用bid/买入用ask
            return None if ref is None else (self.price + ref if self.direction == "BUY"
                                             else self.price + ref)
        else:  # 被动腿是B：买组合→卖B, B限价 = A可成交价 − 目标
            ref = ha if self.direction == "BUY" else hb
            return None if ref is None else ref - self.price

    def step(self, now):
        # 同步被动腿成交并对冲（撤单在途也要吃到最后的成交）
        if self.order is not None:
            got = self._sync_fill(self._last_left)
            if got:
                self.filled += got
                self._do_hedge(got)
            if self.order.alive:
                self._last_left = self.order.left
            else:
                self.order = None
                self._last_left = 0
        if self.filled >= self.volume:
            if self.hedge_filled >= self.volume:
                self.status = "FILLED"
            return
        if now > self.deadline:
            if self.order is not None and self.order.alive:
                self.w.cancel_order(self.order)
                return                     # 下轮吃完尾部成交再定状态
            self.status = "PARTIAL" if self.filled else "TIMEOUT"
            self.msg = "超时"
            return
        px = self._desired_price()
        if px is None:
            return
        q = self.w.get_quote(self._passive_symbol())
        tick = clean(q.price_tick) or 1
        px = round(round(px / tick) * tick, 6)
        if self.order is None:
            self.order = self.w.place_order(self._passive_symbol(), self._passive_dir(),
                                            self.offset, self.volume - self.filled, px,
                                            tag=f"{self.algo_id}被动腿")
            self._last_left = self.order.left
            self._last_quote_ts = now
        else:
            cur = order_price(self.order)
            if (cur is not None and abs(cur - px) >= tick / 2
                    and now - self._last_quote_ts >= self.REPRICE_SEC):
                self.w.cancel_order(self.order)     # 撤成后下轮以新价重挂
                self._last_quote_ts = now

    def _do_hedge(self, vol):
        sym = self._hedge_symbol()
        q = self.w.get_quote(sym)
        d = self._hedge_dir()
        px = clean(q.ask_price1) if d == "BUY" else clean(q.bid_price1)
        if px is None:
            px = clean(q.last_price)
        if px is None:
            self.w.log(f"算法 {self.algo_id} 对冲腿 {sym} 无对价! 需手动处理")
            return
        self.w.place_order(sym, d, self.offset, vol, px, tag=f"{self.algo_id}对冲腿")
        self.hedge_filled += vol


class FormulaAlgo(AlgoBase):
    """公式条件单：公式成立期间自动维护本方挂单（无空价排队/有空价插队，
    跟随重挂+限频）；公式失效立即撤单。开仓成交转持仓，平仓公式成立再挂平仓。"""
    kind = "formula"

    def __init__(self, w, aid, row, direction, volume, open_expr, close_expr, reprice_min_sec):
        super().__init__(w, aid, row["leg_a"], direction, volume)
        self.watch_id = row["id"]
        self.open_expr_s, self.close_expr_s = open_expr, close_expr
        self.open_expr = SafeExpr(open_expr)
        self.close_expr = SafeExpr(close_expr)
        self.reprice_min_sec = max(0.5, reprice_min_sec)
        self.status = "WAITING"
        self.closed = 0
        self._last_left = 0
        self._last_quote_ts = 0.0
        self._row = row

    def describe(self):
        return (f"{self.symbol} 公式单 先{'买' if self.direction == 'BUY' else '卖'}开 "
                f"{self.volume}手 开[{self.open_expr_s}] 平[{self.close_expr_s}]")

    def extra_dict(self):
        return dict(watch_id=self.watch_id, open_expr=self.open_expr_s,
                    close_expr=self.close_expr_s, closed=self.closed)

    def _get(self, field, wid):
        vals = self.w.quote_values(wid if wid else self._row)
        return None if vals is None else vals.get(field)

    def _quote_price(self, side_dir):
        """挂单价：无空价→本方最优排队；有空价→本方价+1tick插队。"""
        q = self.w.get_quote(self.symbol)
        tick = clean(q.price_tick) or 1
        bid, ask = clean(q.bid_price1), clean(q.ask_price1)
        if side_dir == "BUY":
            if bid is None:
                return None if ask is None else round(ask - tick, 6)
            if ask is not None and ask - bid > tick * 1.01:
                return round(bid + tick, 6)
            return bid
        else:
            if ask is None:
                return None if bid is None else round(bid + tick, 6)
            if bid is not None and ask - bid > tick * 1.01:
                return round(ask - tick, 6)
            return ask

    def _sync_order(self):
        """同步在途委托的新增成交；死单清引用。返回新增成交手数。"""
        if self.order is None:
            return 0
        got = self._sync_fill(self._last_left)
        if self.order.alive:
            self._last_left = self.order.left
        else:
            self.order = None
            self._last_left = 0
        return got

    def _maintain(self, cond, side_dir, offset, want_vol, now, quoting_status, idle_status):
        """先同步成交，再按最新剩余量维护挂单(跟随重挂+限频)；公式失效立即撤。"""
        if not cond:
            if self.order is not None and self.order.alive:
                self.w.cancel_order(self.order)   # 公式失效撤单不受限频
            self.status = idle_status
            return
        self.status = quoting_status
        if want_vol <= 0:
            if self.order is not None and self.order.alive:
                self.w.cancel_order(self.order)
            return
        px = self._quote_price(side_dir)
        if px is None:
            return
        if self.order is None:
            self.order = self.w.place_order(self.symbol, side_dir, offset, want_vol, px,
                                            tag=self.algo_id)
            self._last_left = self.order.left
            self._last_quote_ts = now
        else:
            cur = order_price(self.order)
            if (cur is not None and abs(cur - px) > 1e-9
                    and now - self._last_quote_ts >= self.reprice_min_sec):
                self.w.cancel_order(self.order)   # 撤成后下轮以新价重挂
                self._last_quote_ts = now

    def step(self, now):
        if self.status in ("WAITING", "QUOTING_OPEN"):
            self.filled += self._sync_order()
            if self.filled >= self.volume:
                if self.order is not None and self.order.alive:
                    self.w.cancel_order(self.order)
                self.status = "HOLDING"
                self.msg = "开仓完成"
                self.w.log(f"算法 {self.algo_id} 开仓完成 {self.filled}手，转持仓")
                return
            cond = self.open_expr.eval(self._get) is True
            self._maintain(cond, self.direction, "OPEN",
                           self.volume - self.filled, now, "QUOTING_OPEN", "WAITING")
        elif self.status in ("HOLDING", "QUOTING_CLOSE"):
            self.closed += self._sync_order()
            if self.closed >= self.filled and self.filled > 0:
                if self.order is not None and self.order.alive:
                    self.w.cancel_order(self.order)
                self.status = "FILLED"
                self.msg = "开平全部完成"
                self.w.log(f"算法 {self.algo_id} 开平全部完成")
                return
            cond = self.close_expr.eval(self._get) is True
            side = "SELL" if self.direction == "BUY" else "BUY"
            self._maintain(cond, side, "CLOSE",
                           self.filled - self.closed, now, "QUOTING_CLOSE", "HOLDING")


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
def create_app(state: AppState, push_interval: float) -> FastAPI:
    app = FastAPI(title="跨期套利交易台")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.get("/")
    async def index():
        return FileResponse(WEB_FILE)

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/api/snapshot")
    async def snapshot():
        return Response(content=state.get_snapshot_json(), media_type="application/json")

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        import asyncio
        await sock.accept()
        try:
            while True:
                await sock.send_text(state.get_snapshot_json())
                await asyncio.sleep(push_interval)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def _json(req: Request):
        try:
            return await req.json()
        except Exception:
            return None

    @app.post("/api/watch")
    async def watch(req: Request):
        d = await _json(req)
        if not d or d.get("op") not in ("add", "del"):
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)
        if d["op"] == "add":
            if not d.get("leg_a"):
                return JSONResponse({"ok": False, "error": "缺少 leg_a"}, status_code=400)
            if d.get("kind") == "dual" and not d.get("leg_b"):
                return JSONResponse({"ok": False, "error": "缺少 leg_b"}, status_code=400)
            state.commands.put(dict(action="watch_add", kind=d.get("kind", "single"),
                                    leg_a=d["leg_a"], leg_b=d.get("leg_b")))
        else:
            state.commands.put(dict(action="watch_del", id=d.get("id")))
        return {"ok": True}

    @app.post("/api/order")
    async def order(req: Request):
        d = await _json(req)
        need = ("symbol", "direction", "volume", "limit_price")
        if not d or any(k not in d for k in need):
            return JSONResponse({"ok": False, "error": "缺少字段"}, status_code=400)
        state.commands.put(dict(action="order", **{k: d[k] for k in
                                                   (*need, "offset") if k in d}))
        return {"ok": True}

    @app.post("/api/algo")
    async def algo(req: Request):
        d = await _json(req)
        if not d:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)
        op = d.get("op")
        if op == "cancel":
            state.commands.put(dict(action="algo_cancel", algo_id=d.get("algo_id")))
        elif op == "clear_done":
            state.commands.put(dict(action="algo_clear_done"))
        elif op == "create":
            kind = d.get("kind", "single")
            if kind == "formula":
                # 公式在 Web 线程先行校验，错误立刻反馈给前端
                try:
                    SafeExpr(d.get("open_expr", ""))
                    SafeExpr(d.get("close_expr", ""))
                except ValueError as e:
                    return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            state.commands.put(dict(action="algo_create", **d))
        else:
            return JSONResponse({"ok": False, "error": f"未知 op {op}"}, status_code=400)
        return {"ok": True}

    @app.post("/api/cancel")
    async def cancel(req: Request):
        d = await _json(req) or {}
        state.commands.put(dict(action="cancel", order_id=d.get("order_id", "")))
        return {"ok": True}

    @app.post("/api/chase")
    async def chase(req: Request):
        d = await _json(req) or {}
        state.commands.put(dict(action="chase", order_id=d.get("order_id", ""),
                                ticks=int(d.get("ticks", 0))))
        return {"ok": True}

    @app.post("/api/opps_sync")
    async def opps_sync(req: Request):
        d = await _json(req) or {}
        opps = d.get("opps")
        if not isinstance(opps, list):
            return JSONResponse({"ok": False, "error": "opps 应为数组"}, status_code=400)
        state.commands.put(dict(action="opps_sync", opps=opps))
        return {"ok": True}

    return app


def main():
    import sys
    import uvicorn
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="跨期套利半自动交易台后端")
    ap.add_argument("--live", action="store_true", help="实盘（默认 dry-run 模拟成交）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    state = AppState()
    if args.live:
        state.log("⚠ 实盘模式：将发出真实委托！")
    worker = Worker(cfg, state, live=args.live)
    worker.start()
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    state.log(f"交易台: http://{host}:{port}")
    app = create_app(state, cfg["push_interval"])
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
