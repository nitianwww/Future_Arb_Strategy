# -*- coding: utf-8 -*-
"""
全品种季节性复盘（纯缓存 data/daily_k，无需联网）。

思路（去趋势的季节性度量）：
  对每个品种、每个交割月 M：
    近腿=该品种(交割年 y, 月 M)合约，远腿=下一活跃交割月 M2 的同轮合约。
    相邻价差 = 近close − 远close，按近腿价格归一(%)。
    只取「距近腿交割 30–150 天」的可比成熟度窗口，跨年份汇总取均值。
  月强度(M) = 该月归一价差均值 − 品种各月均值。 >0 → M 相对偏强(近月升水更陡=旺季/逼仓迹象)。
输出 output/season_review.html（逐品种月强度）+ 控制台校验。
"""
import os, sys, glob, re, math
from datetime import datetime
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "daily_k")

MIN_ROWS = 25          # 合约至少交易日数
WIN = (30, 150)        # 成熟度窗口(距交割天数)
MIN_YEARS = 3          # 至少几个年份版本才出结论


def parse_name(fname):
    """CFFEX_IC2009.parquet -> ('IC', deliv_month, code)"""
    name = os.path.basename(fname)[:-8]
    if "_" not in name:
        return None
    _, code = name.split("_", 1)
    m = re.match(r"^([A-Za-z]+)(\d+)$", code)
    if not m:
        return None
    prod = m.group(1).upper()
    digits = m.group(2)
    if len(digits) == 4:
        mm = int(digits[2:])
    elif len(digits) == 3:
        mm = int(digits[1:])
    else:
        return None
    if not (1 <= mm <= 12):
        return None
    return prod, mm, code


def load_all():
    """{prod: [ {mm, dy(交割年), df(date,close)} ]}"""
    prods = {}
    for fp in glob.glob(os.path.join(CACHE, "*.parquet")):
        pr = parse_name(fp)
        if not pr:
            continue
        prod, mm, code = pr
        try:
            df = pd.read_parquet(fp, columns=["date", "close"])
        except Exception:
            continue
        df = df[df["close"].notna()]
        if len(df) < MIN_ROWS:
            continue
        df = df.copy()
        df["dt"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[df["dt"].notna()].sort_values("dt")
        if len(df) < MIN_ROWS:
            continue
        last = df["dt"].iloc[-1]
        dy = last.year + (1 if (mm == 1 and last.month == 12) else 0)   # 交割年≈末bar年
        prods.setdefault(prod, []).append({"mm": mm, "dy": dy, "df": df[["dt", "close"]]})
    return prods


def active_months(entries):
    from collections import Counter
    c = Counter(e["mm"] for e in entries)
    # 出现≥2次的月份视为活跃交割月
    return sorted([m for m, n in c.items() if n >= 2])


def deliv_date(dy, mm):
    return datetime(dy, mm, 15)


def month_strength(entries, months):
    """返回 {M: (归一价差均值%, 年份数, 原始价差均值, 近腿价格)}"""
    by = {}
    for e in entries:
        by.setdefault((e["dy"], e["mm"]), e["df"])
    res = {}
    for i, M in enumerate(months):
        M2 = months[(i + 1) % len(months)]
        wrap = 1 if M2 <= M else 0
        pooled = []          # 归一价差%
        raw = []
        px = []
        yrs = set()
        for (dy, mm), dfn in by.items():
            if mm != M:
                continue
            dff = by.get((dy + wrap, M2))
            if dff is None:
                continue
            j = dfn.merge(dff, on="dt", suffixes=("_n", "_f"))
            if j.empty:
                continue
            dd = deliv_date(dy, M)
            days = (dd - j["dt"]).dt.days
            mask = (days >= WIN[0]) & (days <= WIN[1])
            sub = j[mask]
            if sub.empty:
                continue
            sp = sub["close_n"] - sub["close_f"]
            norm = (sp / sub["close_n"] * 100)
            pooled.extend(norm.tolist())
            raw.extend(sp.tolist())
            px.extend(sub["close_n"].tolist())
            yrs.add(dy)
        if len(yrs) >= MIN_YEARS and pooled:
            res[M] = (sum(pooled) / len(pooled), len(yrs),
                      sum(raw) / len(raw), sum(px) / len(px))
    return res


def calendar_profile(entries, months):
    """视角B：按『观察日历月』汇总前端相邻价差(归一%)，看哪个日历时段现货偏紧。
    只用前端合约(距交割 0–120 天)的 M−下一活跃月 价差。返回 {日历月: (均值%, 样本年数)}。"""
    by = {}
    for e in entries:
        by.setdefault((e["dy"], e["mm"]), e["df"])
    buckets = {c: [] for c in range(1, 13)}
    yrs = {c: set() for c in range(1, 13)}
    for i, M in enumerate(months):
        M2 = months[(i + 1) % len(months)]
        wrap = 1 if M2 <= M else 0
        for (dy, mm), dfn in by.items():
            if mm != M:
                continue
            dff = by.get((dy + wrap, M2))
            if dff is None:
                continue
            j = dfn.merge(dff, on="dt", suffixes=("_n", "_f"))
            if j.empty:
                continue
            dd = deliv_date(dy, M)
            days = (dd - j["dt"]).dt.days
            sub = j[(days >= 0) & (days <= 120)]        # 前端窗口
            if sub.empty:
                continue
            norm = (sub["close_n"] - sub["close_f"]) / sub["close_n"] * 100
            for dt_, nv in zip(sub["dt"], norm):
                if not math.isfinite(nv):
                    continue
                buckets[dt_.month].append(nv)
                yrs[dt_.month].add(dt_.year)
    return {c: (sum(v) / len(v), len(yrs[c])) for c, v in buckets.items()
            if len(yrs[c]) >= MIN_YEARS and v}


def analyze(prods):
    out = {}
    for prod, entries in prods.items():
        months = active_months(entries)
        if len(months) < 2:
            continue
        ms = month_strength(entries, months)
        if len(ms) < 2:
            continue
        base = sum(v[0] for v in ms.values()) / len(ms)
        rows = []
        for M, (norm, ny, rawsp, px) in sorted(ms.items()):
            rows.append(dict(month=M, norm=round(norm, 3),
                             strength=round(norm - base, 3),
                             years=ny, raw=round(rawsp, 1), px=round(px, 1)))
        cal = calendar_profile(entries, months)
        cbase = (sum(v[0] for v in cal.values()) / len(cal)) if cal else 0.0
        crows = [dict(cmonth=c, val=round(v[0], 3), rel=round(v[0] - cbase, 3), years=v[1])
                 for c, v in sorted(cal.items())]
        out[prod] = dict(base=round(base, 3), rows=rows, cbase=round(cbase, 3), crows=crows)
    return out


MANUAL = {  # 人工规律(已审核,详见 skill seasonality.md)，用于报告对照
    "LH": "01旺季(冬季腌腊+春节)",
    "MA": "12/01/02合约冬季驼峰,高于11和03;11最弱",
    "JD": "旺季=中秋前一月(逐年查,2026=08);06-07梅雨淡季",
    "AP": "05/10旺季(05易逼仓,10新果)",
    "RU": "1-4月停割期紧;10-11月旺产松",
    "RM": "09合约强(夏季水产饲料)",
    "UR": "08/09合约强(夏季追肥)",
    "CJ": "09弱(新枣上市);12-1月强(春节)",
}


def _fmt(items, key, n=2, sign=True):
    f = "+.2f" if sign else ".2f"
    return " ".join(f"{x['month'] if 'month' in x else x['cmonth']}月:{format(x[key], f)}" for x in items[:n])


def report(res, path):
    import html as _h
    rows_html = []
    data = []
    for prod, r in res.items():
        A = sorted(r["rows"], key=lambda x: -x["strength"])
        Astrong, Aweak = A[:2], A[-2:][::-1]
        amp = A[0]["strength"] - A[-1]["strength"] if len(A) > 1 else 0
        yrs = min(x["years"] for x in r["rows"])
        C = sorted(r["crows"], key=lambda x: -x["rel"]) if r.get("crows") else []
        Ctight, Cloose = (C[:2], C[-2:][::-1]) if C else ([], [])
        man = MANUAL.get(prod, "")
        # 一致性：人工旺季月是否落在 A 偏强前3
        chk = ""
        if man:
            top3 = {x["month"] for x in A[:3]}
            mm = re.findall(r"(\d{2})", man)
            hit = [int(x) for x in mm if int(x) in top3]
            chk = "✓部分" if hit else "✗待议"
        data.append((prod, amp, yrs, Astrong, Aweak, Ctight, Cloose, man, chk))
    data.sort(key=lambda t: -t[1])
    for prod, amp, yrs, As, Aw, Ct, Cl, man, chk in data:
        cls = "man" if man else ""
        rows_html.append(
            f"<tr class='{cls}'><td>{prod}</td>"
            f"<td class='pos'>{_h.escape(_fmt(As,'strength'))}</td>"
            f"<td class='neg'>{_h.escape(_fmt(Aw,'strength'))}</td>"
            f"<td style='text-align:right'>{amp:.2f}</td>"
            f"<td class='pos'>{_h.escape(_fmt(Ct,'rel'))}</td>"
            f"<td class='neg'>{_h.escape(_fmt(Cl,'rel'))}</td>"
            f"<td style='text-align:right'>{yrs}</td>"
            f"<td>{_h.escape(man)}</td><td>{_h.escape(chk)}</td></tr>")
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>全品种季节性复盘</title>
<style>body{{margin:16px;font-family:"Microsoft YaHei",sans-serif;font-size:13px;background:#f5f6fa}}
h2{{margin:4px 0}} .meta{{color:#777;font-size:12px;margin-bottom:8px}}
table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th,td{{border:1px solid #e3e6ec;padding:4px 8px;white-space:nowrap;text-align:left}}
thead th{{background:#eef1f7;position:sticky;top:0;cursor:pointer}}
tr.man{{background:#fffbe8}} tr:hover{{background:#f0f6ff}}
.pos{{color:#c0392b}} .neg{{color:#2e7d32}}
.note{{background:#fff;padding:10px 14px;border-radius:6px;margin:8px 0;line-height:1.7;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
</style></head><body>
<h2>全品种跨期价差季节性复盘</h2>
<div class="meta">缓存日K · {datetime.now():%Y-%m-%d %H:%M} · {len(data)} 品种 · 单位=归一价差%(价差/近月价) · 黄底=已有人工规律</div>
<div class="note">
<b>两个视角</b>：<br>
• <b>视角A 交割月强度</b>：某交割月合约相对下一活跃月的升水(固定成熟度30-150天,跨年均值)。&gt;0=该交割月偏强 → <b>抓"逼仓/交割月型"</b>(如 LH01、AP05)。这种月份的高价差属正常，勿当反套理由。<br>
• <b>视角B 日历季节</b>：前端相邻价差按<b>观察日历月</b>汇总。偏紧月=该日历时段现货紧 → <b>抓"需求时段型"</b>(如生猪秋冬)。<br>
<b>正/负号</b>：红=偏强/偏紧(近月升水更陡)，绿=偏弱/偏松。表按 A 信号幅度降序。
</div>
<table id="t"><thead><tr>
<th>品种</th><th>A·交割月偏强</th><th>A·交割月偏弱</th><th>A幅度</th>
<th>B·日历偏紧</th><th>B·日历偏松</th><th>年数</th><th>人工规律</th><th>核对</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<script>
document.querySelectorAll('#t th').forEach((th,i)=>th.onclick=()=>{{
 const tb=th.closest('table').tBodies[0];const rows=[...tb.rows];
 const num=[3,6].includes(i);
 rows.sort((a,b)=>{{const x=a.cells[i].innerText,y=b.cells[i].innerText;
  return num?parseFloat(y||0)-parseFloat(x||0):x.localeCompare(y,'zh');}});
 rows.forEach(r=>tb.appendChild(r));}});
</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    # CSV
    import csv
    with open(path.replace(".html", ".csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["品种", "A信号幅度", "年数", "A偏强", "A偏弱", "B偏紧", "B偏松", "人工规律", "核对"])
        for prod, amp, yrs, As, Aw, Ct, Cl, man, chk in data:
            w.writerow([prod, round(amp, 3), yrs, _fmt(As, 'strength'), _fmt(Aw, 'strength'),
                        _fmt(Ct, 'rel'), _fmt(Cl, 'rel'), man, chk])
    return len(data)


def main():
    prods = load_all()
    print(f"品种 {len(prods)} 个，合约 {sum(len(v) for v in prods.values())}")
    res = analyze(prods)
    print(f"可出季节性结论品种: {len(res)}\n")
    # 校验：手动品种
    for p in ["AP", "LH", "JD", "MA"]:
        if p not in res:
            print(f"== {p}: 数据不足 =="); continue
        r = res[p]
        strong = sorted(r["rows"], key=lambda x: -x["strength"])[:3]
        weak = sorted(r["rows"], key=lambda x: x["strength"])[:2]
        print(f"== {p} ({MANUAL.get(p,'')}) ==")
        print("  [A交割月强度] 偏强:", [(x["month"], f"{x['strength']:+.2f}%") for x in strong],
              "偏弱:", [(x["month"], f"{x['strength']:+.2f}%") for x in weak])
        if r.get("crows"):
            cs = sorted(r["crows"], key=lambda x: -x["rel"])[:3]
            cw = sorted(r["crows"], key=lambda x: x["rel"])[:3]
            print("  [B日历季节] 偏紧月:", [(x["cmonth"], f"{x['rel']:+.2f}%") for x in cs],
                  "偏松月:", [(x["cmonth"], f"{x['rel']:+.2f}%") for x in cw])
    out = os.path.join(HERE, "output", "season_review.html")
    n = report(res, out)
    print(f"\n报告 -> {out} ({n} 品种) + .csv")
    return res


if __name__ == "__main__":
    main()
