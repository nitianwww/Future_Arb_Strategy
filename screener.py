# -*- coding: utf-8 -*-
"""
套利机会筛选框架 —— 数据结构 / 策略接口 / 机会输出格式（第2部分地基）。

三层数据模型：
    Contract(合约)  →  Spread(单段价差=近月-远月)  →  Butterfly(蝶式=前段/后段)
统一约定：
    - 方向一律 近月 − 远月；比较一律用 bid/ask 实际可成交价。
    - 注销状态分组(cancel_state) 用于"考虑注销规则"的可比性约束。
    - 历史相关(分位数/夏普/季节性) 走 hist_* 接口，由第1部分历史库提供（现为桩）。
每套策略 = 一个函数 (Universe, params) -> List[Opportunity]，注册进 STRATEGIES。
机会统一输出 Opportunity，可转 DataFrame / CSV / HTML 提醒。
"""

from __future__ import annotations
import os
import re
import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict, Callable

import pandas as pd
import spread_monitor as sm

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Contract:
    symbol: str                 # DCE.m2609
    product: str                # 大写品种码 M
    exchange: str
    month: int                  # YYYYMM (可比整数)
    tick: float
    mult: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_vol: Optional[float] = None
    ask_vol: Optional[float] = None
    last: Optional[float] = None
    oi: Optional[float] = None
    days_to_expiry: Optional[int] = None   # 距到期日
    is_main: bool = False                  # 是否主力(品种内持仓量最大)
    is_cancel_month: bool = False          # 该合约月是否属于品种注销月集合
    roll_over: str = ""                    # 转抛 Y/N


@dataclass
class Spread:
    """单段价差 = near − far（near 月份更近）。价差盘口优先组合合约，否则两腿合成。"""
    near: Contract
    far: Contract
    bid: Optional[float] = None        # 卖价差(近bid−远ask 或 组合bid)
    ask: Optional[float] = None        # 买价差(近ask−远bid 或 组合ask)
    bid_vol: Optional[float] = None    # 卖出价差可成交量(两腿取小 或 组合bid量)
    ask_vol: Optional[float] = None    # 买入价差可成交量
    last: Optional[float] = None       # 近last−远last
    source: str = "synth"              # 'combo' | 'synth'
    combo_symbol: Optional[str] = None
    abs_price: Optional[float] = None  # 绝对价格(归一化用)= 近月最新价
    days_to_expiry: Optional[int] = None    # 以近月为准
    involves_main: bool = False        # 任一腿为主力
    cancel_state: str = "none"         # both/near/far/none（前后都注销/仅近/仅远/都不注销）
    fullcarry: Optional[float] = None  # 满仓持有成本(近→远, 含时间贴水)
    decay: float = 0.0                 # 特殊时间贴水成本(如郑棉旧棉日贴水)
    months: Optional[int] = None       # 跨期月份差
    adjacent: bool = True              # 是否相邻上市月份对

    @property
    def name(self):
        return f"{sm.code_of(self.near.symbol)}-{sm.code_of(self.far.symbol)}"

    @property
    def mid(self):
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass
class Butterfly:
    """蝶式 = 后段 − 前段。前段=m1-m2，后段=m2-m3。value>0 表示曲线上凸(折角)。"""
    front: Spread                # m1-m2
    back: Spread                 # m2-m3
    value: Optional[float] = None        # 后段.mid − 前段.mid
    value_norm: Optional[float] = None   # value / 绝对价格
    abs_price: Optional[float] = None

    @property
    def name(self):
        return f"{sm.code_of(self.front.near.symbol)}-{sm.code_of(self.front.far.symbol)}-{sm.code_of(self.back.far.symbol)}"


@dataclass
class Opportunity:
    """筛选命中的机会，统一输出格式。"""
    ts: str
    strategy: str
    kind: str                    # 'spread' | 'butterfly'
    product: str
    structure: str               # 结构名，如 m2609-m2611 或 m2609-m2611-m2701
    direction: str               # 做多价差/做空价差/做多蝶式/做空蝶式
    score: float                 # 触发强度(归一化值/分位/夏普/σ，越大越强)
    conditions: dict = field(default_factory=dict)   # 各条件实际值
    tradeable: str = ""          # 建议交易的组合合约(如有)
    hold_period: Optional[int] = None                # 建议持有期(季节性策略返回)
    suggest: dict = field(default_factory=dict)      # 可入 strategy_config: {fair,qty,direction,...}
    detail: str = ""
    liquid: bool = True          # 腿OI达标(不达标->钓鱼单单列)
    flag: str = ""               # 排除性标注: 规则断层/低流动性/现金交割特殊 等
    rollable: bool = False       # 可转抛机会: 价差类+正套方向+区间无注销月+近月未进交割月
    curve: str = ""              # 品种当下期限结构: back/contango/分歧(同特性月份分组回归判定)
    slope_pctl: Optional[float] = None   # 期限斜率的历史同期分位%
    book: Optional[tuple] = None         # 标的段盘口 (买价,买量,卖价,卖量); spread=本段, kink=B段
    warrant_pctl: Optional[float] = None  # 仓单历年同期分位%(spread类佐证维度)


# --------------------------------------------------------------------------- #
# 注销状态 与 可比性（"考虑注销规则"）
# --------------------------------------------------------------------------- #
def base_product(symbol: str) -> str:
    """基础品种码=合约代码前导字母(大写)。兼容F后缀新标准品: v2609F -> V (product_of 会给 VF)。"""
    return re.match(r"^[A-Za-z]+", sm.code_of(symbol)).group().upper()


def cancel_state(near: Contract, far: Contract) -> str:
    n, f = near.is_cancel_month, far.is_cancel_month
    return {(True, True): "both", (True, False): "near",
            (False, True): "far", (False, False): "none"}[(n, f)]


def cancels_between(near_month, far_month, cancel_set) -> int:
    """区间 (near, far) 严格内部(不含两端)的注销月个数。
    例: PS(注销5,11) 08-10 之间{9}→0；10-12 之间{11}→1 —— 跨不跨注销月本质不同，不可对比。"""
    if not cancel_set:
        return 0
    n, m = 0, sm._next_month(near_month)
    while m < far_month:
        if (m % 100) in cancel_set:
            n += 1
        m = sm._next_month(m)
    return n


def comparable(a: Spread, b: Spread, cancel_set=None) -> bool:
    """两段价差是否可直接对比：同注销状态 且 区间内跨注销月数相同。"""
    if a.cancel_state != b.cancel_state:
        return False
    if cancel_set is not None:
        if cancels_between(a.near.month, a.far.month, cancel_set) != \
           cancels_between(b.near.month, b.far.month, cancel_set):
            return False
    return True


def _cancel_bias(sp: Spread) -> int:
    """段的注销结构偏置(用户2026-08-12 同构放松口径)：
    仅远腿注销 → +1(远腿应折价 → 段应结构性偏高)；
    仅近腿注销 → -1(近腿应折价 → 段应结构性偏低)；both/none → 0。
    用途：非同构组合以同构插值为基准，结构偏置与信号方向一致(比同构更有利)才输出；
    此时插值/零基准是保守下限。注意跨注销数(cancels_between)不在此放松——
    跨注销断链只解释偏离(套利上限失效)，方向不定，仍要求相同。"""
    if sp.cancel_state == "far":
        return 1
    if sp.cancel_state == "near":
        return -1
    return 0


# --------------------------------------------------------------------------- #
# 历史数据接口（第1部分提供；现为桩）
# --------------------------------------------------------------------------- #
def hist_spread_series(product: str, near_month: int, far_month: int,
                       mode: str = "同期", years: int = 10) -> Optional[pd.Series]:
    """返回该段价差的历史序列（index=日期, value=价差）。mode: 同期 / 全期。
    第1部分历史库落地后实现；现返回 None，依赖历史的策略据此跳过或降级。"""
    return None


def hist_percentile(product, near_month, far_month, current, mode="全期", years=10):
    s = hist_spread_series(product, near_month, far_month, mode, years)
    if s is None or len(s) == 0:
        return None
    return float((s < current).mean())


def hist_best_sharpe(product, near_month, far_month, side="long", max_hold=60):
    """遍历持有期求最优夏普，返回 (best_sharpe, best_hold)。桩：无历史返回 (None,None)。"""
    return (None, None)


# --------------------------------------------------------------------------- #
# Universe 构建（实时 + 配置）
# --------------------------------------------------------------------------- #
@dataclass
class Universe:
    ts: str
    contracts: Dict[str, Contract] = field(default_factory=dict)   # symbol -> Contract
    spreads: List[Spread] = field(default_factory=list)
    butterflies: List[Butterfly] = field(default_factory=list)
    by_product: Dict[str, List[Contract]] = field(default_factory=dict)   # 品种 -> 按月排序合约
    meta: Dict[str, dict] = field(default_factory=dict)                   # 品种配置(注销/仓储/贴水)
    spread_by_pair: Dict[tuple, "Spread"] = field(default_factory=dict)   # (near_sym,far_sym)->Spread


def _fullcarry(near_price, fee, months, rate):
    if near_price is None or not months:
        return None
    cap = 1.1 * near_price * (rate / 100.0) * (months / 12.0)
    stor = (fee or 0.0) * 30 * months
    return round(cap + stor, 4)


def build_universe(api, meta, products=None, exchanges=None, max_months=8,
                   funding_rate=6.0, all_pairs=False) -> Universe:
    """从实时行情 + 品种配置(meta) 构建三层数据模型。meta = spread_monitor.load_product_meta 结果。"""
    exchanges = exchanges or sm.FUTURE_EXCHANGES
    products = set(p.upper() for p in products) if products else None

    # 1) 合约发现
    prod_syms: Dict[str, List[str]] = {}
    combo_map = {}
    combo_total, combo_rejects = 0, []
    for ex in exchanges:
        for s in api.query_quotes(ins_class="FUTURE", exchange_id=ex, expired=False) or []:
            if sm.is_settle_f(s):
                continue                     # F结算价合约全系统排除
            p = sm.product_of(s).upper()
            if products and p not in products:
                continue
            prod_syms.setdefault(p, []).append(s)
        for c in api.query_quotes(ins_class="COMBINE", exchange_id=ex, expired=False) or []:
            parsed = sm.parse_combine(c)
            if parsed:
                combo_map[(parsed[2], parsed[3])] = c
            else:
                combo_rejects.append(c)
            combo_total += 1

    # 2) 订阅盘口
    all_syms = [s for ss in prod_syms.values() for s in ss] + list(combo_map.values())
    qd = {q.instrument_id: q for q in api.get_quote_list(all_syms)}
    for _ in range(6):
        api.wait_update(deadline=__import__("time").time() + 0.4)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    U = Universe(ts=ts, meta=meta)

    # 3) Contract
    for p, syms in prod_syms.items():
        ordered = sorted(set(syms), key=sm.month_key)
        syms = ordered[:max_months]
        base = base_product(ordered[0])       # F后缀合约(VF/LF/PPF)映射回基础品种取配置
        if base in MAIN_CYCLE_159:            # 159主力品种: 窗口外的1/5/9月合约也纳入(主力循环价差用)
            extra = [s for s in ordered[max_months:] if sm.month_key(s) % 100 in (1, 5, 9)]
            syms = sorted(set(syms + extra), key=sm.month_key)[:12]
        m = meta.get(base, {})
        # 主力 = 持仓量最大
        main, main_oi = None, -1
        for s in syms:
            q = qd.get(s)
            oi = sm.clean(getattr(q, "open_interest", None)) if q else None
            if oi is not None and oi > main_oi:
                main_oi, main = oi, s
        cons = []
        for s in syms:
            q = qd.get(s)
            mm = sm.month_key(s) % 100
            c = Contract(
                symbol=s, product=base, exchange=s.split(".")[0], month=sm.month_key(s),
                tick=float(getattr(q, "price_tick", 1) or 1), mult=float(getattr(q, "volume_multiple", 1) or 1),
                bid=sm.clean(q.bid_price1) if q else None, ask=sm.clean(q.ask_price1) if q else None,
                bid_vol=sm.clean(getattr(q, "bid_volume1", None)) if q else None,
                ask_vol=sm.clean(getattr(q, "ask_volume1", None)) if q else None,
                last=sm.clean(q.last_price) if q else None, oi=sm.clean(getattr(q, "open_interest", None)) if q else None,
                days_to_expiry=int(getattr(q, "expire_rest_days", 0) or 0) if q else None,
                is_main=(s == main), is_cancel_month=(mm in m.get("cancel", set())),
                roll_over=m.get("roll", ""))
            U.contracts[s] = c
            cons.append(c)
        U.by_product[p] = cons

    # 4) Spread（相邻 + 蝶式所需的连续对）
    for p, cons in U.by_product.items():
        pm = meta.get(cons[0].product, {})               # cons[0].product 已是基础品种
        fee, dw = pm.get("fee"), pm.get("decay")
        for i in range(len(cons) - 1):
            near, far = cons[i], cons[i + 1]
            sp = _make_spread(near, far, combo_map, qd, fee, funding_rate, dw)
            U.spreads.append(sp)
            U.spread_by_pair[(near.symbol, far.symbol)] = sp
        if all_pairs:                                    # 非相邻月份对(同构对比等用)
            for i in range(len(cons)):
                for j in range(i + 2, len(cons)):
                    near, far = cons[i], cons[j]
                    sp = _make_spread(near, far, combo_map, qd, fee, funding_rate, dw)
                    sp.adjacent = False
                    U.spreads.append(sp)
                    U.spread_by_pair[(near.symbol, far.symbol)] = sp
        # 5) Butterfly（连续三月）
        prod_spreads = {(sp.near.symbol, sp.far.symbol): sp
                        for sp in U.spreads if sp.near.product == cons[0].product}
        for i in range(len(cons) - 2):
            f = prod_spreads.get((cons[i].symbol, cons[i + 1].symbol))
            b = prod_spreads.get((cons[i + 1].symbol, cons[i + 2].symbol))
            if f and b:
                U.butterflies.append(_make_butterfly(f, b))

    stats_name = "universe_stats_allpairs.json" if all_pairs else "universe_stats.json"
    _sanity_check(U, meta, combo_total, combo_rejects,
                  os.path.join(HERE, "output", stats_name),
                  filtered=bool(products))
    return U


def _sanity_check(U, meta, combo_total, combo_rejects, stats_path, filtered=False):
    """启动自检：品种映射/月份排序/组合解析/快照突变。只告警不阻断。
    filtered=True(限品种构建)时跳过快照对比且不写基线，防污染。"""
    warns = []
    # 1) 品种映射完整性：每个合约的基础品种都应在配置表里(新品种上市/解析错误第一时间暴露)
    missing = sorted({c.product for c in U.contracts.values() if c.product not in meta})
    if missing:
        warns.append(f"品种不在配置表: {missing} —— 新品种上市? 代码解析错误?")
    # 2) 月份解析交叉验证：month_key 排序应与到期天数排序一致(专治3位码跨十年)
    for p, cons in U.by_product.items():
        days = [c.days_to_expiry for c in cons]
        if any(d is None or d <= 0 for d in days):
            continue
        bad = [(sm.code_of(cons[i].symbol), sm.code_of(cons[i + 1].symbol))
               for i in range(len(days) - 1) if days[i] >= days[i + 1]]
        if bad:
            warns.append(f"{p} 月份排序与到期天数矛盾: {bad} —— 月份码解析可疑")
    # 3) 组合解析失败率(跨品种SPC等本应剔除, 只在异常高/全灭时告警)
    if combo_total:
        rej_rate = len(combo_rejects) / combo_total
        if len(combo_rejects) == combo_total or rej_rate > 0.8:
            warns.append(f"组合解析剔除率异常 {rej_rate:.0%} ({len(combo_rejects)}/{combo_total})"
                         f" 样本: {combo_rejects[:3]} —— 交易所改组合命名?")
    # 4) 快照体检：关键计数与上次对比, 突变±30%告警
    cur = {"contracts": len(U.contracts), "spreads": len(U.spreads),
           "combos_mapped": sum(1 for s in U.spreads if s.combo_symbol),
           "no_book": sum(1 for s in U.spreads if s.bid is None or s.ask is None)}
    prev = None
    if not filtered:
        try:
            prev = json.load(open(stats_path, encoding="utf-8"))
            for k, v in cur.items():
                pv = prev.get(k)
                if pv and (v > pv * 1.3 or v < pv * 0.7):
                    warns.append(f"快照突变 {k}: {pv} -> {v} —— 上游数据源/时段/解析检查")
        except Exception:
            pass
        try:
            os.makedirs(os.path.dirname(stats_path), exist_ok=True)
            json.dump(dict(cur, ts=U.ts, combo_total=combo_total,
                           combo_rejects=len(combo_rejects)),
                      open(stats_path, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            print(f"[自检] 统计写入失败: {e}")
    for w in warns:
        print(f"[自检警告] {w}")
    if not warns:
        base = "首次基线" if prev is None else "对比上次"
        print(f"[自检] 通过({base}): {cur}  组合剔除 {len(combo_rejects)}/{combo_total}(跨品种等)")
    return warns


def _make_spread(near, far, combo_map, qd, fee, rate, decay_windows=None) -> Spread:
    sp = Spread(near=near, far=far)
    sp.months = sm.month_diff(near.symbol, far.symbol)
    sp.abs_price = near.last
    sp.days_to_expiry = near.days_to_expiry
    sp.involves_main = near.is_main or far.is_main
    sp.cancel_state = cancel_state(near, far)
    sp.decay = sm.decay_cost(near.month, far.month, decay_windows)
    sp.fullcarry = _fullcarry(near.last, fee, sp.months, rate)
    if sp.fullcarry is not None:
        sp.fullcarry = round(sp.fullcarry + sp.decay, 4)   # fullcarry 含时间贴水
    sp.last = None if (near.last is None or far.last is None) else round(near.last - far.last, 6)
    # 组合优先
    combo = combo_map.get((near.symbol, far.symbol)) or combo_map.get((far.symbol, near.symbol))
    if combo:
        q = qd.get(combo)
        orient = 1 if (near.symbol, far.symbol) in combo_map else -1
        cb, ca = sm.clean(getattr(q, "bid_price1", None)), sm.clean(getattr(q, "ask_price1", None))
        cbv, cav = sm.clean(getattr(q, "bid_volume1", None)), sm.clean(getattr(q, "ask_volume1", None))
        if orient >= 0:
            sp.bid, sp.ask = cb, ca
            sp.bid_vol, sp.ask_vol = cbv, cav
        else:
            sp.bid = None if ca is None else round(-ca, 6)
            sp.ask = None if cb is None else round(-cb, 6)
            sp.bid_vol, sp.ask_vol = cav, cbv
        sp.source, sp.combo_symbol = "combo", combo
    else:  # 合成
        sp.bid = None if (near.bid is None or far.ask is None) else round(near.bid - far.ask, 6)   # 卖价差
        sp.ask = None if (near.ask is None or far.bid is None) else round(near.ask - far.bid, 6)   # 买价差
        _vmin = lambda a, b: None if (a is None or b is None) else min(a, b)
        sp.bid_vol = _vmin(near.bid_vol, far.ask_vol)     # 卖价差: 近腿打bid+远腿打ask
        sp.ask_vol = _vmin(near.ask_vol, far.bid_vol)
        sp.source = "synth"
    # 最新价差必须被限制在买/卖价之间（与 spread_monitor 口径一致）
    if sp.last is not None and sp.bid is not None and sp.ask is not None:
        lo, hi = min(sp.bid, sp.ask), max(sp.bid, sp.ask)
        sp.last = round(min(max(sp.last, lo), hi), 6)
    return sp


def _make_butterfly(front: Spread, back: Spread) -> Butterfly:
    bf = Butterfly(front=front, back=back, abs_price=front.abs_price)
    if front.mid is not None and back.mid is not None:
        bf.value = round(back.mid - front.mid, 6)
        if front.abs_price:
            bf.value_norm = round(bf.value / front.abs_price, 6)
    return bf


# --------------------------------------------------------------------------- #
# 策略接口 + 参数
# --------------------------------------------------------------------------- #
# 每策略参数（可逐品种覆盖：PARAMS[策略][品种] 覆盖 PARAMS[策略]['*']）
PARAMS: Dict[str, Dict] = {
    "折角修复": {"*": {"N_norm": 0.003}},         # 蝶式/绝对价 阈值
    "主力合约折角修复": {"*": {"N_norm": 0.003, "main_days": 30}},
    "注销月折价": {"*": {}},
    # 季节性统计: pct=同期分位阈值 sharpe=夏普阈值 resid_z=残差过滤阈值(价差~斜率+到期天数回归)
    "价差季节性统计套利(正套)": {"*": {"resid_z": 1.5}},
    "价差季节性统计套利(反套)": {"*": {"resid_z": 1.5}},
    # 远期极值分支(2026-08-12): far_days=很远期门槛(V2701约155天,故取120而非远期口径180);
    # peer_lo/hi=同期分位极值; peer_min_years=同期样本最少年数
    "价差统计套利": {"*": {"far_days": 120, "peer_lo": 10.0, "peer_hi": 90.0,
                          "peer_min_years": 5}},
    # ... 其余策略参数后续补
}


def _p(strategy, product, key, default=None):
    d = PARAMS.get(strategy, {})
    return d.get(product, {}).get(key, d.get("*", {}).get(key, default))


LIQ_MIN_OI = 5000        # (旧口径,已弃用为过滤条件,仅报告展示参考) 腿持仓量下限

# 流动性新口径(用户2026-08-12): 不再限制单腿OI——远月腿天然低OI会误杀范式二/四的
# 目标段(V 1-5、J 3-4)。改看**价差合成盘口的空隙**: (买价差-卖价差), 组合合约盘口优先
# 否则双腿合成, 折算相对主力合约价格的比例; 比例>阈值 或 盘口缺边 → 低流动性(钓鱼)。
LIQ_MAX_GAP_RATIO = 0.003    # 空隙>主力价0.3% → 钓鱼(可调)


def spread_gap(sp: Spread) -> Optional[float]:
    """价差合成盘口空隙 = 买价差 − 卖价差(ask−bid), 交叉盘口按0; 缺边 → None。"""
    if sp.bid is None or sp.ask is None:
        return None
    return max(0.0, sp.ask - sp.bid)

# A类=纯实时可跑的策略（C类统计策略待历史库）
A_STRATEGIES = ["折角修复", "主力合约折角修复", "注销月折价", "价差递延",
                "注销后首月溢价", "远期高低估", "近月转抛", "远月转抛", "同构价差对比"]

# 合约规则断层：跨越该边界的价差/蝶式结构性偏移，不是错价（用户确认逐条登记）。
# 处理(2026-08-12用户口径): 跨断层机会不一律排除——新标准腿(>=断层月)全为多头则保留
# (新标准交割品更优,只做多不做空), 含空新标准腿则剔除; 见 _break_verdict/_annotate。
REGIME_BREAKS: Dict[str, list] = {
    "JM": [(202701, "jm2701起交割标准大幅提高，跨2612/2701的结构偏移为正常")],
}

# 现金交割类：结构反映预期而非仓单套利，仅提示不交易。
# 注意：股指(IF/IC/IH/IM)虽现金交割，但可交易/可转抛(移仓吃基差)，不列入；见 DIVIDEND_PRODUCTS。
ADVISORY_ONLY = {"EC", "T", "TF", "TL", "TS"}

# 股指期货：现金交割、无仓单/注销月，可跨期套利与移仓转抛。
# 分红逻辑：成分股分红集中在 3-9 月(年报分红),远月合约提前贴现分红→近−远价差在该窗口偏大，
# 属季节性正常而非错价。跨该窗口的价差用「同期分位」比较才可比(季节性/统计类已如此)；
# 纯资金carry口径(转抛/远期高低估)未扣分红，该窗口的比例会被高估 —— 命中时打标提示。
DIVIDEND_PRODUCTS = {"IF", "IC", "IH", "IM"}
DIVIDEND_MONTHS = set(range(3, 10))   # 3-9月

# 主力按 1-5-9 循环的品种：1-5/5-9/9-1 主力循环价差纳入同构对比池（可按需增删）
MAIN_CYCLE_159 = {"M", "Y", "A", "C", "CS", "L", "PP", "V", "EG",
                  "RM", "OI", "CF", "SR", "TA", "MA", "FG", "SA"}

StrategyFn = Callable[[Universe, str], List[Opportunity]]
STRATEGIES: Dict[str, StrategyFn] = {}


def strategy(name):
    def deco(fn):
        STRATEGIES[name] = fn
        return fn
    return deco


# --------------------------------------------------------------------------- #
# 参考策略：折角修复（A类·纯实时，验证框架）
# --------------------------------------------------------------------------- #
@strategy("折角修复")
def s_kink_repair(U: Universe, name="折角修复") -> List[Opportunity]:
    """新口径(用户2026-08-10, 替代原蝶式口径; 2026-08-12 同构放松):
    0. 同一品种找三个价差段 A/B/C(月差相同+区间跨注销数相同+近月等距);
       注销状态不再要求完全一致: 同构(三段状态相同)直接输出; 非同构以同构插值为
       基准, 注销偏置 E=bias(B)−(bias(A)+bias(C))/2 与信号同向(结构比同构更有利,
       插值即保守下限)才输出, 反向/中性(E=0)不输出;
    1. 三段互不重复(A/B/C 为不同段);
    2. B 为时间中段, 且 (B<=A 且 B<=C) 或 (B>=A 且 B>=C) —— 折角;
    3. 理论B = (A+C)/2, 输出 价差=理论B−实际B;
    排序: |价差| / 主力合约最新价 的比例, 倒序(run_screen 按 score 全局倒序)。
    无最小阈值过滤, 只按 0/1/2 过滤后全量输出。"""
    out = []
    byprod: Dict[str, list] = {}
    for sp in U.spreads:
        if sp.mid is None or not _both_sides(sp):
            continue
        byprod.setdefault(sp.near.product, []).append(sp)
    for prod, sps in byprod.items():
        cancel = U.meta.get(prod, {}).get("cancel", set())
        main_px = next((c.last for c in U.by_product.get(prod, [])
                        if c.is_main and c.last), None)
        groups: Dict[tuple, list] = {}
        for sp in sps:
            key = (sp.months,
                   cancels_between(sp.near.month, sp.far.month, cancel))
            groups.setdefault(key, []).append(sp)
        for g in groups.values():
            if len(g) < 3:
                continue
            g = sorted(g, key=lambda s: s.near.month)
            mi = [s.near.month // 100 * 12 + s.near.month % 100 for s in g]
            for i in range(len(g) - 2):
                for j in range(i + 1, len(g) - 1):
                    for k in range(j + 1, len(g)):
                        if mi[j] - mi[i] != mi[k] - mi[j]:
                            continue                    # 近月不等距
                        A, B, C = g[i], g[j], g[k]
                        if not (A.far.month <= B.near.month and B.far.month <= C.near.month):
                            continue                    # 三段区间交叉(用户要求完全不交叉,首尾相接允许)
                        if not ((B.mid <= A.mid and B.mid <= C.mid)
                                or (B.mid >= A.mid and B.mid >= C.mid)):
                            continue                    # B 非局部极值
                        theo = (A.mid + C.mid) / 2
                        diff = theo - B.mid
                        if diff == 0:
                            continue                    # 恰在直线上, 无折角
                        homo = A.cancel_state == B.cancel_state == C.cancel_state
                        if not homo:
                            E = _cancel_bias(B) - (_cancel_bias(A) + _cancel_bias(C)) / 2
                            if not ((diff > 0 and E > 0) or (diff < 0 and E < 0)):
                                continue                # 结构不有利于信号方向 → 不输出
                        ratio = (abs(diff) / main_px) if main_px else None
                        out.append(Opportunity(
                            ts=U.ts, strategy=name, kind="kink", product=prod,
                            structure=f"{A.name} | {B.name} | {C.name}",
                            direction="做多B段(实际低于理论)" if diff > 0
                                      else "做空B段(实际高于理论)",
                            score=round(ratio, 6) if ratio is not None else round(abs(diff), 4),
                            conditions={"A": A.mid, "B": B.mid, "C": C.mid,
                                        "理论B=(A+C)/2": round(theo, 2),
                                        "价差(理论-实际)": round(diff, 2),
                                        "主力价": main_px,
                                        "比例%": round(ratio * 100, 4) if ratio is not None else None,
                                        "同构性": "同构" if homo else
                                        f"非同构({A.cancel_state}/{B.cancel_state}/{C.cancel_state},"
                                        "结构有利,插值为保守下限)"},
                            tradeable=B.combo_symbol or "",
                            detail=f"月差{g[0].months} {'同构' if homo else '非同构-结构有利'}"))
    return out


@strategy("注销月折价")
def s_cancel_discount(U: Universe, name="注销月折价") -> List[Opportunity]:
    """中间为注销月、前后非注销 → 前段<0 且 后段>0；或 中间非注销、前后注销 → 前段>0 且 后段<0。"""
    out = []
    for bf in U.butterflies:
        f, b = bf.front, bf.back
        if f.mid is None or b.mid is None:
            continue
        mid_c = f.far  # 中间月 = 前段的远腿 = 后段的近腿
        pre, post = f.mid, b.mid
        c1 = mid_c.is_cancel_month and not f.near.is_cancel_month and not b.far.is_cancel_month and pre < 0 and post > 0
        c2 = (not mid_c.is_cancel_month) and f.near.is_cancel_month and b.far.is_cancel_month and pre > 0 and post < 0
        if c1 or c2:
            out.append(Opportunity(
                ts=U.ts, strategy=name, kind="butterfly", product=f.near.product,
                structure=bf.name, direction="做多前段/做空后段" if c1 else "做空前段/做多后段",
                score=round(abs(pre) + abs(post), 4),
                conditions={"中间注销": mid_c.is_cancel_month, "前段": pre, "后段": post},
                detail=f"注销月折价 {'中间注销' if c1 else '前后注销'}"))
    return out


def _both_sides(sp: Spread) -> bool:
    """双边盘口才有效（盘口效力原则）。"""
    return sp.bid is not None and sp.ask is not None


@strategy("主力合约折角修复")
def s_main_kink(U: Universe, name="主力合约折角修复") -> List[Opportunity]:
    """蝶式>N + 前后段符号相反 + 中间月=主力且距到期<=main_days + 同注销状态。"""
    out = []
    for bf in U.butterflies:
        if bf.value_norm is None or bf.front.mid is None or bf.back.mid is None:
            continue
        if not (_both_sides(bf.front) and _both_sides(bf.back)):
            continue
        prod = bf.front.near.product
        N = _p(name, prod, "N_norm", 0.003)
        mid_c = bf.front.far                      # 中间月
        cond = (abs(bf.value_norm) > N and bf.front.mid * bf.back.mid <= 0
                and mid_c.is_main and (mid_c.days_to_expiry or 999) <= _p(name, prod, "main_days", 30)
                and comparable(bf.front, bf.back, U.meta.get(prod, {}).get("cancel")))
        if cond:
            out.append(Opportunity(
                ts=U.ts, strategy=name, kind="butterfly", product=prod,
                structure=bf.name, direction="做空蝶式" if bf.value > 0 else "做多蝶式",
                score=round(abs(bf.value_norm), 6),
                conditions={"蝶式/绝对价": bf.value_norm, "中间月": sm.code_of(mid_c.symbol),
                            "主力距到期": mid_c.days_to_expiry},
                detail=f"主力{sm.code_of(mid_c.symbol)}临近到期的折角"))
    return out


@strategy("价差递延")
def s_deferral(U: Universe, name="价差递延") -> List[Opportunity]:
    """同构分支: (后段-前段)/绝对价 > N + 同注销状态可比（不要求符号相反）。
    非同构分支(用户2026-08-12, 范式一b): 共中腿+等月差的泛化段对(如 I 1-3 vs 3-5,
    不限连续挂牌月, U.butterflies 之外自行枚举), 跨注销数相同但注销状态不同时,
    注销偏置给出结构性不等式基准: E=bias(后段)−bias(前段), E<0 → 后段理应低于前段
    (理论蝶值<0)。实际蝶值落在 0 或错侧即违背 → 按结构方向输出, |实际−0| 为保守
    错价下限(真实理论值在 0 之外更远处)。例: I(3月注销) 1-3(far注销,+1) vs
    3-5(near注销,-1) → E=-2, 后段应远低于前段, 实际≈持平 → 卖后段买前段。"""
    out = []
    for bf in U.butterflies:
        if bf.value_norm is None:
            continue
        if not (_both_sides(bf.front) and _both_sides(bf.back)):
            continue
        N = _p(name, bf.front.near.product, "N_norm", 0.005)
        if abs(bf.value_norm) > N and comparable(bf.front, bf.back, U.meta.get(bf.front.near.product, {}).get("cancel")):
            out.append(Opportunity(
                ts=U.ts, strategy=name, kind="butterfly", product=bf.front.near.product,
                structure=bf.name, direction="做空蝶式(卖后段买前段)" if bf.value > 0 else "做多蝶式(买后段卖前段)",
                score=round(abs(bf.value_norm), 6),
                conditions={"(后段-前段)/绝对价": bf.value_norm,
                            "前段": bf.front.mid, "后段": bf.back.mid,
                            "注销状态": bf.front.cancel_state}))
    # ---- 非同构结构性不等式分支(泛化蝶式: 共中腿+等月差, 含非连续挂牌月) ----
    byprod: Dict[str, list] = {}
    for sp in U.spreads:
        if sp.mid is None or not _both_sides(sp) or not sp.months:
            continue
        if not sp.abs_price:
            continue                            # 无法归一化的段不进比例制排序(防raw点数霸榜)
        if sm.in_delivery_month(sp.near.month, sp.near.product):
            continue
        byprod.setdefault(sp.near.product, []).append(sp)
    for prod, sps in byprod.items():
        cancel = U.meta.get(prod, {}).get("cancel", set())
        by_near: Dict[str, list] = {}
        for sp in sps:
            by_near.setdefault(sp.near.symbol, []).append(sp)
        for front in sps:
            for back in by_near.get(front.far.symbol, []):
                if back.months != front.months:
                    continue                            # 等月差才有公平的 0 基准
                if front.cancel_state == back.cancel_state:
                    continue                            # 同构走上面的常规分支
                if cancels_between(front.near.month, front.far.month, cancel) != \
                   cancels_between(back.near.month, back.far.month, cancel):
                    continue                            # 跨注销数不同不放松(方向不定)
                E = _cancel_bias(back) - _cancel_bias(front)
                if E == 0:
                    continue                            # 结构无方向 → 不输出
                value = back.mid - front.mid
                if not ((E < 0 and value >= 0) or (E > 0 and value <= 0)):
                    continue                            # 未违背结构性不等式
                absp = front.abs_price
                vnorm = (value / absp) if absp else None
                struct = (f"{sm.code_of(front.near.symbol)}-{sm.code_of(front.far.symbol)}"
                          f"-{sm.code_of(back.far.symbol)}")
                out.append(Opportunity(
                    ts=U.ts, strategy=name, kind="butterfly", product=prod,
                    structure=struct,
                    direction=("做空蝶式(卖后段买前段,结构应后段<前段)" if E < 0
                               else "做多蝶式(买后段卖前段,结构应后段>前段)"),
                    score=round(abs(vnorm), 6) if vnorm is not None else round(abs(value), 4),
                    conditions={"前段": front.mid, "后段": back.mid,
                                "蝶值(后-前)": round(value, 2),
                                "结构基准E": E,
                                "同构性": f"非同构({front.cancel_state}/{back.cancel_state}),"
                                          "结构性不等式违背,|蝶值|为保守错价下限"},
                    detail=f"泛化蝶式 月差{front.months}+{back.months}"))
    return out


@strategy("注销后首月溢价")
def s_post_cancel_premium(U: Universe, name="注销后首月溢价") -> List[Opportunity]:
    """前月为注销月时 后月-后后月 价差<0 + 非显著Contango(期限曲线斜率不显著<0)。"""
    import numpy as np
    out = []
    for p, cons in U.by_product.items():
        # 期限曲线斜率显著性
        pts = [(i, c.last) for i, c in enumerate(cons) if c.last]
        slope, tstat = None, 0.0
        if len(pts) >= 4:
            x = np.array([a for a, _ in pts], float); y = np.array([b for _, b in pts], float)
            n = len(x)
            sxx = ((x - x.mean()) ** 2).sum()
            slope = ((x - x.mean()) * (y - y.mean())).sum() / sxx
            resid = y - (y.mean() + slope * (x - x.mean()))
            se = (resid @ resid / max(n - 2, 1) / sxx) ** 0.5
            tstat = slope / se if se > 0 else 0.0
        significant_contango = slope is not None and slope < 0 and tstat < -2
        if significant_contango:
            continue
        for i in range(len(cons) - 2):
            m1, m2, m3 = cons[i], cons[i + 1], cons[i + 2]
            if not m1.is_cancel_month or m2.is_cancel_month:
                continue        # 前月注销且首月非注销(每月注销品种该条件退化,排除)
            sp = U.spread_by_pair.get((m2.symbol, m3.symbol))
            if sp is None or sp.mid is None or not _both_sides(sp):
                continue
            if sp.mid < 0:                       # 注销后首月反而贴水 -> 异常
                out.append(Opportunity(
                    ts=U.ts, strategy=name, kind="spread", product=p,
                    structure=sp.name, direction="做多价差(买注销后首月卖次月)",
                    score=round(abs(sp.mid) / (sp.abs_price or 1), 6),
                    conditions={"注销月": sm.code_of(m1.symbol), "价差": sp.mid,
                                "期限斜率t": round(tstat, 2)},
                    tradeable=sp.combo_symbol or ""))
    return out


@strategy("远期高低估")
def s_forward_mispricing(U: Universe, name="远期高低估") -> List[Opportunity]:
    """同注销状态的价差(按每月每元归一)截面对比，|z|>N 且 前腿到期>D。
    远期定义(用户口径)：前腿(近腿)到期 6 个月(180天)以上。"""
    out = []
    by_key: Dict[tuple, list] = {}
    for sp in U.spreads:
        if (sp.mid is None or not sp.months or not sp.abs_price or not _both_sides(sp)
                or not sp.days_to_expiry):
            continue
        D = _p(name, sp.near.product, "min_days", 180)
        if sp.days_to_expiry <= D:
            continue
        norm = sp.mid / sp.months / sp.abs_price     # 每月carry率
        by_key.setdefault((sp.near.product, sp.cancel_state), []).append((sp, norm))
    for (prod, state), items in by_key.items():
        if len(items) < 4:
            continue
        import statistics
        vals = [v for _, v in items]
        mu, sd = statistics.mean(vals), statistics.stdev(vals)
        if sd <= 0:
            continue
        N = _p(name, prod, "z", 2.0)
        for sp, v in items:
            z = (v - mu) / sd
            if abs(z) > N:
                out.append(Opportunity(
                    ts=U.ts, strategy=name, kind="spread", product=prod,
                    structure=sp.name, direction="做空价差(高估)" if z > 0 else "做多价差(低估)",
                    score=round(abs(z), 3),
                    conditions={"z": round(z, 2), "每月carry率": round(v, 6),
                                "组均值": round(mu, 6), "注销状态": state, "样本数": len(items)},
                    tradeable=sp.combo_symbol or ""))
    return out


@strategy("近月转抛")
def s_near_roll(U: Universe, name="近月转抛") -> List[Opportunity]:
    """价差/fullcarry>N + 距到期>D + 可转抛(区间无注销月) [近月下行波动率条件待历史库]。"""
    out = []
    for sp in U.spreads:
        prod = sp.near.product
        if (sp.mid is None or not sp.fullcarry or not _both_sides(sp)
                or sm.in_delivery_month(sp.near.month, prod)
                or not sm.can_roll(sp.near.month, sp.far.month,
                                   U.meta.get(prod, {}).get("cancel", set()))):
            continue
        D = _p(name, prod, "min_days", 10)
        if not sp.days_to_expiry or sp.days_to_expiry <= D:
            continue
        ratio = sp.mid / (-sp.fullcarry)
        if ratio > _p(name, prod, "ratio", 1.0):
            out.append(Opportunity(
                ts=U.ts, strategy=name, kind="spread", product=prod,
                structure=sp.name, direction="买近卖远持有转抛",
                score=round(ratio, 4),
                conditions={"转抛比例": round(ratio, 4), "fullcarry": round(-sp.fullcarry, 2),
                            "时间贴水": round(-sp.decay, 2) if sp.decay else 0,
                            "近月波动率条件": "未评估(需历史库)"},
                tradeable=sp.combo_symbol or ""))
    return out


@strategy("远月转抛")
def s_far_roll(U: Universe, name="远月转抛") -> List[Opportunity]:
    """某远月对 价差/fullcarry>N 且 所有近月对都<N。
    远月对定义(用户口径)：前腿(近腿)到期 6 个月(180天)以上；其余为近月对。"""
    out = []
    by_prod: Dict[str, list] = {}
    for sp in U.spreads:
        prod = sp.near.product
        if (sp.mid is None or not sp.fullcarry or not _both_sides(sp)
                or sm.in_delivery_month(sp.near.month, prod)
                or not sm.can_roll(sp.near.month, sp.far.month,
                                   U.meta.get(prod, {}).get("cancel", set()))):
            continue
        by_prod.setdefault(prod, []).append((sp, sp.mid / (-sp.fullcarry)))
    for prod, items in by_prod.items():
        if len(items) < 2:
            continue
        N = _p(name, prod, "ratio", 1.0)
        D = _p(name, prod, "far_days", 180)      # 前腿到期>180天 = 远月对
        near_items = [(sp, r) for sp, r in items
                      if not sp.days_to_expiry or sp.days_to_expiry <= D]
        far_items = [(sp, r) for sp, r in items
                     if sp.days_to_expiry and sp.days_to_expiry > D]
        if not far_items:
            continue
        near_ratios = [r for _, r in near_items]
        if any(r >= N for r in near_ratios):     # 近月对也高 → 属近月转抛,不算远月独有
            continue
        for sp, r in far_items:
            if r > N:
                out.append(Opportunity(
                    ts=U.ts, strategy=name, kind="spread", product=prod,
                    structure=sp.name, direction="买近卖远持有转抛(远月对)",
                    score=round(r, 4),
                    conditions={"转抛比例": round(r, 4), "前腿到期天数": sp.days_to_expiry,
                                "近月对比例": [round(x, 3) for x in near_ratios]},
                    tradeable=sp.combo_symbol or ""))
    return out


def _adj_chain(U, prod, m1, m2):
    """m1→m2 按该品种挂牌月份拆成相邻月价差段链。返回 [Spread] 或 None(缺段/缺盘口)。"""
    cons = sorted(U.by_product.get(prod, []), key=lambda c: c.month)
    months = [c.month for c in cons]
    sym = {c.month: c.symbol for c in cons}
    if m1 not in months or m2 not in months:
        return None
    chain = []
    seq = [m for m in months if m1 <= m <= m2]
    for a, b in zip(seq, seq[1:]):
        sp = U.spread_by_pair.get((sym[a], sym[b]))
        if sp is None or sp.mid is None:
            return None
        chain.append(sp)
    return chain or None


@strategy("同构价差对比")
def s_matched_pair(U: Universe, name="同构价差对比") -> List[Opportunity]:
    """新口径(用户2026-08-10, 替代原两两配对口径; 2026-08-12 同构放松):
    1. 组内(同月差+跨注销数相同, 注销状态不再强制一致)取**连续**等距三段 A/B/C
       (每个 B 只与紧邻的前后段构成一个三元组, 不做全组合枚举);
       判断 B 相对插值 (A+C)/2 高了还是低了 → 给 B 段方向与估值偏差;
       三段注销状态相同=同构直接输出; 非同构时注销偏置 E 须与信号同向
       (结构比同构更有利, 插值为保守下限)才输出;
    2. 若三段跨不止一个挂牌月: 把 A/B/C 各拆成相邻月段链, 逐位置算
       子段偏差 = B子段 − (A子段+C子段)/2, 归因到偏差最大的那个相邻月段,
       不输出所有子组合。
    score = |偏差| / 主力合约最新价, 倒序。"""
    out = []
    groups: Dict[tuple, list] = {}
    for sp in U.spreads:
        if sp.mid is None or not _both_sides(sp) or not sp.months:
            continue
        if sm.in_delivery_month(sp.near.month, sp.near.product):
            continue
        cancel = U.meta.get(sp.near.product, {}).get("cancel", set())
        btw = cancels_between(sp.near.month, sp.far.month, cancel)
        key = (sp.near.product, sp.months, btw)
        groups.setdefault(key, []).append(sp)
    for (prod, span, btw), sps in groups.items():
        if len(sps) < 3:
            continue
        main_px = next((c.last for c in U.by_product.get(prod, [])
                        if c.is_main and c.last), None)
        sps.sort(key=lambda s: s.near.month)
        mi = [s.near.month // 100 * 12 + s.near.month % 100 for s in sps]
        for j in range(1, len(sps) - 1):
            B = sps[j]
            # 取最近的完全不交叉(允许首尾相接)的前后同构段; 再要求等距
            A = next((sps[i] for i in range(j - 1, -1, -1)
                      if sps[i].far.month <= B.near.month), None)
            C = next((sps[k] for k in range(j + 1, len(sps))
                      if sps[k].near.month >= B.far.month), None)
            if A is None or C is None:
                continue
            ia, ic = sps.index(A), sps.index(C)
            if mi[j] - mi[ia] != mi[ic] - mi[j]:
                continue                                  # 近月不等距
            theo = (A.mid + C.mid) / 2
            over_b = B.mid - theo                          # >0 = B 高了
            if over_b == 0:
                continue
            homo = A.cancel_state == B.cancel_state == C.cancel_state
            if not homo:
                E = _cancel_bias(B) - (_cancel_bias(A) + _cancel_bias(C)) / 2
                if not ((over_b > 0 and E < 0) or (over_b < 0 and E > 0)):
                    continue                              # 结构不有利于信号方向 → 不输出
            # 归因: 拆相邻月段链, 逐位置对比
            attr = "B即相邻月段"
            ca = _adj_chain(U, prod, A.near.month, A.far.month)
            cb = _adj_chain(U, prod, B.near.month, B.far.month)
            cc = _adj_chain(U, prod, C.near.month, C.far.month)
            if cb is not None and len(cb) > 1:
                if ca is not None and cc is not None and len(ca) == len(cb) == len(cc):
                    devs = [(b_.mid - (a_.mid + c_.mid) / 2, b_)
                            for a_, b_, c_ in zip(ca, cb, cc)]
                    d0, culprit = max(devs, key=lambda t: abs(t[0]))
                    attr = (f"{culprit.name} {'高估' if d0 > 0 else '低估'}"
                            f"{d0:+.1f}(整段偏差{over_b:+.1f}的主要来源)")
                else:
                    attr = "不可细分(子段缺盘口或三段月份表不齐)"
            ratio = (abs(over_b) / main_px) if main_px else None
            out.append(Opportunity(
                ts=U.ts, strategy=name, kind="kink", product=prod,
                structure=f"{A.name} | {B.name} | {C.name}",
                direction="做空B段(B高于插值)" if over_b > 0 else "做多B段(B低于插值)",
                score=round(ratio, 6) if ratio is not None else round(abs(over_b), 4),
                conditions={"A": A.mid, "B": B.mid, "C": C.mid,
                            "理论B=(A+C)/2": round(theo, 2),
                            "估值偏差(实际-理论)": round(over_b, 2),
                            "比例%": round(ratio * 100, 4) if ratio is not None else None,
                            "归因": attr,
                            "同构": (f"月差{span},跨注销{btw},"
                                     + ("同构" if homo else
                                        f"非同构({A.cancel_state}/{B.cancel_state}/{C.cancel_state},"
                                        "结构有利,插值为保守下限)"))},
                tradeable=B.combo_symbol or "",
                detail=f"组共{len(sps)}段, 仅取紧邻三元组"))
    return out


# --------------------------------------------------------------------------- #
# C类统计策略（历史日K，经 set_hist_provider 注入；未注入时静默跳过）
# --------------------------------------------------------------------------- #
_HIST = None


def set_hist_provider(fn):
    """fn(prod, nearM, farM) -> {year: [(date,mmdd,val)...]} （hist_stats.versions_series_cached）"""
    global _HIST
    _HIST = fn


_SLOPE = None


def set_slope_provider(fn):
    """fn(prod) -> {year: [(date,mmdd,slope元/月)...]} （hist_stats.slope_series_cached）"""
    global _SLOPE
    _SLOPE = fn


def _slope_ctx(prod):
    """当前期限斜率 + 同期分位（日历锚）。返回 (当前斜率, 分位%) 或 (None,None)。"""
    if _SLOPE is None:
        return None, None
    try:
        sl = _SLOPE(prod)
    except Exception:
        return None, None
    years = sorted(sl)
    if len(years) < 4 or not sl.get(years[-1]):
        return None, None
    cur = sl[years[-1]][-1][2]
    ck = _ok_mmdd(datetime.now().strftime("%m-%d"), 1)
    peers = []
    for y in years[:-1]:
        best = None
        for _, mmdd, v in sl[y]:
            k = _ok_mmdd(mmdd, 1)
            if k <= ck and (best is None or k > best[0]):
                best = (k, v)
        if best:
            peers.append(best[1])
    return cur, _pctl(cur, peers)


def _ok_mmdd(mmdd, anchorM):
    return ((int(mmdd[:2]) - anchorM) % 12, int(mmdd[3:]))


# --------------------------------------------------------------------------- #
# 品种期限结构判定（同特性月份分组回归）
# --------------------------------------------------------------------------- #
_CURVE_MEMO: Dict[tuple, dict] = {}


def curve_structure(U: Universe, prod: str) -> dict:
    """当下曲线是否基本符合 back/contango，斜率多少（元/月，OI加权）。
    关键：用同样特性的月份互比 —— 按是否注销月分组(如FG/SA奇数月=注销月)，
    组内各自回归 close~距到期月数；各组斜率同号且幅度相近(≤3倍) → 判定整体结构；
    同号但幅度差大 → 结构成立但标注组差大；异号 → 分歧(驼峰/规则性错位)。
    整体斜率 = 组内去均值后的合并回归(固定效应,剔除注销/非注销的水平差)。
    另给 斜率历史同期分位%（_slope_ctx, 需 set_slope_provider）。"""
    key = (id(U), prod)
    if key in _CURVE_MEMO:
        return _CURVE_MEMO[key]
    res = {"label": "", "slope": None, "groups": {}, "pctl": None}
    _cur, pctl = _slope_ctx(prod)
    res["pctl"] = pctl
    pts = []
    for c in U.by_product.get(prod, []):
        if c.last is None or not c.days_to_expiry or c.days_to_expiry <= 0:
            continue
        w = c.oi if (c.oi and c.oi > 0) else 1.0
        pts.append((bool(c.is_cancel_month), c.days_to_expiry / 30.44, c.last, w))
    if len(pts) < 3:
        res["label"] = "样本不足"
        _CURVE_MEMO[key] = res
        return res
    grp: Dict[bool, list] = {}
    for g, x, y, w in pts:
        grp.setdefault(g, []).append((x, y, w))

    def _wslope(items):
        if len(items) < 2:
            return None
        sw = sum(w for _, _, w in items)
        xb = sum(x * w for x, _, w in items) / sw
        yb = sum(y * w for _, y, w in items) / sw
        sxx = sum(w * (x - xb) ** 2 for x, _, w in items)
        if sxx <= 1e-9:
            return None
        return sum(w * (x - xb) * (y - yb) for x, y, w in items) / sxx

    gs = {}
    for g, items in grp.items():
        s = _wslope(items)
        if s is not None:
            gs["注销月组" if g else "非注销月组"] = round(s, 2)
    num = den = 0.0
    for items in grp.values():
        if len(items) < 2:
            continue
        sw = sum(w for _, _, w in items)
        xb = sum(x * w for x, _, w in items) / sw
        yb = sum(y * w for _, y, w in items) / sw
        num += sum(w * (x - xb) * (y - yb) for x, y, w in items)
        den += sum(w * (x - xb) ** 2 for x, _, w in items)
    res["groups"] = gs
    if den <= 1e-9:
        res["label"] = "无法拟合"
        _CURVE_MEMO[key] = res
        return res
    slope = num / den
    res["slope"] = round(slope, 2)
    vals = list(gs.values())
    base_lbl = "contango" if slope > 0 else "back"
    if len(vals) >= 2:
        if not (all(v > 0 for v in vals) or all(v < 0 for v in vals)):
            res["label"] = "分歧"
        else:
            mags = sorted(abs(v) for v in vals)
            res["label"] = base_lbl if (mags[0] > 0 and mags[-1] / max(mags[0], 1e-9) <= 3.0) \
                else base_lbl + "(组差大)"
    else:
        res["label"] = base_lbl
    _CURVE_MEMO[key] = res
    return res


def curve_str(cs: dict) -> str:
    if cs.get("slope") is None:
        return cs.get("label", "")
    return f"{cs['label']}({cs['slope']:+.1f}元/月)"


_CYCLE_PAIRS = {(1, 5), (5, 9), (9, 1)}


def _hist_ctx(sp: Spread, min_years=4):
    """液态 + (相邻或159循环) 的段才做统计（控制历史构建成本）。返回 sby 或 None。
    流动性口径(2026-08-12): 盘口空隙比例, 不再看腿OI; 基准用近月价(abs_price,
    与主力价同品种同量级, 此处拿不到 U 无法取主力价, 误差可忽略)。"""
    if _HIST is None or sp.mid is None or not _both_sides(sp):
        return None
    g = spread_gap(sp)
    if g is None or not sp.abs_price or g / sp.abs_price > LIQ_MAX_GAP_RATIO:
        return None
    mp = (sp.near.month % 100, sp.far.month % 100)
    if not (sp.adjacent or mp in _CYCLE_PAIRS):
        return None
    sby = _HIST(sp.near.product, mp[0], mp[1])
    return sby if len(sby) >= min_years else None


def _pctl(x, arr):
    return round(100.0 * sum(1 for a in arr if a < x) / len(arr), 1) if arr else None


def _peer_vals(sby, anchorM):
    """各历史年版在"今天"同期位置的值 + 全期样本。"""
    years = sorted(sby)
    ck = _ok_mmdd(datetime.now().strftime("%m-%d"), anchorM)
    peers = {}
    for y in years[:-1]:
        best = None
        for _, mmdd, val in sby[y]:
            k = _ok_mmdd(mmdd, anchorM)
            if k <= ck and (best is None or k > best[0]):
                best = (k, val)
        if best:
            peers[y] = best[1]
    all_hist = [v for y in years[:-1] for _, _, v in sby[y]]
    return peers, all_hist


def _best_sharpe_from(sby, anchorM, max_hold=60, min_years=3):
    """注释39口径: 各历史年版自今天同期位置起的未来日收益跨年取均值，
    夏普(d)=前d日均值收益之和/其标准差，遍历d返回 (最优夏普, 对应d)。正=做多价差有利。"""
    import statistics
    ck = _ok_mmdd(datetime.now().strftime("%m-%d"), anchorM)
    fut = []
    for y in sorted(sby)[:-1]:
        base, vals = None, []
        for _, mmdd, v in sby[y]:
            if _ok_mmdd(mmdd, anchorM) <= ck:
                base = v
            else:
                vals.append(v)
        if base is None or len(vals) < 5:
            continue
        seq = [base] + vals
        fut.append([seq[i + 1] - seq[i] for i in range(len(seq) - 1)])
    if len(fut) < min_years:
        return None, None
    T = min(max_hold, min(len(f) for f in fut))
    if T < 5:
        return None, None
    mr = [statistics.mean(f[t] for f in fut) for t in range(T)]
    best_sh, best_d = None, None
    for d in range(5, T + 1):
        seg = mr[:d]
        s = statistics.pstdev(seg)
        if s <= 0:
            continue
        sh = sum(seg) / s
        if best_sh is None or abs(sh) > abs(best_sh):
            best_sh, best_d = sh, d
    return best_sh, best_d


RESID_FLAG = "未满足残差过滤"      # 季节性统计: 回归(价差~斜率+到期天数)可解释,残差不够大
_RESID_MEMO: Dict[tuple, Optional[dict]] = {}


def _resid_ctx(sp, sby):
    """双自变量回归过滤(用户口径): 自变量1=品种期限结构斜率, 自变量2=距到期天数,
    因变量=同一段价差; 样本=历史各年版逐日(剔当前年版), 按日期与斜率序列对齐。
    到期天数口径与 version_series 一致 = 距近腿交割月首日; 当前点用同口径。
    返回 dict(z,resid,pred,r2,n,years) 或 None(斜率序列缺/样本不足/共线)。"""
    from datetime import date as _d
    key = (sp.near.product, sp.near.month, sp.far.month)
    if key in _RESID_MEMO:
        return _RESID_MEMO[key]
    res = None
    try:
        res = _resid_fit(sp, sby, _d)
    except Exception:
        res = None
    _RESID_MEMO[key] = res
    return res


def _resid_fit(sp, sby, _d):
    if _SLOPE is None or sp.mid is None:
        return None
    prod = sp.near.product
    sl = _SLOPE(prod)
    smap = {dt: v for pts in sl.values() for dt, _, v in pts}
    if not smap:
        return None
    nearM = sp.near.month % 100
    x1, x2, ys = [], [], []
    yrs = set()
    for y in sorted(sby)[:-1]:
        cut = _d(y, nearM, 1)
        for dstr, _, val in sby[y]:
            s = smap.get(dstr)
            if s is None:
                continue
            dte = (cut - _d(int(dstr[:4]), int(dstr[5:7]), int(dstr[8:10]))).days
            if dte <= 0:
                continue
            x1.append(s); x2.append(float(dte)); ys.append(val); yrs.add(y)
    n = len(ys)
    if n < 60 or len(yrs) < 3:
        return None
    m1, m2, my = sum(x1) / n, sum(x2) / n, sum(ys) / n
    s11 = sum((a - m1) ** 2 for a in x1)
    s22 = sum((b - m2) ** 2 for b in x2)
    s12 = sum((a - m1) * (b - m2) for a, b in zip(x1, x2))
    s1y = sum((a - m1) * (c - my) for a, c in zip(x1, ys))
    s2y = sum((b - m2) * (c - my) for b, c in zip(x2, ys))
    det = s11 * s22 - s12 * s12
    if det <= 1e-9:
        return None
    b1 = (s1y * s22 - s2y * s12) / det
    b2 = (s2y * s11 - s1y * s12) / det
    b0 = my - b1 * m1 - b2 * m2
    sse = sum((c - (b0 + b1 * a + b2 * b)) ** 2 for a, b, c in zip(x1, x2, ys))
    sig = (sse / max(n - 3, 1)) ** 0.5
    if sig <= 1e-9:
        return None
    cur_slope, _pp = _slope_ctx(prod)
    if cur_slope is None:
        return None
    cut_now = _d(sp.near.month // 100, nearM, 1)
    dte_now = (cut_now - _d.today()).days
    if dte_now <= 0:
        return None
    pred = b0 + b1 * cur_slope + b2 * dte_now
    r = sp.mid - pred
    sst = sum((c - my) ** 2 for c in ys)
    return dict(z=round(r / sig, 2), resid=round(r, 1), pred=round(pred, 1),
                r2=round(1 - sse / sst, 3) if sst > 0 else None,
                n=n, years=len(yrs))


def _seasonal_stat(U, name, long_side: bool):
    out = []
    for sp in U.spreads:
        sby = _hist_ctx(sp)
        if not sby:
            continue
        prod = sp.near.product
        anchor = sp.near.month % 100
        peers, _all = _peer_vals(sby, anchor)
        if len(peers) < 3:
            continue
        pp = _pctl(sp.mid, list(peers.values()))
        N1 = _p(name, prod, "pct", 20.0)
        if pp is None or (long_side and pp >= N1) or ((not long_side) and pp <= 100 - N1):
            continue
        sh, d = _best_sharpe_from(sby, anchor)
        Nsh = _p(name, prod, "sharpe", 2.0)
        if sh is None:
            continue
        if long_side and sh < Nsh:
            continue
        if (not long_side) and sh > -Nsh:
            continue
        slope_cur, slope_pp = _slope_ctx(prod)
        conds = {"同期分位%": pp, "最优夏普": round(sh, 2), "持有期(交易日)": d,
                 "同期各年": {y: round(v, 1) for y, v in sorted(peers.items())},
                 "当前(实时mid)": sp.mid, "历史年数": len(sby) - 1,
                 "期限斜率(元/月)": slope_cur, "斜率同期分位%": slope_pp}
        # 残差过滤: 回归 价差~斜率+到期天数, 残差需朝有利方向偏离足够大
        # (正套要求便宜: z<=-T; 反套要求偏贵: z>=T), 否则价差被结构解释->降级。
        # 豁免(用户口径): 近腿实际到期>180天(6个月)的远期段不受残差过滤约束, 直接保留
        flag = ""
        rc = _resid_ctx(sp, sby)
        far_keep = (sp.near.days_to_expiry or 0) > _p(name, prod, "far_days", 180)
        if rc is None:
            conds["残差回归"] = "样本不足"
        else:
            conds.update({"残差z": rc["z"], "残差(元)": rc["resid"], "回归预测": rc["pred"],
                          "回归R2": rc["r2"], "回归样本": f"{rc['n']}点/{rc['years']}年"})
            T = _p(name, prod, "resid_z", 1.5)
            if not (rc["z"] <= -T if long_side else rc["z"] >= T):
                if far_keep:
                    conds["保留原因"] = f"近腿到期{sp.near.days_to_expiry}天>180(远期豁免残差过滤)"
                else:
                    flag = RESID_FLAG
        out.append(Opportunity(
            ts=U.ts, strategy=name, kind="spread", product=prod,
            structure=sp.name, direction="做多价差(季节性低位)" if long_side else "做空价差(季节性高位)",
            score=round(abs(sh), 3), hold_period=d, flag=flag,
            conditions=conds,
            tradeable=sp.combo_symbol or ""))
    return out


@strategy("价差季节性统计套利(正套)")
def s_seasonal_long(U: Universe, name="价差季节性统计套利(正套)") -> List[Opportunity]:
    return _seasonal_stat(U, name, True)


@strategy("价差季节性统计套利(反套)")
def s_seasonal_short(U: Universe, name="价差季节性统计套利(反套)") -> List[Opportunity]:
    return _seasonal_stat(U, name, False)


@strategy("价差统计套利")
def s_stat_arb(U: Universe, name="价差统计套利") -> List[Opportunity]:
    """常规分支: abs(全期分位-0.5)>N 且 距到期>D, 过 筛1顺季节性+筛2结构过滤。
    远期极值分支(用户2026-08-12): 前腿到期>far_days(默认120天) + 历史同期分位极值
    (≤peer_lo 或 ≥peer_hi) 即输出, 免筛1/筛2 —— 很远期时时间充裕可等波动/换月催化,
    极值提供安全边际, 结构逆风可被覆盖(V2701-2705 型, 刷波动候选)。"""
    out = []
    for sp in U.spreads:
        sby = _hist_ctx(sp)
        if not sby:
            continue
        prod = sp.near.product
        D = _p(name, prod, "min_days", 30)
        if not sp.days_to_expiry or sp.days_to_expiry <= D:
            continue
        anchor = sp.near.month % 100
        peers, all_hist = _peer_vals(sby, anchor)
        # ---- 远期同期极值分支(免筛1/筛2) ----
        if (sp.days_to_expiry > _p(name, prod, "far_days", 120)
                and len(peers) >= _p(name, prod, "peer_min_years", 5) and sp.mid is not None):
            spp = _pctl(sp.mid, list(peers.values()))
            lo, hi = _p(name, prod, "peer_lo", 10.0), _p(name, prod, "peer_hi", 90.0)
            if spp <= lo or spp >= hi:
                out.append(Opportunity(
                    ts=U.ts, strategy=name, kind="spread", product=prod,
                    structure=sp.name,
                    direction="做多价差(远期+同期极值低位)" if spp <= lo
                              else "做空价差(远期+同期极值高位)",
                    score=round(abs(spp / 100.0 - 0.5), 4),
                    conditions={"分支": "远期同期极值(免季节性/结构过滤,刷波动候选)",
                                "同期分位%": spp, "距到期": sp.days_to_expiry,
                                "当前(实时mid)": sp.mid,
                                "同期各年": {y: round(v, 1) for y, v in sorted(peers.items())}},
                    tradeable=sp.combo_symbol or ""))
                continue                       # 已按远期分支输出, 不再走常规分支(防重复)
        if len(all_hist) < 200:
            continue
        pa = _pctl(sp.mid, all_hist)
        dev = abs(pa / 100.0 - 0.5)
        if dev <= _p(name, prod, "N", 0.45):
            continue
        long_side = pa <= 50
        # 筛1 顺季节性：历史同期起的季节性漂移方向必须与交易方向一致
        sh, _d = _best_sharpe_from(sby, anchor)
        if sh is None:
            continue
        if long_side and sh <= 0:
            continue
        if (not long_side) and sh >= 0:
            continue
        # 筛2 期限结构历史同期过滤(用户口径)：结构处历史极端时,顺结构方向的统计单危险
        # 斜率分位极高=深度contango → 统计正套(做多价差)是接结构性飞刀 → 放弃
        # 斜率分位极低=深度back → 统计反套(做空价差)同理危险 → 放弃
        cs = curve_structure(U, prod)
        pctl = cs.get("pctl")
        if pctl is not None:
            if long_side and pctl > _p(name, prod, "slope_pctl_hi", 90.0):
                continue
            if (not long_side) and pctl < _p(name, prod, "slope_pctl_lo", 10.0):
                continue
        out.append(Opportunity(
            ts=U.ts, strategy=name, kind="spread", product=prod,
            structure=sp.name, direction="做空价差(全期高位)" if pa > 50 else "做多价差(全期低位)",
            score=round(dev, 4),
            conditions={"全期分位%": pa, "样本数": len(all_hist), "当前(实时mid)": sp.mid,
                        "距到期": sp.days_to_expiry, "季节性夏普": round(sh, 2),
                        "期限结构": curve_str(cs), "组斜率": cs.get("groups"),
                        "斜率同期分位%": pctl},
            tradeable=sp.combo_symbol or ""))
    return out


@strategy("蝶式价差统计套利")
def s_bf_stat(U: Universe, name="蝶式价差统计套利") -> List[Opportunity]:
    """abs(蝶式全期分位-0.5)>N 且 abs(前段)/绝对价<N2(排除陡结构/交割规则影响)。"""
    out = []
    for bf in U.butterflies:
        f, b = bf.front, bf.back
        if bf.value is None or not bf.abs_price:
            continue
        sf, sb = _hist_ctx(f), _hist_ctx(b)
        if not sf or not sb:
            continue
        prod = f.near.product
        if abs(f.mid) / bf.abs_price >= _p(name, prod, "front_max", 0.02):
            continue
        # 历史蝶式: 前后段年版按日期重叠配对
        hist = []
        usedB = set()
        for yf, pf in sorted(sf.items())[:-1]:
            da = {d: v for d, _, v in pf}
            best, bn = None, 0
            for yb, pb in sb.items():
                if yb in usedB:
                    continue
                n = sum(1 for d, _, _ in pb if d in da)
                if n > bn:
                    best, bn = yb, n
            if best and bn >= 20:
                usedB.add(best)
                hist += [v2 - da[d] for d, _, v2 in sb[best] if d in da]
        if len(hist) < 200:
            continue
        pa = _pctl(bf.value, hist)
        dev = abs(pa / 100.0 - 0.5)
        if dev <= _p(name, prod, "N", 0.45):
            continue
        out.append(Opportunity(
            ts=U.ts, strategy=name, kind="butterfly", product=prod,
            structure=bf.name, direction="做空蝶式(全期高位)" if pa > 50 else "做多蝶式(全期低位)",
            score=round(dev, 4),
            conditions={"蝶式全期分位%": pa, "样本数": len(hist), "当前蝶值": bf.value,
                        "前段/绝对价": round(abs(f.mid) / bf.abs_price, 4)}))
    return out


@strategy("波动传递")
def s_vol_transfer(U: Universe, name="波动传递") -> List[Opportunity]:
    """(前段短期波动-后段短期波动)/绝对价>N；波动=当前年版近20个日差的样本标准差。"""
    import statistics

    def _vol(sby):
        cur = sby[sorted(sby)[-1]]
        vals = [v for _, _, v in cur][-21:]
        if len(vals) < 10:
            return None
        diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        return statistics.pstdev(diffs)

    out = []
    for bf in U.butterflies:
        f, b = bf.front, bf.back
        if not bf.abs_price:
            continue
        sf, sb = _hist_ctx(f), _hist_ctx(b)
        if not sf or not sb:
            continue
        vf, vb = _vol(sf), _vol(sb)
        if vf is None or vb is None:
            continue
        prod = f.near.product
        val = (vf - vb) / bf.abs_price
        if val <= _p(name, prod, "N", 0.0015):
            continue
        out.append(Opportunity(
            ts=U.ts, strategy=name, kind="butterfly", product=prod,
            structure=bf.name, direction="前段波动显著高于后段(传递观察)",
            score=round(val, 5),
            conditions={"前段20日波动": round(vf, 2), "后段20日波动": round(vb, 2),
                        "归一差": round(val, 5)}))
    return out


# --------------------------------------------------------------------------- #
# 仓单驱动 / 主力溢价 策略
# --------------------------------------------------------------------------- #
_WARR = None


def set_warrant_provider(fn):
    """fn(prod) -> {year: [[mmdd,val],...]} （product_detail.fetch_warrants）"""
    global _WARR
    _WARR = fn


def _warrant_pct(w, min_years=4):
    """当前仓单在历年同日历位置的分位。返回 (分位%, 当前值) 或 (None,None)。"""
    years = sorted(w)
    if len(years) < min_years or not w.get(years[-1]):
        return None, None
    cur_mmdd, cur_val = w[years[-1]][-1]
    peers = []
    for y in years[:-1]:
        best = None
        for mmdd, val in w[y]:
            if mmdd <= cur_mmdd and (best is None or mmdd > best[0]):
                best = (mmdd, val)
        if best:
            peers.append(best[1])
    return _pctl(cur_val, peers), cur_val


@strategy("注销月近月崩塌")
def s_cancel_collapse(U: Universe, name="注销月近月崩塌") -> List[Opportunity]:
    """仓单驱动(用户口径): 注销月效应段 = 注销月(near)−注销后月(far), 如MA611注销→段=611-612。
    仓单强注销压的是近腿(注销月合约), 段应深负; 前一段(610-611)的抬高只是镜像,不在此选取。
    仓单高+同期分位仍高(崩塌未定价)→做空(不限距注销月天数,2026-08-12去掉临近窗口);
    仓单低+临近+分位已极低(折价过度)→做多; 距注销月远但已深度定价→做多(不确定性折扣过度)。"""
    if _WARR is None or _HIST is None:
        return []
    out = []
    wcache = {}
    for sp in U.spreads:
        if not (sp.near.is_cancel_month and not sp.far.is_cancel_month):
            continue
        sby = _hist_ctx(sp)
        if not sby:
            continue
        prod = sp.near.product
        if prod not in wcache:
            try:
                wcache[prod] = _WARR(prod) or {}
            except Exception:
                wcache[prod] = {}
        wp, wval = _warrant_pct(wcache[prod])
        if wp is None:
            continue
        peers, _ = _peer_vals(sby, sp.near.month % 100)
        if len(peers) < 3 or sp.mid is None:
            continue
        spp = _pctl(sp.mid, list(peers.values()))
        days = (datetime(sp.near.month // 100, sp.near.month % 100, 1) - datetime.now()).days
        Dn = _p(name, prod, "near_days", 75)
        Df = _p(name, prod, "far_days", 150)
        sig = None
        # 段=注销月−后月: 崩塌兑现表现为段深负(同期分位低); 分位高=崩塌未定价
        # 做空分支不设 days 窗口(用户2026-08-12): 错价现在就存在, 不必等临近注销月
        # (J 距3月注销约200天、Y 距11月约80天的提前做空均应触发)
        if wp >= _p(name, prod, "w_high", 70) and spp >= _p(name, prod, "s_high", 70):
            sig = ("做空价差(仓单高+崩塌未定价)", (wp + spp - 100) / 100)
        elif days <= Dn and wp <= _p(name, prod, "w_low", 30) and spp <= _p(name, prod, "s_low", 40):
            sig = ("做多价差(仓单低,折价过度)", (100 - wp - spp) / 100)
        elif days >= Df and spp <= _p(name, prod, "s_low", 40):
            sig = ("做多价差(远期已深度定价崩塌,不确定性折扣过度)", (50 - spp) / 100)
        if not sig:
            continue
        out.append(Opportunity(
            ts=U.ts, strategy=name, kind="spread", product=prod,
            structure=sp.name, direction=sig[0], score=round(sig[1], 3),
            conditions={"仓单同期分位%": wp, "当前仓单": wval, "价差同期分位%": spp,
                        "距注销月天数": days, "当前价差(实时mid)": sp.mid,
                        "同期各年": {y: round(v, 1) for y, v in sorted(peers.items())}},
            tradeable=sp.combo_symbol or ""))
    return out


@strategy("流动性溢价")
def s_liq_premium(U: Universe, name="流动性溢价") -> List[Opportunity]:
    """主力合约自带流动性溢价(高估): 前月−主力被压低(换月时回归→做多),
    主力−后月被抬高(→做空)。触发窗口=主力距到期<=D天(移仓溢价衰减期)。"""
    out = []
    for p, cons in U.by_product.items():
        mains = [c for c in cons if c.is_main]
        if not mains:
            continue
        mc = mains[0]
        prod = mc.product
        if prod in ADVISORY_ONLY:
            continue
        D = _p(name, prod, "roll_days", 75)
        if not mc.days_to_expiry or mc.days_to_expiry > D:
            continue
        i = cons.index(mc)
        win = round(1 - mc.days_to_expiry / D, 3)          # 越临近换月越强
        for sp_key, direction, note in (
                ((cons[i - 1].symbol, mc.symbol) if i >= 1 else None,
                 "做多价差(前月−主力被溢价压低)", "front_main"),
                ((mc.symbol, cons[i + 1].symbol) if i + 1 < len(cons) else None,
                 "做空价差(主力−后月被溢价抬高)", "main_back")):
            if sp_key is None:
                continue
            sp = U.spread_by_pair.get(sp_key)
            if sp is None or sp.mid is None or not _both_sides(sp):
                continue
            spp = None
            sby = _hist_ctx(sp)
            if sby:
                peers, _ = _peer_vals(sby, sp.near.month % 100)
                if len(peers) >= 3:
                    spp = _pctl(sp.mid, list(peers.values()))
            out.append(Opportunity(
                ts=U.ts, strategy=name, kind="spread", product=prod,
                structure=sp.name, direction=direction, score=win,
                conditions={"主力": sm.code_of(mc.symbol), "主力距到期": mc.days_to_expiry,
                            "换月进度": win, "当前价差": sp.mid, "价差同期分位%": spp},
                tradeable=sp.combo_symbol or ""))
    return out


C_STRATEGIES = ["价差季节性统计套利(正套)", "价差季节性统计套利(反套)",
                "价差统计套利", "蝶式价差统计套利", "波动传递",
                "注销月近月崩塌", "流动性溢价"]

# 仍待实现的策略保持桩
for _name in ["近月回归"]:
    STRATEGIES.setdefault(_name, lambda U, name=_name: [])   # 桩


# --------------------------------------------------------------------------- #
# 运行 + 机会输出
# --------------------------------------------------------------------------- #
def _break_verdict(o: Opportunity, bm: int, spn: dict, bfn: dict):
    """跨规则断层机会的方向裁决(用户2026-08-12): 新标准腿(月份>=bm)净暴露为多才保留
    (用户口径"只返回把jm2701作为多头腿的策略"; 蝶式 -近+2中-远 中腿=2701 时净+1 → 保留,
    新腿之间互相对冲不剔除)。返回 True=保留(独立标注) / False=剔除 / None=无法判定
    (交易腿不跨断层但对比段跨、方向文本不识别等) → 沿用原排除性标注。"""
    if o.kind == "spread":
        s = spn.get(o.structure)
        if s is None:
            return None
        if not (s.near.month < bm <= s.far.month):
            return None                              # 交易段本身不跨断层
        if o.direction.startswith("做空") or "卖近买远" in o.direction:
            return True                              # 买远腿=买新标准 → 保留
        if o.direction.startswith("做多") or "买近卖远" in o.direction:
            return False                             # 卖新标准腿 → 剔除
        return None
    if o.kind == "kink" and " | " in o.structure:
        ss = [spn.get(n) for n in o.structure.split(" | ")]
        if not all(ss):
            return None
        B = ss[1]                                    # 交易腿=B段
        if not (B.near.month < bm <= B.far.month):
            return None                              # B不跨断层(A/C跨→插值被污染,不判定)
        if o.direction.startswith("做空"):
            return True
        if o.direction.startswith("做多"):
            return False
        return None
    if o.kind == "butterfly":
        bf = bfn.get(o.structure)
        if bf is not None:
            months = (bf.front.near.month, bf.front.far.month, bf.back.far.month)
        else:                                        # 泛化蝶式: 按结构名回查两段
            codes = o.structure.split("-")
            if len(codes) != 3:
                return None
            f_ = spn.get(f"{codes[0]}-{codes[1]}")
            b_ = spn.get(f"{codes[1]}-{codes[2]}")
            if not (f_ and b_):
                return None
            months = (f_.near.month, f_.far.month, b_.far.month)
        d = o.direction
        if "做多蝶式" in d or "做多后段" in d:
            signs = (-1, 2, -1)                      # 买后段卖前段 = -近+2中-远
        elif "做空蝶式" in d or "做多前段" in d:
            signs = (1, -2, 1)                       # 买前段卖后段 = +近-2中+远
        else:
            return None
        return sum(sg for m, sg in zip(months, signs) if m >= bm) > 0
    return None


def _annotate(U: Universe, opps: List[Opportunity]):
    """统一后处理：流动性(腿OI) + 规则断层 + 仅提示类 + 仓单佐证。"""
    spn = {s.name: s for s in U.spreads}
    bfn = {bf.name: bf for bf in U.butterflies}
    wpc = {}
    mpx = {}     # 品种主力最新价缓存(盘口空隙比例基准)

    def _wp(prod):
        """仓单同期分位缓存: (分位%, 当前值)；无数据/无provider → (None, None)。"""
        if prod not in wpc:
            w = {}
            if _WARR is not None:
                try:
                    w = _WARR(prod) or {}
                except Exception:
                    w = {}
            wpc[prod] = _warrant_pct(w) if w else (None, None)
        return wpc[prod]

    for o in opps:
        # 品种期限结构参数（每个品种统一附带）
        cs = curve_structure(U, o.product)
        o.curve = curve_str(cs)
        o.slope_pctl = cs.get("pctl")
        legs, span, tsps = [], None, []       # tsps = 组成可交易结构的价差段(流动性判定用)
        if o.kind == "spread" and o.structure in spn:
            s = spn[o.structure]; legs = [s.near, s.far]; span = (s.near.month, s.far.month)
            tsps = [s]
            long_dir = o.direction.startswith("做多") or "买近卖远" in o.direction
            cancel = U.meta.get(o.product, {}).get("cancel", set())
            o.rollable = (long_dir and sm.can_roll(s.near.month, s.far.month, cancel)
                          and not sm.in_delivery_month(s.near.month, o.product))
            o.book = (s.bid, s.bid_vol, s.ask, s.ask_vol)
            # 注销段标注(用户口径): 注销月效应体现在 注销月(近腿)−下月 段(近腿被强注销压低);
            # 远腿=注销月的段是其镜像(被结构性抬高), 显得"过高"属正常, 勿当错价做空
            if s.far.is_cancel_month and not s.near.is_cancel_month:
                o.conditions["注销段"] = f"远腿{s.far.month % 100}月=注销月,本段结构性偏高(效应段在下一段)"
            elif s.near.is_cancel_month and not s.far.is_cancel_month:
                o.conditions["注销段"] = f"近腿{s.near.month % 100}月=注销月,本段结构性偏低(即注销效应段)"
            # 仓单佐证(2026-08-04 研究): 仓单水位对价差只是弱佐证不成信号(系统化已证伪),
            # 人工层用作方向背书——低仓单支持正套(买近卖远)、高仓单支持反套; 极端分位(≥70/≤30)才标顺/逆风
            wp, wval = _wp(o.product)
            if wp is not None:
                o.warrant_pctl = wp
                short_dir = o.direction.startswith("做空") or "卖近买远" in o.direction
                tag = ""
                if long_dir or short_dir:
                    if wp <= 30:
                        tag = "顺风" if long_dir else "逆风"
                    elif wp >= 70:
                        tag = "逆风" if long_dir else "顺风"
                    else:
                        tag = "中性"
                o.conditions.setdefault(
                    "仓单佐证", f"{tag}(同期分位{wp:.0f}%,当前{wval:g})" if tag
                    else f"同期分位{wp:.0f}%,当前{wval:g}")
        elif o.kind == "butterfly" and o.structure in bfn:
            b = bfn[o.structure]; legs = [b.front.near, b.front.far, b.back.far]
            span = (b.front.near.month, b.back.far.month)
            tsps = [b.front, b.back]
        elif o.kind == "butterfly" and o.structure.count("-") == 2:
            # 泛化蝶式(非连续挂牌月, 不在 U.butterflies): 按结构名回查两段
            c0, c1, c2 = o.structure.split("-")
            f_, b_ = spn.get(f"{c0}-{c1}"), spn.get(f"{c1}-{c2}")
            if f_ and b_:
                legs = [f_.near, f_.far, b_.far]
                span = (f_.near.month, b_.far.month)
                tsps = [f_, b_]
        elif o.kind == "pair" and " vs " in o.structure:
            n1, n2 = o.structure.split(" vs ")
            s1, s2 = spn.get(n1), spn.get(n2)
            if s1 and s2:
                legs = [s1.near, s1.far, s2.near, s2.far]
                span = (min(s1.near.month, s2.near.month), max(s1.far.month, s2.far.month))
                tsps = [s1, s2]
        elif o.kind == "kink" and " | " in o.structure:
            ss = [spn.get(n) for n in o.structure.split(" | ")]
            if all(ss):
                legs = [c for s_ in ss for c in (s_.near, s_.far)]
                span = (min(s_.near.month for s_ in ss), max(s_.far.month for s_ in ss))
                Bsp = ss[1]                       # 标的段 = 中间段 B
                tsps = [Bsp]                      # A/C 仅对比不交易, 不进流动性判定
                o.book = (Bsp.bid, Bsp.bid_vol, Bsp.ask, Bsp.ask_vol)
                if o.direction.startswith("做多"):
                    cancel = U.meta.get(o.product, {}).get("cancel", set())
                    o.rollable = (sm.can_roll(Bsp.near.month, Bsp.far.month, cancel)
                                  and not sm.in_delivery_month(Bsp.near.month, o.product))
        if legs:
            ois = [c.oi for c in legs]
            min_oi = int(min([v for v in ois if v is not None], default=0))
            o.conditions["最小腿OI"] = min_oi    # 仅展示参考, 不再作为过滤条件
        if tsps:
            # 流动性新口径(用户2026-08-12): 可交易段合成盘口空隙之和 / 主力价
            if o.product not in mpx:
                mpx[o.product] = next((c.last for c in U.by_product.get(o.product, [])
                                       if c.is_main and c.last), None)
            main_px = mpx[o.product]
            gaps = [spread_gap(t) for t in tsps]
            if any(g is None for g in gaps) or not main_px:
                o.liquid = False
                o.flag = o.flag or "低流动性(钓鱼)"
                o.conditions["盘口空隙"] = "缺边" if any(g is None for g in gaps) else "无主力价"
            else:
                gap = sum(gaps)
                tick = tsps[0].near.tick
                ratio = gap / main_px
                o.conditions["盘口空隙"] = (f"{gap / tick:.0f}跳/{ratio * 100:.3f}%"
                                            if tick else f"{gap:.2f}/{ratio * 100:.3f}%")
                if ratio > LIQ_MAX_GAP_RATIO:
                    o.liquid = False
                    o.flag = o.flag or "低流动性(钓鱼)"
        if span:
            for bm, note in REGIME_BREAKS.get(o.product, []):
                if span[0] < bm <= span[1]:
                    # 用户2026-08-12: 跨断层不再一律排除——新标准腿(>=断层月)全为
                    # 多头则保留(独立考量因素标注); 含空新标准腿则剔除; 判定不了沿用排除标注
                    v = _break_verdict(o, bm, spn, bfn)
                    if v is True:
                        o.conditions["规则断层"] = f"{note}; 新标准腿(≥{bm})为多头→保留"
                    elif v is False:
                        o.flag = "规则断层-剔除(空新标准腿)"
                    else:
                        o.flag = f"规则断层: {note}"
                    break
            # 股指分红季标注：价差跨越 3-9 月 → 远月贴现分红使价差偏大(季节性正常)
            if o.product in DIVIDEND_PRODUCTS:
                m, hit = span[0], []
                while m <= span[1]:
                    if (m % 100) in DIVIDEND_MONTHS:
                        hit.append(m % 100)
                    m = sm._next_month(m)
                if hit:
                    o.conditions["分红季"] = f"跨{min(hit)}-{max(hit)}月,价差含分红贴现(偏大属正常)"
        if o.product in ADVISORY_ONLY and not o.flag.startswith("规则断层"):
            o.flag = (o.flag + "; " if o.flag else "") + "仅提示(现金交割:集运/国债,不参与)"


def run_screen(U: Universe, only=None) -> List[Opportunity]:
    names = only or list(STRATEGIES)
    res = []
    for n in names:
        try:
            res.extend(STRATEGIES[n](U, n))
        except Exception as e:
            print(f"[策略 {n} 异常] {e}")
    _annotate(U, res)
    # 跨规则断层且做空新标准腿的机会直接剔除(用户2026-08-12)
    res = [o for o in res if o.flag != "规则断层-剔除(空新标准腿)"]
    res.sort(key=lambda o: o.score, reverse=True)
    return res


def opps_to_df(opps: List[Opportunity]) -> pd.DataFrame:
    rows = []
    for o in opps:
        d = asdict(o)
        d["conditions"] = "; ".join(f"{k}={v}" for k, v in o.conditions.items())
        d["suggest"] = "; ".join(f"{k}={v}" for k, v in o.suggest.items())
        rows.append(d)
    cols = ["ts", "strategy", "kind", "product", "structure", "direction", "score",
            "liquid", "flag", "rollable", "curve", "slope_pctl", "warrant_pctl", "book",
            "conditions", "tradeable", "hold_period", "suggest", "detail"]
    return pd.DataFrame(rows, columns=cols)


def emit(opps: List[Opportunity], out_dir=None):
    out_dir = out_dir or os.path.join(HERE, "output")
    os.makedirs(out_dir, exist_ok=True)
    df = opps_to_df(opps)
    df.to_csv(os.path.join(out_dir, "opportunities.csv"), index=False, encoding="utf-8-sig")
    return df


if __name__ == "__main__":
    import time
    from tqsdk import TqApi, TqAuth
    import spread_trader as st
    cfg = st.load_trade_config()
    api = TqApi(auth=TqAuth(cfg["user"], cfg["password"]))
    try:
        meta = sm.load_product_meta(os.path.join(HERE, "all_product_config20260629.xlsx"))
        U = build_universe(api, meta, products=["m", "i", "CF", "RM", "SA"])
        print(f"Universe: 合约{len(U.contracts)} 段{len(U.spreads)} 蝶{len(U.butterflies)}")
        opps = run_screen(U, only=["折角修复", "注销月折价"])
        df = emit(opps)
        print(f"命中机会 {len(opps)} 条 -> output/opportunities.csv")
        if len(df):
            print(df.head(12).to_string(index=False))
    finally:
        api.close()
