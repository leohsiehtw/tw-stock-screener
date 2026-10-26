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


# 快速讀檔：只處理 fetch_data.py / add 產生的標準 csv，結果和 load_bars 完全一樣；
# 有任何不標準的地方（欄名、引號、欄數、編碼、數字格式…）就改用 load_bars 重讀，錯誤訊息也照舊。
FAST_HEADERS = ('date,open,high,low,close,volume,raw_close,name', 'date,open,high,low,close,volume')
FAST_FIELD_MAX = 131072          # csv 模組預設的欄位長度上限；超過交給 load_bars 處理


def _parse_fast(raw):
    """回傳 (name, bars)；不是標準格式就回傳 None"""
    if b'"' in raw:
        return None
    text = raw.decode('utf-8-sig').replace('\r\n', '\n')
    if '\r' in text or '\x00' in text:
        return None
    lines = text.split('\n')
    if lines[0] not in FAST_HEADERS:
        return None
    width = 8 if len(lines[0]) == len(FAST_HEADERS[0]) else 6
    name, seen = '', {}
    for line in lines[1:]:
        if not line:
            continue                 # csv 也略過空白行
        f = line.split(',')
        if len(f) != width or len(line) > FAST_FIELD_MAX:
            return None
        c = float(f[4])
        if width == 8:
            name = f[7] or name
            rc = float(f[6]) if f[6] != '' else c
        else:
            rc = c
        r = dict(date=f[0], open=float(f[1]), high=float(f[2]), low=float(f[3]), close=c, raw_close=rc,
                 volume=float(f[5]))
        if r['volume'] and r['close']:
            seen[r['date']] = r          # 重複日期以後者為準
    return name, [seen[k] for k in sorted(seen)]


def load_file(path, raw=None):
    """和 load_bars(path) 結果相同；raw 是已經讀好的檔案內容（None 表示沒讀到，交給 load_bars）"""
    if raw is not None and not path.endswith('.json'):
        try:
            got = _parse_fast(raw)
        except Exception:
            got = None
        if got is not None:
            return got
    return load_bars(path)


def _read_raw(path):
    if path.endswith('.json'):
        return None
    try:
        with open(path, 'rb') as f:
            return f.read()
    except Exception:
        return None                  # 讓 load_bars 重讀，丟出和以前一樣的錯誤


def read_workers():
    return min(16, max(4, os.cpu_count() or 4))


def prefetch(paths, workers=None):
    """依 paths 的順序逐一回傳 (path, 檔案內容或 None)；用多個執行緒先讀進來（最多先讀 workers×4 檔）。
    解析（load_file）由呼叫端做，錯誤才會和以前一樣從呼叫端丟出（不經過 generator）。"""
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    workers = workers or read_workers()
    it = iter(paths)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        q = deque()
        for p in it:
            q.append((p, ex.submit(_read_raw, p)))
            if len(q) >= workers * 4:
                break
        while q:
            p, fut = q.popleft()
            nxt = next(it, None)
            if nxt is not None:
                q.append((nxt, ex.submit(_read_raw, nxt)))
            yield p, fut.result()


HERE =os.path.dirname(os.path.abspath(__file__))
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


def boll(xs, n=20, k=2, start=0):
    """start > 0：上下軌、帶寬只算 index ≥ start 的部分（其餘維持 None），算出來的值和全部算時完全一樣"""
    mid = sma(xs, n)
    up, lo, bw = [None] * len(xs), [None] * len(xs), [None] * len(xs)
    for i in range(max(n - 1, start), len(xs)):
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
    # 布林只用到最後幾根：up/lo 的 t、y，與 bw[i-120:i]、bw[i-1]（i = t, y），最早是 bw[n-122]
    mid, up, lo, bw = boll(C, start=n - P['squeeze_lookback'] - 2 if n >= 141 else 0)
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
# 結果頁的顏色。測試會用 WCAG 公式檢查：文字對背景 ≥ 4.5:1、圖形記號 ≥ 3:1。
# 漲跌依台灣習慣：紅漲（up）、綠跌（down），並一律加上 ▲▼ 與正負號，不只靠顏色。
PALETTE = dict(
    light=dict(page='#ffffff', surface='#f5f5f7', nav='#fafafc', ink='#1d1d1f', ink2='#6e6e73', ink3='#86868b',
               link='#0066cc', accent='#0071e3', btn='#0071e3', btn_hover='#0076df', btn_active='#006edb',
               on_btn='#ffffff', hairline='rgba(0,0,0,.16)', band='#d2d2d7',
               up='#c8102e', down='#1d7f3a', warn='#b25000', cap='#7d3cb5'),
    dark=dict(page='#000000', surface='#1d1d1f', nav='#161617', ink='#f5f5f7', ink2='#86868b', ink3='#86868b',
              link='#2997ff', accent='#2997ff', btn='#0071e3', btn_hover='#0076df', btn_active='#006edb',
              on_btn='#ffffff', hairline='rgba(255,255,255,.24)', band='#48484a',
              up='#ff7a70', down='#30d158', warn='#ffa94d', cap='#d09cff'),
)


def _css_vars(d):
    return ';'.join('--%s:%s' % (k.replace('_', '-'), v) for k, v in d.items())


HTML_CSS = '''
:root{@@LIGHT@@;color-scheme:light;
--f-display:"SF Pro Display","SF Pro Text","Helvetica Neue",Helvetica,Arial,"PingFang TC","Microsoft JhengHei",sans-serif;
--f-text:"SF Pro Text","Helvetica Neue",Helvetica,Arial,"PingFang TC","Microsoft JhengHei",sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){@@DARK@@;color-scheme:dark}}
:root[data-theme="dark"]{@@DARK@@;color-scheme:dark}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:400 17px/25px var(--f-text);letter-spacing:-.022em}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
h1,h2,h3{font-family:var(--f-display);font-weight:600;margin:0;text-wrap:balance}
h1{font-size:40px;line-height:44px}
h2{font-size:28px;line-height:32px}
h3{font-size:21px;line-height:25px}
p{margin:0}
.wrap{max-width:1028px;margin:0 auto;padding-left:max(24px,env(safe-area-inset-left,0px));padding-right:max(24px,env(safe-area-inset-right,0px))}
.topnav{position:sticky;top:0;z-index:5;background:var(--nav);border-bottom:1px solid var(--hairline)}
.topnav .wrap{height:44px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{font-size:14px;line-height:18px;font-weight:600}
.navdate{font-size:12px;line-height:16px;color:var(--ink2)}
.band{padding-block:48px 56px;--card:var(--surface);scroll-margin-top:44px}
.band.alt{background:var(--surface);--card:var(--page)}
.stack{display:flex;flex-direction:column;gap:24px}
.sub{font-size:21px;line-height:25px;color:var(--ink2)}
.fine{font-size:12px;line-height:16px;color:var(--ink2)}
.cap14{font-size:14px;line-height:18px;color:var(--ink2)}
.jump{display:flex;flex-wrap:wrap;gap:8px 24px;font-size:14px;line-height:18px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:0}
.stats div{border-top:1px solid var(--hairline);padding-top:16px;min-width:0}
.stats dt{font-size:14px;line-height:18px;color:var(--ink2)}
.stats dd{margin:4px 0 0;font:600 28px/32px var(--f-display);font-variant-numeric:tabular-nums}
.stats dd small{font:400 14px/18px var(--f-text);color:var(--ink2);margin-left:4px}
.warn{color:var(--warn);font-weight:600}
.warn::before{content:"! ";}
.card{background:var(--card);border-radius:16px;padding:8px 24px;min-width:0}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:12px 8px;text-align:left;vertical-align:top}
thead th{font-size:12px;line-height:16px;font-weight:600;color:var(--ink2);border-bottom:1px solid var(--hairline);white-space:nowrap}
tbody th{font-weight:400}
.n{text-align:right}
td.n{white-space:nowrap}
.ct th.nm{min-width:180px}
.sb{font:inherit;color:inherit;background:none;border:0;padding:4px 0;cursor:pointer;letter-spacing:inherit;text-align:inherit}
.sb:hover{color:var(--ink)}
.si::after{content:" \\2195";opacity:.7}
th[aria-sort=ascending] .si::after{content:" \\25B2";opacity:1}
th[aria-sort=descending] .si::after{content:" \\25BC";opacity:1}
th[aria-sort] .sb{color:var(--ink)}
.ct tbody+tbody .r1>*{border-top:1px solid var(--hairline)}
.ct .r1>*{padding-top:16px}
.ct .r2 td{padding-top:0;padding-bottom:16px}
.nm .code{font-weight:600;margin-right:8px}
.nm .name{font-weight:600}
.nm .tag{margin-bottom:4px}
.tag{display:inline-block;font-size:12px;line-height:16px;font-weight:600;border:1px solid currentColor;border-radius:980px;padding:1px 8px;white-space:nowrap}
.t-break{color:var(--up)}
.t-pull{color:var(--ink)}
.up{color:var(--up)}
.down{color:var(--down)}
.flat{color:var(--ink2)}
.d-ok{color:var(--down)}
.d-mid{color:var(--warn)}
.d-wide{color:var(--up)}
.g{display:inline-block;font-size:12px;line-height:16px;font-weight:600;border:1px solid currentColor;border-radius:5px;padding:0 4px;margin-left:6px;vertical-align:1px}
.ct td.pos{font-size:17px;white-space:normal;min-width:120px}
.pos small{display:block;font-size:12px;line-height:16px;color:var(--ink2)}
.pos small.flag{color:inherit;font-weight:600}
.p-zero{color:var(--ink2)}
.p-cap{color:var(--cap)}
.p-odd{color:var(--warn)}
.r2 .inner{display:grid;grid-template-columns:minmax(0,320px) minmax(0,1fr);gap:8px 24px;align-items:start}
.desc{font-size:14px;line-height:18px;color:var(--ink2)}
.pb{position:relative;height:20px;margin:2px 6px 0}
.pb span{position:absolute}
.pb-track{left:0;right:0;top:9px;height:2px;background:var(--hairline)}
.pb-band{top:6px;height:8px;background:var(--band);border-radius:5px}
.pb-risk{top:8px;height:4px;background:var(--warn)}
.pb-stop,.pb-ma{top:2px;width:2px;height:16px;margin-left:-1px}
.pb-stop{background:var(--warn)}
.pb-ma{background:var(--ink2)}
.pb-close{top:4px;width:12px;height:12px;margin-left:-6px;border-radius:50%;background:var(--ink);box-shadow:0 0 0 2px var(--card)}
.pb-key{display:flex;flex-wrap:wrap;gap:0 12px;font-size:12px;line-height:16px;color:var(--ink2);margin-top:6px}
.k::before{content:"";display:inline-block;vertical-align:middle;margin-right:4px}
.k-stop::before{width:2px;height:12px;background:var(--warn)}
.k-ma::before{width:2px;height:12px;background:var(--ink2)}
.k-close::before{width:8px;height:8px;border-radius:50%;background:var(--ink)}
.k-band::before{width:14px;height:8px;border-radius:4px;background:var(--band)}
.vh{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.tools{display:flex;flex-wrap:wrap;gap:16px 24px;align-items:flex-end}
.seg{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:8px;min-width:0}
.seg legend{font-size:12px;line-height:16px;color:var(--ink2);padding:0;margin-bottom:6px;width:100%}
.seg label{position:relative}
.seg input{position:absolute;opacity:0;width:1px;height:1px}
.seg span{display:inline-block;font-size:14px;line-height:18px;padding:7px 14px;border:1px solid var(--ink2);border-radius:980px;cursor:pointer;white-space:nowrap}
.seg input:checked+span{background:var(--ink);color:var(--page);border-color:var(--ink)}
.seg input:focus-visible+span{outline:2px solid var(--accent);outline-offset:3px}
.fld{display:flex;flex-direction:column;gap:6px;font-size:12px;line-height:16px;color:var(--ink2)}
.fld input,.fld select{font:inherit;font-size:14px;line-height:18px;color:var(--ink);background:var(--page);border:1px solid var(--ink2);border-radius:8px;padding:7px 12px;min-width:0;letter-spacing:normal}
.fld input{width:200px;max-width:100%}
.status{font-size:14px;line-height:18px;color:var(--ink2)}
.btn{font:inherit;font-size:14px;line-height:18px;border-radius:980px;padding:8px 18px;cursor:pointer;letter-spacing:inherit}
.btn-p{background:var(--btn);color:var(--on-btn);border:1px solid var(--btn)}
.btn-p:hover{background:var(--btn-hover)}
.btn-p:active{background:var(--btn-active)}
.y{text-align:center}
.x{text-align:center;color:var(--ink2)}
details>summary{cursor:pointer;width:max-content;max-width:100%;list-style:none;font-size:17px;line-height:25px;color:var(--link);border:1px solid var(--link);border-radius:980px;padding:8px 20px}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{background:var(--btn);border-color:var(--btn);color:var(--on-btn)}
details[open]>summary{margin-bottom:24px}
.mini>summary{font-size:14px;line-height:18px;padding:6px 16px}
.list{margin:0;padding-left:20px;font-size:14px;line-height:18px;display:flex;flex-direction:column;gap:6px}
.help>*{max-width:720px}
.help ul{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:12px}
footer{border-top:1px solid var(--hairline);padding-block:24px 40px}
.nojs .needs-js{display:none}
.empty{color:var(--ink2)}
@media (max-width:1068px){
.band{padding-block:54px 61px}
h1{font-size:32px;line-height:36px}
.sub{font-size:19px;line-height:23px}
}
@media (max-width:734px){
.wrap{padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.band{padding-block:39px 43px}
h1{font-size:28px;line-height:32px}
h2{font-size:24px;line-height:28px}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}
.fld input{width:100%}
.fld{flex:1 1 100%}
.cards thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.cards table,.cards tbody,.cards tr,.cards td,.cards th{display:block}
.cards.card,.card.cards{background:none;padding:0}
.ct tbody,.wt tbody tr{background:var(--card);border-radius:16px;padding:16px;margin-bottom:16px}
.ct tbody+tbody .r1>*{border-top:0}
.ct .r1,.wt tbody tr{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px 12px}
.cards .r1>*,.wt tbody tr>*{grid-column:span 2}
.cards .r1>.pos{grid-column:span 3}
.cards td,.cards th{padding:0!important;text-align:left;min-width:0}
.cards .nm{grid-column:1/-1}
.nw{white-space:nowrap}
.cards td[data-label]::before{content:attr(data-label);display:block;font-size:12px;line-height:16px;color:var(--ink2)}
.ct .r2 td{padding-top:16px!important}
.r2 .inner{grid-template-columns:minmax(0,1fr)}
.st{font-size:14px;line-height:18px}
.st th,.st td{padding:8px 3px}
.st .lo{display:none}
.st th:not([aria-sort]) .si{display:none}
thead th{white-space:normal}
}
@media print{.topnav{position:static}.needs-js{display:none}}
'''.replace('@@LIGHT@@', _css_vars(PALETTE['light'])).replace('@@DARK@@', _css_vars(PALETTE['dark']))

HTML_HEAD = '''<!DOCTYPE html>
<html lang="zh-Hant" class="nojs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>@@TITLE@@</title>
<style>''' + HTML_CSS + '''</style>
</head>
<body>
'''

# 只用 ES5，不連任何外部資源
HTML_JS = r'''
(function () {
  var d = document;
  d.documentElement.className = d.documentElement.className.replace('nojs', 'js');
  function all(s, r) { return Array.prototype.slice.call((r || d).querySelectorAll(s)); }
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function cmp(x, y, num) {
    if (num) {
      x = parseFloat(x); y = parseFloat(y);
      if (isNaN(x)) { x = -Infinity; }
      if (isNaN(y)) { y = -Infinity; }
      return x < y ? -1 : (x > y ? 1 : 0);
    }
    return String(x).localeCompare(String(y), 'zh-Hant');
  }
  function mark(table, key, dir) {
    all('thead th', table).forEach(function (th) {
      var b = th.querySelector('.sb');
      if (b && b.getAttribute('data-key') === key && dir) { th.setAttribute('aria-sort', dir); } else { th.removeAttribute('aria-sort'); }
    });
  }
  function nextDir(table, btn) {
    var th = btn.parentNode, cur = th.getAttribute('aria-sort');
    if (cur) { return cur === 'ascending' ? 'descending' : 'ascending'; }
    return btn.getAttribute('data-num') ? 'descending' : 'ascending';
  }
  // 伺服器端已產生的列（候選標的：每檔一個 tbody；觀察名單：每檔一個 tr）
  function makeSortable(table, items, box, onSort) {
    function sort(key, dir) {
      var btn = table.querySelector('.sb[data-key="' + key + '"]');
      var num = key === 'i' || (btn && btn.getAttribute('data-num'));
      items.sort(function (a, b) {
        var r = cmp(a.getAttribute('data-' + key), b.getAttribute('data-' + key), num);
        if (dir === 'descending') { r = -r; }
        return r || (a.getAttribute('data-i') - b.getAttribute('data-i'));
      });
      var f = d.createDocumentFragment();
      items.forEach(function (x) { f.appendChild(x); });
      box.appendChild(f);
      mark(table, key, key === 'i' ? '' : dir);
      if (onSort) { onSort(key, dir); }
    }
    all('.sb', table).forEach(function (btn) {
      btn.addEventListener('click', function () { sort(btn.getAttribute('data-key'), nextDir(table, btn)); });
    });
    return sort;
  }

  // 候選標的：排序、篩選、搜尋
  var ct = d.getElementById('ct');
  if (ct) {
    var cands = all('tbody.cand', ct), sel = d.getElementById('csort'), q = d.getElementById('cq');
    var st = d.getElementById('cstatus'), none = d.getElementById('cnone');
    var sortC = makeSortable(ct, cands, ct, function (key, dir) {
      var v = key + '|' + dir;
      if (sel) { sel.value = key === 'i' ? 'i|ascending' : v; if (sel.value !== (key === 'i' ? 'i|ascending' : v)) { sel.selectedIndex = -1; } }
      say();
    });
    if (sel) {
      sel.addEventListener('change', function () { var p = sel.value.split('|'); sortC(p[0], p[1]); });
    }
    var radio = function (name) { var c = d.querySelector('input[name="' + name + '"]:checked'); return c ? c.value : 'all'; };
    var shown = cands.length;
    var say = function () {
      var o = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].text : '';
      if (st) { st.textContent = '顯示 ' + shown + ' / ' + cands.length + ' 檔' + (o ? '，' + o : ''); }
    };
    var filter = function () {
      var t = radio('ft'), k = radio('fk'), s = q ? q.value.replace(/^\s+|\s+$/g, '').toLowerCase() : '';
      shown = 0;
      cands.forEach(function (c) {
        var ok = (t === 'all' || c.getAttribute('data-type') === t) && (k === 'all' || c.getAttribute('data-kind') === k)
          && (!s || (c.getAttribute('data-code') + ' ' + c.getAttribute('data-name')).toLowerCase().indexOf(s) >= 0);
        c.hidden = !ok;
        if (ok) { shown++; }
      });
      if (none) { none.hidden = shown > 0; }
      say();
    };
    all('input[name="ft"],input[name="fk"]').forEach(function (r) { r.addEventListener('change', filter); });
    if (q) { q.addEventListener('input', filter); }
    filter();
  }

  // 觀察名單
  var wt = d.getElementById('wt');
  if (wt && wt.tBodies[0]) { makeSortable(wt, all('tr', wt.tBodies[0]), wt.tBodies[0]); }

  // 各關明細：展開時才產生表格，每次 200 檔
  var det = d.getElementById('stages-det');
  if (det) {
    var loaded = false;
    var open = function () { if (det.open && !loaded) { loaded = true; initStages(); } };
    det.addEventListener('toggle', open);
    open();
  }
  function initStages() {
    var data = JSON.parse(d.getElementById('stage-data').textContent);
    var tb = d.getElementById('st-body'), more = d.getElementById('st-more'), info = d.getElementById('st-info');
    var dq = d.getElementById('dq'), table = d.getElementById('st'), view = data, n = 0, CH = 200;
    data.forEach(function (r, i) { r.push(i); });
    function row(r) {
      var s = '<tr><th scope="row">' + esc(r[0]) + '</th><td>' + esc(r[1]) + '</td><td class="n">' + esc(r[2]) + '</td>';
      for (var k = 0; k < 6; k++) { s += (r[3] >> k) & 1 ? '<td class="y">✓</td>' : '<td class="x">✗</td>'; }
      return s + '<td class="n lo">' + r[4].toFixed(2) + '</td><td class="n lo">' + esc(r[5]) + ' → ' + esc(r[6]) + '</td></tr>';
    }
    function draw(reset) {
      if (reset) { tb.innerHTML = ''; n = 0; }
      var h = [], end = Math.min(n + CH, view.length);
      for (var i = n; i < end; i++) { h.push(row(view[i])); }
      tb.insertAdjacentHTML('beforeend', h.join(''));
      n = end;
      info.textContent = '顯示 ' + n + ' / ' + view.length + ' 檔' + (view.length < data.length ? '（共 ' + data.length + ' 檔）' : '');
      more.hidden = n >= view.length;
      more.textContent = '再顯示 ' + Math.min(CH, view.length - n) + ' 檔（還有 ' + (view.length - n) + ' 檔）';
    }
    function key(r, c) {
      if (c < 2) { return r[c]; }
      if (c === 2) { return parseFloat(r[2]); }
      if (c < 9) { return (r[3] >> (c - 3)) & 1; }
      return c === 9 ? r[4] : parseFloat(r[6]);
    }
    var sk = -1, sd = '';
    function apply() {
      var s = dq.value.replace(/^\s+|\s+$/g, '').toLowerCase();
      view = s ? data.filter(function (r) { return (r[0] + ' ' + r[1]).toLowerCase().indexOf(s) >= 0; }) : data.slice();
      if (sk >= 0) {
        var num = sk > 1;
        view.sort(function (a, b) {
          var r = cmp(key(a, sk), key(b, sk), num);
          if (sd === 'descending') { r = -r; }
          return r || (a[7] - b[7]);
        });
      }
      draw(true);
    }
    all('.sb', table).forEach(function (btn) {
      btn.addEventListener('click', function () {
        sd = nextDir(table, btn); sk = +btn.getAttribute('data-key');
        mark(table, btn.getAttribute('data-key'), sd);
        apply();
      });
    });
    dq.addEventListener('input', apply);
    more.addEventListener('click', function () { draw(false); });
    apply();
  }
})();
'''

HTML_TAIL = '<script>' + HTML_JS + '</script>\n</body>\n</html>\n'

WEEKDAYS = '一二三四五六日'
IS_ETF = re.compile(r'^00')      # 和 fetch_data.KEEP 一樣：00 開頭是 ETF


def _dow(date):
    try:
        return '（星期%s）' % WEEKDAYS[dt.date.fromisoformat(date).weekday()]
    except ValueError:
        return ''


def _a(v):
    """屬性值"""
    return html.escape(str(v), quote=True)


def _sort_th(label, key, num=False, cls=''):
    c = ' class="%s"' % cls if cls else ''
    return ('<th scope="col"%s><button type="button" class="sb" data-key="%s"%s>%s<span class="si" aria-hidden="true">'
            '</span></button></th>' % (c, _a(key), ' data-num="1"' if num else '', html.escape(label)))


def _chg(chg):
    """漲跌%：紅漲綠跌，加 ▲▼ 與正負號，不只靠顏色"""
    if chg > 0:
        return '<span class="up">▲ %+.2f%%</span>' % chg
    if chg < 0:
        return '<span class="down">▼ %+.2f%%</span>' % chg
    return '<span class="flat">平 0.00%</span>'


def _grade(dist):
    """停損距離分級：顏色之外一定附上文字（近／中／遠）"""
    if dist <= 3:
        return 'd-ok', '近'
    if dist <= 6:
        return 'd-mid', '中'
    return 'd-wide', '遠'


def _pos(r, idx, capital):
    """部位試算：回傳 (排序值, 格子內容 HTML)"""
    sz = r.get('size') or []
    if idx >= len(sz):
        return -1, '<span class="p-zero">—</span>'
    s = sz[idx]
    sh = s['shares']
    if sh <= 0:
        return 0, '<span class="p-zero">0 股（停損太遠或資金不足）</span>'
    cls, extra = '', ''
    if capital and sh >= int(capital / r['close']):
        cls, extra = 'p-cap', '（已達總資金上限）'
    elif sh < 1000:
        cls, extra = 'p-odd', '（不到 1 張，零股）'
    return sh, '<span class="%s">%s 股%s</span><small>%s 張・約 %s 元</small>' % (
        cls, format(sh, ','), '<small class="flag">%s</small>' % extra.strip('（）') if extra else '',
        format(s['lots'], 'g'), format(s['amount'], ','))


def _price_bar(r):
    """收盤、MA20、建議停損（有資料時加布林上下軌）畫在同一條刻度上；數字寫在下方圖例"""
    ma = r.get('ma') or {}
    pts = dict(stop=r['stop'], close=r['close'])
    if ma.get('ma20') is not None:
        pts['ma'] = ma['ma20']
    blo, bup = ma.get('boll_lo'), ma.get('boll_up')
    band = blo is not None and bup is not None
    vals = list(pts.values()) + ([blo, bup] if band else [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.04 or max(abs(hi) * 0.01, 0.01)
    lo, hi = lo - pad, hi + pad
    x = lambda v: round((v - lo) / (hi - lo) * 100, 1)
    a, b = sorted((pts['stop'], pts['close']))
    bar = ['<div class="pb" aria-hidden="true"><span class="pb-track"></span>']
    if band:
        bar.append('<span class="pb-band" style="left:%s%%;width:%s%%"></span>' % (x(blo), round(x(bup) - x(blo), 1)))
    bar.append('<span class="pb-risk" style="left:%s%%;width:%s%%"></span>' % (x(a), round(x(b) - x(a), 1)))
    bar.append('<span class="pb-stop" style="left:%s%%"></span>' % x(pts['stop']))
    if 'ma' in pts:
        bar.append('<span class="pb-ma" style="left:%s%%"></span>' % x(pts['ma']))
    bar.append('<span class="pb-close" style="left:%s%%"></span></div>' % x(pts['close']))
    key = ['<span class="vh">價位：</span><span class="k k-stop">停損 %s</span>' % pts['stop']]
    if 'ma' in pts:
        key.append('<span class="k k-ma">MA20 %s</span>' % pts['ma'])
    key.append('<span class="k k-close">收盤 %s</span>' % pts['close'])
    if band:
        key.append('<span class="k k-band">布林 %s～%s</span>' % (blo, bup))
    return ''.join(bar) + '<p class="pb-key">' + ''.join(key) + '</p>'


def _seg(name, legend, opts):
    """一組單選按鈕（看起來像分段按鈕）；opts：[(值, 文字)]"""
    out = ['<fieldset class="seg"><legend>%s</legend>' % html.escape(legend)]
    for i, (v, t) in enumerate(opts):
        out.append('<label><input type="radio" name="%s" value="%s"%s><span>%s</span></label>' % (
            name, _a(v), ' checked' if i == 0 else '', html.escape(t)))
    return ''.join(out) + '</fieldset>'


def _sec_summary(B, ctx):
    h = html.escape
    date, res, ok, cands, watch = ctx['date'], ctx['res'], ctx['ok'], ctx['cands'], ctx['watch']
    nb = sum(1 for r in cands if r['type'] == '突破型')
    B.append('<header class="band alt" id="top"><div class="wrap stack">')
    B.append('<div class="stack" style="gap:8px"><h1><span class="nw">台股盤後選股</span> <span class="nw">%s</span></h1><p class="sub">%s</p></div>' % (
        h(date + _dow(date)), h('收盤資料｜掃描 %d 檔（資料有效 %d 檔）' % (len(res), len(ok)))))
    if date and ctx['meta'].get('today') and date != ctx['meta']['today']:
        B.append('<p class="warn">資料日期不是今天：今天可能是週末、休市，或官方還沒公布收盤資料。</p>')
    B.append('<dl class="stats">'
             '<div><dt>候選標的</dt><dd>%d<small>檔</small></dd></div>'
             '<div><dt>突破型</dt><dd>%d<small>檔</small></dd></div>'
             '<div><dt>回測型</dt><dd>%d<small>檔</small></dd></div>'
             '<div><dt>%s</dt><dd>%d<small>檔</small></dd></div></dl>' % (
                 len(cands), nb, len(cands) - nb, h(ctx['wname']), len(watch)))
    B.append('<p class="cap14">%s</p>' % h('範圍：%s｜部位：%s' % (ctx['scope'], ctx['pos'])))
    B.append('<nav class="jump" aria-label="本頁段落"><a href="#cands">候選標的</a><a href="#watch">%s</a>'
             '<a href="#stages">各關明細</a><a href="#help">怎麼看這一頁</a></nav>' % h(ctx['wname']))
    B.append('</div></header>')


def _sec_cands(B, ctx, capital, risk_txt, risk):
    h = html.escape
    cands = ctx['cands']
    B.append('<section class="band" id="cands" aria-labelledby="cands-h"><div class="wrap stack">')
    B.append(f'<h2 id="cands-h">候選標的（{len(cands)} 檔）</h2>')
    if not cands:
        B.append('<p class="empty">今天沒有標的同時通過所有關卡。</p></div></section>')
        return
    nb = sum(1 for r in cands if r['type'] == '突破型')
    ne = sum(1 for r in cands if IS_ETF.match(r['code']))
    sorts = [('i|ascending', '預設（型態，量比大到小）'), ('vr|descending', '量比大到小'), ('vr|ascending', '量比小到大'),
             ('chg|descending', '漲跌% 大到小'), ('chg|ascending', '漲跌% 小到大'),
             ('dist|ascending', '停損距離 近到遠'), ('dist|descending', '停損距離 遠到近'),
             ('close|descending', '收盤 高到低'), ('close|ascending', '收盤 低到高'), ('code|ascending', '代號')]
    B.append('<div class="tools needs-js">'
             + _seg('ft', '型態', [('all', '全部 %d' % len(cands)), ('突破型', '突破型 %d' % nb), ('回測型', '回測型 %d' % (len(cands) - nb))])
             + _seg('fk', '類別', [('all', '全部 %d' % len(cands)), ('stock', '個股 %d' % (len(cands) - ne)), ('etf', 'ETF %d' % ne)])
             + '<label class="fld" for="cq">搜尋代號或名稱<input type="search" id="cq" autocomplete="off" placeholder="例如 2330 或 台積"></label>'
             + '<label class="fld" for="csort">排序<select id="csort">'
             + ''.join('<option value="%s">%s</option>' % (_a(v), h(t)) for v, t in sorts)
             + '</select></label></div>'
             '<p class="status needs-js" id="cstatus" role="status" aria-live="polite"></p>')
    heads = [_sort_th('標的', 'code', cls='nm'), _sort_th('收盤', 'close', True, 'n'), _sort_th('漲跌%', 'chg', True, 'n'),
             _sort_th('量比', 'vr', True, 'n'), _sort_th('建議停損', 'stop', True, 'n'), _sort_th('停損距離', 'dist', True, 'n')]
    pcols = []
    if capital:
        pcols.append((0, f'部位（風險 {risk_txt}%）'))
        if risk != 0.05:
            pcols.append((1, '部位（5%）'))
    heads += [_sort_th(t, 'p%d' % i, True, 'n') for i, t in pcols]
    ncol = 6 + len(pcols)
    B.append('<div class="card tw cards"><table class="sortable ct" id="ct"><caption class="vh">候選標的；'
             '按欄位名稱可以排序</caption><thead><tr>' + ''.join(heads) + '</tr></thead>')
    for i, r in enumerate(cands):
        dist = round((r['close'] - r['stop']) / r['close'] * 100, 1)
        dcls, word = _grade(dist)
        tcls = 't-break' if r['type'] == '突破型' else 't-pull'
        pos = [(_pos(r, j, capital), t) for j, t in pcols]
        attrs = dict(i=i, type=r['type'], kind='etf' if IS_ETF.match(r['code']) else 'stock', code=r['code'],
                     name=r['name'], close=r['close'], chg=r['chg'], vr=r['vol_ratio'], stop=r['stop'], dist=dist)
        attrs.update(('p%d' % j, p[0]) for (p, _), (j, _) in zip(pos, pcols))
        cells = ['<th scope="row" class="nm"><span class="tag %s">%s</span><br><span class="code">%s</span>'
                 '<span class="name">%s</span></th>' % (tcls, h(r['type']), h(r['code']), h(r['name'])),
                 '<td class="n" data-label="收盤">%s</td>' % h(str(r['close'])),
                 '<td class="n" data-label="漲跌%%">%s</td>' % _chg(r['chg']),
                 '<td class="n" data-label="量比">%.2f</td>' % r['vol_ratio'],
                 '<td class="n" data-label="建議停損">%s</td>' % h(str(r['stop'])),
                 '<td class="n %s" data-label="停損距離">%.1f%%<span class="g">%s</span></td>' % (dcls, dist, word)]
        cells += ['<td class="n pos" data-label="%s">%s</td>' % (_a(t), p[1]) for p, t in pos]
        B.append('<tbody class="cand" %s><tr class="r1">%s</tr><tr class="r2"><td colspan="%d"><div class="inner">'
                 '<div>%s</div><p class="desc">%s</p></div></td></tr></tbody>' % (
                     ' '.join('data-%s="%s"' % (k, _a(v)) for k, v in attrs.items()), ''.join(cells), ncol,
                     _price_bar(r), h(r['setup'] + '；停損：' + r['stop_why'])))
    B.append('</table></div><p class="empty" id="cnone" hidden>沒有符合篩選條件的標的。</p></div></section>')


def _sec_watch(B, ctx, list_mode):
    h = html.escape
    watch, meta = ctx['watch'], ctx['meta']
    B.append('<section class="band alt" id="watch" aria-labelledby="watch-h"><div class="wrap stack">')
    B.append('<div class="stack" style="gap:8px"><h2 id="watch-h">%s（%d 檔）</h2><p class="cap14">'
             '已經通過第 2 關（動能）和第 3 關（量能），但突破型／回測型的型態條件還沒成立。'
             '值得每天盯著，等型態成立再決定要不要進場。</p></div>' % (h(ctx['wtitle']), len(watch)))
    missing = meta.get('missing') or []
    if list_mode and missing:
        B.append('<p class="warn">自選股清單裡有 %d 個代號找不到資料：%s（可能打錯、已下市，或被 Excel 去掉開頭的 0）。</p>'
                 % (len(missing), h('、'.join(missing[:10]))))
    if not watch:
        B.append('<p class="empty">目前沒有。</p></div></section>')
        return
    heads = [_sort_th('標的', 'code', cls='nm'), _sort_th('收盤', 'close', True, 'n'), _sort_th('漲跌%', 'chg', True, 'n'),
             _sort_th('量比', 'vr', True, 'n'), _sort_th('0 週線', 'w0', True, 'y'), _sort_th('1 趨勢', 'w1', True, 'y'),
             _sort_th('OSC 昨→今', 'osc', True, 'n')]
    rows = []
    for i, r in enumerate(watch):
        s = r['stages']
        attrs = dict(i=i, code=r['code'], name=r['name'], close=r['close'], chg=r['chg'], vr=r['vol_ratio'],
                     w0=int(bool(s[0])), w1=int(bool(s[1])), osc=r['osc'][1])
        chk = lambda b, lab: '<td class="%s" data-label="%s">%s</td>' % ('y' if b else 'x', lab, '✓ 通過' if b else '✗ 未過')
        rows.append('<tr %s><th scope="row" class="nm"><span class="code">%s</span><span class="name">%s</span></th>'
                    '<td class="n" data-label="收盤">%s</td><td class="n" data-label="漲跌%%">%s</td>'
                    '<td class="n" data-label="量比">%.2f</td>%s%s<td class="n" data-label="OSC 昨→今">%s → %s</td></tr>' % (
                        ' '.join('data-%s="%s"' % (k, _a(v)) for k, v in attrs.items()), h(r['code']), h(r['name']),
                        h(str(r['close'])), _chg(r['chg']), r['vol_ratio'], chk(s[0], '0 週線'), chk(s[1], '1 趨勢'),
                        h(str(r['osc'][0])), h(str(r['osc'][1]))))
    B.append('<div class="card tw cards"><table class="sortable wt" id="wt"><caption class="vh">%s；按欄位名稱可以排序'
             '</caption><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div></div></section>' % (
                 h(ctx['wname']), ''.join(heads), ''.join(rows)))


def _stage_json(ok):
    """各關明細的精簡資料：[代號, 名稱, 收盤, 各關位元(0,1,2,3,A,B), 量比, OSC 昨, OSC 今]"""
    rows = []
    for r in ok:
        s = r['stages']
        bits = sum(1 << k for k, v in enumerate((s[0], s[1], s[2], s[3], s['A'], s['B'])) if v)
        rows.append([r['code'], r['name'], str(r['close']), bits, r['vol_ratio'], str(r['osc'][0]), str(r['osc'][1])])
    j = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
    return j.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')


def _sec_stages(B, ctx):
    h = html.escape
    ok, res = ctx['ok'], ctx['res']
    B.append('<section class="band" id="stages" aria-labelledby="stages-h"><div class="wrap stack">')
    B.append('<div class="stack" style="gap:8px"><h2 id="stages-h">各關明細（%d 檔）</h2><p class="cap14">'
             '每一檔通過了哪些關卡：✓ 通過、✗ 沒通過。檔數多，預設收起來，展開後每次列出 200 檔。</p></div>' % len(ok))
    if ok:
        cols = [('代號', False, ''), ('名稱', False, ''), ('收盤', True, 'n'), ('0 週線', True, 'y'), ('1 趨勢', True, 'y'),
                ('2 動能', True, 'y'), ('3 量能', True, 'y'), ('A 突破', True, 'y'), ('B 回測', True, 'y'),
                ('量比', True, 'n lo'), ('OSC 昨→今', True, 'n lo')]
        B.append('<details id="stages-det"><summary>展開各關明細（%d 檔）</summary>'
                 '<noscript><p class="cap14">這個表格需要瀏覽器開啟 JavaScript 才會列出；同資料夾的 .csv 檔有完整內容。</p></noscript>'
                 '<div class="stack needs-js"><div class="tools"><label class="fld" for="dq">搜尋代號或名稱'
                 '<input type="search" id="dq" autocomplete="off" placeholder="例如 2330"></label>'
                 '<p class="status" id="st-info" role="status" aria-live="polite"></p></div>'
                 '<div class="card tw"><table class="sortable st" id="st"><caption class="vh">各關明細；按欄位名稱可以排序</caption>'
                 '<thead><tr>%s</tr></thead><tbody id="st-body"></tbody></table></div>'
                 '<p><button type="button" class="btn btn-p" id="st-more" hidden>再顯示</button></p></div></details>'
                 '<script type="application/json" id="stage-data">%s</script>' % (
                     len(ok), ''.join(_sort_th(c, i, n, cls) for i, (c, n, cls) in enumerate(cols)), _stage_json(ok)))
    errs = [r for r in res if 'error' in r]
    if errs:
        B.append('<details class="mini"><summary>資料不足、沒有參與判斷的 %d 檔</summary><ul class="list">%s</ul></details>' % (
            len(errs), ''.join('<li>%s：%s</li>' % (h(r['code']), h(r['error'])) for r in errs)))
    notes = sorted({f"{r['code']} {n}" for r in ok for n in r['notes']})
    if notes:
        B.append('<details class="mini"><summary>備註（%d 則）</summary><ul class="list">%s</ul></details>' % (
            len(notes), ''.join('<li>%s</li>' % h(n) for n in notes)))
    B.append('</div></section>')


def _sec_help(B, ctx):
    B.append('''<section class="band alt" id="help" aria-labelledby="help-h"><div class="wrap stack help">
<h2 id="help-h">怎麼看這一頁</h2><ul>
<li>最上面是摘要：資料日期、掃描檔數、候選標的有幾檔突破型和回測型，以及這次的範圍和部位設定。</li>
<li>候選標的可以用「型態」「類別」（00 開頭的代號算 ETF）篩選，也可以搜尋代號或名稱；用「排序」選單或點表格欄位名稱排序，再點一次反過來排。</li>
<li>型態：<span class="tag t-break">突破型</span> 整理後帶量長紅突破；<span class="tag t-pull">回測型</span> 沿月線上行、量縮回測月線止跌。</li>
<li>漲跌%：依台灣習慣，<span class="up">▲ 紅色是上漲</span>、<span class="down">▼ 綠色是下跌</span>，也都會寫正負號。</li>
<li>停損距離：<span class="d-ok">近</span> 3% 以內，風險空間小；<span class="d-mid">中</span> 3%～6%，普通；<span class="d-wide">遠</span> 超過 6%，停損很遠，單筆虧損會偏大。</li>
<li>價位圖：一條刻度上畫出收盤（圓點）、MA20（灰線）、建議停損（橘線，橘色粗段是收盤到停損的距離），有資料時灰色底是布林通道上下軌；實際數字寫在圖下方。</li>
<li>部位：依「單筆虧損 = 總資金 × 比例」反推股數，1 張 = 1000 股。<span class="p-cap">紫色</span>表示已經買到總資金上限，<span class="p-odd">橘色</span>表示不到 1 張（零股）。沒設定總資金就不會有這一欄。</li>
<li>觀察名單是第二優先：動能和量能到了、型態還沒到。各關明細檔數多，預設收起來，按「展開」才列出，可以搜尋和排序。</li>
<li>頁面會跟著電腦或手機的淺色／深色設定切換；手機上表格會改成一張一張的卡片。</li>
</ul>
<p>進場後：停損跌破月線或突破長紅 K 低點；單筆虧損控制在總資金 2%～5%；停利用移動停損，跟著月線或 5 日線上移。</p>
<p class="cap14">本頁僅供程式與技術分析研究，不是投資建議，也不保證任何績效。資料可能延遲、缺漏或有誤，請以交易所公告為準。投資風險由使用者自行承擔。</p>
</div></section>''')


def render_html(res, list_mode, meta):
    """產生單一 HTML 檔（不連外部資源）。不會修改 res。

    頁面由幾個區塊函式組成（_sec_summary／_sec_cands／_sec_watch／_sec_stages／_sec_help），
    之後要做「分享版」（不含部位、觀察名單、路徑）時，只要換掉組裝的區塊與參數即可。"""
    h = html.escape
    ok = [r for r in res if 'error' not in r]
    date = max((r['date'] for r in ok), default='')
    capital, risk = meta.get('capital'), meta.get('risk') or 0.02
    title = f'台股盤後選股 {date}{_dow(date)}'
    risk_txt = format(risk * 100, 'g')
    scope = '全市場'
    if list_mode:
        scope = '自選股清單（%s，%d 檔）' % (os.path.basename(meta.get('list_path') or ''), meta.get('list_count') or 0)
    ctx = dict(res=res, ok=ok, date=date, title=title, meta=meta, scope=scope,
               cands=sorted([r for r in ok if r.get('type')], key=lambda r: (r['type'], -r['vol_ratio'])),
               watch=[r for r in ok if r['watch'] and not r.get('type')],
               wname='自選股觀察' if list_mode else '觀察名單',
               wtitle='自選股：過第 2、3 關但型態未到' if list_mode else '觀察名單',
               pos=('總資金 %s 元、每筆風險 %s%%' % (format(int(capital), ','), risk_txt)) if capital else '未設定總資金，不試算部位')
    B = ['<div class="topnav"><div class="wrap"><span class="brand">台股盤後選股</span>'
         '<span class="navdate">資料日期 %s</span></div></div><main>' % h(date + _dow(date))]
    _sec_summary(B, ctx)
    _sec_cands(B, ctx, capital, risk_txt, risk)
    _sec_watch(B, ctx, list_mode)
    _sec_stages(B, ctx)
    _sec_help(B, ctx)
    B.append('</main><footer><div class="wrap"><p class="fine">產生時間 %s｜僅供研究，非投資建議。</p>'
             '</div></footer>' % h(str(meta.get('generated') or '')))
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
    todo = [(code, path) for code, path in files(a.data) if wl is None or code in wl]
    for (code, path), (_, raw) in zip(todo, prefetch([p for _, p in todo])):
        name, bars = load_file(path, raw)
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
    todo = list(files(a.data))
    for (code, path), (_, raw) in zip(todo, prefetch([p for _, p in todo])):
        name, bars = load_file(path, raw)
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
