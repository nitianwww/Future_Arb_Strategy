# -*- coding: utf-8 -*-
"""
历史同期统计页：月份对价差(如 M 9-1) 的跨年版本对比 + 同构对 A/B/A−B 三图页。

规则（用户确认）：
  - 每个年版 = 固定的一对合约（m2609-m2701、m2509-m2601...），绝不跨年拼接;
  - 剔除任一腿进入交割月后的数据点（截到近腿交割月首日前）;
  - x轴 = MM-DD，锚定近腿月份重排（9-1对: 09→...→08）; 今年红、逐年后退彩虹色、今年加粗。
数据：TqSdk 日K(含过期合约)，本地缓存 data/daily_k/（过期不可变、活跃当日刷新）。

用法：
  python hist_stats.py M 9 1              # 单价差页 M_09-01.html
  python hist_stats.py M 1-5 vs 9-1       # 同构对页 M_0105_vs_0901.html
输出: output/detail/hist/
"""

import os
import sys
import json
import time
import html as html_mod
from datetime import datetime, date

import pandas as pd
from tqsdk import TqApi, TqAuth

import spread_monitor as sm
import screener as sc

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "daily_k")
OUT_DIR = os.path.join(HERE, "output", "detail", "hist")
N_VERSIONS = 6


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 日K缓存（过期合约不可变；活跃合约当日刷新）
# --------------------------------------------------------------------------- #
def _cache_path(symbol):
    return os.path.join(CACHE_DIR, symbol.replace(".", "_") + ".parquet")


def _cache_fresh(fp):
    if not os.path.exists(fp):
        return False
    try:
        df = pd.read_parquet(fp)
        if "oi" not in df.columns:               # 旧schema(无OI) -> 触发升级重取
            return False
        last = pd.to_datetime(df["date"].iloc[-1]).date()
        if (date.today() - last).days > 45:      # 早已到期 -> 不可变
            return True
        return datetime.fromtimestamp(os.path.getmtime(fp)).date() == date.today()
    except Exception:
        return False


def _trim_recycled(df):
    """剔除同码上一代合约的数据。CZCE三位数代码每10年循环(如MA610=2016年10月与2026年10月),
    TqSdk按代码取K线会把两代连在一起(用户实例: MA610-611的2026年版混入2016年数据)。
    同一代合约内不会有>180天断档 -> 出现断档即换代, 只保留最后一段。"""
    if len(df) < 2:
        return df
    d = pd.to_datetime(df["date"])
    gaps = d.diff().dt.days.to_numpy()
    brk = [i for i, g in enumerate(gaps) if g and g > 180]
    if brk:
        df = df.iloc[brk[-1]:].reset_index(drop=True)
    return df


def get_daily_batch(api, symbols):
    """批量取日K(带缓存)。返回 {symbol: DataFrame(date,close)}。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out, todo = {}, []
    for s in symbols:
        fp = _cache_path(s)
        if _cache_fresh(fp):
            out[s] = _trim_recycled(pd.read_parquet(fp))
        else:
            todo.append(s)
    for i in range(0, len(todo), 12):            # 分批订阅
        batch = todo[i:i + 12]
        serials = {}
        for s in batch:
            try:
                serials[s] = api.get_kline_serial(s, 86400, data_length=500)
            except Exception as e:
                log(f"  {s} 日K订阅失败: {e}")
        t0 = time.time()
        while time.time() - t0 < 12:
            api.wait_update(deadline=time.time() + 0.5)
        for s, k in serials.items():
            try:
                kv = k[(~k["close"].isna()) & (k["datetime"] > 0)]
                df = pd.DataFrame({
                    "date": [datetime.fromtimestamp(t / 1e9).strftime("%Y-%m-%d") for t in kv["datetime"]],
                    "close": kv["close"].astype(float).values,
                    "oi": kv["close_oi"].fillna(0).astype(float).values})
                df = _trim_recycled(df.drop_duplicates("date").reset_index(drop=True))
                if len(df):
                    df.to_parquet(_cache_path(s))
                    out[s] = df
            except Exception as e:
                log(f"  {s} 日K处理失败: {e}")
    return out


# --------------------------------------------------------------------------- #
# 年版发现
# --------------------------------------------------------------------------- #
_PROD_SYMS = {}


def product_legs(api, prod):
    """品种全部合约(含过期, 排除F)。"""
    if prod in _PROD_SYMS:
        return _PROD_SYMS[prod]
    syms = set()
    for expired in (False, True):
        for ex in sm.FUTURE_EXCHANGES:
            for s in api.query_quotes(ins_class="FUTURE", exchange_id=ex, expired=expired) or []:
                if not sm.is_settle_f(s) and sc.base_product(s) == prod:
                    syms.add(s)
    _PROD_SYMS[prod] = sorted(syms)
    return _PROD_SYMS[prod]


def _delivery_year(sym, df):
    """交割年份：过期合约=最后K线年；活跃合约=按代码月份推(3位码取未来最近十年)。"""
    last = pd.to_datetime(df["date"].iloc[-1]).date()
    if (date.today() - last).days > 5:
        return last.year
    digits = "".join(ch for ch in sm.code_of(sym) if ch.isdigit())
    if len(digits) == 4:
        return 2000 + int(digits[:2])
    d, cur = int(digits[0]), date.today().year
    y = (cur // 10) * 10 + d
    return y if y >= cur - 1 else y + 10


def find_versions(api, prod, nearM, farM, n=N_VERSIONS):
    """返回 [{year, near, far, dfn, dff}]，year=近腿交割年，按年升序，最多n个。"""
    legs = product_legs(api, prod)
    nears = [s for s in legs if sm.month_key(s) % 100 == nearM]
    fars = [s for s in legs if sm.month_key(s) % 100 == farM]
    dfs = get_daily_batch(api, nears + fars)
    near_y = {s: _delivery_year(s, dfs[s]) for s in nears if s in dfs and len(dfs[s]) > 20}
    far_y = {s: _delivery_year(s, dfs[s]) for s in fars if s in dfs and len(dfs[s]) > 20}
    delta = 0 if farM > nearM else 1
    vers = []
    for s, ny in sorted(near_y.items(), key=lambda kv: kv[1]):
        fy = ny + delta
        cand = [f for f, yy in far_y.items() if yy == fy]
        if cand:
            vers.append(dict(year=ny, near=s, far=cand[0], dfn=dfs[s], dff=dfs[cand[0]]))
    return vers[-n:]


def version_series(v, nearM):
    """单年版价差序列，截到近腿交割月首日前(任一腿入交割月即剔除)。返回 [(date, mmdd, val)]。"""
    cut = f"{v['year']:04d}-{nearM:02d}-01"
    m = v["dfn"].merge(v["dff"], on="date", suffixes=("_n", "_f"))
    m = m[m["date"] < cut]
    return [(r["date"], r["date"][5:], round(float(r["close_n"] - r["close_f"]), 2))
            for _, r in m.iterrows()]


def _ordkey(mmdd, anchorM):
    mo, dd = int(mmdd[:2]), int(mmdd[3:])
    return ((mo - anchorM) % 12, dd)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def peer_stats(series_by_year, anchorM):
    years = sorted(series_by_year)
    if not years:
        return {}
    cur = years[-1]
    cur_pts = series_by_year[cur]
    if not cur_pts:
        return {}
    cur_mmdd, cur_val = cur_pts[-1][1], cur_pts[-1][2]
    ck = _ordkey(cur_mmdd, anchorM)
    peers = {}
    for y in years[:-1]:
        best = None
        for _, mmdd, val in series_by_year[y]:
            k = _ordkey(mmdd, anchorM)
            if k <= ck and (best is None or k > best[0]):
                best = (k, val)
        if best:
            peers[y] = best[1]
    all_hist = [v for y in years[:-1] for _, _, v in series_by_year[y]]
    def pct(x, arr):
        return round(100.0 * sum(1 for a in arr if a < x) / len(arr), 1) if arr else None
    return {"当前": cur_val, "同期各年": peers,
            "同期分位%": pct(cur_val, list(peers.values())),
            "全期分位%": pct(cur_val, all_hist),
            "历史均值": round(sum(all_hist) / len(all_hist), 1) if all_hist else None}


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
_PAGE_HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
body{margin:12px;background:#f5f6fa;font-family:"Microsoft YaHei",sans-serif;font-size:13px}
h2{margin:4px 0 8px} .meta{color:#888;font-size:12px;margin-bottom:8px}
.card{background:#fff;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.08);padding:8px;margin-bottom:12px}
.card h3{margin:2px 6px 4px;font-size:14px}
.chart{width:100%;height:840px}
table.st{border-collapse:collapse;margin:6px} table.st td,table.st th{border:1px solid #e3e6ec;padding:3px 10px;font-size:12px}
table.st th{background:#eef1f7}
</style></head><body>
<h2>__TITLE__</h2>
<div class="meta">__META__ · 每年版=固定合约(不跨年拼接) · 已剔除任一腿进入交割月后的数据 · 今年红/加粗</div>
<script>
const YCOLORS=['#e64545','#f28c28','#d4a800','#4caf50','#00bcd4','#4a7fd4','#9b59b6'];
const ycolor=(ys,y)=>YCOLORS[Math.min(ys.length-1-ys.indexOf(y),YCOLORS.length-1)];
function drawOverlay(elId, data){
  const years=Object.keys(data.series).sort();
  const el=document.getElementById(elId);
  if(!years.length){el.innerHTML='<p style="color:#999;padding:16px">无数据</p>';return;}
  const c=echarts.init(el);
  const days=data.days;
  const series=years.map(y=>{
    const m=new Map(data.series[y]);
    return {name:y,type:'line',showSymbol:false,connectNulls:true,
      data:days.map(d=>m.has(d)?m.get(d):null),
      itemStyle:{color:ycolor(years,y)},
      lineStyle:{width:y===years[years.length-1]?3:1.2,color:ycolor(years,y)}};});
  c.setOption({tooltip:{trigger:'axis'},legend:{data:years},
    xAxis:{type:'category',data:days,axisLabel:{interval:Math.ceil(days.length/14)}},
    yAxis:{type:'value',scale:true},series});
}
</script>
"""


def _chart_block(cid, title, series_by_year, anchorM, stats):
    days = sorted({mmdd for pts in series_by_year.values() for _, mmdd, _ in pts},
                  key=lambda d: _ordkey(d, anchorM))
    payload = {"days": days,
               "series": {str(y): [[mmdd, v] for _, mmdd, v in pts]
                          for y, pts in series_by_year.items()}}
    st_rows = ""
    if stats:
        peers = " ".join(f"{y}:{v}" for y, v in sorted(stats.get("同期各年", {}).items()))
        st_rows = (f"<table class='st'><tr><th>当前</th><th>同期分位%</th><th>全期分位%</th>"
                   f"<th>历史均值</th><th>同期各年值</th></tr>"
                   f"<tr><td><b>{stats.get('当前')}</b></td><td>{stats.get('同期分位%')}</td>"
                   f"<td>{stats.get('全期分位%')}</td><td>{stats.get('历史均值')}</td>"
                   f"<td>{html_mod.escape(peers)}</td></tr></table>")
    return (f"<div class='card'><h3>{html_mod.escape(title)}</h3>{st_rows}"
            f"<div id='{cid}' class='chart'></div></div>"
            f"<script>drawOverlay('{cid}', {json.dumps(payload, ensure_ascii=False)});</script>")


def build_spread_hist(api, prod, nearM, farM):
    """单价差历史同期页。返回相对 output/detail 的 href 或 None。"""
    vers = find_versions(api, prod, nearM, farM)
    if len(vers) < 2:
        return None
    sby = {v["year"]: version_series(v, nearM) for v in vers}
    sby = {y: pts for y, pts in sby.items() if pts}
    if len(sby) < 2:
        return None
    stats = peer_stats(sby, nearM)
    title = f"{prod} {nearM:02d}-{farM:02d} 月差 历史同期"
    contracts = " ".join(f"{v['year']}:{sm.code_of(v['near'])}-{sm.code_of(v['far'])}" for v in vers)
    blocks = _chart_block("c1", title, sby, nearM, stats)
    slope = slope_series_cached(api, prod)
    if slope:
        # 展示取负(用户口径): 价差习惯近−远, 回归斜率是远−近向; 取负后 正=back 负=contango
        sneg = {y: [(d, mmdd, round(-v, 3)) for d, mmdd, v in pts] for y, pts in slope.items()}
        blocks += _chart_block("c2", f"图2: {prod} 期限结构斜率季节性(取负=近−远口径: 正=back 负=contango, "
                                     f"持仓量加权回归, 元/月)",
                               sneg, 1, peer_stats(sneg, 1))
    doc = (_PAGE_HEAD.replace("__TITLE__", title)
           .replace("__META__", f"生成 {datetime.now():%Y-%m-%d %H:%M} · 年版合约 {contracts}")
           + blocks + "</body></html>")
    os.makedirs(OUT_DIR, exist_ok=True)
    fn = f"{prod}_{nearM:02d}-{farM:02d}.html"
    with open(os.path.join(OUT_DIR, fn), "w", encoding="utf-8") as f:
        f.write(doc)
    return f"hist/{fn}"


def build_pair_hist(api, prod, a, b):
    """同构对页：A、B、A−B 三图。a/b=(nearM,farM)。"""
    vA = find_versions(api, prod, *a)
    vB = find_versions(api, prod, *b)
    if len(vA) < 2 or len(vB) < 2:
        return None
    sA = {v["year"]: version_series(v, a[0]) for v in vA}
    sB = {v["year"]: version_series(v, b[0]) for v in vB}
    sA = {y: p for y, p in sA.items() if p}
    sB = {y: p for y, p in sB.items() if p}
    # A-B: 年版按日期窗重叠配对(贪心)
    diff = {}
    usedB = set()
    for ya, pa in sorted(sA.items()):
        da = {d: v for d, _, v in pa}
        best, bestn = None, 0
        for yb, pb in sB.items():
            if yb in usedB:
                continue
            n = sum(1 for d, _, _ in pb if d in da)
            if n > bestn:
                best, bestn = yb, n
        if best and bestn >= 20:
            usedB.add(best)
            db = {d: (mmdd, v) for d, mmdd, v in sB[best]}
            pts = [(d, db[d][0], round(da[d] - db[d][1], 2)) for d, _, _ in sA[ya] if d in db]
            if pts:
                diff[ya] = pts
    anchor_diff = b[0]        # 差值窗以更早到期一侧(通常B近腿)为锚
    blocks = []
    blocks.append(_chart_block("cA", f"A: {prod} {a[0]:02d}-{a[1]:02d} 历史同期", sA, a[0], peer_stats(sA, a[0])))
    blocks.append(_chart_block("cB", f"B: {prod} {b[0]:02d}-{b[1]:02d} 历史同期", sB, b[0], peer_stats(sB, b[0])))
    if diff:
        blocks.append(_chart_block("cD", f"A−B: ({a[0]:02d}-{a[1]:02d}) − ({b[0]:02d}-{b[1]:02d}) 历史同期",
                                   diff, anchor_diff, peer_stats(diff, anchor_diff)))
    slope = slope_series_cached(api, prod)
    if slope:
        blocks.append(_chart_block("cS", f"图2: {prod} 期限结构斜率季节性(持仓量加权回归, 元/月)",
                                   slope, 1, peer_stats(slope, 1)))
    title = f"{prod} 同构对 {a[0]:02d}-{a[1]:02d} vs {b[0]:02d}-{b[1]:02d} 历史同期"
    doc = (_PAGE_HEAD.replace("__TITLE__", title)
           .replace("__META__", f"生成 {datetime.now():%Y-%m-%d %H:%M}")
           + "".join(blocks) + "</body></html>")
    os.makedirs(OUT_DIR, exist_ok=True)
    fn = f"{prod}_{a[0]:02d}{a[1]:02d}_vs_{b[0]:02d}{b[1]:02d}.html"
    with open(os.path.join(OUT_DIR, fn), "w", encoding="utf-8") as f:
        f.write(doc)
    return f"hist/{fn}"


# --------------------------------------------------------------------------- #
# 缓存预热（独立连接分块：单连接订阅过多K线会全员超时）
# --------------------------------------------------------------------------- #
def collect_pair_symbols(api, pairs):
    """pairs=[(prod,nearM,farM)] -> 需要日K的全部合约代码。"""
    need = set()
    for prod, nM, fM in pairs:
        need.update(product_legs(api, prod))   # 全部月份(期限斜率回归需要整条曲线)
    return sorted(need)


def _new_api(user, pwd, tries=3):
    """建连接；auth 服务偶发超时(30s无响应)，失败退避重试而非整体崩掉。"""
    for k in range(tries):
        try:
            return TqApi(auth=TqAuth(user, pwd))
        except Exception as e:
            if k == tries - 1:
                raise
            wait = 15 * (k + 1)
            log(f"  建连失败({e.__class__.__name__})，{wait}s 后重试 {k + 1}/{tries - 1}")
            time.sleep(wait)


def warm_cache(user, pwd, symbols, chunk=250):
    """分块预热日K缓存，每块用一条全新连接（防止订阅积累导致超时）。"""
    todo = [s for s in symbols if not _cache_fresh(_cache_path(s))]
    log(f"预热日K缓存: 待取 {len(todo)}/{len(symbols)}")
    for i in range(0, len(todo), chunk):
        part = todo[i:i + chunk]
        api = _new_api(user, pwd)
        try:
            get_daily_batch(api, part)
        finally:
            api.close()
        log(f"  预热进度 {min(i + chunk, len(todo))}/{len(todo)}")


# --------------------------------------------------------------------------- #
# 期限结构斜率（持仓量加权回归，元/月）
# --------------------------------------------------------------------------- #
def _wls_slope(xyw):
    """加权最小二乘斜率。xyw=[(x,y,w)]"""
    sw = sum(w for _, _, w in xyw)
    if sw <= 0:
        return None
    xb = sum(w * x for x, _, w in xyw) / sw
    yb = sum(w * y for _, y, w in xyw) / sw
    den = sum(w * (x - xb) ** 2 for x, _, w in xyw)
    if den <= 0:
        return None
    return sum(w * (x - xb) * (y - yb) for x, y, w in xyw) / den


_SLOPE_MEMO = {}


def slope_series_cached(api, prod):
    """品种期限结构斜率日序列(按年分组): 每日对全部存续合约 close~距到期月数 做OI加权回归。
    返回 {year: [(date, mmdd, slope元/月)...]}"""
    if prod in _SLOPE_MEMO:
        return _SLOPE_MEMO[prod]
    import collections
    legs = product_legs(api, prod)
    dfs = get_daily_batch(api, legs)
    bydate = collections.defaultdict(list)
    for s, df in dfs.items():
        if len(df) < 10 or "oi" not in df.columns:
            continue
        mi = _delivery_year(s, df) * 12 + sm.month_key(s) % 100
        for d, c, o in zip(df["date"], df["close"], df["oi"]):
            if o and o > 0:
                bydate[d].append((mi, float(c), float(o)))
    out = {}
    for d, pts in bydate.items():
        obs = int(d[:4]) * 12 + int(d[5:7])
        xyw = [(mi - obs, c, o) for mi, c, o in pts if mi >= obs]
        if len(xyw) < 3:
            continue
        sl = _wls_slope(xyw)
        if sl is None:
            continue
        out.setdefault(d[:4], []).append((d, d[5:], round(sl, 3)))
    for y in out:
        out[y].sort()
    _SLOPE_MEMO[prod] = out
    return out


# --------------------------------------------------------------------------- #
# 统计策略数据供给（screener C类通过 set_hist_provider 注入使用）
# --------------------------------------------------------------------------- #
_SERIES_MEMO = {}


def versions_series_cached(api, prod, nearM, farM):
    """{year: [(date,mmdd,val)...]}，进程内memo + 磁盘日K缓存。"""
    key = (prod, nearM, farM)
    if key in _SERIES_MEMO:
        return _SERIES_MEMO[key]
    try:
        vers = find_versions(api, prod, nearM, farM)
        out = {v["year"]: version_series(v, nearM) for v in vers}
        out = {y: p for y, p in out.items() if len(p) >= 20}
    except Exception as e:
        log(f"  历史序列失败 {prod} {nearM}-{farM}: {e}")
        out = {}
    _SERIES_MEMO[key] = out
    return out


# --------------------------------------------------------------------------- #
# 从机会列表批量生成 + 结构名->链接映射
# --------------------------------------------------------------------------- #
def _months_of(code):
    return sm.month_key(code) % 100


def hist_jobs_from_opps(opps, main_only=True):
    """收集需生成的页面任务。返回 {structure: job}，job=('spread',prod,n,f) 或 ('pair',prod,a,b)。"""
    jobs = {}
    for o in opps:
        if main_only and (not o.liquid or o.flag):
            continue
        try:
            if o.kind == "spread":
                nc, fc = o.structure.split("-")
                jobs[o.structure] = ("spread", o.product, _months_of(nc), _months_of(fc))
            elif o.kind == "kink" and " | " in o.structure:
                # ABC三段类: 历史同期页挂 标的段B(中段) 的月份对
                nc, fc = o.structure.split(" | ")[1].split("-")
                jobs[o.structure] = ("spread", o.product, _months_of(nc), _months_of(fc))
            elif o.kind == "pair" and " vs " in o.structure:
                s1, s2 = o.structure.split(" vs ")
                a = tuple(_months_of(x) for x in s1.split("-"))
                b = tuple(_months_of(x) for x in s2.split("-"))
                jobs[o.structure] = ("pair", o.product, a, b)
        except Exception:
            continue
    return jobs


def ensure_pages(api, jobs):
    """生成缺页(同月份对复用同一页)。返回 {structure: href}。"""
    links, done = {}, {}
    for structure, job in jobs.items():
        try:
            if job[0] == "spread":
                key = ("s", job[1], job[2], job[3])
                if key not in done:
                    done[key] = build_spread_hist(api, job[1], job[2], job[3])
            else:
                key = ("p", job[1], job[2], job[3])
                if key not in done:
                    done[key] = build_pair_hist(api, job[1], job[2], job[3])
            if done[key]:
                links[structure] = done[key]
        except Exception as e:
            log(f"  历史页失败 {structure}: {e}")
    return links


def main():
    import spread_trader as st
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    cfg = st.load_trade_config()
    api = TqApi(auth=TqAuth(cfg["user"], cfg["password"]))
    try:
        prod = args[0].upper()
        if "vs" in [a.lower() for a in args]:
            i = [a.lower() for a in args].index("vs")
            a = tuple(int(x) for x in args[1].replace("-", " ").split())
            b = tuple(int(x) for x in args[i + 1].replace("-", " ").split())
            href = build_pair_hist(api, prod, a, b)
        else:
            href = build_spread_hist(api, prod, int(args[1]), int(args[2]))
        log(f"-> output/detail/{href}" if href else "版本不足(需>=2年)，未生成")
    finally:
        api.close()


if __name__ == "__main__":
    main()
