# -*- coding: utf-8 -*-
"""转抛比例快照报告：筛可转抛(roll_over=Y)的月差，标近月到期天数 + 转抛比例，输出 HTML 表。
当前时刻静态数据(一次快照)。转抛比例 = 最新价差 / −fullcarry。"""
import os
import html
from datetime import datetime

from tqsdk import TqApi, TqAuth
import spread_monitor as sm
import spread_trader as st
import screener as sc

HERE = os.path.dirname(os.path.abspath(__file__))


# 判定已下沉到 spread_monitor（供 screener 等共用），此处保留别名兼容旧调用
can_roll = sm.can_roll
_next_month = sm._next_month


INDEX_PRODUCTS = sm.INDEX_PRODUCTS
in_delivery_month = sm.in_delivery_month


def build_rows(U, meta):
    rows = []
    for sp in U.spreads:
        if in_delivery_month(sp.near.month, sp.near.product):    # 近月已进交割月 -> 排除
            continue
        cancel = meta.get(sp.near.product, {}).get("cancel", set())
        if not can_roll(sp.near.month, sp.far.month, cancel):   # 区间内无注销月才可转抛
            continue
        if sp.last is None or not sp.fullcarry:       # 需要价差与 fullcarry
            continue
        if sp.bid is None or sp.ask is None:          # 无双边盘口 -> 数据效力不足，剔除
            continue
        tick = sp.near.tick or 1
        width = round((sp.ask - sp.bid) / tick, 1)    # 盘口宽度(跳)；组合来源效力最高
        ratio = round(sp.last / (-sp.fullcarry), 4)   # fullcarry 已含时间贴水
        fee = meta.get(sp.near.product, {}).get("fee") or 0.0
        stor = fee * 30 * (sp.months or 0)            # 仓储成本(与利率无关)
        cap = max(sp.fullcarry - stor - sp.decay, 0.0)   # 资金成本(在当前利率下)
        rows.append({
            "交易所": sp.near.exchange, "品种": sp.near.product, "月差": sp.name,
            "组合合约": sm.code_of(sp.combo_symbol) if sp.combo_symbol else "",
            "近月到期天数": sp.days_to_expiry, "月差跨度(月)": sp.months,
            "最新价差": sp.last, "价差买/卖": f"{sp.bid}/{sp.ask}", "盘口宽(跳)": width,
            "fullcarry": round(-sp.fullcarry, 2), "时间贴水": (round(-sp.decay, 2) if sp.decay else ""),
            "转抛比例": ratio,   # 显示为负(持有成本)，比例算法不变
            "可转抛": "是",   # 本表已按 can_roll 过滤，全部可转抛
            # 隐藏字段：供 HTML 端按口径/利率实时重算
            "_last": sp.last, "_ask": sp.ask, "_cap": round(cap, 4), "_stor": round(stor, 4),
            "_dec": round(sp.decay, 4),
        })
    rows.sort(key=lambda r: r["转抛比例"], reverse=True)
    return rows


COLS = ["交易所", "品种", "月差", "组合合约", "近月到期天数", "月差跨度(月)",
        "最新价差", "价差买/卖", "盘口宽(跳)", "fullcarry", "时间贴水", "转抛比例", "可转抛"]
NUM = {"近月到期天数", "月差跨度(月)", "最新价差", "盘口宽(跳)", "fullcarry", "时间贴水", "转抛比例"}
HIDDEN_COLS = ["_last", "_ask", "_cap", "_stor", "_dec"]


def _n(v):
    return "" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def write_csv(rows, path, ts, rate):
    """原始数据层：HTML 之外落一份 CSV(含隐藏字段)，重算脚本优先读它。"""
    import csv
    cols = COLS + HIDDEN_COLS
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"#ts={ts}", f"rate={rate}"])
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def write_html(rows, path, ts, rate):
    head = "".join(f"<th data-c='{i}'>{html.escape(c)}</th>" for i, c in enumerate(COLS))
    body = []
    for r in rows:
        data = (f" data-last=\"{_n(r.get('_last'))}\" data-ask=\"{_n(r.get('_ask'))}\""
                f" data-cap=\"{_n(r.get('_cap'))}\" data-stor=\"{_n(r.get('_stor'))}\""
                f" data-dec=\"{_n(r.get('_dec'))}\"")
        tds = []
        for c in COLS:
            v = r.get(c)
            v = "" if v is None else v
            al = "right" if c in NUM else "left"
            cls = ""
            if c == "转抛比例" and isinstance(v, (int, float)):
                cls = " class='pos'" if v >= 1 else (" class='neg'" if v < 0 else "")
            if c == "时间贴水" and isinstance(v, (int, float)) and v != 0:
                cls = " class='neg'"          # 有特殊贴水 -> 红色警示
            tds.append(f"<td style='text-align:{al}'{cls}>{html.escape(str(v))}</td>")
        body.append(f"<tr{data}>{''.join(tds)}</tr>")
    numlist = [i for i, c in enumerate(COLS) if c in NUM]
    fc_i, ratio_i = COLS.index("fullcarry"), COLS.index("转抛比例")
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>转抛比例快照</title><style>
body{{font-family:"Microsoft YaHei",Consolas,monospace;margin:12px;background:#0f1115;color:#e6e6e6}}
h2{{margin:0 0 4px}} .meta{{color:#9aa4b2;font-size:13px;margin-bottom:8px}}
.bar{{display:flex;gap:10px;align-items:center;margin-bottom:8px;font-size:13px}}
.bar label{{color:#9aa4b2}}
select,input{{background:#1c2230;color:#e6e6e6;border:1px solid #2a2f3a;border-radius:4px;padding:3px 6px;font-size:13px}}
input[type=number]{{width:64px}}
table{{border-collapse:collapse;width:100%;font-size:12px}}
th,td{{border:1px solid #2a2f3a;padding:3px 7px;white-space:nowrap}}
thead th{{position:sticky;top:0;background:#1c2230;color:#cfe1ff;cursor:pointer}}
tbody tr:nth-child(even){{background:#161a22}} tbody tr:hover{{background:#23314a}}
td.pos{{color:#7CFC00;font-weight:bold}} td.neg{{color:#ff6b6b}}
.legend{{margin-top:8px;color:#9aa4b2;font-size:12px}}
</style></head><body>
<h2>转抛比例快照（可转抛月差）</h2>
<div class="bar">
  <label>价差口径 <select id="mode"><option value="last">最新价</option><option value="ask">ask价(买价差)</option></select></label>
  <label>资金利率% <input type="number" id="rate" step="0.1" value="{rate}"></label>
  <label>到期天数≥ <input type="number" id="minDays" step="1" value="0"></label>
  <label>盘口宽≤ <input type="number" id="maxWidth" step="1" placeholder="跳" style="width:52px"></label>
  <label><input type="checkbox" id="comboOnly"> 仅组合盘口</label>
  <span class="meta">快照时间 {html.escape(ts)} · 共 {len(rows)} 条 · <span id="shown"></span> · 点列头排序 · 改口径/利率实时重算</span>
</div>
<table id="t"><thead><tr>{head}</tr></thead><tbody>
{chr(10).join(body)}
</tbody></table>
<div class="legend">转抛比例 = 价差(按口径) / fullcarry；fullcarry = −(1.1×近月价×利率%/100×月数/12 + 仓储×30×月数 + 时间贴水)，显示为负。
时间贴水=交易所特殊日历贴水(如郑棉旧棉8/1起4元/吨·日)，红色警示，配置于 all_product_config 的 time_decay 列。
最新价差已限制在价差买/卖之间。绿色=转抛比例≥1；红色=负。方向 近月−远月。基准利率 {rate}%（重算按比例缩放资金成本项）。</div>
<script>
const NUM={numlist}, FC_I={fc_i}, RATIO_I={ratio_i}, RATE0={rate};
const rows=()=>[...document.querySelectorAll("#t tbody tr")];
function recalc(){{
  const rate=parseFloat(document.getElementById("rate").value)||RATE0;
  const mode=document.getElementById("mode").value;
  rows().forEach(tr=>{{
    const cap=parseFloat(tr.dataset.cap), stor=parseFloat(tr.dataset.stor), dec=parseFloat(tr.dataset.dec)||0;
    const val=parseFloat(mode==="ask"?tr.dataset.ask:tr.dataset.last);
    const fc=(isNaN(cap)?0:cap*(rate/RATE0))+(isNaN(stor)?0:stor)+dec;
    const fcCell=tr.children[FC_I], rCell=tr.children[RATIO_I];
    fcCell.textContent=fc? (-fc).toFixed(2):"";
    if(isNaN(val)||!fc){{rCell.textContent="";rCell.className="";return;}}
    const ratio=-val/fc;
    rCell.textContent=ratio.toFixed(4);
    rCell.className= ratio>=1?"pos":(ratio<0?"neg":"");
  }});
}}
const DAYS_I={COLS.index("近月到期天数")}, COMBO_I={COLS.index("组合合约")}, WIDTH_I={COLS.index("盘口宽(跳)")};
function applyFilter(){{
  const n=parseFloat(document.getElementById("minDays").value)||0;
  const mw=parseFloat(document.getElementById("maxWidth").value);
  const co=document.getElementById("comboOnly").checked;
  let shown=0;
  rows().forEach(tr=>{{
    const d=parseFloat(tr.children[DAYS_I].textContent);
    let ok=!isNaN(d)&&d>=n;
    if(ok&&!isNaN(mw)){{const w=parseFloat(tr.children[WIDTH_I].textContent); if(isNaN(w)||w>mw) ok=false;}}
    if(ok&&co&&!tr.children[COMBO_I].textContent.trim()) ok=false;
    tr.style.display=ok?"":"none"; if(ok)shown++;
  }});
  document.getElementById("shown").textContent="筛选后 "+shown+" 条";
}}
document.getElementById("mode").onchange=recalc;
document.getElementById("rate").oninput=recalc;
document.getElementById("minDays").oninput=applyFilter;
document.getElementById("maxWidth").oninput=applyFilter;
document.getElementById("comboOnly").onchange=applyFilter;
document.querySelectorAll("thead th").forEach(th=>th.addEventListener("click",()=>{{
 const i=+th.dataset.c,tb=document.querySelector("#t tbody"),num=NUM.includes(i);
 const rs=rows(); const asc=th._asc=!th._asc;
 rs.sort((a,b)=>{{let x=a.children[i].textContent.trim(),y=b.children[i].textContent.trim();
  if(num){{x=parseFloat(x);y=parseFloat(y);x=isNaN(x)?-1e18:x;y=isNaN(y)?-1e18:y;return (x-y)*(asc?1:-1);}}
  return x.localeCompare(y,'zh')*(asc?1:-1);}}).forEach(r=>tb.appendChild(r));
}}));
recalc();applyFilter();
</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    cfg = st.load_trade_config()
    rate = 6.0
    try:
        import configparser
        cp = configparser.ConfigParser(); cp.read(os.path.join(HERE, "config.ini"), encoding="utf-8")
        rate = cp.getfloat("settings", "funding_rate", fallback=6.0)
    except Exception:
        pass
    api = TqApi(auth=TqAuth(cfg["user"], cfg["password"]))
    try:
        meta = sm.load_product_meta(os.path.join(HERE, "all_product_config20260629.xlsx"))
        print("构建 universe（当前快照）...")
        U = sc.build_universe(api, meta, funding_rate=rate)   # 全品种
        rows = build_rows(U, meta)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = os.path.join(HERE, "output"); os.makedirs(out, exist_ok=True)
        path = os.path.join(out, "roll_ratio.html")
        write_html(rows, path, ts, rate)
        write_csv(rows, os.path.join(out, "roll_ratio_data.csv"), ts, rate)
        print(f"可转抛月差 {len(rows)} 条 -> {path} (+roll_ratio_data.csv)")
        for r in rows[:12]:
            print(f"  {r['品种']:>4} {r['月差']:<14} 到期{r['近月到期天数']}天 价差{r['最新价差']} "
                  f"fullcarry{r['fullcarry']} 转抛比例{r['转抛比例']}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
