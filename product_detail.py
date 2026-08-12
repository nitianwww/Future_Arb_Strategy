# -*- coding: utf-8 -*-
"""
品种详情图（仿价差监控工具4面板）：筛选出机会后把品种图拉出来。
  面板1 价格结构：各月合约 最新价/昨收 + 持仓量柱 + 仓单注销月竖线
  面板2 相邻价差：价差柱(标注盘口bid/ask) + 仓储成本线 + 完全成本线(fullcarry含时间贴水)
  面板3 跨期价差小图墙：各相邻月差近40个交易日(日K收盘合成)走势
  面板4 主连季节性：主连收盘按年叠加(近6年)
用法:
  python product_detail.py MA           # 单品种
  python product_detail.py MA Y CY JM   # 多品种
  python product_detail.py --from-opps  # 从 output/opportunities.csv 命中品种自动出图
输出: output/detail/{品种}.html (ECharts CDN)
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime

import pandas as pd
from tqsdk import TqApi, TqAuth

import spread_monitor as sm
import spread_trader as st
import screener as sc
import hist_stats as hs

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _pump(api, n=8):
    for _ in range(n):
        api.wait_update(deadline=time.time() + 0.5)


_RQ_READY = False


def _rq_init(user, pwd):
    global _RQ_READY
    if _RQ_READY:
        return True
    try:
        import rqdatac
        rqdatac.init(user, pwd)
        _RQ_READY = True
        return True
    except Exception as e:
        log(f"rqdatac 初始化失败(仓单面板留空): {e}")
        return False


def fetch_warrants(prod, user, pwd):
    """RQData 仓单(on_warrant)日度 2021+，按年分组。本地缓存当日有效。"""
    cache_dir = os.path.join(HERE, "data", "warrants")
    os.makedirs(cache_dir, exist_ok=True)
    fp = os.path.join(cache_dir, f"{prod}.parquet")
    df = None
    if os.path.exists(fp) and datetime.fromtimestamp(os.path.getmtime(fp)).date() == datetime.now().date():
        df = pd.read_parquet(fp)
    else:
        if not _rq_init(user, pwd):
            return {}
        import rqdatac
        try:
            raw = rqdatac.futures.get_warehouse_stocks(
                prod, start_date="2021-01-01", end_date=datetime.now().strftime("%Y-%m-%d"))
        except Exception as e:
            log(f"{prod} 仓单获取失败: {e}")
            return {}
        if raw is None or len(raw) == 0:
            return {}
        df = raw.reset_index()[["date", "on_warrant"]]
        try:
            df.to_parquet(fp)
        except Exception:
            pass
    out = {}
    for _, r in df.iterrows():
        d = pd.to_datetime(str(r["date"]))
        v = r["on_warrant"]
        if pd.notna(v):
            out.setdefault(str(d.year), []).append([d.strftime("%m-%d"), float(v)])
    return out


def oi_seasonal(km, prod, user, pwd):
    """主力合约持仓季节性：x=日历(MM-DD)，y=主力合约持仓量(主连close_oi)，一年一条线。
    主力切换用 RQData get_dominant 标注；最新年份的切换画竖线+合约名，各年逐点合约进tooltip。"""
    out = {"years": {}, "switch": []}
    if km is None:
        return out
    try:
        kv = km[(~km["close_oi"].isna()) & (km["datetime"] > 0)]
    except Exception:
        return out
    if len(kv) == 0:
        return out
    dates = [datetime.fromtimestamp(t / 1e9) for t in kv["datetime"]]
    dom = {}
    if _rq_init(user, pwd):
        import rqdatac
        try:
            s = rqdatac.futures.get_dominant(prod, start_date=dates[0].strftime("%Y-%m-%d"),
                                             end_date=dates[-1].strftime("%Y-%m-%d"))
            if s is not None:
                for idx, val in s.items():
                    dom[pd.to_datetime(str(idx)).strftime("%Y-%m-%d")] = str(val)
        except Exception as e:
            log(f"{prod} 主力序列获取失败(切换标注留空): {e}")
    years = {}
    for d, (_, r) in zip(dates, kv.iterrows()):
        y = str(d.year)
        years.setdefault(y, []).append({"d": d.strftime("%m-%d"), "v": float(r["close_oi"]),
                                        "c": dom.get(d.strftime("%Y-%m-%d"), "")})
    out["years"] = years
    ylast = max(years)
    prev = None
    for p in years[ylast]:
        if p["c"]:
            if prev is not None and p["c"] != prev:
                out["switch"].append({"d": p["d"], "c": p["c"]})
            prev = p["c"]
    return out


def fetch(api, meta, prod, user=None, password=None):
    """取一个品种的全部图数据。"""
    # 详情页展示完整期限结构: 不用筛选默认的8个月窗口(会造成如 03 后直接 05 的空洞)
    U = sc.build_universe(api, meta, products=[prod], max_months=14)
    key = prod.upper()
    cons = U.by_product.get(key) or U.by_product.get(sc.base_product(f"X.{prod}1"), [])
    if not cons:
        # by_product 键可能是 product_of 结果(如 VF)，退化为找 base 匹配
        for k, v in U.by_product.items():
            if v and v[0].product == key:
                cons = v; break
    if not cons:
        raise SystemExit(f"未找到品种 {prod} 的合约")
    spreads = [s for s in U.spreads if s.near.product == key]
    m = meta.get(key, {})

    # 日K：每合约60根(昨收+小图墙)
    kl = {}
    for c in cons:
        kl[c.symbol] = api.get_kline_serial(c.symbol, 86400, data_length=60)
    # 主连：季节图
    code = cons[0].symbol.split(".")[1]
    import re
    prod_code = re.match(r"^[A-Za-z]+", code).group()
    main_sym = f"KQ.m@{cons[0].exchange}.{prod_code}"
    try:
        km = api.get_kline_serial(main_sym, 86400, data_length=1500)
    except Exception:
        km = None
    _pump(api, 12)

    # ---- 面板1 价格结构 ----
    struct = {"labels": [], "last": [], "prev": [], "oi": [], "cancel": []}
    for c in cons:
        struct["labels"].append(sm.code_of(c.symbol))
        struct["last"].append(c.last)
        k = kl[c.symbol]
        prev = None
        try:
            kv = k[~k["close"].isna()]
            if len(kv) >= 2:
                prev = float(kv.iloc[-2]["close"])
        except Exception:
            pass
        struct["prev"].append(prev)
        struct["oi"].append(c.oi)
        struct["cancel"].append(bool(c.is_cancel_month))

    # ---- 面板2 相邻价差 ----
    # no_roll(用户口径): 竖线标"无法转抛"的段=[近,远)区间含注销月(如Y 11月注销→11-1段不可转抛),
    # 而不是远腿恰为注销月的段(旧口径误标在9-11上)
    sp_panel = {"labels": [], "mid": [], "bid": [], "ask": [], "stor": [], "full": [], "no_roll": []}
    fee = m.get("fee") or 0.0
    cancel_set = m.get("cancel", set())
    for s in spreads:
        sp_panel["labels"].append(f"{sm.code_of(s.near.symbol)[-4:] if sm.code_of(s.near.symbol)[-1].isdigit() else sm.code_of(s.near.symbol)}-{sm.code_of(s.far.symbol)[-4:]}")
        sp_panel["mid"].append(s.mid)
        sp_panel["bid"].append(s.bid)
        sp_panel["ask"].append(s.ask)
        sp_panel["stor"].append(round(-fee * 30 * (s.months or 0), 2))
        sp_panel["full"].append(round(-s.fullcarry, 2) if s.fullcarry else None)
        sp_panel["no_roll"].append(not sm.can_roll(s.near.month, s.far.month, cancel_set))

    # ---- 面板3 小图墙：日K合成价差近40天 ----
    walls = []
    for s in spreads:
        kn, kf = kl[s.near.symbol], kl[s.far.symbol]
        try:
            dn = kn[~kn["close"].isna()][["datetime", "close"]]
            df_ = kf[~kf["close"].isna()][["datetime", "close"]]
            mg = dn.merge(df_, on="datetime", suffixes=("_n", "_f"))
            mg["spread"] = mg["close_n"] - mg["close_f"]
            mg = mg.tail(40)
            dates = [datetime.fromtimestamp(t / 1e9).strftime("%m-%d") for t in mg["datetime"]]
            walls.append({"name": f"{sm.code_of(s.near.symbol)}&{sm.code_of(s.far.symbol)}",
                          "dates": dates, "vals": [round(float(v), 2) for v in mg["spread"]],
                          "now": s.mid})
        except Exception:
            continue

    # ---- 面板4 主连季节性 ----
    seasonal = {}
    if km is not None:
        try:
            kv = km[~km["close"].isna()]
            for _, r in kv.iterrows():
                d = datetime.fromtimestamp(r["datetime"] / 1e9)
                seasonal.setdefault(str(d.year), []).append([d.strftime("%m-%d"), round(float(r["close"]), 2)])
        except Exception:
            pass

    # ---- 面板5 主力合约持仓季节性(主连OI + RQData主力切换标注) ----
    oi_seas = oi_seasonal(km, key, user, password)

    # ---- 面板6 仓单季节性 ----
    warrants = fetch_warrants(key, user, password) if user else {}

    # ---- 面板7 期限结构斜率季节性(持仓量加权回归) ----
    # 展示取负(用户口径): 价差习惯近月−远月, 回归斜率是 价格~距到期月数(远−近向),
    # 取负后 正=back(近高远低) 负=contango, 与价差符号一致
    try:
        slope_raw = hs.slope_series_cached(api, key)
        slope_seas = {y: [[mmdd, round(-v, 3)] for _, mmdd, v in pts] for y, pts in slope_raw.items()}
    except Exception as e:
        log(f"{key} 斜率序列失败: {e}")
        slope_seas = {}

    return dict(product=key, exchange=cons[0].exchange, ts=U.ts,
                cancel_months=sorted(m.get("cancel", set())), fee=fee,
                struct=struct, spreads=sp_panel, walls=walls, seasonal=seasonal,
                oi_seas=oi_seas, warrants=warrants, slope_seas=slope_seas)


HTML_TMPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>品种详情-__PROD__</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
body{margin:10px;background:#f5f6fa;font-family:"Microsoft YaHei",sans-serif}
h2{margin:4px 0 10px;font-size:18px} .meta{color:#888;font-size:12px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.card{background:#fff;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.08);padding:8px}
.card h3{margin:2px 6px 4px;font-size:14px;color:#333}
.chart{width:100%;height:510px}
#wall{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
#wall .mini{height:165px}
</style></head><body>
<h2>价差监控 · 品种详情：__PROD__ (__EX__)</h2>
<div class="meta">快照 __TS__ · 注销月 __CANCEL__ · 仓储费 __FEE__/天 · 数据: TqSdk实时+日K</div>
<div class="grid">
<div class="card"><h3>__PROD__ 价格结构</h3><div id="p1" class="chart"></div></div>
<div class="card"><h3>__PROD__ 相邻价差（柱=中枢, 标注=买/卖盘口）</h3><div id="p2" class="chart"></div></div>
<div class="card"><h3>__PROD__ 跨期价差走势（日K合成, 近40交易日）</h3><div id="wall"></div></div>
<div class="card"><h3>__PROD__ 主连季节性走势</h3><div id="p4" class="chart"></div></div>
<div class="card"><h3>__PROD__ 主力合约持仓季节性（一年一线, 竖线=主力换月）</h3><div id="p5" class="chart"></div></div>
<div class="card"><h3>__PROD__ 仓单季节性（注册仓单, 按年叠加）</h3><div id="p6" class="chart"></div></div>
<div class="card"><h3>__PROD__ 期限结构斜率季节性（持仓量加权回归, 元/月, 取负=近−远口径: 正=back 负=contango, 按年叠加）</h3><div id="p7" class="chart"></div></div>
</div>
<script>
const D = __DATA__;
// 年度叠加统一配色: 今年红,去年橙,前年黄...(红橙黄绿青蓝紫按时间倒序)
const YCOLORS=['#e64545','#f28c28','#d4a800','#4caf50','#00bcd4','#4a7fd4','#9b59b6'];
const ycolor=(years,y)=>YCOLORS[Math.min(years.length-1-years.indexOf(y),YCOLORS.length-1)];
const ywidth=(years,y)=>y===years[years.length-1]?3:1.2;
// ---- 面板1 价格结构 ----
(()=>{
const c=echarts.init(document.getElementById('p1'));
const marks=D.struct.labels.map((l,i)=>D.struct.cancel[i]?{xAxis:l}:null).filter(Boolean);
c.setOption({tooltip:{trigger:'axis'},legend:{data:['最新价','昨收价','持仓量']},
 xAxis:{type:'category',data:D.struct.labels,axisLabel:{rotate:45}},
 yAxis:[{type:'value',scale:true,name:'价格'},{type:'value',name:'持仓量'}],
 series:[
  {name:'最新价',type:'line',data:D.struct.last,color:'#e64545',label:{show:true,fontSize:10}},
  {name:'昨收价',type:'line',data:D.struct.prev,color:'#f0b800'},
  {name:'持仓量',type:'bar',yAxisIndex:1,data:D.struct.oi,color:'#7db8e8',opacity:.7,
   markLine:{symbol:'none',lineStyle:{color:'#2bb3a3'},label:{formatter:'注销月'},data:marks}}
 ]});
})();
// ---- 面板2 相邻价差 ----
(()=>{
const c=echarts.init(document.getElementById('p2'));
const marks=D.spreads.labels.map((l,i)=>D.spreads.no_roll[i]?{xAxis:l}:null).filter(Boolean);
c.setOption({tooltip:{trigger:'axis',formatter:ps=>{
   const i=ps[0].dataIndex;
   return D.spreads.labels[i]+'<br>中枢 '+D.spreads.mid[i]+'<br>盘口 '+D.spreads.bid[i]+' / '+D.spreads.ask[i]
     +'<br>仓储成本 '+D.spreads.stor[i]+'<br>完全成本 '+D.spreads.full[i];}},
 legend:{data:['价差','仓储成本','完全成本']},
 xAxis:{type:'category',data:D.spreads.labels,axisLabel:{rotate:45}},
 yAxis:{type:'value',scale:true},
 series:[
  {name:'价差',type:'bar',data:D.spreads.mid,color:'#7db8e8',
   label:{show:true,position:'top',fontSize:10,formatter:p=>{const i=p.dataIndex;
     return p.value+'\\n('+D.spreads.bid[i]+'/'+D.spreads.ask[i]+')';}},
   markLine:{symbol:'none',lineStyle:{color:'#e64545'},label:{formatter:'不可转抛(跨注销)'},data:marks}},
  {name:'仓储成本',type:'line',data:D.spreads.stor,color:'#f0b800',label:{show:true,fontSize:9}},
  {name:'完全成本',type:'line',data:D.spreads.full,color:'#e64545',label:{show:true,fontSize:9}}
 ]});
})();
// ---- 面板3 小图墙 ----
(()=>{
const wall=document.getElementById('wall');
D.walls.forEach(w=>{
  const div=document.createElement('div');div.className='mini';wall.appendChild(div);
  const c=echarts.init(div);
  c.setOption({title:{text:w.name+'  '+(w.now==null?'':w.now),textStyle:{fontSize:10},top:0,left:4},
   grid:{top:18,left:34,right:8,bottom:14},tooltip:{trigger:'axis'},
   xAxis:{type:'category',data:w.dates,axisLabel:{fontSize:8,interval:Math.ceil(w.dates.length/4)}},
   yAxis:{type:'value',scale:true,axisLabel:{fontSize:8}},
   series:[{type:'line',data:w.vals,symbol:'circle',symbolSize:2.5,color:'#4a7fd4',
            markLine:w.now==null?undefined:{symbol:'none',lineStyle:{type:'dashed',color:'#e64545'},
              label:{show:false},data:[{yAxis:w.now}]}}]});
});
})();
// ---- 面板4 季节性 ----
(()=>{
const c=echarts.init(document.getElementById('p4'));
const years=Object.keys(D.seasonal).sort();
const days=[...new Set([].concat(...years.map(y=>D.seasonal[y].map(p=>p[0]))))].sort();
const series=years.map(y=>{const m=new Map(D.seasonal[y]);return {name:y,type:'line',showSymbol:false,
  data:days.map(d=>m.has(d)?m.get(d):null),connectNulls:true,
  itemStyle:{color:ycolor(years,y)},
  lineStyle:{width:ywidth(years,y),color:ycolor(years,y)}};});
c.setOption({tooltip:{trigger:'axis'},legend:{data:years},
 xAxis:{type:'category',data:days,axisLabel:{interval:Math.ceil(days.length/12)}},
 yAxis:{type:'value',scale:true},series});
})();
// ---- 面板5 主力合约持仓季节性(日历x轴, 一年一线, 主力换月竖线) ----
(()=>{
const O=D.oi_seas; const el=document.getElementById('p5');
const years=Object.keys(O.years||{}).sort();
if(!years.length){el.innerHTML='<p style="color:#999;padding:20px">暂无主连持仓数据</p>';return;}
const c=echarts.init(el);
const days=[...new Set([].concat(...years.map(y=>O.years[y].map(p=>p.d))))].sort();
const series=years.map(y=>{
  const m=new Map(O.years[y].map(p=>[p.d,p]));
  const isLast=y===years[years.length-1];
  return {name:y,type:'line',showSymbol:false,connectNulls:true,
    data:days.map(d=>{const p=m.get(d);return p?{value:p.v,c:p.c}:null;}),
    itemStyle:{color:ycolor(years,y)},
    lineStyle:{width:ywidth(years,y),color:ycolor(years,y)},
    markLine:(isLast&&O.switch.length)?{symbol:'none',
      lineStyle:{type:'dashed',color:'#c0392b'},
      label:{formatter:'{b}',fontSize:9,color:'#c0392b'},
      data:O.switch.map(s=>({xAxis:s.d,name:s.c}))}:undefined};
});
c.setOption({tooltip:{trigger:'axis',formatter:ps=>{
   const d=ps[0].axisValue;
   return d+'<br>'+ps.filter(p=>p.data).map(p=>
     p.seriesName+(p.data.c?(' '+p.data.c):'')+': '+Number(p.data.value).toLocaleString()).join('<br>');}},
 legend:{data:years},
 xAxis:{type:'category',data:days,axisLabel:{interval:Math.ceil(days.length/12)}},
 yAxis:{type:'value',name:'主力持仓量',scale:true},series});
})();
// ---- 面板6 仓单季节性 ----
(()=>{
const c=echarts.init(document.getElementById('p6'));
const years=Object.keys(D.warrants).sort();
if(!years.length){document.getElementById('p6').innerHTML='<p style="color:#999;padding:20px">暂无仓单数据(RQData未配置或该品种无仓单)</p>';return;}
const days=[...new Set([].concat(...years.map(y=>D.warrants[y].map(p=>p[0]))))].sort();
const today=new Date();const td=String(today.getMonth()+1).padStart(2,'0')+'-'+String(today.getDate()).padStart(2,'0');
const series=years.map((y,i)=>{const m=new Map(D.warrants[y]);return {name:y,type:'line',showSymbol:false,
  data:days.map(d=>m.has(d)?m.get(d):null),connectNulls:true,
  itemStyle:{color:ycolor(years,y)},
  lineStyle:{width:ywidth(years,y),color:ycolor(years,y)},
  markLine:(i===years.length-1)?{symbol:'none',lineStyle:{type:'dashed',color:'#999'},
    label:{formatter:'今日'},data:[{xAxis:td}]}:undefined};});
c.setOption({tooltip:{trigger:'axis'},legend:{data:years},
 xAxis:{type:'category',data:days,axisLabel:{interval:Math.ceil(days.length/12)}},
 yAxis:{type:'value',name:'仓单',scale:true},series});
})();
// ---- 面板7 期限结构斜率季节性 ----
(()=>{
const el=document.getElementById('p7');
const years=Object.keys(D.slope_seas||{}).sort();
if(!years.length){el.innerHTML='<p style="color:#999;padding:20px">暂无斜率数据(日K缓存待升级)</p>';return;}
const c=echarts.init(el);
const days=[...new Set([].concat(...years.map(y=>D.slope_seas[y].map(p=>p[0]))))].sort();
const series=years.map(y=>{const m=new Map(D.slope_seas[y]);return {name:y,type:'line',showSymbol:false,
  data:days.map(d=>m.has(d)?m.get(d):null),connectNulls:true,
  itemStyle:{color:ycolor(years,y)},
  lineStyle:{width:ywidth(years,y),color:ycolor(years,y)}};});
c.setOption({tooltip:{trigger:'axis'},legend:{data:years},
 xAxis:{type:'category',data:days,axisLabel:{interval:Math.ceil(days.length/12)}},
 yAxis:{type:'value',name:'−斜率(元/月,近−远)',scale:true},series});
})();
</script></body></html>"""


def render(data, out_dir):
    html_doc = (HTML_TMPL
                .replace("__PROD__", data["product"])
                .replace("__EX__", data["exchange"])
                .replace("__TS__", data["ts"])
                .replace("__CANCEL__", ",".join(f"{m}月" for m in data["cancel_months"]) or "无")
                .replace("__FEE__", str(data["fee"]))
                .replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{data['product']}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return path


def main():
    ap = argparse.ArgumentParser(description="品种详情图")
    ap.add_argument("products", nargs="*", help="品种代码，如 MA Y CY")
    ap.add_argument("--from-opps", action="store_true", help="从 output/opportunities.csv 取命中品种")
    a = ap.parse_args()

    prods = [p.upper() for p in a.products]
    if a.from_opps:
        opp = pd.read_csv(os.path.join(HERE, "output", "opportunities.csv"))
        prods += [p for p in opp["product"].astype(str).str.upper().unique() if p not in prods]
    if not prods:
        raise SystemExit("请给品种代码 或 --from-opps")

    cfg = st.load_trade_config()
    meta = sm.load_product_meta(os.path.join(HERE, "all_product_config20260629.xlsx"))
    out_dir = os.path.join(HERE, "output", "detail")
    api = TqApi(auth=TqAuth(cfg["user"], cfg["password"]))
    try:
        for p in prods:
            try:
                d = fetch(api, meta, p, user=cfg["user"], password=cfg["password"])
                path = render(d, out_dir)
                log(f"{p}: 合约{len(d['struct']['labels'])} 价差{len(d['spreads']['labels'])} "
                    f"小图{len(d['walls'])} 季节年{len(d['seasonal'])} -> {path}")
            except SystemExit as e:
                log(f"{p}: {e}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
