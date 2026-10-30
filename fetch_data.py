"""從證交所、櫃買中心抓全市場日 K，建立 data/<代號>.csv 給 screener.py 用。只用 Python 內建模組。

用法：
  python fetch_data.py backfill [--start YYYYMMDD] [--end YYYYMMDD]   補歷史（可中斷續跑）
      不加 --start：從今天往前 430 天，最近 34 週每天抓、更早的每週只抓最後一個交易日（約 12 分鐘）
      加了 --start：從那天起每個交易日都抓（和 v1.1.1 一樣，約 38 分鐘）
  python fetch_data.py daily                                          每天收盤後增量更新（需先 backfill 過）
  python fetch_data.py build                                          只用 cache 重建 data/*.csv（離線）

類股：daily／backfill 在 build() 之後，順便把官方的上市、上櫃公司基本資料裡的「產業別」存到
cache/industry/（每週更新一次；失敗只提醒，不影響股價與結束代碼）。

設計：cache/ 放原始每日快照（未還原），data/*.csv 每次都由 cache + 除權息事件整批重建，
還原價不會被重複套用。csv 欄位 date,open,high,low,close,volume,raw_close,name。

結束代碼：0 成功；3 連不上官方網站；4 官方網站回應異常或資料格式不對；
5 官方還沒提供完整資料（上市有、上櫃還沒有）；6 還沒有任何股價資料（要先 backfill）；
1 程式發生未預期的錯誤。
"""
import argparse, csv, datetime as dt, glob, gzip, http.client, json, os, queue, re, ssl, sys, threading, time
import traceback, urllib.error, urllib.parse, urllib.request, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache')
DATA = os.path.join(HERE, 'data')

# ---- 資料來源（2026-10-24 實測）----
# 上市每日收盤行情：date=YYYYMMDD；回傳 {"stat","tables":[...]}，找 fields 含「證券代號」「收盤價」的 table
#   （舊格式為 fields9/data9）。成交股數單位為「股」。休市日或當日尚未公布 stat="很抱歉，沒有符合條件的資料!"。
TWSE_DAY = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json'
# 上櫃每日收盤行情：date=YYYY/MM/DD（%2F）；tables[0] 的 fields 含「代號 名稱 收盤 開盤 最高 最低 成交股數」
#   （欄名帶空白要 strip），成交股數單位為「股」。休市日 totalCount=0、data=[]。
#   官方 OpenAPI 只有最新一天，不能回補，所以用這個網站內部端點。
TPEX_DAY = 'https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json'
# 上市除權息計算結果表：startDate/endDate=YYYYMMDD；日期為民國「115年09月01日」；欄 股票代號、除權息前收盤價、除權息參考價
TWSE_EX = 'https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={s}&endDate={e}&response=json'
# 上櫃除權息：startDate/endDate=YYYY/MM/DD；日期為民國「115/09/01」；欄 代號、除權息前收盤價、除權息參考價
TPEX_EX = 'https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={s}&endDate={e}&response=json'

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
SPAN_DAYS = 430           # 沒有指定 --start 時，往前補幾天
DENSE_WEEKS = 34          # 最近幾週每天都抓；更早的每週只抓最後一個交易日
GAP = 3.0                 # 同一個網站任兩次請求最短間隔（秒）；兩個網站各自計算、同時進行
GAP_MAX = 10.0            # 被擋（403／429／回傳網頁）之後，那個網站的間隔改成這麼久
BLOCK_LIMIT = 2           # 被擋第 2 次就停止，不再試
WORKERS = 4               # 同時作業的執行緒數（每個網站同一時間仍只送一個請求）
RETRY = (10, 30, 90)      # 失敗後等待秒數再重試（整個網站一起等）
KEEP = re.compile(r'^(?:[1-9]\d{3}|00\d{2,4}[A-Z]?)$')   # 一般股 + ETF
NODATA = '沒有符合條件'
# 類股（產業別）：公司基本資料。探測 2026-10-30 實測：兩邊都是 JSON 陣列，上市 1098 筆、上櫃 896 筆，
# 代號鍵 公司代號／SecuritiesCompanyCode，產業別鍵 產業別／SecuritiesIndustryCode，值是 2 碼數字（01～38、91）
TWSE_IND = 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L'
TPEX_IND = 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O'
IND_CODE_KEYS = ('公司代號', 'SecuritiesCompanyCode')
IND_KEYS = ('產業別', 'SecuritiesIndustryCode')
IND_MIN = 300             # 可用筆數少於這個數字就當作格式不對（實測上市約 1100、上櫃約 900）
IND_DAYS = 7              # 類股資料每隔幾天更新一次
IND_SITES = (('twse', '上市', TWSE_IND), ('tpex', '上櫃', TPEX_IND))

E_NETWORK, E_SITE, E_NODATA, E_NEED_DATA = 3, 4, 5, 6

MSG_NETWORK = ('連不上證交所或櫃買中心的網站（可能沒有網路，或被公司網路、防毒軟體擋住）。',
               '確認瀏覽器打得開 https://www.twse.com.tw/ 之後再執行一次；已經抓到的資料會保留。')
MSG_SITE = ('官方網站暫時沒有正常回應（可能在維護、太忙，或暫時擋住太頻繁的連線）{days}。',
            '等 30 分鐘以上再執行一次，只會補抓缺的日期；不要連續重跑。')
MSG_FORMAT = ('官方網站回傳的資料格式和程式認得的不一樣（網站可能改版了）。',
              '請到 GitHub 專案頁看有沒有新版本；沒有的話請開 Issue 並附上 logs 資料夾裡的紀錄。')
MSG_NODATA = ('官方還沒有提供完整資料：{days} 證交所有、櫃買中心還沒有。',
              '通常是官方還在更新，請 30 分鐘後再執行一次；隔天還是這樣的話，請看 README 的常見問題。')
MSG_NEED = ('還沒有任何股價資料（cache 資料夾是空的）。',
            '請先雙擊 setup.bat，問到「要現在下載嗎」時選 Y；或在程式資料夾執行 python fetch_data.py backfill。')


class FetchError(Exception):
    """網路／HTTP／非 JSON／暫時性錯誤，可重試。kind：network 連不上；site 官方回應異常；
    blocked：官方擋住（403／429／回傳網頁而不是資料）"""

    def __init__(self, msg, kind='site', blocked=False):
        super().__init__(msg)
        self.kind = kind
        self.blocked = blocked


class _Aborted(FetchError):
    """已經決定停止（STOP 被設定），不是真的錯誤"""


class Structure(Exception):
    """回傳結構認不得，不重試"""


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


_log_lock = threading.Lock()


def _out(line):
    """印一行；主控台編碼放不下的字改成跳脫寫法，不要讓程式因為印不出來而中斷"""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        buf = getattr(sys.stdout, 'buffer', None)
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        if buf is None:
            print(line.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)
        else:
            buf.write((line + chr(10)).encode(enc, 'backslashreplace'))
            buf.flush()


def log(*a):
    line = ' '.join([dt.datetime.now().strftime('%H:%M:%S')] + [str(x) for x in a])
    with _log_lock:
        _out(line)
        _tee(line)


def fail(code, cause, todo):
    with _log_lock:
        for line in ('', '【沒有完成】', '原因：' + cause, '怎麼做：' + todo):
            _out(line)
            _tee(line)
    sys.exit(code)


def fmt_days(ds):
    return '、'.join(ds[:5]) + (' 等 %d 天' % len(ds) if len(ds) > 5 else '')


# ---------- 小工具 ----------
def num(s):
    if s is None:
        return None
    s = str(s).replace(',', '').strip()
    if s in ('', '--', '---', 'X', 'x', 'N/A', '-'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_iso(s):
    s = str(s).strip()
    m = re.match(r'^(\d{2,4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})', s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r'^(\d{4})(\d{2})(\d{2})$', s) or re.match(r'^(\d{3})(\d{2})(\d{2})$', s)
        if not m:
            return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _ctx():
    c = ssl.create_default_context()
    if hasattr(ssl, 'VERIFY_X509_STRICT'):
        c.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return c


CTX = _ctx()


_RESET = (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
_conns = {}                # 主機 -> HTTPSConnection（保持連線；只在拿著該主機的鎖時使用）
_proxy_cache = {}


def _proxied(host):
    """有設 https／all 代理（且這個主機不在略過清單）時，改走 urllib，讓它處理代理"""
    if host not in _proxy_cache:
        try:
            px = urllib.request.getproxies()
            _proxy_cache[host] = bool(px.get('https') or px.get('all')) and not urllib.request.proxy_bypass(host)
        except Exception:
            _proxy_cache[host] = False
    return _proxy_cache[host]


def _drop(host):
    c = _conns.pop(host, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def _open(url):
    """送一次 GET，回傳 (狀態碼, 標頭, 本文)；不自動跟轉址。測試時換掉這一個函式就不會連網"""
    u = urllib.parse.urlsplit(url)
    host = u.netloc
    path = u.path + ('?' + u.query if u.query else '')
    hdr = {'User-Agent': UA, 'Accept': 'application/json', 'Accept-Encoding': 'gzip'}
    if _proxied(host):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60, context=CTX) as r:
                body = r.read()
                final = r.geturl() if hasattr(r, 'geturl') else url
                return (r.status if final == url else 302), r.headers, body
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
    c = _conns.get(host)
    reused = c is not None
    for attempt in (0, 1):
        if c is None:
            c = _conns[host] = http.client.HTTPSConnection(host, timeout=60, context=CTX)
            reused = False
        try:
            c.request('GET', path, headers=hdr)
            r = c.getresponse()
            body = r.read()
        except _RESET:
            _drop(host)
            if not (reused and attempt == 0):
                raise
            c = None                       # 閒置太久被伺服器關掉：重連、重送一次
        except BaseException:
            _drop(host)
            raise
        else:
            if r.will_close:
                _drop(host)
            return r.status, r.headers, body


def _header(headers, name):
    v = headers.get(name) or headers.get(name.lower())
    return (v or '').strip().lower()


def http_json(url):
    """送一次請求（不重試）並解成 JSON。403／429／回傳網頁會標記 blocked=True"""
    try:
        status, headers, body = _open(url)
    except FetchError:
        raise
    except (OSError, http.client.HTTPException) as e:
        raise FetchError('%s: %s' % (url, e), 'network')
    except ValueError as e:
        raise FetchError('%s: %s' % (url, e), 'site')
    if status in (403, 429):
        raise FetchError('%s: HTTP %d' % (url, status), 'site', blocked=True)
    if status != 200:
        raise FetchError('%s: HTTP %d' % (url, status), 'site')
    enc = _header(headers, 'Content-Encoding')
    if enc == 'gzip':
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error) as e:
            raise FetchError('%s: gzip 解壓縮失敗 %s' % (url, e), 'site')
    elif enc not in ('', 'identity'):
        raise FetchError('%s: 不認得的 Content-Encoding %s' % (url, enc), 'site')
    try:
        return json.loads(body.decode('utf-8-sig'))
    except ValueError:
        if body.lstrip()[:1] == b'<':
            raise FetchError('回傳網頁而不是資料: ' + url, 'site', blocked=True)
        raise FetchError('non-JSON response: ' + url, 'site')


# ---------- 每個網站各自的限速（同一時間一個請求、間隔 GAP 秒） ----------
class _Host:
    def __init__(self):
        self.lock = threading.Lock()
        self.next = 0.0        # 下一個請求最早可以開始的時間
        self.slow = 0.0        # 被擋之後的間隔（0 = 用 GAP）
        self.count = 0         # 送出的請求數
        self.retries = 0       # 重試次數


STOP = threading.Event()
_hosts = {}
_hosts_lock = threading.Lock()
_blocks = [0]                  # 被擋的次數（所有網站合計）
_threads = []


def _now():
    return time.perf_counter()          # Windows 的 monotonic 只有 15 毫秒解析度，間隔會算不準


def _sleep(s):
    """程式裡唯一的等待：收到 STOP 就立刻丟 _Aborted 結束"""
    if STOP.wait(s):
        raise _Aborted('已停止')


def _host(name):
    with _hosts_lock:
        h = _hosts.get(name)
        if h is None:
            h = _hosts[name] = _Host()
        return h


def _reset_net():
    """清掉上一輪的限速、連線與停止旗標（每個指令開始時呼叫）"""
    for t in list(_threads):
        if t.is_alive():
            t.join(5)
    del _threads[:]
    STOP.clear()
    with _hosts_lock:
        _hosts.clear()
        _blocks[0] = 0
    for name in list(_conns):
        _drop(name)


def get(url, parse, retry=None):
    """對 url 送請求並用 parse 解析；失敗依 RETRY（或 retry）重試。同一個網站一次只送一個請求，間隔至少 GAP 秒"""
    retry = RETRY if retry is None else retry
    h = _host(urllib.parse.urlsplit(url).netloc)
    while not h.lock.acquire(timeout=0.5):
        if STOP.is_set():
            raise _Aborted('已停止')
    try:
        for i in range(len(retry) + 1):
            if STOP.is_set():
                raise _Aborted('已停止')
            while h.next - _now() > 0:          # 等到輪到自己；提早醒來就再等
                _sleep(h.next - _now())
            t0 = _now()
            h.next = t0 + max(GAP, h.slow)
            h.count += 1
            try:
                return parse(http_json(url))
            except _Aborted:
                raise
            except FetchError as e:
                if e.blocked:
                    h.slow = GAP_MAX
                    h.next = max(h.next, t0 + GAP_MAX)
                    with _hosts_lock:
                        _blocks[0] += 1
                        stop = _blocks[0] >= BLOCK_LIMIT
                    if stop:
                        STOP.set()
                        raise
                if i == len(retry):
                    if e.kind == 'network':
                        STOP.set()             # 還拿著鎖，其他執行緒不會再送請求
                    raise
                log('連線失敗，%d 秒後重試（%s）' % (retry[i], str(e)[:120]))
                h.retries += 1
                h.next = max(h.next, _now() + retry[i])
    finally:
        h.lock.release()


def col(fields, *names):
    fs = [re.sub(r'<.*?>', '', str(f)).strip() for f in fields]
    for n in names:
        if n in fs:
            return fs.index(n)
    raise Structure('missing column %s in %s' % (names, fs))


def keep(code, name):
    return bool(KEEP.match(code)) and not name.strip().upper().endswith('-DR')


# ---------- 每日行情 ----------
def parse_twse(d):
    stat = str(d.get('stat', ''))
    if NODATA in stat:
        return None
    if not stat.startswith('OK'):
        raise FetchError('TWSE stat=' + stat)
    tab = None
    for t in d.get('tables') or []:
        f = [str(x).strip() for x in t.get('fields') or []]
        if '證券代號' in f and '收盤價' in f:
            tab = (t['fields'], t.get('data') or [])
            break
    if tab is None and d.get('fields9') and '證券代號' in [str(x).strip() for x in d['fields9']]:
        tab = (d['fields9'], d.get('data9') or [])
    if tab is None:
        raise Structure('TWSE: 找不到含證券代號與收盤價的表')
    f, data = tab
    ic, inm = col(f, '證券代號'), col(f, '證券名稱')
    iv, io, ih, il, icl = col(f, '成交股數'), col(f, '開盤價'), col(f, '最高價'), col(f, '最低價'), col(f, '收盤價')
    rows = []
    for r in data:
        code, name = str(r[ic]).strip(), str(r[inm]).strip()
        if not keep(code, name):
            continue
        o, h, l, c, v = num(r[io]), num(r[ih]), num(r[il]), num(r[icl]), num(r[iv])
        if None in (o, h, l, c, v) or v <= 0 or c <= 0:
            continue
        rows.append([code, name, o, h, l, c, int(v)])
    if not rows:
        raise Structure('TWSE: 表存在但沒有可用資料列')
    return rows


def parse_tpex(d, iso):
    if str(d.get('stat', '')).lower() != 'ok':
        raise FetchError('TPEx stat=%s' % d.get('stat'))
    tabs = d.get('tables') or []
    if not tabs:
        raise Structure('TPEx: 沒有 tables')
    t = tabs[0]
    data = t.get('data') or []
    if not data:
        return None
    if t.get('date') and to_iso(t['date']) != iso:
        raise Structure('TPEx 回傳日期 %s 與要求 %s 不符' % (t.get('date'), iso))
    f = t['fields']
    ic, inm = col(f, '代號'), col(f, '名稱')
    iv, io, ih, il, icl = col(f, '成交股數'), col(f, '開盤'), col(f, '最高'), col(f, '最低'), col(f, '收盤')
    rows = []
    for r in data:
        code, name = str(r[ic]).strip(), str(r[inm]).strip()
        if not keep(code, name):
            continue
        o, h, l, c, v = num(r[io]), num(r[ih]), num(r[il]), num(r[icl]), num(r[iv])
        if None in (o, h, l, c, v) or v <= 0 or c <= 0:
            continue
        rows.append([code, name, o, h, l, c, int(v)])
    if not rows:
        raise Structure('TPEx: 表存在但沒有可用資料列')
    return rows


def fetch_twse(iso):
    return get(TWSE_DAY.format(ymd=iso.replace('-', '')), parse_twse)


def fetch_tpex(iso):
    return get(TPEX_DAY.format(slash=iso.replace('-', '%2F')), lambda d: parse_tpex(d, iso))


# ---------- cache ----------
def cpath(mkt, iso):
    return os.path.join(CACHE, mkt, iso.replace('-', '') + '.json')


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:               # 防毒軟體偶爾會暫時鎖住剛寫好的檔案
            if i == 4:
                raise
            _sleep(0.3)


def write_day(mkt, iso, rows):
    obj = {'date': iso, 'rows': rows or []}
    if rows is None:
        obj['holiday'] = True
    write_atomic(cpath(mkt, iso), json.dumps(obj, ensure_ascii=False, separators=(',', ':')))


def read_day(mkt, iso):
    try:
        with open(cpath(mkt, iso), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fetch_day(iso, today):
    """回傳 ok / skipped / holiday / empty / tpex-missing"""
    tw = read_day('twse', iso)
    new = False
    if tw is None:
        rows = fetch_twse(iso)
        if rows is None:
            if iso < today:
                write_day('twse', iso, None)
                write_day('tpex', iso, None)
                return 'holiday'
            return 'empty'
        write_day('twse', iso, rows)
        new = True
    elif tw.get('holiday'):
        if read_day('tpex', iso) is None:
            write_day('tpex', iso, None)
        return 'skipped'
    if read_day('tpex', iso) is None:
        rows = fetch_tpex(iso)
        if rows is None:
            return 'tpex-missing'
        write_day('tpex', iso, rows)
        new = True
    return 'ok' if new else 'skipped'


def weekdays(a, b):
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d.isoformat()
        d += dt.timedelta(days=1)


def plan_days(start, end, sparse):
    """回傳 (每天都抓的日期, 只抓週線用的週)。sparse：最近 DENSE_WEEKS 週每天抓；
    更早的每週一組（週五在前往週一排），只需要那週最後一個有開市的日子"""
    if not sparse:
        return list(weekdays(start, end)), []
    cut = end - dt.timedelta(weeks=DENSE_WEEKS)
    cut -= dt.timedelta(days=cut.weekday())               # 對齊到週一
    if cut <= start:
        return list(weekdays(start, end)), []
    weeks = []
    mon = start - dt.timedelta(days=start.weekday())
    while mon < cut:
        wk = [d.isoformat() for d in (mon + dt.timedelta(days=i) for i in range(5)) if start <= d < cut]
        if wk:
            weeks.append(wk[::-1])
        mon += dt.timedelta(days=7)
    return list(weekdays(cut, end)), weeks


def _day_cached(iso):
    return os.path.exists(cpath('twse', iso)) and os.path.exists(cpath('tpex', iso))


def _is_holiday(iso):
    """快取裡這天是休市記號（檔案很小才讀）"""
    p = cpath('twse', iso)
    try:
        if os.path.getsize(p) >= 200:
            return False
        with open(p, encoding='utf-8') as f:
            return bool(json.load(f).get('holiday'))
    except (OSError, ValueError, AttributeError):
        return False


def _week_cached(wk):
    """由週五往前：遇到休市記號就看前一天；第一個有開市的日子已經在快取就算這週完成"""
    for iso in wk:
        if not _day_cached(iso):
            return False
        if not _is_holiday(iso):
            return True
    return True


# ---------- 除權息 ----------
def exr_path(mkt, ym):
    return os.path.join(CACHE, 'exright', '%s_%s.json' % (mkt, ym))


def parse_ex(d, mkt):
    if mkt == 'twse':
        if NODATA in str(d.get('stat', '')):
            return []
        if not str(d.get('stat', '')).startswith('OK'):
            raise FetchError('TWT49U stat=' + str(d.get('stat')))
        f, data = d['fields'], d.get('data') or []
        idt, ic, ip, ir = col(f, '資料日期'), col(f, '股票代號'), col(f, '除權息前收盤價'), col(f, '除權息參考價')
    else:
        if str(d.get('stat', '')).lower() != 'ok':
            raise FetchError('exDailyQ stat=' + str(d.get('stat')))
        tabs = d.get('tables') or []
        if not tabs:
            raise Structure('exDailyQ: 沒有 tables')
        f, data = tabs[0]['fields'], tabs[0].get('data') or []
        idt, ic, ip, ir = col(f, '除權息日期'), col(f, '代號'), col(f, '除權息前收盤價'), col(f, '除權息參考價')
    out = []
    for r in data:
        iso = to_iso(r[idt])
        if not iso:
            raise Structure('除權息日期格式不認得: %r' % r[idt])
        out.append([str(r[ic]).strip(), iso, num(r[ip]), num(r[ir])])
    return out


def exright_jobs(start, end):
    """要抓的除權息檔：[(市場, 年月, 網址, 檔案路徑)]；當月與上月每次都重抓，其他月份有檔就略過"""
    today = dt.date.today()
    cur = today.strftime('%Y%m')
    prev = (today.replace(day=1) - dt.timedelta(days=1)).strftime('%Y%m')
    jobs = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        ym = '%04d%02d' % (y, m)
        first = dt.date(y, m, 1)
        last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        last = min(last, today)
        if first <= today:
            for mkt in ('twse', 'tpex'):
                p = exr_path(mkt, ym)
                if os.path.exists(p) and ym not in (cur, prev):
                    continue
                if mkt == 'twse':
                    url = TWSE_EX.format(s=first.strftime('%Y%m%d'), e=last.strftime('%Y%m%d'))
                else:
                    url = TPEX_EX.format(s=first.strftime('%Y/%m/%d').replace('/', '%2F'),
                                         e=last.strftime('%Y/%m/%d').replace('/', '%2F'))
                jobs.append((mkt, ym, url, p))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return jobs


def fetch_exright(job):
    mkt, ym, url, p = job
    ev = get(url, lambda d: parse_ex(d, mkt))
    write_atomic(p, json.dumps(ev, ensure_ascii=False, separators=(',', ':')))
    log('exright %s %s: %d events' % (mkt, ym, len(ev)))


def sync_exright(start, end):
    """依序抓 start~end 涵蓋月份的除權息（每日更新用）"""
    for job in exright_jobs(start, end):
        fetch_exright(job)


def load_events():
    ev, seen = {}, set()
    for p in sorted(glob.glob(os.path.join(CACHE, 'exright', '*.json'))):
        with open(p, encoding='utf-8') as f:
            for code, iso, prev, ref in json.load(f):
                if not prev or not ref or prev <= 0:
                    continue
                fac = ref / prev
                if not 0 < fac <= 1.05:
                    continue
                if (code, iso) in seen:
                    continue
                seen.add((code, iso))
                ev.setdefault(code, []).append((iso, fac))
    for v in ev.values():
        v.sort(reverse=True)
    return ev


# ---------- 重建 data/*.csv ----------
def build():
    bars, dates = {}, set()
    for mkt in ('twse', 'tpex'):
        for p in sorted(glob.glob(os.path.join(CACHE, mkt, '*.json'))):
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            if d.get('holiday'):
                continue
            dates.add(d['date'])
            for code, name, o, h, l, c, v in d['rows']:
                bars.setdefault(code, []).append((d['date'], o, h, l, c, v, name))
    if not dates:
        raise UserError(E_NEED_DATA, *MSG_NEED)
    latest = max(dates)
    ev = load_events()
    os.makedirs(DATA, exist_ok=True)
    written = set()
    for code, bs in bars.items():
        bs.sort()
        if bs[-1][0] != latest:
            continue
        name = bs[-1][6]
        e = ev.get(code, [])
        out, k, cum = [], 0, 1.0
        # 由新往舊走，遇到日期晚於該根 K 的除權息事件就累乘
        for b in reversed(bs):
            while k < len(e) and e[k][0] > b[0]:
                cum *= e[k][1]
                k += 1
            out.append([b[0], round(b[1] * cum, 4), round(b[2] * cum, 4), round(b[3] * cum, 4),
                        round(b[4] * cum, 4), b[5], b[4], name])
        out.reverse()
        path = os.path.join(DATA, code + '.csv')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'name'])
            w.writerows(out)
        os.replace(tmp, path)
        written.add(code)
    removed = 0
    for p in glob.glob(os.path.join(DATA, '*.csv')):
        code = os.path.splitext(os.path.basename(p))[0]
        if KEEP.match(code) and code not in written:
            os.remove(p)
            removed += 1
    write_atomic(os.path.join(CACHE, 'latest.txt'), latest.replace('-', ''))
    log('build: %d stocks, latest %s, removed stale %d' % (len(written), latest, removed))


# ---------- 類股（產業別）快取：cache/industry/twse.json、tpex.json ----------
def ind_path(mkt):
    return os.path.join(CACHE, 'industry', mkt + '.json')


def read_industry(mkt):
    """讀快取；沒有檔案或讀不懂回傳 None"""
    try:
        with open(ind_path(mkt), encoding='utf-8') as f:
            d = json.load(f)
        if d['v'] != 1 or not isinstance(d['rows'], dict):
            return None
        return d
    except (OSError, ValueError, KeyError, TypeError):
        return None


def parse_industry(d):
    """公司基本資料 -> {代號: 原始產業別}；只留 4 碼一般股，其他存證等不要"""
    if not isinstance(d, list):
        raise Structure('industry: not a list')
    rows = {}
    for x in d:
        if not isinstance(x, dict):
            continue
        c = next((x[k] for k in IND_CODE_KEYS if isinstance(x.get(k), str) and x[k].strip()), '')
        v = next((x[k] for k in IND_KEYS if isinstance(x.get(k), str) and x[k].strip()), '')
        c, v = c.strip(), v.strip()
        if re.fullmatch(r'[1-9][0-9]{3}', c) and re.fullmatch(r'[0-9]{1,2}|[\u4e00-\u9fff]{2,12}', v):
            rows[c] = v
    if len(rows) < IND_MIN:
        raise Structure('industry: only %d usable rows' % len(rows))
    return rows


def industry_due(old, today):
    """今天要不要抓：沒有檔案或讀不懂要抓；成功後每 IND_DAYS 天一次；失敗後 1、2、4、7 天再試。同一天最多試一次"""
    if not old:
        return True
    try:
        tried = dt.date.fromisoformat(old['tried'])
        wait = IND_DAYS
        if old.get('stale'):
            wait = min(IND_DAYS, 2 ** max(0, int(old.get('fails') or 0) - 1))
        return (today - tried).days >= wait
    except (KeyError, ValueError, TypeError):
        return True


def refresh_industry(today=None):
    """更新類股快取。除了 KeyboardInterrupt，絕不丟例外；失敗只提醒"""
    today = today or dt.date.today()
    iso = today.isoformat()
    for mkt, label, url in IND_SITES:
        try:
            old = read_industry(mkt)
            if not industry_due(old, today):
                continue
            try:
                rows = get(url, parse_industry, retry=())
                new = {'v': 1, 'rows': rows, 'fetched': iso, 'tried': iso, 'stale': False, 'fails': 0}
                note = '類股資料已更新：%s %d 家' % (label, len(rows))
            except _Aborted:
                raise
            except Exception as e:
                new = {'v': 1, 'rows': (old or {}).get('rows') or {}, 'fetched': (old or {}).get('fetched') or '',
                       'tried': iso, 'stale': True, 'fails': int((old or {}).get('fails') or 0) + 1}
                note = '注意：%s的類股資料這次沒有更新（%s），先沿用上次的資料；股價與選股不受影響。' % (label, str(e)[:80])
            write_atomic(ind_path(mkt), json.dumps(new, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
            log(note)
        except KeyboardInterrupt:
            raise
        except Exception:
            log('注意：%s的類股資料這次沒有更新，股價與選股不受影響。' % label)


# ---------- 指令 ----------
def parse_ymd(s):
    return dt.datetime.strptime(s, '%Y%m%d').date()


def _site_cause(site_days):
    days = '，沒抓到：' + fmt_days(site_days) if site_days else ''
    return MSG_SITE[0].format(days=days), MSG_SITE[1]


def _incomplete(site_days, nodata):
    """資料不完整時丟出對應的 UserError；都沒問題就什麼都不做"""
    if site_days:
        raise UserError(E_SITE, *_site_cause(site_days))
    if nodata:
        raise UserError(E_NODATA, MSG_NODATA[0].format(days=fmt_days(nodata)), MSG_NODATA[1])


def _fmt_dur(s):
    s = int(round(s))
    return '%d 分 %d 秒' % (s // 60, s % 60) if s >= 60 else '%d 秒' % s


def run_jobs(jobs):
    """用 min(WORKERS, 工作數) 條背景執行緒做完 jobs（每個是不帶參數的函式）；回傳執行緒裡丟出的非預期例外。
    Ctrl+C 時立刻通知大家停止，最多等 3 秒再把 KeyboardInterrupt 丟出去"""
    q = queue.Queue()
    for j in jobs:
        q.put(j)
    errs = []

    def worker():
        while not STOP.is_set():
            try:
                j = q.get_nowait()
            except queue.Empty:
                return
            try:
                j()
            except _Aborted:
                return
            except BaseException as e:
                errs.append(e)
                STOP.set()
                return

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(min(WORKERS, len(jobs)))]
    _threads.extend(ts)
    for t in ts:
        t.start()
    try:
        for t in ts:
            while t.is_alive():
                t.join(0.5)
    except KeyboardInterrupt:
        STOP.set()
        end = time.monotonic() + 3
        for t in ts:
            t.join(max(0.0, end - time.monotonic()))
        raise
    return errs


def run_backfill(start, end, sparse):
    _reset_net()
    today_iso = dt.date.today().isoformat()
    days, weeks = plan_days(start, end, sparse)
    wk_todo = [wk for wk in weeks if not _week_cached(wk)]
    d_todo = [iso for iso in days if not _day_cached(iso)]
    ex_todo = exright_jobs(start, end)
    total = len(wk_todo) + len(d_todo)
    cached = len(days) + len(weeks) - total
    if weeks:
        log('backfill %s ~ %s, %d weekdays + %d weeks (weekly only)' % (start, end, len(days), len(weeks)))
    else:
        log('backfill %s ~ %s, %d weekdays' % (start, end, len(days)))
    est = max(total + sum(1 for j in ex_todo if j[0] == m) for m in ('twse', 'tpex')) * GAP
    log('要抓 %d 天、除權息 %d 個檔（已有快取 %d 天），預估約 %d 分鐘；中途關掉視窗也沒關係，再執行一次會接著抓。'
        % (total, len(ex_todo), cached, max(1, int((est + 59) // 60))))

    lock = threading.Lock()                      # 保護下面的共用狀態；拿著它的時候不可以呼叫 log()
    st = dict(done=0, units=0.0, cnt={}, kinds=set(), site_days=[], nodata=[], exr_err=[])
    total_units = total + 0.5 * len(ex_todo)
    t0 = _now()

    def eta():                                   # 呼叫時要拿著 lock
        u = st['units']
        s = (_now() - t0) / u * (total_units - u) if u > 0 else 0
        return '不到 1 分鐘' if s < 60 else _fmt_dur(s)

    def failed(label, e):
        with lock:
            st['kinds'].add(e.kind)
            if e.kind != 'network':
                st['site_days'].append(label)
        log('ERROR', label, str(e)[:200])

    def finish(label, status, suffix=''):
        with lock:
            st['done'] += 1
            st['units'] += 1
            st['cnt'][status] = st['cnt'].get(status, 0) + 1
            if status == 'tpex-missing':
                st['nodata'].append(label)
            n, left = st['done'], eta()
        if status != 'skipped':
            log('[%d/%d] %s %s%s  剩約 %s' % (n, total, label, status, suffix, left))

    def day_job(iso):
        def run():
            try:
                s = fetch_day(iso, today_iso)
            except _Aborted:
                raise
            except FetchError as e:
                failed(iso, e)
                s = 'error'
            finish(iso, s)
        return run

    def week_job(wk):
        def run():
            s, label = 'holiday', wk[0]
            try:
                for iso in wk:
                    label = iso
                    r = fetch_day(iso, today_iso)
                    if r == 'holiday' or (r == 'skipped' and _is_holiday(iso)):
                        continue
                    s = r
                    break
            except _Aborted:
                raise
            except FetchError as e:
                failed(label, e)
                s = 'error'
            finish(label, s, '（週線用）')
        return run

    def ex_job(job):
        def run():
            try:
                fetch_exright(job)
            except _Aborted:
                raise
            except FetchError as e:
                with lock:
                    st['exr_err'].append(e)
                failed('exright %s %s' % job[:2], e)
            with lock:
                st['units'] += 0.5
        return run

    jobs = [week_job(wk) for wk in wk_todo] + [day_job(iso) for iso in d_todo] + [ex_job(j) for j in ex_todo]
    try:
        errs = run_jobs(jobs)
    except KeyboardInterrupt:
        log('已中斷，已經抓到的資料會保留；再執行一次會接著抓。')
        raise
    if errs:
        raise errs[0]
    if 'network' in st['kinds']:
        raise UserError(E_NETWORK, *MSG_NETWORK)
    if _blocks[0] >= BLOCK_LIMIT:
        raise UserError(E_SITE, *_site_cause(sorted(st['site_days'])))
    if st['exr_err']:
        raise st['exr_err'][0]
    build()
    log('summary', st['cnt'])
    n = lambda name: _hosts[name].count if name in _hosts else 0
    log('耗時 %s；請求：證交所 %d、櫃買中心 %d；重試 %d 次'
        % (_fmt_dur(_now() - t0), n('www.twse.com.tw'), n('www.tpex.org.tw'), sum(h.retries for h in _hosts.values())))
    refresh_industry()
    _incomplete(st['site_days'], st['nodata'])
    return 0


def cmd_backfill(a):
    today = dt.date.today()
    end = parse_ymd(a.end) if a.end else today
    start = parse_ymd(a.start) if a.start else today - dt.timedelta(days=SPAN_DAYS)
    return run_backfill(start, end, sparse=not a.start)       # 明確指定 --start：每個交易日都抓


def cmd_daily(a):
    _reset_net()
    today = dt.date.today()
    cached = [os.path.basename(p)[:8] for m in ('twse', 'tpex') for p in glob.glob(os.path.join(CACHE, m, '*.json'))]
    if not cached:
        raise UserError(E_NEED_DATA, *MSG_NEED)
    last = parse_ymd(max(cached))
    start = last - dt.timedelta(days=14)
    deadline = dt.datetime.combine(today, dt.time(16, 0))
    open_t = dt.datetime.combine(today, dt.time(13, 30))
    kinds, site_days, nodata = set(), [], []
    for iso in weekdays(start, today):
        if STOP.is_set():
            break
        while True:
            try:
                st = fetch_day(iso, today.isoformat())
            except FetchError as e:
                st = 'error'
                kinds.add(e.kind)
                if e.kind != 'network':
                    site_days.append(iso)
                log('ERROR', iso, str(e)[:200])
            if iso != today.isoformat() or st not in ('empty', 'tpex-missing'):
                break
            now = dt.datetime.now()
            if now < open_t:
                log('今天還沒收盤或資料還沒公布（建議 14:30 之後再執行），這次先用前一個交易日的資料。')
                break
            if now >= deadline:
                break
            log('官方還沒公布 %s 的資料（%s），10 分鐘後自動再試，最晚等到 16:00；不想等可以先關掉視窗，晚點再執行。' % (iso, st))
            time.sleep(600)
        if 'network' in kinds:
            break
        if st != 'skipped':
            log(iso, st)
        if st == 'empty':
            if dt.datetime.now() >= deadline:
                log('官方今天沒有資料，視為休市。')
        elif st == 'tpex-missing':
            nodata.append(iso)
    if 'network' in kinds:
        raise UserError(E_NETWORK, *MSG_NETWORK)
    if site_days or nodata:
        log('資料不完整，這次不重建 data/。')
        _incomplete(site_days, nodata)
    sync_exright(start, today)
    build()
    refresh_industry(today)
    return 0


def cmd_build(a):
    build()
    return 0


def main():
    ap = argparse.ArgumentParser(description='抓台股全市場日 K，重建 data/*.csv')
    sp = ap.add_subparsers(dest='cmd', required=True)
    b = sp.add_parser('backfill', help='補歷史（可續跑）',
                      description='不加 --start：從今天往前 %d 天，最近 %d 週每天抓、更早的每週只抓最後一個交易日（快）；'
                                  '加了 --start：從那天起每個交易日都抓（慢，約 38 分鐘）。' % (SPAN_DAYS, DENSE_WEEKS))
    b.add_argument('--start', help='YYYYMMDD；有指定就每個交易日都抓')
    b.add_argument('--end', help='YYYYMMDD；預設今天')
    sp.add_parser('daily', help='每日增量')
    sp.add_parser('build', help='用 cache 重建 data/*.csv（離線）')
    a = ap.parse_args()
    try:
        sys.exit({'backfill': cmd_backfill, 'daily': cmd_daily, 'build': cmd_build}[a.cmd](a))
    except UserError as e:
        fail(e.code, e.cause, e.todo)
    except Structure as e:
        log('FAIL:', e)
        fail(E_SITE, *MSG_FORMAT)
    except FetchError as e:
        log('FAIL:', e)
        if e.kind == 'network':
            fail(E_NETWORK, *MSG_NETWORK)
        fail(E_SITE, *_site_cause([]))
    except Exception as e:
        text = traceback.format_exc()
        print(text, flush=True, end='')
        _tee(text.rstrip())
        fail(1, '程式發生未預期的錯誤：%s: %s' % (type(e).__name__, e),
             '請把上面的錯誤訊息（或 logs 資料夾裡今天的紀錄檔）貼到 GitHub 專案的 Issues 回報。')


if __name__ == '__main__':
    main()
