"""台股盤後選股：先過濾，再確認，最後挑進場點。

用法：
  python3 screener.py scan --data DIR [--config 設定.txt] [--list 自選股.csv] [--capital 1000000] [--risk 0.02]
                           [--out 結果.md] [--json 結果.json] [--txt 推播.txt]
                           [--html 結果.html] [--csv 結果.csv] [--out-dir 結果資料夾] [--open]
  python3 screener.py pool --data DIR [--out 自選股候選.csv]
  python3 screener.py add  --data DIR --bars 今日.csv
  python3 screener.py config --show 設定.txt | --write 設定.txt | --check {capital,risk,mode} | --check-watchlist 自選股.csv
                           （給 setup.bat 用；--write 讀環境變數 IN_CAPITAL、IN_RISK、IN_MODE）

選項優先順序：命令列 > 設定.txt（--config）> 預設值。--out-dir 或設定檔的 output_dir 會一次產生
盤後選股_<日期>.html/.csv/.md/.txt/.json。

結束代碼：0 成功；6 找不到股價資料；7 自選股清單有問題；8 檔案或視窗的文字編碼不對；
9 設定檔有問題；1 程式發生未預期的錯誤。

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
import argparse, csv, datetime as dt, html, io, json, math, os, pathlib, re, sys, traceback

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


HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, '設定.範例.txt')
E_NEED_DATA, E_WATCHLIST, E_ENCODING, E_CONFIG = 6, 7, 8, 9
FIX_CONFIG = '用記事本打開 設定.txt 修改（可以參考 設定.範例.txt），或重新雙擊 setup.bat。'
FIX_ENCODING = '用記事本打開它，按「檔案 → 另存新檔」，下方「編碼」選 UTF-8 後存檔，再執行一次。'
MSG_UNEXPECTED = '請把上面的錯誤訊息（或 logs 資料夾裡今天的紀錄檔）貼到 GitHub 專案的 Issues 回報。'


class UserError(Exception):
    """給使用者看的失敗：代碼、原因、怎麼做"""

    def __init__(self, code, cause, todo):
        super().__init__(cause)
        self.code, self.cause, self.todo = code, cause, todo


def _tee(line):
    p = os.environ.get('TWSTOCK_LOG')
    if p:
        try:
            with open(p, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


def say(*a):
    """給使用者的訊息：印到 stderr（不污染 stdout 的報告），有設 TWSTOCK_LOG 就同時寫進去"""
    line = ' '.join(str(x) for x in a)
    print(line, file=sys.stderr, flush=True)
    _tee(line)


def fail(code, cause, todo):
    for line in ('', '【沒有完成】', '原因：' + cause, '怎麼做：' + todo):
        say(line)
    sys.exit(code)


def read_text(path, what):
    """讀使用者編輯的文字檔：UTF-8（有無 BOM 都可）優先，其次 Big5（附警告）；UTF-16 或都不行就報編碼錯誤"""
    with open(path, 'rb') as f:
        raw = f.read()
    bad = UserError(E_ENCODING, f'{what}「{path}」不是 UTF-8 編碼，程式讀不懂。', FIX_ENCODING)
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff') or b'\x00' in raw:
        raise bad
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode('cp950')
    except UnicodeDecodeError:
        raise bad
    say(f'注意：{what}「{os.path.basename(path)}」是 Big5（ANSI）編碼，已經自動讀取；建議用記事本另存成 UTF-8。')
    return text


# ---------- 設定檔 ----------
CONFIG_KEYS = ('capital', 'risk', 'mode', 'watchlist', 'output_dir')


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
        v = v[1:-1].strip()
    return v


def parse_capital(v):
    s = _unquote(v)
    for ch in (',', '_', ' ', '，', '元'):
        s = s.replace(ch, '')
    s = ''.join(s.split())
    if s == '':
        return None
    if not re.fullmatch(r'[0-9]+(\.[0-9]+)?', s):
        raise ValueError('總資金只能寫半形數字，例如 1000000（不用逗號也可以；不想算部位就寫 0 或留空）')
    x = float(s)
    if x == 0:
        return None
    if x > 1e12:
        raise ValueError('總資金太大了，請確認有沒有多打幾個 0')
    return int(x)


def parse_risk(v):
    s = ''.join(_unquote(v).split())
    if s[-1:] in ('%', '％'):
        s = s[:-1]
    if s == '':
        return 0.02
    if not re.fullmatch(r'[0-9]+(\.[0-9]+)?|\.[0-9]+', s):
        raise ValueError('每筆風險要寫成 2 到 5 之間的數字，例如 2 或 2%')
    x = float(s)
    pct = round(x * 100 if x < 1 else x, 6)
    if not 2 <= pct <= 5:
        raise ValueError(f'每筆風險要在 2% 到 5% 之間，目前是 {format(pct, "g")}%')
    return round(pct / 100, 6)


def parse_mode(v):
    s = _unquote(v).strip().lower()
    if s in ('', 'full', 'all', '1', '全市場'):
        return 'full'
    if s in ('watchlist', 'list', '2', '自選股', '自選'):
        return 'watchlist'
    raise ValueError('模式只能寫 full（全市場）或 watchlist（自選股）')


def _cfg_path(v, base):
    v = os.path.expandvars(v)
    return os.path.abspath(v if os.path.isabs(v) else os.path.join(base, v))


def load_config(path):
    name = os.path.basename(path)
    try:
        text = read_text(path, '設定檔')
    except FileNotFoundError:
        raise UserError(E_CONFIG, f'找不到設定檔「{path}」。', FIX_CONFIG)
    base = os.path.dirname(os.path.abspath(path))
    got = {}                                   # key -> (行號, 該行, 值)；重複的以最後一行為準
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        if '=' not in line and '＝' in line:
            line = line.replace('＝', '=', 1)
        if '=' not in line:
            say(f'注意：設定檔「{name}」第 {n} 行看不懂，已略過：{line}')
            continue
        k, v = line.split('=', 1)
        k = k.strip().lower()
        v = v.strip()
        if v.startswith('#'):
            v = ''
        v = _unquote(re.split(r'\s+#', v, 1)[0])
        if k not in CONFIG_KEYS:
            say(f'注意：設定檔「{name}」第 {n} 行的「{k}」不認得，已略過。可以用的項目：{"、".join(CONFIG_KEYS)}')
            continue
        got[k] = (n, line, v)
    cfg = dict(capital=None, risk=0.02, mode='full', path=os.path.abspath(path),
               watchlist=_cfg_path('自選股.csv', base), output_dir=_cfg_path('結果', base))
    for k, (n, line, v) in got.items():
        if v == '':
            continue
        try:
            if k == 'capital':
                cfg[k] = parse_capital(v)
            elif k == 'risk':
                cfg[k] = parse_risk(v)
            elif k == 'mode':
                cfg[k] = parse_mode(v)
            else:
                cfg[k] = _cfg_path(v, base)
        except ValueError as e:
            raise UserError(E_CONFIG, f'設定檔「{name}」第 {n} 行「{line}」不對：{e}。', FIX_CONFIG)
    return cfg


def describe_config(cfg):
    cap = '%s 元' % format(cfg['capital'], ',') if cfg['capital'] else '不設定（不試算部位）'
    scope = '全市場' if cfg['mode'] == 'full' else '自選股清單（%s）' % cfg['watchlist']
    return [f'總資金：{cap}', f'每筆風險：{format(cfg["risk"] * 100, "g")}%', f'範圍：{scope}',
            f'結果存放：{cfg["output_dir"]}']


def write_config(path, capital, risk, mode):
    """只改 capital、risk、mode 三行（沒有就補在最後），其他行（註解、output_dir…）原樣保留"""
    text = None
    for src in (path, TEMPLATE):
        try:
            text = read_text(src, '設定檔')
            break
        except (OSError, UserError):
            continue
    if text is None:
        text = '# 台股盤後選股設定\ncapital=0\nrisk=2%\nmode=full\n'
    vals = dict(capital=str(capital or 0), risk='%s%%' % format(risk * 100, 'g'), mode=mode)
    pat = re.compile(r'^(\s*)(capital|risk|mode)(\s*[=＝]\s*)', re.I)
    lines, seen = [], set()
    for line in text.splitlines():
        m = pat.match(line)
        if m:
            k = m.group(2).lower()
            seen.add(k)
            line = f'{m.group(1)}{m.group(2)}={vals[k]}'
        lines.append(line)
    for k in ('capital', 'risk', 'mode'):
        if k not in seen:
            lines.append(f'{k}={vals[k]}')
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(('\r\n'.join(lines) + '\r\n').encode('utf-8'))
    os.replace(tmp, path)


def load_list(path):
    if not path:
        return None
    base = os.path.basename(path)
    try:
        text = read_text(path, '自選股清單')
    except FileNotFoundError:
        if os.path.exists(path + '.txt'):
            todo = f'找到「{base}.txt」，請把檔名改成「{base}」（Windows 可能隱藏了副檔名）。'
        else:
            todo = '把 自選股.範例.csv 複製一份改名為 自選股.csv 再修改；不用自選股的話，把 設定.txt 的 mode 改成 full。'
        raise UserError(E_WATCHLIST, f'找不到自選股清單「{path}」。', todo)
    rd = csv.DictReader(io.StringIO(text, newline=''))
    names = [(n or '').strip() for n in (rd.fieldnames or [])]
    if '代號' not in names:
        raise UserError(E_WATCHLIST, f'自選股清單第一列要有「代號」欄位，目前第一列是「{",".join(names)[:60]}」。',
                        '用記事本打開它，把第一列改成「代號,名稱,備註」（可以參考 自選股.範例.csv），存檔後再執行。')
    rd.fieldnames = names
    out = {}
    for r in rd:
        code = (r.get('代號') or '').strip().lstrip('=').strip('"').strip()
        if code:
            out[code] = (r.get('名稱') or '').strip()
    if not out:
        raise UserError(E_WATCHLIST, '自選股清單裡沒有任何股票代號。',
                        '用記事本打開它，從第二列開始一行寫一檔，例如「2330,台積電,」。')
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


# ---------- HTML / CSV 輸出（給不用命令列的人看） ----------
HTML_HEAD = '''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>
:root{--bg:#f4f6f8;--fg:#1f2933;--mut:#617080;--card:#fff;--line:#e1e6ec;--up:#d92d20;--down:#12805c;--mid:#c2610c;--cap:#7c3aed;--warnbg:#fff7d6;--warnfg:#7a5b00}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--fg:#e6eaee;--mut:#9aa7b4;--card:#1d232a;--line:#2f3841;--up:#ff6b5e;--down:#3ccf9a;--mid:#f0a050;--cap:#b794f6;--warnbg:#3a3112;--warnfg:#f3d97a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 "Microsoft JhengHei","PingFang TC","Noto Sans TC",sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}
h1{font-size:24px;margin:8px 0 4px}
h2{font-size:19px;margin:0 0 8px}
.meta{color:var(--mut);margin:0 0 12px}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}
nav a{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 12px;color:var(--fg);text-decoration:none}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:0 0 16px}
.warn{background:var(--warnbg);color:var(--warnfg);border-radius:6px;padding:8px 12px;margin:0 0 12px}
.note{color:var(--mut);margin:0 0 8px}
.empty{color:var(--mut);margin:8px 0}
.tw{overflow:auto;max-height:80vh}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;vertical-align:top}
thead th{position:sticky;top:0;background:var(--card);z-index:1;cursor:pointer;user-select:none;border-bottom:2px solid var(--line)}
th[aria-sort=ascending]::after{content:" \\25B2"}
th[aria-sort=descending]::after{content:" \\25BC"}
.n{text-align:right}
td.desc{white-space:normal;min-width:260px}
.tag{display:inline-block;border-radius:4px;padding:0 8px;font-weight:600}
.t-break{background:#fde8e8;color:#b42318}
.t-pull{background:#e8f0fe;color:#1a56db}
.up{color:var(--up)}
.down{color:var(--down)}
.d-ok{color:var(--down);font-weight:600}
.d-mid{color:var(--mid);font-weight:600}
.d-wide{color:var(--up);font-weight:600}
.p-zero{color:var(--mut)}
.p-cap{color:var(--cap)}
.p-odd{color:var(--mid)}
.y{color:var(--down);text-align:center}
.x{color:var(--mut);text-align:center}
small{color:var(--mut)}
details{margin:8px 0}
summary{cursor:pointer}
.help li{margin:4px 0}
</style>
</head>
<body>
<main>
'''

HTML_TAIL = '''</main>
<script>
(function () {
  function val(td, num) {
    if (num) {
      var v = td.getAttribute('data-v');
      if (v === null) { v = td.textContent; }
      v = parseFloat(v);
      return isNaN(v) ? -Infinity : v;
    }
    return td.textContent;
  }
  function sortBy(t, i, th) {
    var num = th.getAttribute('data-type') === 'num';
    var dir = th.getAttribute('aria-sort') === 'ascending' ? 'descending' : 'ascending';
    Array.prototype.forEach.call(t.tHead.rows[0].cells, function (h) { h.removeAttribute('aria-sort'); });
    th.setAttribute('aria-sort', dir);
    var body = t.tBodies[0];
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = val(a.cells[i], num), y = val(b.cells[i], num), r;
      if (num) { r = x < y ? -1 : (x > y ? 1 : 0); } else { r = String(x).localeCompare(String(y), 'zh-Hant'); }
      return dir === 'ascending' ? r : -r;
    });
    var f = document.createDocumentFragment();
    rows.forEach(function (r) { f.appendChild(r); });
    body.appendChild(f);
  }
  Array.prototype.forEach.call(document.querySelectorAll('table.sortable'), function (t) {
    Array.prototype.forEach.call(t.tHead.rows[0].cells, function (th, i) {
      th.setAttribute('tabindex', '0');
      th.addEventListener('click', function () { sortBy(t, i, th); });
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortBy(t, i, th); }
      });
    });
  });
})();
</script>
</body>
</html>
'''

WEEKDAYS = '一二三四五六日'


def _dow(date):
    try:
        return '（星期%s）' % WEEKDAYS[dt.date.fromisoformat(date).weekday()]
    except ValueError:
        return ''


def _th(label, num=False):
    return '<th class="n" data-type="num">%s</th>' % html.escape(label) if num else '<th>%s</th>' % html.escape(label)


def _table(cols, rows):
    """cols：[(標題, 是否數字欄)]；rows：已經組好的 <tr> 字串"""
    return ('<div class="tw"><table class="sortable"><thead><tr>' + ''.join(_th(c, n) for c, n in cols)
            + '</tr></thead><tbody>\n' + '\n'.join(rows) + '\n</tbody></table></div>')


def _num(v, text=None, cls=''):
    """數字欄：data-v 供排序用，顯示文字 text"""
    c = ' ' + cls if cls else ''
    return '<td class="n%s" data-v="%s">%s</td>' % (c, html.escape(str(v)), html.escape(str(v) if text is None else text))


def _chk(b):
    return '<td class="y" data-v="1">✓</td>' if b else '<td class="x" data-v="0">✗</td>'


def _pos_cell(r, idx, capital):
    sz = r.get('size') or []
    if idx >= len(sz):
        return '<td class="n" data-v="-1">—</td>'
    s = sz[idx]
    sh = s['shares']
    if sh <= 0:
        return '<td class="n p-zero" data-v="0">0 股（停損太遠或資金不足）</td>'
    cls, extra = '', ''
    if capital and sh >= int(capital / r['close']):
        cls, extra = ' p-cap', '（已達總資金上限）'
    elif sh < 1000:
        cls, extra = ' p-odd', '（不到 1 張，零股）'
    return '<td class="n%s" data-v="%s">%s 股（%s 張）%s<br><small>約 %s 元</small></td>' % (
        cls, sh, format(sh, ','), format(s['lots'], 'g'), extra, format(s['amount'], ','))


def render_html(res, list_mode, meta):
    """產生單一 HTML 檔（不連外部資源）。不會修改 res。"""
    h = html.escape
    ok = [r for r in res if 'error' not in r]
    date = max((r['date'] for r in ok), default='')
    capital, risk = meta.get('capital'), meta.get('risk') or 0.02
    title = f'台股盤後選股 {date}{_dow(date)}'
    cands = sorted([r for r in ok if r.get('type')], key=lambda r: (r['type'], -r['vol_ratio']))
    watch = [r for r in ok if r['watch'] and not r.get('type')]
    risk_txt = format(risk * 100, 'g')

    scope = '全市場'
    if list_mode:
        scope = '自選股清單（%s，%d 檔）' % (os.path.basename(meta.get('list_path') or ''), meta.get('list_count') or 0)
    pos = ('總資金 %s 元、每筆風險 %s%%' % (format(int(capital), ','), risk_txt)) if capital else '未設定總資金，不試算部位'
    metaline = '｜'.join(['資料日期 ' + date, '掃描 %d 檔（資料有效 %d 檔）' % (len(res), len(ok)), '範圍：' + scope,
                         '部位：' + pos, '產生時間 ' + str(meta.get('generated') or '')])
    B = [f'<header><h1>{h(title)}</h1>', f'<p class="meta">{h(metaline)}</p>']
    if date and meta.get('today') and date != meta['today']:
        B.append('<p class="warn">資料日期不是今天：今天可能是週末、休市，或官方還沒公布收盤資料。</p>')
    B.append('<nav><a href="#cands">候選標的</a><a href="#watch">%s</a><a href="#stages">各關明細</a>'
             '<a href="#help">怎麼看</a></nav></header>' % ('自選股觀察' if list_mode else '觀察名單'))

    # 候選標的
    B.append(f'<section id="cands"><h2>候選標的（{len(cands)} 檔）</h2>')
    if cands:
        cols = [('代號', False), ('名稱', False), ('型態', False), ('收盤', True), ('漲跌%', True), ('量比', True),
                ('建議停損', True), ('停損距離%', True)]
        if capital:
            cols.append((f'部位（風險 {risk_txt}%）', True))
            if risk != 0.05:
                cols.append(('部位（5%）', True))
        cols.append(('說明', False))
        rows = []
        for r in cands:
            dist = round((r['close'] - r['stop']) / r['close'] * 100, 1)
            dcls = 'd-ok' if dist <= 3 else ('d-mid' if dist <= 6 else 'd-wide')
            tcls = 't-break' if r['type'] == '突破型' else 't-pull'
            ccls = 'up' if r['chg'] > 0 else ('down' if r['chg'] < 0 else '')
            tds = [f'<td>{h(r["code"])}</td>', f'<td>{h(r["name"])}</td>',
                   f'<td><span class="tag {tcls}">{h(r["type"])}</span></td>',
                   _num(r['close']), _num(r['chg'], '%+.2f' % r['chg'], ccls), _num(r['vol_ratio'], '%.2f' % r['vol_ratio']),
                   _num(r['stop']), _num(dist, '%.1f' % dist, dcls)]
            if capital:
                tds.append(_pos_cell(r, 0, capital))
                if risk != 0.05:
                    tds.append(_pos_cell(r, 1, capital))
            tds.append('<td class="desc">%s</td>' % h(r['setup'] + '；停損：' + r['stop_why']))
            rows.append('<tr>' + ''.join(tds) + '</tr>')
        B.append(_table(cols, rows))
    else:
        B.append('<p class="empty">今天沒有標的同時通過所有關卡。</p>')
    B.append('</section>')

    # 觀察名單
    wtitle = '自選股：過第 2、3 關但型態未到' if list_mode else '觀察名單'
    B.append(f'<section id="watch"><h2>{h(wtitle)}（{len(watch)} 檔）</h2>')
    B.append('<p class="note">已經通過第 2 關（動能）和第 3 關（量能），但突破型／回測型的型態條件還沒成立。'
             '值得每天盯著，等型態成立再決定要不要進場。</p>')
    missing = meta.get('missing') or []
    if list_mode and missing:
        B.append('<p class="warn">自選股清單裡有 %d 個代號找不到資料：%s（可能打錯、已下市，或被 Excel 去掉開頭的 0）。</p>'
                 % (len(missing), h('、'.join(missing[:10]))))
    if watch:
        cols = [('代號', False), ('名稱', False), ('收盤', True), ('漲跌%', True), ('量比', True),
                ('0 週線', True), ('1 趨勢', True), ('OSC 昨→今', True)]
        rows = []
        for r in watch:
            ccls = 'up' if r['chg'] > 0 else ('down' if r['chg'] < 0 else '')
            rows.append('<tr>' + ''.join([
                f'<td>{h(r["code"])}</td>', f'<td>{h(r["name"])}</td>', _num(r['close']),
                _num(r['chg'], '%+.2f' % r['chg'], ccls), _num(r['vol_ratio'], '%.2f' % r['vol_ratio']),
                _chk(r['stages'][0]), _chk(r['stages'][1]),
                _num(r['osc'][1], '%s → %s' % (r['osc'][0], r['osc'][1]))]) + '</tr>')
        B.append(_table(cols, rows))
    else:
        B.append('<p class="empty">目前沒有。</p>')
    B.append('</section>')

    # 各關明細
    B.append(f'<section id="stages"><h2>各關明細（{len(ok)} 檔）</h2>')
    if ok:
        cols = [('代號', False), ('名稱', False), ('收盤', True), ('0 週線', True), ('1 趨勢', True), ('2 動能', True),
                ('3 量能', True), ('A 突破', True), ('B 回測', True), ('量比', True), ('OSC 昨→今', True)]
        rows = []
        for r in ok:
            s = r['stages']
            rows.append('<tr>' + ''.join([
                f'<td>{h(r["code"])}</td>', f'<td>{h(r["name"])}</td>', _num(r['close']),
                _chk(s[0]), _chk(s[1]), _chk(s[2]), _chk(s[3]), _chk(s['A']), _chk(s['B']),
                _num(r['vol_ratio'], '%.2f' % r['vol_ratio']),
                _num(r['osc'][1], '%s → %s' % (r['osc'][0], r['osc'][1]))]) + '</tr>')
        B.append(_table(cols, rows))
    errs = [r for r in res if 'error' in r]
    if errs:
        B.append('<details><summary>資料不足、沒有參與判斷的 %d 檔</summary><ul>%s</ul></details>' % (
            len(errs), ''.join('<li>%s：%s</li>' % (h(r['code']), h(r['error'])) for r in errs)))
    notes = sorted({f"{r['code']} {n}" for r in ok for n in r['notes']})
    if notes:
        B.append('<details><summary>備註（%d 則）</summary><ul>%s</ul></details>' % (
            len(notes), ''.join('<li>%s</li>' % h(n) for n in notes)))
    B.append('</section>')

    # 說明
    B.append('''<section id="help" class="help"><h2>怎麼看這一頁</h2><ul>
<li>點表格最上面的欄位名稱可以排序，再點一次反過來排。</li>
<li>型態：<span class="tag t-break">突破型</span> 整理後帶量長紅突破；<span class="tag t-pull">回測型</span> 沿月線上行、量縮回測月線止跌。</li>
<li>漲跌%：台灣習慣，<span class="up">紅色是上漲</span>，<span class="down">綠色是下跌</span>。</li>
<li>停損距離%：<span class="d-ok">3% 以內（綠）</span>風險空間小；<span class="d-mid">3%～6%（橘）</span>普通；<span class="d-wide">超過 6%（紅）</span>停損很遠，單筆虧損會偏大。</li>
<li>部位：依「單筆虧損 = 總資金 × 比例」反推股數，1 張 = 1000 股。<span class="p-cap">紫色</span>表示已經買到總資金上限，<span class="p-odd">橘色</span>表示不到 1 張（零股）。沒設定總資金就不會有這一欄。</li>
<li>各關明細：✓ 表示通過、✗ 表示沒通過。0 週線、1 趨勢、2 動能、3 量能四關，加上 A 突破或 B 回測型態，才會出現在候選標的。</li>
</ul>
<p>進場後：停損跌破月線或突破長紅 K 低點；單筆虧損控制在總資金 2%～5%；停利用移動停損，跟著月線或 5 日線上移。</p>
<p class="note">本頁僅供程式與技術分析研究，不是投資建議，也不保證任何績效。資料可能延遲、缺漏或有誤，請以交易所公告為準。投資風險由使用者自行承擔。</p>
</section>
''')
    return HTML_HEAD.replace('@@TITLE@@', h(title)) + '\n'.join(B) + '\n' + HTML_TAIL


CSV_HEAD = ['代號', '名稱', '資料日期', '型態', '收盤', '漲跌%', '量比', '建議停損', '停損距離%', '股數(設定風險)', '金額(設定風險)',
            '股數(5%)', '金額(5%)', '0週線', '1趨勢', '2動能', '3量能', 'A突破', 'B回測', '說明', '停損依據', '備註']


def write_csv(path, res, capital):
    """Excel 用的 csv：UTF-8 含 BOM；代號寫成 ="0050" 讓 Excel 不會把開頭的 0 吃掉。不會修改 res。"""
    ok = [r for r in res if 'error' not in r]
    cands = sorted([r for r in ok if r.get('type')], key=lambda r: (r['type'], -r['vol_ratio']))
    rest = [r for r in ok if not r.get('type')]
    yn = lambda b: '是' if b else '否'
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, dialect='excel')
        w.writerow(CSV_HEAD)
        for r in cands + rest:
            s, sz = r['stages'], r.get('size') or []
            dist = round((r['close'] - r['stop']) / r['close'] * 100, 1) if r.get('type') else ''
            pos = []
            for i in (0, 1):
                pos += [sz[i]['shares'], sz[i]['amount']] if i < len(sz) else ['', '']
            w.writerow([f'="{r["code"]}"', r['name'], r['date'], r.get('type', ''), r['close'], r['chg'], r['vol_ratio'],
                        r.get('stop', ''), dist] + pos
                       + [yn(s[0]), yn(s[1]), yn(s[2]), yn(s[3]), yn(s['A']), yn(s['B']),
                          r.get('setup', ''), r.get('stop_why', ''), '；'.join(r['notes'])])
        for r in res:
            if 'error' in r:
                w.writerow([f'="{r["code"]}"', r['name']] + [''] * 19 + [r['error']])


# ---------- 指令 ----------
def files(d):
    for f in sorted(os.listdir(d)):
        c, ext = os.path.splitext(f)
        if ext in ('.json', '.csv'):
            yield c, os.path.join(d, f)


def resolve_scan_options(a):
    """優先順序：命令列 > 設定檔（--config）> 預設值"""
    cfg = load_config(a.config) if getattr(a, 'config', None) else None
    capital = a.capital if a.capital is not None else (cfg['capital'] if cfg else None)
    risk = a.risk if a.risk is not None else (cfg['risk'] if cfg else 0.02)
    wl = a.list
    if wl is None and cfg and cfg['mode'] == 'watchlist':
        wl = cfg['watchlist']
    out_dir = a.out_dir or (cfg['output_dir'] if cfg else None)
    return dict(capital=capital, risk=risk, list=wl, out_dir=out_dir)


def open_file(path):
    if os.environ.get('TWSTOCK_NO_OPEN'):
        return
    try:
        if os.name == 'nt':
            os.startfile(path)
        else:
            import webbrowser
            webbrowser.open(pathlib.Path(path).resolve().as_uri())
    except OSError as e:
        say(f'無法自動打開瀏覽器（{e}），請自己雙擊打開：{os.path.abspath(path)}')


def cmd_scan(a):
    o = resolve_scan_options(a)
    capital, risk = o['capital'], o['risk']
    if not os.path.isdir(a.data) or next(files(a.data), None) is None:
        raise UserError(E_NEED_DATA, f'找不到股價資料：資料夾「{a.data}」不存在或裡面沒有任何 .csv。',
                        '請先雙擊 setup.bat 並選擇「現在下載」，或在程式資料夾執行 python fetch_data.py backfill。')
    wl = load_list(o['list'])
    res = []
    for code, path in files(a.data):
        if wl is not None and code not in wl:
            continue
        name, bars = load_bars(path)
        r = analyze(code, (wl or {}).get(code) or name, bars)
        size(r, capital, risk)
        res.append(r)
    missing = []
    if wl is not None:
        if not res:
            raise UserError(E_WATCHLIST, f'自選股清單裡的 {len(wl)} 個代號在資料裡都找不到（例如 {"、".join(list(wl)[:5])}）。',
                            '確認代號沒打錯，ETF 要保留開頭的 0（例如 0050 不要寫成 50），改好後再執行。')
        found = {r['code'] for r in res}
        missing = [c for c in wl if c not in found]
        if missing:
            say(f'注意：自選股清單裡有 {len(missing)} 個代號找不到資料：{"、".join(missing[:10])}'
                '（可能打錯、已下市，或被 Excel 去掉開頭的 0）。')
    md = report(res, wl is not None)
    valid = [r['date'] for r in res if 'error' not in r]
    tag = max(valid).replace('-', '') if valid else dt.date.today().strftime('%Y%m%d')

    def target(explicit, ext):
        if explicit:
            return explicit
        if o['out_dir']:
            os.makedirs(o['out_dir'], exist_ok=True)
            return os.path.join(o['out_dir'], f'盤後選股_{tag}.{ext}')
        return None

    md_path, txt_path, json_path = target(a.out, 'md'), target(a.txt, 'txt'), target(a.json, 'json')
    html_path, csv_path = target(a.html, 'html'), target(a.csv, 'csv')
    for path, text in ((md_path, md), (txt_path, push_text(res, wl is not None))):
        if path:
            open(path, 'w', encoding='utf-8').write(text)
    if json_path:
        json.dump(res, open(json_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if html_path:
        meta = dict(capital=capital, risk=risk, list_path=o['list'], list_count=len(wl) if wl else 0, missing=missing,
                    today=dt.date.today().isoformat(), generated=dt.datetime.now().strftime('%Y-%m-%d %H:%M'))
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(render_html(res, wl is not None, meta))
    if csv_path:
        write_csv(csv_path, res, capital)
    if not md_path:
        sys.stdout.write(md)
    if any((html_path, csv_path, md_path, txt_path, json_path)):
        say(f'選股完成：掃描 {len(res)} 檔，候選 {sum(1 for r in res if r.get("type"))} 檔。')
        for label, p in (('網頁報告', html_path), ('Excel 表格', csv_path), ('文字報告', md_path)):
            if p:
                say(f'  {label}：{os.path.abspath(p)}')
    if a.open:
        if html_path:
            open_file(html_path)
        else:
            say('沒有產生 HTML（要加 --html 或 --out-dir），--open 略過。')


CHECK_HINT = dict(capital='請只打半形數字，例如 1000000；不想算部位就直接按 Enter。',
                  risk='請打 2 到 5 之間的數字，例如 2；直接按 Enter = 2。',
                  mode='請打 1（全市場）或 2（自選股）。')


def cmd_config(a):
    """給 setup.bat 用：使用者輸入一律從環境變數 IN_xxx 讀，不經過批次檔的 % 展開"""
    if a.check:
        v = os.environ.get('IN_' + a.check.upper(), '')
        try:
            if a.check == 'capital':
                c = parse_capital(v)
                say('→ 總資金：%s 元' % format(c, ',') if c else '→ 總資金：不設定（不試算部位）')
            elif a.check == 'risk':
                say('→ 每筆風險：%s%%' % format(parse_risk(v) * 100, 'g'))
            else:
                say('→ 範圍：' + ('全市場' if parse_mode(v) == 'full' else '自選股清單'))
        except ValueError as e:
            say(f'輸入不正確：{e}。{CHECK_HINT[a.check]}')
            return E_CONFIG
    elif a.check_watchlist:
        say(f'→ 自選股清單讀取成功，共 {len(load_list(a.check_watchlist))} 檔。')
    elif a.show:
        for line in describe_config(load_config(a.show)):
            say('  ' + line)
    else:
        try:
            write_config(a.write, parse_capital(os.environ.get('IN_CAPITAL', '')),
                         parse_risk(os.environ.get('IN_RISK', '')), parse_mode(os.environ.get('IN_MODE', '')))
        except ValueError as e:
            raise UserError(E_CONFIG, f'輸入不正確：{e}。', '重新雙擊 setup.bat 再設定一次。')
        say(f'已經把設定存到「{os.path.basename(a.write)}」。')
    return 0


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
    s.add_argument('--config')
    s.add_argument('--list')
    s.add_argument('--capital', type=float)
    s.add_argument('--risk', type=float)
    s.add_argument('--out')
    s.add_argument('--json')
    s.add_argument('--txt')
    s.add_argument('--html')
    s.add_argument('--csv')
    s.add_argument('--out-dir')
    s.add_argument('--open', action='store_true')
    s.set_defaults(f=cmd_scan)
    c = sp.add_parser('config')
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument('--check', choices=('capital', 'risk', 'mode'))
    g.add_argument('--check-watchlist')
    g.add_argument('--show')
    g.add_argument('--write')
    c.set_defaults(f=cmd_config)
    p = sp.add_parser('pool')
    p.add_argument('--data', required=True)
    p.add_argument('--out')
    p.set_defaults(f=cmd_pool)
    d = sp.add_parser('add')
    d.add_argument('--data', required=True)
    d.add_argument('--bars', required=True)
    d.set_defaults(f=cmd_add)
    a = ap.parse_args()
    try:
        rc = a.f(a)
    except UserError as e:
        fail(e.code, e.cause, e.todo)
    except UnicodeEncodeError:
        fail(E_ENCODING, '命令列視窗的文字編碼不是 UTF-8，有些符號印不出來。',
             '請改用雙擊 run.bat，或先執行 chcp 65001 和 set PYTHONUTF8=1，或加上 --out 把結果存成檔案。')
    except Exception as e:
        text = traceback.format_exc()
        say(text.rstrip())
        fail(1, '程式發生未預期的錯誤：%s: %s' % (type(e).__name__, e), MSG_UNEXPECTED)
    sys.exit(rc or 0)


if __name__ == '__main__':
    main()
