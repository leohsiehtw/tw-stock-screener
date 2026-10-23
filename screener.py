"""台股盤後選股：先過濾，再確認，最後挑進場點。

用法：
  python3 screener.py scan --data DIR [--list 自選股.csv] [--capital 1000000] [--risk 0.02]
                           [--out 結果.md] [--json 結果.json] [--txt 推播.txt]
  python3 screener.py pool --data DIR [--out 自選股候選.csv]
  python3 screener.py add  --data DIR --bars 今日.csv

add：把每天收盤的一根 K 棒併進 DIR（今日.csv 欄位 code,date,open,high,low,close,volume），
     免得每天重抓一整年。除權息後還原價會變，該檔要重抓整段歷史覆蓋。

DIR 內每檔股票一個檔案，檔名 <代號>.json 或 <代號>.csv：
  .json：TW Stock 連接器 get_adjusted_price_history 的回傳（含 bars），可把多次查詢的 bars 合併
  .csv ：欄位 date,open,high,low,close,volume（FinMind TaiwanStockPrice 也可，欄名 max/min/Trading_Volume 會自動對應）
         可選欄位 raw_close（未還原收盤，顯示用）、name（股票名稱）；fetch_data.py 產生的 csv 即含這兩欄
指標用還原權息價計算；建議至少 12 個月日 K（週 MACD 需要約 35 週暖機）。

關卡：
  第 0 關 週線：週 MACD 在零軸上（DIF > 0）或柱狀體剛翻紅；收盤在週 MA13、MA26 之上；週線空頭排列直接剔除
  第 1 關 趨勢：收盤 > MA60、MA20 > MA60、MA20 不下彎且 MA60 上彎（差距沒擴大會加註「整理中」）
  第 2 關 動能：日 OSC 今天由負翻正；或 3 日內 DIF 上穿 DEA 且今日柱 > 昨日柱
  第 3 關 量能：今日量 > 前 5 日均量 × 1.3，且收紅（無量或收黑不算）
  第 4 關 型態：
    A 突破型（需過 0～3 關）：箱型整理或布林縮口後，帶量長紅 K 收在布林上軌之上（今天突破，或昨天突破今天回測守住）
    B 回測型（需過 0～1 關，不看 2、3 關）：沿月線爬升、量縮回測 MA20 附近止跌
停損：突破型取「突破長紅 K 低點」與「MA20」中較高者；回測型取 MA20 與當日低點中較低者。
"""
import argparse, csv, json, math, os, sys

# 參數集中在這裡，要調整門檻改這裡就好
P = dict(
    wk_ma=(13, 26),          # 週線中長均線
    vol_mult=1.3,            # 第 3 關量能倍數
    vol_days=5,
    slope_days=5,            # 均線上彎比較的天數
    cross_within=3,          # 第 2 關黃金交叉允許幾天內
    box_days=20,             # 箱型整理天數
    box_range=0.12,          # 箱型高低差 / 均價 上限
    squeeze_lookback=120,    # 布林縮口比較期間
    squeeze_pct=0.25,        # 帶寬落在期間內最低 25% 視為縮口
    long_red=0.03,           # 長紅 K：實體 ≥ 3%
    ride_days=20,            # 沿月線爬升：近 20 日
    ride_ratio=0.8,          #   至少 80% 收在 MA20 之上
    pull_near=0.02,          # 回測：最低價在 MA20 ±2% 內
    pull_vol=0.8,            # 回測量縮：量 < 5 日均量 × 0.8
    expand=1.2,              # 自選股池：布林帶寬比 5 日前擴大 20%
)


# ---------- 讀資料 ----------
def load_bars(path):
    if path.endswith('.json'):
        d = json.load(open(path, encoding='utf-8'))
        raw = d['bars'] if isinstance(d, dict) else d
        name = d.get('name', '') if isinstance(d, dict) else ''
        rows = [dict(date=b.get('day') or b.get('date'), open=b['open'], high=b['high'], low=b['low'],
                     close=b['close'], raw_close=b.get('raw_close', b['close']), volume=b['volume']) for b in raw]
    else:
        name = ''
        rows = []
        for r in csv.DictReader(open(path, encoding='utf-8-sig')):
            g = lambda *ks: next(float(r[k]) for k in ks if r.get(k) not in (None, ''))
            c = g('close')
            name = r.get('name') or name
            rows.append(dict(date=r['date'], open=g('open'), high=g('high', 'max'), low=g('low', 'min'),
                             close=c, raw_close=g('raw_close', 'close'), volume=g('volume', 'Trading_Volume')))
    seen = {}
    for r in rows:
        if r['volume'] and r['close']:
            seen[r['date']] = r          # 重複日期以後者為準
    return name, [seen[k] for k in sorted(seen)]


def load_list(path):
    if not path:
        return None
    out = {}
    for r in csv.DictReader(open(path, encoding='utf-8-sig')):
        out[r['代號'].strip()] = (r.get('名稱') or '').strip()
    return out


# ---------- 指標 ----------
def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out, k, e = [], 2 / (n + 1), None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def macd(xs):
    dif = [a - b for a, b in zip(ema(xs, 12), ema(xs, 26))]
    dea = ema(dif, 9)
    return dif, dea, [a - b for a, b in zip(dif, dea)]


def boll(xs, n=20, k=2):
    mid = sma(xs, n)
    up, lo, bw = [None] * len(xs), [None] * len(xs), [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in w) / n)
        up[i], lo[i] = mid[i] + k * sd, mid[i] - k * sd
        bw[i] = (up[i] - lo[i]) / mid[i]
    return mid, up, lo, bw


def weekly(bars):
    """依 ISO 週合併成週 K（只用收盤，週 MACD 與週均線用得到）。"""
    import datetime
    wk = {}
    for b in bars:
        y, w, _ = datetime.date.fromisoformat(b['date']).isocalendar()
        wk[(y, w)] = b['close']
    return [wk[k] for k in sorted(wk)]


# ---------- 判斷 ----------
def analyze(code, name, bars):
    n = len(bars)
    if n < 80:
        return dict(code=code, name=name, error=f'資料只有 {n} 天，至少要 80 天')
    C = [b['close'] for b in bars]
    O = [b['open'] for b in bars]
    H = [b['high'] for b in bars]
    L = [b['low'] for b in bars]
    V = [b['volume'] for b in bars]
    t, y = n - 1, n - 2
    ma5, ma20, ma60 = sma(C, 5), sma(C, 20), sma(C, 60)
    dif, dea, osc = macd(C)
    mid, up, lo, bw = boll(C)
    vavg = sma(V, P['vol_days'])
    adj = bars[t]['raw_close'] / C[t]                 # 還原價 → 實際價
    px = lambda v: round(v * adj, 2)
    r = dict(code=code, name=name, date=bars[t]['date'], close=px(C[t]),
             chg=round((C[t] / C[y] - 1) * 100, 2), stages={}, notes=[])
    S = r['stages']

    # 第 0 關：週線
    W = weekly(bars)
    wd, wde, wo = macd(W)
    wma = {m: sma(W, m)[-1] for m in (5,) + P['wk_ma']}
    m1, m2 = P['wk_ma']
    bear = None not in wma.values() and wma[5] < wma[m1] < wma[m2]
    wk_macd = wd[-1] > 0 or (wo[-1] > 0 and min(wo[-3:-1]) <= 0)
    above = all(v is not None and C[t] > v for k, v in wma.items() if k != 5)
    S[0] = bool(wk_macd and above and not bear)
    if len(W) < 35:
        r['notes'].append(f'週 K 只有 {len(W)} 週，週 MACD 參考性較低')
    r['week'] = dict(dif=round(wd[-1], 2), osc=round(wo[-1], 2), bear=bear,
                     ma=[round(wma[m1] * adj, 2) if wma[m1] else None, round(wma[m2] * adj, 2) if wma[m2] else None])

    # 第 1 關：趨勢
    k = P['slope_days']
    S[1] = bool(C[t] > ma60[t] and ma20[t] > ma60[t] and ma20[t] >= ma20[t - k] and ma60[t] > ma60[t - k])
    if S[1] and (ma20[t] - ma60[t]) <= (ma20[t - k] - ma60[t - k]):
        r['notes'].append('月線與季線差距沒有擴大（整理中）')

    # 第 2 關：動能
    flip = osc[t] > 0 >= osc[y]
    cross = any(osc[i] > 0 >= osc[i - 1] for i in range(t - P['cross_within'] + 1, t + 1))
    S[2] = bool(flip or (cross and osc[t] > osc[y]))

    # 第 3 關：量能（前 5 日均量，不含今天）
    v5 = vavg[y]
    vr = V[t] / v5
    S[3] = bool(vr > P['vol_mult'] and C[t] > O[t] and C[t] > C[y])
    r['vol_ratio'] = round(vr, 2)

    # 第 4A 關：突破型
    def long_red(i):
        return C[i] > O[i] and (C[i] - O[i]) / O[i] >= P['long_red'] and up[i] is not None and C[i] > up[i] \
            and V[i] > vavg[i - 1] * P['vol_mult']

    def setup_before(i):
        b = P['box_days']
        hi, lw = max(H[i - b:i]), min(L[i - b:i])
        box = (hi - lw) / (sum(C[i - b:i]) / b) <= P['box_range'] and C[i] > hi
        past = [x for x in bw[i - P['squeeze_lookback']:i] if x is not None]
        sq = len(past) >= 40 and bw[i - 1] <= sorted(past)[int(len(past) * P['squeeze_pct'])]
        return box, sq

    brk = None
    if long_red(t):
        box, sq = setup_before(t)
        if box or sq:
            brk = dict(day=t, kind='今日突破', box=box, squeeze=sq)
    elif long_red(y):
        box, sq = setup_before(y)
        if (box or sq) and L[t] <= max(up[y], H[y]) and C[t] >= L[y] and L[t] > L[y]:
            brk = dict(day=y, kind='昨日突破、今日回測守住', box=box, squeeze=sq)
    S['A'] = bool(S[0] and S[1] and S[2] and S[3] and brk)

    # 第 4B 關：回測型
    rd = P['ride_days']
    ride = sum(C[i] >= ma20[i] for i in range(t - rd + 1, t + 1)) / rd >= P['ride_ratio'] and ma20[t] > ma20[t - k]
    near = abs(L[t] / ma20[t] - 1) <= P['pull_near'] or L[t] <= ma20[t] <= C[t]
    stop_fall = C[t] >= ma20[t] and (C[t] >= O[t] or (min(O[t], C[t]) - L[t]) >= (H[t] - L[t]) * 0.4)
    dry = V[t] < v5 * P['pull_vol']
    S['B'] = bool(S[0] and S[1] and ride and near and stop_fall and dry)

    # 停損與部位
    if S['A']:
        bl = L[brk['day']]
        stop = max(bl, ma20[t]) if max(bl, ma20[t]) < C[t] else min(bl, ma20[t])
        r['type'] = '突破型'
        r['setup'] = brk['kind'] + ('，箱型整理後' if brk['box'] else '') + ('，布林縮口後' if brk['squeeze'] else '')
        r['stop'] = px(stop)
        r['stop_why'] = f'長紅 K 低點 {px(bl)}、MA20 {px(ma20[t])}，取較近者'
    elif S['B']:
        stop = min(ma20[t], L[t])
        r['type'] = '回測型'
        r['setup'] = f'近 {rd} 日沿月線，量縮 {round(V[t] / v5, 2)} 倍回測 MA20 止跌'
        r['stop'] = px(stop * 0.995)
        r['stop_why'] = f'跌破 MA20 {px(ma20[t])} 或今日低點 {px(L[t])}'
    r['ma'] = dict(ma5=px(ma5[t]), ma20=px(ma20[t]), ma60=px(ma60[t]), boll_up=px(up[t]), boll_lo=px(lo[t]))
    r['osc'] = [round(osc[y], 2), round(osc[t], 2)]
    r['watch'] = bool(S[2] and S[3])          # 自選股清單：每天只盯過 2、3 關的
    return r


def size(r, capital, risk):
    if not capital or 'stop' not in r or r['stop'] >= r['close']:
        return
    per = r['close'] - r['stop']
    for pct in (risk, 0.05):
        sh = min(int(capital * pct / per), int(capital / r['close']))   # 不超過總資金買得到的股數
        r.setdefault('size', []).append(dict(risk=pct, shares=sh, lots=round(sh / 1000, 2), amount=round(sh * r['close'])))


# ---------- 輸出 ----------
def mark(b):
    return '✓' if b else '✗'


def report(res, list_mode):
    ok = [r for r in res if 'error' not in r]
    date = max((r['date'] for r in ok), default='')
    lines = [f'# 盤後選股 {date}', '',
             f'掃描 {len(res)} 檔，資料有效 {len(ok)} 檔。']
    cands = [r for r in ok if r.get('type')]
    lines += ['', f'## 候選標的（{len(cands)} 檔）', '']
    if cands:
        lines += ['| 代號 | 名稱 | 型態 | 收盤 | 漲跌% | 量比 | 建議停損 | 停損距離% | 說明 |', '|---|---|---|---|---|---|---|---|---|']
        for r in sorted(cands, key=lambda r: (r['type'], -r['vol_ratio'])):
            dist = round((r['close'] - r['stop']) / r['close'] * 100, 1)
            lines.append(f"| {r['code']} | {r['name']} | {r['type']} | {r['close']} | {r['chg']} | {r['vol_ratio']} | "
                         f"{r['stop']} | {dist} | {r['setup']}；停損：{r['stop_why']} |")
        sized = [r for r in cands if r.get('size')]
        if sized:
            lines += ['', '部位試算（單筆虧損 = 總資金 × 比例）：', '']
            for r in sized:
                s = '、'.join(f"{int(x['risk'] * 100)}% → {x['shares']} 股（約 {x['amount']:,} 元）" for x in r['size'])
                lines.append(f"- {r['code']} {r['name']}：{s}")
    else:
        lines.append('今天沒有標的同時通過所有關卡。')
    if list_mode:
        w = [r for r in ok if r['watch'] and not r.get('type')]
        lines += ['', f'## 自選股：過第 2、3 關但型態未到（{len(w)} 檔）', '']
        for r in w:
            lines.append(f"- {r['code']} {r['name']} 收 {r['close']}，量比 {r['vol_ratio']}，"
                         f"第0關 {mark(r['stages'][0])} 第1關 {mark(r['stages'][1])}")
    lines += ['', '## 各關明細', '', '| 代號 | 名稱 | 收盤 | 0 週線 | 1 趨勢 | 2 動能 | 3 量能 | A 突破 | B 回測 | 量比 | OSC 昨→今 |',
              '|---|---|---|---|---|---|---|---|---|---|---|']
    for r in ok:
        s = r['stages']
        lines.append(f"| {r['code']} | {r['name']} | {r['close']} | {mark(s[0])} | {mark(s[1])} | {mark(s[2])} | "
                     f"{mark(s[3])} | {mark(s['A'])} | {mark(s['B'])} | {r['vol_ratio']} | {r['osc'][0]} → {r['osc'][1]} |")
    for r in res:
        if 'error' in r:
            lines.append(f"\n- {r['code']}：{r['error']}")
    notes = sorted({f"{r['code']} {n}" for r in ok for n in r['notes']})
    if notes:
        lines += ['', '備註：'] + [f'- {n}' for n in notes]
    lines += ['', '進場後：停損跌破月線或突破長紅 K 低點；單筆虧損控制在總資金 2%～5%；停利用移動停損，跟著月線或 5 日線上移。']
    return '\n'.join(lines) + '\n'


def push_text(res, list_mode):
    ok = [r for r in res if 'error' not in r]
    date = max((r['date'] for r in ok), default='')
    cands = [r for r in ok if r.get('type')]
    out = [f'📈 盤後選股 {date}', '']
    if not cands:
        out.append('今天沒有符合條件的標的。')
    for r in cands:
        out.append(f"{r['code']} {r['name']}｜{r['type']}｜收 {r['close']}（{r['chg']:+}%）量比 {r['vol_ratio']}")
        out.append(f"  停損 {r['stop']}：{r['stop_why']}")
    if list_mode:
        w = [r for r in ok if r['watch'] and not r.get('type')]
        if w:
            out += ['', '自選股過 2、3 關：' + '、'.join(f"{r['code']}{r['name']}" for r in w)]
    return '\n'.join(out) + '\n'


# ---------- 指令 ----------
def files(d):
    for f in sorted(os.listdir(d)):
        c, ext = os.path.splitext(f)
        if ext in ('.json', '.csv'):
            yield c, os.path.join(d, f)


def cmd_scan(a):
    wl = load_list(a.list)
    res = []
    for code, path in files(a.data):
        if wl is not None and code not in wl:
            continue
        name, bars = load_bars(path)
        r = analyze(code, (wl or {}).get(code) or name, bars)
        size(r, a.capital, a.risk)
        res.append(r)
    md = report(res, wl is not None)
    for path, text in ((a.out, md), (a.txt, push_text(res, wl is not None))):
        if path:
            open(path, 'w', encoding='utf-8').write(text)
    if a.json:
        json.dump(res, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if not a.out:
        sys.stdout.write(md)


def cmd_pool(a):
    """自選股池：收盤在季線上，且布林剛擴張（帶寬比 5 日前擴大）、MACD 剛由負轉正或即將轉正。"""
    rows = []
    for code, path in files(a.data):
        name, bars = load_bars(path)
        if len(bars) < 80:
            continue
        C = [b['close'] for b in bars]
        t = len(C) - 1
        ma60 = sma(C, 60)
        _, _, _, bw = boll(C)
        _, _, osc = macd(C)
        if C[t] > ma60[t] and bw[t] > bw[t - 5] * P['expand']:
            turned = any(osc[i] > 0 >= osc[i - 1] for i in range(t - 4, t + 1))
            rows.append(dict(代號=code, 名稱=name, 收盤=round(bars[t]['raw_close'], 2),
                             帶寬擴張=round(bw[t] / bw[t - 5], 2), MACD剛轉正='是' if turned else '否'))
    rows.sort(key=lambda r: (r['MACD剛轉正'] != '是', -r['帶寬擴張']))
    out = open(a.out, 'w', encoding='utf-8-sig', newline='') if a.out else sys.stdout
    w = csv.DictWriter(out, fieldnames=['代號', '名稱', '收盤', '帶寬擴張', 'MACD剛轉正'])
    w.writeheader()
    w.writerows(rows)


def cmd_add(a):
    """每檔存成 <代號>.csv；原本是 .json 的先轉成 csv 再併入。"""
    new = {}
    for r in csv.DictReader(open(a.bars, encoding='utf-8-sig')):
        new.setdefault(r['code'].strip(), []).append(r)
    for code, rows in new.items():
        js, cs = os.path.join(a.data, code + '.json'), os.path.join(a.data, code + '.csv')
        bars = []
        if os.path.exists(cs):
            bars = load_bars(cs)[1]
        elif os.path.exists(js):
            bars = load_bars(js)[1]
        have = {b['date']: b for b in bars}
        for r in rows:
            have[r['date']] = dict(date=r['date'], **{k: float(r[k]) for k in ('open', 'high', 'low', 'close', 'volume')})
        with open(cs, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['date', 'open', 'high', 'low', 'close', 'volume'])
            for d in sorted(have):
                b = have[d]
                w.writerow([d] + [b[k] for k in ('open', 'high', 'low', 'close', 'volume')])
        if os.path.exists(js):
            os.remove(js)
        print(f'{code}: {len(have)} 天，最後 {max(have)}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('scan')
    s.add_argument('--data', required=True)
    s.add_argument('--list')
    s.add_argument('--capital', type=float)
    s.add_argument('--risk', type=float, default=0.02)
    s.add_argument('--out')
    s.add_argument('--json')
    s.add_argument('--txt')
    s.set_defaults(f=cmd_scan)
    p = sp.add_parser('pool')
    p.add_argument('--data', required=True)
    p.add_argument('--out')
    p.set_defaults(f=cmd_pool)
    d = sp.add_parser('add')
    d.add_argument('--data', required=True)
    d.add_argument('--bars', required=True)
    d.set_defaults(f=cmd_add)
    a = ap.parse_args()
    a.f(a)


if __name__ == '__main__':
    main()
