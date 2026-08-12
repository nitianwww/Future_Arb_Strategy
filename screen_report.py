# -*- coding: utf-8 -*-
"""
机会筛选一体化报告：筛选 -> 命中品种自动生成详情图 -> 机会列表HTML(品种名可点开详情页)。
  python screen_report.py            # 全市场
  python screen_report.py --no-detail  # 只出列表不重生成详情图(快)
输出:
  output/opportunities.html   机会列表(主列表/钓鱼桶/仅提示/未满足残差过滤 四段, 品种链接到 detail/)
  output/detail/{品种}.html    命中品种详情图
  output/opportunities.csv    机会明细
"""

import os
import html
import argparse
from datetime import datetime
from collections import Counter

from tqsdk import TqApi, TqAuth

import spread_monitor as sm
import spread_trader as st
import screener as sc
import product_detail as pdt
import hist_stats as hs

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _table(opps, with_flag=False, hist_links=None, sectors=None):
    if not opps:
        return "<p class='empty'>（无）</p>"
    hist_links = hist_links or {}
    sectors = sectors or {}
    rows = []
    for o in sorted(opps, key=lambda x: -x.score):
        cond = "; ".join(f"{k}={v}" for k, v in o.conditions.items())
        flag_td = f"<td>{html.escape(o.flag)}</td>" if with_flag else ""
        href = hist_links.get(o.structure)
        struct_td = (f"<td><a href='detail/{href}' target='_blank' title='历史同期统计'>"
                     f"{html.escape(o.structure)}</a></td>" if href
                     else f"<td>{html.escape(o.structure)}</td>")
        # 方向分类(仅spread类): L=正套(做多/买近卖远) S=反套(做空)，供期限结构极端过滤
        dircls = ""
        if o.kind == "spread":
            if o.direction.startswith("做多") or "买近卖远" in o.direction:
                dircls = "L"
            elif o.direction.startswith("做空"):
                dircls = "S"
        spctl = "" if o.slope_pctl is None else o.slope_pctl
        # 仓单佐证列(弱佐证仅背书): 顺风绿/逆风红, 分位来自 warrant_pctl
        wpctl = "" if getattr(o, "warrant_pctl", None) is None else round(o.warrant_pctl)
        wb = o.conditions.get("仓单佐证", "")
        wtag = "顺" if str(wb).startswith("顺风") else ("逆" if str(wb).startswith("逆风") else "")
        wsty = {"顺": "color:#1a7f37;font-weight:600", "逆": "color:#c0392b;font-weight:600"}.get(wtag, "")
        # 方向列简化为 做多/做空(完整方向文本保留在 CSV 与 data-dir 判定中)
        long_dir0 = o.direction.startswith("做多") or "买近卖远" in o.direction
        dir_txt = "做多" if long_dir0 else ("做空" if o.direction.startswith("做空") else o.direction)
        # 可转抛(用户口径): 仅做多方向填 是/否, 非做多留空
        long_dir = long_dir0
        roll_txt = ("是" if o.rollable else "否") if long_dir else ""
        roll_sty = {"是": "color:#1a7f37;font-weight:600", "否": "color:#c0392b"}.get(roll_txt, "")
        # 盘口(标的段): 买价/买量=bid侧(可按此卖出), 卖价/卖量=ask侧(可按此买入)
        bk = o.book or (None, None, None, None)
        fmt = lambda v: "" if v is None else (f"{v:g}" if isinstance(v, float) else v)
        # 标的当前值/插值目标价/差值/利润率(ABC类策略)
        cur_b = o.conditions.get("B")
        theo_b = o.conditions.get("理论B=(A+C)/2")
        diff_b = "" if (cur_b is None or theo_b is None) else round(theo_b - cur_b, 2)
        profit = o.conditions.get("比例%", "")
        rows.append(
            f"<tr data-prod='{o.product}' data-strat='{html.escape(o.strategy)}'"
            f" data-combo='{1 if (o.tradeable or '').strip() else 0}' data-roll='{1 if o.rollable else 0}'"
            f" data-dir='{dircls}' data-spctl='{spctl}' data-wpctl='{wpctl}'"
            f" data-sector='{html.escape(sectors.get(o.product, ''))}'>"
            f"<td>{html.escape(o.strategy)}</td>"
            f"<td><a href='detail/{o.product}.html' target='_blank'>{o.product}</a></td>"
            + (f"<td><a href='detail/{href}' target='_blank' title='历史同期统计'>{html.escape(o.tradeable)}</a></td>"
               if href and (o.tradeable or "").strip() else f"<td>{html.escape(o.tradeable or '')}</td>")
            +
            f"<td style='color:{'#c0392b' if dir_txt == '做空' else '#1a7f37'};font-weight:600'>{html.escape(dir_txt)}</td>"
            f"{struct_td}"
            f"<td style='text-align:right'>{o.score}</td>"
            f"<td style='text-align:right'>{o.conditions.get('最小腿OI','')}</td>"
            f"<td style='{roll_sty}'>{roll_txt}</td>"
            f"<td style='text-align:right'>{fmt(bk[1])}</td><td style='text-align:right'>{fmt(bk[0])}</td>"
            f"<td style='text-align:right'>{fmt(bk[3])}</td><td style='text-align:right'>{fmt(bk[2])}</td>"
            f"<td style='text-align:right'>{'' if cur_b is None else cur_b}</td>"
            f"<td style='text-align:right'>{'' if theo_b is None else theo_b}</td>"
            f"<td style='text-align:right'>{diff_b}</td>"
            f"<td style='text-align:right'>{profit}</td>"
            f"<td>{html.escape(o.curve or '')}</td>"
            f"<td style='text-align:right'>{'' if o.slope_pctl is None else o.slope_pctl}</td>"
            f"<td style='text-align:right;{wsty}' title='仓单历年同期分位;顺/逆=与方向的佐证(弱证据)'>"
            f"{wpctl}{wtag}</td>"
            f"{flag_td}"
            f"<td class='cond'>{html.escape(cond)}</td></tr>")
    fh = "<th>标注</th>" if with_flag else ""
    return ("<table><thead><tr><th>策略</th><th>品种</th><th>组合合约</th><th>方向</th><th>结构</th>"
            f"<th>强度</th><th>最小腿OI</th><th>可转抛</th>"
            f"<th>买量</th><th>买价</th><th>卖量</th><th>卖价</th>"
            f"<th>当前值</th><th>目标价</th><th>差值</th><th>利润率%</th>"
            f"<th>期限结构</th><th>斜率分位%</th><th>仓单分位%</th>{fh}<th>触发条件</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def archive_snapshot(U, opps):
    """快照归档(追加不覆盖): data/snapshots/{spreads,opportunities}/{YYYYMMDD}.parquet"""
    import pandas as pd
    day = U.ts[:10].replace("-", "")
    base = os.path.join(HERE, "data", "snapshots")
    rows = []
    for sp in U.spreads:
        rows.append(dict(ts=U.ts, product=sp.near.product, name=sp.name,
                         near=sm.code_of(sp.near.symbol), far=sm.code_of(sp.far.symbol),
                         bid=sp.bid, ask=sp.ask, mid=sp.mid, last=sp.last,
                         source=sp.source, combo=sp.combo_symbol or "",
                         months=sp.months, adjacent=sp.adjacent,
                         fullcarry=sp.fullcarry, decay=sp.decay,
                         days_to_expiry=sp.days_to_expiry,
                         near_oi=sp.near.oi, far_oi=sp.far.oi,
                         cancel_state=sp.cancel_state))
    for sub, df_new in (("spreads", pd.DataFrame(rows)), ("opportunities", sc.opps_to_df(opps))):
        d = os.path.join(base, sub); os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{day}.parquet")
        if os.path.exists(fp):
            try:
                df_new = pd.concat([pd.read_parquet(fp), df_new], ignore_index=True)
            except Exception:
                pass
        df_new.to_parquet(fp)
    log(f"快照归档: spreads {len(rows)} 行 + 机会 {len(opps)} 行 -> data/snapshots/*/{day}.parquet")


def write_report(opps, ts, path, hist_links=None, sectors=None):
    sectors = sectors or {}
    resid = [o for o in opps if o.flag == sc.RESID_FLAG]
    main = [o for o in opps if o.liquid and not o.flag]
    fish = [o for o in opps if not o.liquid and o.flag != sc.RESID_FLAG]
    excl = [o for o in opps if o.flag and o.liquid and o.flag != sc.RESID_FLAG]
    strategies = sorted({o.strategy for o in opps})
    strat_opts = "".join(f"<option value='{html.escape(s)}'>{html.escape(s)}</option>" for s in strategies)
    sec_opts = "".join(f"<option value='{html.escape(s)}'>{html.escape(s)}</option>"
                       for s in sorted({sectors.get(o.product, "") for o in opps} - {""}))
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>套利机会筛选</title><style>
body{{margin:14px;background:#f5f6fa;font-family:"Microsoft YaHei",sans-serif;font-size:13px}}
h2{{margin:4px 0}} h3{{margin:16px 0 6px}} .meta{{color:#888;font-size:12px}}
.bar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:8px 0;padding:8px;background:#fff;
 border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:13px}}
.bar label{{color:#666}}
select,input{{border:1px solid #cfd6e0;border-radius:4px;padding:3px 6px;font-size:13px}}
table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
th,td{{border:1px solid #e3e6ec;padding:4px 8px;white-space:nowrap;text-align:left}}
thead th{{background:#eef1f7}} tbody tr:hover{{background:#f0f6ff}}
td.cond{{white-space:normal;max-width:520px;color:#666;font-size:12px}}
a{{color:#2b6cb0;font-weight:bold;text-decoration:none}} a:hover{{text-decoration:underline}}
.empty{{color:#999}}
</style></head><body>
<h2>套利机会筛选（A类实时策略）</h2>
<div class="meta">快照 {html.escape(ts)} · 共 {len(opps)} 条 · 点品种代码打开详情图 · 方向口径 近月−远月</div>
<div class="bar">
  <label>策略 <select id="fStrat"><option value="">全部</option>{strat_opts}</select></label>
  <label>板块 <select id="fSector"><option value="">全部</option>{sec_opts}</select></label>
  <label>品种 <input id="fProd" placeholder="首字母即时过滤,如 S / SA" size="10"></label>
  <label><input type="checkbox" id="fCombo"> 仅有交易所套利合约</label>
  <label><input type="checkbox" id="fRoll"> 可转抛(正套且区间无注销月)</label>
  <label title="斜率同期分位极高=深度contango,统计正套危险；极低=深度back,反套危险">
    <input type="checkbox" id="fSlope"> 期限结构极端过滤(正套@分位&gt;<input id="fSlopeP" size="2" value="90" style="width:34px">危险,反套@&lt;100−该值)</label>
  <button id="btnReset">重置</button>
  <span class="meta" id="shown"></span>
</div>
<h3>✅ 主列表（流动性达标 · 可执行） {len(main)} 条</h3>
{_table(main, hist_links=hist_links, sectors=sectors)}
<h3>🎣 钓鱼桶（低流动性 · 供程序挂单钓鱼） {len(fish)} 条</h3>
{_table(fish, hist_links=hist_links, sectors=sectors)}
<h3>⚠️ 仅提示/排除（现金交割类 · 规则断层） {len(excl)} 条</h3>
{_table(excl, with_flag=True, hist_links=hist_links, sectors=sectors)}
<h3>📉 统计套利·未满足残差过滤（价差可被 期限斜率+到期时间 回归解释,残差不足） {len(resid)} 条</h3>
{_table(resid, hist_links=hist_links, sectors=sectors)}
<script>
const KEY="oppFilterState";
let st={{strat:"",sector:"",prod:"",combo:false,roll:false,slope:false,slopeP:90}};
try{{Object.assign(st,JSON.parse(localStorage.getItem(KEY)||"{{}}"));}}catch(e){{}}
// 2026-08-12 用户决定: 期限结构极端过滤默认关。一次性迁移覆盖旧存档的 slope:true
if(!st._slopeOff){{st.slope=false;st._slopeOff=true;}}
const $=id=>document.getElementById(id);
function apply(){{
  const p=st.prod.trim().toUpperCase();
  const P=parseFloat(st.slopeP)||90;
  let shown=0,total=0;
  document.querySelectorAll("tbody tr").forEach(tr=>{{
    total++;
    let ok=true;
    if(st.strat&&tr.dataset.strat!==st.strat)ok=false;
    if(ok&&st.sector&&tr.dataset.sector!==st.sector)ok=false;
    if(ok&&p&&!tr.dataset.prod.startsWith(p))ok=false;
    if(ok&&st.combo&&tr.dataset.combo!=="1")ok=false;
    if(ok&&st.roll&&tr.dataset.roll!=="1")ok=false;
    if(ok&&st.slope){{
      const d=tr.dataset.dir,sp=parseFloat(tr.dataset.spctl);
      if(d&&!isNaN(sp)){{
        if(d==="L"&&sp>P)ok=false;          // 正套遇深度contango(斜率分位极高)
        if(d==="S"&&sp<100-P)ok=false;      // 反套遇深度back(斜率分位极低)
      }}
    }}
    tr.style.display=ok?"":"none"; if(ok)shown++;
  }});
  $("shown").textContent="筛选后 "+shown+" / "+total+" 条";
}}
function save(){{localStorage.setItem(KEY,JSON.stringify(st));}}
$("fStrat").onchange=e=>{{st.strat=e.target.value;save();apply();}};
$("fSector").onchange=e=>{{st.sector=e.target.value;save();apply();}};
$("fProd").oninput=e=>{{st.prod=e.target.value;save();apply();}};
$("fCombo").onchange=e=>{{st.combo=e.target.checked;save();apply();}};
$("fRoll").onchange=e=>{{st.roll=e.target.checked;save();apply();}};
$("fSlope").onchange=e=>{{st.slope=e.target.checked;save();apply();}};
$("fSlopeP").oninput=e=>{{st.slopeP=e.target.value;save();apply();}};
$("btnReset").onclick=()=>{{st={{strat:"",sector:"",prod:"",combo:false,roll:false,slope:false,slopeP:90,_slopeOff:true}};save();
  $("fStrat").value="";$("fSector").value="";$("fProd").value="";$("fCombo").checked=false;$("fRoll").checked=false;
  $("fSlope").checked=false;$("fSlopeP").value=90;apply();}};
$("fStrat").value=st.strat;$("fSector").value=st.sector||"";$("fProd").value=st.prod;$("fCombo").checked=st.combo;$("fRoll").checked=!!st.roll;
$("fSlope").checked=st.slope===true;$("fSlopeP").value=st.slopeP||90;
apply();
</script>
<!--PINJS-->
<script>
(function(){{
const PKEY="pinnedOpps";
const SNAP=(document.querySelector(".meta")||{{textContent:""}}).textContent.match(/快照 ([0-9 :-]+)/);
const TS=SNAP?SNAP[1].trim():"";
const load=()=>{{try{{return JSON.parse(localStorage.getItem(PKEY)||"[]");}}catch(e){{return[];}}}};
// 打通交易台：选中列表同步给 spread_server(自定义行情)；服务没开则静默忽略
const TRADER="http://127.0.0.1:8010";
const sync=()=>{{fetch(TRADER+"/api/opps_sync",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{opps:load()}})}}).catch(()=>{{}});}};
const save=p=>{{localStorage.setItem(PKEY,JSON.stringify(p));sync();}};
const kof=o=>o.strategy+"|"+o.structure+"|"+o.direction;
function esc(s){{const d=document.createElement("div");d.textContent=s||"";return d.innerHTML;}}
function now(){{const d=new Date();const p=n=>(n<10?"0":"")+n;return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes());}}
const bar=document.querySelector(".bar");
const sec=document.createElement("div");
sec.innerHTML='<h3>📌 已选中的交易机会 (<span id="pinN">0</span>)　<button id="addCust">＋自定义新增</button>　<span class="meta">数据为选中时快照,列表刷新不影响;仅存本机浏览器</span></h3>'
 +'<div id="custForm" style="display:none;margin:6px 0;padding:8px 10px;background:#eef5ff;border:1px solid #b8cbe4;border-radius:4px">'
 +'品种 <input id="cf_prod" size="5" placeholder="M"> 结构 <input id="cf_struct" size="16" placeholder="m2609-m2701"> 方向 <input id="cf_dir" size="12" placeholder="做多价差"> 组合合约 <input id="cf_combo" size="18" placeholder="可空"> 备注 <input id="cf_note" size="36" placeholder="理由/目标/止损"> '
 +'<button id="cf_ok">加入</button> <button id="cf_cancel">取消</button></div>'
 +'<div id="pinBox"></div>';
bar.after(sec);
document.getElementById("addCust").onclick=()=>{{const f=document.getElementById("custForm");f.style.display=f.style.display==="none"?"block":"none";}};
document.getElementById("cf_cancel").onclick=()=>{{document.getElementById("custForm").style.display="none";}};
document.getElementById("cf_ok").onclick=()=>{{
  const v=id=>document.getElementById(id).value.trim();
  const prod=v("cf_prod").toUpperCase(),st=v("cf_struct");
  if(!prod||!st){{alert("品种和结构必填");return;}}
  const o={{t:now(),strategy:"自定义",product:prod,structure:st,direction:v("cf_dir")||"-",score:"-",combo:v("cf_combo"),cond:v("cf_note"),hist:""}};
  const p=load();
  if(p.some(x=>kof(x)===kof(o))){{alert("已存在相同 结构+方向 的自定义记录");return;}}
  p.push(o);save(p);render();mark();
  ["cf_prod","cf_struct","cf_dir","cf_combo","cf_note"].forEach(i=>document.getElementById(i).value="");
  document.getElementById("custForm").style.display="none";
}};
function render(){{
  const p=load();
  document.getElementById("pinN").textContent=p.length;
  const box=document.getElementById("pinBox");
  if(!p.length){{box.innerHTML='<p class="empty">（尚未选中 — 点行尾"选中"按钮或"＋自定义新增"加入）</p>';return;}}
  box.innerHTML='<table><thead><tr><th>选中时间</th><th>品种</th><th>策略</th><th>结构</th><th>方向</th><th>强度@选中</th><th>组合合约</th><th>期限结构@选中</th><th>斜率分位@选中</th><th>条件/备注@选中</th><th></th></tr></thead><tbody>'+
   p.map((o,i)=>'<tr style="background:'+(o.strategy==="自定义"?"#eef9ee":"#fffbe8")+'"><td>'+esc(o.t)+'</td><td><a href="detail/'+esc(o.product)+'.html" target="_blank">'+esc(o.product)+'</a></td><td>'+esc(o.strategy)+'</td><td>'+(o.hist?('<a href="'+esc(o.hist)+'" target="_blank">'+esc(o.structure)+'</a>'):esc(o.structure))+'</td><td>'+esc(o.direction)+'</td><td>'+esc(o.score||"-")+'</td><td>'+esc(o.combo)+'</td><td>'+esc(o.curve||"")+'</td><td>'+esc(o.spctl||"")+'</td><td class="cond" style="white-space:normal;max-width:420px">'+esc(o.cond)+'</td><td><button class="rmbtn" data-i="'+i+'">移除</button></td></tr>').join("")+'</tbody></table>';
  box.querySelectorAll(".rmbtn").forEach(b=>b.onclick=()=>{{const q=load();q.splice(+b.dataset.i,1);save(q);render();mark();}});
}}
function mark(){{
  const keys=new Set(load().map(kof));
  document.querySelectorAll("tbody tr[data-strat]").forEach(tr=>{{
    const btn=tr.querySelector(".pinbtn"); if(!btn)return;
    const c=tr.children;
    const kk=tr.dataset.strat+"|"+c[4].textContent.trim()+"|"+c[3].textContent.trim();
    if(keys.has(kk)){{btn.textContent="已选";btn.disabled=true;}}
    else{{btn.textContent="选中";btn.disabled=false;}}
  }});
}}
document.querySelectorAll("table").forEach(tb=>{{
  if(tb.closest("#pinBox"))return;
  const hr=tb.querySelector("thead tr");
  if(hr&&!hr.querySelector(".pinth")){{const th=document.createElement("th");th.className="pinth";th.textContent="选";hr.appendChild(th);}}
  tb.querySelectorAll("tbody tr[data-strat]").forEach(tr=>{{
    if(tr.querySelector(".pinbtn"))return;
    const td=document.createElement("td");
    const b=document.createElement("button");b.className="pinbtn";b.textContent="选中";
    td.appendChild(b);tr.appendChild(td);
    b.onclick=()=>{{
      const c=tr.children;const histA=c[4].querySelector("a");
      const o={{t:TS,strategy:tr.dataset.strat,product:tr.dataset.prod,
        structure:c[4].textContent.trim(),direction:c[3].textContent.trim(),
        score:c[5].textContent.trim(),combo:c[2].textContent.trim(),curve:c[16]?c[16].textContent.trim():"",spctl:c[17]?c[17].textContent.trim():"",
        cond:(tr.querySelector("td.cond")||{{textContent:""}}).textContent,
        hist:histA?histA.getAttribute("href"):""}};
      const p=load();
      if(p.some(x=>kof(x)===kof(o)))return;
      p.push(o);save(p);render();mark();
    }};
  }});
}});
render();mark();sync();
}})();
</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    ap = argparse.ArgumentParser(description="机会筛选一体化报告")
    ap.add_argument("--no-detail", action="store_true", help="跳过详情图生成")
    a = ap.parse_args()

    cfg = st.load_trade_config()
    meta = sm.load_product_meta(os.path.join(HERE, "all_product_config20260629.xlsx"))
    out = os.path.join(HERE, "output"); os.makedirs(out, exist_ok=True)

    api = hs._new_api(cfg["user"], cfg["password"])   # 带退避重试(auth服务偶发超时)
    try:
        log("构建 universe (含非相邻月份对, 全月份窗口) ...")
        U = sc.build_universe(api, meta, all_pairs=True, max_months=14)
        sc.set_hist_provider(lambda p_, n_, f_: hs.versions_series_cached(api, p_, n_, f_))
        sc.set_warrant_provider(lambda p_: pdt.fetch_warrants(p_, cfg["user"], cfg["password"]))
        sc.set_slope_provider(lambda p_: hs.slope_series_cached(api, p_))
        # C类统计缓存预热(独立连接分块)
        cand = set()
        for sp_ in U.spreads:
            if sp_.bid is None or sp_.ask is None:
                continue
            if any(v is None or v < sc.LIQ_MIN_OI for v in (sp_.near.oi, sp_.far.oi)):
                continue
            mp = (sp_.near.month % 100, sp_.far.month % 100)
            if sp_.adjacent or mp in sc._CYCLE_PAIRS:
                cand.add((sp_.near.product, mp[0], mp[1]))
        syms_ = hs.collect_pair_symbols(api, sorted(cand))
        hs.warm_cache(cfg["user"], cfg["password"], syms_)
        log("运行 A类实时 + C类统计 策略 ...")
        opps = sc.run_screen(U, only=sc.A_STRATEGIES + sc.C_STRATEGIES)
        sc.emit(opps)
        archive_snapshot(U, opps)
        log(f"命中 {len(opps)} 条: " + str(dict(Counter(o.strategy for o in opps))))

        # 命中品种出详情图(主列表+钓鱼桶)
        prods = []
        for o in opps:
            if (o.liquid or not o.flag.startswith("仅提示")) and o.product not in prods:
                prods.append(o.product)
        if not a.no_detail:
            log(f"生成 {len(prods)} 个品种详情图 ...")
            for p in prods:
                try:
                    d = pdt.fetch(api, meta, p, user=cfg["user"], password=cfg["password"])
                    pdt.render(d, os.path.join(out, "detail"))
                except BaseException as e:
                    log(f"  {p} 详情图失败: {e}")

        log("生成历史同期统计页(主列表结构) ...")
        jobs = hs.hist_jobs_from_opps(opps, main_only=True)
        hist_links = hs.ensure_pages(api, jobs)
        log(f"历史页 {len(set(hist_links.values()))} 张, 覆盖 {len(hist_links)} 条结构")
        path = os.path.join(out, "opportunities.html")
        write_report(opps, U.ts, path, hist_links=hist_links,
                     sectors={p: m.get("industry", "") for p, m in meta.items()})
        log(f"报告 -> {path}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
