"""台股盤後選股：先過濾，再確認，最後挑進場點。

用法：
  python3 screener.py scan --data DIR [--config 設定.txt] [--list 自選股.csv] [--capital 1000000] [--risk 0.02]
                           [--out 結果.md] [--json 結果.json] [--txt 推播.txt]
                           [--html 結果.html] [--csv 結果.csv] [--out-dir 結果資料夾] [--open] [--share]
  python3 screener.py pool --data DIR [--out 自選股候選.csv]
  python3 screener.py add  --data DIR --bars 今日.csv
  python3 screener.py config --show 設定.txt | --write 設定.txt | --check {capital,risk,mode} | --check-watchlist 自選股.csv
                           （給 setup.bat 用；--write 讀環境變數 IN_CAPITAL、IN_RISK、IN_MODE）
  python3 screener.py share --config 設定.txt (--publish [--ask] | --confirm-first-publish 帳號/儲存庫 | --confirm-pwa 帳號/儲存庫 | --takedown [帳號/儲存庫])
                           （選用：把分享版公開到 GitHub Pages，或下架；細節見 README「分享給別人看」）

選項優先順序：命令列 > 設定.txt（--config）> 預設值。--out-dir 或設定檔的 output_dir 會一次產生
盤後選股_<日期>.html/.csv/.md/.txt/.json。--share（或設定檔 share=1）另外產生 分享版_<日期>.html
（只含候選標的，通過安全檢查才會寫出）。設定檔 share_pwa=1 時，分享版多了 PWA 與每日歷史（cache/share/history）。

結束代碼：0 成功；6 找不到股價資料；7 自選股清單有問題；8 檔案或視窗的文字編碼不對；
9 設定檔有問題；1 程式發生未預期的錯誤。
10～15 只會出現在 share 公開這一步，選股結果不受影響：10 等你確認第一次公開；11 找不到 gh；12 gh 沒登入；
13 分享版沒有通過安全檢查或沒有產生；14 GitHub 那邊出錯；15 設定或儲存庫不符合條件。

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
import argparse, csv, datetime as dt, html, io, json, math, os, pathlib, re, sys, traceback, unicodedata

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


def read_text(path, what, quiet=False):
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
    if not quiet:
        say(f'注意：{what}「{os.path.basename(path)}」是 Big5（ANSI）編碼，已經自動讀取；建議用記事本另存成 UTF-8。')
    return text


# ---------- 設定檔 ----------
CONFIG_KEYS = ('capital', 'risk', 'mode', 'watchlist', 'output_dir', 'share', 'share_publish', 'share_repo', 'share_pwa')


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


def parse_onoff(v):
    s = _unquote(v).strip().lower()
    if s == '':
        return False
    if s in ('1', 'y', 'yes', 'true', 'on', '是', '開'):
        return True
    if s in ('0', 'n', 'no', 'false', 'off', '否', '關'):
        return False
    raise ValueError('只能寫 1（開）或 0（關）')


REPO_RE = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}')


def parse_repo(v):
    s = _unquote(v).strip()
    if s == '':
        return None
    if not REPO_RE.fullmatch(s) or s.split('/', 1)[1] in ('.', '..') or s.lower().endswith('.git'):
        raise ValueError('要寫成「GitHub 帳號/儲存庫名稱」，例如 yourname/tw-stock-screener-results（只能用英文字母、數字和 - . _）')
    return s


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
               watchlist=_cfg_path('自選股.csv', base), output_dir=_cfg_path('結果', base),
               share=False, share_publish=False, share_repo=None, share_pwa=False)
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
            elif k in ('share', 'share_publish', 'share_pwa'):
                cfg[k] = parse_onoff(v)
            elif k == 'share_repo':
                cfg[k] = parse_repo(v)
            else:
                cfg[k] = _cfg_path(v, base)
        except ValueError as e:
            raise UserError(E_CONFIG, f'設定檔「{name}」第 {n} 行「{line}」不對：{e}。', FIX_CONFIG)
    return cfg


def describe_config(cfg):
    cap = '%s 元' % format(cfg['capital'], ',') if cfg['capital'] else '不設定（不試算部位）'
    scope = '全市場' if cfg['mode'] == 'full' else '自選股清單（%s）' % cfg['watchlist']
    out = [f'總資金：{cap}', f'每筆風險：{format(cfg["risk"] * 100, "g")}%', f'範圍：{scope}',
           f'結果存放：{cfg["output_dir"]}']
    if cfg.get('share') or cfg.get('share_publish'):
        out.append('分享版：每次選股會另外產生 分享版_日期.html（只含候選標的）')
    if cfg.get('share_publish'):
        out.append('公開分享版：開（%s）；第一次公開要你自己確認' % (cfg.get('share_repo') or '還沒有填 share_repo'))
    if cfg.get('share_pwa'):
        out.append('分享版加上 PWA 與每日歷史（可加到手機主畫面、離線看過的頁面；公開時多出的檔案要你自己確認）')
    return out


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


def load_list(path, quiet=False):
    if not path:
        return None
    base = os.path.basename(path)
    try:
        text = read_text(path, '自選股清單', quiet)
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


# ---------- 線圖（候選標的與觀察名單的 K 線圖資料） ----------
# 每一列的資料是 data-ch 屬性裡一串純英文字母（頁面上沒有數字，所以不會被分享版的安全檢查當成金額）；
# 圖由頁面程式在使用者打開時才畫。chart_series 只讀 bars，算法和 analyze 一致（價格一律乘 adj）。
CHART_DAYS = 120
CHART_MAX = 8000
CHART_RE = re.compile(r'[A-Za-z]{8,8000}')
SHARE_CHART_BUDGET = 290_000        # 分享版（含線圖）的位元組上限；share.MAX_BYTES 留 10 KB 的餘裕


def chart_series(bars, days=CHART_DAYS):
    """最後 min(n, days) 根的 K 線與指標。價格類乘 adj（和 analyze 的 px 同一個算法，不四捨五入）；
    量、MACD 用原值；wk = (週 DIF, 週 OSC, 週 OSC 剛翻紅)。"""
    n = len(bars)
    C = [b['close'] for b in bars]
    V = [b['volume'] for b in bars]
    adj = bars[n - 1]['raw_close'] / C[n - 1]
    ma20, ma60 = sma(C, 20), sma(C, 60)
    dif, dea, osc = macd(C)
    _, up, lo, _ = boll(C)
    mv5 = sma(V, 5)
    wd, _, wo = macd(weekly(bars))
    s = n - min(n, days)

    def px(xs):
        return [None if x is None else x * adj for x in xs[s:]]
    return dict(date=[b['date'] for b in bars[s:]], o=px([b['open'] for b in bars]), h=px([b['high'] for b in bars]),
                l=px([b['low'] for b in bars]), c=px(C), ma20=px(ma20), ma60=px(ma60), up=px(up), lo=px(lo),
                v=V[s:], mv5=mv5[s:], dif=dif[s:], dea=dea[s:], osc=osc[s:],
                wk=(wd[-1], wo[-1], wo[-1] > 0 and min(wo[-3:-1]) <= 0))


def _vi(v):
    """非負整數 → 26 進位字母，高位在前；最後一位小寫、其他大寫（解碼時看大小寫就知道一個數在哪裡結束）"""
    d = [v % 26]
    v //= 26
    while v:
        d.append(v % 26)
        v //= 26
    d.reverse()
    return ''.join(chr(65 + x) for x in d[:-1]) + chr(97 + d[-1])


def _zz(v):
    return _vi(2 * v if v >= 0 else -2 * v - 1)


def chart_code(s):
    """chart_series 的結果 → 一串英文字母（欄位順序見下面；解碼寫在 HTML_JS 的 chDec）。
    價格取 4 位有效數，MACD 取 3 位有效數，量分成 0～675 級；遇到不合理的資料丟 ValueError。"""
    n = len(s['c'])
    if not 2 <= n <= 400:
        raise ValueError('線圖：根數不合理')
    for k in ('o', 'h', 'l', 'c'):
        if len(s[k]) != n or any(not _isnum(x) or x <= 0 for x in s[k]):
            raise ValueError('線圖：價格不是正數')
    for k in ('ma20', 'ma60', 'up', 'lo', 'v', 'mv5', 'dif', 'dea', 'osc'):
        if len(s[k]) != n or any(x is not None and not _isnum(x) for x in s[k]):
            raise ValueError('線圖：數值不是有限的數字')
    if any(x < 0 for x in s['v']) or not all(_isnum(x) for x in s['wk'][:2]) or len(s['date']) != n:
        raise ValueError('線圖：數值不合理')
    e = max(-2, math.floor(math.log10(max(s['h']))) - 3)
    q = (lambda x: round(x * 10 ** -e)) if e < 0 else (lambda x: round(x / 10 ** e))

    def grp(xs, f):
        """前導缺值的個數；之後第一個值，再接逐筆差（zigzag）"""
        k = 0
        while k < n and xs[k] is None:
            k += 1
        if any(x is None for x in xs[k:]):
            raise ValueError('線圖：資料中間有缺值')
        out = [_vi(k)]
        if k < n:
            a = [f(x) for x in xs[k:]]
            out.append(_vi(a[0]))
            out.extend(_zz(y - x) for x, y in zip(a, a[1:]))
        return ''.join(out)

    def run(a):
        return _zz(a[0]) + ''.join(_zz(y - x) for x, y in zip(a, a[1:]))
    o, h, l, c = ([q(x) for x in s[k]] for k in ('o', 'h', 'l', 'c'))
    d = s['date']
    ticks = [(i, int(d[i][5:7])) for i in range(1, n) if d[i][5:7] != d[i - 1][5:7]]
    out = [_vi(2), _vi(n), _vi(e + 2), _vi(int(d[-1][5:7])), _vi(int(d[-1][8:10])), _vi(len(ticks))]
    out += [_vi(i) + _vi(m) for i, m in ticks]
    out += [_vi(c[0]), ''.join(_zz(y - x) for x, y in zip(c, c[1:]))]
    out.append(''.join(_zz(a - b) for a, b in zip(o, c)))
    out.append(''.join(_vi(max(0, a - max(b, x))) for a, b, x in zip(h, o, c)))
    out.append(''.join(_vi(max(0, min(b, x) - a)) for a, b, x in zip(l, o, c)))
    out.append(grp(s['ma20'], q))
    out.append(grp(s['ma60'], q))
    out.append(grp([None if u is None else u - m for u, m in zip(s['up'], s['ma20'])], q))
    vmax = max(s['v']) or 1
    lv = lambda x: round(x / vmax * 675)
    out.append(''.join(_vi(lv(x)) for x in s['v']))
    out.append(grp(s['mv5'], lv))
    m = max(abs(x) for k in ('dif', 'dea', 'osc') for x in s[k])
    me = -10 if m == 0 else max(-10, min(5, math.floor(math.log10(m)) - 2))
    qm = (lambda x: round(x * 10 ** -me)) if me < 0 else (lambda x: round(x / 10 ** me))
    out.append(_vi(me + 10))
    out.append(run([qm(x) for x in s['dif']]))
    out.append(run([qm(x) or (x > 0) - (x < 0) for x in s['osc']]))       # 非 0 卻量化成 0：留下正負號
    wd, wo, flip = s['wk']
    out.append(_vi((wd > 0) + 2 * (wo > 0) + 4 * bool(flip)) + _zz(round(wd * 100)) + _zz(round(wo * 100)))
    # 第 2 版加在最後：最後一天的年（減 2000）、最大量（張）、每兩個「相鄰兩根差幾天減 1」併成一個字母（都小於 5 才併，否則 z 加兩個值）
    try:
        dd = [dt.date.fromisoformat(x) for x in d]
    except (TypeError, ValueError):
        raise ValueError('線圖：日期不合理') from None
    g = [(y - x).days - 1 for x, y in zip(dd, dd[1:])]
    if any(x < 0 for x in g) or not 2000 <= dd[-1].year < 2100:
        raise ValueError('線圖：日期不合理')
    g += [0] * (len(g) % 2)
    out.append(_vi(dd[-1].year - 2000) + _vi(round(vmax / 1000)))
    out.extend(_vi(a * 5 + b) if a < 5 and b < 5 else _vi(25) + _vi(a) + _vi(b) for a, b in zip(g[::2], g[1::2]))
    code = ''.join(out)
    if len(code) > CHART_MAX:
        raise ValueError('線圖：資料太長')
    return code


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
    out = [f'盤後選股 {date}', '']
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
# 結果頁的顏色。測試會用 WCAG 公式檢查：主要與次要文字（ink、ink2）≥ 7:1、其他文字 ≥ 4.5:1；
# ink3 是有意義的框線與圖形（篩選鈕、輸入框、類型標籤的框、價位條軌道）≥ 4.5:1；hairline 只是分隔線。
# 漲跌依台灣習慣：紅漲（up）、綠跌（down），並一律加上 ▲▼ 與正負號，不只靠顏色。
PALETTE = dict(
    light=dict(page='#ffffff', surface='#f5f5f7', nav='#fafafc', ink='#1d1d1f', ink2='#4f4f53', ink3='#6e6e73',
               link='#0066cc', accent='#0071e3', btn='#0071e3', btn_hover='#0076df', btn_active='#006edb',
               on_btn='#ffffff', hairline='rgba(0,0,0,.22)', band='#bdbdc2',
               up='#a50e26', down='#0b5e28', warn='#873c00', cap='#6b2fa3'),
    dark=dict(page='#000000', surface='#1d1d1f', nav='#161617', ink='#f5f5f7', ink2='#aeaeb2', ink3='#8e8e93',
              link='#2997ff', accent='#2997ff', btn='#0071e3', btn_hover='#0076df', btn_active='#006edb',
              on_btn='#ffffff', hairline='rgba(255,255,255,.3)', band='#545458',
              up='#ff8a80', down='#30d158', warn='#ffa94d', cap='#d09cff'),
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
.navr{display:flex;align-items:center;gap:16px;min-width:0}
.theme{display:inline-flex;gap:2px;padding:2px;border:1px solid var(--ink3);border-radius:980px}
.tm{position:relative;touch-action:manipulation;font:inherit;font-size:12px;line-height:16px;letter-spacing:normal;color:var(--ink2);background:none;border:0;border-radius:980px;padding:4px 10px;min-width:44px;cursor:pointer}
.tm::after{content:"";position:absolute;left:0;right:0;top:-10px;bottom:-10px}
.tm:hover{color:var(--ink)}
.tm[aria-pressed="true"]{background:var(--ink);color:var(--page)}
.sec{display:inline-block;margin-left:8px;font-size:12px;line-height:16px;font-weight:400;color:var(--ink2);white-space:nowrap}
.kd{display:inline-block;margin-left:8px;font-size:12px;line-height:14px;font-weight:400;color:var(--ink2);border:1px solid var(--ink3);border-radius:5px;padding:0 5px;white-space:nowrap}
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
.sb{touch-action:manipulation;font:inherit;color:inherit;background:none;border:0;padding:4px 0;cursor:pointer;letter-spacing:inherit;text-align:inherit}
.sb:hover{color:var(--ink)}
.si::after{content:" \\2195"}
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
.pb-track{left:0;right:0;top:9px;height:2px;background:var(--ink3)}
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
.chb{touch-action:manipulation;font:inherit;font-size:14px;line-height:18px;letter-spacing:normal;color:var(--ink);background:none;padding:7px 14px;border:1px solid var(--ink3);border-radius:980px;cursor:pointer;white-space:nowrap}
.chb::after{content:" \\25BE"}
.chb[aria-expanded="true"]::after{content:" \\25B4"}
.chb:hover{border-color:var(--ink)}
.chb[aria-expanded="true"]{background:var(--ink);color:var(--page);border-color:var(--ink)}
.nm .chb{display:block;margin-top:8px}
.chp{margin-top:12px}
.chp svg{display:block;width:100%;height:auto}
.chk{display:flex;flex-wrap:wrap;gap:4px 16px;margin:8px 0 0;padding:0;list-style:none;font-size:12px;line-height:16px;color:var(--ink2)}
.chk li{display:flex;align-items:center}
.chk svg{width:24px;height:10px;margin-right:4px;overflow:visible}
.chz,.chi{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:8px 0}
.chi>span,.chg{font-size:12px;line-height:16px;color:var(--ink2)}
.chi>span{min-width:28px}
.chp .chip{font-size:13px;padding:6px 12px;min-height:34px}
.chp .chip:disabled{opacity:.5;cursor:default}
.cho{margin:4px 0;font-size:12px;line-height:16px;color:var(--ink);font-variant-numeric:tabular-nums;white-space:pre-wrap;overflow-wrap:anywhere}
.chq{touch-action:pan-y;-webkit-user-select:none;user-select:none;cursor:crosshair;border-radius:8px}
.chs+.chs{border-top:1px solid var(--hairline)}
.chh{display:flex;align-items:center;gap:6px;min-height:28px;font-size:12px;line-height:16px;color:var(--ink2)}
.chn{flex:none;color:var(--ink);font-weight:600;border:1px solid var(--ink3);border-radius:6px;padding:0 6px}
.chv{flex:1;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.chx{flex:none;touch-action:manipulation;font:inherit;font-size:16px;line-height:16px;color:var(--ink);background:none;min-width:32px;min-height:28px;border:1px solid var(--ink3);border-radius:8px;cursor:pointer}
.chx:hover{border-color:var(--ink)}
@media (max-width:734px){.chp .chip,.chx{min-height:44px;min-width:44px}}
.chw{margin-top:8px;font-size:12px;line-height:16px;color:var(--ink2)}
.chp .chb{margin-top:8px}
.ct .r3 td{padding-top:0}
.cx-up{fill:var(--up)}
.cx-dn{fill:var(--down)}
.cx-fl{fill:var(--ink2)}
.cx-wu,.cx-wd,.cx-wf,.cx-ma5,.cx-ma10,.cx-ma20,.cx-ma60,.cx-bb,.cx-stop,.cx-mv5,.cx-dif,.cx-dea,.cx-k,.cx-d,.cx-gd,.cx-x,.cx-grid{fill:none}
.cx-wu{stroke:var(--up)}
.cx-wd{stroke:var(--down)}
.cx-wf{stroke:var(--ink2)}
.cx-ma20{stroke:var(--link);stroke-width:1.5}
.cx-ma60{stroke:var(--cap);stroke-width:1.5;stroke-dasharray:6 3}
.cx-bb{stroke:var(--ink2);stroke-width:1;stroke-dasharray:2 3}
.cx-stop{stroke:var(--warn);stroke-width:1.5;stroke-dasharray:6 3}
.cx-mv5{stroke:var(--ink);stroke-width:1.25}
.cx-dif{stroke:var(--link);stroke-width:1.5}
.cx-dea{stroke:var(--cap);stroke-width:1.5;stroke-dasharray:4 3}
.cx-grid{stroke:var(--hairline);stroke-width:1}
.cx-ma5{stroke:var(--warn);stroke-width:1}
.cx-ma10{stroke:var(--ink);stroke-width:1.25;stroke-dasharray:8 2 2 2}
.cx-k{stroke:var(--link);stroke-width:1.5}
.cx-d{stroke:var(--cap);stroke-width:1.5;stroke-dasharray:4 3}
.cx-gd{stroke:var(--ink3);stroke-width:1;stroke-dasharray:3 3}
.cx-x{stroke:var(--ink);stroke-width:1;stroke-dasharray:3 2}
.cx-cl{fill:var(--ink);stroke:var(--card);stroke-width:2}
.cx-t,.cx-tb,.cx-ts{font-size:11px}
.cx-t{fill:var(--ink2)}
.cx-tb{fill:var(--ink);font-weight:600}
.cx-ts{fill:var(--warn)}
.vh{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.tools{display:flex;flex-wrap:wrap;gap:16px 24px;align-items:flex-end}
.seg{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:8px;min-width:0}
.seg legend{font-size:12px;line-height:16px;color:var(--ink2);padding:0;margin-bottom:6px;width:100%}
.seg label{position:relative}
.seg input{position:absolute;opacity:0;width:1px;height:1px}
.seg span{display:inline-block;font-size:14px;line-height:18px;padding:7px 14px;border:1px solid var(--ink3);border-radius:980px;cursor:pointer;white-space:nowrap;touch-action:manipulation}
.seg input:not(:checked)+span:hover{border-color:var(--ink)}
.seg input:checked+span{background:var(--ink);color:var(--page);border-color:var(--ink)}
.seg input:focus-visible+span{outline:2px solid var(--accent);outline-offset:3px}
.subs{flex:1 1 100%}
.sublg{font-size:12px;line-height:16px;color:var(--ink2);margin-bottom:-2px;width:100%}
.chip{touch-action:manipulation;font:inherit;font-size:14px;line-height:18px;letter-spacing:normal;color:var(--ink);background:none;padding:7px 14px;border:1px solid var(--ink3);border-radius:980px;cursor:pointer;white-space:nowrap}
.chip:hover{border-color:var(--ink)}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--page);border-color:var(--ink)}
.fld{display:flex;flex-direction:column;gap:6px;font-size:12px;line-height:16px;color:var(--ink2)}
.fld input,.fld select{font:inherit;font-size:14px;line-height:18px;color:var(--ink);background:var(--page);border:1px solid var(--ink3);border-radius:8px;padding:7px 12px;min-width:0;letter-spacing:normal}
.fld input{width:200px;max-width:100%}
.status{font-size:14px;line-height:18px;color:var(--ink2)}
.btn{touch-action:manipulation;font:inherit;font-size:14px;line-height:18px;border-radius:980px;padding:8px 18px;cursor:pointer;letter-spacing:inherit}
.btn-p{background:var(--btn);color:var(--on-btn);border:1px solid var(--btn)}
.btn-p:hover{background:var(--btn-hover)}
.btn-p:active{background:var(--btn-active)}
.y{text-align:center}
.x{text-align:center;color:var(--ink2)}
details>summary{cursor:pointer;touch-action:manipulation;width:max-content;max-width:100%;list-style:none;font-size:17px;line-height:25px;color:var(--link);border:1px solid var(--link);border-radius:980px;padding:8px 20px}
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
.navdate{display:none}
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
.wt tbody tr.chon{margin-bottom:0;border-radius:16px 16px 0 0}
.wt tbody tr.chr{display:block;border-radius:0 0 16px 16px;padding-top:0}
.r2 .inner{grid-template-columns:minmax(0,1fr)}
.st{font-size:14px;line-height:18px}
.st th,.st td{padding:8px 3px}
.st .lo{display:none}
.st th:not([aria-sort]) .si{display:none}
thead th{white-space:normal}
}
@media print{.topnav{position:static}.needs-js{display:none}}
'''.replace('@@LIGHT@@', _css_vars(PALETTE['light'])).replace('@@DARK@@', _css_vars(PALETTE['dark']))

# 放在 <head> 裡先跑：讀出上次選的淺色／深色，頁面畫出來之前就套用（避免閃一下）。localStorage 被擋時什麼都不做
THEME_BOOT = ("(function(){var r=document.documentElement,t='';r.className=r.className.replace('nojs','js');"
              "try{t=localStorage.getItem('twss-theme')||'';}catch(e){}"
              "if(t==='light'||t==='dark'){r.setAttribute('data-theme',t);"
              "var m=document.querySelector('meta[name=\"color-scheme\"]');if(m){m.setAttribute('content',t);}}})();")

HTML_HEAD = '''<!DOCTYPE html>
<html lang="zh-Hant" class="nojs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<script>''' + THEME_BOOT + '''</script>
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
  var TK = 'twss-theme';
  function setTheme(t, keep) {
    var r = d.documentElement, m = d.querySelector('meta[name="color-scheme"]');
    if (t !== 'light' && t !== 'dark') { t = 'auto'; }
    if (t === 'auto') { r.removeAttribute('data-theme'); } else { r.setAttribute('data-theme', t); }
    if (m) { m.setAttribute('content', t === 'auto' ? 'light dark' : t); }
    all('.tm').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-set') === t ? 'true' : 'false'); });
    if (keep) {
      try { if (t === 'auto') { localStorage.removeItem(TK); } else { localStorage.setItem(TK, t); } } catch (e) { }
    }
  }
  var t0 = 'auto';
  try { t0 = localStorage.getItem(TK) || 'auto'; } catch (e) { }
  setTheme(t0, false);
  all('.tm').forEach(function (b) { b.addEventListener('click', function () { setTheme(b.getAttribute('data-set'), true); }); });
  var COLL = null;
  try { COLL = new Intl.Collator('zh-Hant'); } catch (e) { }
  function cmp(x, y, num) {
    if (num) {
      x = parseFloat(x); y = parseFloat(y);
      if (isNaN(x)) { x = -Infinity; }
      if (isNaN(y)) { y = -Infinity; }
      return x < y ? -1 : (x > y ? 1 : 0);
    }
    return COLL ? COLL.compare(String(x), String(y)) : String(x).localeCompare(String(y), 'zh-Hant');
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

  // 線圖：每列的 data-ch 是一串英文字母（欄位順序見 screener.py 的 chart_code），第一次打開才解碼、才畫
  // 指標可以選（所有圖共用，記在 twss-ind）；可以縮放、平移；十字線讀數。MA5、MA10、KD、RSI 由頁面用解碼後的價格算（約略值）
  var chN = 0, chAll = [], chTimer = 0, CH_K = 'twss-ind';
  var CH_MAIN = ['ma5', 'ma10', 'ma20', 'ma60', 'bb'], CH_SUB = ['vol', 'kd', 'macd', 'rsi'];
  var CH_NM = { ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma60: 'MA60', bb: '布林', vol: '成交量', kd: 'KD', macd: 'MACD', rsi: 'RSI' };
  var chOn = { ma20: true, ma60: true, bb: true, vol: true, macd: true };
  (function () {
    var v = null;
    try { v = localStorage.getItem(CH_K); } catch (e) { }
    if (typeof v === 'string' && /^[a-z0-9,]*$/.test(v)) {
      chOn = {};
      v.split(',').forEach(function (k) { if (CH_NM.hasOwnProperty(k)) { chOn[k] = true; } });
    }
  })();
  function chSave() {
    var v = CH_MAIN.concat(CH_SUB).filter(function (k) { return chOn[k]; }).join(',');
    try { localStorage.setItem(CH_K, v); } catch (e) { }
  }
  function chSma(a, n) {
    var r = [], s = 0, i;
    for (i = 0; i < a.length; i++) {
      s += a[i];
      if (i >= n) { s -= a[i - n]; }
      r.push(i >= n - 1 ? s / n : null);
    }
    return r;
  }
  // KD(9,3,3)：RSV = (收 - 9 日最低) / (9 日最高 - 9 日最低) × 100（最高等於最低時當 50）；K、D 從 50 起算，各取 1/3 新值；前 w 根不畫
  function chKD(h, l, c, w) {
    var K = [], D = [], k = 50, q = 50, i, j, hh, ll, rv;
    for (i = 0; i < c.length; i++) {
      if (i < 8) { K.push(null); D.push(null); continue; }
      hh = -Infinity; ll = Infinity;
      for (j = i - 8; j <= i; j++) { if (h[j] > hh) { hh = h[j]; } if (l[j] < ll) { ll = l[j]; } }
      rv = hh > ll ? (c[i] - ll) / (hh - ll) * 100 : 50;
      k = k * 2 / 3 + rv / 3; q = q * 2 / 3 + k / 3;
      K.push(i < w ? null : k); D.push(i < w ? null : q);
    }
    return [K, D];
  }
  // RSI（Wilder）：前 n 個漲跌的平均當起點，之後 (舊 × (n - 1) + 新) / n；漲跌都是 0 時當 50；前 w 根不畫
  function chRSI(c, n, w) {
    var r = [], g = 0, s = 0, i, x;
    for (i = 0; i < c.length; i++) {
      x = i ? c[i] - c[i - 1] : 0;
      if (i <= n) {
        if (x > 0) { g += x; } else { s -= x; }
        if (i === n) { g /= n; s /= n; }
      } else {
        g = (g * (n - 1) + Math.max(x, 0)) / n; s = (s * (n - 1) + Math.max(-x, 0)) / n;
      }
      r.push(i < n || i < w ? null : (g + s > 0 ? g / (g + s) * 100 : 50));
    }
    return r;
  }
  // 可見範圍：從第 a 根開始共 m 根；最少 20 根（不到 20 根就全部），不超出資料
  function chWin(n, a, m) {
    m = Math.max(Math.min(n, 20), Math.min(n, Math.round(m)));
    return [Math.max(0, Math.min(n - m, Math.round(a))), m];
  }
  // 縮放：根數乘 f，畫面上位置 t（0 是左邊、1 是右邊）的那根留在原處
  function chZoom(n, a, m, f, t) {
    var m2 = chWin(n, 0, m * f)[1];
    return chWin(n, a + t * (m - m2), m2);
  }
  function chDec(s) {
    if (!/^[A-Za-z]+$/.test(s || '')) { return null; }
    var p = 0, bad = false, o = {}, n, t, j, ver, b;
    function u() {
      var v = 0, c;
      for (;;) {
        if (p >= s.length) { bad = true; return 0; }
        c = s.charCodeAt(p++);
        if (c < 91) { v = v * 26 + c - 65; } else { return v * 26 + c - 97; }
        if (v > Math.pow(2, 30)) { bad = true; return 0; }
      }
    }
    function z() { var v = u(); return v % 2 ? -(v + 1) / 2 : v / 2; }
    function grp(m) {
      var a = [], k = u(), x, i;
      if (k > m) { bad = true; return a; }
      for (i = 0; i < k; i++) { a.push(null); }
      if (k < m) {
        x = u();
        a.push(x);
        for (i = k + 1; i < m; i++) { x += z(); a.push(x); }
      }
      return a;
    }
    function run(m) {
      var a = [], x = z(), i;
      a.push(x);
      for (i = 1; i < m; i++) { x += z(); a.push(x); }
      return a;
    }
    ver = u(); n = u(); o.n = n; o.e = u() - 2;
    if (bad || ver !== 2 || n < 2 || n > 400) { return null; }
    o.mo = u(); o.dy = u();
    t = u();
    if (t > 40) { return null; }
    o.tk = [];
    for (j = 0; j < t; j++) { o.tk.push([u(), u()]); }
    o.c = []; o.op = []; o.h = []; o.l = []; o.up = []; o.lo = []; o.v = []; o.dea = []; o.gd = []; o.off = [];
    o.c.push(u());
    for (j = 1; j < n; j++) { o.c.push(o.c[j - 1] + z()); }
    for (j = 0; j < n; j++) { o.op.push(o.c[j] + z()); }
    for (j = 0; j < n; j++) { o.h.push(Math.max(o.op[j], o.c[j]) + u()); }
    for (j = 0; j < n; j++) { o.l.push(Math.min(o.op[j], o.c[j]) - u()); }
    o.m20 = grp(n); o.m60 = grp(n);
    b = grp(n);
    for (j = 0; j < n; j++) {
      if (o.m20[j] === null || b[j] === null) { o.up.push(null); o.lo.push(null); } else { o.up.push(o.m20[j] + b[j]); o.lo.push(o.m20[j] - b[j]); }
    }
    for (j = 0; j < n; j++) { o.v.push(u()); }
    o.mv = grp(n);
    o.me = u() - 10;
    o.dif = run(n); o.osc = run(n);
    for (j = 0; j < n; j++) { o.dea.push(o.dif[j] - o.osc[j]); }
    b = u();
    o.wk = [b % 2 === 1, Math.floor(b / 2) % 2 === 1, Math.floor(b / 4) % 2 === 1, z() / 100, z() / 100];
    o.yr = 2000 + u(); o.vk = u();
    for (j = 0; j < n - 1; j += 2) {
      t = u();
      if (t === 25) { o.gd.push(u(), u()); } else { o.gd.push(Math.floor(t / 5), t % 5); }
    }
    if (bad || p !== s.length) { return null; }
    o.off[n - 1] = 0;
    for (j = n - 2; j >= 0; j--) { o.off[j] = o.off[j + 1] + o.gd[j] + 1; }
    o.m5 = chSma(o.c, 5); o.m10 = chSma(o.c, 10);
    b = chKD(o.h, o.l, o.c, 22); o.k = b[0]; o.kd = b[1];
    o.r5 = chRSI(o.c, 5, 25); o.r10 = chRSI(o.c, 10, 40);
    return o;
  }
  function r1(x) { return Math.round(x * 10) / 10; }
  function chP(cls, dd) { return dd ? '<path class="' + cls + '" d="' + dd + '"/>' : ''; }
  function chT(cls, x, y, an, s) {
    return '<text class="' + cls + '" x="' + r1(x) + '" y="' + r1(y) + '" text-anchor="' + an + '">' + s + '</text>';
  }
  function chX(g, j) { return g.x0 + (j - g.a + 0.5) * g.slot; }
  function chLine(g, a, f) {
    var s = '', on = false, j;
    for (j = g.a; j < g.a + g.m; j++) {
      if (a[j] !== null) { s += (on ? 'L' : 'M') + r1(chX(g, j)) + ' ' + r1(f(a[j])); on = true; }
    }
    return s;
  }
  function p2(v) { return (v < 10 ? '0' : '') + v; }
  function chDate(o, i) {
    var x = new Date(o.yr, o.mo - 1, o.dy - o.off[i]);
    return p2(x.getMonth() + 1) + '/' + p2(x.getDate());
  }
  function chBar(g, x, y, hh) { return 'M' + r1(x - g.bw / 2) + ' ' + r1(y) + 'h' + r1(g.bw) + 'v' + r1(Math.max(1, hh)) + 'h' + r1(-g.bw) + 'z'; }
  function chCls(o, i) { return o.c[i] > o.op[i] ? 'u' : (o.c[i] < o.op[i] ? 'd' : 'f'); }
  function chMain(c, g) {
    var o = c.o, n = o.n, a = g.a, b = a + g.m, st = Math.pow(10, o.e), dec = Math.max(0, -o.e), i, k, x;
    var hp = g.small ? 160 : 220, ty = 8, lo = Infinity, hi = -Infinity, last = b === n, ht = ty + hp + 20;
    function one(v) { if (v !== null) { if (v < lo) { lo = v; } if (v > hi) { hi = v; } } }
    function ext(arr) { for (var j = a; j < b; j++) { one(arr[j]); } }
    var sv = c.stop === null ? null : c.stop / st, cv = c.close === null ? o.c[n - 1] : c.close / st;
    ext(o.l); ext(o.h);
    if (chOn.ma5) { ext(o.m5); }
    if (chOn.ma10) { ext(o.m10); }
    if (chOn.ma20) { ext(o.m20); }
    if (chOn.ma60) { ext(o.m60); }
    if (chOn.bb) { ext(o.up); ext(o.lo); }
    if (last) { one(cv); one(sv); }
    var pad = (hi - lo) * 0.05 || 1;
    lo -= pad; hi += pad;
    if (sv !== null && (sv < lo || sv > hi)) { sv = null; }
    function py(v) { return ty + (hi - v) / (hi - lo) * hp; }
    var wk = { u: '', d: '', f: '' }, bd = { u: '', d: '', f: '' };
    for (i = a; i < b; i++) {
      k = chCls(o, i); x = chX(g, i);
      var ya = py(Math.max(o.op[i], o.c[i])), yb = py(Math.min(o.op[i], o.c[i]));
      wk[k] += 'M' + r1(x) + ' ' + r1(py(o.h[i])) + 'V' + r1(py(o.l[i]));
      bd[k] += chBar(g, x, ya, yb - ya);
    }
    var yc = py(cv), ys = sv === null ? -99 : py(sv), cl = yc, sl = ys;
    if (sv !== null && last && Math.abs(yc - ys) < 13) { if (ys >= yc) { sl = yc + 13; } else { cl = ys + 13; } }
    if (!last) { cl = -99; }
    var grid = '', labs = '', f, gy;
    for (i = 0; i < 3; i++) {
      f = [0.1, 0.5, 0.9][i];
      gy = py(lo + (hi - lo) * f);
      grid += 'M' + g.x0 + ' ' + r1(gy) + 'H' + r1(g.x1);
      if (Math.abs(gy - cl) > 9 && Math.abs(gy - sl) > 9) { labs += chT('cx-t', g.x1 + 4, gy + 4, 'start', ((lo + (hi - lo) * f) * st).toFixed(dec)); }
    }
    var out = '<svg viewBox="0 0 ' + g.w + ' ' + ht + '" role="img" aria-labelledby="cht-' + c.id + ' chd-' + c.id + '">';
    out += '<title id="cht-' + c.id + '"></title><desc id="chd-' + c.id + '"></desc>';
    out += chP('cx-grid', grid);
    if (chOn.bb) { out += chP('cx-bb', chLine(g, o.up, py) + chLine(g, o.lo, py)); }
    out += '<g class="cg-p">' + chP('cx-wu', wk.u) + chP('cx-wd', wk.d) + chP('cx-wf', wk.f);
    out += chP('cx-up', bd.u) + chP('cx-dn', bd.d) + chP('cx-fl', bd.f) + '</g>';
    if (chOn.ma60) { out += chP('cx-ma60', chLine(g, o.m60, py)); }
    if (chOn.ma20) { out += chP('cx-ma20', chLine(g, o.m20, py)); }
    if (chOn.ma10) { out += chP('cx-ma10', chLine(g, o.m10, py)); }
    if (chOn.ma5) { out += chP('cx-ma5', chLine(g, o.m5, py)); }
    if (sv !== null) { out += chP('cx-stop', 'M' + g.x0 + ' ' + r1(ys) + 'H' + r1(g.x1)) + chT('cx-ts', g.x1 + 4, sl + 4, 'start', '停損 ' + c.stop); }
    if (last) {
      out += '<circle class="cx-cl" cx="' + r1(chX(g, n - 1)) + '" cy="' + r1(yc) + '" r="3.5"/>';
      out += chT('cx-tb', g.x1 + 4, cl + 4, 'start', '收 ' + (c.close === null ? (o.c[n - 1] * st).toFixed(dec) : c.close));
    }
    out += labs;
    var prev = -99, yd = ty + hp + 14, lc = g.x1 - 15, t, xx;
    for (i = 0; i < o.tk.length; i++) {
      t = o.tk[i];
      if (t[0] < a || t[0] >= b) { continue; }
      xx = chX(g, t[0]);
      if (xx >= 14 && xx - prev >= 28 && lc - xx >= 28) { out += chT('cx-t', xx, yd, 'middle', t[1] + '月'); prev = xx; }
    }
    out += chT('cx-t', g.x1, yd, 'end', chDate(o, b - 1));
    return out + chP('cx-x', 'M0 0') + '</svg>';
  }
  function chSub(c, g, k) {
    var o = c.o, a = g.a, b = a + g.m, h = k === 'vol' ? (g.small ? 44 : 56) : (g.small ? 56 : 72), y1 = 4 + h, i, x, mx = 1, out = '';
    var bx = { u: '', d: '', f: '' };
    function yv(v) { return y1 - v / mx * h; }
    function yp(v) { return y1 - v / 100 * h; }
    if (k === 'vol') {
      for (i = a; i < b; i++) { mx = Math.max(mx, o.v[i], o.mv[i] === null ? 0 : o.mv[i]); }
      for (i = a; i < b; i++) { x = yv(o.v[i]); bx[chCls(o, i)] += chBar(g, chX(g, i), x, y1 - x); }
      out = chP('cx-grid', 'M' + g.x0 + ' ' + y1 + 'H' + r1(g.x1)) + '<g class="cg-v">' + chP('cx-up', bx.u) + chP('cx-dn', bx.d) + chP('cx-fl', bx.f) + '</g>';
      out += chP('cx-mv5', chLine(g, o.mv, yv));
    } else if (k === 'macd') {
      for (i = a; i < b; i++) { mx = Math.max(mx, Math.abs(o.dif[i]), Math.abs(o.dea[i]), Math.abs(o.osc[i])); }
      var yz = 4 + h / 2;
      var my = function (v) { return yz - v / mx * (h / 2 - 2); };
      for (i = a; i < b; i++) {
        if (o.osc[i] !== 0) { x = my(o.osc[i]); bx[o.osc[i] > 0 ? 'u' : 'd'] += chBar(g, chX(g, i), Math.min(x, yz), Math.abs(x - yz)); }
      }
      out = chP('cx-grid', 'M' + g.x0 + ' ' + r1(yz) + 'H' + r1(g.x1)) + '<g class="cg-m">' + chP('cx-up', bx.u) + chP('cx-dn', bx.d) + '</g>';
      out += chP('cx-dea', chLine(g, o.dea, my)) + chP('cx-dif', chLine(g, o.dif, my));
    } else {
      out = chP('cx-gd', 'M' + g.x0 + ' ' + r1(yp(80)) + 'H' + r1(g.x1) + 'M' + g.x0 + ' ' + r1(yp(20)) + 'H' + r1(g.x1));
      out += chP('cx-grid', 'M' + g.x0 + ' ' + r1(yp(50)) + 'H' + r1(g.x1));
      out += chT('cx-t', g.x1 + 4, yp(80) + 4, 'start', '80') + chT('cx-t', g.x1 + 4, yp(50) + 4, 'start', '50') + chT('cx-t', g.x1 + 4, yp(20) + 4, 'start', '20');
      if (k === 'kd') { out += chP('cx-d', chLine(g, o.kd, yp)) + chP('cx-k', chLine(g, o.k, yp)); } else { out += chP('cx-d', chLine(g, o.r10, yp)) + chP('cx-k', chLine(g, o.r5, yp)); }
    }
    return '<svg viewBox="0 0 ' + g.w + ' ' + (y1 + 4) + '" aria-hidden="true">' + out + chP('cx-x', 'M0 0') + '</svg>';
  }
  function chNum(a) {
    var v = parseFloat(a);
    return isFinite(v) && v > 0 ? v : null;
  }
  function chSw(cls) {
    if (cls === 'bar') { return '<path class="cx-up" d="M2 1h8v8h-8z"/><path class="cx-dn" d="M14 1h8v8h-8z"/>'; }
    if (cls === 'dot') { return '<circle class="cx-cl" cx="12" cy="5" r="3.5"/>'; }
    return '<path class="cx-' + cls + '" d="M0 5H24"/>';
  }
  function chLegend(c) {
    var it = [['bar', 'K 棒：紅漲綠跌']];
    CH_MAIN.forEach(function (k) { if (chOn[k]) { it.push([k === 'bb' ? 'bb' : k, k === 'bb' ? '布林上下軌' : CH_NM[k]]); } });
    if (c.stop !== null) { it.push(['stop', '建議停損']); }
    it.push(['dot', '收盤']);
    if (chOn.vol) { it.push(['mv5', '5 日均量']); }
    if (chOn.kd) { it.push(['k', 'K'], ['d', 'D']); }
    if (chOn.macd) { it.push(['dif', 'DIF'], ['dea', 'DEA'], ['bar', 'MACD 柱：紅正綠負']); }
    if (chOn.rsi) { it.push(['k', 'RSI5'], ['d', 'RSI10']); }
    if (chOn.kd || chOn.rsi) { it.push(['gd', '80／20 參考線']); }
    c.lg.textContent = '';
    it.forEach(function (x) {
      var li = d.createElement('li'), sp = d.createElement('span');
      li.innerHTML = '<svg viewBox="0 0 24 10" aria-hidden="true">' + chSw(x[0]) + '</svg>';
      sp.textContent = x[1];
      li.appendChild(sp);
      c.lg.appendChild(li);
    });
  }
  function chBtn(cls, s, lab) {
    var b = d.createElement('button');
    b.type = 'button'; b.className = cls; b.textContent = s;
    if (lab) { b.setAttribute('aria-label', lab); }
    return b;
  }
  function chEl(tag, cls, s) {
    var e = d.createElement(tag);
    if (cls) { e.className = cls; }
    if (s) { e.textContent = s; }
    return e;
  }
  function chFlip(k) {
    chOn[k] = !chOn[k];
    chSave();
    chAll.forEach(function (x) {
      if (!x.o) { return; }
      x.dirty = true;
      if (x.pan && !x.pan.hidden) { chRender(x); }
    });
  }
  function chChips(c, box, keys, lab) {
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', lab);
    box.appendChild(chEl('span', '', lab));
    keys.forEach(function (k) {
      var b = chBtn('chip', CH_NM[k]);
      b.addEventListener('click', function () { chFlip(k); });
      c.cb[k] = b;
      box.appendChild(b);
    });
  }
  function chUi(c) {
    var v = c.view, z = chEl('div', 'chz'), mi = chEl('div', 'chi'), si = chEl('div', 'chi'), q = chEl('div', 'chq');
    z.setAttribute('role', 'group');
    z.setAttribute('aria-label', '顯示範圍');
    c.rb = [];
    [[60, '60 日', '顯示最近 60 日'], [120, '120 日', '顯示最近 120 日'], [0, '全部', '顯示全部']].forEach(function (p) {
      var b = chBtn('chip', p[1], p[2]);
      b._r = p[0];
      b.addEventListener('click', function () { chSet(c, p[0] ? c.o.n - p[0] : 0, p[0] || c.o.n); });
      c.rb.push(b);
      z.appendChild(b);
    });
    c.zo = chBtn('chip', '−', '縮小：看更多天');
    c.zi = chBtn('chip', '+', '放大：看更少天');
    c.zr = chBtn('chip', '重設', '重設縮放與十字線');
    c.zo.addEventListener('click', function () { chZ(c, 1.5); });
    c.zi.addEventListener('click', function () { chZ(c, 1 / 1.5); });
    c.zr.addEventListener('click', function () { c.x = -1; chSet(c, 0, c.o.n, true); });
    z.appendChild(c.zo); z.appendChild(c.zi); z.appendChild(c.zr);
    c.hint = chEl('span', 'chg');
    z.appendChild(c.hint);
    c.ro = chEl('p', 'cho');
    c.live = chEl('span', 'vh');
    c.live.setAttribute('aria-live', 'polite');
    q.tabIndex = 0;
    q.setAttribute('role', 'group');
    q.setAttribute('aria-label', 'K 線圖區：左右鍵移動十字線，加號放大，減號縮小，Esc 取消十字線');
    c.cb = {};
    c.sp = {};
    c.mh = chEl('span', 'chv');
    var mp = chEl('div', 'chs'), hh = chEl('div', 'chh');
    hh.appendChild(chEl('span', 'chn', '價格'));
    hh.appendChild(c.mh);
    c.main = chEl('div');
    mp.appendChild(hh); mp.appendChild(c.main);
    q.appendChild(mp);
    CH_SUB.forEach(function (k) {
      var p = { box: chEl('div', 'chs'), hd: chEl('div', 'chh'), val: chEl('span', 'chv'), svg: chEl('div') }, x = chBtn('chx', '−', '關閉' + CH_NM[k] + '面板');
      p.hd.appendChild(chEl('span', 'chn', CH_NM[k]));
      p.hd.appendChild(p.val);
      p.hd.appendChild(x);
      x.addEventListener('click', function () { chFlip(k); if (c.cb[k]) { c.cb[k].focus(); } });
      p.box.appendChild(p.hd); p.box.appendChild(p.svg);
      c.sp[k] = p;
      q.appendChild(p.box);
    });
    chChips(c, mi, CH_MAIN, '主圖');
    chChips(c, si, CH_SUB, '副圖');
    c.plot = q;
    v.appendChild(z); v.appendChild(c.ro); v.appendChild(q); v.appendChild(mi); v.appendChild(si); v.appendChild(c.live);
    chEvents(c);
  }
  function chGeo(c) {
    var w = c.w, x1 = w - 56;
    return { w: w, small: w < 520, x0: 4, x1: x1, a: c.a, m: c.m, slot: (x1 - 4) / c.m, bw: Math.max(1, (x1 - 4) / c.m * 0.66) };
  }
  function chRender(c) {
    var t0 = Date.now(), o = c.o, g = chGeo(c), last = o.n - 1, de = [], st = Math.pow(10, o.e);
    function fm(v) { return (v * st).toFixed(Math.max(0, -o.e)); }
    c.g = g;
    c.dirty = false;
    c.main.innerHTML = chMain(c, g);
    c.main.querySelector('title').textContent = c.code + ' ' + c.name + ' 近 ' + o.n + ' 日 K 線圖';
    de.push('最後收盤 ' + (c.close === null ? fm(o.c[last]) : c.close));
    if (c.stop !== null) { de.push('建議停損 ' + c.stop); }
    if (o.m20[last] !== null) { de.push('MA20 約 ' + fm(o.m20[last])); }
    if (o.m60[last] !== null) { de.push('MA60 約 ' + fm(o.m60[last])); }
    de.push('MACD 柱狀體' + (o.osc[last] > 0 ? '為正（紅）' : (o.osc[last] < 0 ? '為負（綠）' : '為零')));
    c.main.querySelector('desc').textContent = de.join('，');
    CH_SUB.forEach(function (k) {
      var p = c.sp[k];
      p.box.hidden = !chOn[k];
      p.svg.innerHTML = chOn[k] ? chSub(c, g, k) : '';
    });
    CH_MAIN.concat(CH_SUB).forEach(function (k) { c.cb[k].setAttribute('aria-pressed', chOn[k] ? 'true' : 'false'); });
    c.rb.forEach(function (b) {
      var m = b._r ? Math.min(b._r, o.n) : o.n;
      b.setAttribute('aria-pressed', c.m === m && c.a + c.m === o.n ? 'true' : 'false');
    });
    c.zi.disabled = c.m <= Math.min(o.n, 20);
    c.zo.disabled = c.m >= o.n;
    c.hint.textContent = '顯示 ' + c.m + '／' + o.n + ' 日（' + chDate(o, c.a) + '–' + chDate(o, c.a + c.m - 1) + '）';
    chLegend(c);
    c.xp = [];
    for (var i = 0, ps = c.plot.querySelectorAll('svg'); i < ps.length; i++) {
      c.xp.push([ps[i].querySelector('.cx-x'), +ps[i].getAttribute('viewBox').split(' ')[3]]);
    }
    if (c.x >= 0) { c.x = Math.max(c.a, Math.min(c.a + c.m - 1, c.x)); }
    chCross(c);
    chRead(c);
    c.ms = Date.now() - t0;
  }
  function chCross(c) {
    var x = c.x >= 0 ? r1(chX(c.g, c.x)) : -1;
    c.xp.forEach(function (p) { p[0].setAttribute('d', x < 0 ? 'M0 0' : 'M' + x + ' 0V' + p[1]); });
  }
  function chRead(c) {
    var o = c.o, i = c.x >= 0 ? c.x : c.a + c.m - 1, st = Math.pow(10, o.e), ms = Math.pow(10, o.me), s, mv = [];
    function fm(v) { return v === null || v === undefined ? '－' : (v * st).toFixed(Math.max(0, -o.e)); }
    function f1(v) { return v === null || v === undefined ? '－' : v.toFixed(1); }
    function f2(v) { return (v * ms).toFixed(2); }
    function vz(v) { return v === null ? '－' : String(Math.round(v / 675 * o.vk)); }
    s = chDate(o, i) + (c.x >= 0 ? '' : '（最後一根）') + '  開 ' + fm(o.op[i]) + '  高 ' + fm(o.h[i]) + '  低 ' + fm(o.l[i]) + '  收 ' + fm(o.c[i]);
    if (i > 0 && o.c[i - 1] > 0) { s += '  漲跌 ' + ((o.c[i] - o.c[i - 1]) / o.c[i - 1] * 100).toFixed(2) + '%'; }
    c.ro.textContent = s + '  量約 ' + vz(o.v[i]) + ' 張';
    if (chOn.ma5) { mv.push('MA5 ' + fm(o.m5[i])); }
    if (chOn.ma10) { mv.push('MA10 ' + fm(o.m10[i])); }
    if (chOn.ma20) { mv.push('MA20 ' + fm(o.m20[i])); }
    if (chOn.ma60) { mv.push('MA60 ' + fm(o.m60[i])); }
    if (chOn.bb) { mv.push('布林 ' + fm(o.up[i]) + '／' + fm(o.lo[i])); }
    c.mh.textContent = mv.join('  ');
    c.sp.vol.val.textContent = '量約 ' + vz(o.v[i]) + ' 張  5 日均量約 ' + vz(o.mv[i]) + ' 張';
    c.sp.kd.val.textContent = 'K ' + f1(o.k[i]) + '  D ' + f1(o.kd[i]);
    c.sp.macd.val.textContent = 'DIF ' + f2(o.dif[i]) + '  DEA ' + f2(o.dea[i]) + '  OSC ' + f2(o.osc[i]);
    c.sp.rsi.val.textContent = 'RSI5 ' + f1(o.r5[i]) + '  RSI10 ' + f1(o.r10[i]);
  }
  function chSay(c) {
    clearTimeout(c.lt);
    c.lt = setTimeout(function () { c.live.textContent = c.hint.textContent + '。' + c.ro.textContent; }, 600);
  }
  function chSet(c, a, m, force) {
    var r = chWin(c.o.n, a, m);
    c.pa = null;
    if (!force && r[0] === c.a && r[1] === c.m) { return; }
    c.a = r[0]; c.m = r[1];
    chRender(c);
  }
  function chZ(c, f) {
    var t = c.x >= 0 ? (c.x - c.a + 0.5) / c.m : 1, r = chZoom(c.o.n, c.a, c.m, f, t);
    chSet(c, r[0], r[1]);
  }
  function chLater(c, a, m) {
    c.pa = [a, m];
    if (c.raf) { return; }
    c.raf = 1;
    var go = function () { if (!c.raf) { return; } c.raf = 0; if (c.pa) { chSet(c, c.pa[0], c.pa[1]); } };
    if (window.requestAnimationFrame) { window.requestAnimationFrame(go); }
    setTimeout(go, 50);
  }
  function chIdx(c, cx) {
    var sv = c.main.querySelector('svg'), g = c.g, r, x;
    if (!sv || !g) { return -1; }
    r = sv.getBoundingClientRect();
    if (!r.width) { return -1; }
    x = (cx - r.left) * g.w / r.width;
    return Math.max(g.a, Math.min(g.a + g.m - 1, g.a + Math.floor((x - g.x0) / g.slot)));
  }
  function chPx(c) {
    var sv = c.main.querySelector('svg'), r = sv ? sv.getBoundingClientRect() : null;
    return r && r.width ? r.width / c.g.w * c.g.slot : 1;
  }
  function chAt(c, i) {
    if (i === c.x) { return; }
    c.x = i;
    chCross(c);
    chRead(c);
  }
  function chEvents(c) {
    var q = c.plot, P = {}, np = 0, gs = null;
    function two() {
      var ids = [], k;
      for (k in P) { if (P.hasOwnProperty(k)) { ids.push(P[k]); } }
      return ids.length < 2 ? null : [Math.abs(ids[0].x - ids[1].x) + Math.abs(ids[0].y - ids[1].y), (ids[0].x + ids[1].x) / 2];
    }
    function gone(e) {
      if (P[e.pointerId]) { delete P[e.pointerId]; np--; }
      if (gs && (gs.k === 'z' || gs.id === e.pointerId)) { clearTimeout(gs.tm); gs = null; }
    }
    q.addEventListener('pointerdown', function (e) {
      var t = e.target, w;
      if (e.pointerType === 'mouse' && e.button !== 0) { return; }
      while (t && t !== q) { if (t.tagName === 'BUTTON') { return; } t = t.parentNode; }
      P[e.pointerId] = { x: e.clientX, y: e.clientY };
      np++;
      if (np === 2) {
        if (gs) { clearTimeout(gs.tm); }
        w = two();
        if (w) { gs = { k: 'z', d0: Math.max(20, w[0]), a0: c.a, m0: c.m, t: (chIdx(c, w[1]) - c.a + 0.5) / c.m }; }
        return;
      }
      gs = { k: '', x0: e.clientX, a0: c.a, id: e.pointerId, px: chPx(c) };
      if (e.pointerType !== 'mouse') {
        gs.tm = setTimeout(function () { if (gs && gs.k === '') { gs.k = 'x'; chAt(c, chIdx(c, gs.x0)); } }, 350);
      }
    });
    q.addEventListener('pointermove', function (e) {
      var p = P[e.pointerId], w, r;
      if (p) { p.x = e.clientX; p.y = e.clientY; }
      if (gs && gs.k === 'z') {
        w = two();
        if (w && w[0] > 10) { r = chZoom(c.o.n, gs.a0, gs.m0, gs.d0 / w[0], gs.t); chLater(c, r[0], r[1]); }
        return;
      }
      if (gs && gs.id === e.pointerId) {
        if (gs.k === '' && Math.abs(e.clientX - gs.x0) > 8) {
          gs.k = 'p';
          clearTimeout(gs.tm);
          try { q.setPointerCapture(e.pointerId); } catch (er) { }
        }
        if (gs.k === 'p') { chLater(c, gs.a0 - (e.clientX - gs.x0) / gs.px, c.m); } else if (gs.k === 'x') { chAt(c, chIdx(c, e.clientX)); }
        return;
      }
      if (!gs && e.pointerType === 'mouse') { chAt(c, chIdx(c, e.clientX)); }
    });
    q.addEventListener('pointerup', function (e) {
      var i;
      if (gs && gs.id === e.pointerId && gs.k === '') {
        i = chIdx(c, e.clientX);
        chAt(c, e.pointerType !== 'mouse' && i === c.x ? -1 : i);
      }
      gone(e);
    });
    q.addEventListener('pointercancel', gone);
    q.addEventListener('pointerleave', function (e) {
      if (e.pointerType === 'mouse' && !gs) { chAt(c, -1); }
    });
    q.addEventListener('wheel', function (e) {
      var dx = e.deltaX, dy = e.deltaY, r, b = c.pa || [c.a, c.m];
      if (e.ctrlKey && dy) {
        e.preventDefault();
        r = chZoom(c.o.n, b[0], b[1], dy > 0 ? 1.2 : 1 / 1.2, (chIdx(c, e.clientX) - c.a + 0.5) / c.m);
        chLater(c, r[0], r[1]);
      } else if (dx && Math.abs(dx) > Math.abs(dy)) {
        e.preventDefault();
        chLater(c, b[0] + dx / chPx(c), b[1]);
      }
    }, { passive: false });
    q.addEventListener('keydown', function (e) {
      var k = e.key, i;
      if (k === 'ArrowLeft' || k === 'ArrowRight') {
        e.preventDefault();
        i = c.x < 0 ? c.a + c.m - 1 : Math.max(0, Math.min(c.o.n - 1, c.x + (k === 'ArrowLeft' ? -1 : 1)));
        if (i < c.a || i >= c.a + c.m) { c.x = i; chSet(c, i < c.a ? i : i - c.m + 1, c.m); } else { chAt(c, i); }
        chSay(c);
      } else if (k === '+' || k === '=' || k === '-' || k === '_') {
        e.preventDefault();
        chZ(c, k === '+' || k === '=' ? 1 / 1.5 : 1.5);
        chSay(c);
      } else if ((k === 'Escape' || e.keyCode === 27) && c.x >= 0) {
        e.stopPropagation();
        chAt(c, -1);
      }
    });
  }
  function chBuild(c) {
    var tr = d.createElement('tr'), td = d.createElement('td'), box = d.createElement('div');
    tr.className = c.cand ? 'r3' : 'chr';
    td.setAttribute('colspan', c.r1.children.length);
    box.className = 'chp';
    box.id = 'chp-' + c.id;
    c.view = d.createElement('div');
    c.rest = d.createElement('div');
    box.appendChild(c.view); box.appendChild(c.rest);
    td.appendChild(box); tr.appendChild(td);
    tr.hidden = true;
    if (c.cand) {
      c.host.insertBefore(tr, c.host.querySelector('.r2').nextSibling);
    } else {
      c.r1.parentNode.insertBefore(tr, c.r1.nextSibling);
      tr._r = c.r1;
    }
    box._c = c;
    c.pan = tr; c.box = box;
  }
  function chDraw(c) {
    var w = Math.max(300, Math.min(980, Math.round(c.box.clientWidth))), o = c.o, wl, cb;
    if (c.drawn && !c.dirty && Math.abs(w - c.w) <= 40) { return; }
    c.w = w;
    if (!c.drawn) {
      o = c.o = chDec(c.s);
      c.drawn = true;
      if (!o) { c.view.textContent = '這檔的線圖資料有問題，無法顯示'; return; }
      c.a = 0; c.m = o.n; c.x = -1;
      chUi(c);
      c.lg = d.createElement('ul');
      c.lg.className = 'chk';
      c.rest.appendChild(c.lg);
      wl = d.createElement('p');
      wl.className = 'chw';
      wl.textContent = '週 MACD：DIF ' + o.wk[3].toFixed(2) + (o.wk[0] ? '（零軸上）' : '（零軸下）') + '、柱 ' + o.wk[4].toFixed(2) + (o.wk[1] ? (o.wk[2] ? '（紅，剛翻紅）' : '（紅）') : '（綠）');
      c.rest.appendChild(wl);
      cb = d.createElement('button');
      cb.type = 'button';
      cb.className = 'chb';
      cb.setAttribute('aria-expanded', 'true');
      cb.setAttribute('aria-controls', 'chp-' + c.id);
      cb.textContent = '收起';
      cb.addEventListener('click', function () { chToggle(c, false); c.btn.focus(); });
      c.rest.appendChild(cb);
    }
    if (o) { chRender(c); }
  }
  function chToggle(c, force) {
    var op = c.pan ? c.pan.hidden : true;
    if (typeof force === 'boolean') { op = force; }
    if (op && !c.pan) { chBuild(c); }
    if (!c.pan) { return; }
    c.pan.hidden = !op;
    c.btn.setAttribute('aria-expanded', op ? 'true' : 'false');
    if (!c.cand) { c.r1.className = c.r1.className.replace(/\s*chon\b/, '') + (op ? ' chon' : ''); }
    if (op) { chDraw(c); }
  }
  function chInit(h) {
    var cand = h.tagName === 'TBODY', r1 = cand ? h.querySelector('.r1') : h, th = r1 ? r1.querySelector('th.nm') : null;
    var s = h.getAttribute('data-ch'), c, b;
    if (!s || !th) { return; }
    chN++;
    b = d.createElement('button');
    b.type = 'button';
    b.className = 'chb';
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-controls', 'chp-' + chN);
    b.setAttribute('aria-label', 'K 線圖：' + (h.getAttribute('data-code') || '') + ' ' + (h.getAttribute('data-name') || ''));
    b.textContent = 'K 線圖';
    th.appendChild(b);
    c = { id: chN, btn: b, host: h, r1: r1, cand: cand, s: s, code: h.getAttribute('data-code') || '', name: h.getAttribute('data-name') || '',
      close: chNum(h.getAttribute('data-close')), stop: cand ? chNum(h.getAttribute('data-stop')) : null, pan: null, drawn: false, w: 0 };
    b._c = c;
    chAll.push(c);
    b.addEventListener('click', function () { chToggle(c); });
    r1.addEventListener('click', function (e) {
      var t = e.target, sel = window.getSelection ? String(window.getSelection()) : '';
      if (sel.length) { return; }
      while (t && t !== r1) {
        if (/^(BUTTON|A|INPUT|SELECT|LABEL)$/.test(t.tagName)) { return; }
        t = t.parentNode;
      }
      chToggle(c);
    });
  }
  all('[data-ch]').forEach(chInit);
  d.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' && e.keyCode !== 27) { return; }
    var t = e.target, c = null;
    while (t && !c) { c = t._c || null; t = t.parentNode; }
    if (c && c.pan && !c.pan.hidden) { chToggle(c, false); c.btn.focus(); }
  });
  window.addEventListener('resize', function () {
    clearTimeout(chTimer);
    chTimer = setTimeout(function () { chAll.forEach(function (c) { if (c.pan && !c.pan.hidden) { chDraw(c); } }); }, 200);
  });

  // 候選標的：排序、篩選、搜尋
  var ct = d.getElementById('ct');
  if (ct) {
    var cands = all('tbody.cand', ct), sel = d.getElementById('csort'), q = d.getElementById('cq');
    var st = d.getElementById('cstatus'), none = d.getElementById('cnone'), csec = d.getElementById('csec');
    var subBox = d.getElementById('csub'), subs = subBox ? all('.chip', subBox) : [], sub = 'all';
    var lastK = 'all', isSub = function (v) { return v !== 'all' && subs.some(function (b) { return b.getAttribute('data-sub') === v; }); };
    var hasOpt = function (v) { return csec && all('option', csec).some(function (o) { return o.value === v; }); };
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
      var t = radio('ft'), k = radio('fk'), g = csec ? csec.value : 'all', s = q ? q.value.replace(/^\s+|\s+$/g, '').toLowerCase() : '';
      // ETF 子類：只在類別是 ETF 時出現；離開 ETF 就回到「全部」。和類股選單同步（選單選 ETF-債券 ＝ 按「債券」）
      if (k !== 'etf') {
        sub = 'all';
        if (lastK === 'etf' && g.indexOf('ETF') === 0) { g = 'all'; if (csec) { csec.value = 'all'; } }
      } else if (isSub(g)) { sub = g; }
      lastK = k;
      if (subBox) {
        subBox.hidden = k !== 'etf';
        subs.forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-sub') === sub ? 'true' : 'false'); });
      }
      shown = 0;
      cands.forEach(function (c) {
        var ok = (t === 'all' || c.getAttribute('data-type') === t) && (k === 'all' || c.getAttribute('data-kind') === k)
          && (g === 'all' || (g === 'none' ? !c.getAttribute('data-sec') : (c.getAttribute('data-sec') === g || c.getAttribute('data-sec').split('-')[0] === g)))
          && (sub === 'all' || c.getAttribute('data-sec') === sub)
          && (!s || (c.getAttribute('data-code') + ' ' + c.getAttribute('data-name')).toLowerCase().indexOf(s) >= 0);
        c.hidden = !ok;
        if (ok) { shown++; }
      });
      if (none) { none.hidden = shown > 0; }
      say();
    };
    all('input[name="ft"],input[name="fk"]').forEach(function (r) { r.addEventListener('change', filter); });
    if (q) { q.addEventListener('input', filter); }
    if (csec) {
      csec.addEventListener('change', function () {
        var v = csec.value;
        sub = isSub(v) ? v : 'all';
        filter();
      });
    }
    subs.forEach(function (b) {
      b.addEventListener('click', function () {
        sub = b.getAttribute('data-sub');
        if (csec) { csec.value = sub !== 'all' && hasOpt(sub) ? sub : 'all'; }
        filter();
      });
    });
    filter();
  }

  // 觀察名單
  var wt = d.getElementById('wt');
  if (wt && wt.tBodies[0]) {
    makeSortable(wt, all('tr', wt.tBodies[0]), wt.tBodies[0], function () {
      all('tr.chr', wt).forEach(function (p) { p.parentNode.insertBefore(p, p._r.nextSibling); });
    });
  }

  // 各關明細：展開時才產生表格，每次 200 檔
  var det = d.getElementById('stages-det');
  if (det) {
    var loaded = false;
    var open = function () { if (det.open && !loaded) { loaded = true; initStages(); } };
    det.addEventListener('toggle', open);
    open();
  }
  function initStages() {
    var pack = JSON.parse(d.getElementById('stage-data').textContent), data = pack.r, SEC = pack.s;
    var tb = d.getElementById('st-body'), more = d.getElementById('st-more'), info = d.getElementById('st-info');
    var dq = d.getElementById('dq'), table = d.getElementById('st'), view = data, n = 0, CH = 200;
    data.forEach(function (r, i) { r.push(i, (r[0] + ' ' + r[1]).toLowerCase()); });
    function row(r) {
      var s = '<tr><th scope="row">' + esc(r[0]) + '</th><td>' + esc(r[1]) + '</td><td class="lo">' + esc(SEC[r[7]] || '—') + '</td><td class="n">' + esc(r[2]) + '</td>';
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
      if (c === 2) { return SEC[r[7]] || ''; }
      if (c === 3) { return parseFloat(r[2]); }
      if (c < 10) { return (r[3] >> (c - 4)) & 1; }
      return c === 10 ? r[4] : parseFloat(r[6]);
    }
    var sk = -1, sd = '';
    function apply() {
      var s = dq.value.replace(/^\s+|\s+$/g, '').toLowerCase();
      view = s ? data.filter(function (r) { return r[9].indexOf(s) >= 0; }) : data.slice();
      if (sk >= 0) {
        var num = sk > 2, ks = {};
        view.forEach(function (r) { ks[r[8]] = key(r, sk); });
        view.sort(function (a, b) {
          var r = cmp(ks[a[8]], ks[b[8]], num);
          if (sd === 'descending') { r = -r; }
          return r || (a[8] - b[8]);
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

# ---------- 類股 ----------
# (大類, ((官方產業別代碼, 官方名稱), ...))，順序就是頁面上的顯示順序。2026-10-30 用證交所、櫃買中心的公司基本資料
# 和證交所 ISIN 公告查證（各代碼的公司名稱都一致）：01～12、14～18、20～33、35～38 實測；
# 19 綜合、34 電子商務、80 管理股票、91 存託憑證 當天沒有可對照的公司，名稱未實測
INDUSTRY = (
    ('科技', (('24', '半導體業'), ('25', '電腦及週邊設備業'), ('26', '光電業'), ('27', '通信網路業'), ('28', '電子零組件業'),
              ('29', '電子通路業'), ('30', '資訊服務業'), ('31', '其他電子業'), ('36', '數位雲端'), ('34', '電子商務'))),
    ('金融', (('17', '金融保險業'),)),
    ('傳產', (('01', '水泥工業'), ('02', '食品工業'), ('03', '塑膠工業'), ('04', '紡織纖維'), ('05', '電機機械'), ('06', '電器電纜'),
              ('08', '玻璃陶瓷'), ('09', '造紙工業'), ('10', '鋼鐵工業'), ('11', '橡膠工業'), ('12', '汽車工業'), ('21', '化學工業'),
              ('23', '油電燃氣業'), ('18', '貿易百貨業'), ('16', '觀光餐旅'), ('37', '運動休閒'), ('38', '居家生活'),
              ('32', '文化創意業'), ('33', '農業科技業'), ('19', '綜合'), ('20', '其他業'))),
    ('生技醫療', (('22', '生技醫療業'),)),
    ('建材營造', (('14', '建材營造業'),)),
    ('航運', (('15', '航運業'),)),
    ('綠能環保', (('35', '綠能環保'),)),
    ('管理股票', (('80', '管理股票'),)),
    ('存託憑證', (('91', '存託憑證'),)),
)
INDUSTRY_SHORT = {'電腦及週邊設備業': '電腦週邊', '觀光事業': '觀光餐旅'}
# ETF 類型，由上往下第一個符合的為準：(標籤, 代號最後一碼, 名稱關鍵字)；名稱先做 NFKC，英文不分大小寫。
# 順序：槓桿反向 > 主動式（A 主動股票、D 主動債券、T 平衡）> 債券 > 商品期貨 > 高股息 > 永續 > 主題 > 金融 > 科技
# > 海外 > 策略（動能、等權、優選、Smart 等因子選股）> 市值型：主題勝過地區（國泰北美科技 → 科技、國泰日本不動產 → 主題），地區勝過市值型（元大S&P500 → 海外），
# 所以市值型只剩台股大盤與市值指數。純英數的關鍵字前後不能緊接大寫英文字母（AI 不會配到 TAIWAN，NASDAQxT 仍配到 NASDAQ）
ETF_TYPES = (
    ('ETF-槓桿反向', 'LR', ('正2', '反1', '槓桿', '反向')),
    ('ETF-主動式', 'ADT', ('主動', '平衡')),
    ('ETF-債券', 'B', ('債',)),
    ('ETF-商品期貨', 'U', ('黃金', '原油', '石油', '期貨')),
    ('ETF-高股息', '', ('高股息', '高息', '股息', '股利', '優息', '填息', '收益')),
    ('ETF-永續', '', ('ESG', '永續', '綠能', '綠色', '淨零', '低碳', '潔淨', '公司治理')),
    ('ETF-主題', '', ('生技', '基因', '醫療', '太空', '衛星', '航太', '防衛', '航運', '不動產', '地產', 'REITS',
                     '軟體', '稀土', '支付')),
    ('ETF-金融', '', ('金融',)),
    ('ETF-科技', '', ('科技', '半導體', 'IC設計', '晶圓', 'PCB', 'AI', '5G', '通訊', '網路', '資安', '機器人', '電動車',
                     '元宇宙', '智能車', '未來車', '電池', '儲能', '數據', '電力', '算力', 'FANG', 'MAG7', '費城')),
    ('ETF-海外', '', ('美國', '美股', '北美', 'S&P', '標普', 'NASDAQ', '納斯達克', '那斯達克', '道瓊', '日本', '日經', '東證',
                     '東証', '中國', '滬深', '上證', '上証', '深100', '深証', '深證', '中証', '中證', 'A股', '恒生', '香港', '印度',
                     '越南', '歐洲', '韓國', 'KOSPI', '亞洲', '新興', '全球', '世界')),
    ('ETF-策略', '', ('動能', 'Smart', '智慧', '等權', '優選', '高價', '中小')),
    ('ETF-市值型', '', ('台灣50', '臺灣50', '台50', '中型100', '台灣加權', '臺灣加權', 'MSCI台灣', '摩台', '市值', '富櫃50',
                       '領袖50', '旗艦50', 'TOP50', '藍籌')),
)
# 關鍵字表達不了的個別代號（2026-10-30 依名稱與追蹤主題判斷）：科技股為主的高息 ETF 算科技；
# 綠能及電動車（成分以電動車、電池供應鏈為主）算科技；美國電力基建（公用事業、工程營建）算主題
ETF_OVERRIDE = {
    '00927': 'ETF-科技', '00929': 'ETF-科技', '00943': 'ETF-科技', '00946': 'ETF-科技', '00962': 'ETF-科技',
    '00896': 'ETF-科技', '009805': 'ETF-主題',
}
ETF_OTHER = 'ETF-其他'      # 不叫「ETF」：篩選選單的「ETF（全部）」值就是 ETF
SECTOR_STALE_DAYS = 14      # 類股資料比資料日期舊超過這麼多天，頁面上提醒


def trim_industry(name):
    """官方產業別名稱 -> 短名稱：先套例外表，再依序試去掉結尾的「工業」「業」（剩下至少 2 個字才去）"""
    name = INDUSTRY_SHORT.get(name, name)
    for suf in ('工業', '業'):
        if name.endswith(suf) and len(name) - len(suf) >= 2:
            return name[:-len(suf)]
    return name


def _build_sectors():
    by_code, by_name, order = {}, {}, []
    for big, subs in INDUSTRY:
        for code, official in subs:
            label = big if len(subs) == 1 else '%s-%s' % (big, trim_industry(official))
            by_code[code] = label
            by_name[trim_industry(official)] = label
            if label not in order:
                order.append(label)
    return by_code, by_name, order


SECTOR_BY_CODE, SECTOR_BY_NAME, _ORDER = _build_sectors()
ETF_LABELS = tuple(t[0] for t in ETF_TYPES) + (ETF_OTHER,)
_ETF_RX = tuple(re.compile('|'.join(r'(?<![A-Z])(?i:%s)(?![A-Z])' % re.escape(w) if w.isascii() else re.escape(w) for w in words))
                for _, _, words in ETF_TYPES)
SECTOR_ORDER = tuple(_ORDER) + ETF_LABELS
SECTOR_LABELS = frozenset(SECTOR_ORDER)


def etf_type(code, name):
    if code in ETF_OVERRIDE:
        return ETF_OVERRIDE[code]
    n = unicodedata.normalize('NFKC', name or '')
    last = (code or '')[-1:]
    for (label, letters, _), rx in zip(ETF_TYPES, _ETF_RX):
        if (last.isalpha() and last in letters) or rx.search(n):
            return label
    return ETF_OTHER


def sector_of(code, name, rows):
    """類股標籤。ETF 看代號與名稱（不看快取）；其他看快取裡的官方產業別（代碼或名稱）；查不到回傳空字串"""
    if IS_ETF.match(code or ''):
        return etf_type(code, name)
    raw = str((rows or {}).get(code) or '').strip()
    if re.fullmatch(r'[0-9]{1,2}', raw):
        return SECTOR_BY_CODE.get(raw.zfill(2), '')
    return SECTOR_BY_NAME.get(trim_industry(raw), '') if raw else ''


def attach_sectors(res, rows):
    """每筆（含資料不足的）最後加上 sector；要在 size() 之後呼叫，這樣 sector 一定是最後一個鍵"""
    for r in res:
        r['sector'] = sector_of(r.get('code'), r.get('name'), rows)


def industry_dir(data):
    """類股快取在 --data 的上一層的 cache/industry（fetch_data.py 寫的）"""
    return os.path.join(os.path.dirname(os.path.abspath(data)), 'cache', 'industry')


MARKETS = (('twse', '上市'), ('tpex', '上櫃'))


def load_market(data):
    """代號 -> 上市／上櫃：讀 --data 上一層 cache 裡最新一天的每日快照（含 ETF），再用類股快取補；只讀不寫，絕不丟例外"""
    base, out = os.path.join(os.path.dirname(os.path.abspath(data)), 'cache'), {}
    for mkt, label in MARKETS:
        try:
            days = sorted((f for f in os.listdir(os.path.join(base, mkt)) if re.fullmatch(r'[0-9]{8}\.json', f)), reverse=True)
        except OSError:
            days = []
        for f in days:
            try:
                with open(os.path.join(base, mkt, f), encoding='utf-8') as fh:
                    rows = json.load(fh).get('rows')
            except Exception:
                continue
            if isinstance(rows, list) and rows:
                for row in rows:
                    if isinstance(row, list) and row and isinstance(row[0], str):
                        out.setdefault(row[0], label)
                break
    for mkt, label in MARKETS:
        try:
            with open(os.path.join(base, 'industry', mkt + '.json'), encoding='utf-8') as fh:
                rows = json.load(fh).get('rows')
            for k in rows if isinstance(rows, dict) else ():
                if isinstance(k, str):
                    out.setdefault(k, label)
        except Exception:
            pass
    return out


def attach_markets(res, markets):
    """每筆最後（sector 之後）加上 market：上市、上櫃，查不到是空字串"""
    for r in res:
        r['market'] = (markets or {}).get(r.get('code'), '')


def load_industry(d):
    """讀上市、上櫃的類股快取，絕不丟例外。回傳 rows（代號 -> 原始產業別）、have（讀得到幾邊）、stale、fetched（較舊的那天）"""
    rows, have, stale, days = {}, 0, False, []
    for mkt in ('twse', 'tpex'):
        try:
            with open(os.path.join(d, mkt + '.json'), encoding='utf-8') as f:
                j = json.load(f)
            r, fetched = j['rows'], j.get('fetched') or ''
            if not isinstance(r, dict) or not r or (fetched and not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', str(fetched))):
                raise ValueError
        except Exception:
            stale = True
            continue
        rows.update((k, v) for k, v in r.items() if isinstance(k, str) and isinstance(v, str))
        have += 1
        stale = stale or bool(j.get('stale'))
        days.append(str(fetched))
    return dict(rows=rows, have=have, stale=stale, fetched=min(days) if days and all(days) else '')


def sector_info(ind, date):
    """類股資料的狀態：none 沒有；ok；stale 有一邊缺或更新失敗，或比資料日期舊超過 SECTOR_STALE_DAYS 天"""
    if not ind['rows']:
        return dict(status='none', fetched='')
    stale = ind['stale'] or ind['have'] < 2 or not ind['fetched']
    if not stale and date:
        try:
            stale = (dt.date.fromisoformat(date) - dt.date.fromisoformat(ind['fetched'])).days > SECTOR_STALE_DAYS
        except ValueError:
            pass
    return dict(status='stale' if stale else 'ok', fetched=ind['fetched'])


def _sec_key(label):
    return (SECTOR_ORDER.index(label), '') if label in SECTOR_ORDER else (len(SECTOR_ORDER), label)


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


def _sec_stale(sinfo):
    """類股資料不是最新時的提醒（ok／none 回傳空字串）"""
    if (sinfo or {}).get('status') != 'stale':
        return ''
    when = '（上次更新 %s）' % html.escape(str(sinfo.get('fetched') or '')) if sinfo.get('fetched') else ''
    return '<p class="cap14">類股資料未更新%s，分類可能不是最新的。</p>' % when


def _etf_subs(cands):
    """類別選 ETF 時才出現的子類按鈕列（全部 N、債券 3…，依檔數多到少）；候選裡沒有 ETF 回傳空字串"""
    cnt = {}
    for r in cands:
        if IS_ETF.match(r['code']) and r.get('sector') in ETF_LABELS:
            cnt[r['sector']] = cnt.get(r['sector'], 0) + 1
    if not cnt:
        return ''
    order = sorted(cnt, key=lambda l: (-cnt[l], ETF_LABELS.index(l)))
    btn = '<button type="button" class="chip" data-sub="%s" aria-pressed="%s">%s</button>'
    out = [btn % ('all', 'true', html.escape('全部 %d' % sum(cnt.values())))]
    out += [btn % (_a(l), 'false', html.escape('%s %d' % (l[4:], cnt[l]))) for l in order]
    return ('<div class="seg subs" id="csub" role="group" aria-label="ETF 子類" hidden>'
            '<p class="sublg" aria-hidden="true">ETF 子類</p>%s</div>' % ''.join(out))


def _sector_ui(cands, sinfo):
    """候選標的的類股統計列與選單：沒有類股資料、或一檔都沒分到類時回傳 ('', '')"""
    if (sinfo or {}).get('status', 'none') == 'none' or not any(r.get('sector') for r in cands):
        return '', ''
    h = html.escape
    cnt = {}
    for r in cands:
        cnt[r.get('sector') or ''] = cnt.get(r.get('sector') or '', 0) + 1
    labels = sorted((l for l in cnt if l), key=_sec_key)
    strip = sorted(labels, key=lambda l: (-cnt[l], _sec_key(l)))
    parts = ['%s %d 檔' % (l, cnt[l]) for l in strip] + (['未分類 %d 檔' % cnt['']] if cnt.get('') else [])
    cap = '<p class="cap14">%s</p>' % h('類股：' + '・'.join(parts))
    opts = ['<option value="all">%s</option>' % h('全部 %d' % len(cands))]
    groups = []
    for l in labels:
        g = l.split('-')[0]
        if groups and groups[-1][0] == g:
            groups[-1][1].append(l)
        else:
            groups.append((g, [l]))
    for g, ls in groups:
        if len(ls) > 1:
            opts.append('<option value="%s">%s</option>' % (_a(g), h('%s（全部）%d' % (g, sum(cnt[l] for l in ls)))))
        opts += ['<option value="%s">%s</option>' % (_a(l), h('%s %d' % (l, cnt[l]))) for l in ls]
    if cnt.get(''):
        opts.append('<option value="none">%s</option>' % h('未分類 %d' % cnt['']))
    sel = '<label class="fld" for="csec">類股<select id="csec">%s</select></label>' % ''.join(opts)
    return cap, sel


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
    B.extend(x for x in (_sec_stale(ctx.get('sinfo')),) if x)
    B.append('<nav class="jump" aria-label="本頁段落"><a href="#cands">候選標的</a><a href="#watch">%s</a>'
             '<a href="#stages">各關明細</a><a href="#help">怎麼看這一頁</a></nav>' % h(ctx['wname']))
    B.append('</div></header>')


def _sec_tag(r):
    kd = ('ETF' if IS_ETF.match(r.get('code') or '') else '個股') + ('·' + r['market'] if r.get('market') else '')
    return ('<span class="sec">%s</span>' % html.escape(r['sector']) if r.get('sector') else '') + '<span class="kd">%s</span>' % html.escape(kd)


def _sec_cands(B, ctx, capital, risk_txt, risk, placeholder='例如 2330 或 台積'):
    h = html.escape
    cands = ctx['cands']
    B.append('<section class="band" id="cands" aria-labelledby="cands-h"><div class="wrap stack">')
    B.append(f'<h2 id="cands-h">候選標的（{len(cands)} 檔）</h2>')
    if not cands:
        B.append('<p class="empty">今天沒有標的同時通過所有關卡。</p></div></section>')
        return
    nb = sum(1 for r in cands if r['type'] == '突破型')
    ne = sum(1 for r in cands if IS_ETF.match(r['code']))
    sec_cap, sec_sel = _sector_ui(cands, ctx.get('sinfo'))
    B.extend(x for x in (sec_cap,) if x)
    sorts = [('i|ascending', '預設（型態，量比大到小）'), ('vr|descending', '量比大到小'), ('vr|ascending', '量比小到大'),
             ('chg|descending', '漲跌% 大到小'), ('chg|ascending', '漲跌% 小到大'),
             ('dist|ascending', '停損距離 近到遠'), ('dist|descending', '停損距離 遠到近'),
             ('close|descending', '收盤 高到低'), ('close|ascending', '收盤 低到高'), ('code|ascending', '代號')]
    B.append('<div class="tools needs-js">'
             + _seg('ft', '型態', [('all', '全部 %d' % len(cands)), ('突破型', '突破型 %d' % nb), ('回測型', '回測型 %d' % (len(cands) - nb))])
             + _seg('fk', '類別', [('all', '全部 %d' % len(cands)), ('stock', '個股 %d' % (len(cands) - ne)), ('etf', 'ETF %d' % ne)])
             + _etf_subs(cands)
             + sec_sel
             + '<label class="fld" for="cq">搜尋代號或名稱<input type="search" id="cq" autocomplete="off" placeholder="%s"></label>' % _a(placeholder)
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
        attrs['sec'] = r.get('sector') or ''
        attrs['mkt'] = r.get('market') or ''
        if (ctx.get('charts') or {}).get(r['code']):
            attrs['ch'] = ctx['charts'][r['code']]
        cells = ['<th scope="row" class="nm"><span class="tag %s">%s</span><br><span class="code">%s</span>'
                 '<span class="name">%s</span>%s</th>' % (tcls, h(r['type']), h(r['code']), h(r['name']), _sec_tag(r)),
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
        if (ctx['charts'] or {}).get(r['code']):
            attrs['ch'] = ctx['charts'][r['code']]
        chk = lambda b, lab: '<td class="%s" data-label="%s">%s</td>' % ('y' if b else 'x', lab, '✓ 通過' if b else '✗ 未過')
        rows.append('<tr %s><th scope="row" class="nm"><span class="code">%s</span><span class="name">%s</span>%s</th>'
                    '<td class="n" data-label="收盤">%s</td><td class="n" data-label="漲跌%%">%s</td>'
                    '<td class="n" data-label="量比">%.2f</td>%s%s<td class="n" data-label="OSC 昨→今">%s → %s</td></tr>' % (
                        ' '.join('data-%s="%s"' % (k, _a(v)) for k, v in attrs.items()), h(r['code']), h(r['name']), _sec_tag(r),
                        h(str(r['close'])), _chg(r['chg']), r['vol_ratio'], chk(s[0], '0 週線'), chk(s[1], '1 趨勢'),
                        h(str(r['osc'][0])), h(str(r['osc'][1]))))
    B.append('<div class="card tw cards"><table class="sortable wt" id="wt"><caption class="vh">%s；按欄位名稱可以排序'
             '</caption><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div></div></section>' % (
                 h(ctx['wname']), ''.join(heads), ''.join(rows)))


def _stage_json(ok):
    """各關明細的精簡資料：{"s": ["", 類股標籤…], "r": [[代號, 名稱, 收盤, 各關位元(0,1,2,3,A,B), 量比, OSC 昨, OSC 今, 類股編號]]}"""
    labels = sorted({r.get('sector') for r in ok if r.get('sector')}, key=_sec_key)
    idx = {l: i + 1 for i, l in enumerate(labels)}
    rows = []
    for r in ok:
        s = r['stages']
        bits = sum(1 << k for k, v in enumerate((s[0], s[1], s[2], s[3], s['A'], s['B'])) if v)
        rows.append([r['code'], r['name'], str(r['close']), bits, r['vol_ratio'], str(r['osc'][0]), str(r['osc'][1]),
                     idx.get(r.get('sector'), 0)])
    j = json.dumps({'s': [''] + labels, 'r': rows}, ensure_ascii=False, separators=(',', ':'))
    return j.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')


def _sec_stages(B, ctx):
    h = html.escape
    ok, res = ctx['ok'], ctx['res']
    B.append('<section class="band" id="stages" aria-labelledby="stages-h"><div class="wrap stack">')
    B.append('<div class="stack" style="gap:8px"><h2 id="stages-h">各關明細（%d 檔）</h2><p class="cap14">'
             '每一檔通過了哪些關卡：✓ 通過、✗ 沒通過。檔數多，預設收起來，展開後每次列出 200 檔。</p></div>' % len(ok))
    if ok:
        cols = [('代號', False, ''), ('名稱', False, ''), ('類股', False, 'lo'), ('收盤', True, 'n'), ('0 週線', True, 'y'), ('1 趨勢', True, 'y'),
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
<li>K 線圖：候選標的和觀察名單的每一列都有「K 線圖」按鈕（點那一列也可以），展開近 120 個交易日的日 K 線，附 MA20、MA60、布林上下軌、成交量與 MACD；候選標的另外畫出建議停損線。圖上的價格是約略值，精確數字看表格。按 Esc 或「收起」關閉。</li>
<li>K 線圖的指標與縮放：圖下方的「主圖」「副圖」按鈕選要看的指標（MA5、MA10、MA20、MA60、布林；成交量、KD、MACD、RSI），副圖標題的「−」可以關掉該面板，選擇會記在這個瀏覽器。「60 日」「120 日」「全部」「−」「+」「重設」調整顯示範圍；電腦可用 Ctrl＋滾輪縮放、拖曳平移，手機兩指縮放、單指左右拖曳。滑鼠移到圖上或手機點一下會顯示十字線與當天數值；圖有焦點時可用 ← → 移動、+ − 縮放、Esc 取消。MA5、MA10、KD、RSI 由頁面依圖上的約略價格計算，可能和看盤軟體差一點。</li>
<li>部位：依「單筆虧損 = 總資金 × 比例」反推股數，1 張 = 1000 股。<span class="p-cap">紫色</span>表示已經買到總資金上限，<span class="p-odd">橘色</span>表示不到 1 張（零股）。沒設定總資金就不會有這一欄。</li>
<li>觀察名單是第二優先：動能和量能到了、型態還沒到。各關明細檔數多，預設收起來，按「展開」才列出，可以搜尋和排序。</li>
<li>類股：官方的產業別，例如「科技-半導體」「航運」；ETF 依代號最後一碼與名稱分成主動式、高股息、科技、海外、市值型、債券等；旁邊的小框寫個股或 ETF 與上市／上櫃；候選標的可以用「類股」選單篩選。查不到的標示為未分類（各關明細寫「—」）。</li>
<li>頁面右上角可切換自動（跟著電腦或手機）／淺色／深色，選擇記在這個瀏覽器；無痕或封鎖網站資料時只在這次有效。手機上表格會改成一張一張的卡片。</li>
</ul>
<p>進場後：停損跌破月線或突破長紅 K 低點；單筆虧損控制在總資金 2%～5%；停利用移動停損，跟著月線或 5 日線上移。</p>
<p class="cap14">本頁僅供程式與技術分析研究，不是投資建議，也不保證任何績效。資料可能延遲、缺漏或有誤，請以交易所公告為準。投資風險由使用者自行承擔。</p>
</div></section>''')


def _topnav(date):
    return ('<div class="topnav"><div class="wrap"><span class="brand">台股盤後選股</span>'
            '<div class="navr"><span class="navdate">資料日期 %s</span>'
            '<div class="theme needs-js" role="group" aria-label="頁面顏色">'
            '<button type="button" class="tm" data-set="auto" aria-pressed="true">自動</button>'
            '<button type="button" class="tm" data-set="light" aria-pressed="false">淺色</button>'
            '<button type="button" class="tm" data-set="dark" aria-pressed="false">深色</button>'
            '</div></div></div></div>' % html.escape(date + _dow(date)))


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
               sinfo=meta.get('sector_info') or {'status': 'none', 'fetched': ''}, charts=meta.get('charts') or {},
               wname='自選股觀察' if list_mode else '觀察名單',
               wtitle='自選股：過第 2、3 關但型態未到' if list_mode else '觀察名單',
               pos=('總資金 %s 元、每筆風險 %s%%' % (format(int(capital), ','), risk_txt)) if capital else '未設定總資金，不試算部位')
    B = [_topnav(date) + '<main>']
    _sec_summary(B, ctx)
    _sec_cands(B, ctx, capital, risk_txt, risk)
    _sec_watch(B, ctx, list_mode)
    _sec_stages(B, ctx)
    _sec_help(B, ctx)
    B.append('</main><footer><div class="wrap"><p class="fine">產生時間 %s｜僅供研究，非投資建議。</p>'
             '</div></footer>' % h(str(meta.get('generated') or '')))
    return HTML_HEAD.replace('@@TITLE@@', h(title)) + '\n'.join(B) + '\n' + HTML_TAIL


# ---------- 分享版（只含候選標的；可選擇公開到 GitHub Pages） ----------
# 只複製這幾個欄位：新增欄位不會自動出現在分享版裡（寧可少放，不要多放）
SHARE_FIELDS = ('code', 'name', 'type', 'close', 'chg', 'vol_ratio', 'stop', 'setup', 'stop_why', 'sector', 'market')
SHARE_OPTIONAL = ('sector', 'market')        # 舊資料沒有這些欄位時當作空字串
SHARE_MA = ('ma20', 'boll_up', 'boll_lo')
SHARE_CODE_RE = re.compile(r'[0-9]{4}[0-9A-Z]{0,2}')
SHARE_HEAD = HTML_HEAD.replace('<title>', '<meta name="robots" content="noindex, nofollow">\n<title>', 1)
_cut = HTML_JS.index('\n  // 觀察名單\n')
# 只留「候選標的」的那一段（排序、篩選、搜尋）；整行註解拿掉
SHARE_JS = '\n'.join(x for x in HTML_JS[:_cut].split('\n') if not x.lstrip().startswith('//')) + '\n})();\n'


# ---------- 分享版的 PWA 與每日歷史（設定檔 share_pwa=1 才會用到；只附加，不改 SHARE_JS／HTML_CSS） ----------
PWA_CSS = '''.hbar{display:flex;flex-direction:column;gap:12px}
.hrow{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.hrow .chip{min-width:44px;min-height:44px}
.hrow .chip:disabled{opacity:.4;cursor:default}
.bdg{display:inline-block;margin-left:8px;font-size:12px;line-height:16px;font-weight:400;border:1px solid currentColor;border-radius:5px;padding:0 5px;white-space:nowrap}
.bd-new{color:var(--link)}
.bd-old{color:var(--ink2)}
.hgone{display:flex;flex-direction:column;gap:8px;align-items:flex-start}
.htrend svg{display:block;width:100%;height:auto}
.hb{cursor:pointer}
.hb-p{fill:var(--ink3)}
.hb-b{fill:var(--up)}
.hb-t{fill:transparent}
.hb-s{fill:none;stroke:var(--ink);stroke-width:3}
.hb-ax{stroke:var(--hairline);stroke-width:1}
.hl{font-size:22px;fill:var(--ink2)}
.a2hs{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;font-size:14px;line-height:18px;border:1px solid var(--ink3);border-radius:12px;padding:12px 16px}
.bnav{display:none}
.topnav{padding-top:env(safe-area-inset-top,0px)}
:root{@@GLASS_LIGHT@@}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){@@GLASS_DARK@@}}
:root[data-theme="dark"]{@@GLASS_DARK@@}
@media (max-width:734px){
.bnav{display:flex;position:fixed;left:max(12px,env(safe-area-inset-left,0px));right:max(12px,env(safe-area-inset-right,0px));bottom:calc(8px + env(safe-area-inset-bottom,0px));z-index:6;padding:4px;border-radius:999px;border:1px solid var(--glass-edge);background:linear-gradient(180deg,var(--glass-sheen),rgba(255, 255, 255, 0) 60%),var(--glass);-webkit-backdrop-filter:blur(20px) saturate(180%);backdrop-filter:blur(20px) saturate(180%);box-shadow:inset 0 1px 0 var(--glass-hi),0 8px 24px var(--glass-shadow)}
.bnav::before{content:"";position:absolute;top:4px;bottom:4px;left:4px;width:calc((100% - 8px) / 3);border-radius:999px;background:var(--lens);border:1px solid var(--lens-edge);box-shadow:inset 0 1px 0 var(--glass-hi);opacity:0;pointer-events:none}
.bnav.on::before{opacity:1}
.bnav.at1::before{transform:translateX(100%)}
.bnav.at2::before{transform:translateX(200%)}
.bnav a{position:relative;z-index:1;flex:1 1 0;min-height:44px;display:flex;align-items:center;justify-content:center;border-radius:999px;font-size:14px;line-height:18px;color:var(--ink);touch-action:manipulation}
.bnav a[aria-current]{font-weight:600}
.bnav a:hover{text-decoration:none}
.bnav a:active{background:var(--lens)}
.hrow{flex-wrap:nowrap}
.hrow .fld{flex:1 1 0;min-width:0}
.hrow .fld select{width:100%}
body{padding-bottom:calc(68px + env(safe-area-inset-bottom,0px))}
}
@media (prefers-reduced-motion:no-preference){.bnav::before{transition:transform 160ms ease-out}}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.bnav{background:var(--nav);border-color:var(--ink3)}}
@media (prefers-reduced-transparency:reduce){.bnav{background:var(--nav);border-color:var(--ink3);-webkit-backdrop-filter:none;backdrop-filter:none}.bnav::before{background:var(--surface);border-color:var(--ink3)}}
@media (forced-colors:active){.bnav{background:Canvas;border:1px solid CanvasText;box-shadow:none;-webkit-backdrop-filter:none;backdrop-filter:none}.bnav::before{display:none}.bnav a[aria-current]{border:2px solid CanvasText}}
@media (display-mode:standalone){.a2hs{display:none}}
'''
# 底部快速切換列的玻璃色（r, g, b, 透明度）。測試會把它疊在最極端的底色（含純黑、純白）上算 WCAG 對比，字要 ≥ 4.5:1
GLASS = dict(
    light=dict(glass=(250, 250, 252, .85), sheen=(255, 255, 255, .5), lens=(255, 255, 255, .7), lens_edge=(0, 0, 0, .12),
               glass_edge=(255, 255, 255, .7), glass_hi=(255, 255, 255, .8), glass_shadow=(0, 0, 0, .16)),
    dark=dict(glass=(22, 22, 23, .85), sheen=(255, 255, 255, .06), lens=(255, 255, 255, .14), lens_edge=(255, 255, 255, .22),
              glass_edge=(255, 255, 255, .16), glass_hi=(255, 255, 255, .14), glass_shadow=(0, 0, 0, .5)),
)


def _glass_vars(d):
    return ';'.join('--%s:rgba(%d, %d, %d, %s)' % ((k.replace('_', '-'),) + v[:3] + (('%g' % v[3]).lstrip('0'),))
                    for k, v in d.items())


PWA_CSS = PWA_CSS.replace('@@GLASS_LIGHT@@', _glass_vars(GLASS['light'])).replace('@@GLASS_DARK@@', _glass_vars(GLASS['dark']))

# 全部包在 try 裡，出錯也不影響 SHARE_JS；只用 ES5；不用註解、不寫網址、不用 data／mode 當名稱（會碰到洩漏檢查的規則）
PWA_JS = r'''
(function () {
  try {
    var d = document, loc = location, web = /^https?:$/.test(loc.protocol);
    function el(i) { return d.getElementById(i); }
    function mk(tag, cls, txt) {
      var e = d.createElement(tag);
      if (cls) { e.className = cls; }
      if (txt !== undefined) { e.textContent = txt; }
      return e;
    }
    function ymd(s) { return s.replace(/-/g, ''); }
    if ('serviceWorker' in navigator && web) {
      window.addEventListener('load', function () {
        try { navigator.serviceWorker.register('sw.js').then(null, function () { }); } catch (e) { }
      });
    }
    var ah = el('a2hs');
    if (ah && web && window.matchMedia) {
      var narrow = matchMedia('(max-width: 734px)').matches;
      var standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
      var seen = false;
      try { seen = localStorage.getItem('twss-a2hs') === '1'; } catch (e) { }
      if (narrow && !standalone && !seen) {
        ah.appendChild(mk('span', '', '把這一頁加到主畫面，之後像 App 一樣開啟（瀏覽器選單裡的「加入主畫面」；iPhone 請用 Safari 的分享選單）。'));
        var ins = mk('button', 'chip', '安裝'), x = mk('button', 'chip', '知道了'), ev0 = null;
        ins.type = 'button'; x.type = 'button'; ins.hidden = true;
        window.addEventListener('beforeinstallprompt', function (ev) { ev.preventDefault(); ev0 = ev; ins.hidden = false; });
        ins.addEventListener('click', function () { if (ev0) { ev0.prompt(); } });
        x.addEventListener('click', function () {
          ah.hidden = true;
          try { localStorage.setItem('twss-a2hs', '1'); } catch (e) { }
        });
        ah.appendChild(ins); ah.appendChild(x);
        ah.hidden = false;
      }
    }
    try {
      var bn = el('bnav'), bl = bn ? bn.querySelectorAll('a') : [], bt = [], bk = -1, bq = false, bi;
      for (bi = 0; bi < bl.length; bi++) { bt.push(el(bl[bi].getAttribute('href').slice(1))); }
      var spot = function () {
        var k = 0, lim = (window.innerHeight || 0) * 0.4, de = d.documentElement, j;
        bq = false;
        for (j = 0; j < bt.length; j++) { if (bt[j] && bt[j].getBoundingClientRect().top <= lim) { k = j; } }
        if (bt.length && window.innerHeight + (window.pageYOffset || de.scrollTop) >= de.scrollHeight - 2) { k = bt.length - 1; }
        if (k === bk || !bl.length) { return; }
        bk = k;
        for (j = 0; j < bl.length; j++) {
          if (j === k) { bl[j].setAttribute('aria-current', 'location'); } else { bl[j].removeAttribute('aria-current'); }
        }
        bn.className = 'bnav on at' + k;
      };
      if (bl.length) {
        window.addEventListener('scroll', function () {
          if (bq) { return; }
          bq = true;
          if (window.requestAnimationFrame) { requestAnimationFrame(spot); } else { setTimeout(spot, 50); }
        }, { passive: true });
        window.addEventListener('resize', spot);
        spot();
      }
    } catch (e) { }
    var bar = el('hbar');
    if (!bar || !window.fetch || !web) { return; }
    var TODAY = bar.getAttribute('data-day') || '', TC = ymd(TODAY);
    var h2 = el('cands-h'), view = el('hview'), trend = el('htrend'), nav = el('bnav');
    var h2txt = h2 ? h2.textContent : '', days = [], cache = {}, shown = '', cur = '', hid = [], sum0 = '';
    var stat = mk('p', 'status'), sel = mk('select'), prev = mk('button', 'chip', '前一天'), next = mk('button', 'chip', '後一天');
    var gone = mk('div', 'hgone');
    var DOW = ['日', '一', '二', '三', '四', '五', '六'];
    function dow(s) { var p = s.split('-'); return '星期' + DOW[new Date(+p[0], +p[1] - 1, +p[2]).getDay()]; }
    function okDay(j) {
      if (!j || j.v !== 1 || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(j.date) || !(j.rows instanceof Array)) { return false; }
      for (var i = 0; i < j.rows.length; i++) {
        if (!(j.rows[i] instanceof Array) || j.rows[i].length !== 10) { return false; }
      }
      return true;
    }
    function load(c) {
      if (cache[c]) { return Promise.resolve(cache[c]); }
      return fetch('history/' + c + '.json').then(function (r) {
        if (!r.ok) { throw 0; }
        return r.json();
      }).then(function (j) {
        if (!okDay(j) || ymd(j.date) !== c) { throw 0; }
        cache[c] = j;
        return j;
      });
    }
    function idxOf(c) {
      for (var i = 0; i < days.length; i++) { if (ymd(days[i].d) === c) { return i; } }
      return 0;
    }
    function todayRow() {
      var t = d.querySelectorAll('#ct tbody.cand'), b = 0, i;
      for (i = 0; i < t.length; i++) { if (t[i].getAttribute('data-type') === '突破型') { b++; } }
      return { d: TODAY, n: t.length, b: b };
    }
    function td(lab, cls, kid) {
      var c = mk('td', 'n' + (cls ? ' ' + cls : ''));
      c.setAttribute('data-label', lab);
      if (typeof kid === 'string') { c.textContent = kid; } else { c.appendChild(kid); }
      return c;
    }
    function rowEl(r) {
      var tb = mk('tbody', 'cand'), tr = mk('tr', 'r1'), th = mk('th', 'nm');
      var close = parseFloat(r[3]), chg = parseFloat(r[4]), stop = parseFloat(r[6]);
      var dist = (close - stop) / close * 100, g = dist <= 3 ? ['d-ok', '近'] : (dist <= 6 ? ['d-mid', '中'] : ['d-wide', '遠']);
      th.setAttribute('scope', 'row');
      th.appendChild(mk('span', 'tag ' + (r[2] === '突破型' ? 't-break' : 't-pull'), r[2]));
      th.appendChild(mk('br'));
      th.appendChild(mk('span', 'code', r[0]));
      th.appendChild(mk('span', 'name', r[1]));
      if (r[7]) { th.appendChild(mk('span', 'sec', r[7])); }
      th.appendChild(mk('span', 'kd', (/^00/.test(r[0]) ? 'ETF' : '個股') + (r[8] ? '·' + r[8] : '')));
      if (+r[9] > 1) { th.appendChild(mk('span', 'bdg bd-old', '連續 ' + r[9] + ' 天')); }
      tr.appendChild(th);
      tr.appendChild(td('收盤', '', r[3]));
      tr.appendChild(td('漲跌%', '', mk('span', chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat'),
        chg > 0 ? '▲ +' + chg.toFixed(2) + '%' : (chg < 0 ? '▼ ' + chg.toFixed(2) + '%' : '平 0.00%'))));
      tr.appendChild(td('量比', '', parseFloat(r[5]).toFixed(2)));
      tr.appendChild(td('建議停損', '', r[6]));
      var c = td('停損距離', g[0], dist.toFixed(1) + '%');
      c.appendChild(mk('span', 'g', g[1]));
      tr.appendChild(c);
      tb.appendChild(tr);
      return tb;
    }
    function drawTable(j) {
      var wrap = mk('div', 'card tw cards'), tb = mk('table', 'ct'), cap = mk('caption', 'vh', '過去的候選標的'), hd = mk('tr'), i;
      var cols = ['標的', '收盤', '漲跌%', '量比', '建議停損', '停損距離'];
      for (i = 0; i < cols.length; i++) { hd.appendChild(mk('th', i ? 'n' : 'nm', cols[i])); }
      var th = mk('thead');
      th.appendChild(hd);
      tb.appendChild(cap); tb.appendChild(th);
      for (i = 0; i < j.rows.length; i++) { tb.appendChild(rowEl(j.rows[i])); }
      wrap.appendChild(tb);
      while (view.firstChild) { view.removeChild(view.firstChild); }
      view.appendChild(wrap);
    }
    function showToday() {
      var i;
      view.hidden = true;
      while (view.firstChild) { view.removeChild(view.firstChild); }
      for (i = 0; i < hid.length; i++) { hid[i].n.hidden = hid[i].h; }
      hid = [];
      if (h2) { h2.textContent = h2txt; }
      gone.hidden = false;
      shown = '';
      stat.textContent = sum0;
      draw();
    }
    function showPast(j) {
      if (!hid.length) {
        var n = view.nextSibling;
        while (n) {
          if (n.nodeType === 1) { hid.push({ n: n, h: n.hidden }); n.hidden = true; }
          n = n.nextSibling;
        }
      }
      if (h2) { h2.textContent = '候選標的（' + j.date + ' ' + dow(j.date) + '，' + j.rows.length + ' 檔）'; }
      drawTable(j);
      view.hidden = false;
      gone.hidden = true;
      shown = ymd(j.date);
      stat.textContent = j.date + ' ' + dow(j.date) + '：掃描 ' + j.scanned + ' 檔，候選標的 ' + j.n + ' 檔（突破型 ' + j.nb + '、回測型 ' + j.np + '）。這是當天的紀錄。';
      draw();
    }
    function pick(c) {
      cur = c;
      sel.value = c;
      var i = idxOf(c || TC);
      prev.disabled = i >= days.length - 1;
      next.disabled = i <= 0;
      if (!c) { showToday(); return; }
      stat.textContent = '讀取中…';
      load(c).then(function (j) { if (cur === c) { showPast(j); } }, function () {
        if (cur === c) {
          cur = shown;
          sel.value = shown;
          stat.textContent = '這一天的紀錄讀不到（可能沒有網路）。';
        }
      });
    }
    function step(k) {
      var i = idxOf(cur || TC) + k;
      if (i >= 0 && i < days.length) { pick(i ? ymd(days[i].d) : ''); }
    }
    function draw() {
      var n = days.length, W = 720, H = 150, base = 120, step0 = W / Math.max(n, 24), bw = Math.max(1, step0 * 0.7), mx = 1, i, s = '';
      for (i = 0; i < n; i++) { if (days[i].n > mx) { mx = days[i].n; } }
      var k = (base - 6) / mx, now = shown || TC, tot = 0;
      for (i = 0; i < n; i++) {
        var e = days[n - 1 - i], x = W - (n - i) * step0, h1 = (e.n - e.b) * k, h2b = e.b * k, c = ymd(e.d);
        tot += e.n;
        s += '<rect class="hb hb-p" data-d="' + c + '" x="' + (x + (step0 - bw) / 2).toFixed(1) + '" y="' + (base - h1).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + h1.toFixed(1) + '"></rect>';
        s += '<rect class="hb hb-b" data-d="' + c + '" x="' + (x + (step0 - bw) / 2).toFixed(1) + '" y="' + (base - h1 - h2b).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + h2b.toFixed(1) + '"></rect>';
        s += '<rect class="hb hb-t" data-d="' + c + '" x="' + x.toFixed(1) + '" y="0" width="' + step0.toFixed(1) + '" height="' + base + '"></rect>';
        if (c === now) {
          s += '<rect class="hb-s" x="' + (x + (step0 - bw) / 2 - 1).toFixed(1) + '" y="' + (base - h1 - h2b - 1).toFixed(1) + '" width="' + (bw + 2).toFixed(1) + '" height="' + (h1 + h2b + 2).toFixed(1) + '"></rect>';
        }
      }
      s += '<line class="hb-ax" x1="0" y1="' + base + '" x2="' + W + '" y2="' + base + '"></line>';
      s += '<text class="hl" x="0" y="146">' + days[n - 1].d.slice(5) + '</text><text class="hl" x="' + W + '" y="146" text-anchor="end">' + days[0].d.slice(5) + '</text>';
      s += '<text class="hl" x="' + (W / 2) + '" y="146" text-anchor="middle">最多 ' + mx + ' 檔</text>';
      trend.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="每日候選標的檔數，最近 ' + n + ' 天，最多 ' + mx + ' 檔，今天 ' + days[0].n + ' 檔">' + s + '</svg>';
      trend.hidden = false;
    }
    function badges() {
      var p1 = days.length > 1 ? ymd(days[1].d) : '';
      var none = function () { return null; };
      Promise.all([load(TC).then(null, none), p1 ? load(p1).then(null, none) : null]).then(function (r) {
        var t = r[0], p = r[1], tm = {}, pm = {}, i, rows = d.querySelectorAll('#ct tbody.cand'), a = 0, b = 0, off = [];
        if (!t && !p) { return; }
        if (t) { for (i = 0; i < t.rows.length; i++) { tm[t.rows[i][0]] = +t.rows[i][9]; } }
        if (p) { for (i = 0; i < p.rows.length; i++) { pm[p.rows[i][0]] = p.rows[i][1]; } }
        var now = {};
        for (i = 0; i < rows.length; i++) {
          var code = rows[i].getAttribute('data-code'), kk = tm[code] || 0, fresh = p ? !(code in pm) : kk === 1;
          var th = rows[i].querySelector('th.nm'), chb = th ? th.querySelector('.chb') : null;
          now[code] = 1;
          if (fresh) { a++; } else { b++; }
          if (th && (p || kk)) {
            var bd = mk('span', 'bdg ' + (fresh ? 'bd-new' : 'bd-old'), fresh ? '新增' : (kk > 1 ? '續留・第 ' + kk + ' 天' : '續留'));
            if (chb) { th.insertBefore(bd, chb); } else { th.appendChild(bd); }
          }
        }
        if (!p) { return; }
        for (var code2 in pm) { if (!(code2 in now)) { off.push(code2 + ' ' + pm[code2]); } }
        sum0 = '和 ' + days[1].d + ' 比較：新增 ' + a + ' 檔、續留 ' + b + ' 檔、消失 ' + off.length + ' 檔。';
        if (!shown) { stat.textContent = sum0; }
        if (off.length) {
          var btn = mk('button', 'chip', '看消失的 ' + off.length + ' 檔'), box = mk('p', 'cap14', off.join('、'));
          btn.type = 'button'; btn.setAttribute('aria-expanded', 'false'); box.hidden = true;
          btn.addEventListener('click', function () {
            box.hidden = !box.hidden;
            btn.setAttribute('aria-expanded', box.hidden ? 'false' : 'true');
          });
          gone.appendChild(btn); gone.appendChild(box);
        }
      });
    }
    function start(j) {
      var list = [todayRow()], i, e;
      if (j && j.v === 1 && j.days instanceof Array) {
        for (i = 0; i < j.days.length; i++) {
          e = j.days[i];
          if (e && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(e.d) && e.d < TODAY && typeof e.n === 'number' && typeof e.b === 'number') { list.push(e); }
        }
      }
      days = list.slice(0, 120);
      sum0 = TODAY + ' ' + dow(TODAY) + '：候選標的 ' + days[0].n + ' 檔。';
      var row = mk('div', 'hrow'), lab = mk('label', 'fld', '日期'), i2;
      prev.type = 'button'; next.type = 'button';
      prev.setAttribute('aria-label', '前一天'); next.setAttribute('aria-label', '後一天');
      lab.setAttribute('for', 'hsel'); sel.id = 'hsel';
      for (i2 = 0; i2 < days.length; i2++) {
        var o = mk('option', '', (i2 ? '' : '今天 ') + days[i2].d + ' ' + dow(days[i2].d) + '（' + days[i2].n + ' 檔）');
        o.value = i2 ? ymd(days[i2].d) : '';
        sel.appendChild(o);
      }
      lab.appendChild(sel);
      row.appendChild(prev); row.appendChild(lab); row.appendChild(next);
      stat.setAttribute('role', 'status'); stat.setAttribute('aria-live', 'polite');
      bar.appendChild(row); bar.appendChild(stat); bar.appendChild(gone);
      sel.addEventListener('change', function () { pick(sel.value); });
      prev.addEventListener('click', function () { step(1); });
      next.addEventListener('click', function () { step(-1); });
      if (trend) {
        trend.addEventListener('click', function (ev) {
          var c = ev.target && ev.target.getAttribute ? ev.target.getAttribute('data-d') : null;
          if (c) { pick(c === TC ? '' : c); }
        });
      }
      if (nav) {
        var a0 = nav.querySelector('a');
        if (a0) { a0.addEventListener('click', function () { if (cur) { pick(''); } }); }
      }
      bar.hidden = false;
      pick('');
      badges();
    }
    fetch('history/index.json', { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) { throw 0; }
      return r.json();
    }).then(start, function () {
      bar.appendChild(stat);
      stat.textContent = '歷史紀錄暫時讀不到，目前只顯示今天。';
      bar.hidden = false;
    });
  } catch (e) { }
})();
'''


PWA_HIST = ('<section class="band alt" id="hist" aria-labelledby="hist-h"><div class="wrap stack"><h2 id="hist-h">每日歷史</h2>'
            '<p class="cap14">每個交易日收盤後新增一天，保留最近 120 個交易日。下面的長條是每天的候選標的檔數（紅色是突破型，灰色是回測型），'
            '點長條或用上面的日期列就能切換到那一天；需要網路載入，沒有網路時只看得到之前開過的日期。</p>'
            '<div class="htrend needs-js" id="htrend" hidden></div></div></section>')
PWA_BNAV = ('<nav class="bnav" id="bnav" aria-label="快速切換"><a href="#cands">今日</a><a href="#hist">歷史</a>'
            '<a href="#help">說明</a></nav>')


def _pwa_head():
    import share
    h = SHARE_HEAD.replace('content="width=device-width, initial-scale=1"', 'content="%s"' % share.PWA_VIEWPORT, 1)
    return h.replace('<title>', share.PWA_HEAD_TAGS + '\n<title>', 1).replace('</style>', PWA_CSS + '</style>', 1)


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


SHARE_BAD_CATS = {'Cc', 'Cf', 'Co', 'Cs', 'Cn', 'Zl', 'Zp'}     # 控制、格式（零寬、方向控制）、私用、未定義、行／段分隔


def _share_text(v, label, limit):
    if not isinstance(v, str) or len(v) > limit or any(unicodedata.category(c) in SHARE_BAD_CATS for c in v):
        raise ValueError('分享版：「%s」的內容不合規則' % label)
    return v


def share_rows(res):
    """只取候選標的、只複製 SHARE_FIELDS（和 ma 的三個價位）；資料有任何不合規則的地方就丟 ValueError"""
    rows = []
    for r in res:
        if 'error' in r or not r.get('type'):
            continue
        try:
            row = {k: (r.get(k, '') if k in SHARE_OPTIONAL else r[k]) for k in SHARE_FIELDS}
        except KeyError as e:
            raise ValueError('分享版：候選標的缺少欄位 %s' % e.args[0])
        if not isinstance(row['code'], str) or not SHARE_CODE_RE.fullmatch(row['code']):
            raise ValueError('分享版：有股票代號的格式不對')
        if row['type'] not in ('突破型', '回測型'):
            raise ValueError('分享版：有型態不是突破型或回測型')
        _share_text(row['name'], '名稱', 60)
        _share_text(row['setup'], '說明', 300)
        _share_text(row['stop_why'], '停損依據', 300)
        if not isinstance(row['sector'], str) or (row['sector'] and row['sector'] not in SECTOR_LABELS):
            raise ValueError('分享版：類股的內容不合規則')
        if row['market'] not in ('', '上市', '上櫃'):
            raise ValueError('分享版：市場的內容不合規則')
        for k in ('close', 'chg', 'vol_ratio', 'stop'):
            if not _isnum(row[k]):
                raise ValueError('分享版：「%s」不是有效的數字' % k)
        if row['close'] <= 0 or row['stop'] <= 0:
            raise ValueError('分享版：收盤或停損不是正數')
        ma = {}
        for k in SHARE_MA:
            v = (r.get('ma') or {}).get(k)
            if v is None:
                continue
            if not _isnum(v):
                raise ValueError('分享版：「%s」不是有效的數字' % k)
            ma[k] = v
        row['ma'] = ma
        rows.append(row)
    return sorted(rows, key=lambda r: (r['type'], -r['vol_ratio'], r['code']))     # 代號放最後，輸入順序不同也產生同樣的頁面


def _share_help(pwa=False):
    s = """<section class="band alt" id="help" aria-labelledby="help-h"><div class="wrap stack help">
<h2 id="help-h">怎麼看這一頁</h2>
<h3>關卡說明</h3><ul>
<li>第 0 關 週線：週 MACD 在零軸上（或柱狀體剛翻紅），收盤在週 MA13、MA26 之上，週線不是空頭排列。</li>
<li>第 1 關 趨勢：收盤在 MA60 之上，MA20 在 MA60 之上，MA20 不下彎、MA60 上彎。</li>
<li>第 2 關 動能：日 OSC 今天由負翻正；或 3 日內 DIF 上穿 DEA，而且今天的柱狀體比昨天大。</li>
<li>第 3 關 量能：今天的量大於前 5 日均量的 1.3 倍，而且收紅。</li>
<li>第 4 關 型態：突破型要過第 0～3 關，是整理或布林縮口之後，帶量長紅 K 收在布林上軌之上；回測型要過第 0～1 關，是沿月線爬升、量縮回測 MA20 附近止跌。</li>
<li>建議停損：突破型取長紅 K 低點與 MA20 中較高的一個；回測型取 MA20 與當日低點中較低的一個。</li>
</ul>
<h3>怎麼看這一頁</h3><ul>
<li>最上面是摘要：資料日期、掃描檔數，以及候選標的有幾檔突破型和回測型。</li>
<li>候選標的可以用「型態」「類別」（00 開頭的代號算 ETF）篩選，也可以搜尋代號或名稱；用「排序」選單或點表格欄位名稱排序，再點一次反過來排。</li>
<li>型態：<span class="tag t-break">突破型</span> 整理後帶量長紅突破；<span class="tag t-pull">回測型</span> 沿月線上行、量縮回測月線止跌。</li>
<li>漲跌%：依台灣習慣，<span class="up">▲ 紅色是上漲</span>、<span class="down">▼ 綠色是下跌</span>，也都會寫正負號。</li>
<li>停損距離：<span class="d-ok">近</span> 3% 以內，停損很近；<span class="d-mid">中</span> 3%～6%，普通；<span class="d-wide">遠</span> 超過 6%，停損很遠，一旦停損虧損會偏大。</li>
<li>價位圖：一條刻度上畫出收盤（圓點）、MA20（灰線）、建議停損（橘線，橘色粗段是收盤到停損的距離），有資料時灰色底是布林通道上下軌；實際數字寫在圖下方。</li>
<li>K 線圖：每一列都有「K 線圖」按鈕（點那一列也可以），展開近 120 個交易日的日 K 線，附 MA20、MA60、布林上下軌、成交量與 MACD，並畫出建議停損線。圖上的價格是約略值，精確數字看表格；候選太多時只有前面的標的有圖。按 Esc 或「收起」關閉。</li>
<li>K 線圖的指標與縮放：圖下方的「主圖」「副圖」按鈕選要看的指標（MA5、MA10、MA20、MA60、布林；成交量、KD、MACD、RSI），副圖標題的「−」可以關掉該面板，選擇會記在這個瀏覽器。「60 日」「120 日」「全部」「−」「+」「重設」調整顯示範圍；電腦可用 Ctrl＋滾輪縮放、拖曳平移，手機兩指縮放、單指左右拖曳。滑鼠移到圖上或手機點一下會顯示十字線與當天數值；圖有焦點時可用 ← → 移動、+ − 縮放、Esc 取消。MA5、MA10、KD、RSI 由頁面依圖上的約略價格計算，可能和看盤軟體差一點。</li>
<li>類股：官方的產業別，例如「科技-半導體」「航運」；ETF 依代號最後一碼與名稱分成主動式、高股息、科技、海外、市值型、債券等；旁邊的小框寫個股或 ETF 與上市／上櫃；候選標的可以用「類股」選單篩選。</li>
<li>頁面右上角可切換自動（跟著電腦或手機）／淺色／深色，選擇記在這個瀏覽器；無痕或封鎖網站資料時只在這次有效。手機上表格會改成一張一張的卡片。</li>
</ul>
<p class="cap14">本頁僅供程式與技術分析研究，不是投資建議，也不保證任何績效。資料可能延遲、缺漏或有誤，請以交易所公告為準。投資決定與後果由使用者自行承擔。</p>
</div></section>"""
    if not pwa:
        return s
    more = ('<li>歷史紀錄：每個交易日收盤後新增一天，保留最近 120 個交易日。日期列的「前一天」「後一天」或選單可以切換到過去的某一天'
            '（過去的日期只列出當天的候選標的，沒有圖和篩選）。標的名稱旁的「新增」表示前一個交易日沒有上榜，「續留・第 N 天」表示連續上榜的天數。</li>\n'
            '<li>加到主畫面：手機瀏覽器的選單有「加入主畫面」（iPhone 用 Safari 的分享選單），之後像 App 一樣開啟。看過的頁面沒有網路也能開，'
            '但資料不一定是最新的，瀏覽器也可能清掉離線資料。</li>\n')
    return s.replace('</ul>\n<p class="cap14">', more + '</ul>\n<p class="cap14">').replace('class="band alt" id="help"', 'class="band" id="help"', 1)


def render_share_html(res, sinfo=None, charts=None, pwa=False):
    """分享版：只有今天的候選標的與關卡說明。不收任何 meta（總資金、路徑、產生時間…），同樣的資料一定產生同樣的位元組。
    不會修改 res；資料有不合規則的地方丟 ValueError。
    charts：{代號: data-ch}，依顯示順序從前面開始放，整頁超過 SHARE_CHART_BUDGET 就不再放（只有前段的標的有圖）。"""
    h = html.escape
    cands = share_rows(res)
    if charts:
        if not all(isinstance(k, str) and isinstance(v, str) and CHART_RE.fullmatch(v) for k, v in charts.items()):
            raise ValueError('分享版：線圖的資料不合規則')
        left = SHARE_CHART_BUDGET - len(render_share_html(res, sinfo, None, pwa).encode('utf-8'))
        keep = {}
        for r in cands:
            v = charts.get(r['code'])
            if v is None:
                continue
            left -= len(v) + len(' data-ch=""')
            if left < 0:
                break
            keep[r['code']] = v
        charts = keep
    ok = [r for r in res if 'error' not in r]
    dates = [r['date'] for r in ok]
    if not dates or any(not isinstance(d, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', d) for d in dates):
        raise ValueError('分享版：沒有有效的資料日期')
    date = max(dates)
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError('分享版：資料日期不是有效的日期')
    nb = sum(1 for r in cands if r['type'] == '突破型')
    sinfo = sinfo or {'status': 'none', 'fetched': ''}
    if (not isinstance(sinfo, dict) or sinfo.get('status') not in ('none', 'ok', 'stale')
            or not isinstance(sinfo.get('fetched'), str) or not re.fullmatch(r'|[0-9]{4}-[0-9]{2}-[0-9]{2}', sinfo['fetched'])):
        raise ValueError('分享版：類股資料狀態不合規則')
    ctx = dict(cands=cands, sinfo=sinfo, charts=charts or {})
    B = [_topnav(date) + '<main>']
    B.append('<header class="band alt" id="top"><div class="wrap stack">')
    B.append('<div class="stack" style="gap:8px"><h1><span class="nw">台股盤後選股</span> <span class="nw">%s</span></h1><p class="sub">%s</p></div>' % (
        h(date + _dow(date)), h('收盤資料｜全市場掃描 %d 檔（資料有效 %d 檔）' % (len(res), len(ok)))))
    B.append('<dl class="stats">'
             '<div><dt>候選標的</dt><dd>%d<small>檔</small></dd></div>'
             '<div><dt>突破型</dt><dd>%d<small>檔</small></dd></div>'
             '<div><dt>回測型</dt><dd>%d<small>檔</small></dd></div></dl>' % (len(cands), nb, len(cands) - nb))
    B.append('<p class="cap14">分享版：只列出今天的候選標的與關卡說明。僅供研究，非投資建議。</p>')
    B.extend(x for x in (_sec_stale(sinfo),) if x)
    jump = '<nav class="jump" aria-label="本頁段落"><a href="#cands">候選標的</a><a href="#help">怎麼看這一頁</a></nav>'
    if pwa:
        B.append('<div class="a2hs" id="a2hs" role="note" hidden></div>')
        jump = jump.replace('<a href="#help">', '<a href="#hist">歷史紀錄</a><a href="#help">')
    B.append(jump)
    B.append('</div></header>')
    n0 = len(B)
    _sec_cands(B, ctx, None, '', 0.02, placeholder='輸入代號或名稱')
    if pwa:
        for i in range(n0, len(B)):
            if B[i].startswith('<h2 id="cands-h">'):
                B[i] += '<div class="hbar needs-js" id="hbar" data-day="%s" hidden></div><div id="hview" hidden></div>' % h(date)
                break
        B.append(PWA_HIST)
    B.append(_share_help(pwa))
    B.append('</main>' + (PWA_BNAV if pwa else '') + '<footer><div class="wrap"><p class="fine">資料日期 %s｜僅供研究，非投資建議。</p></div></footer>' % h(date))
    return (_pwa_head() if pwa else SHARE_HEAD).replace('@@TITLE@@', h('台股盤後選股 %s%s' % (date, _dow(date)))) + '\n'.join(B) + '\n<script>' + SHARE_JS + (PWA_JS if pwa else '') + '</script>\n</body>\n</html>\n'


# ---------- 每日歷史（share_pwa=1）：每個交易日存一個日檔，格式與檢查見 share.check_history ----------
def share_history_dir(cfg):
    """歷史日檔存在設定檔所在資料夾的 cache\\share\\history（用暫存資料夾的設定檔測試，就只會寫進暫存資料夾）"""
    return os.path.join(os.path.dirname(os.path.abspath(cfg['path'])), 'cache', 'share', 'history')


def _prev_trading_day(data, tag):
    """交易日曆：資料資料夾旁的 cache\\twse\\YYYYMMDD.json 檔名；回傳比 tag 早的最近一天，沒有就回 None"""
    d = os.path.join(os.path.dirname(os.path.abspath(data)), 'cache', 'twse')
    try:
        days = [n[:8] for n in os.listdir(d) if re.fullmatch(r'[0-9]{8}\.json', n)]
    except OSError:
        return None
    before = [n for n in days if n < tag]
    return max(before) if before else None


def _num4(x):
    return str(round(float(x), 4))


def history_json(rows, date, scanned, valid, prev_ks):
    """rows 是 share_rows 的結果；數字一律寫成字串（數字陣列會被金額規則誤判）；k = 連續上榜天數（前一個交易日有這檔就加 1）"""
    out = [[r['code'], r['name'], r['type'], _num4(r['close']), _num4(r['chg']), _num4(r['vol_ratio']), _num4(r['stop']),
            r['sector'], r['market'], str(prev_ks.get(r['code'], 0) + 1)] for r in rows]
    nb = sum(1 for r in rows if r['type'] == '突破型')
    obj = {'v': 1, 'date': date, 'scanned': scanned, 'valid': valid, 'n': len(rows), 'nb': nb, 'np': len(rows) - nb, 'rows': out}
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _load_ks(hist_dir, day):
    try:
        with open(os.path.join(hist_dir, day + '.json'), encoding='utf-8') as f:
            return {r[0]: int(r[9]) for r in json.load(f)['rows']}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {}


def _snapshot(res, tag, data, hist_dir, rules, share):
    ok = [r for r in res if 'error' not in r]
    date = max(r['date'] for r in ok)
    if date.replace('-', '') != tag:
        raise ValueError('資料日期和檔名不一樣')
    prev = _prev_trading_day(data, tag)
    b = history_json(share_rows(res), date, len(res), len(ok), _load_ks(hist_dir, prev) if prev else {})
    problems = share.check_history(b, rules, tag)
    if problems:
        raise ValueError('；'.join(problems[:3]))
    return b


def _write_if_changed(path, b):
    try:
        with open(path, 'rb') as f:
            if f.read() == b:
                return False
    except OSError:
        pass
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(b)
    os.replace(tmp, path)
    return True


def _backfill_history(hist_dir, out_dir, data, tag, rules, share):
    """歷史資料夾還不存在時，一次性從結果資料夾補：只取「分享版_D.html 和 盤後選股_D.json 都在」的日期（最多 HISTORY_DAYS 天）"""
    have = {}
    for n in os.listdir(out_dir):
        m = re.fullmatch(r'盤後選股_([0-9]{8})\.json', n)
        if m and m.group(1) < tag and os.path.exists(os.path.join(out_dir, '分享版_%s.html' % m.group(1))):
            have[m.group(1)] = os.path.join(out_dir, n)
    os.makedirs(hist_dir, exist_ok=True)
    for d in sorted(have)[-share.HISTORY_DAYS:]:
        try:
            with open(have[d], encoding='utf-8') as f:
                res = json.load(f)
            _write_if_changed(os.path.join(hist_dir, d + '.json'), _snapshot(res, d, data, hist_dir, rules, share))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def _prune_history(hist_dir, keep):
    names = sorted(n for n in os.listdir(hist_dir) if re.fullmatch(r'[0-9]{8}\.json', n))
    for n in names[:-keep]:
        try:
            os.remove(os.path.join(hist_dir, n))
        except OSError:
            pass


def update_share_history(hist_dir, res, data, out_dir, rules):
    """把今天的候選標的存成一個日檔（內容沒變就不寫），只留最新 HISTORY_DAYS 個。失敗只印提醒，分享版不受影響。回傳是否成功。"""
    try:
        import share
        tag = max(r['date'] for r in res if 'error' not in r).replace('-', '')
        if not os.path.isdir(hist_dir):
            _backfill_history(hist_dir, out_dir, data, tag, rules, share)
        os.makedirs(hist_dir, exist_ok=True)
        _write_if_changed(os.path.join(hist_dir, tag + '.json'), _snapshot(res, tag, data, hist_dir, rules, share))
        _prune_history(hist_dir, share.HISTORY_DAYS)
        return True
    except Exception as e:
        say('注意：今天的歷史紀錄沒有寫入（%s），分享版不受影響。' % (e if isinstance(e, ValueError) else type(e).__name__))
        return False


CSV_HEAD = ['代號', '名稱', '資料日期', '型態', '收盤', '漲跌%', '量比', '建議停損', '停損距離%', '股數(設定風險)', '金額(設定風險)',
            '股數(5%)', '金額(5%)', '0週線', '1趨勢', '2動能', '3量能', 'A突破', 'B回測', '說明', '停損依據', '備註', '類股', '市場']


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
                          r.get('setup', ''), r.get('stop_why', ''), '；'.join(r['notes']), r.get('sector', ''), r.get('market', '')])
        for r in res:
            if 'error' in r:
                w.writerow([f'="{r["code"]}"', r['name']] + [''] * 19 + [r['error'], r.get('sector', ''), r.get('market', '')])


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
    share = bool(getattr(a, 'share', False) or (cfg and (cfg['share'] or cfg['share_publish'])))
    return dict(capital=capital, risk=risk, list=wl, out_dir=out_dir, share=share, cfg=cfg)


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


SHARE_STATE = os.path.join(HERE, 'cache', 'share', 'state.json')


def _share_rules(share, cfg, capital, amounts, data_dir, out_dir, list_path=None):
    """分享版安全檢查的規則：總資金、部位試算金額、自選股清單、本機路徑、帳號名稱都不可以出現在分享版裡"""
    import tempfile
    paths = [HERE, os.path.expanduser('~'), os.getcwd(), tempfile.gettempdir()]
    paths += [os.path.abspath(p) for p in (data_dir, out_dir) if p]
    if cfg:
        paths += [os.path.dirname(cfg['path']), cfg['path']]
    wl = {}
    for p in (cfg['watchlist'] if cfg else None, os.path.join(HERE, '自選股.csv'), os.path.join(os.getcwd(), '自選股.csv'), list_path):
        if p and os.path.isfile(p):
            try:
                wl.update(load_list(p, quiet=True))
            except Exception:
                pass
    return share.make_rules(capital=capital, amounts=amounts, paths=paths, watchlist=wl,
                            repo=cfg['share_repo'] if cfg else None, sectors=SECTOR_LABELS)


def build_share_page(res, o, a, tag, wl, sinfo=None, charts=None):
    """產生分享版並做安全檢查，通過才寫檔（回傳路徑）。不能產生或沒通過：不寫檔、刪掉同一天舊的分享版、
    印出原因，回傳 None。不影響選股結果。"""
    out_dir = o['out_dir']
    if wl is not None:
        say('注意：分享版只支援全市場（mode=full），自選股清單模式不會產生分享版。')
        return None
    if not out_dir or not os.path.isdir(out_dir):
        say('注意：沒有結果資料夾（--out-dir 或設定檔的 output_dir），所以沒有產生分享版。')
        return None
    path = os.path.join(out_dir, f'分享版_{tag}.html')
    pwa = bool(o['cfg'] and o['cfg'].get('share_pwa'))
    rules = None
    try:
        import share
        data = render_share_html(res, sinfo, charts, pwa).encode('utf-8')
        amounts = [v for r in res for s in (r.get('size') or []) for v in (s['shares'], s['amount'])]
        rules = _share_rules(share, o['cfg'], o['capital'], amounts, a.data, out_dir, o['list'])
        problems = share.check_html(data, rules, pwa) if pwa else share.check_html(data, rules)
    except Exception as e:
        problems = ['%s' % (e if isinstance(e, ValueError) else '%s: %s' % (type(e).__name__, e))]
    if problems:
        try:
            os.remove(path)
        except OSError:
            pass
        say('注意：分享版沒有產生（安全檢查沒有通過），選股結果不受影響。原因：')
        for p in problems[:6]:
            say('  - ' + p)
        if len(problems) > 6:
            say('  - 還有 %d 項' % (len(problems) - 6))
        return None
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    if pwa and rules is not None:
        update_share_history(share_history_dir(o['cfg']), res, a.data, out_dir, rules)
    return path


def cmd_scan(a):
    o = resolve_scan_options(a)
    capital, risk = o['capital'], o['risk']
    if not os.path.isdir(a.data) or next(files(a.data), None) is None:
        raise UserError(E_NEED_DATA, f'找不到股價資料：資料夾「{a.data}」不存在或裡面沒有任何 .csv。',
                        '請先雙擊 setup.bat 並選擇「現在下載」，或在程式資料夾執行 python fetch_data.py backfill。')
    wl = load_list(o['list'])
    res, charts = [], {}
    todo = [(code, path) for code, path in files(a.data) if wl is None or code in wl]
    for (code, path), (_, raw) in zip(todo, prefetch([p for _, p in todo])):
        name, bars = load_file(path, raw)
        r = analyze(code, (wl or {}).get(code) or name, bars)
        size(r, capital, risk)
        if 'error' not in r and (r.get('type') or r.get('watch')):
            try:
                charts[code] = chart_code(chart_series(bars))
            except (ValueError, ZeroDivisionError, OverflowError):
                pass
        res.append(r)
    ind = load_industry(industry_dir(a.data))
    attach_sectors(res, ind['rows'])
    attach_markets(res, load_market(a.data))
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
    sinfo = sector_info(ind, max(valid) if valid else '')

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
                    today=dt.date.today().isoformat(), generated=dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
                    sector_info=sinfo, charts=charts)
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(render_html(res, wl is not None, meta))
    if csv_path:
        write_csv(csv_path, res, capital)
    share_path = build_share_page(res, o, a, tag, wl, sinfo, charts) if o['share'] else None
    if not md_path:
        sys.stdout.write(md)
    if any((html_path, csv_path, md_path, txt_path, json_path)):
        say(f'選股完成：掃描 {len(res)} 檔，候選 {sum(1 for r in res if r.get("type"))} 檔。')
        for label, p in (('網頁報告', html_path), ('Excel 表格', csv_path), ('文字報告', md_path), ('分享版', share_path)):
            if p:
                say(f'  {label}：{os.path.abspath(p)}')
    if a.open:
        if html_path:
            open_file(html_path)
        else:
            say('沒有產生 HTML（要加 --html 或 --out-dir），--open 略過。')


def cmd_share(a):
    """選用：把分享版公開到 GitHub Pages（--publish），或下架（--takedown）。沒開 share_publish 時什麼都不做。"""
    import share
    cfg = load_config(a.config)
    token = a.confirm_first_publish
    if token is not None:
        try:
            token = parse_repo(token) or ''
        except ValueError as e:
            raise UserError(share.E_SHARE, f'--confirm-first-publish 後面的寫法不對：{e}。', '例如 --confirm-first-publish yourname/tw-stock-screener-results')
    pwa_token = getattr(a, 'confirm_pwa', None)
    if pwa_token is not None:
        try:
            pwa_token = parse_repo(pwa_token) or ''
        except ValueError as e:
            raise UserError(share.E_SHARE, f'--confirm-pwa 後面的寫法不對：{e}。', '例如 --confirm-pwa yourname/tw-stock-screener-results')
    try:
        if a.takedown is not None:
            repo = parse_repo(a.takedown) if a.takedown else (cfg['share_repo'] or share.load_state(SHARE_STATE).get('repo'))
            return share.takedown(repo, SHARE_STATE, say)
        if pwa_token is not None and not cfg['share_pwa']:
            raise UserError(share.E_SHARE, '設定檔的 share_pwa 沒有打開，所以 --confirm-pwa 不能用。',
                            '要公開 PWA 版：在 設定.txt 把 share_pwa 改成 1 後再執行；只公開 index.html 和 README.md 兩個檔案的版本，請用 --confirm-first-publish。')
        if not cfg['share_publish']:
            if token is not None or pwa_token is not None:
                raise UserError(share.E_SHARE, '設定檔的 share_publish 沒有打開，所以不會公開。', '在 設定.txt 把 share_publish 改成 1 後再執行。')
            return 0
        out_dir = cfg['output_dir']
        amounts = []
        _, tag, _ = share.latest_files(out_dir)
        try:
            with open(os.path.join(out_dir, f'盤後選股_{tag}.json'), encoding='utf-8') as f:
                amounts = [v for r in json.load(f) for s in (r.get('size') or []) for v in (s['shares'], s['amount'])]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        rules = _share_rules(share, cfg, cfg['capital'], amounts, os.path.join(HERE, 'data'), out_dir)
        if cfg['share_pwa']:
            if token is not None:
                raise UserError(share.E_SHARE, '設定檔的 share_pwa 打開了，PWA 版要用 --confirm-pwa 確認，不能用 --confirm-first-publish。',
                                '例如 --confirm-pwa yourname/tw-stock-screener-results；或雙擊 run.bat，看到問題時輸入 Y。')
            return share.publish_pwa(cfg['share_repo'], cfg['mode'], out_dir, share_history_dir(cfg), SHARE_STATE, rules, ask=a.ask,
                                     token=pwa_token, say=say)
        return share.publish(cfg['share_repo'], cfg['mode'], out_dir, SHARE_STATE, rules, ask=a.ask, token=token, say=say)
    except share.ShareError as e:
        raise UserError(e.code, e.cause, e.todo)


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
    s.add_argument('--share', action='store_true')
    s.set_defaults(f=cmd_scan)
    sh = sp.add_parser('share')
    sh.add_argument('--config', required=True)
    sg = sh.add_mutually_exclusive_group(required=True)
    sg.add_argument('--publish', action='store_true')
    sg.add_argument('--confirm-first-publish', metavar='帳號/儲存庫')
    sg.add_argument('--confirm-pwa', metavar='帳號/儲存庫')
    sg.add_argument('--takedown', nargs='?', const='', metavar='帳號/儲存庫')
    sh.add_argument('--ask', action='store_true')
    sh.set_defaults(f=cmd_share)
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
